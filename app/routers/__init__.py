"""HTTP routers next to the image API (``app/api.py``): jobs, tools, config and the legal pages."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.errors import ToolError, error_info
from app.core.limits import ClientInfo, client_info


def client_of(request: Request) -> ClientInfo:
    return client_info(request, request.app.state.settings)


def error_response(exc: BaseException) -> JSONResponse:
    """``{"detail": …, **extra}`` with the exception's status (+ ``Retry-After`` for 429/503)."""
    info = error_info(exc) or {"status": 500, "detail": "Internal server error"}
    status = int(info.pop("status"))
    headers = None
    retry = getattr(exc, "retry_after", None)
    if retry is None and status in (429, 503) and isinstance(exc, ToolError):
        retry = 5
    if retry is not None:
        headers = {"Retry-After": str(int(retry))}
    return JSONResponse(info, status_code=status, headers=headers)
