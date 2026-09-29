"""PDFium's image loading, ported for `render_image.py`: CPDF_DIB (LoadColorInfo, the decode and
colour-key arrays, LoadPalette, GetScanline and TranslateScanline24bpp), the image decoders it
creates (Flate with PNG/TIFF predictors, RunLength, DCT through libjpeg) and the colour spaces an
image can be in (DeviceGray, DeviceRGB, DeviceCMYK, Indexed, ICCBased as sRGB or read through its
alternate), value for value.

`load` gives a `DIB`: a CFX_DIBBase's format ("mask1" k1bppMask, "rgb1" k1bppRgb, "rgb8" k8bppRgb,
"bgr" kBgr, "bgra" kBgra), its rows (1-bit formats unpacked to 0/1 per pixel), its palette (ARGB
ints or None) and, for /SMask or a /Mask stream, the mask DIB and its matte colour. What PDFium
would fail to load gives None (nothing is drawn); what is not ported raises `Unsupported`, which the
renderer turns into PdfError: an image is decoded exactly or refused."""

from __future__ import annotations

import io
import math
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Union

import numpy as np

from ...arrays import Bytes, Floats32, Mask, Pixels, UInt32
from ...typing_compat import assert_never
from . import filters as FL
from .colors import adobe_cmyk_to_srgb_array, icc_openable, icc_srgb
from .crt import roundf
from .syntax import InlineImage, Name, PdfArray, PdfDict, PdfObject, Stream, String

if TYPE_CHECKING:
    from .document import PdfFile

F32 = np.float32

ImageStream = Union[Stream, InlineImage]
"""What an image is read from: an image XObject, or an inline image (BI ... ID ... EI)."""


class Unsupported(Exception):
    """Something PDFium does that is not ported: the page is refused, not drawn differently."""


def F(v: float) -> float:
    return struct.unpack("f", struct.pack("f", v))[0]


def argb(a: int, r: int, g: int, b: int) -> int:
    """ArgbEncode: uint32 arguments, so a channel outside 0..255 runs into the bytes above it."""
    return (((a & 0xFFFFFFFF) << 24) | ((r & 0xFFFFFFFF) << 16) | ((g & 0xFFFFFFFF) << 8)
            | (b & 0xFFFFFFFF)) & 0xFFFFFFFF


DibFormat = Literal["mask1", "rgb1", "rgb8", "bgr", "bgra"]
"""The CFX_DIBBase formats an image loads as: k1bppMask, k1bppRgb, k8bppRgb, kBgr, kBgra."""

NO_MATTE = 0xFFFFFFFF
"""A DIB's `matte` when its soft mask has no /Matte (CPDF_DIB's m_MatteColor starts there)."""


@dataclass(frozen=True, kw_only=True)
class DIB:
    """A loaded image (CPDF_DIB): `rows` are h x w (x channels) bytes in `fmt`'s layout, 1-bit
    formats unpacked to 0/1 per pixel; `palette` its ARGB colours (None: the format's own), `mask`
    its soft mask (/SMask or a /Mask stream, loaded as a DIB of its own), `matte` the /Matte colour
    (`NO_MATTE` for none) and `interpolate` its /Interpolate."""
    fmt: DibFormat
    rows: Pixels
    palette: list[int] | None
    mask: DIB | None
    matte: int
    interpolate: bool

    @property
    def h(self) -> int:
        return self.rows.shape[0]

    @property
    def w(self) -> int:
        return self.rows.shape[1]

    @property
    def bpp(self) -> int:
        fmt = self.fmt
        match fmt:
            case "mask1" | "rgb1":
                return 1
            case "rgb8":
                return 8
            case "bgr":
                return 24
            case "bgra":
                return 32
            case _:
                assert_never(fmt)


def _unmasked(fmt: DibFormat, rows: Pixels, palette: list[int] | None, interpolate: bool) -> DIB:
    """A DIB with no mask of its own (a mask, a stencil, or an image loaded without its mask)."""
    return DIB(fmt=fmt, rows=rows, palette=palette, mask=None, matte=NO_MATTE, interpolate=interpolate)


# ---------------------------------------------------------------------- colour spaces

Channels = tuple[Floats32, Floats32, Floats32, Mask]
"""GetRGB over an array of colours: r, g and b (float32, 0-1) and where the colour is valid
(GetRGBOrZerosOnError gives zeros where it is not)."""

DeviceFamily = Literal["DeviceGray", "DeviceRGB", "DeviceCMYK"]


def default_range(i: int) -> tuple[float, float]:
    """GetDefaultValue's (min, max) of component `i`: (0, 1) in every space an image loads here
    (the device spaces, and the bases an ICC or Indexed space reads through)."""
    return 0.0, 1.0


@dataclass(frozen=True, kw_only=True)
class DeviceCS:
    """CPDF_DeviceCS: one of the three stock spaces (`GRAY`, `RGB`, `CMYK`)."""
    family: DeviceFamily
    n: int

    def rgb(self, v: Floats32, std: bool) -> Channels:
        """GetRGB over (..., n) float32 values. `std` is IsStdConversionEnabled(), which only the
        CMYK conversion reads."""
        family = self.family
        match family:
            case "DeviceGray":
                g = np.clip(v[..., 0], F32(0), F32(1))
                return g, g, g, np.ones(g.shape, bool)
            case "DeviceRGB":
                c = np.clip(v[..., :3], F32(0), F32(1))
                return c[..., 0], c[..., 1], c[..., 2], np.ones(c.shape[:-1], bool)
            case "DeviceCMYK":
                return _cmyk_std_f(v) if std else _cmyk_rgb_f(v)
            case _:
                assert_never(family)


@dataclass(frozen=True, kw_only=True)
class IccCS:
    """CPDF_ICCBasedCS with `n` components: an sRGB profile (`srgb`: the first three components are
    the colour) or, with no transform ported, the device space it reads through (`base`)."""
    n: int
    srgb: bool
    base: DeviceCS

    @property
    def family(self) -> Literal["ICCBased"]:
        return "ICCBased"

    def rgb(self, v: Floats32, std: bool) -> Channels:
        """GetRGB; `std` is passed on to the base (CPDF_BasedCS::EnableStdConversion)."""
        if self.srgb:   # GetRGB hands the first three components back: no clamp, always valid
            c = v[..., :3].astype(F32)
            return c[..., 0], c[..., 1], c[..., 2], np.ones(c.shape[:-1], bool)
        if self.n == 1 and self.base.n > 1:
            v = np.repeat(v[..., :1], self.base.n, axis=-1)
        return self.base.rgb(v, std)


@dataclass(frozen=True, kw_only=True)
class IndexedCS:
    """CPDF_IndexedCS: one component, an index up to `max_index` into `lookup`, the base space's
    colours as bytes."""
    base: DeviceCS | IccCS
    max_index: int
    lookup: bytes

    @property
    def family(self) -> Literal["Indexed"]:
        return "Indexed"

    @property
    def n(self) -> int:
        return 1

    def rgb(self, v: Floats32, std: bool) -> Channels:
        """GetRGB: an index out of range or past the lookup's bytes is an error (not valid)."""
        x = v[..., 0]
        finite = np.isfinite(x) & (x > -2147483648.0) & (x < 2147483648.0)
        idx = np.where(finite, x, -1).astype(np.int64)
        n = self.base.n
        ok = (idx >= 0) & (idx <= self.max_index) & ((idx + 1) * n <= len(self.lookup))
        table = np.frombuffer(self.lookup, np.uint8)
        safe = np.where(ok, idx, 0)
        comps = np.zeros(idx.shape + (max(n, 1),), F32)
        for i in range(n):
            lo, hi = default_range(i)
            mx = F32(F(hi - lo))
            byte = table[np.minimum(safe * n + i, len(table) - 1)] if len(table) else np.zeros(idx.shape, np.uint8)
            comps[..., i] = F32(lo) + (mx * byte.astype(F32)) / F32(255)
        r, g, b, valid = self.base.rgb(comps, std)
        return r, g, b, valid & ok


CS = Union[DeviceCS, IccCS, IndexedCS]
"""The image-relevant part of a CPDF_ColorSpace: the spaces an image loads in."""


def _cmyk_rgb_f(v: Floats32) -> Channels:
    """CPDF_DeviceCS::GetRGB for CMYK without std conversion: AdobeCmykToStandardRgbF, each
    channel clamped, rounded to a byte with the 0.49999997f offset, looked up, times 1/255.f."""
    c = np.clip(np.nan_to_num(v[..., :4].astype(F32), nan=0.0), F32(0), F32(1))
    q = (c * F32(255) + F32(0.49999997)).astype(np.int64)
    table = adobe_cmyk_to_srgb_array(q.reshape(-1, 4))
    rgb = (table.astype(F32) * F32(1.0 / 255.0)).reshape(q.shape[:-1] + (3,))
    return rgb[..., 0], rgb[..., 1], rgb[..., 2], np.ones(q.shape[:-1], bool)


def _cmyk_std_f(v: Floats32) -> Channels:
    """CPDF_DeviceCS::GetRGB for CMYK with std conversion (the colours of an image loaded inside a
    soft mask, or of a /SMask stream): 1 - min(1, c + k) per channel, the components not normalised,
    and std::min(1.0f, x) keeping 1.0f where x is NaN. Always valid."""
    c = v[..., :4].astype(F32)
    k = c[..., 3]
    out: list[Floats32] = []
    for i in range(3):
        t = F32(1) - np.where(c[..., i] + k < F32(1), c[..., i] + k, F32(1))
        out.append(t.astype(F32))
    return out[0], out[1], out[2], np.ones(c.shape[:-1], bool)


GRAY = DeviceCS(family="DeviceGray", n=1)
RGB = DeviceCS(family="DeviceRGB", n=3)
CMYK = DeviceCS(family="DeviceCMYK", n=4)
_STOCK = {"DeviceGray": GRAY, "G": GRAY, "DeviceRGB": RGB, "RGB": RGB, "DeviceCMYK": CMYK, "CMYK": CMYK}
_BY_COMPONENTS = {1: GRAY, 3: RGB, 4: CMYK}


def load_cs(doc: PdfFile, obj: PdfObject, resources: PdfDict | None, depth: int) -> CS | None:
    """CPDF_DocPageData::GetColorSpace for an image: None when it doesn't load. `depth` counts the
    names and arrays followed to get here (0 for an image's own /ColorSpace)."""
    r = doc.resolve
    obj = r(obj)
    if depth > 8:
        return None
    if isinstance(obj, Name):
        name = str(obj)
        stock = _STOCK.get(name)
        if stock is not None:
            spaces = r(resources.get("ColorSpace")) if resources is not None else None
            if isinstance(spaces, dict) and any(k in spaces for k in ("DefaultGray", "DefaultRGB", "DefaultCMYK")):
                raise Unsupported("Default colour spaces")
            return stock
        if name == "Pattern":
            raise Unsupported("Pattern image colour spaces")
        if resources is None:
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


def _load_array(doc: PdfFile, arr: PdfArray, depth: int) -> CS | None:
    r = doc.resolve
    fam = r(arr[0]) if arr else None
    if not isinstance(fam, Name):
        return None
    f = str(fam)
    if f in ("Indexed", "I"):
        if len(arr) < 4:
            return None
        base = load_cs(doc, arr[1], None, depth + 1)
        if base is None or isinstance(base, IndexedCS):
            return None
        hi = r(arr[2])
        max_index = max(0, min(255, int(hi))) if isinstance(hi, (int, float)) and not isinstance(hi, bool) else 0
        lk = r(arr[3])
        lookup = b""
        if isinstance(lk, Stream):
            data = doc.stream_data(lk)
            decoders = FL.decoder_array(lk.dict, r)
            if decoders:
                last = decoders[-1][0]
                if FL.ABBREVIATIONS.get(last, last) not in ("FlateDecode", "LZWDecode", "ASCII85Decode",
                                                            "ASCIIHexDecode", "RunLengthDecode"):
                    raise Unsupported("an image-coded palette")
            lookup = data
        elif isinstance(lk, String):
            lookup = bytes(lk)
        return IndexedCS(base=base, max_index=max_index, lookup=lookup)
    if f == "ICCBased":
        st = r(arr[1]) if len(arr) > 1 else None
        if not isinstance(st, Stream):
            return None
        n = r(st.get("N"))
        if not isinstance(n, int) or isinstance(n, bool) or n not in (1, 3, 4):
            raise Unsupported("an ICC profile without a usable /N")
        data = doc.stream_data(st)
        if icc_srgb(data, n):
            # No lcms transform and no clamping: the alternate PDFium still loads is never read.
            return IccCS(n=3, srgb=True, base=RGB)
        if icc_openable(data):
            raise Unsupported("ICC profiles")
        device: DeviceCS | None = None
        if st.get("Alternate") is not None:
            alt = load_cs(doc, st.get("Alternate"), None, depth + 1)
            if alt is not None:
                if isinstance(alt, DeviceCS) and alt.n == n:
                    device = alt
                elif alt.n == n:
                    raise Unsupported("ICC alternates that are not device spaces")
        return IccCS(n=n, srgb=False, base=device if device is not None else _BY_COMPONENTS[n])
    if f[:4] in ("Devi", "CalG", "CalR", "Lab", "Sepa", "Patt", "Inde"):
        raise Unsupported(f"{f} image colour spaces")
    return None


def _is_cmyk(cs: CS) -> bool:
    """A DeviceCMYK image, or an ICC one read through DeviceCMYK."""
    return cs.family == "DeviceCMYK" or (isinstance(cs, IccCS) and cs.base.family == "DeviceCMYK")


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
        fast = FL.png_unfilter(raw, colors, 8, columns)
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


def _num(d: PdfDict, key: str, missing: int, r: FL.Resolve) -> int:
    """A number of `d` truncated to an int; `missing` when it has none (or something else)."""
    v = r(d.get(key))
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else missing


def _flate_lines(data: bytes, params: PdfDict, r: FL.Resolve, bpc: int, comps: int, width: int,
                 height: int) -> bytes:
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
    # any other predictor is none
    return raw


def _jpeg(data: bytes, params: PdfDict, r: FL.Resolve, width: int, height: int, comps: int,
          dev_size: tuple[int, int]) -> tuple[bytes, int, int] | None:
    """CJpegDecoder through Pillow's libjpeg (both ISLOW with fancy upsampling): the decoded
    bytes, width and height, or None when PDFium's decoder would not be created."""
    from PIL import Image, ImageFile, JpegImagePlugin
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
        if img.format != "JPEG" or not isinstance(img, JpegImagePlugin.JpegImageFile):
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
        raise Unsupported(f"a JPEG libjpeg complains about ({type(e).__name__})") from e


@dataclass(frozen=True, kw_only=True)
class ImageBytes:
    """What `image_bytes` decoded: the lines (`height` of the image's pitch), the size the decoder
    gives (a JPEG may come smaller), how many lines GetScanline finds data for (the rest are a
    zeroed buffer) and the last filter (None: the data as it was left)."""
    data: bytes
    width: int
    height: int
    present: int
    codec: str | None


def image_bytes(doc: PdfFile, d: PdfDict, raw: bytes, bpc: int, comps: int, width: int, height: int,
                dev_size: tuple[int, int]) -> ImageBytes | None:
    """CPDF_StreamAcc::LoadAllDataImageAcc + CreateDecoder; None when PDFium's load fails."""
    r = doc.resolve
    decoders = FL.decoder_array(d, r)
    if decoders is None:
        return None
    data = raw
    codec: str | None = None
    params: PdfDict = {}
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
                data = FL.lzw(data, FL.early_change(pr))
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
        except Exception as e:  # noqa: BLE001
            raise Unsupported("a filter that fails") from e
    if codec == "RunLengthDecode" and not _rl_dest_size_ok(data, bpc, comps, width, height):
        return None
    if not data:
        raise Unsupported("filters that decode to nothing")
    pitch = pitch8(bpc, comps, width)
    if codec == "FlateDecode":
        lines = _flate_lines(data, params, r, bpc, comps, width, height)
        return ImageBytes(data=_pad(lines, pitch, height), width=width, height=height, present=height, codec=codec)
    if codec == "RunLengthDecode":
        return ImageBytes(data=_pad(FL.run_length(data), pitch, height), width=width, height=height,
                          present=height, codec=codec)
    if codec == "DCTDecode":
        got = _jpeg(data, params, r, width, height, comps, dev_size)
        if got is None:
            return None
        out, w, h = got
        return ImageBytes(data=out, width=w, height=h, present=h, codec=codec)
    present = min(height, -(-len(data) // pitch)) if pitch else 0
    return ImageBytes(data=_pad(data, pitch, height), width=width, height=height, present=present, codec=codec)


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


def _bits(rows: Bytes, bpc: int, count: int) -> UInt32:
    """GetBits8 for `count` consecutive samples of every row."""
    if bpc == 8:
        return rows[:, :count].astype(np.uint32)
    if bpc == 16:
        b = rows[:, :count * 2].astype(np.uint32)
        return b[:, 0::2] * 256 + b[:, 1::2]
    bits = np.unpackbits(rows, axis=1)[:, :count * bpc].reshape(rows.shape[0], count, bpc).astype(np.uint32)
    weights = (1 << np.arange(bpc - 1, -1, -1)).astype(np.uint32)
    return (bits * weights).sum(axis=2).astype(np.uint32)


def _float_array(v: PdfObject, r: FL.Resolve) -> PdfArray | None:
    v = r(v)
    return v if isinstance(v, list) else None


def _get_float(arr: PdfArray | None, i: int, r: FL.Resolve) -> float:
    if arr is None or i >= len(arr):
        return 0.0
    x = r(arr[i])
    return F(float(x)) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def _get_int(arr: PdfArray | None, i: int, r: FL.Resolve) -> int:
    if arr is None or i >= len(arr):
        return 0
    x = r(arr[i])
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return 0
    return int(x)


def load(doc: PdfFile, stream: ImageStream, resources: PdfDict | None, dev_size: tuple[int, int], *,
         std_cs: bool, group_cmyk: bool) -> DIB | None:
    """CPDF_DIB::Load of an image to draw, with its mask when it has one.

    `std_cs` is StartLoadDIBBase's bStdCS: an image drawn inside a soft mask (and every /SMask or
    /Mask stream) has EnableStdConversion on while it loads, which is over again by the time its
    lines are translated, so it reaches LoadPalette and the /Matte colour and nothing else.
    `group_cmyk` is the other half of TransMask(): a luminosity mask whose group colour space is
    DeviceCMYK makes a DeviceCMYK image's lines the naive (1-c)(1-k) instead of Adobe's table."""
    return _load(doc, stream, resources, dev_size, is_mask=False, with_mask=True, std_cs=std_cs,
                 group_cmyk=group_cmyk)


def load_unmasked(doc: PdfFile, stream: ImageStream, resources: PdfDict | None,
                  dev_size: tuple[int, int]) -> DIB | None:
    """CPDF_DIB::Load of the image alone (no mask, no std conversion): what FPDFImageObj_GetBitmap
    gives."""
    return _load(doc, stream, resources, dev_size, is_mask=False, with_mask=False, std_cs=False,
                 group_cmyk=False)


def _load(doc: PdfFile, stream: ImageStream, resources: PdfDict | None, dev_size: tuple[int, int], *,
          is_mask: bool, with_mask: bool, std_cs: bool, group_cmyk: bool) -> DIB | None:
    """CPDF_DIB::Load (+ the mask, when the image has one and `with_mask`); `is_mask` loads an
    /SMask or /Mask stream (which never has one of its own)."""
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
    image_mask = r(d.get("ImageMask")) is True
    cs: CS | None = None        # None: an image mask (a stencil)
    decode = _float_array(d.get("Decode"), r)
    default_decode = True
    if image_mask or d.get("ColorSpace") is None:
        bpc, comps = 1, 1
        default_decode = decode is None or not _get_int(decode, 0, r)
    else:
        cs = load_cs(doc, d.get("ColorSpace"), resources, 0)
        if cs is None:
            return None
        comps = cs.n
        if last == "DCTDecode":
            bpc = 8
        if bpc not in (1, 2, 4, 8, 16):
            return None
        if _is_cmyk(cs) and is_mask:
            raise Unsupported("CMYK soft masks")
    got = image_bytes(doc, d, raw, bpc, comps, w, h, dev_size)
    if got is None:
        return None
    w, h, present = got.width, got.height, got.present
    pitch = pitch8(bpc, comps, w)
    rows = np.frombuffer(got.data, np.uint8)[:pitch * h].reshape(h, pitch) if pitch else np.zeros((h, 0), np.uint8)

    if cs is None:
        bits = np.unpackbits(rows, axis=1)[:, :w]
        if default_decode:
            bits = 1 - bits
        bits[present:] = 0
        return _unmasked("mask1", bits.astype(np.uint8), None, r(d.get("Interpolate")) is True)

    family = cs.family
    # GetDecodeAndMaskArray
    max_data = (1 << bpc) - 1
    comp_min: list[float] = []
    comp_step: list[float] = []
    for i in range(comps):
        lo, hi = default_range(i)
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
    trans_mask = group_cmyk and family == "DeviceCMYK"   # CPDF_DIB::TransMask()

    # LoadPalette
    palette: list[int] | None = None
    if bits == 1:
        if not (default_decode and family in ("DeviceGray", "DeviceRGB")) and cs.n <= 3:
            vals = np.array([[comp_min[0]] * 3, [F(comp_min[0] + comp_step[0])] * 3], F32)
            pr, pg, pb, ok = cs.rgb(vals, std_cs)
            cols = [argb(255, roundf(F(float(pr[k]) * 255)) if ok[k] else 0,
                         roundf(F(float(pg[k]) * 255)) if ok[k] else 0,
                         roundf(F(float(pb[k]) * 255)) if ok[k] else 0) for k in range(2)]
            if isinstance(cs, IndexedCS) and cs.max_index == 0:
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
        pr, pg, pb, ok = cs.rgb(vals, std_cs)
        palette = []
        for k in range(n):
            if ok[k]:
                palette.append(argb(255, roundf(float(F32(pr[k]) * F32(255))), roundf(float(F32(pg[k]) * F32(255))),
                                    roundf(float(F32(pb[k]) * F32(255)))))
            else:
                palette.append(argb(255, 0, 0, 0))

    # GetScanline
    fmt: DibFormat
    if bits == 1:
        b1 = np.unpackbits(rows, axis=1)[:, :w].astype(np.uint8)
        if not color_key:
            b1[present:] = 0
            fmt, pixels = "rgb1", b1
        else:
            set_v = 0 if key_max[0] == 1 else (palette[1] if palette else 0xFFFFFFFF)
            reset_v = 0 if key_min[0] == 0 else (palette[0] if palette else 0xFF000000)
            v = np.where(b1 == 1, np.uint32(set_v), np.uint32(reset_v)).astype("<u4")
            out = v.view(np.uint8).reshape(h, w, 4).copy()
            out[present:] = 0
            fmt, pixels, palette = "bgra", out, None
    elif bits <= 8:
        if bpc == 8:
            idx = rows[:, :w].astype(np.uint32)
        else:
            s = _bits(rows, bpc, w * comps).reshape(h, w, comps)
            idx = np.zeros((h, w), np.uint32)
            for c in range(comps):
                idx |= s[..., c] << (c * bpc)
        idx8 = (idx & 255).astype(np.uint8)
        if not color_key:
            idx8[present:] = 0
            fmt, pixels = "rgb8", idx8
        else:
            out = np.zeros((h, w, 4), np.uint8)
            if palette:
                pal = np.array(palette, np.uint32)[idx8]
                out[..., 0], out[..., 1], out[..., 2] = pal & 255, (pal >> 8) & 255, (pal >> 16) & 255
            else:
                out[..., 0] = out[..., 1] = out[..., 2] = idx8
            out[..., 3] = np.where((idx8 < key_min[0]) | (idx8 > key_max[0]), 255, 0)
            out[present:] = 0
            fmt, pixels, palette = "bgra", out, None
    else:
        bgr = _translate24(rows, cs, bpc, comps, w, h, default_decode, comp_min, comp_step, trans_mask)
        if color_key:
            s = _bits(rows, bpc, w * comps).reshape(h, w, comps)
            out_of = np.zeros((h, w), bool)
            for c in range(comps):
                out_of |= (s[..., c] < key_min[c]) | (s[..., c] > key_max[c])
            out = np.zeros((h, w, 4), np.uint8)
            out[..., :3] = bgr
            out[..., 3] = np.where(out_of, 255, 0)
            out[present:] = 0
            fmt, pixels, palette = "bgra", out, None
        else:
            bgr[present:] = 0
            fmt, pixels, palette = "bgr", bgr, None
    interpolate = r(d.get("Interpolate")) is True
    if is_mask or not with_mask:
        return _unmasked(fmt, pixels, palette, interpolate)

    # StartLoadMask
    matte = NO_MATTE
    smask = r(d.get("SMask"))
    mstream: Stream | None = None
    if isinstance(smask, Stream):
        mstream = smask
        matte_array = _float_array(smask.dict.get("Matte"), r)
        # (a Pattern space, which PDFium also leaves out here, never loads for an image)
        if matte_array is not None and len(matte_array) == comps and cs.n <= comps:
            vals = np.array([[_get_float(matte_array, i, r) for i in range(comps)]], F32)
            pr, pg, pb, ok = cs.rgb(vals, std_cs)     # StartLoadMask runs inside the std window
            if ok[0]:
                matte = argb(0, roundf(float(F32(pr[0]) * F32(255))), roundf(float(F32(pg[0]) * F32(255))),
                             roundf(float(F32(pb[0]) * F32(255))))
            else:
                matte = 0
    else:
        m = r(d.get("Mask"))
        if isinstance(m, Stream):
            mstream = m
    mask: DIB | None = None
    if mstream is not None:
        # StartLoadMaskDIB loads the mask with bStdCS true, whatever the image itself was loaded with
        mask = _load(doc, mstream, None, (0, 0), is_mask=True, with_mask=True, std_cs=True, group_cmyk=False)
        if mask is not None and mask.fmt not in ("rgb1", "rgb8", "mask1"):
            raise Unsupported("soft masks that are not gray")
    return DIB(fmt=fmt, rows=pixels, palette=palette, mask=mask, matte=matte, interpolate=interpolate)


def _translate24(rows: Bytes, cs: CS, bpc: int, comps: int, w: int, h: int, default_decode: bool,
                 comp_min: list[float], comp_step: list[float], trans_mask: bool) -> Pixels:
    """TranslateScanline24bpp: BGR bytes. Under TransMask() the CMYK lines are (1-c)(1-k) and no
    colour space is asked: the default-decode path writes that byte-wise through
    CPDF_DeviceCS::TranslateImageLine, which fills an FX_RGB_STRUCT laid over the BGR bytes in its
    own order, so the cyan channel lands where blue goes - the float path below does not."""
    out = np.zeros((h, w, 3), np.uint8)
    if default_decode:
        if cs.family != "DeviceRGB":
            if bpc == 8:
                if comps != cs.n:
                    raise Unsupported("a stale translation line")
                src = rows[:, :w * comps].reshape(h, w, comps)
                bf = cs.base.family if isinstance(cs, IccCS) else cs.family
                if bf == "DeviceGray":
                    out[..., 0] = out[..., 1] = out[..., 2] = src[..., 0]
                elif bf == "DeviceRGB":
                    out[...] = src[..., ::-1]
                elif bf == "DeviceCMYK":
                    if trans_mask:
                        px = src[..., :4].astype(np.int64)
                        k = 255 - px[..., 3]
                        for ch in range(3):
                            out[..., ch] = (((255 - px[..., ch]) * k) // 255).astype(np.uint8)
                        return out
                    rgb = adobe_cmyk_to_srgb_array(src[..., :4].reshape(-1, 4)).astype(np.uint8)
                    out[...] = rgb[:, ::-1].reshape(h, w, 3)
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
    if trans_mask:
        k = F32(1) - vals[..., 3]
        pr, pg, pb = (F32(1) - vals[..., 0]) * k, (F32(1) - vals[..., 1]) * k, (F32(1) - vals[..., 2]) * k
        ok = np.ones((h, w), bool)
    else:
        pr, pg, pb, ok = cs.rgb(vals, False)
    for k, ch in ((0, pb), (1, pg), (2, pr)):
        v = np.where(ok, np.clip(ch, F32(0), F32(1)), F32(0)).astype(F32)
        out[..., k] = (v * F32(255)).astype(np.uint8)
    return out
