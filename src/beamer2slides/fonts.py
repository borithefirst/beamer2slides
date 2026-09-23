"""Interpret the font names found in TeX-produced PDFs."""

import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class FontInfo:
    family: str  # sans | serif | mono | math | icon
    bold: bool = False
    italic: bool = False
    smallcaps: bool = False
    design_size: float | None = None  # TeX optical size (CMSS10 -> 10, SFSS1200 -> 12)


MATH_PREFIXES = (
    "CMMI", "CMSY", "CMEX", "CMBSY", "CMMIB", "MSAM", "MSBM", "EUFM", "EUFB", "EUSM", "EUSB",
    "EURM", "EURB", "RSFS", "STMARY", "WASY", "LASY", "LATINMODERNMATH", "STIXMATH",
    "STIXTWOMATH", "XITSMATH", "CAMBRIAMATH", "FIRAMATH", "NEWCMMATH",
    # newtx / newpx / txfonts / pxfonts math: letters (NewTXMI, txmiaX), symbols (txsys), extension
    "NEWTXMI", "NEWTXBMI", "NEWPXMI", "NEWPXBMI", "NTXMI", "NTXBMI", "NTXSY", "NTXEX", "TXMI",
    "TXBMI", "TXSY", "TXBSY", "TXEX", "PXMI", "PXBMI", "PXSY", "PXBSY", "PXEX",
)  # plus any name containing MATH
# Math fonts whose letters are italic (math_text): CMMI and its cousins; PazoMath-Italic and
# the like say so in their name.
MATH_ITALIC_RE = re.compile(r"^(CMMI|LMMATHITALIC|NEWTXB?MI|NEWPXB?MI|NTXB?MI|TXB?MI|PXB?MI)|ITALIC")
# Symbol fonts whose glyphs are pictures, not letters (ccicons, fontawesome, pifont, marvosym, ...;
# LaTeX's picture-mode line and circle pieces: \cancel strokes, \circle)
ICON_FONT_RE = re.compile(r"CCICONS|FONTAWESOME|DINGBAT|ZAPF|MARVOSYM|WEBDINGS|WINGDINGS|ACADEMICONS|BBDING|"
                          r"^PZDR|UTFSYM|OCTICONS|MATERIALICONS|^L(INE|CIRCLE)W?10$")

# Computer Modern Type 1 names: CM<variant><size>
CM_VARIANTS = {
    "R": ("serif", False, False, False), "B": ("serif", True, False, False),
    "BX": ("serif", True, False, False), "TI": ("serif", False, True, False),
    "SL": ("serif", False, True, False), "U": ("serif", False, True, False),
    "BXTI": ("serif", True, True, False), "BXSL": ("serif", True, True, False),
    "CSC": ("serif", False, False, True), "SS": ("sans", False, False, False),
    "SSBX": ("sans", True, False, False), "SSDC": ("sans", True, False, False),
    "SSI": ("sans", False, True, False), "SSQ": ("sans", False, False, False),
    "SSQI": ("sans", False, True, False), "TT": ("mono", False, False, False),
    "ITT": ("mono", False, True, False), "SLTT": ("mono", False, True, False),
    "TCSC": ("mono", False, False, True), "VTT": ("mono", False, False, False),
}
# EC (T1-encoded CM) names: SF<variant><size*100>
EC_VARIANTS = {
    "RM": ("serif", False, False, False), "BX": ("serif", True, False, False),
    "TI": ("serif", False, True, False), "SL": ("serif", False, True, False),
    "BI": ("serif", True, True, False), "CC": ("serif", False, False, True),
    "XC": ("serif", True, False, True), "SS": ("sans", False, False, False),
    "SX": ("sans", True, False, False), "SI": ("sans", False, True, False),
    "TT": ("mono", False, False, False), "IT": ("mono", False, True, False),
}


# PDF font name prefix (spaces removed) -> Google Fonts family available in Google Slides.
GOOGLE_FAMILIES = {
    "FiraSans": "Fira Sans", "FiraSansCondensed": "Fira Sans Condensed", "FiraMono": "Fira Mono",
    "FiraCode": "Fira Code", "SourceSansPro": "Source Sans Pro", "SourceSans3": "Source Sans 3",
    "SourceSerifPro": "Source Serif Pro", "SourceSerif4": "Source Serif 4", "SourceCodePro": "Source Code Pro",
    "Roboto": "Roboto", "RobotoMono": "Roboto Mono", "RobotoSlab": "Roboto Slab", "RobotoCondensed": "Roboto Condensed",
    "OpenSans": "Open Sans", "Lato": "Lato", "Montserrat": "Montserrat", "NotoSans": "Noto Sans",
    "NotoSerif": "Noto Serif", "NotoSansMono": "Noto Sans Mono", "Inter": "Inter", "IBMPlexSans": "IBM Plex Sans",
    "IBMPlexSerif": "IBM Plex Serif", "IBMPlexMono": "IBM Plex Mono", "Raleway": "Raleway",
    "Merriweather": "Merriweather", "MerriweatherSans": "Merriweather Sans", "PTSans": "PT Sans",
    "PTSerif": "PT Serif", "PTMono": "PT Mono", "EBGaramond": "EB Garamond", "Oswald": "Oswald",
    "Poppins": "Poppins", "Nunito": "Nunito", "NunitoSans": "Nunito Sans", "Ubuntu": "Ubuntu",
    "UbuntuMono": "Ubuntu Mono", "Inconsolata": "Inconsolata", "Cabin": "Cabin", "Karla": "Karla",
    "WorkSans": "Work Sans", "Carlito": "Carlito", "Caladea": "Caladea", "Arimo": "Arimo", "Tinos": "Tinos",
    "Cousine": "Cousine", "JetBrainsMono": "JetBrains Mono", "Spectral": "Spectral", "Lora": "Lora",
    "CrimsonText": "Crimson Text", "CrimsonPro": "Crimson Pro", "LibreBaskerville": "Libre Baskerville",
    "Mulish": "Mulish", "Rubik": "Rubik", "Manrope": "Manrope", "DejaVuSans": None, "Alegreya": "Alegreya",
    "AlegreyaSans": "Alegreya Sans", "Cormorant": "Cormorant", "Arvo": "Arvo", "Quicksand": "Quicksand",
    # Open-source Google Sans (themes/google); Slides has both families.
    "GoogleSansFlex": "Google Sans Flex", "GoogleSansCode": "Google Sans Code",
    # Metric-compatible stand-ins for classic PostScript fonts used via helvet/mathptmx/courier
    # or TeX Gyre, and for Office fonts; all available in Slides.
    "Helvetica": "Arial", "NimbusSanL": "Arial", "NimbusSans": "Arial", "TeXGyreHeros": "Arial",
    "Arial": "Arial", "ArialMT": "Arial", "LiberationSans": "Arial",
    "Times": "Times New Roman", "TimesNewRoman": "Times New Roman", "TimesNewRomanPSMT": "Times New Roman",
    "NimbusRomNo9L": "Times New Roman", "NimbusRoman": "Times New Roman", "TeXGyreTermes": "Times New Roman",
    "LiberationSerif": "Times New Roman",
    "Courier": "Courier New", "CourierNew": "Courier New", "NimbusMonL": "Courier New",
    "NimbusMonoPS": "Courier New", "TeXGyreCursor": "Courier New", "LiberationMono": "Courier New",
    "Calibri": "Carlito", "Cambria": "Caladea", "Georgia": "Georgia", "Verdana": "Verdana",
    # newtx's text face is TeX Gyre Termes with extra glyphs ("…X", peeled in google_font).
    # Math fonts drawn to match a text face: their letters are that face's.
    "FiraMath": "Fira Sans", "CambriaMath": "Caladea",
    # Monospaced faces 0.6 em wide (measured on their PDFs: every glyph one advance): any 0.6 em
    # monospaced Google font sets them at the PDF's widths, at the PDF's size. Roboto Mono is the
    # one whose advances emit knows (emit.slides_width). (CMTT-like 0.525 em faces - t1xtt, txtt -
    # stay family mono, sized by the CMTT factor.)
    "BeraSansMono": "Roboto Mono", "DejaVuSansMono": "Roboto Mono", "VeraSansMono": "Roboto Mono",
    "BitstreamVeraSansMono": "Roboto Mono", "LinLibertineMT": "Roboto Mono", "LinLibertineM": "Roboto Mono",
}
# Families a name does not say the class of (no "Sans", "Mono" in it), by the start of the family
# part of the name, lower case without spaces. Unknown faces get the calibrated substitute of
# their class (emit.FONT_FOR_FAMILY): a sans deck stays sans, code stays monospaced.
SANS_FAMILIES = (
    "segoe", "tahoma", "trebuchet", "verdana", "calibri", "carlito", "candara", "corbel", "arial",
    "arimo", "helvet", "nimbussan", "texgyreheros", "texgyreadventor", "urwgothic", "avant", "futura",
    "gill", "optima", "myriad", "frutiger", "univers", "avenir", "lucidagrande", "lucidasans",
    "cantarell", "inter", "fira", "roboto", "lato", "montserrat", "raleway", "poppins", "nunito", "ubuntu",
    "cabin", "karla", "mulish", "rubik", "manrope", "quicksand", "oswald", "worksans", "ibmplexsans",
    "opensans", "sourcesans", "notosans", "dejavusans", "liberationsans", "googlesans", "biolinum",
    "linbiolinum", "meiryo", "malgun", "pingfang", "simhei", "microsoftyahei", "hiraginokaku",
    "hiraginosans", "sourcehansans",
)
MONO_FAMILIES = ("menlo", "monaco", "lucidaconsole", "andale", "cousine", "inconsolata", "ptmono", "sfmono")
# TeX's own typewriter font names: t1xtt, txtt, newtxtt, pxtt (CMTT-like, 0.525 em)
TEX_TT_RE = re.compile(r"(T1X|NEWTX|TX|PX)TT[A-Z]*\d*")
# The libertine package's Type 1 names: LinLibertine / LinBiolinum, a variant (Display, Mono,
# Initials, Keyboard, C...), T (Type 1) or O (OpenType), then B / Z (bold, semibold), I / O (slant).
LIBERTINE_RE = re.compile(r"LIN(LIBERTINE|BIOLINUM)(DISPLAY|M|I|K|C)?([TO])([BZ])?([IO])?")
WEIGHTS = [("thin", 100), ("hairline", 100), ("extralight", 200), ("ultralight", 200), ("light", 300),
           ("book", 400), ("regular", 400), ("medium", 500), ("semibold", 600), ("demibold", 600),
           ("extrabold", 800), ("ultrabold", 800), ("bold", 700), ("black", 900), ("heavy", 900)]


@lru_cache(maxsize=None)
def google_font(name: str) -> tuple[str, int, bool] | None:
    """(Google family, weight, italic) when the PDF font is itself a Google font, e.g.
    'ABCDEF+FiraSans-LightItalic' -> ('Fira Sans', 300, True)."""
    base = name.split("+", 1)[-1]
    base = re.sub(r"-Identity-H$", "", base)
    family_part, _, style = base.partition("-")
    family_part = family_part.replace(" ", "")
    # TeX Gyre and friends come lower-cased from some engines ("texgyreheros-bold").
    canonical = {k.lower(): k for k in GOOGLE_FAMILIES}
    family_part = canonical.get(family_part.lower(), family_part)
    style_l = style.lower()
    # "FiraSansLight" style names without a hyphen: peel a known weight suffix off the family.
    if family_part not in GOOGLE_FAMILIES and family_part[-1:] in "xX" and family_part[:-1].lower() in canonical:
        family_part = canonical[family_part[:-1].lower()]  # newtx's TeXGyreTermesX: Termes with extra glyphs
    if family_part not in GOOGLE_FAMILIES:
        for word, _ in WEIGHTS:
            if family_part.lower().endswith(word) and family_part[:-len(word)] in GOOGLE_FAMILIES:
                style_l = word + style_l
                family_part = family_part[:-len(word)]
                break
    family = GOOGLE_FAMILIES.get(family_part)
    if not family:
        return None
    weight = next((w for word, w in WEIGHTS if word in style_l), 400)
    if weight == 400 and re.search(r"(^|[^a-z])(medi|bold)", style_l):  # Nimbus: "MediItal", "Bold"
        weight = 700
    italic = any(k in style_l for k in ("italic", "oblique", "ital", "obli")) or style_l.endswith("it")
    return family, weight, italic


@lru_cache(maxsize=None)
def font_info(name: str) -> FontInfo:
    base = name.split("+", 1)[-1]
    key = re.sub(r"[^A-Z0-9]", "", base.upper())
    if ICON_FONT_RE.search(key):
        return FontInfo("icon")
    if key.startswith(MATH_PREFIXES) or "MATH" in key:
        return FontInfo("math")

    m = re.fullmatch(r"CM([A-Z]+)(\d+)", key)
    if m and m.group(1) in CM_VARIANTS:
        return FontInfo(*CM_VARIANTS[m.group(1)], design_size=float(m.group(2)))
    m = re.fullmatch(r"SF([A-Z]{2})(\d{4})", key)
    if m and m.group(1) in EC_VARIANTS:
        return FontInfo(*EC_VARIANTS[m.group(1)], design_size=int(m.group(2)) / 100)

    # Latin Modern (LMSans10-Bold, LMRomanCaps10-Regular, LMMono10-Italic, ...) and
    # anything else: read the class and the style from the name.
    size = re.search(r"(\d+(?:\.\d+)?)", base)
    design = float(size.group(1)) if size and base.startswith("LM") else None
    lower = base.lower()
    family_part, _, style = lower.partition("-")
    family_part = family_part.replace(" ", "").replace("_", "")
    m = LIBERTINE_RE.fullmatch(key)
    if m:  # LinBiolinumTBO: Type 1 (T), bold (B), oblique (O)
        face, variant, _, weight, slant = m.groups()
        return FontInfo("mono" if variant == "M" else "sans" if face == "BIOLINUM" else "serif",
                        bold=bool(weight), italic=bool(slant))
    if any(k in lower for k in ("mono", "courier", "consol", "typewriter")) or re.search(r"(?<!uni)code", lower) \
            or family_part.startswith(MONO_FAMILIES) or key.startswith("LMTT") or TEX_TT_RE.fullmatch(key):
        family = "mono"
    elif (any(k in lower for k in ("sans", "gothic", "grotesk", "grotesque")) or family_part.startswith(SANS_FAMILIES)) \
            and "slab" not in family_part and "serif" not in family_part:  # Roboto Slab, Noto Serif
        family = "sans"
    else:
        family = "serif"
    words = re.findall(r"[a-z]+", style)
    return FontInfo(
        family,
        bold=any(k in lower for k in ("bold", "black", "heavy", "demi", "semibold", "dark"))
        # URW's Medi is Times' bold (NimbusRomNo9L-Medi, -MediItal); Medium is not bold
        or any(w.startswith("medi") and not w.startswith("medium") for w in words),
        # URW's Ital / Obli (URWPalladioL-Ital, NimbusSanL-ReguItal), Adobe's It (MinionPro-BoldIt)
        italic=any(k in lower for k in ("italic", "oblique", "slant", "kursiv"))
        or any(k in style for k in ("ital", "obli")) or style.endswith("it"),
        # LMRomanCaps10, TeXPalladioL-SC, Palatino-RomanSC, MinionPro-SmallCaps
        smallcaps="caps" in lower or style.endswith("sc") or "smcp" in lower,
        design_size=design,
    )
