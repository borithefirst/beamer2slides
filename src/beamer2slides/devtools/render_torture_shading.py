"""Random torture PDFs for the pure renderer's shadings (`pdf/pure/render_shading.py`): axial and
radial shadings painted by `sh` and as shading patterns filling or stroking paths, coloured by
functions of types 0, 2, 3 and 4 through Device Gray/RGB/CMYK, CalGray, Separation and DeviceN,
with random /Domain, /Extend, /Background, /BBox, pattern /Matrix, `cm`, clips, constant alpha and
forms. Each page is rendered by PDFium and by the pure reader; any pixel that differs is a failure,
shrunk to the lines of the page that still make it differ. A page the pure reader refuses is
counted, not failed (that is what refusing is for), and listed with its reason.

    python tools/render_torture_shading.py [seed0] [n] [--out DIR]

Failures go to DIR as seedN.pdf (the shrunk page) and seedN.png (PDFium | pure | difference).
The page layout is `render_torture`'s (`pdf_bytes` with the shading objects in front)."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import numpy as np

from .render_torture import EXTGS, num, random_cm, random_path

MEDIA = (0, 0, 200, 150)


def pdf_bytes(pages: list[bytes], objects: list[bytes], resources: bytes, forms=(), media=MEDIA) -> bytes:
    """`render_torture.pdf_bytes` with `objects` numbered 1.. in front (the resources refer to
    them) and `resources` added to every resource dictionary."""
    objs: list[bytes] = list(objects)

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    xids: list[int] = []
    for entries, content in forms:
        sub = b"<< " + EXTGS + resources + b" /XObject << " + \
            b" ".join(b"/X%d %d 0 R" % (j, x) for j, x in enumerate(xids)) + b" >> >>"
        xids.append(add(b"<< /Type /XObject /Subtype /Form /Resources %s %s /Length %d >>\nstream\n"
                        % (sub, entries, len(content)) + content + b"\nendstream"))
    res = b"<< " + EXTGS + resources + b" /XObject << " + \
        b" ".join(b"/X%d %d 0 R" % (j, x) for j, x in enumerate(xids)) + b" >> >>"
    content_ids = [add(b"<< /Length %d >>\nstream\n" % len(c) + c + b"\nendstream") for c in pages]
    pages_obj = len(objs) + 1 + len(pages)
    box = b"[%s]" % b" ".join(b"%g" % v for v in media)
    kids = [add(b"<< /Type /Page /Parent %d 0 R /MediaBox %s /Resources %s /Contents %d 0 R >>"
                % (pages_obj, box, res, cid)) for cid in content_ids]
    assert add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
               + b"] /Count %d >>" % len(kids)) == pages_obj
    cat = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj)
    out = bytearray(b"%PDF-1.7\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat, x)
    return bytes(out)


# ---------------------------------------------------------------------- random objects


class Builder:
    """Indirect objects (functions, shadings, patterns) and the resource entries naming them."""

    def __init__(self, r: random.Random):
        self.r = r
        self.objects: list[bytes] = []
        self.shadings: list[bytes] = []
        self.patterns: list[bytes] = []

    def add(self, b: bytes) -> bytes:
        self.objects.append(b)
        return b"%d 0 R" % len(self.objects)

    def resources(self) -> bytes:
        out = b""
        if self.shadings:
            out += b" /Shading << " + b" ".join(b"/Sh%d %s" % (i, s) for i, s in enumerate(self.shadings)) + b" >>"
        if self.patterns:
            out += b" /Pattern << " + b" ".join(b"/P%d %s" % (i, s) for i, s in enumerate(self.patterns)) + b" >>"
        return out

    # ---- numbers
    def f(self, lo=0.0, hi=1.0) -> bytes:
        r = self.r
        v = r.choice([r.uniform(lo, hi), r.uniform(lo, hi), round(r.uniform(lo, hi), 1), lo, hi,
                      r.uniform(lo - 0.5, hi + 0.5)])
        return (b"%.4f" % v).rstrip(b"0").rstrip(b".") if r.random() < 0.8 else b"%.5g" % v

    def arr(self, vals) -> bytes:
        return b"[" + b" ".join(vals) + b"]"

    def domain(self) -> tuple[bytes, float, float]:
        r = self.r
        d0, d1 = r.choice([(0, 1), (0, 1), (0, 1), (-1, 2), (0.2, 0.7), (1, 0), (0, 10), (0.5, 0.5)])
        return b"[%g %g]" % (d0, d1), d0, d1

    # ---- functions (one input, n outputs)
    def function(self, n: int, depth: int = 0, domain=None) -> bytes:
        r = self.r
        kind = r.choice([2, 2, 3, 3, 0, 4]) if depth == 0 else r.choice([2, 2, 2, 0, 4, 3 if depth < 2 else 2])
        dom = domain if domain is not None else r.choice([b"[0 1]", b"[0 1]", b"[0 1]", b"[-1 2]", b"[0.2 0.8]"])
        rng = b""
        if r.random() < 0.2 or kind in (0, 4):
            rng = b" /Range " + self.arr([b"%s %s" % (self.f(-0.2, 0.3), self.f(0.7, 1.2)) for _ in range(n)])
        if kind == 2:
            c0 = b" /C0 " + self.arr([self.f() for _ in range(n)]) if r.random() < 0.9 or n != 1 else b""
            c1 = b" /C1 " + self.arr([self.f() for _ in range(n)]) if r.random() < 0.9 or n != 1 else b""
            nn = r.choice([b"1", b"1", b"2", b"0.5", b"3.3", b"0", b"1.0"])
            body = b"<< /FunctionType 2 /Domain %s%s%s%s /N %s >>" % (dom, rng, c0, c1, nn)
            return self.add(body) if r.random() < 0.5 else body
        if kind == 3:
            k = r.randint(1, 4)
            ds = [float(x) for x in dom[1:-1].split()]
            cuts = sorted(r.uniform(ds[0], ds[1]) for _ in range(k - 1))
            if r.random() < 0.2 and cuts:
                cuts[0] = ds[0]
            subs = [self.function(n, depth + 1, b"[0 1]") for _ in range(k)]
            enc = []
            for _ in range(k):
                enc.append(r.choice([b"0 1", b"1 0", b"0 1", b"%s %s" % (self.f(), self.f())]))
            body = b"<< /FunctionType 3 /Domain %s%s /Functions [%s] /Bounds [%s] /Encode [%s] >>" % (
                dom, rng, b" ".join(subs), b" ".join(b"%.4f" % c for c in cuts), b" ".join(enc))
            return self.add(body) if r.random() < 0.5 else body
        if kind == 0:
            size = r.choice([1, 2, 2, 3, 5, 17, 256])
            bps = r.choice([1, 2, 4, 8, 8, 8, 12, 16, 24, 32])
            nbits = size * n * bps
            data = bytes(r.randrange(256) for _ in range((nbits + 7) // 8))
            extra = b""
            if r.random() < 0.3:
                extra += b" /Encode [%s %s]" % (self.f(0, size), self.f(0, size))
            if r.random() < 0.3:
                extra += b" /Decode " + self.arr([b"%s %s" % (self.f(), self.f()) for _ in range(n)])
            return self.add(b"<< /FunctionType 0 /Domain %s%s /Size [%d] /BitsPerSample %d%s /Length %d >>\nstream\n"
                            % (dom, rng, size, bps, extra, len(data)) + data + b"\nendstream")
        # PostScript: output i from `i index` (x is at the bottom) through a random expression
        prog = []
        for i in range(n):
            prog.append(b"%d index" % i)
            prog.append(self.ps_expr())
        prog.append(b"%d -1 roll pop" % (n + 1))
        text = b"{ " + b" ".join(prog) + b" }"
        return self.add(b"<< /FunctionType 4 /Domain %s%s /Length %d >>\nstream\n" % (dom, rng, len(text))
                        + text + b"\nendstream")

    def ps_expr(self) -> bytes:
        r = self.r
        parts = []
        for _ in range(r.randint(1, 4)):
            parts.append(r.choice([
                b"%s mul" % self.f(-2, 2), b"%s add" % self.f(-1, 1), b"%s sub" % self.f(-1, 1),
                b"%s div" % r.choice([b"2", b"0.3", b"0", b"-1.5"]), b"abs", b"neg", b"dup mul",
                b"360 mul sin", b"180 mul cos", b"sqrt", b"%s exp" % r.choice([b"2", b"0.5", b"1.7"]),
                b"1 add ln", b"2 add log", b"1 atan 360 div", b"10 mul floor 10 div", b"10 mul ceiling 10 div",
                b"10 mul round 10 div", b"10 mul truncate 10 div", b"10 mul cvi 10 idiv 0.1 mul",
                b"0.5 gt { 0.2 } { 0.9 } ifelse", b"dup 0.5 lt { 1 exch sub } if",
                b"100 mul cvi 7 mod 7 div", b"100 mul cvi 3 bitshift 800 div", b"100 mul cvi 255 and 255 div",
                b"1 exch 2 copy pop pop", b"0.3 exch 2 1 roll pop", b"dup 0.2 ge exch 0.8 le and { 1 } { 0 } ifelse",
                b"cvr", b"1.5e-1 add", b"-.25 mul",
            ]))
        return b" ".join(parts)

    # ---- colour spaces: (object, component count)
    def colorspace(self) -> tuple[bytes, int]:
        r = self.r
        k = r.random()
        if k < 0.3:
            return b"/DeviceRGB", 3
        if k < 0.45:
            return b"/DeviceGray", 1
        if k < 0.6:
            return b"/DeviceCMYK", 4
        if k < 0.7:
            return b"[/CalGray << /WhitePoint [0.9505 1 1.089] >>]", 1
        if k < 0.85:
            base, n = r.choice([(b"/DeviceRGB", 3), (b"/DeviceCMYK", 4), (b"/DeviceGray", 1)])
            return b"[/Separation /Spot %s %s]" % (base, self.function(n, 1)), 1
        base, n = r.choice([(b"/DeviceRGB", 3), (b"/DeviceCMYK", 4)])
        m = r.choice([1, 2, 3])
        names = b"[" + b" ".join(b"/C%d" % i for i in range(m)) + b"]"
        fn = self.nfunction(m, n)
        return b"[/DeviceN %s %s %s]" % (names, base, fn), m

    def nfunction(self, m: int, n: int) -> bytes:
        """A function of m inputs, n outputs (DeviceN's tint transform)."""
        r = self.r
        dom = b"[" + b" ".join([b"0 1"] * m) + b"]"
        rng = b"[" + b" ".join([b"0 1"] * n) + b"]"
        if m == 1 and r.random() < 0.5:
            return self.function(n, 1, b"[0 1]")
        if r.random() < 0.5:
            size = r.choice([2, 3])
            bps = r.choice([8, 16])
            data = bytes(r.randrange(256) for _ in range(size ** m * n * bps // 8))
            return self.add(b"<< /FunctionType 0 /Domain %s /Range %s /Size [%s] /BitsPerSample %d /Length %d >>\nstream\n"
                            % (dom, rng, b" ".join([b"%d" % size] * m), bps, len(data)) + data + b"\nendstream")
        # PostScript: output j = a weighted sum of the inputs
        prog = []
        for j in range(n):
            terms = []
            for i in range(m):
                terms.append(b"%d index %s mul" % (i + len(terms) + j, self.f()))
            prog.append(b" ".join(terms) + b" add" * (m - 1))
        prog.append(b"%d %d roll" % (m + n, n) + b" pop" * m)
        text = b"{ " + b" ".join(prog) + b" }"
        return self.add(b"<< /FunctionType 4 /Domain %s /Range %s /Length %d >>\nstream\n" % (dom, rng, len(text))
                        + text + b"\nendstream")

    # ---- shadings
    def shading(self, pattern: bool) -> bytes:
        r = self.r
        stype = r.choice([2, 2, 3, 3, 3])
        cs, n = self.colorspace()
        dom, d0, d1 = self.domain()
        if stype == 2:
            coords = [self.f(-40, 240) for _ in range(4)]
            if r.random() < 0.1:
                coords[2:] = coords[:2]
        else:
            x0, y0, x1, y1 = (self.f(-20, 220) for _ in range(4))
            r0, r1 = self.f(0, 60), self.f(0, 120)
            if r.random() < 0.25:
                x1, y1 = x0, y0
            if r.random() < 0.15:
                r0 = b"0"
            if r.random() < 0.1:
                r0, r1 = r1, r0
            coords = [x0, y0, r0, x1, y1, r1]
        body = b"<< /ShadingType %d /ColorSpace %s /Coords %s" % (stype, cs, self.arr(coords))
        if r.random() < 0.7:
            body += b" /Domain " + dom
        else:
            d0, d1 = 0, 1
        if r.random() < 0.7:
            body += b" /Extend [%s %s]" % (r.choice([b"true", b"false"]), r.choice([b"true", b"false"]))
        fdom = b"[%g %g]" % (d0, d1) if r.random() < 0.8 else b"[0 1]"
        if n > 1 and r.random() < 0.3:
            body += b" /Function [%s]" % b" ".join(self.function(1, 1, fdom) for _ in range(n))
        else:
            body += b" /Function %s" % self.function(n, 0, fdom)
        if r.random() < 0.25:
            body += b" /Background " + self.arr([self.f() for _ in range(n)])
        if r.random() < 0.25:
            body += b" /BBox " + self.arr([self.f(-20, 220) for _ in range(4)])
        if r.random() < 0.2:
            body += b" /AntiAlias true"
        body += b" >>"
        return self.add(body) if pattern or r.random() < 0.7 else body

    def new_shading(self) -> int:
        self.shadings.append(self.shading(False))
        return len(self.shadings) - 1

    def new_pattern(self) -> int:
        r = self.r
        m = b""
        if r.random() < 0.6:
            vals = [r.choice([1, 0, -1, r.uniform(-2, 2)]) for _ in range(4)] + [r.uniform(-60, 60), r.uniform(-60, 60)]
            m = b" /Matrix [" + b" ".join(b"%.4f" % v for v in vals) + b"]"
        self.patterns.append(self.add(b"<< /PatternType 2 /Shading %s%s >>" % (self.shading(True), m)))
        return len(self.patterns) - 1


def random_group(r: random.Random, b: Builder, nforms: int) -> bytes:
    g = [b"q"]
    if r.random() < 0.4:
        g.append(random_cm(r))
    if r.random() < 0.35:
        g.append(random_path(r) + r.choice([b" W n", b" W* n"]))
    if r.random() < 0.3:
        g.append(b"/A%d gs" % r.randint(0, 4))
    k = r.random()
    if nforms and k < 0.2:
        g.append(b"/X%d Do" % r.randrange(nforms))
    elif k < 0.5:
        sh = r.randrange(len(b.shadings)) if b.shadings and r.random() < 0.3 else b.new_shading()
        g.append(b"/Sh%d sh" % sh)
    elif k < 0.9:
        p = r.randrange(len(b.patterns)) if b.patterns and r.random() < 0.3 else b.new_pattern()
        stroke = r.random() < 0.35
        if stroke:
            g.append(b"/Pattern CS /P%d SCN %s w %d J %d j" % (p, r.choice([b"1", b"4", b"12", b"0"]),
                                                             r.randint(0, 2), r.randint(0, 2)))
            if r.random() < 0.2:
                g.append(b"[6 3] 0 d")
            g.append(random_path(r) + b" " + r.choice([b"S", b"s", b"B", b"b"]))
        else:
            g.append(b"/Pattern cs /P%d scn" % p)
            g.append(random_path(r) + b" " + r.choice([b"f", b"f*", b"B", b"b*"]))
    else:
        g.append(b"%.3f %.3f %.3f rg" % (r.random(), r.random(), r.random()))
        g.append(random_path(r) + b" f")
    g.append(b"Q")
    return b"\n".join(g)


def case(seed: int):
    """(content, forms, objects, resources, zoom, transparent) for `seed`."""
    r = random.Random(seed)
    b = Builder(r)
    forms = []
    for k in range(r.choice([0, 0, 0, 1, 2])):
        entries = b"/BBox [%s %s %s %s]" % (num(r), num(r), num(r), num(r))
        if r.random() < 0.7:
            m = [r.choice([1, 0, -1, r.uniform(-2, 2)]) for _ in range(4)] + [r.uniform(-60, 60), r.uniform(-60, 60)]
            entries += b" /Matrix [" + b" ".join(b"%.4f" % v for v in m) + b"]"
        forms.append((entries, b"\n".join(random_group(r, b, k) for _ in range(r.randint(1, 3)))))
    content = b"\n".join(random_group(r, b, len(forms)) for _ in range(r.randint(1, 4)))
    return content, forms, b.objects, b.resources(), r.choice([0.5, 1, 1.37, 2, 3.1]), r.random() < 0.3


def compare(content: bytes, objects, resources, zoom: float, transparent: bool, forms=()):
    """(pixels that differ or None when the pure reader refuses, PDFium's render, pure's render,
    per-pixel difference or the refusal)."""
    from ..pdf.api import PdfError
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    data = pdf_bytes([content], objects, resources, forms=forms)
    a = PdfiumBackend().open(data)[0].render(zoom, transparent=transparent)
    try:
        b = PureBackend().open(data)[0].render(zoom, transparent=transparent)
    except PdfError as e:
        return None, a, None, str(e)
    d = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return int((d > 0).sum()), a, b, d


def shrink(content: bytes, objects, resources, zoom: float, transparent: bool, forms=()):
    """Drop lines (the page's, then each form's) while the difference remains."""
    forms = list(forms)

    def fails(c, fs):
        try:
            return (compare(c, objects, resources, zoom, transparent, fs)[0] or 0) > 0
        except Exception:
            return False

    def cut(text, test):
        lines = text.split(b"\n")
        changed = True
        while changed:
            changed = False
            for i in range(len(lines)):
                if lines[i] in (b"q", b"Q"):
                    continue
                trial = lines[:i] + lines[i + 1:]
                if test(b"\n".join(trial)):
                    lines, changed = trial, True
                    break
        return b"\n".join(lines)

    content = cut(content, lambda c: fails(c, forms))
    for k in range(len(forms)):
        entries = forms[k][0]
        forms[k] = (entries, cut(forms[k][1], lambda c: fails(content, forms[:k] + [(entries, c)] + forms[k + 1:])))
    return content, forms


def run(seed0: int, n: int, out: Path | None = None, verbose: bool = True) -> dict:
    """{'failed': [seeds], 'refused': {reason: count}, 'drawn': count}."""
    stats = {"failed": [], "refused": {}, "drawn": 0}
    for seed in range(seed0, seed0 + n):
        content, forms, objects, resources, zoom, transparent = case(seed)
        try:
            npx, a, b, d = compare(content, objects, resources, zoom, transparent, forms)
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
        small, sforms = shrink(content, objects, resources, zoom, transparent, forms)
        npx, a, b, d = compare(small, objects, resources, zoom, transparent, sforms)
        print(f"seed {seed} zoom {zoom} transparent {transparent}: {npx} px, max {d.max() if npx else 0}")
        for k, (e, c) in enumerate(sforms):
            print(f"-- X{k} {e.decode()}\n{c.decode()}")
        print("-- page\n" + small.decode())
        if out is not None and npx:
            out.mkdir(parents=True, exist_ok=True)
            from PIL import Image
            vis = np.concatenate([a[..., :3], b[..., :3], np.stack([np.where(d > 0, 255, 0)] * 3, -1)], 1)
            Image.fromarray(vis.astype(np.uint8)).save(out / f"seed{seed}.png")
            (out / f"seed{seed}.pdf").write_bytes(pdf_bytes([small], objects, resources, forms=sforms))
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seed0", type=int, nargs="?", default=0)
    ap.add_argument("n", type=int, nargs="?", default=200)
    ap.add_argument("--out", default="out/render-torture-shading")
    args = ap.parse_args(argv)
    stats = run(args.seed0, args.n, Path(args.out))
    print(f"seeds {args.seed0}..{args.seed0 + args.n - 1}: {stats['drawn']} exact, "
          f"{len(stats['failed'])} failed, refused {stats['refused']}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
