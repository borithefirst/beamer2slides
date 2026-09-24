"""TeX's bitmap text fonts, for reading Type 3 text (`beamer2slides.type3`).

pdflatex without cm-super (T1, T2A text) or without a Type 1 companion font (TS1 symbols) embeds
the Metafont bitmaps as Type 3 fonts: no /BaseFont, no /ToUnicode, glyph names /a<code>. PDFium
gives the character codes back as if they were Latin-1 (the fi ligature is U+001C, T1's ś the
plus-minus sign) and every font is called "Type3". What still tells the fonts apart is their
advances, which are the TFM widths: this table holds them for every EC (T1), TC (TS1) and LH
(T2A) font of the TeX installation, with the three encodings' code -> Unicode maps taken from
LaTeX's own `<enc>enc.def` / `<enc>enc.dfu` files.

Usage: python tools/tex_fonts.py [--miktex DIR] [--user DIR]
       (writes src/beamer2slides/calibration/tex_fonts.json)
"""

import argparse
import base64
import json
import os
import re
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "src" / "beamer2slides" / "calibration" / "tex_fonts.json"
# encoding -> (font directory under fonts/tfm, TFM name prefix, .def file, .dfu file)
ENCODINGS = {
    "T1": ("jknappen/ec", "ec", "base/t1enc.def", "base/t1enc.dfu"),
    "TS1": ("jknappen/ec", "tc", "base/ts1enc.def", "base/ts1enc.dfu"),
    "T2A": ("lh/lh-t2a", "la", "cyrillic/t2aenc.def", "base/t2aenc.dfu"),
}
WIDTH_UNIT = 8192  # widths are stored as uint16 in 1/8192 em (0: no glyph)
# Spacing accents for T1/T2A codes 0-12 and TS1's capital accents: classify.compose_accents
# builds them with the letter under them.
ACCENT_TEXT = {"\\`": "`", "\\'": "´", "\\^": "ˆ", "\\~": "˜", '\\"': "¨", "\\H": "˝", "\\r": "˚",
               "\\v": "ˇ", "\\u": "˘", "\\=": "¯", "\\.": "˙", "\\c": "¸", "\\k": "˛"}
# What the .def/.dfu files leave out or say ambiguously, per encoding: glyphs that are font
# ligatures (no LaTeX command), a mark with no text of its own, the dash a Unicode range shares.
OVERRIDES = {
    "T1": {0x17: "", 0x18: "", 0x1B: "ﬀ", 0x1C: "ﬁ", 0x1D: "ﬂ", 0x1E: "ﬃ", 0x1F: "ﬄ",
           0x15: "–", 0x16: "—", 0x27: "’", 0x60: "‘", 0x7F: "-", 0x0B: "¸", 0x0C: "˛",
           0xDF: "SS"},  # \SS, the capital of ß, is a glyph pair
    "T2A": {0x17: "", 0x18: "", 0x1B: "ﬀ", 0x1C: "ﬁ", 0x1D: "ﬂ", 0x1E: "ﬃ", 0x1F: "ﬄ",
            0x15: "–", 0x16: "—", 0x27: "’", 0x60: "‘", 0x7F: "-", 0x0B: "¸", 0x0C: "˛"},
    "TS1": {0x0B: "¸", 0x0C: "˛", 0x0D: "‚", 0x12: "„", 0x15: "–", 0x16: "—", 0x17: "", 0x18: "←",
            0x19: "→", 0x1F: "", 0x24: "$", 0x27: "'", 0x2A: "*", 0x2D: "=", 0x3D: "−", 0x5B: "⟦",
            0x5D: "⟧", 0x60: "`", 0x62: "*", 0x63: "⚮", 0x64: "†", 0x6C: "❧", 0x6D: "⚭", 0x7E: "~",
            0x7F: "=", 0x83: "˵", 0x8A: "$", 0x8B: "¢", 0x95: "⸘", 0xA0: "⁅", 0xA1: "⁆", 0xAB: "🄯",
            0xB5: "µ", 0xBB: "√", **{c: chr(c) for c in range(0x30, 0x3A)}},  # oldstyle digits
}
# Families kept (the second two letters of ecrm1095): TeX's everyday text faces and their slanted,
# bold and small-caps companions. Left out: Dunhill (dh), classical and unslanted italic (ci, ui),
# roman bold without extension (rb), variable-width typewriter (vt, vi).
SHAPES = {"rm", "sl", "ti", "bx", "bl", "bi", "cc", "sc", "xc", "oc", "ss", "si", "sx", "so",
          "tt", "st", "it", "tc"}


def miktex_root() -> Path:
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        for sub in ("Programs/MiKTeX", "MiKTeX"):
            p = Path(base) / sub
            if (p / "fonts" / "tfm").is_dir() and (p / "tex" / "latex").is_dir():
                return p
    raise SystemExit("MiKTeX not found: pass --miktex")


def tfm(path: Path) -> tuple[float, list[float]]:
    """Design size (pt) and the 256 widths (em, 0 for a code without a glyph)."""
    d = path.read_bytes()
    lf, lh, bc, ec, nw = struct.unpack(">5H", d[:10])
    design = struct.unpack(">i", d[28:32])[0] / 2 ** 20
    info = 24 + 4 * lh
    base = info + 4 * (ec - bc + 1)
    widths = [struct.unpack(">i", d[base + 4 * i:base + 4 * i + 4])[0] / 2 ** 20 for i in range(nw)]
    out = [0.0] * 256
    for c in range(bc, ec + 1):
        if d[info + 4 * (c - bc)]:
            out[c] = widths[d[info + 4 * (c - bc)]]
    return design, out


def _number(s: str) -> int:
    s = s.strip()
    if s.startswith('"'):
        return int(s[1:], 16)
    if s.startswith("'"):
        return int(s[1:], 8)
    if s.startswith("`"):  # `\X: the character X (`\} is cut short by the pattern's braces)
        rest = s[1:].removeprefix("\\")
        return ord(rest[0]) if rest else ord("}")
    return int(s)


def encoding_map(latex: Path, enc: str, def_file: str, dfu_file: str) -> dict[int, str]:
    """Code -> Unicode text of one font encoding, from LaTeX's declarations: a code's command
    (\\DeclareTextSymbol, a \\DeclareTextComposite of accent and letter), and the Unicode
    character that command stands for in the .dfu file."""
    text = (latex / def_file).read_text(encoding="latin-1")
    symbols: dict[str, int] = {}
    composites: dict[tuple[str, str], int] = {}
    for m in re.finditer(r"\\DeclareTextSymbol\{(\\[^}]+)\}\{[^}]*\}\{([^}]+)\}", text):
        symbols.setdefault(m.group(1), _number(m.group(2)))
    for m in re.finditer(r"\\DeclareTextAccent\{(\\[^}]+)\}\{[^}]*\}\{([^}]+)\}", text):
        symbols.setdefault("accent:" + m.group(1), _number(m.group(2)))
    for m in re.finditer(r"\\DeclareTextComposite\{(\\[^}]+)\}\{[^}]*\}\{([^}]*)\}\{([^}]+)\}", text):
        composites.setdefault((m.group(1), m.group(2).strip()), _number(m.group(3)))
    out: dict[int, str] = {}
    if enc != "TS1":  # the printable ASCII range is itself, but for what the .def files redefine
        out.update({c: chr(c) for c in range(0x21, 0x7F)})
    for acc, code in ((k[7:], v) for k, v in symbols.items() if k.startswith("accent:")):
        if acc in ACCENT_TEXT:
            out[code] = ACCENT_TEXT[acc]
    body_re = re.compile(r"(?:\\@tabacckludge)?(\\[^A-Za-z@]|\\[A-Za-z]+)\s*(\{?)(\\[A-Za-z]+|[A-Za-z])?\}?\s*$")
    seen: set[int] = set()
    for m in re.finditer(r"\\DeclareUnicodeCharacter\{([0-9A-F]+)\}\{(.*)\}\s*$",
                         (latex / dfu_file).read_text(encoding="latin-1"), re.M):
        char, body = chr(int(m.group(1), 16)), m.group(2).strip()
        code = None
        if body.startswith("\\@tabacckludge"):
            body = "\\" + body[len("\\@tabacckludge"):]
        if body in symbols:
            code = symbols[body]
        else:
            b = body_re.match(body)
            if b and b.group(3):
                code = composites.get((b.group(1), b.group(3)))
        if code is not None and code not in seen and (enc == "TS1" or not 0x21 <= code < 0x7F):
            # (T2A's Cyrillic І and Ј are the Latin I and J glyphs: those codes stay ASCII)
            seen.add(code)
            out[code] = char
    out.update(OVERRIDES.get(enc, {}))
    return dict(sorted(out.items()))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--miktex", type=Path, default=None, help="the installation (fonts/tfm, tex/latex)")
    ap.add_argument("--user", type=Path, default=Path(os.environ.get("LOCALAPPDATA", "")) / "MiKTeX",
                    help="where fonts made on demand live (LH fonts are generated, not shipped)")
    args = ap.parse_args()
    root = args.miktex or miktex_root()
    fonts: dict[str, dict] = {}
    tables: list[bytes] = []
    index: dict[bytes, int] = {}
    encodings = {}
    for enc, (sub, prefix, def_file, dfu_file) in ENCODINGS.items():
        encodings[enc] = {str(k): v for k, v in encoding_map(root / "tex" / "latex", enc, def_file, dfu_file).items()}
        paths = {}
        for base in (root, args.user):
            d = base / "fonts" / "tfm" / sub
            if d.is_dir():
                paths.update({p.stem: p for p in d.glob(f"{prefix}*.tfm")
                              if re.fullmatch(prefix + r"[a-z]{2}\d{4}", p.stem) and p.stem[2:4] in SHAPES})
        for name in sorted(paths):
            design, widths = tfm(paths[name])
            blob = struct.pack("<256H", *(min(65535, round(w * WIDTH_UNIT)) for w in widths))
            if blob not in index:  # slanted faces have their upright twin's widths
                index[blob] = len(tables)
                tables.append(blob)
            fonts[name] = {"enc": enc, "size": round(design, 3), "table": index[blob]}
        print(enc, len(paths), "fonts", len(encodings[enc]), "codes")
    packed = base64.b64encode(zlib.compress(b"".join(tables), 9)).decode("ascii")
    TABLE.write_text(json.dumps({
        "source": "tools/tex_fonts.py: TFM advances (uint16 little endian, 1/%d em, 256 per table, "
                  "zlib, base64) of the EC/TC/LH bitmap fonts; encodings from LaTeX's .def/.dfu" % WIDTH_UNIT,
        "unit": WIDTH_UNIT, "encodings": encodings, "fonts": fonts, "tables": packed,
    }, ensure_ascii=False, indent=0).replace("\n", "") + "\n", encoding="utf-8")
    print("wrote", TABLE, len(tables), "tables", TABLE.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
