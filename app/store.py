"""Temporary upload store (TTL'd per-upload directories) plus an LRU of decoded images.

Layout: ``<root>/<id>/source`` (the uploaded bytes, untouched), ``<root>/<id>/meta.json`` and
optional auxiliary cache files (e.g. rendered thumbnails). Ids are ``uuid4().hex`` and are
validated against ``^[0-9a-f]{32}$`` before any filesystem access. Removal first renames the
directory to a ``.del-*`` tombstone, so a write racing with it can't leave files behind.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from app.imaging.io import Loaded

ID_RE = re.compile(r"^[0-9a-f]{32}$")
TOMBSTONE_PREFIX = ".del-"
_AUX_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SOURCE_NAME = "source"
META_NAME = "meta.json"
CHUNK_SIZE = 1024 * 1024
_NO_SPACE = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
_ILLEGAL = re.compile(r'[<>:"/\\|?*]')


class StoreError(Exception):
    """Store failure carrying the HTTP status the API should answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class NotFound(StoreError):
    def __init__(self, detail: str = "File not found or expired") -> None:
        super().__init__(404, detail)


@dataclass(frozen=True)
class StoredFile:
    id: str
    name: str
    size_bytes: int
    created: float
    path: Path  # the source bytes
    width: int | None = None   # decoded (oriented) size and format, recorded after a successful upload
    height: int | None = None
    format: str | None = None


def is_valid_id(value: object) -> bool:
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def clean_name(name: str | None, default: str = "image", max_len: int = 150) -> str:
    """Make a user-supplied file name safe: no path parts, control chars or reserved names."""
    if not name:
        return default
    name = re.split(r"[/\\]", str(name))[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    name = _ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            name = stem[: max_len - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            name = name[:max_len].rstrip(" .")
    if not name:
        return default
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name


def _human_size(n: int) -> str:
    if n >= 1024 * 1024 and n % (1024 * 1024) == 0:
        return f"{n // (1024 * 1024)} MB"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(1, round(n / 1024))} KB"


def pixel_bytes(img: object) -> int:
    """Memory Pillow holds for an image: 1 byte/px for 1/L/P, 2 for I;16, else 4 (RGB is padded to 4)."""
    try:
        mode, pixels = str(img.mode), int(img.width) * int(img.height)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return 0
    if mode in ("1", "L", "P"):
        return pixels
    return pixels * (2 if mode.startswith("I;16") else 4)


class FileStore:
    """Per-upload directories under ``root`` that expire after ``ttl`` seconds of inactivity.

    The decoded-image LRU is bounded by ``cache_items`` and by ``cache_bytes`` of pixel memory; an
    image larger than the whole budget is not cached. ``on_remove(id)`` runs whenever an upload goes
    away (delete, expiry, wipe) so derived in-memory caches can be dropped too. ``pinned(ids)`` keeps
    uploads from expiring while a long job still needs them.
    """

    def __init__(self, root: str | os.PathLike[str], ttl: float, *, cache_items: int = 6,
                 cache_bytes: int = 512 * 1024 * 1024,
                 on_remove: Callable[[str], None] | None = None) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be > 0 seconds")
        self.root = Path(root)
        self.ttl = float(ttl)
        self.cache_items = max(0, int(cache_items))
        self.cache_bytes = max(0, int(cache_bytes))
        self.on_remove = on_remove
        self._cache: OrderedDict[str, Loaded] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._bytes = 0
        self._pins: Counter[str] = Counter()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def init(self) -> None:
        """Create the root (private) and remove anything left from a previous run."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.wipe()

    def wipe(self) -> int:
        """Delete every upload directory. Only id-named entries are touched, never anything else."""
        with self._lock:
            self._cache.clear()
            self._sizes.clear()
            self._bytes = 0
        removed = 0
        if not self.root.is_dir():
            return 0
        for entry in self.root.iterdir():
            if ID_RE.fullmatch(entry.name):
                self._discard(entry.name)
                removed += 1
            elif entry.name.startswith(TOMBSTONE_PREFIX):
                self._remove_dir(entry)
        return removed

    def cleanup_expired(self, now: float | None = None) -> list[str]:
        """Delete uploads idle for longer than the TTL (pinned ones excepted); returns the removed ids."""
        now = time.time() if now is None else now
        removed: list[str] = []
        if not self.root.is_dir():
            return removed
        for entry in self.root.iterdir():
            if entry.name.startswith(TOMBSTONE_PREFIX):
                self._remove_dir(entry)  # left over from an interrupted delete
            elif ID_RE.fullmatch(entry.name) and not self.is_pinned(entry.name) \
                    and now - self._last_access(entry) > self.ttl:
                if self._discard(entry.name):
                    removed.append(entry.name)
        return removed

    # ------------------------------------------------------------------ files
    def save(self, name: str | None, fileobj: BinaryIO, *, max_bytes: int) -> StoredFile:
        """Stream ``fileobj`` to disk in chunks. Raises StoreError 400 (empty), 413 (over
        ``max_bytes``; the partial file is removed) or 507 (disk full)."""
        file_id = uuid.uuid4().hex
        directory = self.root / file_id
        safe_name = clean_name(name)
        created = time.time()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            directory.mkdir(mode=0o700)
            written = 0
            with open(directory / SOURCE_NAME, "wb") as out:
                while chunk := fileobj.read(CHUNK_SIZE):
                    written += len(chunk)
                    if written > max_bytes:
                        raise StoreError(413, f"File exceeds the {_human_size(max_bytes)} upload limit")
                    out.write(chunk)
            if written == 0:
                raise StoreError(400, "File is empty")
            meta = {"id": file_id, "name": safe_name, "size_bytes": written, "created": created}
            self._write_json(directory / META_NAME, meta)
        except OSError as exc:
            self._remove_dir(directory)
            if exc.errno in _NO_SPACE:
                raise StoreError(507, "Server storage full") from None
            raise
        except BaseException:
            self._remove_dir(directory)
            raise
        return StoredFile(file_id, safe_name, written, created, directory / SOURCE_NAME)

    def get(self, file_id: str) -> StoredFile:
        """Metadata for ``file_id``; refreshes its inactivity timer. Raises NotFound."""
        directory = self._dir(file_id)
        meta_path = directory / META_NAME
        try:
            mtime = meta_path.stat().st_mtime
            if time.time() - mtime > self.ttl and not self.is_pinned(file_id):
                self.delete(file_id)
                raise NotFound()
            meta = json.loads(meta_path.read_text("utf-8"))
            os.utime(meta_path)
        except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
            raise NotFound() from None
        width, height, fmt = meta.get("width"), meta.get("height"), meta.get("format")
        return StoredFile(file_id, str(meta.get("name") or "image"), int(meta.get("size_bytes", 0)),
                          float(meta.get("created", mtime)), directory / SOURCE_NAME,
                          int(width) if width else None, int(height) if height else None,
                          str(fmt) if fmt else None)

    def set_image_info(self, file_id: str, *, width: int, height: int, fmt: str) -> None:
        """Record the decoded size and format (lets jobs be planned without decoding)."""
        meta_path = self._dir(file_id) / META_NAME
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            self._write_json(meta_path, {**meta, "width": int(width), "height": int(height), "format": fmt})
        except (OSError, json.JSONDecodeError):
            pass  # deleted meanwhile; nothing to record

    def touch(self, file_id: str) -> None:
        self.get(file_id)

    def exists(self, file_id: str) -> bool:
        return is_valid_id(file_id) and (self.root / file_id / META_NAME).is_file()

    def delete(self, file_id: str) -> bool:
        """Remove an upload and its cached decode. Returns False if it did not exist."""
        self._dir(file_id)  # validates the id
        return self._discard(file_id)

    @contextlib.contextmanager
    def pinned(self, file_ids: Iterable[str]) -> Iterator[None]:
        """Keep ``file_ids`` from expiring while a job runs; their idle timers restart when it ends."""
        ids = list(file_ids)
        with self._lock:
            self._pins.update(ids)
        try:
            yield
        finally:
            with self._lock:
                self._pins.subtract(ids)
                for file_id in ids:
                    if self._pins[file_id] <= 0:
                        del self._pins[file_id]
            for file_id in ids:
                with contextlib.suppress(OSError, StoreError):
                    os.utime(self._dir(file_id) / META_NAME)

    def is_pinned(self, file_id: str) -> bool:
        with self._lock:
            return self._pins[file_id] > 0

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(e.name for e in self.root.iterdir() if ID_RE.fullmatch(e.name))

    # ------------------------------------------------------------------ aux cache files
    def read_aux(self, file_id: str, name: str) -> bytes | None:
        try:
            return (self._dir(file_id) / self._aux(name)).read_bytes()
        except OSError:
            return None

    def write_aux(self, file_id: str, name: str, data: bytes) -> None:
        """Best effort: a failed write (e.g. disk full, or the upload was deleted) is ignored."""
        directory = self._dir(file_id)
        if not directory.is_dir():
            return
        target = directory / self._aux(name)
        tmp = target.with_name(target.name + f".{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------ decoded-image LRU
    def load(self, file_id: str) -> Loaded:
        """Decoded image for ``file_id`` (LRU-cached). Raises NotFound or io.ImageError."""
        stored = self.get(file_id)
        cached = self.cache_get(file_id)
        if cached is not None:
            return cached
        from app.imaging import io as imgio

        loaded = imgio.load(str(stored.path))
        self.cache_put(file_id, loaded)
        if not self.exists(file_id):  # deleted while decoding: _discard renames before it evicts
            self.evict(file_id)
        return loaded

    def cache_get(self, file_id: str) -> Loaded | None:
        with self._lock:
            loaded = self._cache.get(file_id)
            if loaded is not None:
                self._cache.move_to_end(file_id)
            return loaded

    def cache_put(self, file_id: str, loaded: Loaded) -> None:
        """Insert as most recent, then evict the oldest until within the item and byte budgets."""
        size = pixel_bytes(getattr(loaded, "image", None))
        with self._lock:
            self._pop(file_id)
            if self.cache_items <= 0 or size > self.cache_bytes or self.cache_bytes <= 0:
                return
            self._cache[file_id] = loaded
            self._sizes[file_id] = size
            self._bytes += size
            while len(self._cache) > self.cache_items or self._bytes > self.cache_bytes:
                self._pop(next(iter(self._cache)))

    def evict(self, file_id: str) -> None:
        with self._lock:
            self._pop(file_id)

    def cached_ids(self) -> list[str]:
        with self._lock:
            return list(self._cache)

    @property
    def cached_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def _pop(self, file_id: str) -> None:
        """Remove one cache entry; caller holds the lock."""
        self._cache.pop(file_id, None)
        self._bytes -= self._sizes.pop(file_id, 0)

    # ------------------------------------------------------------------ helpers
    def _discard(self, file_id: str) -> bool:
        """Remove one upload: tombstone rename (atomic), evict, notify, then delete the files.

        Renaming first means a concurrent ``write_aux`` can no longer create files in it, and a
        concurrent ``load``/pipeline cache put that re-checks ``exists`` afterwards drops its entry.
        """
        directory = self.root / file_id
        tomb = self.root / f"{TOMBSTONE_PREFIX}{file_id}-{uuid.uuid4().hex[:8]}"
        try:
            os.rename(directory, tomb)
        except FileNotFoundError:
            tomb = None
        except OSError:
            tomb = directory  # rename refused (odd filesystem): delete in place
        self.evict(file_id)
        self._removed(file_id)
        if tomb is None:
            return False
        self._remove_dir(tomb)
        return True

    def _removed(self, file_id: str) -> None:
        if self.on_remove is not None:
            self.on_remove(file_id)

    def _dir(self, file_id: str) -> Path:
        if not is_valid_id(file_id):
            raise NotFound()
        return self.root / file_id

    @staticmethod
    def _aux(name: str) -> str:
        if not _AUX_RE.fullmatch(name) or name in (SOURCE_NAME, META_NAME):
            raise ValueError(f"invalid auxiliary file name {name!r}")
        return name

    @staticmethod
    def _last_access(directory: Path) -> float:
        try:
            return (directory / META_NAME).stat().st_mtime
        except OSError:
            try:
                return directory.stat().st_mtime
            except OSError:
                return 0.0

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(data), "utf-8")
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _remove_dir(directory: Path) -> None:
        for _ in range(3):  # a writer that still holds an open file can make one pass miss it
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                return
