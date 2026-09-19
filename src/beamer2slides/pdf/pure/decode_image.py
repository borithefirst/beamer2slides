"""PDFium's image loading, ported for `render_image.py`: CPDF_DIB (LoadColorInfo, the decode and
colour-key arrays, LoadPalette, GetScanline and TranslateScanline24bpp), the image decoders it
creates (Flate with PNG/TIFF predictors, RunLength, DCT through libjpeg) and the colour spaces an
image can be in (DeviceGray, DeviceRGB, DeviceCMYK, Indexed, ICCBased read through its alternate),
value for value.

`load` gives a `DIB`: a CFX_DIBBase's format ("mask1" k1bppMask, "rgb1" k1bppRgb, "rgb8" k8bppRgb,
"bgr" kBgr, "bgra" kBgra), its rows (1-bit formats unpacked to 0/1 per pixel), its palette (ARGB
ints or None) and, for /SMask or a /Mask stream, the mask DIB and its matte colour. What PDFium
would fail to load gives None (nothing is drawn); what is not ported raises `Unsupported`, which the
renderer turns into PdfError: an image is decoded exactly or refused."""

from __future__ import annotations

import io
import math
import struct
import zlib

import numpy as np

from . import filters as FL
from .colors import adobe_cmyk_to_srgb
from .syntax import Name, Stream

F32 = np.float32


class Unsupported(Exception):
    """Something PDFium does that is not ported: the page is refused, not drawn differently."""


def F(v: float) -> float:
    return struct.unpack("f", struct.pack("f", v))[0]


def roundf(v: float) -> int:
    """FXSYS_roundf for the 0..255 range images use."""
    return int(math.floor(abs(v) + 0.5)) * (1 if v >= 0 else -1)


def argb(a: int, r: int, g: int, b: int) -> int:
    return ((a & 255) << 24) | ((r & 255) << 16) | ((g & 255) << 8) | (b & 255)


class DIB:
    __slots__ = ("fmt", "w", "h", "rows", "palette", "mask", "matte", "interpolate")

    def __init__(self, fmt, rows, palette=None):
        self.fmt, self.rows, self.palette = fmt, rows, palette
        self.h, self.w = rows.shape[:2]
        self.mask, self.matte, self.interpolate = None, 0xFFFFFFFF, False

    @property
    def bpp(self) -> int:
        return {"mask1": 1, "rgb1": 1, "rgb8": 8, "mask8": 8, "bgr": 24, "bgra": 32}[self.fmt]


# ---------------------------------------------------------------------- colour spaces


class CS:
    """The image-relevant part of a CPDF_ColorSpace."""

    def __init__(self, family: str, n: int, base: "CS | None" = None, stock: bool = False):
        self.family, self.n, self.base, self.stock = family, n, base, stock
        self.lookup = b""
        self.max_index = 0

    def default(self, i):
        """GetDefaultValue: (min, max)."""
        return 0.0, 1.0

    def rgb(self, v: np.ndarray):
        """GetRGB over (..., n) float32 values: (r, g, b) float32 arrays and a validity mask
        (GetRGBOrZerosOnError gives zeros where it is False)."""
        f = self.family
        if f == "DeviceGray":
            g = np.clip(v[..., 0], F32(0), F32(1))
            return g, g, g, np.ones(g.shape, bool)
        if f == "DeviceRGB":
            c = np.clip(v[..., :3], F32(0), F32(1))
            return c[..., 0], c[..., 1], c[..., 2], np.ones(c.shape[:-1], bool)
        if f == "DeviceCMYK":
            return _cmyk_rgb_f(v)
        if f == "ICCBased":
            if self.n == 1 and self.base.n > 1:
                v = np.repeat(v[..., :1], self.base.n, axis=-1)
            return self.base.rgb(v)
        if f == "Indexed":
            x = v[..., 0]
            finite = np.isfinite(x) & (x > -2147483648.0) & (x < 2147483648.0)
            idx = np.where(finite, x, -1).astype(np.int64)
            n = self.base.n
            ok = (idx >= 0) & (idx <= self.max_index) & ((idx + 1) * n <= len(self.lookup))
            table = np.frombuffer(self.lookup, np.uint8)
            safe = np.where(ok, idx, 0)
            comps = np.zeros(idx.shape + (max(n, 1),), F32)
            for i in range(n):
                lo, hi = self.base.default(i)
                mx = F32(F(hi - lo))
                byte = table[np.minimum(safe * n + i, len(table) - 1)] if len(table) else np.zeros(idx.shape, np.uint8)
                comps[..., i] = F32(lo) + (mx * byte.astype(F32)) / F32(255)
            r, g, b, valid = self.base.rgb(comps)
            return r, g, b, valid & ok
        raise Unsupported(f"{f} image colours")


def _cmyk_rgb_f(v: np.ndarray):
    """CPDF_DeviceCS::GetRGB for CMYK without std conversion: AdobeCmykToStandardRgbF, each
    channel clamped, rounded to a byte with the 0.49999997f offset, looked up, times 1/255.f."""
    c = np.clip(np.nan_to_num(v[..., :4].astype(F32), nan=F32(0)), F32(0), F32(1))
    q = (c * F32(255) + F32(0.49999997)).astype(np.int64)
    flat = q.reshape(-1, 4)
    keys, inv = np.unique(flat, axis=0, return_inverse=True)
    table = np.array([adobe_cmyk_to_srgb(*(int(x) for x in k)) for k in keys], np.int64).reshape(-1, 3)
    rgb = (table[np.asarray(inv).reshape(-1)].astype(F32) * F32(1.0 / 255.0)).reshape(q.shape[:-1] + (3,))
    return rgb[..., 0], rgb[..., 1], rgb[..., 2], np.ones(q.shape[:-1], bool)


GRAY, RGB, CMYK = CS("DeviceGray", 1, stock=True), CS("DeviceRGB", 3, stock=True), CS("DeviceCMYK", 4, stock=True)
_STOCK = {"DeviceGray": GRAY, "G": GRAY, "DeviceRGB": RGB, "RGB": RGB, "DeviceCMYK": CMYK, "CMYK": CMYK}


def _icc_valid(data: bytes) -> bool:
    """Could lcms open this as a profile? Anything that might be one is refused: only data that
    cannot be an ICC profile falls back to the alternate as PDFium's does."""
    return len(data) >= 128 and data[36:40] == b"acsp"


def load_cs(doc, obj, resources, depth=0) -> CS | None:
    """CPDF_DocPageData::GetColorSpace for an image: None when it doesn't load."""
    r = doc.resolve
    obj = r(obj)
    if depth > 8:
        return None
    if isinstance(obj, Name):
        name = str(obj)
        if name in _STOCK:
            spaces = r(resources.get("ColorSpace")) if isinstance(resources, dict) else None
            if isinstance(spaces, dict) and any(k in spaces for k in ("DefaultGray", "DefaultRGB", "DefaultCMYK")):
                raise Unsupported("Default colour spaces")
            return _STOCK[name]
        if name == "Pattern":
            raise Unsupported("Pattern image colour spaces")
        if not isinstance(resources, dict):
            return None
        spaces = r(resources.get("ColorSpace"))
        if not isinstance(spaces, dict) or name not in spaces:
            return None
        return load_cs(doc, spaces[name], None, depth + 1)
    if isinstance(obj, list):
        if len(obj) == 1:
            return load_cs(doc, obj[0], resources, depth + 1)
        return _load_array(doc, obj, depth)
    if isinstance(obj, Stream):
        raise Unsupported("colour space streams")
    return None


def _load_array(doc, arr, depth) -> CS | None:
    r = doc.resolve
    fam = r(arr[0]) if arr else None
    if not isinstance(fam, Name):
        return None
    f = str(fam)
    if f in ("Indexed", "I"):
        if len(arr) < 4:
            return None
        base = _guarded(doc, arr[1], depth)
        if base is None or base.family in ("Indexed", "Pattern"):
            return None
        cs = CS("Indexed", 1, base)
        hi = r(arr[2])
        cs.max_index = max(0, min(255, int(hi))) if isinstance(hi, (int, float)) and not isinstance(hi, bool) else 0
        lk = r(arr[3])
        if isinstance(lk, Stream):
            data = doc.stream_data(lk)
            if FL.decoder_array(lk.dict, r):
                last = FL.decoder_array(lk.dict, r)[-1][0]
                if FL.ABBREVIATIONS.get(last, last) not in ("FlateDecode", "LZWDecode", "ASCII85Decode",
                                                            "ASCIIHexDecode", "RunLengthDecode"):
                    raise Unsupported("an image-coded palette")
            cs.lookup = bytes(data)
        elif isinstance(lk, (bytes, bytearray)) or type(lk).__name__ == "String":
            cs.lookup = bytes(lk)
        return cs
    if f == "ICCBased":
        st = r(arr[1]) if len(arr) > 1 else None
        if not isinstance(st, Stream):
            return None
        n = r(st.get("N"))
        if not isinstance(n, int) or isinstance(n, bool) or n not in (1, 3, 4):
            raise Unsupported("an ICC profile without a usable /N")
        if _icc_valid(doc.stream_data(st)):
            raise Unsupported("ICC profiles")
        alt = None
        if st.get("Alternate") is not None:
            try:
                alt = load_cs(doc, st.get("Alternate"), None, depth + 1)
            except Unsupported:
                raise
            if alt is not None and (alt.family not in ("DeviceGray", "DeviceRGB", "DeviceCMYK") or alt.n != n):
                if alt.n == n:
                    raise Unsupported("ICC alternates that are not device spaces")
                alt = None
        if alt is None:
            alt = {1: GRAY, 3: RGB, 4: CMYK}[n]
        return CS("ICCBased", n, alt)
    if f[:4] in ("Devi", "CalG", "CalR", "Lab", "Sepa", "Patt", "Inde"):
        raise Unsupported(f"{f} image colour spaces")
    return None


def _guarded(doc, obj, depth):
    return load_cs(doc, obj, None, depth + 1)


# ---------------------------------------------------------------------- stream decoding


def pitch8(bpc: int, comps: int, width: int) -> int:
    return (bpc * comps * width + 7) // 8


def _png_predict(raw: bytes, colors: int, bpc: int, columns: int, rows: int) -> bytes:
    """The PNG predictor of FlateScanlineDecoder, row by row against the previous row."""
    row = pitch8(bpc, colors, columns)
    bpp = (colors * bpc + 7) // 8
    need = rows * (row + 1)
    raw = raw[:need] + bytes(max(0, need - len(raw)))
    tags = raw[::row + 1][:rows]
    if colors in (1, 2, 3, 4) and bpc == 8 and all(t <= 4 for t in tags) and rows > 0:
        fast = FL._png_unfilter(b"", raw, {"Colors": colors, "BitsPerComponent": 8, "Columns": columns})
        if fast is not None and len(fast) == rows * row:
            return fast
    out = bytearray()
    prev = bytearray(row)
    for k in range(rows):
        tag = raw[k * (row + 1)]
        cur = bytearray(raw[k * (row + 1) + 1:(k + 1) * (row + 1)])
        if tag == 1:
            for i in range(bpp, row):
                cur[i] = (cur[i] + cur[i - bpp]) & 255
        elif tag == 2:
            for i in range(row):
                cur[i] = (cur[i] + prev[i]) & 255
        elif tag == 3:
            for i in range(row):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 255
        elif tag == 4:
            for i in range(row):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                cur[i] = (cur[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += cur
        prev = cur
    return bytes(out)


def _num(d, key, default, r):
    v = r(d.get(key))
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _flate_lines(data: bytes, params: dict, r, bpc: int, comps: int, width: int, height: int) -> bytes:
    """FlateScanlineDecoder (with its predictor): `height` lines of the image's pitch."""
    pitch = pitch8(bpc, comps, width)
    raw = FL.flate(data)
    pred = _num(params, "Predictor", 0, r) if params else 0
    if pred >= 10 or pred == 2:
        colors = _num(params, "Colors", 1, r)
        pbpc = _num(params, "BitsPerComponent", 8, r)
        cols = _num(params, "Columns", 1, r)
        if pbpc * colors * cols == 0:
            pbpc, colors, cols = bpc, comps, width
        if pitch8(pbpc, colors, cols) != pitch or colors <= 0 or pbpc <= 0 or cols <= 0:
            raise Unsupported("predictor rows unlike the image's")
        if pred >= 10:
            return _png_predict(raw, colors, pbpc, cols, height)
        if pbpc != 8:
            raise Unsupported("TIFF predictors below or above 8 bits")
        need = height * pitch
        raw = raw[:need] + bytes(max(0, need - len(raw)))
        a = np.frombuffer(raw, np.uint8).reshape(height, pitch)
        usable = (pitch // colors) * colors
        out = a.copy()
        if usable:
            px = a[:, :usable].reshape(height, -1, colors)
            out[:, :usable] = np.cumsum(px, axis=1, dtype=np.uint8).reshape(height, usable)
        return out.tobytes()
    if pred not in (0, 1) and params:
        pass    # an unknown predictor: none
    return raw


def _jpeg(data: bytes, params: dict, r, width: int, height: int, comps: int, dev_size):
    """CJpegDecoder through Pillow's libjpeg (both ISLOW with fancy upsampling): the decoded
    bytes, width and height, or None when PDFium's decoder would not be created."""
    from PIL import Image, ImageFile
    if params and _num(params, "ColorTransform", 1, r) == 0:
        raise Unsupported("JPEG /ColorTransform 0")
    start = data.find(b"\xff\xd8")
    if start < 0:
        return None
    data = data[start:]
    if len(data) >= 2 and data[-2:] != b"\xff\xd9":
        raise Unsupported("a JPEG PDFium patches the end of")
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        img = Image.open(io.BytesIO(data))
        if img.format != "JPEG":
            raise Unsupported("a JPEG Pillow reads as something else")
        ncomp = len(img.layer)
        if (ncomp, img.mode) not in ((1, "L"), (3, "RGB"), (4, "CMYK")):
            raise Unsupported(f"{ncomp}-component JPEGs")
        if ncomp < comps:
            return None
        if ncomp != comps:
            raise Unsupported("a JPEG with more components than its colour space")
        jw, jh = img.size
        if jw < width:
            return None
        if (jw, jh) != (width, height):
            raise Unsupported("a JPEG whose size differs from the image's")
        denom = 1
        if dev_size[0] and dev_size[1]:
            ratio = max(1, min(width // dev_size[0], height // dev_size[1]))
            skip = int(math.log2(ratio))
            denom = 1 << min(skip, 3)
        if denom > 1:
            mh = max(l[1] for l in img.layer)
            mv = max(l[2] for l in img.layer)
            if jw % (mh * 8) or jh % (mv * 8):
                denom = 1
        if denom > 1:
            want = (-(-jw // denom), -(-jh // denom))
            img.draft(img.mode, (jw // denom, jh // denom))
            if img.size != want:
                raise Unsupported("a JPEG scale Pillow does not give")
        img.load()
        out = img.tobytes()
        if ncomp == 4:
            # libjpeg's CMYK as it comes (PDFium leaves Adobe's inverted polarity to /Decode);
            # Pillow's "CMYK;I" unpacker inverts every byte, which undoes exactly
            out = np.bitwise_xor(np.frombuffer(out, np.uint8), np.uint8(255)).tobytes()
        return out, img.size[0], img.size[1]
    except Unsupported:
        raise
    except Exception as e:  # noqa: BLE001 - libjpeg errors: PDFium's own recovery is not ported
        raise Unsupported(f"a JPEG libjpeg complains about ({type(e).__name__})")


def image_bytes(doc, d: dict, raw: bytes, bpc: int, comps: int, width: int, height: int, dev_size):
    """CPDF_StreamAcc::LoadAllDataImageAcc + CreateDecoder: (bytes, width, height, rows present,
    last filter) with `rows present` the lines GetScanline finds data for (the rest come back as
    a zeroed buffer); None when PDFium's load fails."""
    r = doc.resolve
    decoders = FL.decoder_array(d, r)
    if decoders is None:
        return None
    data = raw
    codec, params = None, {}
    for i, (name, p) in enumerate(decoders):
        name = FL.ABBREVIATIONS.get(name, name)
        last = i == len(decoders) - 1
        if name == "Crypt":
            continue
        pr = {k: r(v) for k, v in p.items()}
        if name == "FlateDecode" and last:
            codec, params = name, p
            break
        if name == "RunLengthDecode" and last:
            codec, params = name, p
            break
        try:
            if name == "FlateDecode":
                if (pr.get("Predictor") or 1) != 1:
                    raise Unsupported("predictors before the last filter")
                data = FL.flate(data)
            elif name == "LZWDecode":
                if (pr.get("Predictor") or 1) != 1:
                    raise Unsupported("LZW predictors")
                data = FL.lzw(data, pr.get("EarlyChange", 1))
            elif name == "ASCII85Decode":
                data = FL.ascii85(data)
            elif name == "ASCIIHexDecode":
                data = FL.ascii_hex(data)
            elif name == "RunLengthDecode":
                data = FL.run_length(data)
            elif name == "DCTDecode":
                codec, params = name, p
                break
            else:
                raise Unsupported(f"{name} images")
        except Unsupported:
            raise
        except Exception:  # noqa: BLE001
            raise Unsupported("a filter that fails")
    if codec == "RunLengthDecode" and not _rl_dest_size_ok(data, bpc, comps, width, height):
        return None
    if not data:
        raise Unsupported("filters that decode to nothing")
    pitch = pitch8(bpc, comps, width)
    if codec == "FlateDecode":
        lines = _flate_lines(data, params, r, bpc, comps, width, height)
        return _pad(lines, pitch, height), width, height, height, codec
    if codec == "RunLengthDecode":
        return _pad(FL.run_length(data), pitch, height), width, height, height, codec
    if codec == "DCTDecode":
        got = _jpeg(data, params, r, width, height, comps, dev_size)
        if got is None:
            return None
        out, w, h = got
        return out, w, h, h, codec
    present = min(height, -(-len(data) // pitch)) if pitch else 0
    return _pad(data, pitch, height), width, height, present, codec


def _rl_dest_size_ok(src: bytes, bpc: int, comps: int, width: int, height: int) -> bool:
    """RLScanlineDecoder::CheckDestSize: the runs must promise at least the whole image (counted
    from the run lengths, whether the data behind them is there or not), or the load fails."""
    i, size = 0, 0
    while i < len(src):
        op = src[i]
        if op < 128:
            size += op + 1
            i += op + 2
        elif op > 128:
            size += 257 - op
            i += 2
        else:
            break
        if size > 0xFFFFFFFF:
            return False
    return ((width * comps * bpc * height + 7) & 0xFFFFFFFF) // 8 <= size


def _pad(data: bytes, pitch: int, height: int) -> bytes:
    need = pitch * height
    return data[:need] + bytes(max(0, need - len(data)))


# ---------------------------------------------------------------------- CPDF_DIB


def _bits(rows: np.ndarray, bpc: int, count: int) -> np.ndarray:
    """GetBits8 for `count` consecutive samples of every row."""
    if bpc == 8:
        return rows[:, :count].astype(np.uint32)
    if bpc == 16:
        b = rows[:, :count * 2].astype(np.uint32)
        return b[:, 0::2] * 256 + b[:, 1::2]
    bits = np.unpackbits(rows, axis=1)[:, :count * bpc].reshape(rows.shape[0], count, bpc).astype(np.uint32)
    weights = (1 << np.arange(bpc - 1, -1, -1)).astype(np.uint32)
    return (bits * weights).sum(axis=2).astype(np.uint32)


def _float_array(v, r) -> list:
    v = r(v)
    return v if isinstance(v, list) else None


def _get_float(arr, i, r) -> float:
    if arr is None or i >= len(arr):
        return 0.0
    x = r(arr[i])
    return F(float(x)) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def _get_int(arr, i, r) -> int:
    if arr is None or i >= len(arr):
        return 0
    x = r(arr[i])
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return 0
    return int(x)


def load(doc, stream, resources, dev_size=(0, 0), is_mask=False, with_mask=True) -> DIB | None:
    """CPDF_DIB::Load (+ the mask, when the image has one and `with_mask`)."""
    r = doc.resolve
    d = stream.dict
    raw = stream.raw if isinstance(stream, Stream) else stream.data
    w, h = r(d.get("Width")), r(d.get("Height"))
    if not isinstance(w, int) or not isinstance(h, int) or isinstance(w, bool) or isinstance(h, bool):
        return None
    if not (0 < w <= 0x1FFFF and 0 < h <= 0x1FFFF):
        return None
    decoders = FL.decoder_array(d, r)
    if decoders is None:
        return None
    last = FL.ABBREVIATIONS.get(decoders[-1][0], decoders[-1][0]) if decoders else ""
    if last in ("JPXDecode", "JBIG2Decode", "CCITTFaxDecode"):
        raise Unsupported(f"{last} images")
    bpc = _num(d, "BitsPerComponent", 0, r)
    if not 0 <= bpc <= 16:
        return None
    image_mask = bool(r(d.get("ImageMask")) is True)
    cs = None
    decode = _float_array(d.get("Decode"), r)
    default_decode = True
    if image_mask or d.get("ColorSpace") is None:
        image_mask, bpc, comps = True, 1, 1
        default_decode = decode is None or not _get_int(decode, 0, r)
        family = None
    else:
        cs = load_cs(doc, d.get("ColorSpace"), resources)
        if cs is None:
            return None
        comps = cs.n
        family = cs.family
        if last == "DCTDecode":
            bpc = 8
        if bpc not in (1, 2, 4, 8, 16):
            return None
    if family == "DeviceCMYK" or (family == "ICCBased" and cs.base.family == "DeviceCMYK"):
        if is_mask:
            raise Unsupported("CMYK soft masks")
    got = image_bytes(doc, d, raw, bpc, comps, w, h, dev_size)
    if got is None:
        return None
    data, w, h, present, codec = got
    pitch = pitch8(bpc, comps, w)
    rows = np.frombuffer(data, np.uint8)[:pitch * h].reshape(h, pitch) if pitch else np.zeros((h, 0), np.uint8)

    if image_mask:
        bits = np.unpackbits(rows, axis=1)[:, :w]
        if default_decode:
            bits = 1 - bits
        bits[present:] = 0
        dib = DIB("mask1", bits.astype(np.uint8))
        dib.interpolate = bool(r(d.get("Interpolate")) is True)
        return dib

    # GetDecodeAndMaskArray
    max_data = (1 << bpc) - 1
    comp_min, comp_step = [], []
    for i in range(comps):
        lo, hi = cs.default(i)
        if decode is not None:
            mn = _get_float(decode, 2 * i, r)
            mx = _get_float(decode, 2 * i + 1, r)
            comp_min.append(mn)
            comp_step.append(F((F(mx - mn)) / max_data))
            def_max = float(max_data) if family == "Indexed" else hi
            if lo != mn or def_max != mx:
                default_decode = False
        else:
            top = float(max_data) if family == "Indexed" else hi
            comp_min.append(lo)
            comp_step.append(F(F(top - lo) / max_data))
    color_key = False
    key_min, key_max = [0] * comps, [0] * comps
    if d.get("SMask") is None:
        m = r(d.get("Mask"))
        if isinstance(m, list):
            if len(m) >= comps * 2:
                for i in range(comps):
                    key_min[i] = max(_get_int(m, 2 * i, r), 0)
                    key_max[i] = min(max(_get_int(m, 2 * i + 1, r), 0), max_data)
            color_key = True
    if color_key and is_mask:
        raise Unsupported("a colour-keyed soft mask")

    bits = bpc * comps
    cmyk = family == "DeviceCMYK" or (family == "ICCBased" and cs.base.family == "DeviceCMYK")

    # LoadPalette
    palette = None
    if bits == 1:
        if not (default_decode and family in ("DeviceGray", "DeviceRGB")) and cs.n <= 3:
            vals = np.array([[comp_min[0]] * 3, [F(comp_min[0] + comp_step[0])] * 3], F32)
            pr, pg, pb, ok = cs.rgb(vals)
            cols = [argb(255, roundf(F(float(pr[k]) * 255)) if ok[k] else 0,
                         roundf(F(float(pg[k]) * 255)) if ok[k] else 0,
                         roundf(F(float(pb[k]) * 255)) if ok[k] else 0) for k in range(2)]
            if family == "Indexed" and cs.max_index == 0:
                cols[1] = 0xFF000000
            if cols[0] != 0xFF000000 or cols[1] != 0xFFFFFFFF:
                palette = cols
    elif bits <= 8 and not (bpc == 8 and default_decode and cs is GRAY):
        n = 1 << bits
        vals = np.zeros((n, max(comps, 1)), F32)
        for i in range(n):
            cd = i
            for j in range(comps):
                enc = cd % (1 << bpc)
                cd //= 1 << bpc
                vals[i, j] = F32(comp_min[j]) + F32(comp_step[j]) * F32(enc)
        pr, pg, pb, ok = cs.rgb(vals)
        palette = []
        for k in range(n):
            if ok[k]:
                palette.append(argb(255, roundf(float(F32(pr[k]) * F32(255))), roundf(float(F32(pg[k]) * F32(255))),
                                    roundf(float(F32(pb[k]) * F32(255)))))
            else:
                palette.append(argb(255, 0, 0, 0))

    # GetScanline
    if bits == 1:
        b1 = np.unpackbits(rows, axis=1)[:, :w].astype(np.uint8)
        if not color_key:
            b1[present:] = 0
            dib = DIB("rgb1", b1, palette)
        else:
            set_v = 0 if key_max[0] == 1 else (palette[1] if palette else 0xFFFFFFFF)
            reset_v = 0 if key_min[0] == 0 else (palette[0] if palette else 0xFF000000)
            v = np.where(b1 == 1, np.uint32(set_v), np.uint32(reset_v)).astype("<u4")
            out = v.view(np.uint8).reshape(h, w, 4).copy()
            out[present:] = 0
            dib = DIB("bgra", out)
    elif bits <= 8:
        if bpc == 8:
            idx = rows[:, :w].astype(np.uint32)
        else:
            s = _bits(rows, bpc, w * comps).reshape(h, w, comps)
            idx = np.zeros((h, w), np.uint32)
            for c in range(comps):
                idx |= s[..., c] << (c * bpc)
        idx = (idx & 255).astype(np.uint8)
        if not color_key:
            idx[present:] = 0
            dib = DIB("rgb8", idx, palette)
        else:
            out = np.zeros((h, w, 4), np.uint8)
            if palette:
                pal = np.array(palette, np.uint32)[idx]
                out[..., 0], out[..., 1], out[..., 2] = pal & 255, (pal >> 8) & 255, (pal >> 16) & 255
            else:
                out[..., 0] = out[..., 1] = out[..., 2] = idx
            out[..., 3] = np.where((idx < key_min[0]) | (idx > key_max[0]), 255, 0)
            out[present:] = 0
            dib = DIB("bgra", out)
    else:
        bgr = _translate24(rows, cs, family, bpc, comps, w, h, default_decode, comp_min, comp_step, cmyk)
        if color_key:
            s = _bits(rows, bpc, w * comps).reshape(h, w, comps)
            out_of = np.zeros((h, w), bool)
            for c in range(comps):
                out_of |= (s[..., c] < key_min[c]) | (s[..., c] > key_max[c])
            out = np.zeros((h, w, 4), np.uint8)
            out[..., :3] = bgr
            out[..., 3] = np.where(out_of, 255, 0)
            out[present:] = 0
            dib = DIB("bgra", out)
        else:
            bgr[present:] = 0
            dib = DIB("bgr", bgr)
    dib.interpolate = bool(r(d.get("Interpolate")) is True)
    if is_mask or not with_mask:
        return dib

    # StartLoadMask
    smask = r(d.get("SMask"))
    mstream = None
    if isinstance(smask, Stream):
        mstream = smask
        matte = _float_array(smask.dict.get("Matte"), r)
        if matte is not None and len(matte) == comps and cs.n <= comps and cs.family != "Pattern":
            vals = np.array([[_get_float(matte, i, r) for i in range(comps)]], F32)
            pr, pg, pb, ok = cs.rgb(vals)
            if ok[0]:
                dib.matte = argb(0, roundf(float(F32(pr[0]) * F32(255))), roundf(float(F32(pg[0]) * F32(255))),
                                 roundf(float(F32(pb[0]) * F32(255))))
            else:
                dib.matte = 0
    else:
        m = r(d.get("Mask"))
        if isinstance(m, Stream):
            mstream = m
    if mstream is not None:
        mdib = load(doc, mstream, None, (0, 0), is_mask=True)
        if mdib is not None:
            if mdib.fmt not in ("rgb1", "rgb8", "mask1"):
                raise Unsupported("soft masks that are not gray")
            dib.mask = mdib
    return dib


def _translate24(rows, cs, family, bpc, comps, w, h, default_decode, comp_min, comp_step, cmyk) -> np.ndarray:
    """TranslateScanline24bpp: BGR bytes."""
    out = np.zeros((h, w, 3), np.uint8)
    if default_decode:
        if family != "DeviceRGB":
            if bpc == 8:
                if comps != cs.n:
                    raise Unsupported("a stale translation line")
                src = rows[:, :w * comps].reshape(h, w, comps)
                base = cs.base if family == "ICCBased" else cs
                bf = base.family
                if bf == "DeviceGray":
                    out[..., 0] = out[..., 1] = out[..., 2] = src[..., 0]
                elif bf == "DeviceRGB":
                    out[...] = src[..., ::-1]
                elif bf == "DeviceCMYK":
                    flat = src[..., :4].reshape(-1, 4)
                    keys, inv = np.unique(flat, axis=0, return_inverse=True)
                    table = np.array([adobe_cmyk_to_srgb(*(int(x) for x in k)) for k in keys],
                                     np.uint8).reshape(-1, 3)
                    out[...] = table[np.asarray(inv).reshape(-1)][:, ::-1].reshape(h, w, 3)
                else:
                    raise Unsupported(f"{bf} image lines")
                return out
        else:
            if comps != 3:
                raise Unsupported("a stale translation line")
            if bpc == 8:
                return rows[:, :w * 3].reshape(h, w, 3)[..., ::-1].copy()
            if bpc == 16:
                s = rows[:, :w * 6].reshape(h, w, 6)
                out[..., 0], out[..., 1], out[..., 2] = s[..., 4], s[..., 2], s[..., 0]
                return out
            mx = (1 << bpc) - 1
            s = np.minimum(_bits(rows, bpc, w * 3).reshape(h, w, 3), mx)
            out[...] = (s[..., ::-1] * 255 // mx).astype(np.uint8)
            return out
    s = _bits(rows, bpc, w * comps).reshape(h, w, comps)
    vals = np.zeros((h, w, max(comps, cs.n)), F32)
    for c in range(comps):
        vals[..., c] = F32(comp_min[c]) + F32(comp_step[c]) * s[..., c].astype(F32)
    pr, pg, pb, ok = cs.rgb(vals)
    for k, ch in ((0, pb), (1, pg), (2, pr)):
        v = np.where(ok, np.clip(ch, F32(0), F32(1)), F32(0)).astype(F32)
        out[..., k] = (v * F32(255)).astype(np.uint8)
    return out
