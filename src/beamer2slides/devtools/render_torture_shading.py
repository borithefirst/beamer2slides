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
    them) and `resources` added to every resource dictionary; `resources` may be a pair (entries,
    ExtGState entries added to render_torture's)."""
    objs: list[bytes] = list(objects)
    extgs = EXTGS
    if isinstance(resources, tuple):
        resources, more = resources
        extgs = EXTGS[:-2] + more + b" >>"

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    xids: list[int] = []
    for entries, content in forms:
        sub = b"<< " + extgs + resources + b" /XObject << " + \
            b" ".join(b"/X%d %d 0 R" % (j, x) for j, x in enumerate(xids)) + b" >> >>"
        xids.append(add(b"<< /Type /XObject /Subtype /Form /Resources %s %s /Length %d >>\nstream\n"
                        % (sub, entries, len(content)) + content + b"\nendstream"))
    res = b"<< " + extgs + resources + b" /XObject << " + \
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


class _Bits:
    """A big-endian bit writer (mesh shading streams)."""

    def __init__(self):
        self.v, self.n = 0, 0

    def put(self, value: int, bits: int) -> None:
        self.v = (self.v << bits) | (value & ((1 << bits) - 1))
        self.n += bits

    def align(self) -> None:
        self.put(0, -self.n % 8)

    def bytes(self) -> bytes:
        pad = -self.n % 8
        return (self.v << pad).to_bytes((self.n + pad) // 8, "big")


class Builder:
    """Indirect objects (functions, shadings, patterns) and the resource entries naming them."""

    def __init__(self, r: random.Random, mode: str = "classic"):
        self.r, self.mode = r, mode
        self.objects: list[bytes] = []
        self.shadings: list[bytes] = []
        self.patterns: list[bytes] = []
        self.extgs: list[bytes] = []

    def add(self, b: bytes) -> bytes:
        self.objects.append(b)
        return b"%d 0 R" % len(self.objects)

    def resources(self) -> bytes:
        out = b""
        if self.shadings:
            out += b" /Shading << " + b" ".join(b"/Sh%d %s" % (i, s) for i, s in enumerate(self.shadings)) + b" >>"
        if self.patterns:
            out += b" /Pattern << " + b" ".join(b"/P%d %s" % (i, s) for i, s in enumerate(self.patterns)) + b" >>"
        if self.extgs:
            return out, b" " + b" ".join(b"/T%d %s" % (i, e) for i, e in enumerate(self.extgs))
        return out

    # ---- transfer functions (transfer mode)
    def transfer_value(self) -> bytes:
        """A /TR or /TR2 value: a function, an array of three, a name, or something PDFium ignores."""
        r = self.r
        k = r.random()
        if k < 0.45:
            return self.function(r.choice([1, 1, 1, 1, 2, 3, 17]))
        if k < 0.8:
            fs = [self.function(r.choice([1, 1, 1, 2])) for _ in range(3)]
            if r.random() < 0.1:
                fs[r.randrange(3)] = b"/Identity"
            if r.random() < 0.08:
                fs = fs[:2]
            if r.random() < 0.08:
                fs.append(self.function(1))
            return self.arr(fs)
        return r.choice([b"/Identity", b"/Default", b"null", b"3", b"[/Identity /Identity /Identity]",
                         b"<< /FunctionType 2 /Domain [0 1] /N 1 >>", b"<< /FunctionType 2 /Domain [0 1 0 1] /N 1 >>"])

    def smask_group(self) -> bytes:
        """A transparency group of plain fills for a soft mask."""
        r = self.r
        ops = []
        t0 = b""
        if r.random() < 0.3:
            t0 = b" /t0 << /TR %s >>" % self.function(1)
            ops.append(b"/t0 gs")
        for _ in range(r.randint(1, 4)):
            if r.random() < 0.3:
                ops.append(b"/a%d gs" % r.randrange(2))
            ops.append(r.choice([b"%.3f g" % r.random(), b"%.3f %.3f %.3f rg" % (r.random(), r.random(), r.random())]))
            ops.append(b"%s %s %s %s re f" % (num(r), num(r), num(r), num(r)))
        body = b"\n".join(ops)
        cs = r.choice([b"", b" /CS /DeviceRGB", b" /CS /DeviceGray"])
        return self.add(b"<< /Type /XObject /Subtype /Form /BBox [-50 -50 250 200] /Group << /S /Transparency%s >> "
                        b"/Resources << /ExtGState << /a0 << /ca 0.3 >> /a1 << /ca 0.75 >>%s >> >> /Length %d >>\n"
                        b"stream\n" % (cs, t0, len(body)) + body + b"\nendstream")

    def new_transfer_gs(self) -> int:
        r = self.r
        if self.extgs and r.random() < 0.3:
            return r.randrange(len(self.extgs))
        k = r.random()
        if k < 0.35:
            e = b"/TR " + self.transfer_value()
        elif k < 0.6:
            e = b"/TR2 " + self.transfer_value()
        elif k < 0.7:
            e = b"/TR %s /TR2 %s" % (self.transfer_value(), self.transfer_value())
        else:
            lum = r.random() < 0.6
            e = b"/SMask << /S /%s /G %s" % (b"Luminosity" if lum else b"Alpha", self.smask_group())
            if lum and r.random() < 0.4:
                e += b" /BC [%.3f %.3f %.3f]" % (r.random(), r.random(), r.random())
            if r.random() < 0.8:
                e += b" /TR " + (self.function(r.choice([1, 1, 1, 2])) if r.random() < 0.85
                                 else r.choice([b"/Identity", b"[/Identity]", b"1"]))
            e += b" >>"
            if r.random() < 0.3:
                e += b" /TR " + self.transfer_value()
        if r.random() < 0.2:
            e += b" /ca %.2f /CA %.2f" % (r.random(), r.random())
        self.extgs.append(b"<< " + e + b" >>")
        return len(self.extgs) - 1

    # ---- numbers
    def f(self, lo=0.0, hi=1.0) -> bytes:
        r = self.r
        if r.random() < 0.02:
            return r.choice([b"0", b"-0", b"0.0000001", b"100000", b"-100000", b"340000000000000000000000000000000000000",
                             b".5", b"-.0001", b"16777217", b"0.1", b"2", b"-3"])
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
            ds = [float(x) for x in dom[1:-1].split()] + [0.0, 1.0]
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
        self.cs_ranges = None
        if self.mode != "classic" and r.random() < (0.8 if self.mode == "cie" else 0.4):
            return self.cie_colorspace(0.3 if self.mode == "mesh" else 0.05)
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

    def cie_colorspace(self, indexed: float = 0.2) -> tuple[bytes, int]:
        """CalRGB, CalGray, Lab, Indexed, or Separation/DeviceN over a CIE space; `cs_ranges` is set
        to the components' ranges when they are not [0 1]."""
        r = self.r
        k = r.random()
        if k < indexed:
            base, n = r.choice([(b"/DeviceRGB", 3), (b"/DeviceRGB", 3), (b"/DeviceGray", 1), (b"/DeviceCMYK", 4),
                                None, None]) or self._cie_base()
            hival = r.choice([0, 1, 3, 7, 15, 255, r.randint(0, 40)])
            if r.random() < 0.05:
                hival = r.choice([-1, 256, 300])
            size = (max(hival, 0) + 1) * n
            if r.random() < 0.15:
                size = r.randrange(size + 1)
            table = bytes(r.randrange(256) for _ in range(size))
            if r.random() < 0.5:
                lookup = b"<" + table.hex().encode() + b">"
            else:
                lookup = self.add(b"<< /Length %d >>\nstream\n" % len(table) + table + b"\nendstream")
            self.cs_ranges = [(0, max(hival, 0))]
            return b"[/Indexed %s %d %s]" % (base, hival, lookup), 1
        if k < indexed + 0.2 * (1 - indexed):
            alt, n = self._cie_base()
            ranges = self.cs_ranges
            if r.random() < 0.6:
                fn = self.scaled_function(ranges, b"[0 1]") if ranges else self.function(n, 1, b"[0 1]")
                self.cs_ranges = None
                return b"[/Separation /Spot %s %s]" % (alt, fn), 1
            m = r.choice([1, 2, 3])
            names = b"[" + b" ".join(b"/C%d" % i for i in range(m)) + b"]"
            dom = b"[" + b" ".join([b"0 1"] * m) + b"]"
            rng = b"[" + b" ".join(b"%g %g" % rg for rg in (ranges or [(0, 1)] * n)) + b"]"
            size = r.choice([2, 3])
            data = bytes(r.randrange(256) for _ in range(size ** m * n))
            fn = self.add(b"<< /FunctionType 0 /Domain %s /Range %s /Size [%s] /BitsPerSample 8 /Length %d >>\nstream\n"
                          % (dom, rng, b" ".join([b"%d" % size] * m), len(data)) + data + b"\nendstream")
            self.cs_ranges = None
            return b"[/DeviceN %s %s %s]" % (names, alt, fn), m
        return self._cie_base()

    def _white(self) -> bytes:
        r = self.r
        w = r.choice([b"[0.9505 1 1.089]", b"[0.9505 1 1.089]", b"[0.9642 1 0.8249]", b"[1 1 1]",
                      b"[0.3127 1 0.329]", b"[2 1 0.5]"])
        if r.random() < 0.05:
            w = r.choice([b"[0.95 0.9 1.09]", b"[0 1 1]", b"[0.95 1]", b"[-1 1 1]"])
        return w

    def _cie_base(self) -> tuple[bytes, int]:
        r = self.r
        self.cs_ranges = None
        k = r.random()
        if k < 0.4:
            d = b"/WhitePoint " + self._white()
            if r.random() < 0.6:
                g = r.choice([[b"1", b"1", b"1"], [b"2.2", b"2.2", b"2.2"], [b"1.8", b"1.8", b"1.8"],
                              [self.f(0.3, 3) for _ in range(3)]])
                d += b" /Gamma [" + b" ".join(g) + b"]"
            if r.random() < 0.6:
                m = r.choice([[b"0.4124", b"0.2126", b"0.0193", b"0.3576", b"0.7152", b"0.1192", b"0.1805",
                               b"0.0722", b"0.9505"], [self.f(-0.2, 1) for _ in range(9)],
                              [b"1", b"0", b"0", b"0", b"1", b"0", b"0", b"0", b"1"]])
                d += b" /Matrix [" + b" ".join(m) + b"]"
            if r.random() < 0.2:
                d += b" /BlackPoint [0 0 0]"
            return b"[/CalRGB << %s >>]" % d, 3
        if k < 0.55:
            d = b"/WhitePoint " + self._white()
            if r.random() < 0.6:
                d += b" /Gamma " + r.choice([b"1", b"2.2", self.f(0.3, 3)])
            return b"[/CalGray << %s >>]" % d, 1
        d = b"/WhitePoint " + self._white()
        rng = (-100.0, 100.0, -100.0, 100.0)
        if r.random() < 0.5:
            rng = r.choice([(-128, 127, -128, 127), (-50, 50, -20, 80), (0, 0, -100, 100), (20, -20, 0, 50)])
            d += b" /Range [%g %g %g %g]" % rng
        self.cs_ranges = [(0, 100), (rng[0], rng[1]), (rng[2], rng[3])]
        return b"[/Lab << %s >>]" % d, 3

    def scaled_function(self, ranges, dom: bytes) -> bytes:
        """A 1-in function whose outputs span `ranges` (Lab's L*a*b*, an Indexed space's indices)."""
        r = self.r
        n = len(ranges)
        if r.random() < 0.6:
            c0 = [self.f(lo, hi) for lo, hi in ranges]
            c1 = [self.f(lo, hi) for lo, hi in ranges]
            nn = r.choice([b"1", b"1", b"2", b"0.5"])
            body = b"<< /FunctionType 2 /Domain %s /C0 %s /C1 %s /N %s >>" % (dom, self.arr(c0), self.arr(c1), nn)
            return self.add(body) if r.random() < 0.5 else body
        size = r.choice([2, 3, 5, 17])
        data = bytes(r.randrange(256) for _ in range(size * n))
        dec = b" ".join(b"%g %g" % rg for rg in ranges)
        return self.add(b"<< /FunctionType 0 /Domain %s /Range [%s] /Decode [%s] /Size [%d] /BitsPerSample 8 "
                        b"/Length %d >>\nstream\n" % (dom, dec, dec, size, len(data)) + data + b"\nendstream")

    def function2(self, n: int, dom: bytes) -> bytes:
        """A function of 2 inputs, n outputs (type 1 shadings)."""
        r = self.r
        k = r.random()
        if k < 0.45:
            size = [r.choice([1, 2, 3, 5, 9]) for _ in range(2)]
            bps = r.choice([1, 4, 8, 8, 12, 16])
            nbits = size[0] * size[1] * n * bps
            data = bytes(r.randrange(256) for _ in range((nbits + 7) // 8))
            extra = b""
            if r.random() < 0.3:
                extra += b" /Encode [0 %d %s %s]" % (size[0] - 1, self.f(0, size[1]), self.f(0, size[1]))
            ranges = self.cs_ranges or [(0, 1)] * n
            if len(ranges) != n:
                ranges = [(0, 1)] * n
            rng = b"[" + b" ".join(b"%g %g" % rg for rg in ranges) + b"]"
            if r.random() < 0.3 and self.cs_ranges is None:
                extra += b" /Decode " + self.arr([b"%s %s" % (self.f(), self.f()) for _ in range(n)])
            elif self.cs_ranges is not None:
                extra += b" /Decode " + rng
            return self.add(b"<< /FunctionType 0 /Domain %s /Range %s /Size [%d %d] /BitsPerSample %d%s /Length %d >>"
                            b"\nstream\n" % (dom, rng, size[0], size[1], bps, extra, len(data)) + data + b"\nendstream")
        if k < 0.95:
            # PostScript: stack x y; output j from both inputs, then the inputs rolled away
            prog = []
            scale = self.cs_ranges if self.cs_ranges and len(self.cs_ranges) == n else None
            for j in range(n):
                op = r.choice([b"add 0.5 mul", b"mul", b"sub abs", b"exch pop", b"pop",
                               b"dup mul exch dup mul add sqrt", b"2 copy gt { pop } { exch pop } ifelse"])
                prog.append(b"%d index %d index %s %s" % (1 + j, 1 + j, op, self.ps_expr() if r.random() < 0.5 else b""))
                if scale:
                    lo, hi = scale[j]
                    prog.append(b"%g mul %g add" % (hi - lo, lo))
            prog.append(b"%d %d roll pop pop" % (n + 2, n))
            text = b"{ " + b" ".join(prog) + b" }"
            rng = b" /Range [%s]" % b" ".join(b"%g %g" % rg for rg in (scale or [(0, 1)] * n))
            if r.random() < 0.2:
                rng = b""
            return self.add(b"<< /FunctionType 4 /Domain %s%s /Length %d >>\nstream\n" % (dom, rng, len(text))
                            + text + b"\nendstream")
        return self.function(n, 1, dom)        # 1-in: fails Validate

    def type1_shading(self, cs: bytes, n: int) -> bytes:
        r = self.r
        dom = r.choice([b"[0 1 0 1]", b"[0 1 0 1]", b"[-1 1 -1 1]", b"[0 2 0 0.5]", b"[0.25 0.75 0 1]", b"[1 0 0 1]"])
        body = b"<< /ShadingType 1 /ColorSpace %s" % cs
        if r.random() < 0.8:
            body += b" /Domain " + dom
        else:
            dom = b"[0 1 0 1]"
        if r.random() < 0.85:
            sx, sy = r.uniform(20, 200), r.uniform(20, 150)
            if r.random() < 0.3:
                vals = [r.uniform(-150, 150) for _ in range(4)]
            else:
                vals = [sx, 0, 0, sy]
            vals += [r.uniform(-30, 150), r.uniform(-30, 120)]
            body += b" /Matrix [" + b" ".join(b"%.4f" % v for v in vals) + b"]"
        if n > 1 and r.random() < 0.3:
            ranges, self.cs_ranges = self.cs_ranges, None
            fs = []
            for j in range(n):
                self.cs_ranges = [ranges[j]] if ranges else None
                fs.append(self.function2(1, dom))
            self.cs_ranges = ranges
            body += b" /Function [%s]" % b" ".join(fs)
        else:
            body += b" /Function %s" % self.function2(n, dom)
        return body

    def mesh_shading(self, cs: bytes, n: int) -> tuple[bytes, bytes]:
        """(dictionary entries, stream data) of a type 4-7 shading."""
        r = self.r
        stype = r.choice([4, 4, 5, 5, 6, 6, 7])
        indexed = cs.startswith(b"[/Indexed")
        funcs = r.random() < (0.05 if indexed else 0.4)
        comps = 1 if funcs else n
        bpc = r.choice([8, 8, 16, 16, 32, 24, 12, 4, 2, 1])
        bpcomp = r.choice([8, 8, 16, 4, 12, 2, 1])
        bpf = r.choice([8, 8, 2, 4])
        x0, x1 = r.uniform(-60, 60), r.uniform(140, 260)
        y0, y1 = r.uniform(-60, 40), r.uniform(110, 210)
        if r.random() < 0.1:
            x0, x1 = x1, x0
        if funcs:
            cr = [r.choice([(0, 1), (0, 1), (-1, 2), (1, 0), (0.25, 0.5)])]
        elif self.cs_ranges and len(self.cs_ranges) == comps:
            cr = list(self.cs_ranges)
        else:
            cr = [r.choice([(0, 1), (0, 1), (0, 1), (1, 0), (-0.5, 1.5)]) for _ in range(comps)]
        decode = [x0, x1, y0, y1] + [v for rg in cr for v in rg]
        w = _Bits()
        cmax, kmax = (1 << bpc) - 1, (1 << bpcomp) - 1

        def coord(x, y):
            for v, lo, hi in ((x, x0, x1), (y, y0, y1)):
                t = (v - lo) / (hi - lo) if hi != lo else 0
                w.put(max(0, min(cmax, int(round(t * cmax)))), bpc)

        def color():
            for _ in range(comps):
                w.put(r.randrange(kmax + 1), bpcomp)

        def point(cx, cy, spread):
            return cx + r.uniform(-spread, spread), cy + r.uniform(-spread, spread)
        spread = r.choice([5, 20, 60, 150])
        entries = b""
        if stype == 4:
            for i in range(r.randint(1, 6)):
                flag = 0 if i == 0 or r.random() < 0.3 else r.choice([1, 2, 3])
                cx, cy = r.uniform(0, 200), r.uniform(0, 150)
                for j in range(3 if flag == 0 else 1):
                    w.put(flag if j == 0 else r.randrange(4), bpf)
                    coord(*point(cx, cy, spread))
                    color()
                    w.align()
        elif stype == 5:
            per_row, rows = r.randint(2, 5), r.randint(2, 4)
            gx, gy = r.uniform(-20, 100), r.uniform(-20, 80)
            sx, sy = r.uniform(10, 80), r.uniform(10, 60)
            for i in range(rows):
                for j in range(per_row):
                    coord(*point(gx + j * sx, gy + i * sy, spread / 8))
                    color()
                    w.align()
            if r.random() < 0.1:
                per_row = r.choice([0, 1, -3, 7])
            entries += b" /VerticesPerRow %d" % per_row
        else:
            count = 16 if stype == 7 else 12
            order = [(0, 0), (0, 1), (0, 2), (0, 3), (1, 3), (2, 3), (3, 3), (3, 2), (3, 1), (3, 0), (2, 0), (1, 0),
                     (1, 1), (1, 2), (2, 2), (2, 1)][:count]
            for i in range(r.randint(1, 3)):
                flag = 0 if i == 0 or r.random() < 0.4 else r.choice([1, 2, 3])
                gx, gy = r.uniform(-20, 150), r.uniform(-20, 110)
                sx, sy = r.uniform(5, 40), r.uniform(5, 30)
                jit = r.choice([0, 1, 5, 20])
                w.put(flag, bpf)
                for (a, b) in order[4 if flag else 0:]:
                    coord(*point(gx + b * sx, gy + a * sy, jit))
                for _ in range(2 if flag else 4):
                    color()
        data = w.bytes()
        if r.random() < 0.1:
            data = data[:r.randrange(len(data) + 1)]
        if r.random() < 0.05:
            data += bytes(r.randrange(256) for _ in range(r.randint(1, 6)))
        if r.random() < 0.05:
            bpc = r.choice([0, 3, 64])
        if r.random() < 0.03:
            decode = decode[:-1]
        entries = (b"/ShadingType %d /ColorSpace %s /BitsPerCoordinate %d /BitsPerComponent %d /BitsPerFlag %d"
                   b" /Decode [%s]" % (stype, cs, bpc, bpcomp, bpf, b" ".join(b"%.4f" % v for v in decode))) + entries
        if funcs:
            fdom = b"[%g %g]" % cr[0]
            if n > 1 and r.random() < 0.3:
                entries += b" /Function [%s]" % b" ".join(self.function(1, 1, fdom) for _ in range(n))
            elif self.cs_ranges and len(self.cs_ranges) == n and not indexed:
                entries += b" /Function %s" % self.scaled_function(self.cs_ranges, fdom)
            else:
                entries += b" /Function %s" % self.function(n, 0, fdom)
        return entries, data

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
        if self.mode != "classic":
            other = {"func": 0.85, "mesh": 0.9, "cie": 0.3}.get(self.mode, 0.0)
            if r.random() < other:
                return self.new_style_shading(pattern)
        stype = r.choice([2, 2, 3, 3, 3])
        cs, n = self.colorspace()
        if self.cs_ranges is not None and not cs.startswith(b"[/Indexed") and r.random() < 0.8:
            return self._scaled_axial(stype, cs, pattern)
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
            if r.random() < 0.12:
                # a shrinking circle whose radius falls by more than the whole part of the distance
                # its centre moves, less than the distance: PDFium truncates the distance
                cx, cy, dist, ang = r.uniform(20, 180), r.uniform(20, 130), r.uniform(1, 40), r.uniform(0, 6.3)
                end = r.uniform(0, 30)
                x0, y0, x1, y1 = (b"%.4f" % v for v in (cx, cy, cx + dist * np.cos(ang), cy + dist * np.sin(ang)))
                r0, r1 = b"%.4f" % (end + r.uniform(np.floor(dist), dist)), b"%.4f" % end
            coords = [x0, y0, r0, x1, y1, r1]
        wild = r.random() < 0.15
        if wild and r.random() < 0.2:
            coords = coords[:r.randrange(len(coords))]
        if wild and r.random() < 0.1:
            cs, n = r.choice([(b"[/Indexed /DeviceRGB 1 <FF000000FF00>]", 1), (b"[/Lab << /WhitePoint [0.95 1 1.09] >>]", 3),
                              (b"[/CalRGB << /WhitePoint [0.95 1 1.09] >>]", 3), (b"/Pattern", 1)])
        body = b"<< /ShadingType %d /ColorSpace %s /Coords %s" % (stype, cs, self.arr(coords))
        if r.random() < 0.7:
            body += b" /Domain " + dom
        else:
            d0, d1 = 0, 1
        if r.random() < 0.7:
            body += b" /Extend [%s %s]" % (r.choice([b"true", b"false"]), r.choice([b"true", b"false"]))
        elif wild and r.random() < 0.3:
            body += r.choice([b" /Extend [true]", b" /Extend [1 1]", b" /Extend true"])
        fdom = b"[%g %g]" % (d0, d1) if r.random() < 0.8 else b"[0 1]"
        if wild and r.random() < 0.15:
            fdom = r.choice([b"[1 0]", b"[0]", b"[0 0]", b"[0 1 0 1]"])
        outs = n
        if wild and r.random() < 0.1:
            outs = r.choice([1, 2, 3, 4])
        if outs > 1 and r.random() < 0.3:
            body += b" /Function [%s]" % b" ".join(self.function(1, 1, fdom) for _ in range(outs))
        else:
            body += b" /Function %s" % self.function(outs, 0, fdom)
        if r.random() < 0.25:
            k = n - 1 if wild and r.random() < 0.3 else n
            body += b" /Background " + self.arr([self.f() for _ in range(k)])
        if r.random() < 0.25:
            k = 3 if wild and r.random() < 0.3 else 4
            body += b" /BBox " + self.arr([self.f(-20, 220) for _ in range(k)])
        if r.random() < 0.2:
            body += b" /AntiAlias true"
        body += b" >>"
        return self.add(body) if pattern or r.random() < 0.7 else body

    def _extras(self, n: int) -> bytes:
        r = self.r
        body = b""
        if r.random() < 0.2:
            body += b" /Background " + self.arr([self.f() for _ in range(n)])
        if r.random() < 0.2:
            body += b" /BBox " + self.arr([self.f(-20, 220) for _ in range(4)])
        return body

    def _scaled_axial(self, stype: int, cs: bytes, pattern: bool) -> bytes:
        """An axial or radial shading over Lab (functions spanning L*a*b*)."""
        r = self.r
        if stype == 2:
            coords = [self.f(-40, 240) for _ in range(4)]
        else:
            coords = [self.f(-20, 220), self.f(-20, 220), self.f(0, 60), self.f(-20, 220), self.f(-20, 220),
                      self.f(0, 120)]
        body = b"<< /ShadingType %d /ColorSpace %s /Coords %s" % (stype, cs, self.arr(coords))
        if r.random() < 0.6:
            body += b" /Extend [%s %s]" % (r.choice([b"true", b"false"]), r.choice([b"true", b"false"]))
        body += b" /Function %s" % self.scaled_function(self.cs_ranges, b"[0 1]") + self._extras(3) + b" >>"
        return self.add(body) if pattern or r.random() < 0.7 else body

    def new_style_shading(self, pattern: bool) -> bytes:
        """A function-based (type 1) or mesh (4-7) shading."""
        r = self.r
        cs, n = self.colorspace()
        mesh = self.mode == "mesh" or (self.mode == "cie" and r.random() < 0.7)
        if not mesh:
            body = self.type1_shading(cs, n) + self._extras(n) + b" >>"
            return self.add(body) if pattern or r.random() < 0.7 else body
        entries, data = self.mesh_shading(cs, n)
        entries += self._extras(n)
        if r.random() < 0.03:
            return entries.join([b"<< ", b" >>"])        # a dictionary: never drawn
        return self.add(b"<< %s /Length %d >>\nstream\n" % (entries, len(data)) + data + b"\nendstream")

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
    if b.mode == "transfer":
        if r.random() < 0.75:
            g.append(b"/T%d gs" % b.new_transfer_gs())
        if r.random() < 0.6:          # plain colours are what a transfer function changes
            for _ in range(r.randint(1, 3)):
                col = r.choice([b"%.3f %.3f %.3f" % (r.random(), r.random(), r.random()), b"%.3f" % r.random(),
                                b"%.3f %.3f %.3f %.3f" % (r.random(), r.random(), r.random(), r.random())])
                op = {3: (b"rg", b"RG"), 1: (b"g", b"G"), 4: (b"k", b"K")}[len(col.split())]
                if r.random() < 0.3:
                    g.append(b"%s %s %d w" % (col, op[1], r.randint(1, 6)))
                    g.append(random_path(r) + b" " + r.choice([b"S", b"B", b"b*"]))
                else:
                    g.append(b"%s %s" % (col, op[0]))
                    g.append(random_path(r) + b" " + r.choice([b"f", b"f*"]))
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


MODES = ("classic", "cie", "func", "mesh", "transfer")


def case(seed: int, mode: str = "classic"):
    """(content, forms, objects, resources, zoom, transparent) for `seed`. `mode`: classic (axial and
    radial), cie (CalRGB, CalGray, Lab, Indexed), func (type 1), mesh (types 4-7), transfer (/TR)."""
    r = random.Random(seed if mode == "classic" else f"{mode}:{seed}")
    b = Builder(r, mode)
    forms = []
    for k in range(r.choice([0, 0, 0, 1, 2])):
        entries = b"/BBox [%s %s %s %s]" % (num(r), num(r), num(r), num(r))
        if r.random() < 0.7:
            m = [r.choice([1, 0, -1, r.uniform(-2, 2)]) for _ in range(4)] + [r.uniform(-60, 60), r.uniform(-60, 60)]
            entries += b" /Matrix [" + b" ".join(b"%.4f" % v for v in m) + b"]"
        if r.random() < 0.25:
            entries += r.choice([b" /Group << /S /Transparency >>", b" /Group << /S /Transparency /I true >>",
                                 b" /Group << /S /Transparency /K true >>"])
        forms.append((entries, b"\n".join(random_group(r, b, k) for _ in range(r.randint(1, 3)))))
    content = b"\n".join(random_group(r, b, len(forms)) for _ in range(r.randint(1, 4)))
    zoom = r.choice([0.5, 1, 1.37, 2, 3.1])
    if mode == "func":
        zoom = min(zoom, 1.37)          # a Python function call per pixel
    return content, forms, b.objects, b.resources(), zoom, r.random() < 0.3


def compare(content: bytes, objects, resources, zoom: float, transparent: bool, forms=()):
    """(pixels that differ or None when the pure reader refuses, PDFium's render, pure's render,
    per-pixel difference or the refusal)."""
    from ..pdf.api import PdfError
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    data = pdf_bytes([content], objects, resources, forms=forms)
    ref, pure = PdfiumBackend().open(data), PureBackend().open(data)
    try:
        a = ref[0].render(zoom, transparent=transparent)
        try:
            b = pure[0].render(zoom, transparent=transparent)
        except PdfError as e:
            return None, a, None, str(e)
    finally:
        ref.close()
        pure.close()
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


def run(seed0: int, n: int, out: Path | None = None, verbose: bool = True, mode: str = "classic") -> dict:
    """{'failed': [seeds], 'refused': {reason: count}, 'drawn': count}."""
    stats = {"failed": [], "refused": {}, "drawn": 0}
    for seed in range(seed0, seed0 + n):
        content, forms, objects, resources, zoom, transparent = case(seed, mode)
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
            if out is not None:     # what another platform's PDFium drew, to study where it is
                out.mkdir(parents=True, exist_ok=True)
                (out / f"{mode}-{seed}.pdf").write_bytes(pdf_bytes([content], objects, resources, forms=forms))
                np.savez_compressed(out / f"{mode}-{seed}.npz", pdfium=a, pure=b, zoom=zoom,
                                    transparent=transparent)
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
    ap.add_argument("--mode", choices=MODES, default="classic")
    ap.add_argument("--quiet", action="store_true", help="no shrinking, only the counts")
    args = ap.parse_args(argv)
    stats = run(args.seed0, args.n, Path(args.out), not args.quiet, args.mode)
    print(f"{args.mode} seeds {args.seed0}..{args.seed0 + args.n - 1}: {stats['drawn']} exact, "
          f"{len(stats['failed'])} failed {stats['failed']}, refused {stats['refused']}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
