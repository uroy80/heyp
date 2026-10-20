"""Encode an image to a target file size (PLAN.md §4 compress).

Lossy formats search the quality and downscale only when quality 5 is still too large (sized so
that a mid quality fits, then searched again); lossless formats go optimise → palette (256 → 16
colours) → downscale. Images above PROXY_ABOVE px search on a ≤ PROXY_PIXELS proxy whose sizes,
calibrated by the real encodes, predict the full-size result, so only a few encodes ever run at
full size. Colour conversion, alpha handling and metadata are resolved once (``io._prepare``) and
reused for every trial encode; MAX_ENCODES bounds the work of one job.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any

from PIL import Image

from . import io

LOSSY = frozenset({"jpeg", "webp", "avif", "heic", "pdf"})
MIN_QUALITY, MAX_QUALITY = 5, 95
REFERENCE_QUALITY = 50
MAX_SEARCH_ENCODES = 8
MAX_DOWNSCALE_ROUNDS = 6
MIN_SIDE = 16
PALETTE_STEPS = (256, 128, 64, 32, 16)
REFINE_STEPS = 4
PROXY_ABOVE = 12_000_000     # px: larger outputs search their quality on a proxy
PROXY_PIXELS = 3_000_000     # proxy size
MAX_LARGE_ENCODES = 4        # real encodes per (large) output size during a proxy-guided search
MAX_ENCODES = 40             # hard cap per job, proxy encodes included
_GOOD_ENOUGH = 0.95          # a fit using this share of max_bytes ends a guided search
_FIT_MARGINS = (0.97, 0.93, 0.85)  # aim below max_bytes, lower after each miss
_ALPHA_RANGE = (0.3, 1.5)    # bytes ∝ pixels**alpha when extrapolating from the proxy
_ALPHA_OPTIMISTIC = 0.5      # lower bound used to skip encodes that cannot fit

_PREPARE_KEYS = ("dpi", "exif", "icc_profile", "strip_metadata", "strip_gps", "colorspace", "background")
_ENCODE_KEYS = ("quality", "lossless", "progressive", "chroma")


@dataclass
class _Result:
    data: bytes
    quality: int | None
    size: tuple[int, int]
    scale: float


class _BudgetExhausted(Exception):
    """MAX_ENCODES reached: the job finishes with the best result so far."""


def _kb(n: int) -> str:
    kb = n / 1024
    return f"{kb:.1f} KB" if kb < 10 else f"{kb:.0f} KB"


def _limit(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number of bytes")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number of bytes") from None
    if number <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return number


class _Encoder:
    """Trial encodes for one target-size job: counts encodes, tracks the scale and the best result."""

    def __init__(self, prepared: io._Prepared, opts: dict[str, Any], max_bytes: int | None = None,
                 min_bytes: int | None = None) -> None:
        self.base = prepared
        self.current = prepared
        self.opts = opts
        self.iterations = 0
        self.scale = 1.0
        self.max_bytes, self.min_bytes = max_bytes, min_bytes
        self.best: _Result | None = None
        self.smallest: _Result | None = None

    @property
    def pixels(self) -> int:
        return self.base.image.width * self.base.image.height

    def count(self) -> None:
        if self.iterations >= MAX_ENCODES:
            raise _BudgetExhausted
        self.iterations += 1

    def encode(self, quality: int | None = None, **overrides: Any) -> _Result:
        self.count()
        opts = {**self.opts, **overrides}
        if quality is not None:
            opts["quality"] = quality
        data = io._encode(self.current, **opts)
        lossy = not opts.get("lossless") and "colors" not in overrides and "optimize" not in overrides
        result = _Result(data, opts["quality"] if lossy else None, self.current.image.size, self.scale)
        self._remember(result)
        return result

    def _remember(self, result: _Result) -> None:
        size = len(result.data)
        if self.smallest is None or size < len(self.smallest.data):
            self.smallest = result
        if self.max_bytes is not None and size > self.max_bytes:
            return
        key = (self.min_bytes is None or size >= self.min_bytes, result.scale, result.quality or 0)
        if self.best is None or key > (self.min_bytes is None or len(self.best.data) >= self.min_bytes,
                                       self.best.scale, self.best.quality or 0):
            self.best = result

    def downscale(self, factor: float) -> bool:
        """Shrink to ``factor`` × the current scale (sides never below MIN_SIDE); False if no smaller size exists."""
        W, H = self.base.image.size
        cur_w, cur_h = self.current.image.size
        scale = max(self.scale * factor, self.min_scale)
        if round(W * scale) >= cur_w and round(H * scale) >= cur_h:
            return False
        self.set_scale(scale)
        return True

    @property
    def min_scale(self) -> float:
        return min(1.0, MIN_SIDE / min(self.base.image.size))

    def size_at(self, scale: float) -> tuple[int, int]:
        W, H = self.base.image.size
        return max(1, round(W * scale)), max(1, round(H * scale))

    def pixels_at(self, scale: float) -> int:
        w, h = self.size_at(scale)
        return w * h

    def set_scale(self, scale: float) -> None:
        """Resample the prepared image to ``scale`` × its original size (always from the original)."""
        size = self.size_at(scale)
        if size == self.current.image.size:
            self.scale = scale
            return
        if size == self.base.image.size:
            self.current = self.base
        else:
            image = self.base.image.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
            self.current = io._replace_image(self.base, image)
        self.scale = scale


class _Proxy:
    """A ≤ PROXY_PIXELS copy of a large image whose encodes predict the size of real encodes.

    Real encodes calibrate the prediction: at an already measured output size the measured ratio
    real/proxy is reused, elsewhere bytes are interpolated as a power law of the pixel count.
    """

    def __init__(self, enc: _Encoder, extra: dict[str, Any]) -> None:
        W, H = enc.base.image.size
        k = math.sqrt(PROXY_PIXELS / (W * H))
        size = max(MIN_SIDE, round(W * k)), max(MIN_SIDE, round(H * k))
        self.enc, self.extra = enc, extra
        self.prepared = io._replace_image(enc.base, enc.base.image.resize(size, Image.Resampling.LANCZOS,
                                                                           reducing_gap=3.0))
        self.pixels = size[0] * size[1]
        self.sizes: dict[int, int] = {}
        self.points: list[tuple[int, int, float]] = []  # (pixels, quality, real bytes / proxy bytes)
        self._local_alpha: float | None = None

    def _encode(self, prepared: io._Prepared, quality: int, **overrides: Any) -> int:
        self.enc.count()
        return len(io._encode(prepared, **{**self.enc.opts, **self.extra, "quality": quality, **overrides}))

    def size(self, quality: int) -> int:
        if quality not in self.sizes:
            self.sizes[quality] = self._encode(self.prepared, quality)
        return self.sizes[quality]

    def lossless_size(self) -> int:
        return self._encode(self.prepared, self.enc.opts["quality"], lossless=True)

    def local_alpha(self) -> float:
        """Exponent between a quarter-size copy and the proxy: the first guess before any real encode."""
        if self._local_alpha is None:
            small = io._replace_image(self.prepared, self.prepared.image.reduce(2))
            q = REFERENCE_QUALITY
            ratio = self.size(q) / max(1, self._encode(small, q))
            pixel_ratio = self.pixels / (small.image.width * small.image.height)
            alpha = math.log(max(ratio, 1e-6)) / math.log(pixel_ratio) if pixel_ratio > 1 else 1.0
            self._local_alpha = min(max(alpha, _ALPHA_RANGE[0]), 1.0)
        return self._local_alpha

    def _measured(self, pixels: int, quality: int) -> float | None:
        """Real/proxy ratio measured at this output size, interpolated linearly in quality."""
        pts = sorted((q, r) for px, q, r in self.points if px == pixels)
        if not pts:
            return None
        below = [p for p in pts if p[0] <= quality]
        above = [p for p in pts if p[0] >= quality]
        if below and above:
            (q0, r0), (q1, r1) = below[-1], above[0]
            return r0 if q1 == q0 else r0 + (r1 - r0) * (quality - q0) / (q1 - q0)
        return below[-1][1] if below else above[0][1]

    def ratio(self, pixels: int, quality: int) -> float:
        """Predicted real bytes / proxy bytes for an output of ``pixels`` at ``quality``.

        Piecewise power law in the pixel count through the measured sizes (and the proxy itself,
        ratio 1): exact at every measurement, interpolated between them, extrapolated beyond.
        """
        known = {self.pixels: 1.0}
        for px in {p[0] for p in self.points}:
            known[px] = self._measured(px, quality) or 1.0
        if pixels in known:
            return known[pixels]
        xs = sorted(known)
        if len(xs) == 1:
            return (pixels / self.pixels) ** self.local_alpha()
        i = bisect.bisect(xs, pixels)
        a, b = (xs[i - 1], xs[i]) if 0 < i < len(xs) else (xs[0], xs[1]) if i == 0 else (xs[-2], xs[-1])
        slope = math.log(known[b] / known[a]) / math.log(b / a)
        if not 0 < i < len(xs):
            slope = min(max(slope, _ALPHA_RANGE[0]), _ALPHA_RANGE[1])
        anchor = a if abs(math.log(pixels / a)) <= abs(math.log(pixels / b)) else b
        return known[anchor] * (pixels / anchor) ** slope

    def predict(self, pixels: int, quality: int) -> float:
        return self.size(quality) * self.ratio(pixels, quality)

    def learn(self, pixels: int, quality: int, nbytes: int) -> None:
        if quality in self.sizes:  # only calibrate against qualities the proxy already encoded
            self.points.append((pixels, quality, nbytes / max(1, self.sizes[quality])))

    def measured(self, pixels: int) -> bool:
        return any(p[0] == pixels for p in self.points)

    def highest(self, pixels: int, lo: int, hi: int, limit: float) -> int | None:
        """Highest quality in [lo, hi] predicted to be ≤ ``limit`` bytes (bisection on proxy encodes)."""
        if lo > hi:
            return None
        if self.predict(pixels, hi) <= limit:
            return hi
        best, hi = None, hi - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.predict(pixels, mid) <= limit:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best

    def lowest(self, pixels: int, lo: int, hi: int, limit: float) -> int | None:
        """Lowest quality in [lo, hi] predicted to be ≥ ``limit`` bytes."""
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.predict(pixels, mid) >= limit:
                best, hi = mid, mid - 1
            else:
                lo = mid + 1
        return best

    def scale_for(self, quality: int, limit: float, lo: float, hi: float) -> float:
        """Scale in [lo, hi] whose predicted size at ``quality`` is ``limit`` bytes."""
        for _ in range(40):
            mid = math.sqrt(lo * hi)
            if self.predict(self.enc.pixels_at(mid), quality) <= limit:
                lo = mid
            else:
                hi = mid
        return lo


def encode_to_target(img: Image.Image, fmt: str, *, max_bytes: int | None = None, min_bytes: int | None = None,
                     **save_kwargs: Any) -> tuple[bytes, dict]:
    """Encode with io.save semantics so the result is ≤ ``max_bytes`` (and ≥ ``min_bytes`` if possible).

    Returns ``(data, info)`` with info = {"quality", "scale", "iterations", "met", "warning", "width",
    "height", "size_bytes"}. Never returns more than ``max_bytes`` when the target is reachable and
    never pads a file to reach ``min_bytes``.
    """
    fmt = io.canonical_format(fmt)
    if fmt not in io.OUTPUT_FORMATS:
        raise ValueError(f"Cannot write {fmt.upper()} files")
    max_bytes = _limit(max_bytes, "max_bytes")
    min_bytes = _limit(min_bytes, "min_bytes")
    if max_bytes is not None and min_bytes is not None and min_bytes > max_bytes:
        raise ValueError("The minimum size must not be larger than the maximum size")
    unknown = set(save_kwargs) - set(_PREPARE_KEYS) - set(_ENCODE_KEYS)
    if unknown:
        raise TypeError(f"Unexpected argument(s): {', '.join(sorted(unknown))}")
    prepared = io._prepare(img, fmt, **{k: v for k, v in save_kwargs.items() if k in _PREPARE_KEYS})
    opts = {k: v for k, v in save_kwargs.items() if k in _ENCODE_KEYS}
    quality = opts.get("quality", 90)
    if isinstance(quality, bool) or not isinstance(quality, (int, float)) or not 1 <= quality <= 100:
        raise ValueError("quality must be between 1 and 100")
    opts["quality"] = int(round(quality))
    enc = _Encoder(prepared, opts, max_bytes, min_bytes)

    if max_bytes is None and min_bytes is None:
        result = enc.encode()
        if fmt not in LOSSY:
            result.quality = None
        return _finish(enc, result, max_bytes, min_bytes)
    try:
        if fmt in LOSSY:
            return _lossy(enc, fmt, max_bytes, min_bytes)
        return _lossless(enc, fmt, max_bytes, min_bytes)
    except _BudgetExhausted:
        result = enc.best or enc.smallest
        if result is None:  # pragma: no cover — a search makes real encodes long before the cap
            raise ValueError("Could not search for the file size") from None
        return _finish(enc, result, max_bytes, min_bytes, f"Size search stopped after {MAX_ENCODES} encodes")


def _finish(enc: _Encoder, result: _Result, max_bytes: int | None, min_bytes: int | None,
            *notes: str | None) -> tuple[bytes, dict]:
    size = len(result.data)
    fits = max_bytes is None or size <= max_bytes
    warnings = [n for n in notes if n]
    if not fits:
        warnings.append(f"Could not compress below {_kb(max_bytes)}; smallest result is {_kb(size)}")
    elif result.scale < 1.0 and max_bytes is not None:
        warnings.append(f"Downscaled to {result.size[0]}×{result.size[1]} px to fit {_kb(max_bytes)}")
    if min_bytes is not None and size < min_bytes:
        warnings.append(f"Could not reach the minimum of {_kb(min_bytes)} without padding; result is {_kb(size)}")
    info = {
        "quality": result.quality,
        "scale": round(result.scale, 4),
        "iterations": enc.iterations,
        "met": fits and (min_bytes is None or size >= min_bytes),
        "warning": "; ".join(warnings) or None,
        "width": result.size[0],
        "height": result.size[1],
        "size_bytes": size,
    }
    return result.data, info


def _lossy(enc: _Encoder, fmt: str, max_bytes: int | None, min_bytes: int | None) -> tuple[bytes, dict]:
    extra: dict[str, Any] = {"fast": True} if fmt in ("avif", "heic") else {}
    proxy = _Proxy(enc, dict(extra, lossless=False)) if enc.pixels > PROXY_ABOVE else None
    note = None
    if enc.opts.get("lossless") and fmt in ("webp", "avif", "heic"):
        # The full-size lossless file is never smaller than the proxy's: skip encodes that cannot fit.
        hopeless = (proxy is not None and max_bytes is not None
                    and proxy.lossless_size() * (enc.pixels / proxy.pixels) ** _ALPHA_OPTIMISTIC > max_bytes)
        if not hopeless:
            result = enc.encode(**extra)
            result.quality = None
            if max_bytes is None or len(result.data) <= max_bytes:
                return _finish(enc, result, max_bytes, min_bytes)
        note = "Lossless output was too large; used lossy compression"
    extra["lossless"] = False
    q_user = enc.opts["quality"]
    floor_q = min(MIN_QUALITY, q_user)

    def trial(q: int) -> _Result:
        result = enc.encode(quality=q, **extra)
        if proxy is not None:
            proxy.learn(enc.pixels_at(enc.scale), q, len(result.data))
        return result

    def fits(result: _Result) -> bool:
        return max_bytes is None or len(result.data) <= max_bytes

    sizes: dict[int, int] = {}
    if proxy is not None:
        best = _guided_max(enc, proxy, trial, fits, 1.0, floor_q, q_user, max_bytes)
    else:
        best = trial(q_user)
        sizes[q_user] = len(best.data)
        if not fits(best):
            best = _search_down(trial, floor_q, min(q_user - 1, MAX_QUALITY), fits, sizes)
        if best is None and floor_q not in sizes:
            result = trial(floor_q)
            sizes[floor_q] = len(result.data)
            best = result if fits(result) else None
    if best is None:
        assert max_bytes is not None
        best = _downscaled(enc, trial, fits, q_user, floor_q, sizes, max_bytes, proxy)
    if best is None:
        assert enc.smallest is not None
        return _finish(enc, enc.smallest, max_bytes, min_bytes, note)

    if min_bytes is not None and len(best.data) < min_bytes and (best.quality or 0) < 100:
        enc.set_scale(best.scale)
        if proxy is not None and enc.pixels_at(best.scale) > PROXY_ABOVE:
            best = _guided_min(enc, proxy, trial, fits, best, min_bytes, max_bytes)
        else:
            best = _search_up(trial, fits, best, (best.quality or q_user) + 1, min_bytes)
    return _finish(enc, best, max_bytes, min_bytes, note)


def _search_quality(trial: Any, lo: int, hi: int, fits: Any, sizes: dict[int, int] | None = None) -> _Result | None:
    """Highest quality in [lo, hi] whose encode fits (binary search, ≤ MAX_SEARCH_ENCODES encodes)."""
    best = None
    count = 0
    while lo <= hi and count < MAX_SEARCH_ENCODES:
        mid = (lo + hi) // 2
        result = trial(mid)
        count += 1
        if sizes is not None:
            sizes[mid] = len(result.data)
        if fits(result):
            best, lo = result, mid + 1
        else:
            hi = mid - 1
    return best


def _search_down(trial: Any, floor_q: int, hi: int, fits: Any, sizes: dict[int, int]) -> _Result | None:
    """Highest fitting quality in [floor_q, hi]: probe the middle, then the floor, then bisect.

    Probing the floor second means a target that needs downscaling costs two encodes here, not a
    bisection all the way down to the floor.
    """
    if hi < floor_q:
        return None
    mid = (floor_q + hi) // 2
    probe = trial(mid)
    sizes[mid] = len(probe.data)
    if fits(probe):
        return _search_quality(trial, mid + 1, hi, fits, sizes) or probe
    if mid == floor_q:
        return None
    low = trial(floor_q)
    sizes[floor_q] = len(low.data)
    if not fits(low):
        return None
    return _search_quality(trial, floor_q + 1, mid - 1, fits, sizes) or low


def _search_up(trial: Any, fits: Any, best: _Result, lo: int, min_bytes: int) -> _Result:
    """Lowest quality in [lo, 100] reaching ``min_bytes`` while still fitting; else the largest fitting."""
    hi, count = 100, 0
    candidate: _Result | None = None
    largest = best
    while lo <= hi and count < MAX_SEARCH_ENCODES:
        mid = (lo + hi) // 2
        result = trial(mid)
        count += 1
        if not fits(result):
            hi = mid - 1
        elif len(result.data) >= min_bytes:
            candidate, hi = result, mid - 1
        else:
            if len(result.data) > len(largest.data):
                largest = result
            lo = mid + 1
    return candidate or largest


def _guided_max(enc: _Encoder, proxy: _Proxy, trial: Any, fits: Any, scale: float, lo: int, hi: int,
                max_bytes: int | None, known: _Result | None = None) -> _Result | None:
    """Highest fitting quality in [lo, hi] at ``scale`` (or ``known``, a fit just below ``lo``), checked
    with ≤ MAX_LARGE_ENCODES real encodes."""
    enc.set_scale(scale)
    pixels = enc.pixels_at(scale)
    limit = math.inf if max_bytes is None else max_bytes
    best: _Result | None = known
    top, misses, tried = hi, 0, set()
    for attempt in range(MAX_LARGE_ENCODES):
        floor = best.quality + 1 if best is not None and best.quality is not None else lo
        if floor > top:
            break
        if best is not None:  # improving on a fit: play safe on the last try
            margin = _FIT_MARGINS[1 if attempt == MAX_LARGE_ENCODES - 1 else 0]
        else:
            margin = _FIT_MARGINS[min(misses, len(_FIT_MARGINS) - 1)]
        q = proxy.highest(pixels, floor, top, limit * margin)
        if best is None and (q is None or misses >= 2):
            # About to give up on this size, or the model keeps missing: try the floor for real
            # unless even an optimistic estimate (bytes ∝ pixels**0.5 from the proxy) rules it out.
            if lo in tried or proxy.size(lo) * (pixels / proxy.pixels) ** _ALPHA_OPTIMISTIC > limit:
                break
            q = lo
        elif q is None:
            break
        tried.add(q)
        result = trial(q)
        if fits(result):
            best = result
            if max_bytes is not None and len(result.data) >= _GOOD_ENOUGH * max_bytes:
                break
        else:
            top, misses = q - 1, misses + 1
    return best


def _guided_min(enc: _Encoder, proxy: _Proxy, trial: Any, fits: Any, best: _Result, min_bytes: int,
                max_bytes: int | None) -> _Result:
    """Raise the quality at the current scale until ``min_bytes`` is reached (≤ MAX_LARGE_ENCODES encodes)."""
    pixels = enc.pixels_at(enc.scale)
    lo, hi = (best.quality or enc.opts["quality"]) + 1, 100
    largest = best
    for _ in range(MAX_LARGE_ENCODES):
        if lo > hi:
            break
        q = proxy.lowest(pixels, lo, hi, min_bytes * 1.03)
        if q is None:
            q = hi
        if max_bytes is not None:
            q = min(q, proxy.highest(pixels, lo, hi, max_bytes * _FIT_MARGINS[0]) or q)
        result = trial(q)
        if not fits(result):
            hi = q - 1
        elif len(result.data) >= min_bytes:
            return result
        else:
            if len(result.data) > len(largest.data):
                largest = result
            lo = q + 1
    return largest


def _downscaled(enc: _Encoder, trial: Any, fits: Any, q_user: int, floor_q: int, sizes: dict[int, int],
                max_bytes: int, proxy: _Proxy | None = None) -> _Result | None:
    """Quality 5 is still too big at full size: find the largest scale where a mid quality fits.

    Bytes do not scale with pixel count (detail per pixel changes), so the scale is stepped by
    sqrt(target/size)·0.95 (or, for large images, by the calibrated proxy prediction) until it
    fits and then bisected (≤ MAX_DOWNSCALE_ROUNDS encodes); the quality is finally raised as far as
    the target allows at that scale. Fewer pixels at a decent quality look far better than more
    pixels at quality 5.
    """
    reference = max(floor_q, min(q_user, REFERENCE_QUALITY))
    known = [q for q in sizes if q <= reference]
    q_ref = max(known) if known else reference
    too_big_scale = 1.0
    too_big_size = sizes.get(q_ref, 4 * max_bytes)  # the proxy path predicts sizes instead
    fit: _Result | None = None
    min_scale = enc.min_scale
    for _ in range(MAX_DOWNSCALE_ROUNDS):
        if proxy is not None:
            lo = fit.scale if fit is not None else min_scale
            aim = 0.95 if fit is not None else 0.93
            scale = proxy.scale_for(q_ref, aim * max_bytes, lo, too_big_scale)
            if fit is not None and scale <= fit.scale * 1.001:
                scale = math.sqrt(fit.scale * too_big_scale)
            scale = min(scale, too_big_scale * 0.98)
        elif fit is None:
            scale = max(min_scale, too_big_scale * math.sqrt(max_bytes / too_big_size) * 0.95)
        else:
            scale = math.sqrt(fit.scale * too_big_scale)
        scale = max(scale, min_scale)
        size = enc.size_at(scale)
        if size == enc.size_at(too_big_scale) or (fit is not None and size == fit.size):
            break
        enc.set_scale(scale)
        result = trial(q_ref)
        if fits(result):
            fit = result
            if len(result.data) >= 0.9 * max_bytes:
                break
        else:
            too_big_scale, too_big_size = scale, len(result.data)
            if fit is None and scale <= min_scale:
                break
    if fit is None:
        # Even the smallest allowed size is too big at the reference quality: go below it there.
        return _search_quality(trial, floor_q, q_ref - 1, fits)
    hi = min(q_user, MAX_QUALITY)
    if proxy is not None and enc.pixels_at(fit.scale) > PROXY_ABOVE:
        better = _guided_max(enc, proxy, trial, fits, fit.scale, q_ref + 1, hi, max_bytes, known=fit)
    else:
        enc.set_scale(fit.scale)
        better = _search_quality(trial, q_ref + 1, hi, fits)
    return better or fit


def _lossless(enc: _Encoder, fmt: str, max_bytes: int | None, min_bytes: int | None) -> tuple[bytes, dict]:
    def fits(result: _Result) -> bool:
        return max_bytes is None or len(result.data) <= max_bytes

    result = enc.encode(optimize=True)
    smallest = result
    if fits(result):
        return _finish(enc, result, max_bytes, min_bytes)
    reference = len(result.data)
    for colors in PALETTE_STEPS:
        if fmt == "gif" and colors == 256:
            continue  # GIF is always palettised: the first encode already used 256 colours
        result = enc.encode(optimize=True, colors=colors)
        if colors == 256:
            reference = len(result.data)
        if len(result.data) < len(smallest.data):
            smallest = result
        if fits(result):
            return _finish(enc, result, max_bytes, min_bytes)
    assert max_bytes is not None
    for _round in range(MAX_DOWNSCALE_ROUNDS):
        too_big_scale = enc.scale
        if not enc.downscale(math.sqrt(max_bytes / reference) * 0.95):
            break
        result = enc.encode(optimize=True, colors=256)
        if len(result.data) < len(smallest.data):
            smallest = result
        if fits(result):
            return _finish(enc, _refine_scale(enc, result, too_big_scale, fits), max_bytes, min_bytes)
        reference = len(result.data)
    return _finish(enc, smallest, max_bytes, min_bytes)


def _refine_scale(enc: _Encoder, ok: _Result, too_big: float, fits: Any) -> _Result:
    """Palette images shrink faster than pixel count; bisect back towards the largest size that fits."""
    for _ in range(REFINE_STEPS):
        mid = math.sqrt(ok.scale * too_big)
        if enc.size_at(mid) in (ok.size, enc.size_at(too_big)):
            break
        enc.set_scale(mid)
        result = enc.encode(optimize=True, colors=256)
        if fits(result):
            ok = result
        else:
            too_big = mid
    return ok
