"""Stream filter parameters as PDFium reads them (`pdf/pure/filters.py`): /DecodeParms values are
read with GetIntegerFor, so a real is truncated and /EarlyChange is a boolean. Each page's text is
compared with PDFium's, character for character."""

from __future__ import annotations

import dataclasses
import zlib

import pytest

from beamer2slides import pdf
from beamer2slides.pdf.api import Char


def _lzw(data: bytes, early: int) -> bytes:
    """LZWDecode's encoder: a clear code, 9- to 12-bit codes whose width grows one code before the
    table needs it when `early` is 1 (as a decoder with that /EarlyChange reads them), then EOD."""
    table = {bytes([i]): i for i in range(256)}
    codes: list[int] = [256]
    w = b""
    for byte in data:
        wc = w + bytes([byte])
        if wc in table:
            w = wc
            continue
        codes.append(table[w])
        table[wc] = len(table) + 2     # 256 and 257 are the clear and end codes
        w = bytes([byte])
    if w:
        codes.append(table[w])
    codes.append(257)
    out = bytearray()
    acc, bits = 0, 0
    for k, code in enumerate(codes):
        # the decoder's table after the codes before this one: 258 entries, one more per code after
        # the first (the clear code resets it)
        size = 258 + max(0, k - 2) + early
        width = 9 if size < 512 else 10 if size < 1024 else 11 if size < 2048 else 12
        acc = (acc << width) | code
        bits += width
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if bits:
        out.append((acc << (8 - bits)) & 0xFF)
    return bytes(out)


def _page(content: bytes, stream_dict: bytes) -> bytes:
    """A one-page PDF drawing `content` (already encoded as `stream_dict` says) in Helvetica."""
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 5 0 R >> >>"
               b" /Contents 4 0 R >>",
            4: b"<< /Length %d %s >>\nstream\n" % (len(content), stream_dict) + content + b"\nendstream",
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for n, body in objs.items():
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for n in range(1, 6):
        out += b"%010d 00000 n \n" % offsets[n]
    return bytes(out + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref)


def _text(lines: int) -> bytes:
    """Content drawing `lines` lines of words that repeat just enough for LZW to fill its table
    past 512 entries (a 10-bit code) and not past 4096."""
    words = [b"alpha", b"bravo", b"charlie", b"delta", b"echo", b"foxtrot", b"golf", b"hotel", b"india",
             b"juliet", b"kilo", b"lima", b"mike", b"november", b"oscar", b"papa"]
    out = [b"BT /F1 9 Tf 20 780 Td 11 TL"]
    for i in range(lines):
        picked = b" ".join(words[(i * 7 + j * j * 3) % len(words)] + b"%d" % ((i * j) % 13) for j in range(8))
        out.append(b"(%s) '" % picked)
    out.append(b"ET")
    return b"\n".join(out)


def _rows(content: bytes, columns: int) -> bytes:
    """`content` as PNG-predicted rows of `columns` bytes, each with filter byte 0 (None) and the
    last padded with spaces."""
    padded = content + b" " * (-len(content) % columns)
    return b"".join(b"\0" + padded[i:i + columns] for i in range(0, len(padded), columns))


def _chars(backend: str, data: bytes) -> list[tuple[object, ...]]:
    doc = pdf.resolve(backend).open(data)
    try:
        chars: list[Char] = doc[0].chars()
        return [dataclasses.astuple(dataclasses.replace(c, font_id=-1)) for c in chars]
    finally:
        doc.close()


def _cases() -> dict[str, bytes]:
    text = _text(60)
    return {
        # FlateOrLZWDecode's bEarlyChange is `!!GetIntegerFor("EarlyChange", 1)`: a 2 is a 1
        "lzw_early_change_2": _page(_lzw(text, 1), b"/Filter /LZWDecode /DecodeParms << /EarlyChange 2 >>"),
        "lzw_early_change_0": _page(_lzw(text, 0), b"/Filter /LZWDecode /DecodeParms << /EarlyChange 0 >>"),
        "lzw_early_change_default": _page(_lzw(text, 1), b"/Filter /LZWDecode"),
        # a real /Columns is truncated (GetIntegerFor): the rows decode, short ones (numpy) and long
        # ones (the Pillow path) alike
        "flate_png_real_columns": _page(zlib.compress(_rows(text[:1500], 40)),
                                        b"/Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 40.0 >>"),
        "flate_png_real_columns_long": _page(zlib.compress(_rows(text, 40)),
                                             b"/Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 40.7 >>"),
    }


CASES = _cases()


@pytest.mark.parametrize("name", CASES)
def test_filter_parameters_are_read_as_pdfium_reads_them(name: str) -> None:
    data = CASES[name]
    want = _chars("pdfium", data)
    assert len(want) > 1000, "PDFium drew the page's words"
    assert _chars("pure", data) == want
