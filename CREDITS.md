# Credits and third-party licences

The fonts, models and programs heyp is built on, and their licences.

heyp itself is free software under the
[GNU Affero General Public License v3.0](https://github.com/uroy80/heyp/blob/main/LICENSE) (AGPL-3.0-or-later). It
stands on the work of many other projects; each component below keeps its own licence. This page is also served by
every running copy at `/credits`.

Exact versions are pinned in `requirements.lock` (Python), the `Dockerfile` / `services/*/Dockerfile` (system
packages, base images) and `scripts/fetch_models.py` (models, with sha256). Every published image also carries a
signed SBOM listing every package and licence it contains:

```bash
gh attestation verify oci://ghcr.io/uroy80/heyp:<tag> -R uroy80/heyp --predicate-type https://spdx.dev/Document/v2.3
```

## Fonts

All fonts are bundled — the site loads nothing from font CDNs.

| Font | Used for | Licence | Source |
|---|---|---|---|
| Fraunces | headlines and the nameplate (UI); text on images | SIL Open Font License 1.1 | [github.com/undercasetype/Fraunces](https://github.com/undercasetype/Fraunces) |
| Libre Franklin | interface text; text on images | SIL Open Font License 1.1 | [github.com/impallari/Libre-Franklin](https://github.com/impallari/Libre-Franklin) |
| IBM Plex Mono | figures and file sizes (UI); text on images | SIL Open Font License 1.1 | [github.com/IBM/plex](https://github.com/IBM/plex) |
| Anton | meme captions, watermarks | SIL Open Font License 1.1 | [github.com/googlefonts/AntonFont](https://github.com/googlefonts/AntonFont) |
| DejaVu Sans, Serif and Sans Mono | text on images, fallback text, SVG rendering | Bitstream Vera licence + public-domain changes | [dejavu-fonts.github.io](https://dejavu-fonts.github.io/) |
| Caveat, Dancing Script, Great Vibes | handwriting-style text on images | SIL Open Font License 1.1 | [Google Fonts](https://github.com/google/fonts/tree/main/ofl) |
| Homemade Apple | handwriting-style text on images | Apache License 2.0 | [Google Fonts](https://github.com/google/fonts/tree/main/apache/homemadeapple) |
| Noto Sans (Bengali, Devanagari, Gujarati, Gurmukhi, Hebrew, Kannada, Malayalam, Oriya, Tamil, Telugu, Thai), Noto Naskh Arabic, Noto Sans CJK SC (subset) | text in other scripts on images | SIL Open Font License 1.1 | [github.com/notofonts](https://github.com/notofonts), [github.com/notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) |

The licence text of each font ships next to it (`static/fonts/LICENSE-*.txt` for the interface fonts,
`app/assets/fonts/licenses/` for the fonts used on images).

## Stickers

Emoji stickers in the photo editor are from **[OpenMoji](https://openmoji.org/)** (release 17.0.0) — the open-source
emoji and icon project — licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Stickers that aren't
OpenMoji were drawn for heyp and are covered by the project licence.

## Image models (run on the heyp server)

| Model file | Used for | Licence | Source |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` (YuNet) | face detection — passport framing, suggested blur areas | MIT | [opencv/opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) |
| `u2net_human_seg.onnx` (U²-Net human segmentation) | passport backgrounds, "person" background removal | Apache-2.0 | [xuebinqin/U-2-Net](https://github.com/xuebinqin/U-2-Net); ONNX file from [danielgatis/rembg](https://github.com/danielgatis/rembg) (MIT) |
| `birefnet-lite-dc-1024.onnx`, `birefnet-lite-dc-1024.int8.onnx` (BiRefNet lite) | background removal for any subject | MIT | [ZhengPeng7/BiRefNet](https://github.com/ZhengPeng7/BiRefNet); ONNX re-exported by heyp (`scripts/export_models/`) |
| `realesr-general-x4v3.onnx`, `realesr-general-x4v3-dn05.onnx` (Real-ESRGAN general x4v3, and its 50 % blend with the `wdn` weights) | upscaling 2×/4× | BSD-3-Clause | [xinntao/Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN); ONNX exported by heyp (`scripts/export_models/`) |
| `yolo-v9-t-640-license-plates-end2end.onnx` (YOLOv9-t licence plates) | suggested blur areas for number plates | MIT (model repository); YOLOv9 architecture GPL-3.0 | [ankandrew/open-image-models](https://github.com/ankandrew/open-image-models), [WongKinYiu/yolov9](https://github.com/WongKinYiu/yolov9) |

The models heyp exports itself are published at
[github.com/uroy80/heyp/releases/tag/models-v1](https://github.com/uroy80/heyp/releases/tag/models-v1), with the scripts
that reproduce them in `scripts/export_models/`. The plate model's training data is not documented by its authors.
None of these models identifies people: face detection finds where a face is, never whose it is. heyp sends no file
to any AI service.

## Engines in the `heyp` image

| Component | Used for | Licence |
|---|---|---|
| [Pillow](https://github.com/python-pillow/Pillow), [pillow-heif](https://github.com/bigcat88/pillow_heif) (libheif LGPL-3.0) | image decoding and encoding, PDF export of photos | MIT-CMU; BSD-3-Clause |
| [OpenCV](https://github.com/opencv/opencv) (headless) | face detection, image operations | Apache-2.0 |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | running the models above | MIT |
| [NumPy](https://numpy.org/) | image arrays | BSD-3-Clause |
| [fontTools](https://github.com/fonttools/fonttools) | per-character font fallback for text on images | MIT |
| [resvg](https://github.com/linebender/resvg) via resvg-py | SVG → image | Apache-2.0 OR MIT |
| [rawpy](https://github.com/letmaik/rawpy) with [LibRaw](https://www.libraw.org/) | camera RAW files | MIT; LibRaw LGPL-2.1 / CDDL-1.0 |
| [psd-tools](https://github.com/psd-tools/psd-tools) | PSD / PSB files | MIT |
| [scour](https://github.com/scour-project/scour) | SVG optimisation | Apache-2.0 |
| [gifsicle](https://www.lcdf.org/gifsicle/) (separate program) | animated GIF compression | GPL-2.0 |
| [pngquant](https://pngquant.org/) (separate program) | PNG compression | GPL-3.0-or-later |
| [FriBiDi](https://github.com/fribidi/fribidi) (`libfribidi0`) | right-to-left and Indic text shaping (with Pillow's bundled raqm) | LGPL-2.1-or-later |
| [FastAPI](https://github.com/fastapi/fastapi), [Starlette](https://github.com/encode/starlette), [Uvicorn](https://github.com/encode/uvicorn), [Pydantic](https://github.com/pydantic/pydantic) | the web server | MIT / BSD-3-Clause |
| [Debian](https://www.debian.org/) 13 "trixie" and [Python](https://www.python.org/) 3.12 (`python:3.12-slim-trixie`) | base system | various free licences; PSF-2.0 |

## Engines in the `heyp-renderer` and `heyp-egress` images (web page → image)

| Component | Licence |
|---|---|
| [Playwright](https://github.com/microsoft/playwright-python) | Apache-2.0 |
| [Chromium](https://www.chromium.org/) headless shell (bundled `LICENSE.headless_shell` and third-party notices) | BSD-3-Clause and others |
| Noto fonts (`fonts-noto-core`) | OFL-1.1 |
| The egress filter (`services/egress/`) | AGPL-3.0 (part of heyp) |

The renderer's seccomp profile (`deploy/seccomp/chromium-userns.json`) is Docker's default profile from
[moby/profiles](https://github.com/moby/profiles) (Apache-2.0) with `clone`, `unshare` and `chroot` added.

## Source code for GPL and AGPL components

The images published at `ghcr.io/uroy80/heyp*` contain programs licensed under the GPL (gifsicle, pngquant, and the
GPL/LGPL libraries of the Debian base system). We distribute them unmodified, as built by Debian or by their authors:

- **Debian packages** (gifsicle, pngquant, FriBiDi, fonts, …): the exact source of every version is kept permanently
  at [snapshot.debian.org](https://snapshot.debian.org/) and browsable at [sources.debian.org](https://sources.debian.org/).
  Inside an image, `dpkg-query -W` lists the exact versions and `/usr/share/doc/<package>/copyright` holds each
  licence.
- **Python packages**: the exact versions are in `requirements.lock`; their sources are on [PyPI](https://pypi.org/)
  (sdists) and in the project repositories linked above.
- **heyp itself**: this repository; every image is labelled with the commit it was built from
  (`org.opencontainers.image.revision`), and the site's footer links to that commit.

If you can't get any of these sources from the places above, write to the maintainer through
[github.com/uroy80/heyp](https://github.com/uroy80/heyp/issues) and we'll send you the complete corresponding source
for any image we published, for at least three years after we last distributed it.

## Thanks

Product inspiration: the many "do one thing to a file" web tools — heyp was written from scratch, with its own name,
design and words.
