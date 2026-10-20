"""app.core.files: magic-byte sniffing of uploads (extensions are only hints): images, HTML pages, nothing else."""
from __future__ import annotations

import gzip
import io
import struct
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.core import files as kinds
from app.core.files import FileKindError, sniff


def img_bytes(fmt: str, size=(32, 24), mode="RGB", **kw) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 40) if mode == "RGB" else 128).save(buf, fmt, **kw)
    return buf.getvalue()


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ images
@pytest.mark.parametrize(("fmt", "expected"), [("JPEG", "jpeg"), ("PNG", "png"), ("GIF", "gif"), ("WEBP", "webp"),
                                               ("BMP", "bmp"), ("TIFF", "tiff"), ("ICO", "ico")])
def test_images_by_magic(tmp_path, fmt, expected):
    found = sniff(write(tmp_path, "x.bin", img_bytes(fmt)), "x.bin")
    assert (found.kind, found.format) == ("image", expected)
    assert found.mime.startswith("image/")


def test_heic_and_avif_by_ftyp_brand(tmp_path):
    import pillow_heif

    pillow_heif.register_heif_opener()
    assert sniff(write(tmp_path, "a", img_bytes("HEIF", quality=50))).format == "heic"
    assert sniff(write(tmp_path, "b", img_bytes("AVIF", quality=50))).format == "avif"


def test_the_extension_never_decides_the_kind(tmp_path):
    png_named_pdf = sniff(write(tmp_path, "report.pdf", img_bytes("PNG")), "report.pdf")
    assert (png_named_pdf.kind, png_named_pdf.format) == ("image", "png")
    html_named_jpg = sniff(write(tmp_path, "photo.jpg", b"<!DOCTYPE html><html><p>x"), "photo.jpg")
    assert html_named_jpg.kind == "html"
    text_named_png = sniff(write(tmp_path, "photo.png", b"just some text"), "photo.png")
    assert text_named_png == kinds.Sniffed("other", "png", "application/octet-stream")


def test_psd_psb_svg_svgz_and_raw(tmp_path):
    psd = b"8BPS\x00\x01" + b"\x00" * 6 + struct.pack(">HIIHH", 3, 10, 10, 8, 3) + b"\x00" * 64
    assert sniff(write(tmp_path, "a.psd", psd)).format == "psd"
    psb = b"8BPS\x00\x02" + psd[6:]
    assert sniff(write(tmp_path, "a.psb", psb)).format == "psb"
    svg = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
    assert sniff(write(tmp_path, "a.svg", svg)) == kinds.Sniffed("image", "svg", "image/svg+xml")
    assert sniff(write(tmp_path, "a.svgz", gzip.compress(svg)), "a.svgz").format == "svg"
    raf = b"FUJIFILMCCD-RAW 0201FF383501" + b"\x00" * 100
    assert sniff(write(tmp_path, "a.raf", raf)).format == "raf"
    cr2 = b"II*\x00\x10\x00\x00\x00CR\x02\x00" + b"\x00" * 100
    assert sniff(write(tmp_path, "a.cr2", cr2)).format == "cr2"
    # a TIFF-structured RAW without its own mark is told apart by the extension hint
    assert sniff(write(tmp_path, "b.nef", img_bytes("TIFF")), "b.nef").format == "nef"


def test_xhtml_with_inline_svg_is_html_not_an_image(tmp_path):
    doc = b'<!DOCTYPE html><html><body><svg width="1" height="1"></svg></body></html>'
    assert sniff(write(tmp_path, "page.html", doc)).kind == "html"


# ------------------------------------------------------------------ documents are not heyp's
@pytest.mark.parametrize(("name", "data"), [
    ("a.pdf", b"%PDF-1.7\n1 0 obj << /Type /Catalog >>\nendobj\n%%EOF\n"),
    ("a.docx", b"PK\x03\x04" + b"\x00" * 100),
    ("a.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600),
    ("a.rtf", b"{\\rtf1\\ansi hello}"),
    ("a.txt", "héllo wörld\n".encode()),
    ("id.p12", b"\x30\x82\x01\x00" + b"\x01" * 256),
])
def test_documents_text_and_certificates_are_other(tmp_path, name, data):
    found = sniff(write(tmp_path, name, data), name)
    assert found.kind == "other" and found.mime == "application/octet-stream"
    assert found.format == name.rsplit(".", 1)[1]


def test_zip_archives_are_other(tmp_path):
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("readme", "x")
    assert sniff(write(tmp_path, "a.zip", plain.getvalue()), "a.zip").kind == "other"


# ------------------------------------------------------------------ HTML, the rest
def test_html_detection_and_size_cap(tmp_path, monkeypatch):
    assert sniff(write(tmp_path, "a", b"<!-- c -->\n<!DOCTYPE html><html><p>x")).kind == "html"
    assert sniff(write(tmp_path, "b.html", b"<div>fragment</div>"), "b.html").kind == "html"
    assert sniff(write(tmp_path, "c", b"<div>fragment</div>")).kind == "other"  # no html mark, no hint
    monkeypatch.setattr(kinds, "HTML_MAX_BYTES", 100)
    with pytest.raises(FileKindError) as info:
        sniff(write(tmp_path, "d.html", b"<html>" + b"x" * 200), "d.html")
    assert info.value.status == 413


def test_unknown_bytes_are_other(tmp_path):
    found = sniff(write(tmp_path, "x.exe", b"MZ\x90\x00" + b"\x00" * 100), "x.exe")
    assert found == kinds.Sniffed("other", "exe", "application/octet-stream")
    assert "“.exe” files" in kinds.unsupported_detail("x.exe")
    assert kinds.unsupported_detail("noext") == "This file type can't be opened (unsupported or damaged file)"


def test_accept_parameter():
    assert kinds.parse_accept(None) is None and kinds.parse_accept(" ") is None
    assert kinds.parse_accept("image, html") == frozenset({"image", "html"})
    for bad in ("image,exe", "pdf", "office", "cert"):
        with pytest.raises(ValueError):
            kinds.parse_accept(bad)
    detail = kinds.refused_kind_detail(kinds.Sniffed("html", "html", "text/html"), frozenset({"image"}))
    assert detail == "This is an HTML file; only images can be opened here"
    both = kinds.refused_kind_detail(kinds.Sniffed("image", "png", "image/png"), frozenset({"image", "html"}))
    assert both.endswith("only images or HTML files can be opened here")
