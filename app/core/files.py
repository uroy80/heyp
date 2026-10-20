"""File kinds: magic-byte sniffing of uploads and job results.

``sniff(path, name)`` decides the kind from the bytes — the extension is only a hint used to tell apart
formats that share a container (TIFF-based RAW) and to recognise an HTML fragment without a doctype.

Upload kinds: ``image`` · ``html`` (the HTML → image tool) · ``other`` (refused at upload). Job results may
also be ``pdf`` (photos combined into one PDF) or ``other`` (a ZIP of several results); the tool names that kind.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

KINDS = ("image", "html", "other")
HEAD_BYTES = 8192
HTML_MAX_BYTES = 5 * 1024 * 1024

RAW_EXTS = {"dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2", "pef", "srw"}
MIME = {
    "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "tiff": "image/tiff", "ico": "image/x-icon", "jp2": "image/jp2", "heic": "image/heic", "avif": "image/avif",
    "psd": "image/vnd.adobe.photoshop", "psb": "image/vnd.adobe.photoshop", "svg": "image/svg+xml",
    "dng": "image/x-adobe-dng", "cr2": "image/x-canon-cr2", "cr3": "image/x-canon-cr3", "nef": "image/x-nikon-nef",
    "arw": "image/x-sony-arw", "raf": "image/x-fuji-raf", "orf": "image/x-olympus-orf", "rw2": "image/x-panasonic-rw2",
    "pef": "image/x-pentax-pef", "srw": "image/x-samsung-srw",
    "html": "text/html",
}
_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}
_BMP_DIB_SIZES = {12, 16, 40, 52, 56, 64, 108, 124}
_HTML_RE = re.compile(rb"^\s*(<!--.*?-->\s*)*<(!doctype\s+html|html[\s>]|head[\s>]|body[\s>])", re.I | re.S)


class FileKindError(Exception):
    """A file that must be refused at upload (status 413 / 415 / 422)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Sniffed:
    kind: str                 # one of KINDS
    format: str               # canonical short format ("jpeg", "png", "html", …)
    mime: str


def ext_of(name: str | None) -> str:
    stem, dot, ext = (name or "").rpartition(".")
    return ext.lower() if dot and stem is not None else ""


def _ftyp_brands(head: bytes) -> set[bytes]:
    if len(head) < 16 or head[4:8] != b"ftyp":
        return set()
    size = int.from_bytes(head[:4], "big")
    end = min(len(head), size if 16 <= size <= 4096 else 64)
    return {head[8:12]} | {head[i:i + 4] for i in range(16, end - 3, 4)}


def _tiff_based(head: bytes, ext: str) -> str | None:
    """TIFF and TIFF-container RAW formats (told apart by their own marks, else the extension)."""
    if head[:4] in (b"IIRO", b"IIRS", b"MMOR"):
        return "orf"
    if head[:4] == b"IIU\x00":
        return "rw2"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        if head[8:10] == b"CR":
            return "cr2"
        return ext if ext in RAW_EXTS else "tiff"
    return None


def _io_sniff(head: bytes) -> str | None:
    """The image decoder's own recogniser (SVG/SVGZ, PSD/PSB, RAW flavours), so upload and decode agree."""
    try:
        from app.imaging import io as imgio
    except Exception:  # pragma: no cover - the imaging package ships with the app
        return None
    try:
        return imgio.sniff(head)
    except Exception:
        return None


def _image_format(head: bytes, ext: str) -> str | None:
    found = _io_sniff(head)
    if found == "raw":
        found = ext if ext in RAW_EXTS else "dng"
    if found:
        return found
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM" and len(head) >= 26 and int.from_bytes(head[14:18], "little") in _BMP_DIB_SIZES:
        return "bmp"
    if head[:4] == b"8BPS":
        return "psb" if head[4:6] == b"\x00\x02" else "psd"
    if head[:16].startswith(b"FUJIFILMCCD-RAW"):
        return "raf"
    tiff = _tiff_based(head, ext)
    if tiff:
        return tiff
    if head[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if head[:12] == b"\x00\x00\x00\x0cjP  \r\n\x87\n" or head[:4] == b"\xff\x4f\xff\x51":
        return "jp2"
    brands = _ftyp_brands(head)
    if b"crx " in brands:
        return "cr3"
    if brands & _AVIF_BRANDS:
        return "avif"
    if brands & _HEIF_BRANDS:
        return "heic"
    return None


def _text_like(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 4:  # a multi-byte sequence cut at the end of the window is fine
            try:
                head.decode("cp1252")
            except UnicodeDecodeError:
                return False
    return True


def _strip_bom(head: bytes) -> bytes:
    return head[3:] if head.startswith(b"\xef\xbb\xbf") else head


def sniff(path: str | Path, name: str | None = None) -> Sniffed:
    """Decide a file's kind from its bytes (``name``'s extension only disambiguates)."""
    path = Path(path)
    ext = ext_of(name)
    with open(path, "rb") as fh:
        head = fh.read(HEAD_BYTES)

    fmt = _image_format(head, ext)
    if fmt:
        return Sniffed("image", fmt, MIME.get(fmt, "application/octet-stream"))
    text = _strip_bom(head)
    if _HTML_RE.match(text[:2048]) or (ext in ("html", "htm") and _text_like(head) and text.lstrip()[:1] == b"<"):
        if path.stat().st_size > HTML_MAX_BYTES:
            raise FileKindError(413, f"HTML files are limited to {HTML_MAX_BYTES // 2**20} MB")
        return Sniffed("html", "html", MIME["html"])
    return Sniffed("other", ext or "bin", "application/octet-stream")


# ------------------------------------------------------------------ upload classification
KIND_NAMES = {"image": "an image", "html": "an HTML file", "pdf": "a PDF",  # "pdf": combined-PDF export results
              "other": "a file of an unsupported type"}


def parse_accept(raw: str | None) -> frozenset[str] | None:
    """``?accept=image`` → the kinds an upload request takes (None = every supported kind)."""
    if raw is None or not raw.strip():
        return None
    kinds = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = sorted(kinds - set(KINDS[:-1]))
    if unknown:
        raise ValueError(f"accept: unknown kind {unknown[0][:20]!r} (use {', '.join(KINDS[:-1])})")
    return frozenset(kinds)


def refused_kind_detail(sniffed: Sniffed, accepted: frozenset[str]) -> str:
    """Why an upload of the wrong kind is refused, naming what this page takes."""
    names = {"image": "images", "html": "HTML files"}
    wanted = [names[k] for k in KINDS if k in accepted]
    takes = ", ".join(wanted[:-1]) + (" or " if len(wanted) > 1 else "") + wanted[-1]
    return f"This is {KIND_NAMES.get(sniffed.kind, 'a file')}; only {takes} can be opened here"


def unsupported_detail(name: str | None) -> str:
    ext = ext_of(name)
    what = f"“.{ext}” files" if ext and len(ext) <= 8 and ext.isalnum() else "This file type"
    return f"{what} can't be opened (unsupported or damaged file)"
