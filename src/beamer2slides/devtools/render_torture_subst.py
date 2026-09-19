"""Random torture PDFs for text in fonts the PDF does not embed (`pdf/pure/render_text.py` over
`pdf/pure/fontmapper.py`'s substitutes): each page is rendered by PDFium and by the pure reader and
any pixel that differs is a failure, shrunk to the lines that still make it differ. The twin of
`render_torture_text.py`, whose page generator it reuses.

    python tools/render_torture_subst.py [seed0] [n] [--simple 0|1|2] [--out DIR]

Every font is made up on the spot: a simple font dictionary (/Type1, /TrueType or /MMType1) with a
random /BaseFont - names nothing on the machine carries (PDFium's generic FoxitSansMM / FoxitSerifMM
then, blended at the requested weight and skewed by the italic angle), Symbol / ZapfDingbats and
their variants (Foxit's CFF faces), and base-14 or installed names (GDI TrueType faces, which the
pure renderer refuses until TrueType drawing lands) - random /Flags (fixed, serif, symbolic, italic,
force bold), a /FontDescriptor with random /FontWeight, /StemV and /ItalicAngle, /Widths that agree
with nothing (or none), and /Encoding by name or /Differences. PDFium's Foxit faces must be in the
user cache (`python -m beamer2slides.pdf.pure.foxit`), or every page is refused.

Failures are written to DIR as seedN.pdf (the shrunk page) and seedN.png (PDFium | pure |
difference). A refused page (PdfError) is counted, never a failure."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import numpy as np

from .render_torture_text import FontSpec, pdf_bytes, random_text

UNKNOWN = ["Foo", "Qwerty-Bold", "Blorp,Italic", "Zed-BoldItalic", "Nimbus-Oblique", "Frobnitz-Light",
           "Wibble-Black", "AAAA+Gloop", "Serifish-Roman", "Sansy,BoldItalic", "Monoid-Regular", "X"]
# Symbol and ZapfDingbats (and "SymbolMT") get Foxit's CFF faces; a styled ZapfDingbats with the
# symbolic flag too, under a name that is not a standard one, which is where PDFium's glyph spacing
# heuristic (GetCharPosList) moves or narrows glyphs whose /Widths disagree with the face.
SYMBOLIC = ["Symbol", "Symbol,Bold", "Symbol,Italic", "SymbolMT", "ZapfDingbats", "Dingbats",
            "ZapfDingbatsITC", "ZapfDingbats,Bold", "ZapfDingbats,Italic", "ZapfDingbats,BoldItalic"]
INSTALLED = ["Helvetica", "Times-Roman", "Courier-Bold", "Arial", "TimesNewRoman,Bold", "Verdana"]
WEIGHTS = [100, 200, 300, 400, 500, 600, 700, 800, 900]
ANGLES = [0, 0, -8, -12, -15.5, -20, -45, 10, 30]
NAMES = ["A", "B", "a", "b", "e", "g", "x", "zero", "one", "alpha", "bullet", "space", "Omega",
         "quotedbl", "ampersand", "Q", "W", "m", "f", "fi", "adieresis", "a20", "a71", "uni0041"]


def random_font(r: random.Random, pool: str = "any") -> FontSpec:
    k = r.random()
    if pool == "unknown" or (pool == "any" and k < 0.6):
        base, family = r.choice(UNKNOWN), "unknown"
    elif pool == "symbol" or (pool == "any" and k < 0.9):
        base, family = r.choice(SYMBOLIC), "symbol"
    else:
        base, family = r.choice(INSTALLED), "installed"
    subtype = r.choice(["Type1", "Type1", "TrueType", "MMType1"])
    flags = 0
    for bit, p in ((1, 0.2), (2, 0.4), (4, 0.3), (32, 0.6), (64, 0.3), (1 << 18, 0.15)):
        if r.random() < p:
            flags |= bit
    if family == "symbol" and r.random() < 0.6:
        flags |= 4
    desc = [b"/Type /FontDescriptor", b"/FontName /%s" % base.encode(), b"/Flags %d" % flags,
            b"/FontBBox [%d %d %d %d]" % (r.randint(-200, 0), r.randint(-300, 0), r.randint(500, 1200), r.randint(600, 1100))]
    if r.random() < 0.8:
        desc.append(b"/ItalicAngle %g" % r.choice(ANGLES))
    if r.random() < 0.7:
        desc.append(b"/Ascent %d /Descent %d /CapHeight %d" % (r.randint(600, 900), -r.randint(100, 300), r.randint(500, 750)))
    if r.random() < 0.6:
        desc.append(b"/StemV %d" % r.choice([50, 80, 120, 160, 200]))
    if r.random() < 0.6:
        desc.append(b"/FontWeight %d" % r.choice(WEIGHTS))
    first, last = r.choice([(32, 126), (0, 255), (65, 90)])
    font = [b"/Type /Font", b"/Subtype /%s" % subtype.encode(), b"/BaseFont /%s" % base.encode()]
    w = r.random()
    if w < 0.8:
        if w < 0.3:
            widths = [r.randint(0, 1200) for _ in range(first, last + 1)]
        elif w < 0.6:
            widths = [r.choice([250, 500, 600, 722]) for _ in range(first, last + 1)]
        else:
            fixed = r.choice([0, 300, 600, 1000])
            widths = [fixed] * (last - first + 1)
        font.append(b"/FirstChar %d /LastChar %d /Widths [%s]" % (first, last, b" ".join(b"%d" % v for v in widths)))
    e = r.random()
    if e < 0.3:
        font.append(b"/Encoding /%s" % r.choice([b"WinAnsiEncoding", b"MacRomanEncoding", b"StandardEncoding"]))
    elif e < 0.55:
        diffs = []
        for _ in range(r.randint(1, 6)):
            diffs.append(b"%d" % r.randint(32, 126))
            diffs += [b"/" + r.choice(NAMES).encode() for _ in range(r.randint(1, 3))]
        base_enc = b"/BaseEncoding /WinAnsiEncoding " if r.random() < 0.5 else b""
        font.append(b"/Encoding << /Type /Encoding " + base_enc + b"/Differences [" + b" ".join(diffs) + b"] >>")
    objects = [b"<< " + b" ".join(font), b"<< " + b" ".join(desc) + b" >>"]
    if r.random() < 0.85:
        objects[0] += b" /FontDescriptor @1@ >>"
    else:
        objects[0] += b" >>"
        objects = objects[:1]
    lo, hi = max(first, 32), min(last, 126) if r.random() < 0.8 else 255
    codes = list(range(lo, hi + 1)) or list(range(32, 127))
    return FontSpec(f"{base}/{subtype}/{flags}", family, objects, codes)


def case(seed: int, simple: int = 2, pool: str = "any"):
    """(content, fonts, zoom, transparent) for seed `seed`."""
    r = random.Random(seed)
    fonts = [random_font(r, pool) for _ in range(r.randint(1, 3))]
    groups = [random_text(r, fonts, simple) for _ in range(r.randint(1, 1 if simple == 0 else 4))]
    zoom = 1 if simple == 0 else r.choice([0.5, 1, 1.37, 2, 3.1])
    transparent = False if simple == 0 else r.random() < 0.3
    return b"\n".join(groups), fonts, zoom, transparent


_RESET_FONTS = [FontSpec(f"reset/{flags}", "unknown", [
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Reset%d /FirstChar 65 /LastChar 65 /Widths [640] "
    b"/FontDescriptor @1@ >>" % flags,
    b"<< /Type /FontDescriptor /FontName /Reset%d /Flags %d /FontBBox [0 0 1000 1000] >>" % (flags, flags)], [65])
    for flags in (32, 34)]
_RESET = pdf_bytes(b"BT /F0 20 Tf 60 60 Td (A) Tj ET BT /F1 20 Tf 120 60 Td (A) Tj ET", _RESET_FONTS)


def resync() -> None:
    """Put both backends' generic faces (FoxitSansMM, FoxitSerifMM) into one known blend. A face's
    blend is process-wide state that every glyph drawn moves and every width measured without /Widths
    reads (`ftoutline.Face.adjust_variation`), so a page the pure reader refuses - which PDFium draws -
    leaves the two apart. Drawing one glyph of each face with a /Widths width sets both axes."""
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    for backend in (PdfiumBackend, PureBackend):
        doc = backend().open(_RESET)
        try:
            doc[0].render(1)
        finally:
            doc.close()


def compare(content: bytes, fonts, zoom: float, transparent: bool):
    """(pixels that differ, PDFium's render, pure's render, per-pixel max difference)."""
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    resync()
    data = pdf_bytes(content, fonts)
    da, db = PdfiumBackend().open(data), PureBackend().open(data)
    try:
        a = da[0].render(zoom, transparent=transparent)
        b = db[0].render(zoom, transparent=transparent)
    finally:
        da.close()
        db.close()
    d = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return int((d > 0).sum()), a, b, d


def shrink(content: bytes, fonts, zoom: float, transparent: bool) -> bytes:
    """Drop lines while the difference remains."""
    def fails(c):
        try:
            return compare(c, fonts, zoom, transparent)[0] > 0
        except Exception:  # noqa: BLE001
            return False

    lines = content.split(b"\n")
    changed = True
    while changed:
        changed = False
        for i in range(len(lines)):
            if lines[i] in (b"q", b"Q", b"BT", b"ET"):
                continue
            trial = lines[:i] + lines[i + 1:]
            if fails(b"\n".join(trial)):
                lines, changed = trial, True
                break
    return b"\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seed0", type=int, nargs="?", default=0)
    ap.add_argument("n", type=int, nargs="?", default=200)
    ap.add_argument("--simple", type=int, default=2)
    ap.add_argument("--pool", default="any", choices=["any", "unknown", "symbol"])
    ap.add_argument("--out", default="out/render-torture-subst")
    ap.add_argument("--no-shrink", action="store_true")
    args = ap.parse_args(argv)
    from ..pdf.api import PdfError
    out = Path(args.out)
    fails = refused = drawn = 0
    reasons: dict = {}
    for seed in range(args.seed0, args.seed0 + args.n):
        content, fonts, zoom, transparent = case(seed, args.simple, args.pool)
        try:
            npx = compare(content, fonts, zoom, transparent)[0]
        except PdfError as e:
            refused += 1                     # the pure reader refuses the page: never drawn wrong
            reasons[str(e)] = reasons.get(str(e), 0) + 1
            continue
        except Exception as e:  # noqa: BLE001
            print("seed", seed, "EXC", type(e).__name__, e)
            fails += 1
            continue
        drawn += 1
        if not npx:
            continue
        fails += 1
        small = content if args.no_shrink else shrink(content, fonts, zoom, transparent)
        npx, a, b, d = compare(small, fonts, zoom, transparent)
        print(f"seed {seed} zoom {zoom} transparent {transparent} fonts {[f.name for f in fonts]}: "
              f"{npx} px, max {d.max()}")
        print(small.decode())
        out.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        vis = np.concatenate([a[..., :3], b[..., :3], np.stack([np.where(d > 0, 255, 0)] * 3, -1)], 1)
        Image.fromarray(vis.astype(np.uint8)).save(out / f"seed{seed}.png")
        (out / f"seed{seed}.pdf").write_bytes(pdf_bytes(small, fonts))
    for why, k in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  refused {k}: {why}")
    print(f"seeds {args.seed0}..{args.seed0 + args.n - 1}: {drawn} drawn, {fails} failed, {refused} refused")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
