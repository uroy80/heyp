"""Runtime settings read from ``HEYP_*`` environment variables (PLAN.md §2)."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache

VERSION = "1.1.0"
MAX_DEFAULT_JOBS = 4  # each job on a max-size image can peak above 1 GB; more CPUs don't change that
DEFAULT_MAX_PIXELS = 50_000_000  # 4 bytes/px in Pillow → 200 MB per decoded copy; phones produce 12–48 MP
GIB = 1024 ** 3
UPLOAD_REQUEST_MIN_MB = 256      # one upload request may carry this much (the UI sends ≤ 200 MB chunks)
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _available_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))  # respects cpusets / taskset (Linux)
    except (AttributeError, OSError):
        return os.cpu_count() or 2


def _default_jobs() -> int:
    return max(2, min(MAX_DEFAULT_JOBS, _available_cpus()))


def memory_limit_bytes() -> int | None:
    """The container's memory limit (cgroup v2/v1), else physical memory; None if unknown."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path, encoding="ascii") as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 60:  # "max" / a huge v1 value mean "no limit"
            return int(raw)
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class Settings:
    """Immutable service configuration."""

    port: int = 8080
    max_upload_mb: int = 100
    max_files: int = 200
    max_pixels: int = DEFAULT_MAX_PIXELS
    ttl_minutes: int = 60
    tmp_dir: str = "/tmp/heyp"
    max_jobs: int = field(default_factory=_default_jobs)
    cache_items: int = 6
    cache_mb: int = 512
    job_memory_mb: int = 0  # memory budget shared by concurrent jobs; 0 = derive from the container limit
    models_dir: str = "/opt/heyp/models"
    preview_max: int = 1600
    allow_cross_origin: bool = False
    docs: bool = False

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_request_bytes(self) -> int:
        """Largest multipart upload body accepted before parsing (one file at the limit always fits)."""
        return (max(UPLOAD_REQUEST_MIN_MB, self.max_upload_mb) + 1) * 1024 * 1024

    @property
    def cache_bytes(self) -> int:
        return self.cache_mb * 1024 * 1024

    @property
    def job_memory_bytes(self) -> int:
        """Memory concurrent jobs may plan to use: HEYP_JOB_MEMORY_MB, else 60 % of the limit minus the cache."""
        if self.job_memory_mb > 0:
            return self.job_memory_mb * 1024 * 1024
        limit = memory_limit_bytes() or 4 * GIB
        return max(GIB, int(limit * 0.6) - self.cache_bytes)

    @property
    def ttl_seconds(self) -> int:
        return self.ttl_minutes * 60

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``env`` (defaults to ``os.environ``); raises ValueError on bad values."""
        env = os.environ if env is None else env
        d = cls()

        def num(name: str, default: int, minimum: int) -> int:
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                value = int(raw.strip().replace("_", ""))
            except ValueError:
                raise ValueError(f"{name} must be an integer, got {raw!r}") from None
            if value < minimum:
                raise ValueError(f"{name} must be >= {minimum}, got {value}")
            return value

        def text(name: str, default: str) -> str:
            raw = env.get(name)
            return raw.strip() if raw and raw.strip() else default

        def flag(name: str, default: bool) -> bool:
            raw = (env.get(name) or "").strip().lower()
            if not raw:
                return default
            if raw in _TRUE:
                return True
            if raw in _FALSE:
                return False
            raise ValueError(f"{name} must be 0 or 1, got {raw!r}")

        return cls(
            port=num("HEYP_PORT", d.port, 1),
            max_upload_mb=num("HEYP_MAX_UPLOAD_MB", d.max_upload_mb, 1),
            max_files=num("HEYP_MAX_FILES", d.max_files, 1),
            max_pixels=num("HEYP_MAX_PIXELS", d.max_pixels, 1),
            ttl_minutes=num("HEYP_TTL_MINUTES", d.ttl_minutes, 1),
            tmp_dir=text("HEYP_TMP_DIR", d.tmp_dir),
            max_jobs=num("HEYP_MAX_JOBS", d.max_jobs, 1),
            cache_items=num("HEYP_CACHE_ITEMS", d.cache_items, 0),
            cache_mb=num("HEYP_CACHE_MB", d.cache_mb, 0),
            job_memory_mb=num("HEYP_JOB_MEMORY_MB", d.job_memory_mb, 0),
            models_dir=text("HEYP_MODELS_DIR", d.models_dir),
            preview_max=num("HEYP_PREVIEW_MAX", d.preview_max, 16),
            allow_cross_origin=flag("HEYP_ALLOW_CROSS_ORIGIN", d.allow_cross_origin),
            docs=flag("HEYP_DOCS", d.docs),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings (read once; call ``get_settings.cache_clear()`` in tests)."""
    return Settings.from_env()
