"""Lazy, thread-safe onnxruntime sessions for heyp's image models.

One :class:`ModelSession` per model file. ``available()`` is a cheap check (onnxruntime importable,
file present and plausibly sized) that never loads the model; ``get()`` creates the session once,
under a lock, on first use. A file that fails to load is remembered as broken so every later call
fails fast with the same ModelUnavailable (a RuntimeError) instead of re-reading hundreds of MB.

Thread count: ``HEYP_AI_THREADS`` (default ``min(4, CPUs available to this container)``). Sessions
never spin-wait between runs, so an idle model costs no CPU on a shared VPS.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import importlib.util
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_THREADS = 4  # measured: BiRefNet and Real-ESRGAN gain little above 4 intra-op threads


def models_dir() -> Path:
    return Path(os.environ.get("HEYP_MODELS_DIR", "/opt/heyp/models"))


def _cgroup_cpus() -> float | None:
    """CPU quota of this container (cgroup v2 ``cpu.max``), None when unlimited/unknown."""
    try:
        with open("/sys/fs/cgroup/cpu.max", encoding="ascii") as fh:
            quota, period = fh.read().split()[:2]
    except (OSError, ValueError):
        return None
    if quota == "max":
        return None
    try:
        return max(1.0, int(quota) / int(period))
    except (ValueError, ZeroDivisionError):
        return None


def available_cpus() -> int:
    """CPUs this process may use: affinity mask, capped by the cgroup quota."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    quota = _cgroup_cpus()
    if quota is not None:
        cpus = min(cpus, int(quota + 0.5))
    return max(1, cpus)


def ai_threads() -> int:
    """Intra-op threads per model run: HEYP_AI_THREADS, else min(4, available CPUs)."""
    raw = os.environ.get("HEYP_AI_THREADS", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value >= 1:
            return min(value, 64)
    return max(1, min(MAX_THREADS, available_cpus()))


class ModelUnavailable(RuntimeError):
    """The model file is missing or could not be loaded (HTTP 503 at the API)."""


_libc = None


def release_memory() -> None:
    """Hand freed heap pages back to the OS (glibc ``malloc_trim``); a no-op elsewhere.

    After a BiRefNet run this takes the process from its ~1.6 GB peak back to ~0.4 GB.
    """
    global _libc
    try:
        if _libc is None:
            name = ctypes.util.find_library("c")
            _libc = ctypes.CDLL(name) if name else False
        if _libc and hasattr(_libc, "malloc_trim"):
            _libc.malloc_trim(0)
    except Exception:  # not glibc / not Linux
        _libc = False


class ModelSession:
    """One ONNX model file, loaded on first use.

    ``low_memory`` turns off onnxruntime's CPU arena and memory-pattern planning: for BiRefNet that
    cuts the peak from 4.3 GB to 1.6 GB at the same speed (research measurement, ORT 1.31).
    ``serialize`` makes ``run`` hold a lock, so at most one inference of this model is in flight
    per process — the model's peak memory is then paid once, however many requests arrive.
    """

    def __init__(self, filename: str, *, min_bytes: int = 100_000, low_memory: bool = True,
                 serialize: bool = False, label: str | None = None) -> None:
        self.filename = filename
        self.min_bytes = min_bytes
        self.low_memory = low_memory
        self.label = label or filename
        self._lock = threading.Lock()
        self._run_lock = threading.Lock() if serialize else None
        self._session: Any = None
        self._failed: str | None = None

    @property
    def path(self) -> Path:
        return models_dir() / self.filename

    def available(self) -> bool:
        """Cheap check: no model load, just onnxruntime + a plausible file."""
        if self._session is not None:
            return True
        if self._failed is not None:
            return False
        if importlib.util.find_spec("onnxruntime") is None:
            return False
        try:
            return self.path.is_file() and self.path.stat().st_size >= self.min_bytes
        except OSError:
            return False

    def loaded(self) -> bool:
        return self._session is not None

    def get(self) -> Any:
        """The onnxruntime session; raises ModelUnavailable when the model is missing or broken."""
        session = self._session
        if session is not None:
            return session
        with self._lock:
            if self._session is None:
                if self._failed is not None:
                    raise ModelUnavailable(self._failed)
                if not self.available():
                    raise ModelUnavailable(f"the {self.label} model is not installed")
                import onnxruntime as ort

                opts = ort.SessionOptions()
                opts.intra_op_num_threads = ai_threads()
                opts.inter_op_num_threads = 1
                opts.log_severity_level = 3
                if self.low_memory:
                    opts.enable_cpu_mem_arena = False
                    opts.enable_mem_pattern = False
                opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
                try:
                    self._session = ort.InferenceSession(str(self.path), sess_options=opts,
                                                         providers=["CPUExecutionProvider"])
                except Exception as exc:  # truncated / incompatible file
                    self._failed = f"could not load the {self.label} model ({type(exc).__name__})"
                    raise ModelUnavailable(self._failed) from None
            return self._session

    def input_name(self) -> str:
        return self.get().get_inputs()[0].name

    def input_shape(self) -> list:
        return list(self.get().get_inputs()[0].shape)

    def run(self, feeds: Mapping[str, Any]) -> list:
        session = self.get()
        if self._run_lock is None:
            return session.run(None, dict(feeds))
        with self._run_lock:
            return session.run(None, dict(feeds))

    def unload(self) -> None:
        """Drop the session (frees its weights); the next ``get()`` loads it again."""
        with self._lock:
            self._session = None
            self._failed = None
        release_memory()


__all__ = ["ModelSession", "ModelUnavailable", "ai_threads", "available_cpus", "models_dir", "release_memory"]
