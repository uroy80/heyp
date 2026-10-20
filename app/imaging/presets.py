"""Passport / visa / ID photo presets and print paper sizes.

Fractions are relative to the photo HEIGHT. `eye_*` is measured from the BOTTOM edge,
`top_*` (crown → top edge margin) from the TOP edge. Every preset cites its official source.
`crown_includes_hair=False` marks authorities that measure the head to the top of the skull
("natural top of the head") rather than the top of the hair; `background_edit_allowed=False` marks
those that reject digitally altered photos (so background replacement is off by default).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

MM_PER_INCH = 25.4


@dataclass(frozen=True)
class PassportPreset:
    id: str
    name: str
    country: str
    width_mm: float
    height_mm: float
    dpi: int = 300
    pixel_width: int | None = None
    pixel_height: int | None = None
    head_min: float = 0.0
    head_max: float = 0.0
    eye_min: float | None = None
    eye_max: float | None = None
    top_min: float | None = None
    top_max: float | None = None
    background: str = "#ffffff"
    notes: str = ""
    source: str = ""
    background_edit_allowed: bool = True  # False: the authority rejects digitally altered backgrounds
    crown_includes_hair: bool = True      # False: head height is chin → top of the skull, hair excluded

    def output_size(self) -> tuple[int, int]:
        """Pixel size of the finished photo."""
        if self.pixel_width and self.pixel_height:
            return self.pixel_width, self.pixel_height
        return (round(self.width_mm / MM_PER_INCH * self.dpi),
                round(self.height_mm / MM_PER_INCH * self.dpi))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["width_px"], d["height_px"] = self.output_size()
        return d


@dataclass(frozen=True)
class Paper:
    id: str
    name: str
    width_mm: float
    height_mm: float
    borderless: bool = False  # photo-lab paper: margins may shrink to 0 to fit more copies

    def to_dict(self) -> dict:
        return asdict(self)


PAPERS: dict[str, Paper] = {p.id: p for p in [
    Paper("4x6", "4×6 in", 101.6, 152.4, borderless=True),
    Paper("5x7", "5×7 in", 127.0, 177.8, borderless=True),
    Paper("10x15", "10×15 cm", 100.0, 150.0, borderless=True),
    Paper("13x18", "13×18 cm", 130.0, 180.0, borderless=True),
    Paper("a5", "A5", 148.0, 210.0),
    Paper("a4", "A4", 210.0, 297.0),
    Paper("letter", "US Letter", 215.9, 279.4),
]}

# Researched table (checked 2026-10). Head ranges are chin→crown as a fraction of photo height;
# where an authority measures to the top of the skull (hair excluded) the note says so.
# Order: "us" first, then by display name.
_PRESETS: list[PassportPreset] = [
    PassportPreset(
        id="us", name="United States — passport & visa (2×2 in)", country="US",
        width_mm=50.8, height_mm=50.8, dpi=300, pixel_width=600, pixel_height=600,
        head_min=1.0 / 2.0, head_max=1.375 / 2.0, eye_min=1.125 / 2.0, eye_max=1.375 / 2.0,
        background="#ffffff",
        notes="Head 1–1⅜ in (25–35 mm, 50–69 %) chin to top of hair; eyes 1⅛–1⅜ in (56–69 %) from bottom. "
              "White/off-white background, neutral expression, no glasses. Digital: square, 600×600–1200×1200 px. "
              "USCIS green-card photos use the same spec.",
        source="https://travel.state.gov/en/passports/apply/help/photos.html",
    ),
    PassportPreset(
        id="au", name="Australia — passport (35×45 mm)", country="AU",
        width_mm=35.0, height_mm=45.0, dpi=300,
        head_min=32.0 / 45.0, head_max=36.0 / 45.0,
        background="#ffffff",
        notes="Photo 35–40 mm wide × 45–50 mm high; face chin to crown 32–36 mm. Plain white or light grey "
              "background, neutral expression, under 6 months old. Glossy dye-sublimation print, no inkjet.",
        source="https://www.passports.gov.au/passport-photos",
    ),
    PassportPreset(
        id="ca", name="Canada — passport (50×70 mm)", country="CA",
        width_mm=50.0, height_mm=70.0, dpi=300,
        head_min=31.0 / 70.0, head_max=36.0 / 70.0,
        background="#ffffff",
        notes="Face chin to crown (top of head; IRCC puts the crown where the top of the skull would be when hair "
              "hides it) 31–36 mm. Plain white or light-coloured background, neutral expression, mouth closed; no tinted "
              "lenses or glare. Two identical prints by a commercial photographer, under 6 months old, not altered in any "
              "way (changing the background counts as altering).",
        source="https://www.canada.ca/en/immigration-refugees-citizenship/services/canadian-passports/photos.html",
        background_edit_allowed=False,
        crown_includes_hair=False,
    ),
    PassportPreset(
        id="ca-digital", name="Canada — passport online renewal (digital)", country="CA",
        width_mm=50.8, height_mm=76.2, dpi=600, pixel_width=1200, pixel_height=1800,
        head_min=0.45, head_max=0.50,
        background="#ffffff",
        notes="Online renewal only: JPEG, 3:2 portrait, 1200×1800 px minimum (max 3000×4500), 200 KB–5 MB. "
              "Face chin to crown (top of head; the top of the skull where hair hides it, as for prints) 45–50 % of height. "
              "Must be the camera original, not altered in any way (background changes included); not for printing.",
        source="https://www.canada.ca/en/immigration-refugees-citizenship/services/canadian-passports/photos.html",
        background_edit_allowed=False,
        crown_includes_hair=False,
    ),
    PassportPreset(
        id="ca-visa", name="Canada — visa & immigration (35×45 mm)", country="CA",
        width_mm=35.0, height_mm=45.0, dpi=300,
        head_min=31.0 / 45.0, head_max=36.0 / 45.0,
        background="#ffffff",
        notes="IRCC temporary-resident/immigration photos: frame at least 35×45 mm, chin to crown 31–36 mm "
              "(crown = top of skull where hair hides it). Plain white or light background, neutral expression; digital "
              "photos must not be altered in any way.",
        source="https://www.canada.ca/en/immigration-refugees-citizenship/services/application/application-forms-guides/temporary-resident-visa-application-photograph-specifications.html",
        background_edit_allowed=False,
        crown_includes_hair=False,
    ),
    PassportPreset(
        id="cn", name="China — visa (33×48 mm)", country="CN",
        width_mm=33.0, height_mm=48.0, dpi=300,
        head_min=28.0 / 48.0, head_max=33.0 / 48.0, top_min=3.0 / 48.0, top_max=5.0 / 48.0,
        background="#ffffff",
        notes="Head height 28–33 mm, head width 15–22 mm, crown to top edge 3–5 mm, at least 7 mm below the chin. "
              "White or near-white background, no border; ears visible; no thick-rimmed or tinted glasses.",
        source="https://pdf.visaforchina.cn/HKG4_EN/tongzhigonggao/551650028926668847.html",
    ),
    PassportPreset(
        id="fr", name="France — passport & ID card (35×45 mm)", country="FR",
        width_mm=35.0, height_mm=45.0, dpi=300,
        head_min=32.0 / 45.0, head_max=36.0 / 45.0,
        background="#e5e7eb",
        notes="Face 32–36 mm measured chin to top of skull EXCLUDING hair (70–80 %). Plain light background "
              "(light grey or light blue) — white is not allowed. Neutral expression, mouth closed, untinted glasses only.",
        source="https://www.service-public.gouv.fr/particuliers/vosdroits/F10619",
        crown_includes_hair=False,
    ),
    PassportPreset(
        id="in-oci", name="India — OCI card (2×2 in)", country="IN",
        width_mm=50.8, height_mm=50.8, dpi=300,
        head_min=1.0 / 2.0, head_max=1.375 / 2.0, eye_min=1.125 / 2.0, eye_max=1.375 / 2.0,
        background="#ffffff",
        notes="2×2 in (51×51 mm); full head top of hair to chin 1–1⅜ in; eyes 1⅛–1⅜ in from bottom. "
              "Plain light background, natural expression, no tinted glasses. Upload limits are shown by the portal.",
        source="https://ociservices.gov.in/Photo-Spec-FINAL.pdf",
    ),
    PassportPreset(
        id="in", name="India — passport (ICAO, 630×810 px)", country="IN",
        width_mm=35.0, height_mm=45.0, dpi=457, pixel_width=630, pixel_height=810,
        head_min=0.80, head_max=0.85,
        background="#ffffff",
        notes="ICAO-compliant photo, 630×810 px colour; close-up of head and top of shoulders with the face taking "
              "up 80–85 % of the photo (read here as head height, top of hair to chin). White background, unaltered, "
              "glasses off, mouth closed, eyes open. Required by Indian missions since late 2025. 35×45 mm print "
              "size is nominal (matches the 7:9 aspect).",
        source="https://www.cgiauckland.gov.in/page/specifications-for-the-passport-photos/",
        background_edit_allowed=False,
    ),
    PassportPreset(
        id="in-visa", name="India — visa (2×2 in)", country="IN",
        width_mm=50.8, height_mm=50.8, dpi=300,
        head_min=1.0 / 2.0, head_max=1.375 / 2.0, eye_min=1.125 / 2.0, eye_max=1.375 / 2.0,
        background="#ffffff",
        notes="Square photo; head 1–1⅜ in (25–35 mm); eyes 1⅛–1⅜ in from bottom. Plain light or white background. "
              "Regular visa upload: JPEG 10–300 KB (e-Visa portal shows its own limits).",
        source="https://indianvisaonline.gov.in/visa/instruction.html",
    ),
    PassportPreset(
        id="jp", name="Japan — passport (35×45 mm)", country="JP",
        width_mm=35.0, height_mm=45.0, dpi=300,
        head_min=32.0 / 45.0, head_max=36.0 / 45.0, top_min=2.0 / 45.0, top_max=6.0 / 45.0,
        background="#ffffff",
        notes="Crown to chin 34 ± 2 mm (32–36 mm); crown to top edge 4 ± 2 mm (2–6 mm); no border. Plain "
              "background with no scenery or shadow; hatless, facing front; under 6 months old. Spec follows ICAO.",
        source="https://www.mofa.go.jp/mofaj/toko/passport/ic_photo.html",
    ),
    PassportPreset(
        id="my", name="Malaysia — passport (35×50 mm)", country="MY",
        width_mm=35.0, height_mm=50.0, dpi=300,
        head_min=25.0 / 50.0, head_max=30.0 / 50.0,
        background="#ffffff",
        notes="Two 35×50 mm photos on a WHITE background without shadow; face 25–30 mm (50–60 % of the photo, "
              "per ICAO). In Malaysia the photo is usually captured at the immigration counter.",
        source="https://www.kln.gov.my/web/pak_islamabad/passport_renewal",
    ),
    PassportPreset(
        id="nl", name="Netherlands — passport & ID card (35×45 mm)", country="NL",
        width_mm=35.0, height_mm=45.0, dpi=400,
        head_min=26.0 / 45.0, head_max=30.0 / 45.0,
        background="#ffffff",
        notes="Face chin to crown 26–30 mm from age 11 (19–30 mm under 11); face width 16–20 mm excluding ears. "
              "Plain grey, light blue or white background; neutral expression; transparent glasses only. "
              "Fotomatrix 2020 requires prints of at least 400 dpi on glossy photo paper.",
        source="https://www.government.nl/topics/identification-documents/requirements-for-photos",
    ),
    PassportPreset(
        id="nz-visa", name="New Zealand — visa & NZeTA (digital)", country="NZ",
        width_mm=76.2, height_mm=101.6, dpi=300, pixel_width=900, pixel_height=1200,
        head_min=0.75, head_max=0.75, eye_min=0.50, eye_max=0.70,
        background="#e5e7eb",
        notes="Immigration NZ online photo: JPEG 3:4, 900×1200 to 2250×3000 px, 500 KB–3 MB. Head length 75 % of "
              "the frame, head width 70 %, eyes 50–70 % up from the bottom. Light, not white, background.",
        source="https://www.immigration.govt.nz/assets/inz/documents/apply-for-a-visa/Taking-acceptable-visa-photos.pdf",
    ),
    PassportPreset(
        id="schengen", name="Schengen / EU — passport & visa (35×45 mm)", country="EU",
        width_mm=35.0, height_mm=45.0, dpi=300,
        head_min=32.0 / 45.0, head_max=36.0 / 45.0,
        background="#ffffff",
        notes="ICAO layout: chin to crown 32–36 mm (71–80 %), measured to the natural top of the head (hairstyle "
              "ignored); eyes level inside the template's eye zone. Plain, uniform white or light background. Used by "
              "Germany (passport/ID photos must be submitted digitally since 1 May 2025) and Italy (35×45 mm, face "
              "70–80 %). France and the Netherlands have their own presets; Spain uses 26×32 mm (use Custom).",
        source="https://riga.diplo.de/blob/2495124/0c16d00db5412ff133a7930bf2e07a8e/photo-template-data.pdf",
        crown_includes_hair=False,
    ),
    PassportPreset(
        id="sg", name="Singapore — passport & IC (35×45 mm)", country="SG",
        width_mm=35.0, height_mm=45.0, dpi=290, pixel_width=400, pixel_height=514,
        head_min=32.0 / 45.0, head_max=36.0 / 45.0,
        background="#ffffff",
        notes="e-Services upload 400×514 px (JPG/JPEG/HEIC/HEIF/PNG, max 8 MB). White background, shoulders and "
              "hair fully visible, neutral expression, untinted glasses, under 3 months old, matte or semi-matte "
              "print, unaltered. ICA gives no head size and states its rules follow ISO/ICAO, so the ICAO 32–36 mm "
              "on 45 mm range is used; the 35×45 mm print size is nominal (matches 400×514 px).",
        source="https://www.ica.gov.sg/photo-guidelines",
        background_edit_allowed=False,
    ),
    PassportPreset(
        id="za", name="South Africa — passport & ID (35×45 mm)", country="ZA",
        width_mm=35.0, height_mm=45.0, dpi=300,
        head_min=29.0 / 45.0, head_max=34.0 / 45.0,
        background="#e5e7eb",
        notes="DHA template: chin to crown at least 29 mm, at most 34 mm (the sheet also says the face fills "
              "70–80 %). Light grey or plain cream background, neutral expression, no tinted lenses; under 1 month "
              "old; two identical professionally printed photos.",
        source="https://dirco.gov.za/uk/wp-content/uploads/sites/5/2024/04/Passport_Photo_Requirements.pdf",
    ),
    PassportPreset(
        id="kr", name="South Korea — passport (35×45 mm)", country="KR",
        width_mm=35.0, height_mm=45.0, dpi=300, pixel_width=413, pixel_height=531,
        head_min=32.0 / 45.0, head_max=36.0 / 45.0,
        background="#ffffff",
        notes="Head 3.2–3.6 cm from top of skull (hair excluded) to chin; online upload 413×531 px recommended. "
              "Uniform white background, no border; digitally removing or replacing the background is NOT accepted.",
        source="https://www.passport.go.kr/home/kor/contents.do?menuPos=32",
        background_edit_allowed=False,
        crown_includes_hair=False,
    ),
    PassportPreset(
        id="uk", name="United Kingdom — passport (35×45 mm)", country="GB",
        width_mm=35.0, height_mm=45.0, dpi=300,
        head_min=29.0 / 45.0, head_max=34.0 / 45.0,
        background="#e5e7eb",
        notes="Printed: chin to crown 29–34 mm, crown = top of the head, NOT the top of the hair. Plain cream or "
              "light grey background (5–10 % grey recommended), under 1 month old, unaltered by software; no glasses "
              "unless you must. Digital (online): at least 600×750 px, 50 KB–10 MB, plain light background.",
        source="https://www.gov.uk/photos-for-passports/photo-requirements",
        background_edit_allowed=False,
        crown_includes_hair=False,
    ),
    PassportPreset(
        id="us-dv", name="United States — Diversity Visa entry (600×600 px)", country="US",
        width_mm=50.8, height_mm=50.8, dpi=300, pixel_width=600, pixel_height=600,
        head_min=1.0 / 2.0, head_max=1.375 / 2.0, eye_min=1.125 / 2.0, eye_max=1.375 / 2.0,
        background="#ffffff",
        notes="DV lottery entry: JPEG, square, exactly 600×600 px, at most 240 KB; head 50–69 % (chin to top of "
              "head), eyes 56–69 % up from the bottom. Plain white/off-white background; no glasses.",
        source="https://travel.state.gov/content/travel/en/us-visas/visa-information-resources/photos.html",
    ),
]

PRESETS: dict[str, PassportPreset] = {p.id: p for p in _PRESETS}


def get_preset(preset_id: str) -> PassportPreset:
    return PRESETS[preset_id]


def list_presets() -> list[dict]:
    return [p.to_dict() for p in PRESETS.values()]


def list_papers() -> list[dict]:
    return [p.to_dict() for p in PAPERS.values()]


def custom_preset(width_mm: float, height_mm: float, *, dpi: int = 300,
                  head_min: float = 0.5, head_max: float = 0.69,
                  eye_min: float | None = None, eye_max: float | None = None,
                  background: str = "#ffffff",
                  pixel_width: int | None = None, pixel_height: int | None = None,
                  crown_includes_hair: bool = True) -> PassportPreset:
    return PassportPreset(
        id="custom", name="Custom", country="", width_mm=width_mm, height_mm=height_mm, dpi=dpi,
        pixel_width=pixel_width, pixel_height=pixel_height, head_min=head_min, head_max=head_max,
        eye_min=eye_min, eye_max=eye_max, background=background, crown_includes_hair=crown_includes_hair,
    )


__all__ = ["PassportPreset", "Paper", "PAPERS", "PRESETS", "get_preset", "list_presets",
           "list_papers", "custom_preset", "replace", "MM_PER_INCH"]
