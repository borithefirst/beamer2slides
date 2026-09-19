"""FreeType's charmaps and glyph names of an sfnt (TrueType) face, as PDFium asks for them.

A simple TrueType font's glyphs are found by CPDF_TrueTypeFont::LoadGlyphMap, which only talks to
FreeType: which charmaps the face has (FT_Face.charmaps, in cmap order), which one is active
(FT_Set_Charmap / FT_Select_Charmap, and the Unicode one FT_Open_Face picks), FT_Get_Char_Index
on it, and FT_Get_Name_Index over the `post` names. Each of those has rules a fontTools reading
does not follow, so this is a port of the FreeType PDFium pins (656cb777):

- sfnt/ttcmap.c `tt_face_build_cmaps`: every subtable whose format has a class is validated at
  FT_VALIDATE_DEFAULT and dropped when that fails (a format 0 shorter than 262 bytes, a format 4
  segment past the table, groups out of order...); overlapping format 4 segments are kept with a
  flag that changes the lookup; the lookups themselves (format 4's broken-last-segment rule,
  delta arithmetic modulo 65536, format 12's overflow guard);
- sfnt/sfobjs.c: the encoding each (platform, encoding) pair gets, and the Unicode charmap
  FreeType *synthesizes* from glyph names (psnames `ps_unicodes_init`) when the font has no
  Unicode or MS Symbol subtable but a `post` table with names;
- base/ftobjs.c: `find_unicode_charmap` (UCS-4 first, then any Unicode one, both from the end),
  FT_Set_Charmap refusing a format 14 subtable, FT_Get_Char_Index's `>= num_glyphs` guard;
- sfnt/ttpost.c + sfdriver.c: glyph names from `post` formats 1 (only with exactly 258 glyphs),
  2 and 2.5, `.notdef` for everything else, and the first glyph of a name wins.
"""
from __future__ import annotations

import struct

from . import psnames_data

# FT_Encoding values PDFium distinguishes (fxge::FontEncoding)
UNICODE, MS_SYMBOL, APPLE_ROMAN, NONE = "unicode", "ms_symbol", "apple_roman", "none"
SJIS, PRC, BIG5, WANSUNG, JOHAB = "sjis", "prc", "big5", "wansung", "johab"

_ENCODINGS = [   # sfnt_find_encoding: (platform, encoding or -1 for any)
    (2, -1, UNICODE), (0, -1, UNICODE), (1, 0, APPLE_ROMAN), (3, 0, MS_SYMBOL), (3, 10, UNICODE),
    (3, 1, UNICODE), (3, 2, SJIS), (3, 3, PRC), (3, 4, BIG5), (3, 5, WANSUNG), (3, 6, JOHAB),
]

UNSORTED, OVERLAPPING = 1, 2          # TT_CMAP_FLAG_*
VARIANT_BIT = 0x80000000


def find_encoding(platform: int, encoding: int) -> str:
    for p, e, enc in _ENCODINGS:
        if p == platform and e in (-1, encoding):
            return enc
    return NONE


class Invalid(Exception):
    """FT_INVALID_*: the validator's longjmp."""


def _u16(t: bytes, i: int) -> int:
    return (t[i] << 8) | t[i + 1]


def _s16(t: bytes, i: int) -> int:
    v = _u16(t, i)
    return v - 0x10000 if v & 0x8000 else v


def _u32(t: bytes, i: int) -> int:
    return struct.unpack_from(">I", t, i)[0]


# ---------------------------------------------------------------------- subtables


class CMap:
    """One FT_CharMap. `base` is the subtable's offset in `table` (the whole cmap table, whose
    end is the validator's limit)."""

    def __init__(self, platform: int, encoding_id: int, fmt: int, table: bytes, base: int, flags: int = 0):
        self.platform, self.encoding_id, self.format = platform, encoding_id, fmt
        self.encoding = find_encoding(platform, encoding_id)
        self.table, self.base, self.flags = table, base, flags

    def char_index(self, code: int, num_glyphs: int) -> int:
        """The class's char_index (FT_Get_Char_Index adds the num_glyphs guard)."""
        return getattr(self, f"_index{self.format}")(code & 0xFFFFFFFF, num_glyphs)

    # -- format 0
    def _index0(self, code: int, num_glyphs: int) -> int:
        return self.table[self.base + 6 + code] if code < 256 else 0

    # -- format 2
    def _index2(self, code: int, num_glyphs: int) -> int:
        t, b = self.table, self.base
        if code >= 0x10000:
            return 0
        lo, hi = code & 0xFF, code >> 8
        subs = b + 518
        if hi == 0:
            sub = subs
            if _u16(t, b + 6 + lo * 2) != 0:
                return 0
        else:
            sub = subs + (_u16(t, b + 6 + hi * 2) & ~7)
            if sub == subs:
                return 0
        start, count, delta, offset = _u16(t, sub), _u16(t, sub + 2), _s16(t, sub + 4), _u16(t, sub + 6)
        idx = (lo - start) & 0xFFFFFFFF
        if idx < count and offset != 0:
            g = _u16(t, sub + 6 + offset + 2 * idx)
            if g != 0:
                return (g + delta) & 0xFFFF
        return 0

    # -- format 4
    def _segment(self, i: int, n: int) -> tuple[int, int, int, int, int]:
        """(end, start, delta, offset, offset field position) of segment i of n."""
        t, b = self.table, self.base
        pos = b + 16 + 6 * n + 2 * i
        return _u16(t, b + 14 + 2 * i), _u16(t, b + 16 + 2 * n + 2 * i), _s16(t, b + 16 + 4 * n + 2 * i), \
            _u16(t, pos), pos

    def _index4(self, code: int, num_glyphs: int) -> int:
        if code >= 0x10000:
            return 0
        return self._map4_linear(code, num_glyphs) if self.flags & UNSORTED else self._map4_binary(code, num_glyphs)

    def _glyph4(self, code, start, end, delta, offset, r, i, n, num_glyphs) -> int | None:
        """The lookup once a segment holds the code; None for `continue` (linear search)."""
        limit = len(self.table)
        if i >= n - 1 and start == 0xFFFF and end == 0xFFFF:   # an incorrect last segment
            if offset and r + offset + 2 > limit:
                delta, offset = 1, 0
        if offset == 0xFFFF:
            return None
        if offset:
            g = _u16(self.table, r + offset + (code - start) * 2)
            if g:
                g = (g + delta) & 0xFFFF
                if g >= num_glyphs:
                    g = 0
            return g
        return (code + delta) & 0xFFFF

    def _map4_linear(self, code: int, num_glyphs: int) -> int:
        n = _u16(self.table, self.base + 6) >> 1
        for i in range(n):
            end, start, delta, offset, r = self._segment(i, n)
            if code < start:
                break
            if code <= end:
                g = self._glyph4(code, start, end, delta, offset, r, i, n, num_glyphs)
                if g is None:
                    continue
                return g
        return 0

    def _map4_binary(self, code: int, num_glyphs: int) -> int:
        n = _u16(self.table, self.base + 6) >> 1
        if not n:
            return 0
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) >> 1
            end, start, delta, offset, r = self._segment(mid, n)
            if code < start:
                hi = mid
            elif code > end:
                lo = mid + 1
            else:
                if mid >= n - 1 and start == 0xFFFF and end == 0xFFFF:
                    if offset and r + offset + 2 > len(self.table):
                        delta, offset = 1, 0
                if self.flags & OVERLAPPING:
                    # the first segment holding the code whose offset isn't 0xFFFF
                    top = mid
                    if offset == 0xFFFF:
                        mid = top + 1
                    i = top
                    while i > 0:
                        prev_end = _u16(self.table, self.base + 14 + (i - 1) * 2)
                        if code > prev_end:
                            break
                        end, start, delta, offset, r = self._segment(i - 1, n)
                        if offset != 0xFFFF:
                            mid = i - 1
                        i -= 1
                    if mid == top + 1:
                        if i != top:
                            end, start, delta, offset, r = self._segment(top, n)
                        mid = top
                        i = top + 1
                        while i < n:
                            next_start = _u16(self.table, self.base + 16 + 2 * n + 2 * i)
                            if code < next_start:
                                break
                            end, start, delta, offset, r = self._segment(i, n)
                            if offset != 0xFFFF:
                                mid = i
                            i += 1
                        i -= 1
                        if mid == top:
                            return 0     # "still no luck": gindex stays 0
                    if mid != i:
                        end, start, delta, offset, r = self._segment(mid, n)
                elif offset == 0xFFFF:
                    return 0
                if offset:
                    g = _u16(self.table, r + offset + (code - start) * 2)
                    if g:
                        g = (g + delta) & 0xFFFF
                        if g >= num_glyphs:
                            g = 0
                    return g
                return (code + delta) & 0xFFFF
        return 0

    # -- format 6
    def _index6(self, code: int, num_glyphs: int) -> int:
        t, b = self.table, self.base
        start, count = _u16(t, b + 6), _u16(t, b + 8)
        idx = (code - start) & 0xFFFFFFFF
        return _u16(t, b + 10 + 2 * idx) if idx < count else 0

    # -- format 8
    def _index8(self, code: int, num_glyphs: int) -> int:
        t, b = self.table, self.base
        p = b + 8204
        for k in range(_u32(t, p)):
            start, end, start_id = struct.unpack_from(">III", t, p + 4 + 12 * k)
            if code < start:
                break
            if code <= end:
                if start_id > 0xFFFFFFFF - (code - start):
                    return 0
                return start_id + code - start
        return 0

    # -- format 10
    def _index10(self, code: int, num_glyphs: int) -> int:
        t, b = self.table, self.base
        start, count = _u32(t, b + 12), _u32(t, b + 16)
        if code < start:
            return 0
        idx = code - start
        return _u16(t, b + 20 + 2 * idx) if idx < count else 0

    # -- formats 12 and 13
    def _groups(self, code: int) -> tuple[int, int, int] | None:
        t, b = self.table, self.base
        n = _u32(t, b + 12)
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) >> 1
            start, end, value = struct.unpack_from(">III", t, b + 16 + 12 * mid)
            if code < start:
                hi = mid
            elif code > end:
                lo = mid + 1
            else:
                return start, end, value
        return None

    def _index12(self, code: int, num_glyphs: int) -> int:
        g = self._groups(code)
        if g is None:
            return 0
        start, _, start_id = g
        return 0 if start_id > 0xFFFFFFFF - (code - start) else start_id + code - start

    def _index13(self, code: int, num_glyphs: int) -> int:
        g = self._groups(code)
        return 0 if g is None else g[2]

    # -- format 14 (variation selectors): "This can't happen"
    def _index14(self, code: int, num_glyphs: int) -> int:
        return 0


def _validate(fmt: int, t: bytes, b: int) -> int:
    """tt_cmapN_validate at FT_VALIDATE_DEFAULT: the flags, or Invalid."""
    limit = len(t)
    if fmt == 0:
        if b + 4 > limit:
            raise Invalid
        length = _u16(t, b + 2)
        if b + length > limit or length < 262:
            raise Invalid
        return 0
    if fmt == 2:
        if b + 4 > limit:
            raise Invalid
        length = _u16(t, b + 2)
        if b + length > limit or length < 6 + 512:
            raise Invalid
        max_subs = max(_u16(t, b + 6 + 2 * k) >> 3 for k in range(256))
        subs = b + 518
        glyph_ids = subs + (max_subs + 1) * 8
        if glyph_ids > limit:
            raise Invalid
        for k in range(max_subs + 1):
            p = subs + 8 * k
            count, offset = _u16(t, p + 2), _u16(t, p + 6)
            if count == 0:
                continue
            if offset != 0:
                ids = p + 6 + offset
                if ids < glyph_ids or ids + count * 2 > b + length:
                    raise Invalid
        return 0
    if fmt == 4:
        if b + 4 > limit:
            raise Invalid
        length = limit - b         # too long or too short: both corrected to the table's end
        if length < 16:
            raise Invalid
        n = _u16(t, b + 6) // 2
        if length < 16 + n * 2 * 4:
            raise Invalid
        glyph_ids = b + 16 + 8 * n
        flags = 0
        last_start = last_end = 0
        for i in range(n):
            end, start = _u16(t, b + 14 + 2 * i), _u16(t, b + 16 + 2 * n + 2 * i)
            offset_pos = b + 16 + 6 * n + 2 * i
            offset = _u16(t, offset_pos)
            if start > end:
                raise Invalid
            if start <= last_end and i > 0:
                flags |= UNSORTED if last_start > start or last_end > end else OVERLAPPING
            if offset and offset != 0xFFFF:
                p = offset_pos + offset
                if i != n - 1 or not (start == 0xFFFF and end == 0xFFFF):
                    if p < glyph_ids or p + (end - start + 1) * 2 > limit:
                        raise Invalid
            elif offset == 0xFFFF:
                if i != n - 1 or not (start == 0xFFFF and end == 0xFFFF):
                    raise Invalid
            last_start, last_end = start, end
        return flags
    if fmt == 6:
        if b + 10 > limit:
            raise Invalid
        length, count = _u16(t, b + 2), _u16(t, b + 8)
        if b + length > limit or length < 10 + count * 2:
            raise Invalid
        return 0
    if fmt == 8:
        if b + 16 + 8192 > limit:
            raise Invalid
        length = _u32(t, b + 4)
        if length > limit - b or length < 8192 + 16:
            raise Invalid
        p = b + 12 + 8192
        n = _u32(t, p)
        p += 4
        if n > (limit - p) // 12:
            raise Invalid
        last = 0
        for k in range(n):
            start, end, _ = struct.unpack_from(">III", t, p + 12 * k)
            if start > end or (k > 0 and start <= last):
                raise Invalid
            last = end
        return 0
    if fmt == 10:
        if b + 20 > limit:
            raise Invalid
        length, count = _u32(t, b + 4), _u32(t, b + 16)
        if length > limit - b or length < 20 or (length - 20) // 2 < count:
            raise Invalid
        return 0
    if fmt in (12, 13):
        if b + 16 > limit:
            raise Invalid
        length, n = _u32(t, b + 4), _u32(t, b + 12)
        if length > limit - b or length < 16 or (length - 16) // 12 < n:
            raise Invalid
        last = 0
        for k in range(n):
            start, end, _ = struct.unpack_from(">III", t, b + 16 + 12 * k)
            if start > end or (k > 0 and start <= last):
                raise Invalid
            last = end
        return 0
    if fmt == 14:
        if b + 10 > limit:
            raise Invalid
        length, n = _u32(t, b + 2), _u32(t, b + 6)
        if length > limit - b or length < 10 or (length - 10) // 11 < n:
            raise Invalid
        last_sel, p = 1, b + 10
        for _ in range(n):
            sel = (t[p] << 16) | _u16(t, p + 1)
            default, nondefault = _u32(t, p + 3), _u32(t, p + 7)
            p += 11
            if default >= length or nondefault >= length:
                raise Invalid
            if sel < last_sel:
                raise Invalid
            last_sel = sel + 1
            if default:
                q = b + default
                if q + 4 > limit:
                    raise Invalid
                ranges = _u32(t, q)
                q += 4
                if ranges > (limit - q) // 4:
                    raise Invalid
                last_base = 0
                for _ in range(ranges):
                    base, cnt = (t[q] << 16) | _u16(t, q + 1), t[q + 3]
                    q += 4
                    if base + cnt >= 0x110000 or base < last_base:
                        raise Invalid
                    last_base = base + cnt + 1
            if nondefault:
                q = b + nondefault
                if q + 4 > limit:
                    raise Invalid
                maps = _u32(t, q)
                q += 4
                if maps > (limit - q) // 5:
                    raise Invalid
                last_uni = 0
                for _ in range(maps):
                    uni = (t[q] << 16) | _u16(t, q + 1)
                    q += 5
                    if uni >= 0x110000 or uni < last_uni:
                        raise Invalid
                    last_uni = uni + 1
        return 0
    raise KeyError(fmt)   # no class: the subtable is skipped


def build_cmaps(table: bytes) -> list[CMap]:
    """tt_face_build_cmaps."""
    out: list[CMap] = []
    if len(table) < 4:
        return out
    n = _u16(table, 2)
    p = 4
    while n > 0 and p + 8 <= len(table):
        platform, encoding_id, offset = _u16(table, p), _u16(table, p + 2), _u32(table, p + 4)
        p += 8
        n -= 1
        if offset and offset <= len(table) - 2:
            fmt = _u16(table, offset)
            try:
                flags = _validate(fmt, table, offset)
            except (Invalid, KeyError, IndexError, struct.error):
                # IndexError: a read past the buffer; FreeType's reads are bounded by the checks
                # above, so this only happens where it would have read memory it doesn't own
                continue
            out.append(CMap(platform, encoding_id, fmt, table, offset, flags))
    return out


# ---------------------------------------------------------------------- glyph names -> Unicode (psnames)


def ps_unicode_value(name: str) -> int:
    """psnames `ps_unicode_value`: uniXXXX / uXXXX[XX] (upper-case hex only), then the Adobe Glyph
    List for the name up to its first dot; VARIANT_BIT marks a suffix (`A.swash`)."""
    def hex_digits(s: str, most: int) -> tuple[int, int]:
        value = n = 0
        while n < most and n < len(s) and ("0" <= s[n] <= "9" or "A" <= s[n] <= "F"):
            value = (value << 4) + int(s[n], 16)
            n += 1
        return value, n

    if name.startswith("uni"):
        value, n = hex_digits(name[3:], 4)
        if n == 4:
            rest = name[7:]
            if not rest:
                return value
            if rest[0] == ".":
                return value | VARIANT_BIT
    if name.startswith("u"):
        value, n = hex_digits(name[1:], 6)
        if n >= 4:
            rest = name[1 + n:]
            if not rest:
                return value
            if rest[0] == ".":
                return value | VARIANT_BIT
    dot = name.find(".", 1)     # a non-initial dot: `.notdef` is looked up whole (and isn't there)
    base = name if dot < 0 else name[:dot]
    value = psnames_data.AGL.get(base, 0)
    return value | VARIANT_BIT if dot >= 0 else value


_EXTRA = ((0x0394, "Delta"), (0x03A9, "Omega"), (0x2215, "fraction"), (0x00AD, "hyphen"),
          (0x02C9, "macron"), (0x03BC, "mu"), (0x2219, "periodcentered"), (0x00A0, "space"),
          (0x021A, "Tcommaaccent"), (0x021B, "tcommaaccent"))


def msvc_qsort(a: list, gt, eq) -> None:
    """The UCRT's qsort (qsort.cpp), in place: a quicksort with median-of-three pivots, an explicit
    stack, the smaller side first, and `shortsort` (select the maximum, swap it to the end) below
    CUTOFF = 8 elements. `gt(x, y)` is compare(x, y) > 0, `eq(x, y)` compare(x, y) == 0."""
    if len(a) < 2:
        return
    stack = []
    lo, hi = 0, len(a) - 1
    while True:
        size = hi - lo + 1
        if size <= 8:
            h = hi
            while h > lo:                        # shortsort
                m = lo
                for p in range(lo + 1, h + 1):
                    if gt(a[p], a[m]):
                        m = p
                a[m], a[h] = a[h], a[m]
                h -= 1
        else:
            mid = lo + (size // 2)
            if gt(a[lo], a[mid]):
                a[lo], a[mid] = a[mid], a[lo]
            if gt(a[lo], a[hi]):
                a[lo], a[hi] = a[hi], a[lo]
            if gt(a[mid], a[hi]):
                a[mid], a[hi] = a[hi], a[mid]
            loguy, higuy = lo, hi
            while True:
                if mid > loguy:
                    loguy += 1
                    while loguy < mid and not gt(a[loguy], a[mid]):
                        loguy += 1
                if mid <= loguy:
                    loguy += 1
                    while loguy <= hi and not gt(a[loguy], a[mid]):
                        loguy += 1
                higuy -= 1
                while higuy > mid and gt(a[higuy], a[mid]):
                    higuy -= 1
                if higuy < loguy:
                    break
                a[loguy], a[higuy] = a[higuy], a[loguy]
                if mid == higuy:
                    mid = loguy
            higuy += 1
            if mid < higuy:
                higuy -= 1
                while higuy > mid and eq(a[higuy], a[mid]):
                    higuy -= 1
            if mid >= higuy:
                higuy -= 1
                while higuy > lo and eq(a[higuy], a[mid]):
                    higuy -= 1
            if higuy - lo >= hi - loguy:
                if lo < higuy:
                    stack.append((lo, higuy))
                if loguy < hi:
                    lo = loguy
                    continue
            else:
                if loguy < hi:
                    stack.append((loguy, hi))
                if lo < higuy:
                    hi = higuy
                    continue
        if not stack:
            return
        lo, hi = stack.pop()


class PsUnicodes:
    """psnames' synthesized Unicode charmap (ps_unicodes_init / ps_unicodes_char_index)."""

    def __init__(self, names):
        maps: list[tuple[int, int]] = []
        states = [0] * len(_EXTRA)
        extra = [0] * len(_EXTRA)
        for n, name in enumerate(names):
            if not name:
                continue
            for k, (_, extra_name) in enumerate(_EXTRA):
                if name == extra_name:
                    if states[k] == 0:
                        states[k], extra[k] = 1, n
                    break
            u = ps_unicode_value(name)
            if u & ~VARIANT_BIT:
                for k, (extra_u, _) in enumerate(_EXTRA):
                    if u == extra_u:
                        states[k] = 2
                        break
                maps.append((u, n))
        for k, (extra_u, _) in enumerate(_EXTRA):
            if states[k] == 1:
                maps.append((extra_u, extra[k]))
        # compare_uni_maps: by base value, a base glyph before its variants. Two glyphs with one
        # value compare equal, and which of them the search meets is up to ft_qsort = the C
        # library's qsort, which on Windows is the UCRT's (not stable)
        msvc_qsort(maps, lambda a, b: (a[0] & ~VARIANT_BIT, a[0]) > (b[0] & ~VARIANT_BIT, b[0]),
                   lambda a, b: (a[0] & ~VARIANT_BIT, a[0]) == (b[0] & ~VARIANT_BIT, b[0]))
        self.maps = maps

    def __bool__(self) -> bool:
        return bool(self.maps)

    def char_index(self, u: int) -> int:
        maps = self.maps
        lo, hi = 0, len(maps)
        mid = lo + ((hi - lo) >> 1)
        result = None
        while lo < hi:
            value = maps[mid][0]
            if value == u:
                result = mid
                break
            base = value & ~VARIANT_BIT
            if base == u:
                result = mid
            if base < u:
                lo = mid + 1
            else:
                hi = mid
            mid += (u - base) & 0xFFFFFFFF    # "reasonable prediction"; unsigned, so never back
            if mid >= hi or mid < lo:
                mid = lo + ((hi - lo) >> 1)
        return maps[result][1] if result is not None else 0


class SynthCMap:
    """The Unicode charmap sfobjs.c adds from glyph names, reported as (3, 1)."""

    platform, encoding_id, format, encoding, flags = 3, 1, -1, UNICODE, 0

    def __init__(self, unicodes: PsUnicodes):
        self.unicodes = unicodes

    def char_index(self, code: int, num_glyphs: int) -> int:
        return self.unicodes.char_index(code & 0xFFFFFFFF)


class AdobeCMap:
    """A bare CFF program's own encoding as a charmap (cffcmap.c, platform 7 = TT_PLATFORM_ADOBE:
    0 standard, 1 expert, 2 custom); `gids` gives each code below 256 its glyph."""

    platform, format, flags = 7, -1, 0

    def __init__(self, encoding_id: int, gids: list[int]):
        self.encoding_id, self.gids = encoding_id, gids
        self.encoding = ("adobe_standard", "adobe_expert", "adobe_custom", "adobe_latin1")[encoding_id]

    def char_index(self, code: int, num_glyphs: int) -> int:
        return self.gids[code] if code < 256 else 0


# ---------------------------------------------------------------------- the face


class FaceError(Exception):
    """FT_Open_Face fails: PDFium drops the program and substitutes a font."""


def _read(data: bytes, pos: int, n: int) -> bytes:
    """FT_STREAM_READ / FT_FRAME_ENTER: all `n` bytes at `pos` or an error (never bounded by a
    table's length, only by the stream's size)."""
    if pos < 0 or pos + n > len(data):
        raise FaceError(f"cannot read {n} bytes at {pos}")
    return data[pos:pos + n]


def font_dir(data: bytes, font_number: int = 0) -> list[tuple[bytes, int, int]]:
    """tt_face_load_font_dir (sfnt/ttload.c): FreeType's (tag, offset, length) for one font of the
    file, in directory order. check_table_dir first (not for OTTO): entries that can't be read end
    the directory, an entry past the stream is ignored, and every `head` entry must be at least
    0x36 bytes and some `head` must be there. Then an entry starting past the stream or running
    past it is dropped (hmtx/vmtx are cut to a multiple of 4 instead) and of two entries with one
    tag the first wins - a zero-length one included, which tt_face_lookup_table then calls missing."""
    base = 0
    if data[:4] == b"ttcf":
        head = _read(data, 0, 12)
        if font_number >= _u32(head, 8):
            raise FaceError("no such font in the collection")
        base = _u32(_read(data, 12 + 4 * font_number, 4), 0)
    head = _read(data, base, 12)
    tag, num_tables = head[:4], _u16(head, 4)
    size = len(data)
    if tag != b"OTTO":
        has_head, valid = False, 0
        for nn in range(num_tables):
            at = base + 12 + 16 * nn
            if at + 16 > size:
                num_tables = nn
                break
            t, _, offset, length = struct.unpack_from(">4sIII", data, at)
            if offset > size:
                continue
            if length > size - offset and t not in (b"hmtx", b"vmtx"):
                continue
            valid += 1
            if t == b"head":
                has_head = True
                if length < 0x36:
                    raise FaceError("head too small")
                _read(data, offset + 12, 4)             # the magic number, only traced
        if not valid:
            raise FaceError("no valid tables")
        if not has_head:                                # no SING+META either: this port reads no bitmaps
            raise FaceError("no head table")
    elif not num_tables:
        raise FaceError("no tables")
    raw = _read(data, base + 12, 16 * num_tables)
    out: list[tuple[bytes, int, int]] = []
    for nn in range(num_tables):
        t, _, offset, length = struct.unpack_from(">4sIII", raw, 16 * nn)
        if offset > size:
            continue
        if length > size - offset:
            if t not in (b"hmtx", b"vmtx"):
                continue
            length = (size - offset) & ~3
        if any(e[0] == t for e in out):
            continue
        out.append((t, offset, length))
    if not out:
        raise FaceError("no valid tables")
    return out


class Face:
    """FT_Face of an sfnt as PDFium's LoadGlyphMap uses it.

    An OpenType font with a `CFF ` table goes through cffobjs.c after sfnt_load_face: `cff` is then
    (charset names, CID-keyed?, Adobe encoding id or None when the encoding maps no code, code ->
    glyph for codes < 256). Its glyph names are the charset's, the sfnt loader's Unicode charmap
    (from `post` names) may already be there, and the CFF driver adds its own from the charset
    when no (3, 1) or Apple Unicode subtable is, then the Adobe encoding charmap."""

    def __init__(self, data: bytes, cff: tuple | None = None, font_number: int = 0):
        self.data = data
        self.dir = font_dir(data, font_number)
        self.format_tag = _read(data, _u32(data, 12 + 4 * font_number) if data[:4] == b"ttcf" else 0, 4)
        self._open(data, cff is not None)
        post = self.table(b"post")
        self._post = data[post[0]:post[0] + post[1]] if post else b""
        self._post_end = len(data) - post[0] if post else len(data)   # bytes from `post` to the file's end
        # tt_face_load_post reads its 32-byte header from the stream, not from the table: a shorter
        # table followed by other data still loads (and load_post_names then finds no names)
        self.post_format = _u32(data, post[0]) if post and post[0] + 32 <= len(data) else None
        self._cff_names = None
        # sfnt_load_face: FT_FACE_FLAG_GLYPH_NAMES when tt_face_load_post succeeded (formats 1, 2,
        # 2.5 and 3) and the format isn't 3
        self.has_glyph_names = self.post_format in (0x00010000, 0x00020000, 0x00025000)
        cmap = self.table(b"cmap")
        self.charmaps: list = build_cmaps(data[cmap[0]:cmap[0] + cmap[1]] if cmap else b"")
        if not any(c.encoding in (UNICODE, MS_SYMBOL) for c in self.charmaps) and self.has_glyph_names:
            unicodes = PsUnicodes(self._post_names())
            if unicodes:            # else No_Unicode_Glyph_Name: no charmap
                self.charmaps.append(SynthCMap(unicodes))
        if cff is not None:
            names, cid_keyed, encoding_id, gids = cff
            self.num_glyphs = len(names)
            if not cid_keyed:
                self.has_glyph_names = True
                self._cff_names = names
            if not any((c.platform, c.encoding_id) == (3, 1) or c.platform == 0 for c in self.charmaps):
                unicodes = PsUnicodes(names if not cid_keyed else [])
                if unicodes:
                    self.charmaps.append(SynthCMap(unicodes))
            if encoding_id is not None:
                self.charmaps.append(AdobeCMap(encoding_id, gids))
        self.charmap = None
        self.select_unicode()       # FT_Open_Face: the Unicode charmap by default

    # -- opening (sfnt_load_face, tt_face_init)
    def table(self, tag: bytes) -> tuple[int, int] | None:
        """tt_face_lookup_table: (offset, length) of the table, None when missing or empty."""
        for t, offset, length in self.dir:
            if t == tag and length:
                return offset, length
        return None

    def _open(self, data: bytes, cff: bool) -> None:
        """sfnt_load_face's checks, then tt_face_init's for a glyf font. Raises FaceError where
        FT_Open_Face fails. Every read is from the stream at the table's offset: a table shorter
        than its header is read on into whatever follows it."""
        self.has_outline = any(self.table(t) for t in (b"glyf", b"CFF ", b"CFF2"))
        if self.table(b"CBLC") or self.table(b"CBDT"):
            self.has_outline = False
        head = self.table(b"head")
        if not head:
            raise FaceError("head missing")
        h = _read(data, head[0], 54)
        self.units_per_em = _u16(h, 18)
        self.head_bbox = tuple(_s16(h, 36 + 2 * k) for k in range(4))
        self.index_to_loc_format = _s16(h, 50)
        self.head_flags = _u16(h, 16)
        if not 16 <= self.units_per_em <= 16384:
            raise FaceError("units per em")
        # tt_face_load_maxp: its error is ignored, numGlyphs stays 0 when the first 6 bytes fail
        self.maxp: dict[str, int] = {"numGlyphs": 0}
        maxp = self.table(b"maxp")
        if maxp and maxp[0] + 6 <= len(data):
            version = struct.unpack_from(">i", data, maxp[0])[0]
            self.maxp = {"version": version, "numGlyphs": _u16(data, maxp[0] + 4)}
            if version >= 0x10000 and maxp[0] + 32 <= len(data):
                names = ("maxPoints maxContours maxCompositePoints maxCompositeContours maxZones "
                         "maxTwilightPoints maxStorage maxFunctionDefs maxInstructionDefs maxStackElements "
                         "maxSizeOfInstructions maxComponentElements maxComponentDepth").split()
                self.maxp.update({n: _u16(data, maxp[0] + 6 + 2 * k) for k, n in enumerate(names)})
                self.maxp["maxFunctionDefs"] = max(64, self.maxp["maxFunctionDefs"])
                self.maxp["maxTwilightPoints"] = min(0xFFFF - 4, self.maxp["maxTwilightPoints"])
        self.maxp_num_glyphs = self.num_glyphs = self.maxp["numGlyphs"]
        # tt_face_load_hhea / hmtx: both needed ('true' Mac fonts may lack hhea: no outlines then)
        self.hhea = None
        hhea = self.table(b"hhea")
        if hhea:
            self.hhea = self._metrics_header(hhea[0])
            if not self.table(b"hmtx"):
                raise FaceError("hmtx missing")
            self.hmtx = self.table(b"hmtx")
        elif self.format_tag == b"true":
            self.has_outline = False
        else:
            raise FaceError("hhea missing")
        self.vhea = self.vmtx = None
        vhea = self.table(b"vhea")
        if vhea:
            self.vhea = self._metrics_header(vhea[0])
            self.vmtx = self.table(b"vmtx")
        self.os2 = self._load_os2(data)
        # sfnt_load_face's metrics: OS/2's typo metrics when USE_TYPO_METRICS says so, else hhea's,
        # and when those are both 0 OS/2's typo metrics, else its win metrics
        os2 = self.os2
        if os2 and os2["fsSelection"] & 128:
            self.ascender, self.descender = os2["sTypoAscender"], os2["sTypoDescender"]
        else:
            self.ascender = self.hhea["ascender"] if self.hhea else 0
            self.descender = self.hhea["descender"] if self.hhea else 0
            if not (self.ascender or self.descender) and os2:
                if os2["sTypoAscender"] or os2["sTypoDescender"]:
                    self.ascender, self.descender = os2["sTypoAscender"], os2["sTypoDescender"]
                else:
                    self.ascender = _s16(struct.pack(">H", os2["usWinAscent"]), 0)
                    self.descender = -_s16(struct.pack(">H", os2["usWinDescent"]), 0)
        self.num_locations, self.loca, self.glyf = 0, b"", (0, 0)
        self.cvt = self.fpgm = self.prep = b""
        if cff:
            return
        if self.format_tag not in (b"\0\1\0\0", b"\0\2\0\0", b"true"):
            raise FaceError("not a TrueType font")
        # tt_face_init loads loca only for a scalable face, and sfnt_load_face makes a face with
        # neither outlines nor bitmaps scalable ("it has only empty glyphs then"): PDFium's FreeType
        # refuses a glyf font without loca even when glyf is missing too, and draws empty glyphs when
        # only glyf is (measured, FPDFFont_GetIsEmbedded on fonts with either table renamed). Bitmap
        # tables aren't read here, so loca is loaded for every face
        self._load_loca(data)
        for name in ("cvt ", "fpgm", "prep"):
            t = self.table(name.encode())
            if t:
                setattr(self, name.strip(), data[t[0]:t[0] + t[1]])

    def _load_os2(self, data: bytes) -> dict[str, int] | None:
        """tt_face_load_os2: a 78-byte frame, then 8 more bytes from version 1, 10 from 2, 4 from 5.
        Any read that fails makes the table missing (FreeType's version 0xFFFF): None."""
        t = self.table(b"OS/2")
        if not t:
            return None
        pos = t[0]
        if pos + 78 > len(data):
            return None
        version = _u16(data, pos)
        extra = 8 if version >= 1 else 0
        extra += 10 if version >= 2 else 0
        extra += 4 if version >= 5 else 0
        if pos + 78 + extra > len(data) or version == 0xFFFF:
            return None             # a table that says 0xFFFF is as missing as one FreeType refused
        return {"version": version, "fsType": _u16(data, pos + 8), "fsSelection": _u16(data, pos + 62),
                "sTypoAscender": _s16(data, pos + 68), "sTypoDescender": _s16(data, pos + 70),
                "sTypoLineGap": _s16(data, pos + 72), "usWinAscent": _u16(data, pos + 74),
                "usWinDescent": _u16(data, pos + 76)}

    def _metrics_header(self, at: int) -> dict[str, int]:
        h = _read(self.data, at, 36)
        return {"ascender": _s16(h, 4), "descender": _s16(h, 6), "line_gap": _s16(h, 8),
                "advance_max": _u16(h, 10), "number_of_metrics": _u16(h, 34)}

    def _load_loca(self, data: bytes) -> None:
        """tt_face_load_loca (truetype/ttpload.c): a loca shorter than maxp asks for is read further
        when the bytes up to the next table allow it, else the face has fewer glyphs; one longer
        than 0x10000 entries is cut."""
        self.glyf = self.table(b"glyf") or (0, 0)
        loca = self.table(b"loca")
        if not loca:
            raise FaceError("loca missing")
        pos, table_len = loca
        shift = 2 if self.index_to_loc_format != 0 else 1
        table_len = min(table_len, 0x10000 << shift)
        self.num_locations = table_len >> shift
        if self.num_locations < self.num_glyphs + 1:
            new_len = (self.num_glyphs + 1) << shift
            after = [o - pos for _, o, _ in self.dir if o - pos > 0]
            dist = min(after) if after else len(data) - pos
            if new_len <= dist:
                self.num_locations, table_len = self.num_glyphs + 1, new_len
            else:
                self.num_glyphs = self.num_locations - 1 if self.num_locations else 0
        self.loca = _read(data, pos, table_len)

    def location(self, index: int) -> tuple[int, int]:
        """tt_face_get_location: (offset in the file, size) of a glyph's glyf data; size 0 for an
        empty glyph or broken location data."""
        pos1 = pos2 = 0
        n = self.num_locations
        if index < n:
            if self.index_to_loc_format != 0:
                pos1 = pos2 = _u32(self.loca, 4 * index)
                if 4 * index + 8 <= 4 * n:
                    pos2 = _u32(self.loca, 4 * index + 4)
            else:
                pos1 = pos2 = _u16(self.loca, 2 * index)
                if 2 * index + 4 <= 2 * n:
                    pos2 = _u16(self.loca, 2 * index + 2)
                pos1, pos2 = pos1 << 1, pos2 << 1
        glyf_offset, glyf_len = self.glyf
        if pos1 > glyf_len:
            return 0, 0
        if pos2 > glyf_len:
            if index != n - 2:
                return 0, 0
            pos2 = glyf_len
        # an unordered loca only bounds the size (and a missing glyf gives a wrong, non-zero one)
        return glyf_offset + pos1, pos2 - pos1 if pos2 >= pos1 else glyf_len - pos1

    def metrics(self, index: int, vertical: bool = False) -> tuple[int, int]:
        """tt_face_get_metrics: (advance, side bearing) in font units."""
        header, table = (self.vhea, self.vmtx) if vertical else (self.hhea, getattr(self, "hmtx", None))
        if not header or not table:
            return 0, 0
        pos, size = table
        end, k, data = pos + size, header["number_of_metrics"], self.data
        if k == 0:
            return 0, 0
        if index < k:
            pos += 4 * index
            if pos + 4 > end or pos + 4 > len(data):
                return 0, 0
            return _u16(data, pos), _s16(data, pos + 2)
        pos += 4 * (k - 1)
        if pos + 2 > end or pos + 2 > len(data):
            return 0, 0
        advance = _u16(data, pos)
        pos += 4 + 2 * (index - k)
        bearing = _s16(data, pos) if pos + 2 <= end and pos + 2 <= len(data) else 0
        return advance, bearing

    # -- glyph names (tt_face_get_ps_name, or the CFF charset)
    def glyph_names(self) -> list[str]:
        if getattr(self, "_glyph_names", None) is None:
            self._glyph_names = self._cff_names if self._cff_names is not None else self._post_names()
        return self._glyph_names

    def glyph_name(self, index: int) -> str:
        """FT_Get_Glyph_Name: '' without glyph names or past the face's last glyph (the post names
        run to maxp's count, the face may have fewer after the loca check)."""
        names = self.glyph_names() if self.has_glyph_names else []
        return names[index] if 0 <= index < min(len(names), self.num_glyphs) else ""

    def _post_names(self) -> list[str]:
        n = self.maxp_num_glyphs          # load_post_names counts maxp's glyphs, not the face's
        mac = psnames_data.MAC_NAMES
        names = [".notdef"] * n
        post, fmt = self._post, self.post_format
        if fmt == 0x00010000:
            if n == 258:
                names = list(mac)
            return names
        if fmt not in (0x00020000, 0x00025000):
            return names
        if len(post) < 34:
            return names
        count = _u16(post, 32)
        if count > n or count == 0:
            return names
        post_len = len(post) - 34
        if fmt == 0x00020000:
            if count * 2 > post_len:
                return names
            indices = [_u16(post, 34 + 2 * k) for k in range(count)]
            num_names = max(indices)
            num_names = num_names - 257 if num_names > 257 else 0
            if num_names and 34 + 2 * count >= self._post_end:
                # FT_STREAM_READ of the strings, even of 0 bytes, fails at the end of the file
                # (FT_Stream_ReadAt: pos >= size), and the whole name table with it
                return names
            # load_format_20 turns the Pascal strings into C strings in place: each length byte
            # becomes the previous name's terminator, so the last name loaded runs on into whatever
            # follows it up to the next 0 byte or the table's end ("B" followed by an unused
            # "\x04a205" is the name "B\x04a205")
            buf = bytearray(post[34 + 2 * count:]) + b"\0"
            p_end = len(buf) - 1
            starts: list[int] = []
            p = 0
            while p < p_end and len(starts) < num_names:
                length = buf[p]
                buf[p] = 0
                starts.append(p + 1)
                p += 1 + length
            starts += [p_end] * (num_names - len(starts))
            strings = [bytes(buf[s:buf.index(0, s)]).decode("latin-1") for s in starts]
            for k, idx in enumerate(indices):
                names[k] = mac[idx] if idx < 258 else strings[idx - 258]
        else:
            if count > post_len or count > 258 + 128:
                return names
            for k in range(count):
                d = post[34 + k]
                idx = k + (d - 256 if d > 127 else d)
                names[k] = mac[idx if 0 <= idx <= 257 else 0]
        return names

    def name_index(self, name: str) -> int:
        """FT_Get_Name_Index (sfnt_get_name_index): the first glyph of that name below the face's
        glyph count, else 0."""
        if not self.has_glyph_names or name is None:
            return 0
        if getattr(self, "_name_first", None) is None:
            first: dict[str, int] = {}
            for i, n in enumerate(self.glyph_names()[:self.num_glyphs]):
                first.setdefault(n, i)
            self._name_first = first
        return self._name_first.get(name, 0)

    # -- charmaps
    def char_index(self, code: int) -> int:
        """FT_Get_Char_Index on the active charmap."""
        if self.charmap is None:
            return 0
        g = self.charmap.char_index(code, self.num_glyphs)
        return 0 if g >= self.num_glyphs else g

    def set_charmap(self, index: int) -> None:
        """FT_Set_Charmap (CFX_Face::SetCharMapByIndex): refused for a format 14 subtable."""
        if self.charmaps[index].format != 14:
            self.charmap = self.charmaps[index]

    def select_unicode(self) -> bool:
        """FT_Select_Charmap(FT_ENCODING_UNICODE) = find_unicode_charmap."""
        for c in reversed(self.charmaps):
            if c.encoding == UNICODE and ((c.platform, c.encoding_id) in ((3, 10), (0, 4))
                                          or (c.platform, c.encoding_id, c.format) == (0, 6, 13)):
                self.charmap = c
                return True
        for c in reversed(self.charmaps):
            if c.encoding == UNICODE:
                self.charmap = c
                return True
        return False

    def ids(self) -> list[tuple[int, int]]:
        return [(c.platform, c.encoding_id) for c in self.charmaps]
