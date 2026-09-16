"""Interpret the font names found in TeX-produced PDFs."""

import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class FontInfo:
    family: str  # sans | serif | mono | math
    bold: bool = False
    italic: bool = False
    smallcaps: bool = False
    design_size: float | None = None  # TeX optical size (CMSS10 -> 10, SFSS1200 -> 12)


MATH_PREFIXES = (
    "CMMI", "CMSY", "CMEX", "CMBSY", "CMMIB", "MSAM", "MSBM", "EUFM", "EUFB", "EUSM", "EUSB",
    "EURM", "EURB", "RSFS", "STMARY", "WASY", "LASY", "LATINMODERNMATH", "STIXMATH",
    "STIXTWOMATH", "XITSMATH", "CAMBRIAMATH", "FIRAMATH", "NEWCMMATH",
)  # plus any name containing MATH

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


@lru_cache(maxsize=None)
def font_info(name: str) -> FontInfo:
    base = name.split("+", 1)[-1]
    key = re.sub(r"[^A-Z0-9]", "", base.upper())
    if key.startswith(MATH_PREFIXES) or "MATH" in key:
        return FontInfo("math")

    m = re.fullmatch(r"CM([A-Z]+)(\d+)", key)
    if m and m.group(1) in CM_VARIANTS:
        return FontInfo(*CM_VARIANTS[m.group(1)], design_size=float(m.group(2)))
    m = re.fullmatch(r"SF([A-Z]{2})(\d{4})", key)
    if m and m.group(1) in EC_VARIANTS:
        return FontInfo(*EC_VARIANTS[m.group(1)], design_size=int(m.group(2)) / 100)

    # Latin Modern (LMSans10-Bold, LMRomanCaps10-Regular, LMMono10-Italic, ...) and
    # anything else: read the style from keywords in the name.
    size = re.search(r"(\d+(?:\.\d+)?)", base)
    lower = base.lower()
    if any(k in lower for k in ("mono", "courier", "code", "consol", "typewriter")) or key.startswith("LMTT"):
        family = "mono"
    elif any(k in lower for k in ("sans", "helvet", "arial", "fira", "roboto", "lato", "source sans")):
        family = "sans"
    else:
        family = "serif"
    return FontInfo(
        family,
        bold=any(k in lower for k in ("bold", "black", "heavy", "demi", "semibold", "dark")),
        italic=any(k in lower for k in ("italic", "oblique", "slant")),
        smallcaps="caps" in lower,
        design_size=float(size.group(1)) if size and base.startswith("LM") else None,
    )
