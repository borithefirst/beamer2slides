"""Random torture PDFs for the pure renderer's text (`pdf/pure/render_text.py`): each page is
rendered by PDFium and by the pure reader, and any pixel that differs is a failure, shrunk to the
lines that still make it differ. The text twin of `render_torture.py`.

    python tools/render_torture_text.py [seed0] [n] [--kind type1|cff|truetype|type3|any] [--out DIR]

The fonts are harvested at run time from the test decks' PDFs (`tests/decks/out/*.pdf`, built by
`tests/decks/build.py`): each font dictionary is copied with everything it refers to, so the
torture page embeds exactly the program pdflatex / xelatex / lualatex wrote (subset fonts; only the
codes whose glyphs the subset kept are shown). Nothing is committed: no font binaries in the tree.

A page is a few `BT ... ET` groups: a random `cm`, clip paths, colours, constant alpha, then text
with random Tf size (tiny to huge), Tm (upright, scaled, skewed, turned, mirrored), Tz, Tc, Tw, Ts,
TL / T* / ' / \", Tr 0..7, line width, TJ kerning. Failures are written to DIR as seedN.pdf (the
shrunk page) and seedN.png (PDFium | pure | difference)."""

from __future__ import annotations

import argparse
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
DECKS = Path(os.environ.get("B2S_TEST_DECKS") or ROOT / "tests" / "decks" / "out")

EXTGS = b"/ExtGState << " + b" ".join(
    b"/A%d << /ca %s /CA %s >>" % (k, v, v) for k, v in enumerate([b"0.5", b"0.25", b"0.8", b"0.0", b"1"])) + b" >>"


# ---------------------------------------------------------------------- fonts from the decks


@dataclass
class FontSpec:
    name: str                  # where it came from: deck/fontname
    kind: str                  # type1 | cff | truetype | cid-cff | cid-truetype | type3
    objects: list              # serialized objects; object 0 is the font dict; refs are "@k@" placeholders
    codes: list                # codes worth showing (a glyph in the subset)
    two_byte: bool = False     # CID font with a 2-byte CMap (Identity-H)
    extra: dict = field(default_factory=dict)


def _write_value(v, refmap) -> bytes:
    from ..pdf.pure.document import _write_name, _write_number
    from ..pdf.pure.syntax import Name, Ref, String
    if v is None:
        return b"null"
    if isinstance(v, (bool, int, float)):
        return _write_number(v)
    if isinstance(v, Name):
        return _write_name(v)
    if isinstance(v, Ref):
        return b"@%d@" % refmap(v)
    if isinstance(v, (bytes, String)):
        return b"<" + bytes(v).hex().encode() + b">"
    if isinstance(v, str):
        return _write_name(v)
    if isinstance(v, list):
        return b"[" + b" ".join(_write_value(x, refmap) for x in v) + b"]"
    if isinstance(v, dict):
        return b"<<" + b"".join(_write_name(k) + b" " + _write_value(x, refmap) + b"\n" for k, x in v.items()) + b">>"
    return b"null"


def copy_graph(pdf, root_ref) -> list:
    """The object `root_ref` and everything it refers to, as serialized objects whose references
    are placeholders `@k@` (k = index in the returned list)."""
    from ..pdf.pure.syntax import Stream
    order: dict = {}
    out: list = []

    def refmap(ref):
        if ref.num not in order:
            order[ref.num] = len(out)
            out.append(None)
            obj = pdf.get(ref.num)
            if isinstance(obj, Stream):
                d = dict(obj.dict)
                d["Length"] = len(obj.raw)
                body = _write_value(d, refmap) + b"\nstream\n" + obj.raw + b"\nendstream"
            else:
                body = _write_value(obj, refmap)
            out[order[ref.num]] = body
        return order[ref.num]

    refmap(root_ref)
    return out


def _font_refs(pdf):
    """(ref, font dict) of every font a page's or form's resources name."""
    from ..pdf.pure.syntax import Ref, Stream
    seen, found = set(), []

    def walk_res(res, depth=0):
        res = pdf.resolve(res)
        if not isinstance(res, dict) or depth > 6:
            return
        fonts = pdf.resolve(res.get("Font"))
        if isinstance(fonts, dict):
            for v in fonts.values():
                if isinstance(v, Ref) and v.num not in seen:
                    seen.add(v.num)
                    d = pdf.resolve(v)
                    if isinstance(d, dict):
                        found.append((v, d))
                        if d.get("Subtype") == "Type3":
                            walk_res(d.get("Resources"), depth + 1)
        xo = pdf.resolve(res.get("XObject"))
        if isinstance(xo, dict):
            for v in xo.values():
                if isinstance(v, Ref) and ("x", v.num) not in seen:
                    seen.add(("x", v.num))
                    s = pdf.resolve(v)
                    if isinstance(s, Stream) and s.get("Subtype") == "Form":
                        walk_res(s.get("Resources"), depth + 1)

    for i in range(pdf.page_count):
        found_page = pdf.page_dict(i)
        if found_page is not None:
            walk_res(pdf.page_attr(found_page[1], "Resources"))
    return found


def _kind(pdf, d) -> str | None:
    r = pdf.resolve
    sub = r(d.get("Subtype"))
    if sub == "Type3":
        return "type3"
    if sub == "Type0":
        desc = r(d.get("DescendantFonts"))
        if not isinstance(desc, list) or not desc:
            return None
        cid = r(desc[0])
        fd = r(cid.get("FontDescriptor")) if isinstance(cid, dict) else None
        if not isinstance(fd, dict):
            return None
        if "FontFile2" in fd:
            return "cid-truetype"
        if "FontFile3" in fd:
            return "cid-cff"
        return None
    fd = r(d.get("FontDescriptor"))
    if not isinstance(fd, dict):
        return None
    if "FontFile" in fd:
        return "type1"
    if "FontFile2" in fd:
        return "truetype"
    if "FontFile3" in fd:
        return "cff"
    return None


def _codes(font) -> list:
    """Codes of a loaded pure Font that have a glyph (simple fonts: 0..255; CID: from the widths)."""
    from ..pdf.pure.fonts import NO_GLYPH, CIDFont, Type3Font
    codes = []
    if isinstance(font, Type3Font):
        for code in range(256):
            try:
                if font._load_char(code) is not None:
                    codes.append(code)
            except Exception:  # noqa: BLE001
                pass
        return codes
    if isinstance(font, CIDFont):
        for code in range(0, 2000):
            try:
                g = font._glyph(code)
            except Exception:  # noqa: BLE001
                continue
            if g not in (None, -1, 0, NO_GLYPH) and font.char_width(code) > 0:
                codes.append(code)
        return codes
    glyphs = getattr(font, "glyphs", None) or {}
    for code in range(256):
        g = glyphs.get(code, NO_GLYPH) if isinstance(glyphs, dict) else (glyphs[code] if code < len(glyphs) else NO_GLYPH)
        if g not in (None, -1, NO_GLYPH) and font.char_width(code) > 0:
            codes.append(code)
    return codes


_HARVEST: list | None = None


def harvest(decks: Path = DECKS, limit_per_kind: int = 40) -> list[FontSpec]:
    """Every embedded font of the test decks, deduplicated by base font name and kind."""
    global _HARVEST
    if _HARVEST is not None:
        return _HARVEST
    from ..pdf.pure.document import read
    from ..pdf.pure.fonts import load_font
    specs, seen, per = [], set(), {}
    for path in sorted(decks.glob("*.pdf")):
        if path.stem.endswith("-handout"):
            continue
        try:
            pdf = read(path.read_bytes())
        except Exception:  # noqa: BLE001
            continue
        for ref, d in _font_refs(pdf):
            kind = _kind(pdf, d)
            if kind is None:
                continue
            base = str(pdf.resolve(d.get("BaseFont")) or f"t3-{path.stem}-{ref.num}")
            key = (kind, base.split("+")[-1])
            if key in seen or per.get(kind, 0) >= limit_per_kind:
                continue
            try:
                font = load_font(pdf, d)
                codes = _codes(font) if font is not None else []
            except Exception:  # noqa: BLE001
                continue
            if not codes:
                continue
            seen.add(key)
            per[kind] = per.get(kind, 0) + 1
            two = kind.startswith("cid") and str(pdf.resolve(d.get("Encoding"))) in ("Identity-H", "Identity-V")
            specs.append(FontSpec(f"{path.stem}/{base}", kind, copy_graph(pdf, ref), codes, two))
    _HARVEST = specs
    return specs


# ---------------------------------------------------------------------- PDF


def pdf_bytes(content: bytes, fonts: list[FontSpec], media=(0, 0, 200, 150)) -> bytes:
    """One page drawing `content` with /F0, /F1... = `fonts`."""
    objs: list[bytes] = []
    font_nums = []
    for spec in fonts:
        base = len(objs) + 1
        for body in spec.objects:
            objs.append(re.sub(rb"@(\d+)@", lambda m: b"%d 0 R" % (base + int(m.group(1))), body))
        font_nums.append(base)
    res = b"<< " + EXTGS + b" /Font << " + b" ".join(b"/F%d %d 0 R" % (k, n) for k, n in enumerate(font_nums)) + b" >> >>"
    cid = len(objs) + 1
    objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    pages_num = len(objs) + 2
    box = b"[%s]" % b" ".join(b"%g" % v for v in media)
    objs.append(b"<< /Type /Page /Parent %d 0 R /MediaBox %s /Resources %s /Contents %d 0 R >>"
                % (pages_num, box, res, cid))
    objs.append(b"<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % (pages_num - 1))
    objs.append(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_num)
    out = bytearray(b"%PDF-1.7\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, len(objs), x)
    return bytes(out)


# ---------------------------------------------------------------------- random pages


def _string(r: random.Random, spec: FontSpec, n: int) -> bytes:
    codes = [r.choice(spec.codes) for _ in range(n)]
    if spec.two_byte:
        return b"<" + b"".join(b"%04x" % c for c in codes) + b">"
    return b"<" + b"".join(b"%02x" % c for c in codes) + b">"


def random_tm(r: random.Random) -> bytes:
    k = r.random()
    x, y = r.uniform(-10, 190), r.uniform(-10, 140)
    if k < 0.45:
        m = (1, 0, 0, 1)
    elif k < 0.6:
        s = r.uniform(0.2, 3)
        m = (s, 0, 0, s)
    elif k < 0.7:
        m = (r.choice([1, -1]) * r.uniform(0.3, 2), 0, 0, r.choice([1, -1]) * r.uniform(0.3, 2))
    elif k < 0.8:
        m = r.choice([(0, 1, -1, 0), (0, -1, 1, 0), (-1, 0, 0, -1)])
    else:
        m = tuple(r.uniform(-2, 2) for _ in range(4))
    return b"%.4f %.4f %.4f %.4f %.3f %.3f Tm" % (*m, x, y)


def random_text(r: random.Random, fonts: list[FontSpec], simple: int) -> bytes:
    """One BT..ET group. `simple`: 0 = upright one-glyph pages only, 1 = no Tr/Tz/clip, 2 = anything."""
    g = [b"q"]
    if simple >= 2 and r.random() < 0.25:
        a, b, c, d = (r.uniform(-1.5, 1.5) for _ in range(4))
        g.append(b"%.4f %.4f %.4f %.4f %.3f %.3f cm" % (a, b, c, d, r.uniform(-30, 120), r.uniform(-30, 100)))
    if simple >= 2 and r.random() < 0.2:
        g.append(b"%.2f %.2f %.2f %.2f re W n" % (r.uniform(0, 150), r.uniform(0, 100), r.uniform(5, 100), r.uniform(5, 80)))
    g.append(b"%.3f %.3f %.3f rg %.3f %.3f %.3f RG" % tuple(r.choice([0, 1, r.random()]) for _ in range(6)))
    if simple >= 1 and r.random() < 0.25:
        g.append(b"/A%d gs" % r.randint(0, 4))
    if simple >= 2 and r.random() < 0.3:
        g.append(b"%.3f w" % r.choice([0, 0.2, 0.5, 1, 2.5]))
    g.append(b"BT")
    k = r.randrange(len(fonts))
    spec = fonts[k]
    if simple == 0:
        size = r.choice([6, 8, 10, 10.95, 12, 14.4, 17.28, 20.74, 24.88, 30, 40])
        g.append(b"/F%d %g Tf" % (k, size))
        g.append(b"%d %d Td" % (r.randint(0, 170), r.randint(10, 130)))
        g.append(_string(r, spec, 1) + b" Tj")
    else:
        size = r.choice([r.uniform(1, 14), r.uniform(4, 30), r.uniform(20, 80), r.choice([0.5, 5, 9, 11, 60, 120])])
        g.append(b"/F%d %.3f Tf" % (k, size))
        g.append(random_tm(r) if r.random() < 0.6 else b"%.3f %.3f Td" % (r.uniform(0, 170), r.uniform(10, 130)))
        if simple >= 2:
            if r.random() < 0.3:
                g.append(b"%.2f Tz" % r.choice([50, 80, 120, 200, r.uniform(10, 300)]))
            if r.random() < 0.3:
                g.append(b"%.3f Tc" % r.uniform(-1, 3))
            if r.random() < 0.3:
                g.append(b"%.3f Tw" % r.uniform(-2, 8))
            if r.random() < 0.2:
                g.append(b"%.3f Ts" % r.uniform(-5, 5))
            if r.random() < 0.5:
                g.append(b"%d Tr" % r.randint(0, 7))
        for _ in range(r.randint(1, 3)):
            q = r.random()
            if q < 0.5:
                g.append(_string(r, spec, r.randint(1, 8)) + b" Tj")
            elif q < 0.8:
                parts = []
                for _ in range(r.randint(1, 4)):
                    parts.append(_string(r, spec, r.randint(1, 5)))
                    if r.random() < 0.7:
                        parts.append(b"%d" % r.randint(-500, 800))
                g.append(b"[" + b" ".join(parts) + b"] TJ")
            else:
                g.append(b"%.2f TL T* " % r.uniform(-20, 20) + _string(r, spec, r.randint(1, 5)) + b" Tj")
    g.append(b"ET")
    g.append(b"Q")
    return b"\n".join(g)


def case(seed: int, kind: str = "any", simple: int = 2):
    """(content, fonts, zoom, transparent) for seed `seed`."""
    r = random.Random(seed)
    pool = [s for s in harvest() if kind == "any" or s.kind == kind or (kind == "cid" and s.kind.startswith("cid"))]
    if not pool:
        raise SystemExit(f"no {kind} fonts found in {DECKS} (build the test decks first)")
    fonts = [r.choice(pool) for _ in range(r.randint(1, 3))]
    groups = [random_text(r, fonts, simple) for _ in range(r.randint(1, 1 if simple == 0 else 4))]
    zoom = 1 if simple == 0 else r.choice([0.5, 1, 1.37, 2, 3.1])
    transparent = False if simple == 0 else r.random() < 0.3
    return b"\n".join(groups), fonts, zoom, transparent


def compare(content: bytes, fonts, zoom: float, transparent: bool):
    """(pixels that differ, PDFium's render, pure's render, per-pixel max difference)."""
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    data = pdf_bytes(content, fonts)
    a = PdfiumBackend().open(data)[0].render(zoom, transparent=transparent)
    b = PureBackend().open(data)[0].render(zoom, transparent=transparent)
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
    ap.add_argument("--kind", default="any")
    ap.add_argument("--simple", type=int, default=2)
    ap.add_argument("--out", default="out/render-torture-text")
    ap.add_argument("--no-shrink", action="store_true")
    args = ap.parse_args(argv)
    from ..pdf.api import PdfError
    out = Path(args.out)
    fails = refused = 0
    reasons: dict = {}
    for seed in range(args.seed0, args.seed0 + args.n):
        content, fonts, zoom, transparent = case(seed, args.kind, args.simple)
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
    print(f"seeds {args.seed0}..{args.seed0 + args.n - 1}: {fails} failed, {refused} refused")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
