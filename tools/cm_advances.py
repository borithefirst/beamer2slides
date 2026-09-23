"""Computer Modern's advance widths, kerns and interword spaces (em), for emit's width prediction.

emit's size factors make an average *sentence* as wide in Slides as in the PDF; a text whose
letters are unlike a sentence's (capitals, a line of W and M) still comes out wider or narrower.
`FontMapper.shape_ratio` predicts that from the substitute's advances on Slides' renderer
(advances.json, tools/probe_advances.py) against the ones TeX set the text with - these, read
from the Type 1 fonts' AFM files (advances, kerning pairs) and the TFM files (the interword
space and the extra space after a sentence, fontdimens 2 and 7). EC (T1) and Latin Modern share
Computer Modern's metrics for the characters kept here.

Usage: python tools/cm_advances.py [--miktex DIR]   (writes src/beamer2slides/calibration/cm_advances.json)
"""

import argparse
import json
import os
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "src" / "beamer2slides" / "calibration" / "cm_advances.json"
FONTS = ["cmss10", "cmssbx10", "cmssi10", "cmr10", "cmbx10", "cmti10", "cmbxti10"]  # slanted: upright widths
# OT1 glyph names -> the Unicode characters a PDF's text layer gives for them.
NAMES = {
    "exclam": "!", "quotedblright": "”", "numbersign": "#", "dollar": "$", "sterling": "£",
    "percent": "%", "ampersand": "&", "quoteright": "’", "parenleft": "(", "parenright": ")",
    "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-", "period": ".", "slash": "/",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "colon": ":", "semicolon": ";", "exclamdown": "¡", "equal": "=",
    "questiondown": "¿", "question": "?", "at": "@", "bracketleft": "[", "quotedblleft": "“",
    "bracketright": "]", "quoteleft": "‘", "endash": "–", "emdash": "—",
    "germandbls": "ß", "ae": "æ", "oe": "œ", "oslash": "ø", "AE": "Æ",
    "OE": "Œ", "Oslash": "Ø", "dotlessi": "ı",
    "ff": "ﬀ", "fi": "ﬁ", "fl": "ﬂ", "ffi": "ﬃ", "ffl": "ﬄ",
}
NAMES.update({c: c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"})


def miktex_root() -> Path:
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        for sub in ("Programs/MiKTeX", "MiKTeX"):
            p = Path(base) / sub
            if (p / "fonts" / "afm").is_dir():
                return p
    raise SystemExit("MiKTeX not found: pass --miktex")


def afm(path: Path) -> tuple[dict[str, float], dict[str, float]]:
    widths, kerns = {}, {}
    for line in path.read_text(encoding="latin-1").splitlines():
        if line.startswith("C "):
            fields = dict(f.strip().split(" ", 1) for f in line.split(";") if f.strip())
            name = fields.get("N")
            if name in NAMES:
                widths[NAMES[name]] = round(float(fields["WX"]) / 1000, 4)
        elif line.startswith("KPX "):
            _, a, b, k = line.split()
            if a in NAMES and b in NAMES:
                kerns[NAMES[a] + NAMES[b]] = round(float(k) / 1000, 4)
    return widths, kerns


def tfm_params(path: Path) -> list[float]:
    """The font's fontdimens (em): slant, space, stretch, shrink, x-height, quad, extra space."""
    data = path.read_bytes()
    lf, lh, bc, ec, nw, nh, nd, ni, nl, nk, ne, np_ = struct.unpack(">12H", data[:24])
    start = 4 * (lf - np_)
    return [struct.unpack(">i", data[start + 4 * i:start + 4 * i + 4])[0] / 2 ** 20 for i in range(np_)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--miktex", type=Path, default=None)
    root = ap.parse_args().miktex or miktex_root()
    fonts = {}
    for name in FONTS:
        widths, kerns = afm(next((root / "fonts" / "afm").rglob(f"{name}.afm")))
        params = tfm_params(next((root / "fonts" / "tfm").rglob(f"{name}.tfm")))
        fonts[name] = {"space": round(params[1], 4), "extra_space": round(params[6], 4),
                       "advances": widths, "kerns": kerns}
        print(name, "space", fonts[name]["space"], "extra", fonts[name]["extra_space"],
              len(widths), "advances", len(kerns), "kerns")
    body = ",\n".join(f" {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}" for k, v in fonts.items())
    TABLE.write_text('{"source": "tools/cm_advances.py: Computer Modern AFM advances and kerns, TFM spaces (em)",\n'
                     f'"fonts": {{\n{body}\n}}}}\n', encoding="utf-8")
    json.loads(TABLE.read_text(encoding="utf-8"))
    print("wrote", TABLE)


if __name__ == "__main__":
    main()
