"""Server-side text rendering with the bundled fonts (watermarks, memes, the photo editor).

Fonts live in ``app/assets/fonts`` (OFL / Apache-2.0, see ``app/assets/fonts/README.md``). Text is
laid out with explicit ``\\n`` line breaks, greedy word wrapping to a maximum width (CJK and other
unspaced text breaks between characters, never inside a cluster), left/centre/right alignment, and
per-character fallback for glyphs the chosen font lacks: DejaVu Sans (accents, Greek, Cyrillic,
symbols), then Noto faces for Arabic, Hebrew, the Indic scripts, Thai and Chinese/Japanese/Korean
(:data:`FALLBACKS`). Complex scripts (Arabic joining, right-to-left order, Indic conjuncts and vowel
signs) need Pillow's raqm layout (libraqm + FriBiDi); when the server lacks it, :func:`text_warnings`
says so instead of silently drawing unjoined letters. Every size is capped so a request can't allocate
an absurd canvas.

Public API: :data:`FONTS`, :func:`list_fonts`, :func:`font_path`, :func:`font_dir`, :func:`get_font`,
:func:`wrap`, :func:`measure`, :func:`render`, :func:`fit_size`, :func:`text_warnings`,
:func:`fallback_files`, :func:`shaping_available`.
"""
from __future__ import annotations

import functools
import math
import threading
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

from .ops import parse_color

__all__ = [
    "FONTS", "DEFAULT_FONT", "FALLBACK_FONT", "MAX_TEXT_CHARS", "MAX_FONT_PX", "FontSpec", "list_fonts",
    "font_ids", "font_path", "font_dir", "get_font", "wrap", "measure", "render", "fit_size", "TextTooLarge",
    "FALLBACKS", "fallback_files", "shaping_available", "text_warnings", "script_runs",
]

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
MAX_TEXT_CHARS = 2000          # per rendered block
MAX_LINES = 200
MAX_FONT_PX = 2000             # glyph size cap (a 30 000 px image still gets a 2 000 px headline)
MIN_FONT_PX = 4
MAX_TEXT_PIXELS = 64_000_000   # rendered text bitmap cap (before rotation)


class TextTooLarge(ValueError):
    """The text would need a bitmap above MAX_TEXT_PIXELS (maps to 422 like other ValueErrors)."""


@dataclass(frozen=True)
class FontSpec:
    id: str
    name: str                    # human label for the UI
    file: str                    # file name in FONT_DIR
    category: str                # display | sans | serif | mono | script
    weight: int | None = None    # variable fonts: default wght axis value (Regular)
    bold_weight: int | None = None
    bold_file: str | None = None  # static bold face
    css: str = ""                # CSS font-family the UI can use to preview (may be absent client-side)
    slug: str = ""               # id used by the heyp UI ("libre-franklin"); every spelling is accepted

    @property
    def ui_id(self) -> str:
        return self.slug or self.id.replace("_", "-")

    @property
    def variable(self) -> bool:
        return self.weight is not None


FONTS: dict[str, FontSpec] = {spec.id: spec for spec in (
    FontSpec("anton", "Anton", "Anton-Regular.ttf", "display", css="Anton"),
    FontSpec("libre_franklin", "Libre Franklin", "LibreFranklin-Variable.ttf", "sans", 400, 700,
             css="Libre Franklin"),
    FontSpec("fraunces", "Fraunces", "Fraunces-Variable.ttf", "serif", 400, 700, css="Fraunces"),
    FontSpec("plex_mono", "IBM Plex Mono", "IBMPlexMono-Regular.ttf", "mono", bold_file="IBMPlexMono-Bold.ttf",
             css="IBM Plex Mono", slug="ibm-plex-mono"),
    FontSpec("dejavu_sans", "DejaVu Sans", "DejaVuSans.ttf", "sans", bold_file="DejaVuSans-Bold.ttf",
             css="DejaVu Sans"),
    FontSpec("dejavu_serif", "DejaVu Serif", "DejaVuSerif.ttf", "serif", css="DejaVu Serif"),
    FontSpec("dejavu_mono", "DejaVu Sans Mono", "DejaVuSansMono.ttf", "mono", css="DejaVu Sans Mono"),
    FontSpec("caveat", "Caveat", "Caveat-Variable.ttf", "script", 400, 700, css="Caveat"),
    FontSpec("dancing_script", "Dancing Script", "DancingScript-Variable.ttf", "script", 400, 700,
             css="Dancing Script"),
    FontSpec("great_vibes", "Great Vibes", "GreatVibes-Regular.ttf", "script", css="Great Vibes"),
    FontSpec("homemade_apple", "Homemade Apple", "HomemadeApple-Regular.ttf", "script", css="Homemade Apple"),
)}
DEFAULT_FONT = "libre_franklin"
FALLBACK_FONT = "dejavu_sans"
_ALIASES = {"impact": "anton", "sans": "libre_franklin", "serif": "fraunces", "mono": "plex_mono",
            "monospace": "plex_mono", "ibm_plex_mono": "plex_mono", "dejavu": "dejavu_sans", "handwriting": "caveat",
            "dejavu_sans_mono": "dejavu_mono"}
BOLD_WEIGHT = 700
_local = threading.local()


def font_ids() -> list[str]:
    return list(FONTS)


def list_fonts() -> list[dict[str, Any]]:
    """Fonts for the UI: id (the UI spelling, e.g. ``libre-franklin``), key (the canonical id; every spelling is
    accepted), name, category, whether a bold face exists, and the CSS family."""
    return [{"id": f.ui_id, "key": f.id, "name": f.name, "category": f.category,
             "bold": bool(f.bold_weight or f.bold_file), "css": f.css} for f in FONTS.values()]


def resolve(font_id: str | None) -> FontSpec:
    """Font spec for an id, UI id or alias (``None`` → default); ValueError for unknown ids."""
    if font_id is not None and not isinstance(font_id, str):
        raise ValueError("font must be a string")
    key = (font_id or DEFAULT_FONT).strip().lower().replace("-", "_").replace(" ", "_")
    key = _ALIASES.get(key, key)
    spec = FONTS.get(key)
    if spec is None:
        raise ValueError(f"Unknown font {font_id!r} (choose from {', '.join(FONTS)})")
    return spec


def font_dir() -> Path:
    """Directory holding every bundled TTF (resvg ``font_dirs``)."""
    return FONT_DIR


def font_path(font_id: str | None = None, bold: bool = False) -> Path:
    """TTF file for a font (Pillow renders with it, the UI previews it); a bold static face when one exists."""
    spec = resolve(font_id)
    name = spec.bold_file if bold and spec.bold_file else spec.file
    path = FONT_DIR / name
    if not path.is_file():
        raise RuntimeError(f"Font file {name} is missing from {FONT_DIR}")
    return path


def _font_size(size: Any) -> int:
    try:
        value = float(size)
    except (TypeError, ValueError):
        raise ValueError("font size must be a number") from None
    if not math.isfinite(value):
        raise ValueError("font size must be finite")
    return int(min(MAX_FONT_PX, max(MIN_FONT_PX, round(value))))


def _weight(spec: FontSpec, bold: bool, weight: int | None) -> int | None:
    """Requested weight (100–900) for a font: ``weight`` wins over ``bold``; None = the font's regular."""
    if weight is not None:
        if isinstance(weight, bool) or not 100 <= int(weight) <= 900:
            raise ValueError("font weight must be between 100 and 900")
        return int(weight)
    return BOLD_WEIGHT if bold else None


def get_font(font_id: str | None, size: float, bold: bool = False, *, weight: int | None = None) -> ImageFont.FreeTypeFont:
    """A FreeType font object at ``size`` px (cached per thread: FreeType faces are not thread-safe).

    ``weight`` (100–900) sets a variable font's wght axis; static fonts switch to their bold face
    at 600 and above (``bold`` = weight 700)."""
    spec = resolve(font_id)
    px = _font_size(size)
    wanted = _weight(spec, bold, weight)
    cache: dict = getattr(_local, "fonts", None) or {}
    if not cache:
        _local.fonts = cache
    key = (spec.id, px, wanted)
    font = cache.get(key)
    if font is not None:
        return font
    if len(cache) > 64:
        cache.clear()
    use_static_bold = wanted is not None and wanted >= 600 and spec.bold_file is not None
    font = ImageFont.truetype(str(font_path(spec.id, bold=use_static_bold)), px)
    weight = spec.weight if wanted is None else wanted
    if spec.variable and weight is not None:
        try:
            axes = font.get_variation_axes()
            values = []
            for axis in axes:
                name = axis.get("name")
                name = name.decode("ascii", "ignore") if isinstance(name, bytes) else str(name)
                if name.lower() == "weight":
                    values.append(min(axis["maximum"], max(axis["minimum"], weight)))
                elif name.lower() == "optical size":
                    values.append(min(axis["maximum"], max(axis["minimum"], min(px * 0.75, 144))))
                else:
                    values.append(axis["default"])
            font.set_variation_by_axes(values)
        except Exception:  # FreeType without variation support: the default instance is still usable
            pass
    cache[key] = font
    return font


# ------------------------------------------------------------------ glyph coverage (cmap) + fallback runs
@dataclass(frozen=True)
class Fallback:
    """A face used for characters the chosen font lacks; preferred for the code point ``ranges`` of its script."""
    key: str
    file: str
    bold_file: str | None
    script: str
    ranges: tuple[tuple[int, int], ...] = ()
    family: str = ""    # the face's family name (SVG text is pointed at it, see script_runs)


_ARABIC = ((0x0600, 0x06FF), (0x0750, 0x077F), (0x0870, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
_HEBREW = ((0x0590, 0x05FF), (0xFB1D, 0xFB4F))
_CJK = ((0x1100, 0x11FF), (0x2E80, 0x2FDF), (0x3000, 0x33FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA960, 0xA97F),
        (0xAC00, 0xD7FF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF))


def _noto(key: str, stem: str, script: str, ranges: tuple[tuple[int, int], ...], family: str) -> Fallback:
    return Fallback(key, f"{stem}-Regular.ttf", f"{stem}-Bold.ttf", script, ranges, family)


FALLBACKS: tuple[Fallback, ...] = (
    Fallback("dejavu_sans", "DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "Latin, Greek or Cyrillic", (), "DejaVu Sans"),
    _noto("noto_naskh_arabic", "NotoNaskhArabic", "Arabic", _ARABIC, "Noto Naskh Arabic"),
    _noto("noto_hebrew", "NotoSansHebrew", "Hebrew", _HEBREW, "Noto Sans Hebrew"),
    _noto("noto_devanagari", "NotoSansDevanagari", "Devanagari", ((0x0900, 0x097F), (0xA8E0, 0xA8FF), (0x1CD0, 0x1CFF)),
          "Noto Sans Devanagari"),
    _noto("noto_bengali", "NotoSansBengali", "Bengali", ((0x0980, 0x09FF),), "Noto Sans Bengali"),
    _noto("noto_gurmukhi", "NotoSansGurmukhi", "Gurmukhi", ((0x0A00, 0x0A7F),), "Noto Sans Gurmukhi"),
    _noto("noto_gujarati", "NotoSansGujarati", "Gujarati", ((0x0A80, 0x0AFF),), "Noto Sans Gujarati"),
    _noto("noto_oriya", "NotoSansOriya", "Odia", ((0x0B00, 0x0B7F),), "Noto Sans Oriya"),
    _noto("noto_tamil", "NotoSansTamil", "Tamil", ((0x0B80, 0x0BFF),), "Noto Sans Tamil"),
    _noto("noto_telugu", "NotoSansTelugu", "Telugu", ((0x0C00, 0x0C7F),), "Noto Sans Telugu"),
    _noto("noto_kannada", "NotoSansKannada", "Kannada", ((0x0C80, 0x0CFF),), "Noto Sans Kannada"),
    _noto("noto_malayalam", "NotoSansMalayalam", "Malayalam", ((0x0D00, 0x0D7F),), "Noto Sans Malayalam"),
    _noto("noto_thai", "NotoSansThai", "Thai", ((0x0E00, 0x0E7F),), "Noto Sans Thai"),
    Fallback("noto_cjk", "NotoSansCJKsc-Subset.otf", None, "Chinese, Japanese or Korean", _CJK, "Noto Sans CJK SC"),
)
_FALLBACK_BY_KEY = {fb.key: fb for fb in FALLBACKS}
# Scripts whose letters change shape, combine or run right to left: drawn correctly only with raqm.
_NEEDS_SHAPING = tuple(fb for fb in FALLBACKS if fb.key not in ("dejavu_sans", "noto_cjk"))
_JOINERS = frozenset("\u200c\u200d")   # ZWNJ / ZWJ steer Indic and Arabic shaping


@functools.lru_cache(maxsize=1)
def shaping_available() -> bool:
    """True when Pillow can use raqm (HarfBuzz shaping + FriBiDi bidi): needs libfribidi on the server."""
    try:
        return bool(features.check("raqm"))
    except Exception:  # pragma: no cover — very old Pillow
        return False


def fallback_files() -> list[str]:
    """Every fallback face file that is installed (resvg ``font_files`` for SVG text uses the same set)."""
    out: list[str] = []
    for fb in FALLBACKS:
        for name in (fb.file, fb.bold_file):
            if name and name not in out and (FONT_DIR / name).is_file():
                out.append(name)
    return out


@functools.lru_cache(maxsize=64)
def _cmap(file: str) -> frozenset[int] | None:
    """Code points a font maps (None when fontTools is unavailable → no fallback splitting)."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:  # pragma: no cover — fontTools is a pinned dependency (requirements.txt)
        return None
    if not (FONT_DIR / file).is_file():
        return frozenset()
    try:
        with TTFont(str(FONT_DIR / file), lazy=True) as tt:
            table = tt.getBestCmap() or {}
            return frozenset(table)
    except Exception:
        return None


def _covered(spec: FontSpec, bold: bool) -> frozenset[int] | None:
    file = spec.bold_file if bold and spec.bold_file else spec.file
    return _cmap(file)


def _fallback_file(fb: Fallback, bold: bool) -> str:
    return fb.bold_file if bold and fb.bold_file and (FONT_DIR / fb.bold_file).is_file() else fb.file


@functools.lru_cache(maxsize=8192)
def _fallback_for(cp: int, bold: bool) -> str | None:
    """Key of the fallback face for ``cp``: its script's face first, then the chain in order (None: no face)."""
    for fb in FALLBACKS:
        if any(lo <= cp <= hi for lo, hi in fb.ranges) and cp in (_cmap(_fallback_file(fb, bold)) or ()):
            return fb.key
    for fb in FALLBACKS:
        if cp in (_cmap(_fallback_file(fb, bold)) or ()):
            return fb.key
    return None


def _attaches(ch: str) -> bool:
    """Spaces, punctuation, marks and joiners stay in the run before them (one shaping run per phrase)."""
    if ch.isspace() or ch in _JOINERS:
        return True
    cat = unicodedata.category(ch)
    return cat[0] in "MPZ" or cat in ("Nd", "Sk", "Cf")


def _runs(text: str, spec: FontSpec, bold: bool) -> list[tuple[str, str | None]]:
    """Split ``text`` into (chunk, fallback key or None for the chosen font) runs by glyph coverage."""
    primary = _covered(spec, bold)
    if primary is None:
        return [(text, None)] if text else []
    runs: list[tuple[str, str | None]] = []
    for ch in text:
        cp = ord(ch)
        key: str | None
        if (runs and runs[-1][1] not in (None, FALLBACK_FONT) and _attaches(ch)
                and (ch in _JOINERS or cp in _run_cmap(runs[-1][1], spec, bold))):
            key = runs[-1][1]   # keeps a Noto script run whole for shaping (Latin/Greek runs split as before)
        elif cp in primary or ch.isspace():
            key = None
        else:
            key = _fallback_for(cp, bold)
            if key is not None and _FALLBACK_BY_KEY[key].file == spec.file:
                key = None   # the chosen font is that face (e.g. DejaVu Sans)
        if runs and runs[-1][1] == key:
            runs[-1] = (runs[-1][0] + ch, key)
        else:
            runs.append((ch, key))
    return runs


def _script_family(ch: str) -> str | None:
    cp = ord(ch)
    for fb in FALLBACKS[1:]:
        if any(lo <= cp <= hi for lo, hi in fb.ranges):
            return fb.family
    return None


def script_runs(value: str) -> list[tuple[str, str | None]]:
    """Split ``value`` into (chunk, Noto family or None) runs for the scripts :data:`FALLBACKS` covers;
    spaces, punctuation and digits between two letters of one script stay in its run. SVG text is
    pointed at these faces so resvg shapes each run with the right font (its own glyph fallback can't
    re-shape Indic text)."""
    runs: list[tuple[str, str | None]] = []
    pending = ""
    for ch in value:
        family = _script_family(ch)
        if family is None and runs and runs[-1][1] is not None and _attaches(ch):
            if unicodedata.category(ch)[0] == "M" and not pending:
                family = runs[-1][1]   # a combining mark joins its base
            else:
                pending += ch          # a neutral inside a script run: decided by the next letter
                continue
        if pending:
            if family is not None and runs[-1][1] == family:
                runs[-1] = (runs[-1][0] + pending, family)
            else:
                runs.append((pending, None))
            pending = ""
        if runs and runs[-1][1] == family:
            runs[-1] = (runs[-1][0] + ch, family)
        else:
            runs.append((ch, family))
    if pending:
        runs.append((pending, None))
    merged: list[tuple[str, str | None]] = []
    for chunk, family in runs:
        if merged and merged[-1][1] == family:
            merged[-1] = (merged[-1][0] + chunk, family)
        else:
            merged.append((chunk, family))
    return merged


def _run_cmap(key: str | None, spec: FontSpec, bold: bool) -> frozenset[int]:
    if key is None:
        return _covered(spec, bold) or frozenset()
    return _cmap(_fallback_file(_FALLBACK_BY_KEY[key], bold)) or frozenset()


def _direction(text: str) -> bool | None:
    """True/False for right-to-left/left-to-right by the first strong character (Unicode bidi P2/P3);
    None when ``text`` has none (spaces, punctuation, digits)."""
    for ch in text:
        kind = unicodedata.bidirectional(ch)
        if kind in ("R", "AL"):
            return True
        if kind == "L":
            return False
    return None


def _bidi_runs(runs: list[tuple[str, str | None]], rtl: bool) -> list[tuple[str, str | None]]:
    """Visual-order helper for raqm: spaces and punctuation ending a run against the line's direction
    resolve to the line's direction (bidi rule N1/N2), so they become a run of their own; a right-to-left
    line then lists its runs right to left (raqm orders the text inside each run)."""
    out: list[tuple[str, str | None]] = []
    for chunk, key in runs:
        direction = _direction(chunk)
        if direction is not None and direction != rtl:
            cut = len(chunk)
            while cut > 0 and (chunk[cut - 1].isspace() or unicodedata.category(chunk[cut - 1])[0] in "PZ"):
                cut -= 1
            if 0 < cut < len(chunk):
                out += [(chunk[:cut], key), (chunk[cut:], key)]
                continue
        out.append((chunk, key))
    return out[::-1] if rtl else out


def _fallback_face(key: str, px: int, bold: bool) -> ImageFont.FreeTypeFont:
    if key == FALLBACK_FONT:
        return get_font(FALLBACK_FONT, px, bold)
    cache: dict = getattr(_local, "fonts", None) or {}
    if not cache:
        _local.fonts = cache
    ck = ("fallback", key, px, bold)
    font = cache.get(ck)
    if font is None:
        if len(cache) > 64:
            cache.clear()
        font = ImageFont.truetype(str(FONT_DIR / _fallback_file(_FALLBACK_BY_KEY[key], bold)), px)
        cache[ck] = font
    return font


def _has_glyph(cp: int, spec: FontSpec, bold: bool) -> bool:
    primary = _covered(spec, bold)
    return primary is None or cp in primary or _fallback_for(cp, bold) is not None


def text_warnings(strings: Iterable[str | None], font: str | None = None, *, bold: bool = False) -> list[str]:
    """User-facing warnings for text the server can't draw faithfully: characters no bundled font has
    (they would show as boxes), and complex scripts when raqm shaping is unavailable."""
    spec = resolve(font)
    missing: list[str] = []
    scripts: list[str] = []
    shaping = shaping_available()
    for value in strings:
        for ch in value or "":
            if ch.isspace() or not ch.isprintable():
                continue
            cp = ord(ch)
            if unicodedata.category(ch)[0] == "P" and _has_glyph(cp, spec, bold):
                continue   # shared punctuation (e.g. the danda) needs no shaping
            if not _has_glyph(cp, spec, bold):
                if ch not in missing:
                    missing.append(ch)
            elif not shaping:
                for fb in _NEEDS_SHAPING:
                    if fb.script not in scripts and any(lo <= cp <= hi for lo, hi in fb.ranges):
                        scripts.append(fb.script)
    out = []
    if missing:
        shown = " ".join(missing[:8]) + (" …" if len(missing) > 8 else "")
        out.append(f"Some characters aren't in heyp's fonts and will show as empty boxes: {shown}")
    if scripts:
        names = ", ".join(scripts[:-1]) + (f" and {scripts[-1]}" if len(scripts) > 1 else scripts[0])
        out.append(f"This server can't shape {names} text yet, so letters may not join or combine correctly")
    return out


@dataclass
class _Line:
    text: str
    runs: list[tuple[str, ImageFont.FreeTypeFont]]   # in visual (drawing) order
    width: float


class _Layout:
    """Fonts and line metrics for one (font, size, weight) combination."""

    def __init__(self, font_id: str | None, size: float, bold: bool, weight: int | None = None) -> None:
        self.spec = resolve(font_id)
        self.weight = _weight(self.spec, bold, weight)
        self.bold = self.weight is not None and self.weight >= 600   # static bold face / fallback weight
        self.px = _font_size(size)
        self.font = get_font(self.spec.id, self.px, weight=self.weight)
        self.shaping = shaping_available()
        ascent, descent = self.font.getmetrics()
        self.ascent, self.descent = ascent, descent

    @property
    def fallback(self) -> ImageFont.FreeTypeFont:
        return _fallback_face(FALLBACK_FONT, self.px, self.bold)

    def line(self, text: str) -> _Line:
        logical = _runs(text, self.spec, self.bold)
        if self.shaping and len(logical) > 1:
            logical = _bidi_runs(logical, bool(_direction(text)))
        runs = [(chunk, _fallback_face(key, self.px, self.bold) if key else self.font) for chunk, key in logical]
        width = sum(font.getlength(chunk) for chunk, font in runs)
        return _Line(text, runs, width)

    def width(self, text: str) -> float:
        return self.line(text).width


def _check_text(text: Any) -> str:
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    joiners = _JOINERS if shaping_available() else frozenset()   # without raqm they would draw as boxes
    text = "".join(ch for ch in text if ch == "\n" or ch.isprintable() or ch in joiners)
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"Text is too long ({len(text)} characters, max {MAX_TEXT_CHARS})")
    return text


def _breakable(prev: str, ch: str) -> bool:
    return not (unicodedata.category(ch)[0] == "M" or ch in _JOINERS or prev in _JOINERS
                or unicodedata.combining(prev) == 9)


def _wrap_paragraph(layout: _Layout, paragraph: str, max_width: float | None) -> list[str]:
    if max_width is None or layout.width(paragraph) <= max_width:
        return [paragraph]
    lines: list[str] = []
    current = ""
    for word in paragraph.split(" "):
        candidate = word if not current else f"{current} {word}"
        if layout.width(candidate) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        # A single word wider than the line (or unspaced CJK text) is broken between characters, never
        # inside a cluster (before a combining mark or joiner, or after a virama).
        if layout.width(word) > max_width:
            piece = ""
            for ch in word:
                if piece and layout.width(piece + ch) > max_width and _breakable(piece[-1], ch):
                    lines.append(piece)
                    piece = ch
                else:
                    piece += ch
            current = piece
        else:
            current = word
    lines.append(current)
    return lines


def wrap(text: str, font: str | None, size: float, max_width: float | None = None, *, bold: bool = False,
         weight: int | None = None) -> list[str]:
    """Lines after explicit breaks and greedy word wrapping to ``max_width`` px."""
    text = _check_text(text)
    layout = _Layout(font, size, bold, weight)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(_wrap_paragraph(layout, paragraph, max_width))
        if len(lines) > MAX_LINES:
            raise ValueError(f"Text has too many lines (max {MAX_LINES})")
    return lines


def _line_height(layout: _Layout, spacing: float) -> int:
    return max(1, int(round((layout.ascent + layout.descent) * spacing)))


def measure(text: str, font: str | None, size: float, *, max_width: float | None = None, bold: bool = False,
            line_spacing: float = 1.15, stroke: int = 0, weight: int | None = None) -> tuple[int, int]:
    """(width, height) in px of the rendered block (before padding/rotation)."""
    layout = _Layout(font, size, bold, weight)
    lines = [layout.line(t) for t in wrap(text, font, size, max_width, bold=bold, weight=weight)]
    width = max((ln.width for ln in lines), default=0.0)
    height = _line_height(layout, line_spacing) * (len(lines) - 1) + layout.ascent + layout.descent
    return int(math.ceil(width)) + 2 * stroke, int(height) + 2 * stroke


def fit_size(text: str, font: str | None, box_w: float, box_h: float, *, max_size: float, min_size: float = MIN_FONT_PX,
             bold: bool = False, wrap_text: bool = True, line_spacing: float = 1.1, stroke_ratio: float = 0.0,
             weight: int | None = None) -> int:
    """Largest font size (px) whose block fits ``box_w × box_h`` (binary search, wrapping when allowed)."""
    lo, hi = int(max(MIN_FONT_PX, min_size)), int(min(MAX_FONT_PX, max(min_size, max_size)))
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        stroke = int(round(mid * stroke_ratio))
        try:
            w, h = measure(text, font, mid, max_width=(box_w - 2 * stroke) if wrap_text else None, bold=bold,
                           line_spacing=line_spacing, stroke=stroke, weight=weight)
        except ValueError:
            w, h = math.inf, math.inf
        if w <= box_w and h <= box_h:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def render(text: str, *, font: str | None = None, size: float = 48, color: Any = "#000000", bold: bool = False,
           align: str = "center", max_width: float | None = None, line_spacing: float = 1.15,
           stroke_color: Any = None, stroke_width: int = 0, background: Any = None, padding: int = 0,
           shadow: Any = None, shadow_offset: tuple[int, int] | None = None, shadow_blur: float = 0.0,
           opacity: float = 1.0, weight: int | None = None) -> Image.Image:
    """Render a text block as a tight RGBA image.

    ``stroke_color``/``stroke_width`` outline every glyph (memes), ``background`` fills a box behind
    the text (``padding`` px around it), ``shadow`` draws a soft offset shadow. ``opacity`` scales
    the alpha of the whole block. Empty text gives a 1×1 transparent image.
    """
    if align not in ("left", "center", "right"):
        raise ValueError(f"Invalid alignment {align!r} (use left, center or right)")
    if not 0.0 <= float(opacity) <= 1.0:
        raise ValueError("opacity must be between 0 and 1")
    text = _check_text(text)
    layout = _Layout(font, size, bold, weight)
    stroke = max(0, int(stroke_width or 0))
    stroke = min(stroke, layout.px)
    pad = max(0, int(padding or 0))
    lines = [layout.line(t) for t in wrap(text, font, size, max_width, bold=bold, weight=weight)]
    if not any(ln.text.strip() for ln in lines):
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    lh = _line_height(layout, line_spacing)
    block_w = int(math.ceil(max(ln.width for ln in lines)))
    block_h = lh * (len(lines) - 1) + layout.ascent + layout.descent
    sx, sy = shadow_offset or (0, 0)
    blur = max(0.0, float(shadow_blur or 0.0))
    extra = int(math.ceil(blur * 3)) + max(abs(sx), abs(sy)) if shadow is not None else 0
    width = block_w + 2 * (stroke + pad + extra)
    height = block_h + 2 * (stroke + pad + extra)
    if width * height > MAX_TEXT_PIXELS:
        raise TextTooLarge(f"Text block would be {width}×{height} px; use a smaller size or less text")
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ox, oy = stroke + pad + extra, stroke + pad + extra
    fill = parse_color(color)
    outline = parse_color(stroke_color) if stroke and stroke_color is not None else None

    def draw_glyphs(target: Image.Image, rgba: tuple[int, int, int, int], dx: int, dy: int,
                    outline_rgba: tuple[int, int, int, int] | None) -> None:
        draw = ImageDraw.Draw(target)
        for index, line in enumerate(lines):
            if align == "left":
                x = 0.0
            elif align == "right":
                x = block_w - line.width
            else:
                x = (block_w - line.width) / 2
            y = index * lh
            for chunk, fnt in line.runs:
                if outline_rgba is not None:
                    draw.text((ox + dx + x, oy + dy + y), chunk, font=fnt, fill=rgba, stroke_width=stroke,
                              stroke_fill=outline_rgba)
                else:
                    draw.text((ox + dx + x, oy + dy + y), chunk, font=fnt, fill=rgba)
                x += fnt.getlength(chunk)

    if background is not None:
        bg = parse_color(background)
        if bg[3]:
            ImageDraw.Draw(canvas).rectangle(
                (extra, extra, width - extra - 1, height - extra - 1), fill=bg)
    if shadow is not None:
        sh = parse_color(shadow)
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw_glyphs(layer, sh, sx, sy, sh if outline is not None else None)
        if blur:
            layer = layer.filter(ImageFilter.GaussianBlur(blur))
        canvas = Image.alpha_composite(canvas, layer)
    if fill[3] < 255 or (outline is not None and outline[3] < 255):
        # Draw opaque, then apply the translucency once (overlapping strokes would otherwise darken).
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw_glyphs(layer, (*fill[:3], 255), 0, 0, (*outline[:3], 255) if outline else None)
        if outline is not None and outline[3] != fill[3]:
            # Separate passes so fill and outline keep their own alpha.
            ring = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            draw_glyphs(ring, (*outline[:3], 255), 0, 0, (*outline[:3], 255))
            ring.putalpha(ring.getchannel("A").point(lambda v: v * outline[3] // 255))
            canvas = Image.alpha_composite(canvas, ring)
            layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            draw_glyphs(layer, (*fill[:3], 255), 0, 0, None)
            layer.putalpha(layer.getchannel("A").point(lambda v: v * fill[3] // 255))
        else:
            layer.putalpha(layer.getchannel("A").point(lambda v: v * fill[3] // 255))
        canvas = Image.alpha_composite(canvas, layer)
    else:
        draw_glyphs(canvas, fill, 0, 0, outline)
    if opacity < 1.0:
        alpha = canvas.getchannel("A").point(lambda v: int(round(v * float(opacity))))
        canvas.putalpha(alpha)
    return canvas


def rotate_rgba(img: Image.Image, angle: float) -> Image.Image:
    """Rotate an RGBA layer CLOCKWISE by ``angle`` degrees with expansion (transparent corners)."""
    angle = float(angle) % 360.0
    if abs(angle) < 1e-9:
        return img
    quarter = round(angle / 90.0)
    if abs(angle - quarter * 90.0) < 1e-9:
        method = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}
        return img.transpose(method[quarter % 4]) if quarter % 4 else img
    return img.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(0, 0, 0, 0))
