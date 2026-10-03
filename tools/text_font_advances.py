"""Advance widths, kerns and interword spaces (em) of the TeX text fonts that are not Computer
Modern, for emit's size factors (calibration/text_advances.json).

Computer Modern runs are sized by factors measured on Google's renderer (calibration/fonts.json)
and shaped by its own advances (tools/cm_advances.py). Any other TeX text face used to take CM's
factor: Linux Libertine set in PT Serif came out 7-9% wider than the PDF, Bera Sans and DejaVu
Sans in Lato 13-17% narrower. Their factor is predicted instead (`FontMapper.size_of`): the
calibration sentences in the face's own advances, against the substitute's advances on Slides'
renderer (advances.json) - the prediction that is within 0.5% of Google's renderer for CM.

Everything is read from the TFM files TeX set the text with (the T1-encoded ones): advances,
kerning pairs from the lig/kern program, and the interword and extra space (fontdimens 2 and 7).
One family per entry, one face per style (regular, bold, italic, bold_italic), each a
CM_ADVANCES-shaped record. Charter and Utopia's psnfss metrics are not in every TeX tree: a
family whose TFM files are missing is left out with a message.

Usage: python tools/text_font_advances.py [--miktex DIR]
       (writes src/beamer2slides/calibration/text_advances.json)
"""

import argparse
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "src" / "beamer2slides" / "calibration" / "text_advances.json"

# family -> style -> the T1 TFM file (without .tfm). The family keys are `fonts.metrics_family`'s.
FAMILIES: dict[str, dict[str, str]] = {
    # the libertine package's Type 1 faces (LinLibertineT, TB, TI, TBI; Biolinum's italic is TI,
    # its bold italic TBO)
    "libertine": {"regular": "LinLibertineT-tlf-t1", "bold": "LinLibertineTB-tlf-t1",
                  "italic": "LinLibertineTI-tlf-t1", "bold_italic": "LinLibertineTBI-tlf-t1"},
    "biolinum": {"regular": "LinBiolinumT-tlf-t1", "bold": "LinBiolinumTB-tlf-t1",
                 "italic": "LinBiolinumTI-tlf-t1", "bold_italic": "LinBiolinumTBO-tlf-t1"},
    # Bera (Bitstream Vera): the bera package's fvs (sans) and fve (serif)
    "bera_sans": {"regular": "fvsr8t", "bold": "fvsb8t", "italic": "fvsro8t", "bold_italic": "fvsbo8t"},
    "bera_serif": {"regular": "fver8t", "bold": "fveb8t"},
    # DejaVu (the dejavu package; also the text of charts made by matplotlib or criterion)
    "dejavu_sans": {"regular": "DejaVuSans-tlf-t1", "bold": "DejaVuSans-Bold-tlf-t1",
                    "italic": "DejaVuSans-Oblique-tlf-t1", "bold_italic": "DejaVuSans-BoldOblique-tlf-t1"},
    "dejavu_sans_condensed": {"regular": "DejaVuSansCondensed-tlf-t1", "bold": "DejaVuSansCondensed-Bold-tlf-t1",
                              "italic": "DejaVuSansCondensed-Oblique-tlf-t1",
                              "bold_italic": "DejaVuSansCondensed-BoldOblique-tlf-t1"},
    "dejavu_serif": {"regular": "DejaVuSerif-tlf-t1", "bold": "DejaVuSerif-Bold-tlf-t1",
                     "italic": "DejaVuSerif-Italic-tlf-t1", "bold_italic": "DejaVuSerif-BoldItalic-tlf-t1"},
    "dejavu_serif_condensed": {"regular": "DejaVuSerifCondensed-tlf-t1", "bold": "DejaVuSerifCondensed-Bold-tlf-t1",
                               "italic": "DejaVuSerifCondensed-Italic-tlf-t1",
                               "bold_italic": "DejaVuSerifCondensed-BoldItalic-tlf-t1"},
    # Palatino: TeX Gyre Pagella (tgpagella, newpxtext's TeXGyrePagellaX), URW Palladio, Adobe's
    "palatino": {"regular": "ec-qplr", "bold": "ec-qplb", "italic": "ec-qplri", "bold_italic": "ec-qplbi"},
    # Utopia (fourier's T1 metrics)
    "utopia": {"regular": "futr8t", "bold": "futb8t", "italic": "futri8t", "bold_italic": "futbi8t"},
    # Charter (psnfss's bch, XCharter)
    "charter": {"regular": "bchr8t", "bold": "bchb8t", "italic": "bchri8t", "bold_italic": "bchbi8t"},
    # Inconsolata (the inconsolata package's zi4: monospaced, 0.5 em)
    "inconsolata": {"regular": "t1-zi4r-0", "bold": "t1-zi4b-0"},
}

# T1 (Cork) codes -> the characters a PDF's text layer gives for them. ASCII is itself except
# the quotes; accented letters are left out (a width table falls back to the base letter).
T1: dict[int, str] = {c: chr(c) for c in range(0x21, 0x7F)}
T1.update({0x27: "’", 0x60: "‘", 0x0D: "‚", 0x0E: "‹", 0x0F: "›", 0x10: "“", 0x11: "”", 0x12: "„",
           0x13: "«", 0x14: "»", 0x15: "–", 0x16: "—", 0x19: "ı", 0x1B: "ﬀ", 0x1C: "ﬁ", 0x1D: "ﬂ",
           0x1E: "ﬃ", 0x1F: "ﬄ", 0xC6: "Æ", 0xD7: "Œ", 0xD8: "Ø", 0xE6: "æ", 0xF7: "œ", 0xF8: "ø",
           0xFF: "ß"})


@dataclass(frozen=True, kw_only=True)
class Tfm:
    """What emit needs of a TFM file (em): advances and kerning pairs by character, interword
    space and the extra space after a sentence."""
    advances: dict[str, float]
    kerns: dict[str, float]
    space: float
    extra_space: float


def fix_word(data: bytes, at: int) -> float:
    return struct.unpack(">i", data[at:at + 4])[0] / 2 ** 20


def read_tfm(path: Path) -> Tfm:
    """A TFM file's widths, kerns (from the lig/kern program) and fontdimens 2 and 7."""
    data = path.read_bytes()
    lf, lh, bc, ec, nw, nh, nd, ni, nl, nk, ne, np_ = struct.unpack(">12H", data[:24])
    char_info = 24 + 4 * lh
    widths = char_info + 4 * (ec - bc + 1)
    lig_kern = widths + 4 * (nw + nh + nd + ni)
    kern = lig_kern + 4 * nl
    param = kern + 4 * nk + 4 * ne
    if param + 4 * np_ != 4 * lf:
        raise SystemExit(f"{path}: not a TFM file")
    advances: dict[str, float] = {}
    kerns: dict[str, float] = {}
    for code in range(bc, ec + 1):
        info = data[char_info + 4 * (code - bc):char_info + 4 * (code - bc) + 4]
        if info[0] == 0:
            continue  # no such character
        ch = T1.get(code)
        if ch is not None:
            advances[ch] = round(fix_word(data, widths + 4 * info[0]), 4)
        if ch is None or info[2] & 3 != 1:  # tag 1: a lig/kern program
            continue
        i = info[3]
        first = data[lig_kern + 4 * i:lig_kern + 4 * i + 4]
        if first[0] > 128:  # the program starts elsewhere
            i = 256 * first[2] + first[3]
        seen: set[int] = set()  # (the first instruction for a pair is the one TeX follows)
        while True:
            skip, nxt, op, rem = data[lig_kern + 4 * i:lig_kern + 4 * i + 4]
            other = T1.get(nxt)
            if skip <= 128 and nxt not in seen:
                seen.add(nxt)
                value = round(fix_word(data, kern + 4 * (256 * (op - 128) + rem)), 4) if op >= 128 else 0.0
                if value and other is not None:
                    kerns[ch + other] = value
            if skip >= 128:
                break
            i += skip + 1
    space, extra = fix_word(data, param + 4), fix_word(data, param + 24) if np_ >= 7 else 0.0
    return Tfm(advances=advances, kerns=kerns, space=round(space, 4), extra_space=round(extra, 4))


def miktex_root(given: Path | None) -> Path:
    if given is not None:
        return given
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        for sub in ("Programs/MiKTeX", "MiKTeX"):
            p = Path(base) / sub
            if (p / "fonts" / "tfm").is_dir():
                return p
    raise SystemExit("MiKTeX not found: pass --miktex")


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--miktex", type=Path, default=None)
    tfm_dir = miktex_root(ap.parse_args().miktex) / "fonts" / "tfm"
    found = {p.stem: p for p in tfm_dir.rglob("*.tfm")}
    families: dict[str, dict[str, Tfm]] = {}
    for family, styles in FAMILIES.items():
        missing = [name for name in styles.values() if name not in found]
        if missing:
            print(family, "left out: no", ", ".join(missing))
            continue
        families[family] = {style: read_tfm(found[name]) for style, name in styles.items()}
        for style, t in families[family].items():
            print(family, style, "space", t.space, "extra", t.extra_space, len(t.advances), "advances",
                  len(t.kerns), "kerns")
    body = ",\n".join(
        f" {json.dumps(family)}: {{\n" + ",\n".join(
            f"  {json.dumps(style)}: " + json.dumps({"space": t.space, "extra_space": t.extra_space,
                                                     "advances": t.advances, "kerns": t.kerns}, ensure_ascii=False)
            for style, t in faces.items()) + "\n }"
        for family, faces in families.items())
    TABLE.write_text('{"source": "tools/text_font_advances.py: T1 TFM advances, kerns and spaces (em) of TeX text '
                     f'fonts other than Computer Modern",\n"fonts": {{\n{body}\n}}}}\n', encoding="utf-8")
    json.loads(TABLE.read_text(encoding="utf-8"))
    print("wrote", TABLE)


if __name__ == "__main__":
    main()
