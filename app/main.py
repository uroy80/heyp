"""FastAPI application factory: API routers, the static UI at ``/``, the job runner, the lifespan cleanup loop,
security headers, the hosted-mode policies and the ``/pdf/*`` redirect to heypdf (``HEYP_SIBLING_URL``)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import api
from app.config import VERSION, Settings, get_settings
from app.core import sandbox
from app.core.jobs import JobManager
from app.core.limits import Limiter
from app.imaging import io, pipeline
from app.store import FileStore

log = logging.getLogger("heyp")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CLEANUP_INTERVAL_SECONDS = 60
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
# FastAPI's interactive docs (HEYP_DOCS=1 only) load Swagger UI/ReDoc from a CDN with inline scripts.
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DEFAULT_PORTS = {"http": "80", "https": "443"}
CROSS_ORIGIN_DETAIL = "Cross-origin requests are not allowed"
JSON_BODY_MAX_BYTES = 2 * 1024 * 1024  # a ProcessRequest with 200 ids × 32 ops is well below this
# Powerful browser features are off everywhere.
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
# Downloads are attachments; a sandbox CSP also keeps an uploaded HTML/SVG from ever running as a page.
DOWNLOAD_PATH_RE = re.compile(r"^/api/files/[0-9a-f]{32}/download$")
SECURITY_HEADERS = {"content-security-policy": CSP, "x-content-type-options": "nosniff",
                    "referrer-policy": "no-referrer", "permissions-policy": PERMISSIONS_POLICY}
_HEADER_BYTES = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in SECURITY_HEADERS.items()]
_HEADER_NAMES = frozenset(k for k, _ in _HEADER_BYTES) | {b"cache-control"}
# The slim image has no /etc/mime.types: without these, fonts would be served as text/plain.
for _type, _ext in (("font/woff2", ".woff2"), ("font/woff", ".woff"), ("font/ttf", ".ttf"), ("font/otf", ".otf"),
                    ("text/javascript", ".mjs"), ("image/svg+xml", ".svg"), ("application/manifest+json", ".webmanifest")):
    mimetypes.add_type(_type, _ext)


def _netloc(value: str, scheme: str) -> str:
    """Lower-cased ``host[:port]`` with the scheme's default port dropped."""
    value = value.strip().lower()
    host, sep, port = value.rpartition(":")
    if sep and "]" not in port and port == _DEFAULT_PORTS.get(scheme):
        return host
    return value


def is_cross_origin(headers: Headers) -> bool:
    """True when a browser marked the request cross-site, or its Origin isn't this host.

    ``X-Forwarded-Host`` is accepted as an alias of Host (reverse proxies); browsers can't set it on
    a cross-site request without a CORS preflight, which heyp never grants.
    """
    if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return True
    origin = headers.get("origin")
    if origin is None:
        return False  # non-browser client (curl, scripts) or a same-origin GET-style navigation
    parts = urlsplit(origin.strip())
    if parts.scheme not in _DEFAULT_PORTS or not parts.netloc:
        return True  # "null" (sandboxed/file://) and other opaque origins
    allowed = {_netloc(h, parts.scheme) for h in (headers.get("host", ""),
                                                  headers.get("x-forwarded-host", "").split(",")[0]) if h.strip()}
    return _netloc(parts.netloc, parts.scheme) not in allowed


def _has_dot_segment(path: str) -> bool:
    return any(part.startswith(".") for part in path.split("/"))


class SecurityHeadersMiddleware:
    """Pure ASGI middleware: CSP, nosniff and no-referrer on every HTTP response, plus two guards.

    - State-changing requests from another origin get 403 unless ``allow_cross_origin``.
    - Paths with a dot segment (``/.DS_Store``, ``/.env``) are 404 and never reach the static files.
    Static UI files also get `Cache-Control: no-cache` (revalidate via ETag) so a browser never mixes
    ES modules from two heyp versions after an upgrade.
    """

    def __init__(self, app: ASGIApp, *, allow_cross_origin: bool = False, docs: bool = False,
                 hosted: bool = False) -> None:
        self.app = app
        self.allow_cross_origin = allow_cross_origin
        self.docs = docs
        self.hosted = hosted

    def _headers(self, path: str, existing: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        """Response headers with the security set applied for ``path``.

        - CSP everywhere (not on the opt-in API docs), plus ``sandbox`` on file downloads;
        - Permissions-Policy: every powerful feature off;
        - Cache-Control: hosted mode answers every API response with ``no-store`` (files, previews, jobs);
          static UI files revalidate (``no-cache``) unless the response set its own.
        """
        is_api = path.startswith("/api/") or path == "/openapi.json" or path in DOCS_PATHS
        cache = next((v for k, v in existing if k.lower() == b"cache-control"), None)
        headers = [(k, v) for k, v in existing if k.lower() not in _HEADER_NAMES]
        csp = CSP + "; sandbox" if DOWNLOAD_PATH_RE.match(path) else CSP
        if not (self.docs and path in DOCS_PATHS):
            headers.append((b"content-security-policy", csp.encode("latin-1")))
        headers += [(k, v) for k, v in _HEADER_BYTES if k != b"content-security-policy"]
        if is_api and self.hosted:
            cache = b"no-store"
        elif not is_api and cache is None:
            cache = b"no-cache"
        if cache is not None:
            headers.append((b"cache-control", cache))
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": self._headers(path, list(message.get("headers", [])))}
            await send(message)

        if _has_dot_segment(path):
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send_wrapper)
            return
        if (not self.allow_cross_origin and str(scope.get("method", "")).upper() in _UNSAFE_METHODS
                and is_cross_origin(Headers(scope=scope))):
            await JSONResponse({"detail": CROSS_ORIGIN_DETAIL}, status_code=403)(scope, receive, send_wrapper)
            return
        await self.app(scope, receive, send_wrapper)


class BodyLimitMiddleware:
    """413 for request bodies above a cap, before anything is parsed or spooled to disk.

    Multipart uploads may carry ``upload_bytes``; any other body (JSON) ``other_bytes``. A declared
    Content-Length is checked up front; chunked bodies are counted as they stream in.
    """

    def __init__(self, app: ASGIApp, *, upload_bytes: int, other_bytes: int = JSON_BODY_MAX_BYTES) -> None:
        self.app = app
        self.upload_bytes = upload_bytes
        self.other_bytes = other_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or str(scope.get("method", "")).upper() not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        multipart = headers.get("content-type", "").lower().startswith("multipart/")
        limit = self.upload_bytes if multipart else self.other_bytes
        detail = (f"Upload too large: one request may carry at most {limit // 2**20} MB; send fewer files at once"
                  if multipart else f"Request body too large (max {limit // 2**20} MB)")
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > limit:
            await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, detail)  # FastAPI re-raises it from body parsing as a 413
            return message

        await self.app(scope, counting_receive, send)


def sibling_redirect(sibling_url: str) -> Callable[[Request], Response]:
    """``/pdf`` and ``/pdf/<path>`` (heypdf's address when it was served from heyp) → 308 to the same path on
    heypdf (``HEYP_SIBLING_URL``), query string kept; 404 when no sibling is configured."""

    def redirect(request: Request) -> Response:
        if not sibling_url:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        rest = str(request.scope["path"]).removeprefix("/pdf").lstrip("/")
        query = request.url.query
        return RedirectResponse(f"{sibling_url}/{quote(rest)}" + (f"?{query}" if query else ""), status_code=308)

    return redirect


async def _cleanup_loop(store: FileStore, interval: float = CLEANUP_INTERVAL_SECONDS,
                        jobs: JobManager | None = None) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            removed = await asyncio.to_thread(store.cleanup_expired)  # on_remove drops analysis caches
            if removed:
                log.info("expired %d upload(s)", len(removed))
            if jobs is not None:
                jobs.cleanup()  # job records expire with their files
        except Exception:  # keep the loop alive; never log file names
            log.exception("upload cleanup failed")


def _validation_detail(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        msg = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "Invalid request"


class HealthCheckLogFilter(logging.Filter):
    """Drop access-log lines for ``/api/health``: the container HEALTHCHECK polls it every 30 s.

    In hosted mode (``drop_all``) every access-log line is dropped: the privacy policy promises the
    app keeps no request logs (client IPs, paths) — the reverse proxy's log policy applies instead.
    """

    drop_all = False

    def filter(self, record: logging.LogRecord) -> bool:
        if self.drop_all:
            return False
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 3:  # uvicorn: (client, method, path?query, http_version, status)
            return str(args[2]).split("?", 1)[0] != "/api/health"
        message = record.getMessage()
        return " /api/health " not in message and " /api/health?" not in message


_ACCESS_LOG = logging.getLogger("uvicorn.access")
if not any(isinstance(f, HealthCheckLogFilter) for f in _ACCESS_LOG.filters):
    _ACCESS_LOG.addFilter(HealthCheckLogFilter())


def set_access_log(hosted: bool) -> None:
    """Hosted mode: no access log at all (also when uvicorn runs without ``--no-access-log``)."""
    HealthCheckLogFilter.drop_all = hosted


def _mount_tool_routers(app: FastAPI) -> None:
    """Import every tool module (registers the tools) and mount the routers they expose under /api."""
    from app import tools

    errors = tools.load()
    for name, error in errors.items():
        log.error("tool module %s is unavailable: %s", name, error)
    for name, router in tools.routers():
        try:
            app.include_router(router, prefix="/api")
        except Exception:
            log.exception("router of %s could not be mounted", name)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. ``settings`` defaults to the environment (``get_settings()``)."""
    settings = settings or get_settings()
    store = FileStore(settings.tmp_dir, settings.ttl_seconds, cache_items=settings.cache_items,
                      cache_bytes=settings.cache_bytes, on_remove=pipeline.forget,
                      hard_ttl=settings.hard_ttl_seconds)
    slots = api.make_job_slots(settings)
    limiter = Limiter(settings)
    job_manager = JobManager(settings, store, limiter=limiter, slots=slots)
    set_access_log(settings.hosted)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Sandbox children run as this uid: keep secret-looking variables out of every environment they could see,
        # keep them out of this process's memory, and tell the sandbox which directory is off limits.
        sandbox.seal_secrets()
        sandbox.harden_process()
        sandbox.protect(store=store.root)
        io.register_plugins()
        store.init()
        pipeline.clear_caches()
        job_manager.start()
        task = asyncio.create_task(_cleanup_loop(store, jobs=job_manager))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.to_thread(job_manager.stop)
            await asyncio.to_thread(store.wipe)
            pipeline.clear_caches()
            sandbox.protect(store=None)

    docs = {} if settings.docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="heyp", version=VERSION, summary="Image toolbox", lifespan=lifespan, **docs)
    app.state.settings = settings
    app.state.store = store
    app.state.jobs = slots                # request admission (slots + memory budget), shared with heavy jobs
    app.state.limiter = limiter           # hosted-mode abuse limits (no-ops locally)
    app.state.job_manager = job_manager   # /api/jobs

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": _validation_detail(exc)}, status_code=422)

    @app.exception_handler(Exception)
    async def _on_unexpected(_request: Request, _exc: Exception) -> JSONResponse:
        # Starlette re-raises after this response, so the server still logs the traceback. This
        # handler runs outside the middleware stack, so the security headers are set here.
        return JSONResponse({"detail": "Internal server error"}, status_code=500, headers=SECURITY_HEADERS)

    from app.routers import config as config_routes, jobs as job_routes, legal as legal_routes, \
        tools as tool_routes

    app.add_middleware(BodyLimitMiddleware, upload_bytes=settings.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware, allow_cross_origin=settings.allow_cross_origin,
                       docs=settings.docs, hosted=settings.hosted)
    app.include_router(api.router)
    app.include_router(job_routes.router)
    app.include_router(tool_routes.router)
    app.include_router(config_routes.router)
    app.include_router(legal_routes.router)
    _mount_tool_routers(app)
    redirect = sibling_redirect(settings.sibling_url)
    for path in ("/pdf", "/pdf/{rest:path}"):  # heypdf moved to its own site
        app.add_api_route(path, redirect, methods=["GET", "HEAD"], include_in_schema=False)
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
