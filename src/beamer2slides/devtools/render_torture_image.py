"""Random torture PDFs for the pure renderer's images (`pdf/pure/render_image.py`,
`decode_image.py`): image XObjects and inline images of 1 to 16 bits per component in DeviceGray,
DeviceRGB, DeviceCMYK, Indexed and ICCBased (read through its alternate), stored raw or through
Flate (with PNG and TIFF predictors), DCT, RunLength, ASCIIHex and ASCII85, with /Decode arrays,
/ImageMask stencils, colour-key /Mask arrays, /Mask streams, /SMask (with /Matte) and /Interpolate,
drawn upright, flipped, scaled up and down and turned by quarter turns, under clips and constant
alpha. Each page is rendered by PDFium and by the pure reader; any pixel that differs is a failure,
shrunk to the lines of the page that still make it differ. A page the pure reader refuses is
counted, not failed, and listed with its reason.

    python tools/render_torture_image.py [seed0] [n] [--out DIR]

Failures go to DIR as seedN.pdf (the shrunk page) and seedN.png (PDFium | pure | difference).
The page layout is `render_torture_shading.pdf_bytes` (the image objects in front)."""

from __future__ import annotations

import argparse
import base64
import io
import random
import zlib
from pathlib import Path

import numpy as np

from .render_torture_shading import pdf_bytes

JUNK_PROFILE = b"not an ICC profile"


def _hex(b: bytes) -> bytes:
    return b.hex().encode() + b">"


def _rl(b: bytes, r: random.Random) -> bytes:
    out = bytearray()
    i = 0
    while i < len(b):
        run = 1
        while i + run < len(b) and run < 128 and b[i + run] == b[i]:
            run += 1
        if run >= 2 and r.random() < 0.8:
            out += bytes([257 - run, b[i]])
            i += run
            continue
        n = min(len(b) - i, r.randint(1, 128))
        out += bytes([n - 1]) + b[i:i + n]
        i += n
    return bytes(out) + b"\x80"


def _png(rows: np.ndarray, bpp: int, r: random.Random) -> bytes:
    """PNG-predict the rows (random filter per row)."""
    out = bytearray()
    prev = np.zeros(rows.shape[1], np.int64)
    for row in rows.astype(np.int64):
        tag = r.randint(0, 4)
        left = np.concatenate([np.zeros(bpp, np.int64), row[:-bpp]]) if len(row) > bpp else np.zeros(len(row), np.int64)
        ul = np.concatenate([np.zeros(bpp, np.int64), prev[:-bpp]]) if len(row) > bpp else np.zeros(len(row), np.int64)
        if tag == 0:
            f = row
        elif tag == 1:
            f = row - left
        elif tag == 2:
            f = row - prev
        elif tag == 3:
            f = row - (left + prev) // 2
        else:
            p = left + prev - ul
            pa, pb, pc = abs(p - left), abs(p - prev), abs(p - ul)
            f = row - np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, prev, ul))
        out += bytes([tag]) + (f & 255).astype(np.uint8).tobytes()
        prev = row
    return bytes(out)


class Builder:
    def __init__(self, r: random.Random):
        self.r = r
        self.objects: list[bytes] = []
        self.names: list[bytes] = []

    def add(self, b: bytes) -> int:
        self.objects.append(b)
        return len(self.objects)

    def stream(self, entries: bytes, data: bytes) -> int:
        return self.add(b"<< %s /Length %d >>\nstream\n" % (entries, len(data)) + data + b"\nendstream")

    def resources(self) -> bytes:
        return b""

    # ---- pixel data
    def pixels(self, w: int, h: int, comps: int, bpc: int) -> bytes:
        r = self.r
        pitch = (w * comps * bpc + 7) // 8
        kind = r.random()
        if kind < 0.5:
            data = bytes(r.getrandbits(8) for _ in range(pitch * h))
        else:   # smooth: gradients survive JPEG and make the resampling visible
            y, x = np.mgrid[0:h, 0:pitch]
            a, b, c = r.uniform(-9, 9), r.uniform(-9, 9), r.randint(0, 255)
            data = ((x * a + y * b + c) % 256).astype(np.uint8).tobytes()
        if r.random() < 0.1:   # truncated data
            data = data[:r.randint(0, len(data))]
        return data

    def encode(self, data: bytes, w: int, h: int, comps: int, bpc: int, jpeg_ok: bool):
        """(filter entries, encoded data)."""
        r = self.r
        pitch = (w * comps * bpc + 7) // 8
        k = r.random()
        if jpeg_ok and k < 0.2:
            from PIL import Image
            full = data + bytes(max(0, pitch * h - len(data)))
            img = Image.frombytes("L" if comps == 1 else "RGB", (w, h), full[:pitch * h])
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=r.choice([30, 75, 95]), subsampling=r.choice([0, 1, 2]))
            return b"/Filter /DCTDecode", buf.getvalue()
        if k < 0.45:
            return b"/Filter /FlateDecode", zlib.compress(data)
        if k < 0.6 and len(data) == pitch * h and h > 0:
            rows = np.frombuffer(data, np.uint8).reshape(h, pitch)
            if r.random() < 0.6:
                bpp = (comps * bpc + 7) // 8
                parms = b"/DecodeParms << /Predictor %d /Colors %d /BitsPerComponent %d /Columns %d >>" % (
                    r.randint(10, 15), comps, bpc, w)
                return b"/Filter /FlateDecode " + parms, zlib.compress(_png(rows, bpp, r))
            if bpc == 8:
                px = rows.reshape(h, w, comps).astype(np.int64)
                diff = px.copy()
                diff[:, 1:] = px[:, 1:] - px[:, :-1]
                parms = b"/DecodeParms << /Predictor 2 /Colors %d /Columns %d >>" % (comps, w)
                return b"/Filter /FlateDecode " + parms, zlib.compress((diff & 255).astype(np.uint8).tobytes())
        if k < 0.7:
            return b"/Filter /ASCIIHexDecode", _hex(data)
        if k < 0.78:
            return b"/Filter /ASCII85Decode", base64.a85encode(data) + b"~>"
        if k < 0.86:
            return b"/Filter /RunLengthDecode", _rl(data, r)
        if k < 0.9:
            return b"/Filter [/ASCIIHexDecode /FlateDecode]", _hex(zlib.compress(data))
        return b"", data

    def colorspace(self):
        """(entry, components, is indexed)"""
        r = self.r
        k = r.random()
        if k < 0.3:
            return b"/DeviceGray", 1, False
        if k < 0.6:
            return b"/DeviceRGB", 3, False
        if k < 0.68:
            return b"/DeviceCMYK", 4, False
        if k < 0.85:
            base, n = r.choice([(b"/DeviceGray", 1), (b"/DeviceRGB", 3)])
            hival = r.randint(0, 255)
            size = (hival + 1) * n if r.random() < 0.8 else r.randint(0, (hival + 1) * n)
            lookup = bytes(r.getrandbits(8) for _ in range(size))
            if r.random() < 0.5:
                return b"[/Indexed %s %d <%s>]" % (base, hival, lookup.hex().encode()), 1, True
            sid = self.stream(b"", lookup)
            return b"[/Indexed %s %d %d 0 R]" % (base, hival, sid), 1, True
        n = r.choice([1, 3])
        alt = b" /Alternate %s" % (b"/DeviceGray" if n == 1 else b"/DeviceRGB") if r.random() < 0.5 else b""
        sid = self.stream(b"/N %d%s" % (n, alt), JUNK_PROFILE)
        return b"[/ICCBased %d 0 R]" % sid, n, False

    def image(self, inline: bool):
        """(dictionary entries without /Length, data) of a random image."""
        r = self.r
        w, h = r.randint(1, 24), r.randint(1, 24)
        if r.random() < 0.15:
            w, h = r.randint(30, 90), r.randint(30, 90)
        extra = b""
        if r.random() < 0.12:
            data = self.pixels(w, h, 1, 1)
            filt, enc = self.encode(data, w, h, 1, 1, False)
            dec = r.choice([b"", b"", b" /Decode [1 0]", b" /Decode [0 1]"])
            return b"/Width %d /Height %d /ImageMask true%s %s" % (w, h, dec, filt), enc, True
        cs, comps, indexed = self.colorspace()
        bpc = r.choice([1, 2, 4, 8, 8, 8, 16])
        if comps == 4 and bpc != 8:
            bpc = 8
        data = self.pixels(w, h, comps, bpc)
        jpeg_ok = bpc == 8 and comps in (1, 3) and not indexed and len(data) == ((w * comps * 8 + 7) // 8) * h
        filt, enc = self.encode(data, w, h, comps, bpc, jpeg_ok)
        if r.random() < 0.2:
            vals = []
            for _ in range(comps):
                if indexed:
                    vals += [r.randint(0, 3), r.randint(0, (1 << bpc) - 1)]
                else:
                    vals += [r.choice([0, 1, 0.25, r.uniform(-0.5, 1.5)]), r.choice([1, 0, 0.75, r.uniform(-0.5, 1.5)])]
            extra += b" /Decode [" + b" ".join(b"%.4g" % v for v in vals) + b"]"
        if r.random() < 0.15:
            mx = (1 << bpc) - 1
            vals = []
            for _ in range(comps):
                a = r.randint(0, mx)
                vals += [a, min(mx, a + r.randint(0, max(1, mx // 3)))]
            if r.random() < 0.2:
                vals = vals[:r.randint(0, len(vals))]
            extra += b" /Mask [" + b" ".join(b"%d" % v for v in vals) + b"]"
        elif not inline and r.random() < 0.25:
            mw, mh = (w, h) if r.random() < 0.5 else (r.randint(1, 30), r.randint(1, 30))
            mbpc = r.choice([8, 8, 1, 4])
            mdata = self.pixels(mw, mh, 1, mbpc)
            mfilt, menc = self.encode(mdata, mw, mh, 1, mbpc, mbpc == 8)
            matte = b""
            if r.random() < 0.3 and not indexed and comps != 4:
                matte = b" /Matte [" + b" ".join(b"%.3g" % r.random() for _ in range(comps)) + b"]"
            if r.random() < 0.3:
                mid = self.stream(b"/Width %d /Height %d /ImageMask true %s" % (mw, mh, mfilt),
                                  self.encode(self.pixels(mw, mh, 1, 1), mw, mh, 1, 1, False)[1]
                                  if not mfilt else menc) if False else None
            mid = self.stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                              b"/BitsPerComponent %d %s%s" % (mw, mh, mbpc, mfilt, matte), menc)
            extra += b" /SMask %d 0 R" % mid
        elif not inline and r.random() < 0.1:
            mw, mh = r.randint(1, 30), r.randint(1, 30)
            mdata = self.pixels(mw, mh, 1, 1)
            mfilt, menc = self.encode(mdata, mw, mh, 1, 1, False)
            mid = self.stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ImageMask true %s"
                              % (mw, mh, mfilt), menc)
            extra += b" /Mask %d 0 R" % mid
        if r.random() < 0.2:
            extra += b" /Interpolate true"
        return (b"/Width %d /Height %d /ColorSpace %s /BitsPerComponent %d%s %s"
                % (w, h, cs, bpc, extra, filt)), enc, False

    def draw(self) -> bytes:
        r = self.r
        inline = r.random() < 0.25
        entries, data, stencil = self.image(inline)
        ops = [b"q"]
        if stencil or r.random() < 0.3:
            ops.append(b"%.3f %.3f %.3f rg" % (r.random(), r.random(), r.random()))
        if r.random() < 0.25:
            ops.append(r.choice([b"/GS1 gs", b"/GS2 gs", b"/GS3 gs"]))
        if r.random() < 0.3:
            x, y = r.uniform(0, 150), r.uniform(0, 100)
            ops.append(b"%.2f %.2f %.2f %.2f re W n" % (x, y, r.uniform(5, 120), r.uniform(5, 100)))
        if r.random() < 0.15:
            ops.append(b"%.1f %.1f m %.1f %.1f l %.1f %.1f l h W n" % tuple(r.uniform(0, 200) for _ in range(6)))
        sx = r.choice([1, 1, -1]) * r.choice([r.uniform(2, 60), r.uniform(60, 190), r.uniform(0.3, 4)])
        sy = r.choice([1, 1, -1]) * r.choice([r.uniform(2, 60), r.uniform(60, 140), r.uniform(0.3, 4)])
        tx, ty = r.uniform(-20, 190), r.uniform(-20, 140)
        k = r.random()
        if k < 0.6:
            m = (sx, 0, 0, sy, tx, ty)
        elif k < 0.9:
            m = (0, sy, sx, 0, tx, ty)
        else:
            m = (sx, r.uniform(-3, 3), r.uniform(-3, 3), sy, tx, ty)
        ops.append(b"%.4f %.4f %.4f %.4f %.3f %.3f cm" % m)
        if inline:
            abbrev = entries.replace(b"/Width", b"/W").replace(b"/Height", b"/H").replace(
                b"/ColorSpace", b"/CS").replace(b"/BitsPerComponent", b"/BPC").replace(b"/Filter", b"/F")
            if b"/DecodeParms" in abbrev or b" R" in abbrev or b"/DCTDecode" in abbrev:
                name = b"/Im%d" % len(self.names)
                self.names.append(self.stream(b"/Type /XObject /Subtype /Image " + entries, data))
                ops.append(name + b" Do")
            else:
                if b"/ASCII" not in abbrev and len(data) and r.random() < 0.5:
                    entries_hex = abbrev.replace(b"/F ", b"/F [/AHx ") if False else None
                ops.append(b"BI " + abbrev + b" ID " + data + b"\nEI")
        else:
            name = b"/Im%d" % len(self.names)
            self.names.append(self.stream(b"/Type /XObject /Subtype /Image " + entries, data))
            ops.append(name + b" Do")
        ops.append(b"Q")
        return b"\n".join(ops)


EXTGS_IMAGE = b" /ExtGState << /GS1 << /ca 0.5 >> /GS2 << /ca 0.83 >> /GS3 << /ca 0.2 /CA 0.7 >> >>"


def case(seed: int):
    """(content, objects, resources, zoom, transparent) for `seed`."""
    r = random.Random(seed)
    b = Builder(r)
    content = b"\n".join(b.draw() for _ in range(r.randint(1, 3)))
    res = b" /XObject << " + b" ".join(b"/Im%d %d 0 R" % (i, n) for i, n in enumerate(b.names)) + b" >>"
    return content, b.objects, res, r.choice([0.5, 1, 1.37, 2, 3.1]), r.random() < 0.3


def _pdf(content, objects, resources):
    # the shared layout adds /XObject for forms; images go in their own dictionary key
    data = pdf_bytes([content], objects, b"")
    return data.replace(b"/XObject << ", b"/XObject << " + resources[len(b" /XObject << "):-3] + b" ", 1) \
        .replace(b"<< /ExtGState", b"<< " + EXTGS_IMAGE[1:].split(b" /GS1")[0], 0) if False else _pdf2(content, objects, resources)


def _pdf2(content, objects, resources):
    import re
    data = pdf_bytes([content], objects, b"")
    names = resources[len(b" /XObject << "):-3]
    data = data.replace(b"/XObject <<  >>", b"/XObject << " + names + b" >>" + EXTGS_IMAGE)
    # fix the xref offsets after the edit
    return _reindex(data)


def _reindex(data: bytes) -> bytes:
    import re
    body_end = data.rfind(b"xref\n")
    body = data[:body_end]
    offs = [m.start() for m in re.finditer(rb"(?m)^\d+ 0 obj\n", body)]
    out = bytearray(body)
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(offs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    trailer = data[data.find(b"trailer", body_end):]
    trailer = re.sub(rb"startxref\n\d+", b"startxref\n%d" % x, trailer)
    return bytes(out) + trailer


def compare(content: bytes, objects, resources, zoom: float, transparent: bool):
    """(pixels that differ or None when the pure reader refuses, PDFium's render, pure's render,
    per-pixel difference or the refusal)."""
    from ..pdf.api import PdfError
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    data = _pdf2(content, objects, resources)
    a = PdfiumBackend().open(data)[0].render(zoom, transparent=transparent)
    try:
        b = PureBackend().open(data)[0].render(zoom, transparent=transparent)
    except PdfError as e:
        return None, a, None, str(e)
    d = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return int((d > 0).sum()), a, b, d


def shrink(content: bytes, objects, resources, zoom: float, transparent: bool):
    """Drop lines of the page while the difference remains."""
    def fails(c):
        try:
            return (compare(c, objects, resources, zoom, transparent)[0] or 0) > 0
        except Exception:
            return False
    lines = content.split(b"\n")
    changed = True
    while changed:
        changed = False
        for i in range(len(lines)):
            if lines[i] in (b"q", b"Q") or lines[i].startswith(b"BI "):
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
        content, objects, resources, zoom, transparent = case(seed)
        try:
            npx, a, b, d = compare(content, objects, resources, zoom, transparent)
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
        small = shrink(content, objects, resources, zoom, transparent)
        npx, a, b, d = compare(small, objects, resources, zoom, transparent)
        print(f"seed {seed} zoom {zoom} transparent {transparent}: {npx} px, max {d.max() if npx else 0}")
        print("-- page\n" + small.decode("latin-1")[:3000])
        if out is not None and npx:
            out.mkdir(parents=True, exist_ok=True)
            from PIL import Image
            vis = np.concatenate([a[..., :3], b[..., :3], np.stack([np.where(d > 0, 255, 0)] * 3, -1)], 1)
            Image.fromarray(vis.astype(np.uint8)).save(out / f"seed{seed}.png")
            (out / f"seed{seed}.pdf").write_bytes(_pdf2(small, objects, resources))
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seed0", type=int, nargs="?", default=0)
    ap.add_argument("n", type=int, nargs="?", default=200)
    ap.add_argument("--out", default="out/render-torture-image")
    args = ap.parse_args(argv)
    stats = run(args.seed0, args.n, Path(args.out))
    print(f"seeds {args.seed0}..{args.seed0 + args.n - 1}: {stats['drawn']} exact, "
          f"{len(stats['failed'])} failed, refused {stats['refused']}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
