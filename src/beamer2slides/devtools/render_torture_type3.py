"""Random torture PDFs for the pure renderer's Type 3 text (`pdf/pure/render_type3.py`): made-up
Type 3 fonts whose glyphs are image masks (inline or XObjects, with /Decode, flipped, blank edge
rows), paths (filled, stroked, clipped, with colours a d1 glyph must ignore), coloured d0 glyphs,
forms, nested Type 3 fonts, empty glyphs and codes without a glyph, under ordinary, mirrored,
skewed and translated FontMatrix values, drawn with every render mode, character and horizontal
spacing, text matrices, fill colours, constant alpha and clips, on white or clear bitmaps at
several zooms. Each page is rendered by PDFium and by the pure reader; any pixel that differs is a
failure, shrunk to the lines of the page that still make it differ. A page the pure reader refuses
is counted, not failed, and listed with its reason.

    python tools/render_torture_type3.py [seed0] [n] [--out DIR] [-q]

Failures go to DIR as seedN.pdf (the shrunk page) and seedN.png (PDFium | pure | difference)."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import numpy as np

from .render_torture import EXTGS

MEDIA = (0, 0, 200, 150)


def _n(v: float) -> bytes:
    return (b"%.4f" % v).rstrip(b"0").rstrip(b".") or b"0"


def _ns(*vs) -> bytes:
    return b" ".join(_n(v) for v in vs)


def pdf_bytes(content: bytes, objects: list[bytes], fonts: list[tuple[bytes, int]], media=MEDIA) -> bytes:
    """One page of `content`, with `objects` numbered 1.. in front and `fonts` (name, object
    number) in its /Font dictionary; the ExtGStates are `render_torture.EXTGS` (/A0../A4)."""
    objs = list(objects)
    res = b"<< " + EXTGS + b" /Font << " + b" ".join(b"/%s %d 0 R" % (n, i) for n, i in fonts) + b" >> >>"
    objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    cid = len(objs)
    pages = cid + 2
    box = b"[%s]" % b" ".join(b"%g" % v for v in media)
    objs.append(b"<< /Type /Page /Parent %d 0 R /MediaBox %s /Resources %s /Contents %d 0 R >>" % (pages, box, res, cid))
    objs.append(b"<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % (cid + 1))
    objs.append(b"<< /Type /Catalog /Pages %d 0 R >>" % pages)
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


GLYPH_KINDS = ("bitmap", "bitmap", "bitmap", "path", "path", "colored", "form", "nested", "empty",
               "colored_image", "self", "mixed", "mixed")


class Builder:
    def __init__(self, r: random.Random):
        self.r = r
        self.objects: list[bytes] = []
        self.fonts: list[tuple[bytes, int]] = []

    def add(self, b: bytes) -> int:
        self.objects.append(b)
        return len(self.objects)

    def stream(self, entries: bytes, data: bytes) -> int:
        return self.add(b"<< %s /Length %d >>\nstream\n" % (entries, len(data)) + data + b"\nendstream")

    # ---- glyph pieces
    def mask(self):
        """(width, height, packed rows, /Decode entry) of a random image mask."""
        r = self.r
        w, h = r.randint(1, 28), r.randint(1, 28)
        if r.random() < 0.1:
            w, h = r.randint(30, 70), r.randint(30, 70)
        bits = np.array([[r.random() < 0.55 for _ in range(w)] for _ in range(h)], bool)
        if r.random() < 0.85:           # PDFium stretches a glyph only when both edge rows have ink
            for row in (0, h - 1):
                if not bits[row].any():
                    bits[row, r.randrange(w)] = True
        if r.random() < 0.3:
            bits[:] = r.random() < 0.5
        decode = r.choice([b"", b"", b" /D [1 0]", b" /D [0 1]"])
        # the painted samples are the 0 ones by default, the 1 ones under /Decode [1 0]
        painted = bits if decode == b" /D [1 0]" else ~bits
        packed = np.packbits(painted.astype(np.uint8), axis=1).tobytes()
        return w, h, packed, decode

    def path_ops(self, box, colors: bool) -> list[bytes]:
        r = self.r
        l, b, rt, t = box
        ops = []

        def pt():
            return r.uniform(l, rt), r.uniform(b, t)

        for _ in range(r.randint(1, 3)):
            if colors and r.random() < 0.5:
                ops.append(r.choice([b"%s rg" % _ns(r.random(), r.random(), r.random()), b"%s g" % _n(r.random()),
                                     b"%s k" % _ns(r.random(), r.random(), r.random(), r.random()),
                                     b"%s RG" % _ns(r.random(), r.random(), r.random())]))
            if r.random() < 0.15:
                x, y = pt()
                ops.append(b"%s re W n" % _ns(x, y, r.uniform(0.2, 1) * (rt - l), r.uniform(0.2, 1) * (t - b)))
            k = r.random()
            if k < 0.3:
                x, y = pt()
                ops.append(b"%s re" % _ns(x, y, r.uniform(-0.5, 1) * (rt - l), r.uniform(-0.5, 1) * (t - b)))
            else:
                x, y = pt()
                seg = [b"%s m" % _ns(x, y)]
                for _ in range(r.randint(2, 5)):
                    if r.random() < 0.3:
                        seg.append(b"%s c" % _ns(*pt(), *pt(), *pt()))
                    else:
                        seg.append(b"%s l" % _ns(*pt()))
                if r.random() < 0.6:
                    seg.append(b"h")
                ops.append(b" ".join(seg))
            if r.random() < 0.3:
                ops[-1] = b"%s w " % _n(r.uniform(0.01, 0.12) * (t - b)) + ops[-1]
            ops[-1] += b" " + r.choice([b"f", b"f", b"f*", b"S", b"B", b"b*", b"s"])
        return ops

    def glyph(self, kind: str, U: float, res: dict, depth: int) -> bytes:
        """The content stream of one glyph procedure (glyph space: U units per text unit)."""
        r = self.r
        w = r.uniform(0.2, 1.1) * U
        box = (r.uniform(-0.1, 0.1) * U, r.uniform(-0.3, 0.05) * U, w, r.uniform(0.3, 0.9) * U)
        l, b, rt, t = box
        d1 = b"%s 0 %s d1" % (_n(w), _ns(*(round(v) for v in box)))
        d0 = b"%s 0 d0" % _n(w)
        if kind == "empty":
            return d1
        if kind in ("bitmap", "colored_image"):
            mw, mh, packed, decode = self.mask()
            sx, sy = rt - l, t - b
            if r.random() < 0.15:
                sx = r.uniform(0.2, 1.2) * U
            tx, ty = l, b
            if r.random() < 0.1:       # flipped upside down or mirrored
                if r.random() < 0.5:
                    sy, ty = -sy, t
                else:
                    sx, tx = -sx, rt
            skew = b"0 0"
            if r.random() < 0.08:
                skew = _ns(r.uniform(-0.3, 0.3) * sy, r.uniform(-0.3, 0.3) * sx)
            head = [d0 if kind == "colored_image" else d1]
            if kind == "colored_image" and r.random() < 0.5:
                head.append(b"%s rg" % _ns(r.random(), r.random(), r.random()))
            head.append(b"q %s %s %s %s cm" % (_n(sx), skew, _n(sy), _ns(tx, ty)))
            if r.random() < 0.75:
                if r.random() < 0.3:
                    data = packed.hex().encode() + b">"
                    img = b"BI /IM true /W %d /H %d%s /F /AHx ID %s EI" % (mw, mh, decode, data)
                else:
                    img = b"BI /IM true /W %d /H %d%s ID " % (mw, mh, decode) + packed + b"\nEI"
            else:
                name = b"I%d" % len(res.setdefault("XObject", []))
                oid = self.stream(b"/Type /XObject /Subtype /Image /ImageMask true /Width %d /Height %d%s"
                                  % (mw, mh, decode.replace(b"/D ", b"/Decode ")), packed)
                res["XObject"].append((name, oid))
                img = b"/" + name + b" Do"
            return b"\n".join(head + [img, b"Q"])
        if kind == "path":
            return b"\n".join([d1] + self.path_ops(box, True))
        if kind == "mixed":
            ops = [r.choice([d0, d1])] + self.path_ops(box, True)[:r.randint(0, 2)]
            for _ in range(r.randint(1, 2)):
                k = r.random()
                cm = b"q %s 0 0 %s %s cm" % (_n(r.uniform(0.2, 1) * (rt - l)), _n(r.uniform(0.2, 1) * (t - b)),
                                             _ns(r.uniform(l, rt), r.uniform(b, t)))
                if k < 0.4:
                    mw, mh, packed, decode = self.mask()
                    ops += [cm, b"BI /IM true /W %d /H %d%s ID " % (mw, mh, decode) + packed + b"\nEI", b"Q"]
                elif k < 0.7:
                    w_, h_ = r.randint(1, 12), r.randint(1, 12)
                    data = bytes(r.randrange(256) for _ in range(w_ * h_))
                    ops += [cm, b"BI /W %d /H %d /CS /G /BPC 8 ID " % (w_, h_) + data + b"\nEI", b"Q"]
                else:
                    name = b"Sh%d" % len(res.setdefault("Shading", []))
                    sid = self.add(b"<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [%s] /Function << "
                                   b"/FunctionType 2 /Domain [0 1] /C0 [%s] /C1 [%s] /N 1 >> /Extend [true true] >>"
                                   % (_ns(l, b, rt, t), _ns(r.random(), r.random(), r.random()),
                                      _ns(r.random(), r.random(), r.random())))
                    res["Shading"].append((name, sid))
                    ops += [b"q %s re W n /%s sh Q" % (_ns(l, b, 0.5 * (rt - l), 0.5 * (t - b)), name)]
            return b"\n".join(ops)
        if kind == "colored":
            return b"\n".join([d0] + self.path_ops(box, True))
        if kind == "form":
            inner = self.path_ops(box, True)
            fid = self.stream(b"/Type /XObject /Subtype /Form /BBox [%s]" % _ns(l - U, b - U, rt + U, t + U),
                              b"\n".join(inner))
            name = b"F%d" % len(res.setdefault("XObject", []))
            res["XObject"].append((name, fid))
            head = r.choice([d0, d1])
            pre = [b"%s rg" % _ns(r.random(), r.random(), r.random())] if r.random() < 0.4 else []
            return b"\n".join([head] + pre + [b"/" + name + b" Do"] + self.path_ops(box, True)[:r.randint(0, 1)])
        # nested Type 3 text (or the font inside its own glyphs)
        if kind == "self" or depth >= 2:
            name, fsize = b"S", U
            res.setdefault("Font", []).append((b"S", None))
        else:
            fid, _ = self.font(depth + 1)
            name = b"N%d" % len(res.setdefault("Font", []))
            res["Font"].append((name, fid))
            fsize = r.uniform(0.3, 1.2) * U
        head = r.choice([d0, d1])
        pre = [b"%s rg" % _ns(r.random(), r.random(), r.random())] if r.random() < 0.5 else []
        codes = bytes(r.randrange(4) for _ in range(r.randint(1, 3)))
        text = b"BT /%s %s Tf %s Td <%s> Tj ET" % (name, _n(fsize), _ns(l, b + 0.3 * (t - b)), codes.hex().encode())
        return b"\n".join([head] + pre + [text])

    def font(self, depth: int = 0) -> tuple[int, int]:
        """(object number, number of glyphs) of a random Type 3 font."""
        r = self.r
        s = r.choice([0.001, 0.001, 0.001, 0.01, 0.011, 0.0005, 0.05, 1.0])
        fm = [s, 0.0, 0.0, s, 0.0, 0.0]
        k = r.random()
        if k < 0.1:
            fm[0] = -s
        elif k < 0.2:
            fm[3] = -s
        elif k < 0.3:
            fm[1], fm[2] = r.uniform(-0.3, 0.3) * s, r.uniform(-0.3, 0.3) * s
        elif k < 0.4:
            fm[3] = s * r.uniform(0.4, 2.5)
        if r.random() < 0.15:
            fm[4], fm[5] = r.uniform(-0.2, 0.2), r.uniform(-0.2, 0.2)
        U = 1 / s
        n = r.randint(1, 5)
        res: dict = {}
        kinds = [r.choice(GLYPH_KINDS[:9] if depth or r.random() < 0.5 else GLYPH_KINDS) for _ in range(n)]
        if r.random() < 0.4:
            kinds = [kinds[0]] * n
        procs = []
        widths = []
        for i, kind in enumerate(kinds):
            content = self.glyph(kind, U, res, depth)
            procs.append((b"g%d" % i, self.stream(b"", content)))
            widths.append(r.uniform(0.3, 1.0) * U)
        me = self.add(b"")                 # placeholder: the font may name itself
        fonts = [(nm, me if oid is None else oid) for nm, oid in res.get("Font", [])]
        parts = []
        if res.get("XObject"):
            parts.append(b"/XObject << " + b" ".join(b"/%s %d 0 R" % x for x in res["XObject"]) + b" >>")
        if res.get("Shading"):
            parts.append(b"/Shading << " + b" ".join(b"/%s %d 0 R" % x for x in res["Shading"]) + b" >>")
        if fonts:
            parts.append(b"/Font << " + b" ".join(b"/%s %d 0 R" % x for x in fonts) + b" >>")
        k = r.random()
        if parts or k < 0.5:
            resources = b" /Resources << /ProcSet [/PDF /ImageB] " + b" ".join(parts) + b" >>"
        else:
            resources = b""
        bbox = b"[0 0 0 0]" if r.random() < 0.3 else b"[%s]" % _ns(-0.1 * U, -0.3 * U, 1.2 * U, 1.0 * U)
        self.objects[me - 1] = (
            b"<< /Type /Font /Subtype /Type3 /FontMatrix [%s] /FontBBox %s /CharProcs << %s >> "
            b"/Encoding << /Type /Encoding /Differences [0 %s] >> /FirstChar 0 /LastChar %d /Widths [%s]%s >>"
            % (_ns(*fm), bbox, b" ".join(b"/%s %d 0 R" % p for p in procs),
               b" ".join(b"/" + p[0] for p in procs), n - 1, _ns(*widths), resources))
        return me, n

    # ---- the page
    def draw(self) -> bytes:
        r = self.r
        if not self.fonts or r.random() < 0.3:
            fid, n = self.font()
            self.fonts.append((b"T%d" % len(self.fonts), fid))
            self.glyph_counts = getattr(self, "glyph_counts", []) + [n]
        fi = r.randrange(len(self.fonts))
        name, n = self.fonts[fi][0], self.glyph_counts[fi]
        ops = [b"q"]
        if r.random() < 0.7:
            ops.append(b"%s rg" % _ns(r.random(), r.random(), r.random()))
        if r.random() < 0.25:
            ops.append(r.choice([b"/A0 gs", b"/A1 gs", b"/A2 gs", b"/A3 gs", b"/A4 gs"]))
        if r.random() < 0.2:
            ops.append(b"%s re W n" % _ns(r.uniform(0, 150), r.uniform(0, 100), r.uniform(5, 150), r.uniform(5, 100)))
        if r.random() < 0.1:
            ops.append(b"%s m %s l %s l h W n" % (_ns(r.uniform(0, 200), r.uniform(0, 150)),
                                                   _ns(r.uniform(0, 200), r.uniform(0, 150)),
                                                   _ns(r.uniform(0, 200), r.uniform(0, 150))))
        ops.append(b"BT")
        ops.append(b"/%s %s Tf" % (name, _n(r.choice([r.uniform(4, 16), r.uniform(16, 60), r.uniform(1, 4)]))))
        if r.random() < 0.3:
            ops.append(b"%d Tr" % r.randint(0, 7))
        if r.random() < 0.2:
            ops.append(b"%s Tc" % _n(r.uniform(-1, 3)))
        if r.random() < 0.15:
            ops.append(b"%s Tz" % _n(r.uniform(50, 150)))
        k = r.random()
        x, y = r.uniform(-10, 170), r.uniform(0, 140)
        if k < 0.6:
            ops.append(b"%s Td" % _ns(x, y))
        elif k < 0.85:
            ops.append(b"%s Tm" % _ns(r.choice([1, 1, -1]) * r.uniform(0.5, 2), 0, 0,
                                      r.choice([1, 1, -1]) * r.uniform(0.5, 2), x, y))
        else:
            ops.append(b"%s Tm" % _ns(r.uniform(-2, 2), r.uniform(-2, 2), r.uniform(-2, 2), r.uniform(-2, 2), x, y))
        codes = bytes(r.randrange(n + (1 if r.random() < 0.2 else 0)) for _ in range(r.randint(1, 8)))
        if r.random() < 0.3:
            half = len(codes) // 2
            ops.append(b"[<%s> %s <%s>] TJ" % (codes[:half].hex().encode(), _n(r.uniform(-500, 500)),
                                                 codes[half:].hex().encode()))
        else:
            ops.append(b"<%s> Tj" % codes.hex().encode())
        ops.append(b"ET")
        ops.append(b"Q")
        return b"\n".join(ops)


def case(seed: int):
    """(content, objects, fonts, zoom, transparent) for `seed`."""
    r = random.Random(seed)
    b = Builder(r)
    content = b"\n".join(b.draw() for _ in range(r.randint(1, 3)))
    zoom = r.choice([0.5, 1, 1.37, 2, 3.1])
    transparent = r.random() < 0.3
    return content, b.objects, b.fonts, zoom, transparent


def compare(content: bytes, objects, fonts, zoom: float, transparent: bool):
    """(pixels that differ or None when the pure reader refuses, PDFium's render, pure's render,
    per-pixel difference or the refusal)."""
    from ..pdf.api import PdfError
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    import dataclasses
    data = pdf_bytes(content, objects, fonts)
    docs = PdfiumBackend().open(data), PureBackend().open(data)
    try:
        # what the page holds first: a Type 3 glyph that selects fonts loads their chars
        read = [([dataclasses.astuple(o) for o in doc[0].objects()], doc[0].object_bounds()) for doc in docs]
        a = docs[0][0].render(zoom, transparent=transparent)
        if read[0] != read[1]:
            return -1, a, a, "objects or bounds apart"
        try:
            b = docs[1][0].render(zoom, transparent=transparent)
        except PdfError as e:
            return None, a, None, str(e)
    finally:
        for doc in docs:
            doc.close()
    d = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return int((d > 0).sum()), a, b, d


def shrink(content: bytes, objects, fonts, zoom: float, transparent: bool):
    """Drop lines of the page while the difference remains."""
    def fails(c):
        try:
            return (compare(c, objects, fonts, zoom, transparent)[0] or 0) != 0
        except Exception:
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


def run(seed0: int, n: int, out: Path | None = None, verbose: bool = True) -> dict:
    """{'failed': [seeds], 'refused': {reason: count}, 'drawn': count}."""
    stats = {"failed": [], "refused": {}, "drawn": 0}
    for seed in range(seed0, seed0 + n):
        content, objects, fonts, zoom, transparent = case(seed)
        try:
            npx, a, b, d = compare(content, objects, fonts, zoom, transparent)
        except Exception as e:
            if verbose:
                print("seed", seed, "EXC", type(e).__name__, e)
            stats["failed"].append(seed)
            continue
        if npx is None:
            stats["refused"][d] = stats["refused"].get(d, 0) + 1
            continue
        if not npx:
            stats["drawn"] += 1
            continue
        stats["failed"].append(seed)
        if not verbose:
            continue
        small = shrink(content, objects, fonts, zoom, transparent)
        npx, a, b, d = compare(small, objects, fonts, zoom, transparent)
        if isinstance(d, str):
            print(f"seed {seed}: {d}")
        else:
            print(f"seed {seed} zoom {zoom} transparent {transparent}: {npx} px, max {d.max() if npx else 0}")
        print("-- page\n" + small.decode("latin-1")[:1500])
        if out is not None and npx and not isinstance(d, str):
            out.mkdir(parents=True, exist_ok=True)
            from PIL import Image
            vis = np.concatenate([a[..., :3], b[..., :3], np.stack([np.where(d > 0, 255, 0)] * 3, -1)], 1)
            Image.fromarray(vis.astype(np.uint8)).save(out / f"seed{seed}.png")
            (out / f"seed{seed}.pdf").write_bytes(pdf_bytes(small, objects, fonts))
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seed0", type=int, nargs="?", default=0)
    ap.add_argument("n", type=int, nargs="?", default=200)
    ap.add_argument("--out", default="out/render-torture-type3")
    ap.add_argument("-q", "--quiet", action="store_true", help="no shrinking, only the counts")
    args = ap.parse_args(argv)
    stats = run(args.seed0, args.n, Path(args.out), verbose=not args.quiet)
    print(f"seeds {args.seed0}..{args.seed0 + args.n - 1}: {stats['drawn']} exact, "
          f"{len(stats['failed'])} failed {stats['failed'][:30]}, refused {stats['refused']}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
