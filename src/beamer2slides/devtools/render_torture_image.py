"""Random torture PDFs for the pure renderer's images (`pdf/pure/render_image.py`,
`decode_image.py`): image XObjects and inline images of 1 to 16 bits per component in DeviceGray,
DeviceRGB, DeviceCMYK, Indexed and ICCBased (read through its alternate), stored raw or through
Flate (with PNG and TIFF predictors), DCT, RunLength, ASCIIHex and ASCII85, with /Decode arrays,
/ImageMask stencils, colour-key /Mask arrays, /Mask streams, /SMask (with /Matte) and /Interpolate,
drawn upright, flipped, scaled up and down and turned by quarter turns or any angle, under clips and constant
alpha. Each page is rendered by PDFium and by the pure reader; any pixel that differs is a failure,
shrunk to the lines of the page that still make it differ. A page the pure reader refuses is
counted, not failed, and listed with its reason.

    python tools/render_torture_image.py [seed0] [n] [--level 0..8] [--out DIR]

`--level` grows the generator one class of images at a time: 0 = one upright 8-bit RGB Flate
image; 1 = scaled and flipped, several per page, on clear bitmaps too; 2 = quarter turns, skews,
clips and constant alpha; 3 = every colour space and bit depth; 4 = filters, predictors, /Decode,
masks, inline images, truncated data; 5 = CMYK at every bit depth, Indexed over CMYK, CMYK mattes
and fill overprint; 6 = everything, images turned by any angle and skewed
(CFX_ImageTransformer) and CMYK JPEGs too; 7 = ICCBased spaces whose profile PDFium
detects as sRGB (`SRGB_PROFILE`), with the low bit depths and wide /Decode arrays that make its
unclamped GetRGB show in the palette; 8 (the default) = images drawn inside a soft mask's group
(luminosity and alpha, every group /CS with and without /BC), where LoadSMask's SetStdCS makes a
CMYK palette the naive 1 - min(1, c + k) and a DeviceCMYK group family makes a DeviceCMYK image's
lines TransMask's (1-c)(1-k) (levels below keep their seeds).
Failures go to DIR as seedN.pdf (the shrunk page) and seedN.png (PDFium | pure | difference)."""

from __future__ import annotations

import argparse
import base64
import io
import math
import random
import zlib
from pathlib import Path

import numpy as np

from .render_torture import EXTGS

MEDIA = (0, 0, 200, 150)
# render_torture's ExtGStates plus overprint ones (PDFium draws a CMYK image under fill overprint
# with /OPM 0 in Darken)
IMAGE_EXTGS = EXTGS[:-2] + (b"/OP0 << /OP true >> /OP1 << /op true /OPM 1 >> /OP2 << /OP true /op false >> "
                            b"/OP3 << /op true /OPM 0 >> >>")
JUNK_PROFILE = b"not an ICC profile"


def _srgb_profile() -> bytes:
    """A made-up ICC profile PDFium detects as sRGB: DetectSRGB looks at nothing but the length
    (3144) and the description at offset 400. It carries no `acsp` signature, so under any /N but 3
    it is data lcms cannot open and both readers fall back to the alternate space."""
    b = bytearray(3144)
    b[0:4] = (3144).to_bytes(4, "big")
    b[16:20] = b"RGB "
    b[20:24] = b"XYZ "
    b[400:417] = b"sRGB IEC61966-2.1"
    return bytes(b)


SRGB_PROFILE = _srgb_profile()


def pdf_bytes(content: bytes, objects: list[bytes], xobjects: list[tuple[bytes, int]], media=MEDIA,
              extgs=()) -> bytes:
    """One page of `content`, with `objects` numbered 1.. in front and `xobjects` (name, object
    number) in its /XObject dictionary; the ExtGStates are `render_torture.EXTGS` (/A0../A4) plus
    `extgs` (entries of the same dictionary, e.g. the soft-mask states level 8 builds)."""
    objs = list(objects)
    gs = IMAGE_EXTGS if not extgs else IMAGE_EXTGS[:-2] + b" " + b" ".join(extgs) + b" >>"
    res = b"<< " + gs + b" /XObject << " + b" ".join(b"/%s %d 0 R" % (n, i) for n, i in xobjects) + b" >> >>"
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
    """PNG-predict the rows (a random filter per row)."""
    out = bytearray()
    width = rows.shape[1]
    prev = np.zeros(width, np.int64)

    def shift(a):
        return np.concatenate([np.zeros(bpp, np.int64), a[:-bpp]])[:width] if width > bpp else np.zeros(width, np.int64)

    for row in rows.astype(np.int64):
        tag = r.randint(0, 4)
        left, ul = shift(row), shift(prev)
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
    def __init__(self, r: random.Random, level: int = 8):
        self.r = r
        self.level = level
        self.objects: list[bytes] = []
        self.xobjects: list[tuple[bytes, int]] = []
        self.gstates: list[bytes] = []
        self.srgb = False

    def add(self, b: bytes) -> int:
        self.objects.append(b)
        return len(self.objects)

    def stream(self, entries: bytes, data: bytes) -> int:
        return self.add(b"<< %s /Length %d >>\nstream\n" % (entries, len(data)) + data + b"\nendstream")

    # ---- pixel data
    def pixels(self, w: int, h: int, comps: int, bpc: int) -> bytes:
        r = self.r
        pitch = (w * comps * bpc + 7) // 8
        if r.random() < 0.5:
            data = bytes(r.getrandbits(8) for _ in range(pitch * h))
        else:   # smooth: gradients survive JPEG and make the resampling visible
            y, x = np.mgrid[0:h, 0:pitch]
            a, b, c = r.uniform(-9, 9), r.uniform(-9, 9), r.randint(0, 255)
            data = ((x * a + y * b + c) % 256).astype(np.uint8).tobytes()
        if self.level >= 4 and r.random() < 0.1:   # truncated data
            data = data[:r.randint(0, len(data))]
        return data

    def encode(self, data: bytes, w: int, h: int, comps: int, bpc: int, jpeg_ok: bool):
        """(filter entries, encoded data)."""
        r = self.r
        if self.level < 4:
            return b"/Filter /FlateDecode", zlib.compress(data)
        pitch = (w * comps * bpc + 7) // 8
        k = r.random()
        if jpeg_ok and k < 0.2:
            from PIL import Image
            img = Image.frombytes({1: "L", 3: "RGB", 4: "CMYK"}[comps], (w, h), data[:pitch * h])
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=r.choice([30, 75, 95]), subsampling=r.choice([0, 1, 2]))
            return b"/Filter /DCTDecode", buf.getvalue()
        if k < 0.45:
            return b"/Filter /FlateDecode", zlib.compress(data)
        if k < 0.6 and len(data) == pitch * h:
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
        """(entry, components, is indexed); `self.srgb` says whether it is an sRGB ICCBased one."""
        r = self.r
        self.srgb = False
        if self.level < 3:
            return b"/DeviceRGB", 3, False
        k = r.random()
        if k < 0.3:
            return b"/DeviceGray", 1, False
        if k < 0.6:
            return b"/DeviceRGB", 3, False
        if k < 0.68:
            return b"/DeviceCMYK", 4, False
        if k < 0.85:
            bases = [(b"/DeviceGray", 1), (b"/DeviceRGB", 3)] + ([(b"/DeviceCMYK", 4)] if self.level >= 5 else [])
            base, n = r.choice(bases)
            hival = r.randint(0, 255)
            size = (hival + 1) * n if r.random() < 0.8 else r.randint(0, (hival + 1) * n)
            lookup = bytes(r.getrandbits(8) for _ in range(size))
            if r.random() < 0.5:
                return b"[/Indexed %s %d <%s>]" % (base, hival, lookup.hex().encode()), 1, True
            sid = self.stream(b"", lookup)
            return b"[/Indexed %s %d %d 0 R]" % (base, hival, sid), 1, True
        n = r.choice([1, 3])
        alt = b" /Alternate %s" % (b"/DeviceGray" if n == 1 else b"/DeviceRGB") if r.random() < 0.5 else b""
        profile = JUNK_PROFILE
        if self.level >= 7:
            profile = r.choice([JUNK_PROFILE, SRGB_PROFILE, SRGB_PROFILE, SRGB_PROFILE])
            if profile is SRGB_PROFILE and r.random() < 0.75:
                n, alt = 3, r.choice([b"", b" /Alternate /DeviceRGB", b" /Alternate /DeviceCMYK"])
            self.srgb = profile is SRGB_PROFILE and n == 3
        sid = self.stream(b"/N %d%s" % (n, alt), profile)
        return b"[/ICCBased %d 0 R]" % sid, n, False

    def image(self, inline: bool):
        """(dictionary entries without /Length, data, is a stencil) of a random image."""
        r = self.r
        lv = self.level
        w, h = r.randint(1, 24), r.randint(1, 24)
        if r.random() < 0.15:
            w, h = r.randint(30, 90), r.randint(30, 90)
        extra = b""
        if lv >= 3 and r.random() < 0.12:
            data = self.pixels(w, h, 1, 1)
            filt, enc = self.encode(data, w, h, 1, 1, False)
            dec = r.choice([b"", b"", b" /Decode [1 0]", b" /Decode [0 1]"]) if lv >= 4 else b""
            return b"/Width %d /Height %d /ImageMask true%s %s" % (w, h, dec, filt), enc, True
        cs, comps, indexed = self.colorspace()
        bpc = r.choice([1, 2, 4, 8, 8, 8, 16]) if lv >= 3 else 8
        if comps == 4 and bpc != 8 and lv < 5:
            bpc = 8
        if lv >= 7 and self.srgb and r.random() < 0.5:
            bpc = r.choice([1, 2])   # bpc * comps <= 8: the palette, where the missing clamp shows
        data = self.pixels(w, h, comps, bpc)
        jpeg_ok = (bpc == 8 and (comps in (1, 3) or comps == 4 and lv >= 6) and not indexed
                   and len(data) == ((w * comps * 8 + 7) // 8) * h)
        filt, enc = self.encode(data, w, h, comps, bpc, jpeg_ok)
        if lv >= 4 and r.random() < (0.6 if lv >= 7 and self.srgb else 0.2):
            vals = []
            for _ in range(comps):
                if indexed:
                    vals += [r.randint(0, 3), r.randint(0, (1 << bpc) - 1)]
                else:
                    vals += [r.choice([0, 1, 0.25, r.uniform(-0.5, 1.5)]), r.choice([1, 0, 0.75, r.uniform(-0.5, 1.5)])]
            extra += b" /Decode [" + b" ".join(b"%.4g" % v for v in vals) + b"]"
        if lv >= 4 and r.random() < 0.15:
            mx = (1 << bpc) - 1
            vals = []
            for _ in range(comps):
                a = r.randint(0, mx)
                vals += [a, min(mx, a + r.randint(0, max(1, mx // 3)))]
            if r.random() < 0.2:
                vals = vals[:r.randint(0, len(vals))]
            extra += b" /Mask [" + b" ".join(b"%d" % v for v in vals) + b"]"
        elif lv >= 4 and not inline and r.random() < 0.25:
            mw, mh = (w, h) if r.random() < 0.5 else (r.randint(1, 30), r.randint(1, 30))
            mbpc = r.choice([8, 8, 1, 4])
            mdata = self.pixels(mw, mh, 1, mbpc)
            mfilt, menc = self.encode(mdata, mw, mh, 1, mbpc, mbpc == 8 and len(mdata) == mw * mh)
            matte = b""
            if r.random() < 0.3 and not indexed and (comps != 4 or lv >= 5):
                matte = b" /Matte [" + b" ".join(b"%.3g" % r.random() for _ in range(comps)) + b"]"
            mid = self.stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                              b"/BitsPerComponent %d %s%s" % (mw, mh, mbpc, mfilt, matte), menc)
            extra += b" /SMask %d 0 R" % mid
        elif lv >= 4 and not inline and r.random() < 0.1:
            mw, mh = r.randint(1, 30), r.randint(1, 30)
            mdata = self.pixels(mw, mh, 1, 1)
            mfilt, menc = self.encode(mdata, mw, mh, 1, 1, False)
            mid = self.stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ImageMask true %s"
                              % (mw, mh, mfilt), menc)
            extra += b" /Mask %d 0 R" % mid
        if lv >= 4 and r.random() < 0.2:
            extra += b" /Interpolate true"
        return (b"/Width %d /Height %d /ColorSpace %s /BitsPerComponent %d%s %s"
                % (w, h, cs, bpc, extra, filt)), enc, False

    def matrix(self):
        r = self.r
        lv = self.level
        if lv == 0:
            return (r.uniform(20, 150), 0, 0, r.uniform(20, 120), r.uniform(0, 60), r.uniform(0, 40))
        sx = r.choice([1, 1, -1]) * r.choice([r.uniform(2, 60), r.uniform(60, 190), r.uniform(0.3, 4)])
        sy = r.choice([1, 1, -1]) * r.choice([r.uniform(2, 60), r.uniform(60, 140), r.uniform(0.3, 4)])
        tx, ty = r.uniform(-20, 190), r.uniform(-20, 140)
        k = r.random() if lv >= 2 else 0
        if k < (0.45 if lv >= 6 else 0.6):
            return (sx, 0, 0, sy, tx, ty)
        if k < (0.65 if lv >= 6 else 0.9):
            return (0, sy, sx, 0, tx, ty)
        if lv >= 6 and r.random() < 0.6:
            # any angle (CFX_ImageTransformer), now and then a hair off a quarter turn, maybe skewed
            q = r.randint(0, 3) * 90
            deg = q + r.choice([r.uniform(-2, 2), r.uniform(0, 360)])
            t = math.radians(deg)
            sk = r.choice([0, 0, r.uniform(-1, 1)])
            cs, sn = math.cos(t), math.sin(t)
            return (sx * cs, sx * sn, sy * (sk * cs - sn), sy * (sk * sn + cs), tx, ty)
        return (sx, r.uniform(-3, 3), r.uniform(-3, 3), sy, tx, ty)

    def draw(self) -> bytes:
        r = self.r
        lv = self.level
        inline = lv >= 4 and r.random() < 0.25
        entries, data, stencil = self.image(inline)
        ops = [b"q"]
        if stencil or (lv >= 2 and r.random() < 0.3):
            ops.append(b"%.3f %.3f %.3f rg" % (r.random(), r.random(), r.random()))
        if lv >= 2 and r.random() < 0.25:
            ops.append(r.choice([b"/A0 gs", b"/A1 gs", b"/A2 gs"]
                                + ([b"/OP0 gs", b"/OP1 gs", b"/OP2 gs", b"/OP3 gs"] if lv >= 5 else [])))
        if lv >= 2 and r.random() < 0.3:
            x, y = r.uniform(0, 150), r.uniform(0, 100)
            ops.append(b"%.2f %.2f %.2f %.2f re W n" % (x, y, r.uniform(5, 120), r.uniform(5, 100)))
        if lv >= 2 and r.random() < 0.15:
            ops.append(b"%.1f %.1f m %.1f %.1f l %.1f %.1f l h W n" % tuple(r.uniform(0, 200) for _ in range(6)))
        ops.append(b"%.4f %.4f %.4f %.4f %.3f %.3f cm" % self.matrix())
        abbrev = entries.replace(b"/Width", b"/W").replace(b"/Height", b"/H").replace(
            b"/ColorSpace", b"/CS").replace(b"/BitsPerComponent", b"/BPC").replace(b"/Filter", b"/F")
        if inline and not (b"/DecodeParms" in abbrev or b" R" in abbrev or b"/DCTDecode" in abbrev):
            ops.append(b"BI " + abbrev + b" ID " + data + b"\nEI")
        else:
            name = b"Im%d" % len(self.xobjects)
            self.xobjects.append((name, self.stream(b"/Type /XObject /Subtype /Image " + entries, data)))
            ops.append(b"/" + name + b" Do")
        ops.append(b"Q")
        return b"\n".join(ops)

    def masked(self) -> bytes:
        """Images drawn inside a soft mask's group, the mask laid over a fill or another image.
        The group gets no /Resources, so its images are the page's own: the same stream can be
        drawn inside the mask and outside it, and PDFium's page image cache keeps whichever
        conversion loaded it first."""
        r = self.r
        inner = b"\n".join(self.draw() for _ in range(r.randint(1, 2)))
        lum = r.random() < 0.75
        cs = r.choice([None, b"/DeviceGray", b"/DeviceRGB", b"/DeviceCMYK", b"/DeviceCMYK"])
        group = b"/Group << /S /Transparency"
        if cs is not None:
            group += b" /CS " + cs
        if r.random() < 0.4:
            group += b" /I true"
        fid = self.stream(b"/Type /XObject /Subtype /Form /BBox [0 0 200 150] " + group + b" >>", inner)
        bc = b""
        if r.random() < 0.8:      # without /BC the group family stays kUnknown: no TransMask
            n = {b"/DeviceGray": 1, b"/DeviceCMYK": 4}.get(cs, 3)
            bc = b" /BC [" + b" ".join(b"%.3g" % r.choice([0, 1, r.random()]) for _ in range(n)) + b"]"
        name = b"Sm%d" % len(self.gstates)
        self.gstates.append(b"/%s << /SMask << /S %s /G %d 0 R%s >> >>"
                            % (name, b"/Luminosity" if lum else b"/Alpha", fid, bc))
        ops = [b"q", b"/" + name + b" gs"]
        if r.random() < 0.5:
            ops.append(b"%.3f %.3f %.3f rg %.1f %.1f %.1f %.1f re f"
                       % (r.random(), r.random(), r.random(), r.uniform(-10, 40), r.uniform(-10, 40),
                          r.uniform(60, 200), r.uniform(60, 160)))
        else:
            ops.append(self.draw())
        ops.append(b"Q")
        return b"\n".join(ops)


def case(seed: int, level: int = 8):
    """(content, objects, xobjects, zoom, transparent, extgs) for `seed`."""
    r = random.Random(seed)
    b = Builder(r, level)
    count = 1 if level == 0 else r.randint(1, 3)
    parts = [b.masked() if level >= 8 and r.random() < 0.7 else b.draw() for _ in range(count)]
    content = b"\n".join(parts)
    zoom = r.choice([0.5, 1, 1.37, 2, 3.1])
    transparent = level >= 1 and r.random() < 0.3
    return content, b.objects, b.xobjects, zoom, transparent, b.gstates


def compare(content: bytes, objects, xobjects, zoom: float, transparent: bool, extgs=()):
    """(pixels that differ or None when the pure reader refuses, PDFium's render, pure's render,
    per-pixel difference or the refusal)."""
    from ..pdf.api import PdfError
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    data = pdf_bytes(content, objects, xobjects, extgs=extgs)
    docs = PdfiumBackend().open(data), PureBackend().open(data)
    try:
        a = docs[0][0].render(zoom, transparent=transparent)
        try:
            b = docs[1][0].render(zoom, transparent=transparent)
        except PdfError as e:
            return None, a, None, str(e)
    finally:
        for doc in docs:
            doc.close()
    d = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return int((d > 0).sum()), a, b, d


def shrink(content: bytes, objects, xobjects, zoom: float, transparent: bool, extgs=()):
    """Drop lines of the page while the difference remains."""
    def fails(c):
        try:
            return (compare(c, objects, xobjects, zoom, transparent, extgs)[0] or 0) > 0
        except Exception:
            return False
    lines = content.split(b"\n")
    changed = True
    while changed:
        changed = False
        for i in range(len(lines)):
            if lines[i] in (b"q", b"Q") or lines[i].startswith(b"BI ") or lines[i] == b"EI":
                continue
            trial = lines[:i] + lines[i + 1:]
            if fails(b"\n".join(trial)):
                lines, changed = trial, True
                break
    return b"\n".join(lines)


def run(seed0: int, n: int, out: Path | None = None, verbose: bool = True, level: int = 8) -> dict:
    """{'failed': [seeds], 'refused': {reason: count}, 'drawn': count}."""
    stats = {"failed": [], "refused": {}, "drawn": 0}
    for seed in range(seed0, seed0 + n):
        content, objects, xobjects, zoom, transparent, extgs = case(seed, level)
        try:
            npx, a, b, d = compare(content, objects, xobjects, zoom, transparent, extgs)
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
        small = shrink(content, objects, xobjects, zoom, transparent, extgs)
        npx, a, b, d = compare(small, objects, xobjects, zoom, transparent, extgs)
        print(f"seed {seed} zoom {zoom} transparent {transparent}: {npx} px, max {d.max() if npx else 0}")
        print("-- page\n" + small.decode("latin-1")[:1500])
        if out is not None and npx:
            out.mkdir(parents=True, exist_ok=True)
            from PIL import Image
            vis = np.concatenate([a[..., :3], b[..., :3], np.stack([np.where(d > 0, 255, 0)] * 3, -1)], 1)
            Image.fromarray(vis.astype(np.uint8)).save(out / f"seed{seed}.png")
            (out / f"seed{seed}.pdf").write_bytes(pdf_bytes(small, objects, xobjects, extgs=extgs))
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seed0", type=int, nargs="?", default=0)
    ap.add_argument("n", type=int, nargs="?", default=200)
    ap.add_argument("--level", type=int, default=8)
    ap.add_argument("--out", default="out/render-torture-image")
    ap.add_argument("-q", "--quiet", action="store_true", help="no shrinking, only the counts")
    args = ap.parse_args(argv)
    stats = run(args.seed0, args.n, Path(args.out), verbose=not args.quiet, level=args.level)
    print(f"seeds {args.seed0}..{args.seed0 + args.n - 1}: {stats['drawn']} exact, "
          f"{len(stats['failed'])} failed {stats['failed'][:30]}, refused {stats['refused']}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
