"""Stream filters (ISO 32000-1, 7.4): Flate with predictors, LZW, ASCII85, ASCIIHex, RunLength.
Image codecs (DCT, JPX, CCITT, JBIG2) are left encoded: `decode` stops at the first of them and
says which it was."""

from __future__ import annotations

import zlib

import numpy as np

from .syntax import Name, Stream

IMAGE_CODECS = {"DCTDecode", "JPXDecode", "CCITTFaxDecode", "JBIG2Decode"}
ABBREVIATIONS = {"AHx": "ASCIIHexDecode", "A85": "ASCII85Decode", "LZW": "LZWDecode", "Fl": "FlateDecode",
                 "RL": "RunLengthDecode", "CCF": "CCITTFaxDecode", "DCT": "DCTDecode"}


class FilterError(Exception):
    pass


def flate(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error:
        # a damaged or truncated stream: keep what decodes, up to the byte zlib stops at, as
        # FlateUncompress does (inflate's output before its error is kept)
        d = zlib.decompressobj()
        out = bytearray()
        for i in range(0, len(data), 4096):
            saved = d.copy()
            try:
                out += d.decompress(data[i:i + 4096])
            except zlib.error:
                d = saved
                for k in range(i, min(i + 4096, len(data))):
                    try:
                        out += d.decompress(data[k:k + 1])
                    except zlib.error:
                        break
                break
        if not out:
            try:  # raw deflate without the zlib header
                return zlib.decompress(data, -15)
            except zlib.error:
                pass
        return bytes(out)


def lzw(data: bytes, early: int = 1) -> bytes:
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    bits, pos, nbits = 0, 0, 9
    buf = 0
    prev = b""
    n = len(data)
    i = 0
    while True:
        while bits < nbits:
            if i >= n:
                return bytes(out)
            buf = (buf << 8) | data[i]
            i += 1
            bits += 8
        code = (buf >> (bits - nbits)) & ((1 << nbits) - 1)
        bits -= nbits
        if code == 256:
            table = table[:258]
            nbits = 9
            prev = b""
            continue
        if code == 257:
            break
        if code < len(table):
            entry = table[code]
            if prev:
                table.append(prev + entry[:1])
        elif prev:
            entry = prev + prev[:1]
            table.append(entry)
        else:
            break
        out += entry
        prev = entry
        size = len(table) + early
        if size >= 4096:
            nbits = 12
        elif size >= 2048:
            nbits = 12
        elif size >= 1024:
            nbits = 11
        elif size >= 512:
            nbits = 10
    return bytes(out)


def ascii85(data: bytes) -> bytes:
    data = bytes(c for c in data if c not in b" \t\n\r\x0c\x00")
    if data.startswith(b"<~"):
        data = data[2:]
    end = data.find(b"~>")
    if end >= 0:
        data = data[:end]
    out = bytearray()
    group: list[int] = []
    for c in data:
        if c == ord("z") and not group:
            out += b"\0\0\0\0"
            continue
        if not 33 <= c <= 117:
            continue
        group.append(c - 33)
        if len(group) == 5:
            v = 0
            for g in group:
                v = v * 85 + g
            out += (v & 0xFFFFFFFF).to_bytes(4, "big")
            group = []
    if group:
        k = len(group)
        group += [84] * (5 - k)
        v = 0
        for g in group:
            v = v * 85 + g
        out += (v & 0xFFFFFFFF).to_bytes(4, "big")[:k - 1]
    return bytes(out)


def ascii_hex(data: bytes) -> bytes:
    end = data.find(b">")
    if end >= 0:
        data = data[:end]
    digits = bytes(c for c in data if c in b"0123456789abcdefABCDEF")
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode())


def run_length(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        length = data[i]
        if length == 128:
            break
        if length < 128:
            out += data[i + 1:i + 2 + length]
            i += length + 2
        else:
            out += data[i + 1:i + 2] * (257 - length)
            i += 2
    return bytes(out)


def predict(data: bytes, parms: dict) -> bytes:
    predictor = parms.get("Predictor", 1) or 1
    if predictor == 1:
        return data
    colors = parms.get("Colors", 1) or 1
    bpc = parms.get("BitsPerComponent", 8) or 8
    columns = parms.get("Columns", 1) or 1
    bpp = max(1, colors * bpc // 8)
    row = (colors * bpc * columns + 7) // 8
    if predictor == 2:  # TIFF: horizontal differencing (8-bit components)
        if bpc != 8:
            return data
        a = np.frombuffer(data[:len(data) // row * row], np.uint8).reshape(-1, row).reshape(-1, columns, colors)
        return np.cumsum(a, axis=1, dtype=np.uint8).tobytes()
    # PNG predictors: a filter byte per row
    stride = row + 1
    rows = len(data) // stride
    out = np.zeros((rows, row), np.uint8)
    prev = np.zeros(row, np.int32)
    src = np.frombuffer(data[:rows * stride], np.uint8).reshape(rows, stride)
    for r in range(rows):
        kind = src[r, 0]
        line = src[r, 1:].astype(np.int32)
        if kind == 0:
            cur = line
        elif kind == 1:  # Sub
            cur = line.copy()
            for i in range(bpp, row, bpp):
                cur[i:i + bpp] = (cur[i:i + bpp] + cur[i - bpp:i]) & 255
        elif kind == 2:  # Up
            cur = (line + prev) & 255
        elif kind == 3:  # Average
            cur = line.copy()
            for i in range(row):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif kind == 4:  # Paeth
            cur = line.copy()
            for i in range(row):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                cur[i] = (line[i] + pred) & 255
        else:
            cur = line
        out[r] = cur
        prev = cur.astype(np.int32)
    return out.tobytes()


_PNG_TYPES = {1: 0, 2: 4, 3: 2, 4: 6}  # components -> PNG colour type (the bytes are what matter)


def _png_unfilter(compressed: bytes, raw: bytes, parms: dict) -> bytes | None:
    """PNG-predicted Flate data is a PNG's IDAT: let Pillow undo the filters (a Paeth row in Python
    costs a second per megapixel). None when the layout is not one PNG can hold."""
    import struct
    import io
    colors = parms.get("Colors", 1) or 1
    bpc = parms.get("BitsPerComponent", 8) or 8
    columns = parms.get("Columns", 1) or 1
    if colors not in _PNG_TYPES or bpc != 8 or len(raw) < 2048:
        return None
    stride = colors * columns + 1
    rows = len(raw) // stride
    if rows < 1 or len(raw) != rows * stride:
        return None

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", columns, rows, 8, _PNG_TYPES[colors], 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw, 1) if raw is not None else compressed) + chunk(b"IEND", b"")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(png)) as img:
            return img.tobytes()
    except Exception:  # noqa: BLE001 - a row filter PNG does not know: the slow path decides
        return None


_PIPELINE = {"FlateDecode", "Fl", "LZWDecode", "LZW", "ASCII85Decode", "A85", "ASCIIHexDecode", "AHx",
             "RunLengthDecode", "RL"}


def _params_dict(p, resolve) -> dict:
    """CPDF_Object::GetDict: a dictionary, or a stream's; anything else is none."""
    p = resolve(p)
    if isinstance(p, dict):
        return p
    return p.dict if isinstance(p, Stream) else {}


def decoder_array(d: dict, resolve=lambda v: v) -> list[tuple[str, dict]] | None:
    """GetDecoderArray: (filter name, parameters) pairs; None when /Filter is not a name or an
    array of names (and, for several filters, only the last one may be something other than
    Flate, LZW, ASCII85, ASCIIHex or RunLength: ValidateDecoderPipeline)."""
    names = resolve(d.get("Filter"))
    if names is None:
        return []
    params = resolve(d.get("DecodeParms"))
    if isinstance(names, list):
        names = [resolve(n) for n in names]
        if not all(isinstance(n, Name) for n in names):
            return None
        if len(names) > 1 and any(str(n) not in _PIPELINE for n in names[:-1]):
            return None
        plist = params if isinstance(params, list) else None
        return [(str(n), _params_dict(plist[i], resolve) if plist is not None and i < len(plist) else {})
                for i, n in enumerate(names)]
    if not isinstance(names, Name):
        return None
    return [(str(names), _params_dict(params, resolve) if params is not None else {})]


def decode(data: bytes, d: dict, resolve=lambda v: v) -> tuple[bytes, str | None]:
    """CPDF_StreamAcc::LoadAllDataFiltered over PDF_DataDecode: the stream's bytes through its
    filters, up to an image codec; (bytes, codec or None). Any name PDFium doesn't decode itself
    counts as an image codec. The bytes as stored are what comes back when the filters can't be
    read, a filter fails, or nothing was decoded before a codec (or the result is empty)."""
    decoders = decoder_array(d, resolve)
    if not decoders:
        return data, None
    out = b""
    current = data
    for name, p in decoders:
        if name == "Crypt":
            continue
        name = ABBREVIATIONS.get(name, name)
        p = {k: resolve(v) for k, v in p.items()}
        try:
            if name == "FlateDecode":
                raw = flate(current)
                current = (_png_unfilter(current, raw, p) if (p.get("Predictor") or 1) >= 10 else None) \
                    or predict(raw, p)
            elif name == "LZWDecode":
                current = predict(lzw(current, p.get("EarlyChange", 1)), p)
            elif name == "ASCII85Decode":
                current = ascii85(current)
            elif name == "ASCIIHexDecode":
                current = ascii_hex(current)
            elif name == "RunLengthDecode":
                current = run_length(current)
            else:
                return (out or data), name
        except Exception:  # noqa: BLE001 - PDFium's decoder gives up (FX_INVALID_OFFSET): the stored bytes
            return data, None
        out = current
    return (out or data), None
