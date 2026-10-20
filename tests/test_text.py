"""Server-side text rendering (app/imaging/text.py): fonts, ids, weights, fallback, wrapping, limits."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from app.imaging import text


def ink(img: Image.Image) -> int:
    """Number of visible pixels in an RGBA layer."""
    return int((np.asarray(img.getchannel("A")) > 0).sum())


# ------------------------------------------------------------------ fonts
def test_every_bundled_font_exists_and_renders():
    for spec in text.FONTS.values():
        assert text.font_path(spec.id).is_file()
        if spec.bold_file:
            assert text.font_path(spec.id, bold=True).is_file()
        layer = text.render("Hey 123", font=spec.id, size=32, color="#000")
        assert layer.mode == "RGBA" and ink(layer) > 50, spec.id


def test_font_ids_accept_every_spelling():
    assert text.resolve("libre-franklin").id == "libre_franklin"
    assert text.resolve("libre_franklin").id == "libre_franklin"
    assert text.resolve("IBM-Plex-Mono").id == "plex_mono"
    assert text.resolve("dancing-script").id == "dancing_script"
    assert text.resolve(None).id == text.DEFAULT_FONT
    assert text.resolve("impact").id == "anton"


@pytest.mark.parametrize("bad", ["comic-sans", "../../etc/passwd", "anton.ttf", 12])
def test_unknown_fonts_are_rejected(bad):
    with pytest.raises(ValueError):
        text.resolve(bad)


def test_list_fonts_gives_ui_ids_and_canonical_keys():
    fonts = {f["id"]: f for f in text.list_fonts()}
    assert {"libre-franklin", "fraunces", "anton", "ibm-plex-mono", "dejavu-sans", "caveat", "dancing-script",
            "great-vibes"} <= set(fonts)
    assert fonts["ibm-plex-mono"]["key"] == "plex_mono" and fonts["ibm-plex-mono"]["bold"] is True


def test_weight_makes_variable_and_static_fonts_heavier():
    for font in ("libre-franklin", "dejavu-sans"):
        regular = ink(text.render("Weight", font=font, size=48))
        heavy = ink(text.render("Weight", font=font, size=48, weight=800 if font == "libre-franklin" else 700))
        assert heavy > regular * 1.15, font
    with pytest.raises(ValueError):
        text.get_font("anton", 20, weight=1000)


# ------------------------------------------------------------------ layout
def test_missing_glyphs_fall_back_to_dejavu():
    # Anton has no Greek; the fallback must draw it rather than tofu boxes of identical shape.
    a = text.render("ΑΒΓ", font="anton", size=40)
    b = text.render("ΔΕΖ", font="anton", size=40)
    assert ink(a) > 50 and not np.array_equal(np.asarray(a), np.asarray(b))


def test_wrap_respects_explicit_breaks_and_width():
    lines = text.wrap("one two three four five six", "dejavu-sans", 20, max_width=90)
    assert len(lines) > 1 and all(text.measure(line, "dejavu-sans", 20)[0] <= 90 for line in lines)
    assert text.wrap("a\nb", "dejavu-sans", 20) == ["a", "b"]
    assert len(text.wrap("x" * 60, "dejavu-sans", 20, max_width=100)) > 1  # long words break by characters


def test_fit_size_is_the_largest_size_that_fits():
    size = text.fit_size("Fit me in", "anton", 300, 80, max_size=200)
    w, h = text.measure("Fit me in", "anton", size)
    assert w <= 300 and h <= 80
    w2, h2 = text.measure("Fit me in", "anton", size + 2)
    assert w2 > 300 or h2 > 80


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_alignment_places_short_lines(align):
    layer = np.asarray(text.render("WWWWWW\nI", font="dejavu-sans", size=30, align=align).getchannel("A"))
    second = layer[layer.shape[0] * 2 // 3:]
    cols = np.where(second.max(axis=0) > 0)[0]
    centre = cols.mean() / layer.shape[1]
    expected = {"left": 0.1, "center": 0.5, "right": 0.9}[align]
    assert abs(centre - expected) < 0.15


def test_stroke_opacity_shadow_and_background():
    plain = text.render("Meme", font="anton", size=40, color="#fff")
    stroked = text.render("Meme", font="anton", size=40, color="#fff", stroke_color="#000", stroke_width=3)
    assert stroked.width >= plain.width + 6 and ink(stroked) > ink(plain)
    half = text.render("Meme", font="anton", size=40, color="#fff", opacity=0.5)
    assert np.asarray(half.getchannel("A")).max() <= 128
    boxed = text.render("Meme", font="anton", size=40, color="#fff", background="#ff0000", padding=6)
    assert boxed.getpixel((1, 1)) == (255, 0, 0, 255)
    shadowed = text.render("Meme", font="anton", size=40, color="#fff", shadow="#000", shadow_offset=(3, 3),
                           shadow_blur=2)
    assert shadowed.width > plain.width


def test_empty_and_whitespace_text_is_a_transparent_pixel():
    assert text.render("   ").size == (1, 1)


# ------------------------------------------------------------------ abuse
def test_text_length_and_lines_are_capped():
    with pytest.raises(ValueError, match="too long"):
        text.render("x" * (text.MAX_TEXT_CHARS + 1))
    with pytest.raises(ValueError, match="too many lines"):
        text.wrap("a\n" * (text.MAX_LINES + 2), None, 10)


def test_font_size_is_clamped_and_huge_blocks_refused():
    assert text.get_font("anton", 10**9).size == text.MAX_FONT_PX
    assert text.get_font("anton", -5).size == text.MIN_FONT_PX
    with pytest.raises(ValueError):
        text.get_font("anton", float("nan"))
    with pytest.raises(text.TextTooLarge):
        text.render("W" * 400, font="anton", size=text.MAX_FONT_PX)


def test_control_characters_are_dropped():
    assert text.wrap("a\x00b\x07c", None, 12) == ["abc"]


def test_rotate_rgba_quarter_turns_are_exact_and_others_expand():
    layer = text.render("Rot", font="anton", size=30)
    assert text.rotate_rgba(layer, 90).size == (layer.height, layer.width)
    assert text.rotate_rgba(layer, 360) is layer
    turned = text.rotate_rgba(layer, 30)
    assert turned.width > layer.width and turned.mode == "RGBA"


# ------------------------------------------------------------------ img-02: Indic, CJK, Arabic, Hebrew, Thai
SCRIPTS = {"hindi": "नमस्ते दुनिया", "tamil": "வணக்கம் உலகம்", "bengali": "আমার সোনার বাংলা", "cjk": "你好世界 こんにちは 안녕",
           "arabic": "مرحبا بالعالم", "hebrew": "שלום עולם", "thai": "สวัสดีชาวโลก", "gujarati": "નમસ્તે",
           "telugu": "నమస్కారం", "kannada": "ನಮಸ್ಕಾರ", "malayalam": "നമസ്കാരം", "punjabi": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ", "odia": "ନମସ୍କାର"}


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_every_supported_script_draws_real_glyphs(name):
    sample = SCRIPTS[name]
    for font in ("anton", "libre-franklin", "fraunces", "caveat"):
        spec = text.resolve(font)
        assert all(text._has_glyph(ord(ch), spec, False) for ch in sample), font
        runs = text._runs(sample, spec, False)
        assert all(key is not None for chunk, key in runs if chunk.strip()), (font, runs)
    assert not [w for w in text.text_warnings([sample], "anton") if "empty boxes" in w]
    # the drawn text is not a row of identical notdef boxes
    drawn = np.asarray(text.render(sample, font="anton", size=40).getchannel("A"))
    tofu = np.asarray(text.render("\u179f" * len(sample), font="anton", size=40).getchannel("A"))
    assert drawn.shape != tofu.shape or not np.array_equal(drawn, tofu)


def test_missing_characters_and_unshaped_scripts_are_reported():
    warns = text.text_warnings(["hello \u179f\u17bd"], "anton")   # Khmer: no bundled face has it
    assert any("empty boxes" in w and "\u179f" in w for w in warns)
    assert text.text_warnings(["Hello, w\u00f6rld \u2014 \u0395\u03bb\u03bb\u03ac\u03b4\u03b1 123"], "anton") == []
    shaped = text.text_warnings([SCRIPTS["hindi"], SCRIPTS["arabic"]], "libre-franklin")
    bengali = text.text_warnings([SCRIPTS["bengali"] + "\u0964"], "anton")   # the shared danda is not "Devanagari"
    if text.shaping_available():
        assert shaped == [] and bengali == []
    else:
        assert shaped == ["This server can't shape Devanagari and Arabic text yet, so letters may not join or "
                          "combine correctly"]
        assert bengali == ["This server can't shape Bengali text yet, so letters may not join or combine correctly"]


def test_cjk_without_spaces_wraps_between_characters_and_clusters_stay_whole():
    lines = text.wrap("你好世界你好世界你好世界", "anton", 40, max_width=130)
    assert len(lines) > 1 and "".join(lines) == "你好世界你好世界你好世界"
    for line in text.wrap("क्षत्रियक्षत्रियक्षत्रिय", "anton", 40, max_width=90):
        assert not text.unicodedata.category(line[0]).startswith("M")   # no line starts with a vowel sign
        assert text.unicodedata.combining(line[0]) != 9


def test_mixed_direction_lines_keep_spaces_between_runs():
    runs = text._bidi_runs([("Hello ", None), ("مرحبا ", "noto_naskh_arabic"), ("world", None)], rtl=False)
    assert runs == [("Hello ", None), ("مرحبا", "noto_naskh_arabic"), (" ", "noto_naskh_arabic"), ("world", None)]
    rtl = text._bidi_runs([("مرحبا ", "noto_naskh_arabic"), ("Hello ", None), ("عالم", "noto_naskh_arabic")], rtl=True)
    assert rtl == [("عالم", "noto_naskh_arabic"), (" ", None), ("Hello", None), ("مرحبا ", "noto_naskh_arabic")]


def test_bundled_fallback_faces_exist_with_licences():
    files = text.fallback_files()
    for fb in text.FALLBACKS:
        assert fb.file in files and (text.FONT_DIR / fb.file).is_file()
    licences = text.FONT_DIR / "licenses"
    assert (licences / "OFL-Noto.txt").is_file() and (licences / "OFL-NotoSansCJK.txt").is_file()
    readme = (text.FONT_DIR / "README.md").read_text()
    for fb in text.FALLBACKS:
        assert fb.file in readme
