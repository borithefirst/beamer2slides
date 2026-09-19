"""Fonts as PDFium sees them: character codes, widths, glyph boxes and Unicode.

What the text layer needs from a font is small - how a string splits into codes, each code's
width and glyph box (in 1/1000 em, integers, as PDFium keeps them) and its Unicode - but *which*
glyph a code names is a chain of rules that decides whether a box is right. This module follows
PDFium (core/fpdfapi/font) rule for rule, citing the function each piece comes from:

- simple fonts (Type1, TrueType, Type3): /Widths, the font descriptor, the PDF encoding
  (`CPDF_SimpleFont::LoadPDFEncoding`), the glyph map (`CPDF_Type1Font::LoadGlyphMap`) and
  the metrics fallbacks (`CPDF_Font::CheckFontMetrics`);
- CID fonts (Type0): the CMap (Identity-H/V and embedded CMaps), /W and /DW, CID -> glyph;
- ToUnicode CMaps (`CPDF_ToUnicodeMap`, with its strict entry counts).

Glyph boxes come from the font program through fontTools (Type 1, CFF, CID-keyed CFF,
TrueType): FreeType's unscaled control box, normalised to 1/1000 em by `normalize_metric`.
Without fontTools a program cannot be read and glyph boxes are empty (widths and Unicode still
work: they come from the PDF), which is what PDFium does for a font it cannot load either."""

from __future__ import annotations

import io
import math
import os
import re
import sys
from pathlib import Path
from typing import Callable

from . import sfnt
from .encodings import NAMES, UNICODES
from .syntax import Name, Stream, String, float32, operations

# PDFium's FontEncoding values, by the table names in encodings.py
BUILTIN, STANDARD, WINANSI, MACROMAN, MACEXPERT, PDFDOC, SYMBOL, ZAPF, MSSYMBOL = (
    "builtin", "kStandardEncoding", "kAdobeWinAnsiEncoding", "kMacRomanEncoding", "kMacExpertEncoding",
    "kPDFDocEncoding", "kAdobeSymbolEncoding", "kZapfEncoding", "kMSSymbolEncoding")
_NAME_TABLES = {STANDARD: "kStandardEncodingNames", WINANSI: "kAdobeWinAnsiEncodingNames",
                MACROMAN: "kMacRomanEncodingNames", MACEXPERT: "kMacExpertEncodingNames",
                PDFDOC: "kPDFDocEncodingNames", SYMBOL: "kAdobeSymbolEncodingNames", ZAPF: "kZapfEncodingNames"}
_PREDEFINED = {"WinAnsiEncoding": WINANSI, "MacRomanEncoding": MACROMAN, "MacExpertEncoding": MACEXPERT,
               "PDFDocEncoding": PDFDOC}

FLAG_FIXED, FLAG_SYMBOLIC, FLAG_NONSYMBOLIC, FLAG_ITALIC, FLAG_ALLCAPS = 1, 4, 32, 64, 65536
FLAG_USE_EXTERN_ATTR = 0x80000   # kFontUseExternAttr: the descriptor's weight and angle are to be believed
NOTDEF = ".notdef"
NO_GLYPH = None   # PDFium's 0xffff: no glyph at all (not even .notdef)
INVALID_CODE = 0xFFFFFFFF  # CPDF_Font::kInvalidCharCode


def _cdiv(a: int, b: int) -> int:
    """C++ integer division (towards zero)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def normalize_metric(value: float, upem: int) -> int:
    """fx_font.cpp NormalizeFontMetric: font units -> 1/1000 em. `upem / 2` is an integer
    division and the result is truncated towards zero, as the C++ cast does."""
    if not upem:
        return int(value)
    return int((value * 1000.0 + upem // 2) / upem)


def round_half_away(x: float) -> int:
    """FXSYS_roundf."""
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


def _int(v, default=0) -> int:
    """CPDF_Object::GetInteger: numbers truncate, anything else is the default."""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return int(v)
    return default


def _num(v, default=0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


# ---------------------------------------------------------------------- Adobe glyph names

def unicode_from_adobe_name(name: str) -> int:
    """UnicodeFromAdobeName: FreeType's ps_unicode_value (psnames: uniXXXX and uXXXX[XX] in
    upper-case hex, then its own glyph list up to the first non-initial dot), without the variant
    bit and cut to Windows' 16-bit wchar_t. 0 when unknown."""
    return sfnt.ps_unicode_value(name) & 0xFFFF if name else 0


def _predefined_name(encoding: str, code: int) -> str | None:
    """CharNameFromPredefinedCharSet."""
    table = NAMES.get(_NAME_TABLES.get(encoding, ""))
    if table is None:
        return None
    first = 24 if encoding == PDFDOC else 32
    if code < first or code - first >= len(table):
        return None
    return table[code - first]


def _predefined_unicode(encoding: str, code: int) -> int:
    if encoding == PDFDOC:
        from .document import _PDFDOC
        return _PDFDOC.get(code, code) if code >= 0x18 else 0
    table = UNICODES.get(encoding)
    return table[code] if table and 0 <= code < 256 else 0


# ---------------------------------------------------------------------- CMaps

_CMAP_WORD = re.compile(rb"[\x00\t\n\x0c\r ]*(?:%[^\r\n]*[\r\n\x00\t\x0c ]*)*"
                        rb"(<<|>>|<[^>]*>|\((?:\\.|[^\\)])*\)|[\[\]{}]|/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*"
                        rb"|[^\x00\t\n\x0c\r ()<>\[\]{}/%]+)", re.S)


def cmap_words(data: bytes) -> list[bytes]:
    """CPDF_SimpleParser's words: hex strings kept whole with their brackets."""
    return [m.group(1) for m in _CMAP_WORD.finditer(data)]


def _string_to_code(word: bytes) -> int | None:
    """CPDF_ToUnicodeMap::StringToCode."""
    if len(word) <= 2 or word[:1] != b"<" or word[-1:] != b">":
        return None
    code = 0
    for c in word[1:-1]:
        if c in b"\x00\t\n\x0c\r ":
            continue
        if chr(c) not in "0123456789ABCDEFabcdef":
            return None
        code = code * 16 + int(chr(c), 16)
        if code > 0xFFFFFFFF:
            return None
    return code


def _string_to_units(word: bytes) -> list[int]:
    """StringToWideString: groups of 4 hex digits, one UTF-16 unit each."""
    if len(word) <= 2 or word[:1] != b"<" or word[-1:] != b">":
        return []
    out, ch, n = [], 0, 0
    for c in word[1:-1]:
        if c in b"\x00\t\n\x0c\r ":
            continue
        if chr(c) not in "0123456789ABCDEFabcdef":
            break
        ch = ch * 16 + int(chr(c), 16)
        n += 1
        if n == 4:
            out.append(ch)
            ch, n = 0, 0
    return out


def _string_data_add(units: list[int]) -> list[int]:
    """StringDataAdd: the next string of a multi-unit bfrange (16-bit carry)."""
    out, value = [], 1
    for u in reversed(units):
        ch = (u + value) & 0xFFFF
        if ch < u:
            out.insert(0, 0)
        else:
            out.insert(0, ch)
            value = 0
    if value:
        out.insert(0, value)
    return out


def _to_int(word: bytes) -> int:
    """StringToInt: leading sign and digits, 0 otherwise."""
    m = re.match(rb"[+-]?\d+", word or b"")
    return int(m.group(0)) if m else 0


class ToUnicodeMap:
    """CPDF_ToUnicodeMap: code -> UTF-16 units. Entries are stored the way PDFium stores them
    (one unit, or 0xffff plus an index into the multi-unit strings), because duplicate codes
    keep the *smaller* stored value."""

    LIMIT, BF_LIMIT = 0xFFFF, 160000

    def __init__(self, data: bytes):
        self.map: dict[int, int] = {}
        self.reverse: dict[int, int] = {}
        self.multi: list[list[int]] = []
        words = cmap_words(data)
        i, previous = 0, b""
        while i < len(words):
            word = words[i]
            i += 1
            if word == b"beginbfchar":
                i, word = self._bfchar(words, i, previous)
            elif word == b"beginbfrange":
                i, word = self._bfrange(words, i, previous)
            previous = word

    def _insert(self, code: int, dest: int) -> None:
        self.map[code] = min(self.map.get(code, dest), dest)
        self.reverse[dest] = min(self.reverse.get(dest, code), code)

    def _set(self, code: int, units: list[int]) -> None:
        if not units:
            return
        if len(units) == 1:
            self._insert(code, units[0])
        else:
            self._insert(code, len(self.multi) * 0x10000 + 0xFFFF)
            self.multi.append(units)

    def _bfchar(self, words, i, previous):
        count = _to_int(previous)
        valid = 0 <= count <= self.BF_LIMIT
        entries = []
        word = b""
        while i < len(words):
            word = words[i]
            i += 1
            if word == b"endbfchar":
                break
            if not valid:
                continue
            code = _string_to_code(word)
            if code is None or code > self.LIMIT:
                valid = False
                continue
            word = words[i] if i < len(words) else b""
            i += 1
            entries.append((code, word))
            if len(entries) > count:
                valid = False
        else:
            word = b""
        if valid and len(entries) == count:
            for code, dest in entries:
                self._set(code, _string_to_units(dest))
        return i, word

    def _bfrange(self, words, i, previous):
        count = _to_int(previous)
        valid = 0 <= count <= self.BF_LIMIT
        ranges = []
        word = b""

        def take():
            nonlocal i
            w = words[i] if i < len(words) else b""
            i += 1
            return w

        while i < len(words):
            word = take()
            if word == b"endbfrange":
                break
            if not valid:
                continue
            low = _string_to_code(word)
            if low is None:
                valid = False
                continue
            word = take()
            high = _string_to_code(word)
            if high is None:
                valid = False
                continue
            high = (low & 0xFFFFFF00) | (high & 0xFF)
            if low > self.LIMIT or high > self.LIMIT or low > high:
                valid = False
                continue
            word = take()
            if word == b"[":
                ranges.append(("array", low, [take() for _ in range(low, high + 1)]))
                if len(ranges) > count:
                    valid = False
                    continue
                word = take()
                if word != b"]":
                    valid = False
                continue
            dest = _string_to_units(word)
            if len(dest) == 1:
                ranges.append(("single", low, high, dest[0]))
            else:
                strings = [dest]
                for _ in range(low + 1, high + 1):
                    strings.append(_string_data_add(strings[-1]))
                ranges.append(("multi", low, strings))
            if len(ranges) > count:
                valid = False
        else:
            word = b""
        if valid and len(ranges) == count:
            for r in ranges:
                if r[0] == "array":
                    for k, w in enumerate(r[2]):
                        self._set(r[1] + k, _string_to_units(w))
                elif r[0] == "single":
                    value = r[3]
                    for code in range(r[1], r[2] + 1):
                        self._insert(code, value)
                        value += 1
                else:
                    for k, units in enumerate(r[2]):
                        self._insert(r[1] + k, len(self.multi) * 0x10000 + 0xFFFF)
                        self.multi.append(units)
        return i, word

    def lookup(self, code: int) -> list[int]:
        """UTF-16 units; [] when the map has nothing for the code."""
        value = self.map.get(code)
        if value is None:
            return []
        if value & 0xFFFF != 0xFFFF:
            return [value & 0xFFFF]
        index = value >> 16
        return list(self.multi[index]) if index < len(self.multi) else []

    def reverse_lookup(self, unit: int) -> int:
        return self.reverse.get(unit, 0)


class CMap:
    """A CID font's encoding: how a string splits into codes and each code's CID. Identity-H/V
    and embedded CMaps (codespace ranges, cidrange, cidchar); a predefined CMap other than
    Identity is read as Identity (none of the PDFs this pipeline sees use one)."""

    def __init__(self, name: str = "Identity-H", data: bytes | None = None):
        self.vertical = name.endswith("-V")
        self.spaces: list[tuple[int, int, int]] = [(2, 0, 0xFFFF)]   # (bytes, low, high)
        self.cids: dict[int, int] | None = None                    # None: identity
        self.ranges: list[tuple[int, int, int]] = []
        if data is not None:
            self._parse(data)

    def _parse(self, data: bytes) -> None:
        words = cmap_words(data)
        spaces, cids, ranges = [], {}, []
        i = 0
        while i < len(words):
            w = words[i]
            i += 1
            if w == b"/WMode" and i < len(words):
                self.vertical = _to_int(words[i]) == 1
            elif w == b"begincodespacerange":
                while i + 1 < len(words) and words[i] != b"endcodespacerange":
                    lo, hi = words[i], words[i + 1]
                    i += 2
                    a, b = _string_to_code(lo), _string_to_code(hi)
                    if a is not None and b is not None:
                        spaces.append((max(1, len(re.sub(rb"\s", b"", lo)) - 2) // 2, a, b))
            elif w == b"begincidrange":
                while i + 2 < len(words) and words[i] != b"endcidrange":
                    a, b, cid = _string_to_code(words[i]), _string_to_code(words[i + 1]), _to_int(words[i + 2])
                    i += 3
                    if a is not None and b is not None:
                        ranges.append((a, b, cid))
            elif w == b"begincidchar":
                while i + 1 < len(words) and words[i] != b"endcidchar":
                    a = _string_to_code(words[i])
                    if a is not None:
                        cids[a] = _to_int(words[i + 1])
                    i += 2
        if spaces:
            self.spaces = spaces
        self.cids, self.ranges = cids, ranges

    def codes(self, data: bytes) -> list[int]:
        out, i = [], 0
        while i < len(data):
            for size, lo, hi in sorted(self.spaces):
                if i + size <= len(data):
                    code = int.from_bytes(data[i:i + size], "big")
                    if lo <= code <= hi:
                        out.append(code)
                        i += size
                        break
            else:  # no codespace matches: the shortest one's bytes
                size = min(s for s, _, _ in self.spaces)
                out.append(int.from_bytes(data[i:i + size], "big"))
                i += size
        return out

    def char_size(self, code: int) -> int:
        """Bytes of a code (CPDF_CMap::GetCharSize): the codespace that holds it."""
        for size, lo, hi in sorted(self.spaces):
            if lo <= code <= hi:
                return size
        return 1

    def cid(self, code: int) -> int:
        if self.cids is None:
            return code & 0xFFFF
        if code in self.cids:
            return self.cids[code]
        for lo, hi, start in self.ranges:
            if lo <= code <= hi:
                return (start + code - lo) & 0xFFFF
        return 0


# ---------------------------------------------------------------------- font programs


def _ft_bbox(bbox):
    """A Type 1 / CFF FontBBox as FreeType's face bbox: 16.16 fixed, mins floored (>> 16) and maxes
    ceiled ((v + 0xFFFF) >> 16). FreeType also makes these faces' ascender yMax, descender yMin."""
    if not bbox or len(bbox) < 4:
        return None
    try:
        x0, y0, x1, y1 = (int(round(float(v) * 65536)) for v in bbox[:4])
    except (TypeError, ValueError):
        return None
    return x0 >> 16, y0 >> 16, (x1 + 0xFFFF) >> 16, (y1 + 0xFFFF) >> 16


class Charmap:
    """One FT_CharMap: its ids, FreeType's encoding for them, and code -> glyph index."""

    __slots__ = ("pid", "eid", "encoding", "lookup", "format")

    def __init__(self, pid: int, eid: int, encoding: str, lookup: Callable[[int], int], format: int = 0):
        self.pid, self.eid, self.encoding, self.lookup, self.format = pid, eid, encoding, lookup, format


class Program:
    """A font program read by fontTools: glyph names or ids -> unscaled control boxes."""

    def __init__(self, kind: str, glyphs, order: list[str], upem: int, encoding=None,
                 cid_keyed: bool = False, bbox=None, ascender=0, descender=0, cmaps=None):
        self.kind, self.glyphs, self.order, self.upem = kind, glyphs, order, upem
        self.index = {n: i for i, n in enumerate(order)}
        self.encoding = encoding          # builtin: list of 256 names, or None
        self.cid_keyed = cid_keyed
        self.bbox = bbox                  # font units (l, b, r, t)
        self.ascender, self.descender = ascender, descender
        self.cmaps = cmaps or {}          # TrueType: {(platform, encoding): {code: glyph name}}
        self.cmap_list: list = []         # sfnt: [(platform, encoding, {code: glyph name})] in file order
        self.sfnt = False                 # a TrueType/OpenType file (CFX_Font::IsTTFont)
        self.glyph_names = True           # FT_HAS_GLYPH_NAMES
        self.ps_names = True              # names FT_Get_Name_Index can find (sfnt: post format 1/2/2.5)
        self.encoding_kind = "standard"   # Type 1 / CFF: which FreeType encoding charmap the face gets
        self._boxes: dict[int, tuple] = {}
        self._charmaps: list[Charmap] | None = None
        self._charmap: Charmap | None = None

    def name_index(self, name: str) -> int:
        """FT_Get_Name_Index: 0 when the font has no glyph of that name (or no glyph names)."""
        if self.sfnt_face is not None:
            return self.sfnt_face.name_index(name)
        if not (self.glyph_names and self.ps_names):
            return 0
        return self.index.get(name, 0)

    sfnt_face: sfnt.Face | None = None     # an sfnt program's FT_Face (charmaps, `post` names)

    def attach_face(self, data: bytes, cff: tuple | None = None, font_number: int = 0) -> None:
        """Take charmaps and glyph names from FreeType's reading of the sfnt, not fontTools'."""
        self.sfnt_face = sfnt.Face(data, cff, font_number)
        self.glyph_names = self.sfnt_face.has_glyph_names
        self.ps_names = True

    # -- the face's charmaps, as FreeType lists and selects them. The selection is state of the face,
    #    and a face the font mapper caches is shared by every font drawn with it, as in PDFium.
    def charmaps(self) -> list["Charmap"]:
        if self._charmaps is None:
            self._charmaps = self._build_charmaps()
            self._charmap = None
            self.select_unicode()                        # FT_Open_Face: find_unicode_charmap
        return self._charmaps

    def _build_charmaps(self) -> list["Charmap"]:
        if self.sfnt_face is not None:
            # sfnt.Face: the validated subtables in file order, then the charmaps FreeType synthesizes
            n = self.sfnt_face.num_glyphs
            return [Charmap(c.platform, c.encoding_id, c.encoding,
                            lambda code, c=c: c.char_index(code, n), c.format) for c in self.sfnt_face.charmaps]
        # Type 1 (T1_Face_Init) and CFF (cff_face_init): psnames' Unicode charmap, which exists when
        # some glyph name has a Unicode value (ps_unicodes_init fails otherwise), then the Adobe
        # encoding charmap - always for Type 1, for CFF only when the encoding maps some code
        out = []
        unicodes = sfnt.PsUnicodes(self.order if not self.cid_keyed else [])
        if unicodes:
            out.append(Charmap(3, 1, "unicode", lambda code: unicodes.char_index(code & 0xFFFFFFFF)))
        if not self.cid_keyed and self.encoding is not None and \
                (self.kind == "type1" or any(self.builtin_index(c) for c in range(256))):
            eid = {"standard": 0, "expert": 1, "custom": 2, "latin1": 3}[self.encoding_kind]
            enc = ("adobe_standard", "adobe_expert", "adobe_custom", "adobe_latin1")[eid]
            out.append(Charmap(7, eid, enc, self.builtin_index))
        return out

    def select_unicode(self) -> bool:
        """FT_Select_Charmap(FT_ENCODING_UNICODE) = find_unicode_charmap: a UCS-4 table from the end,
        else any Unicode one from the end."""
        maps = self.charmaps() if self._charmaps is not None else []
        for cm in reversed(maps):
            if cm.encoding == "unicode" and ((cm.pid, cm.eid) in ((3, 10), (0, 4)) or
                                             (cm.pid, cm.eid, cm.format) == (0, 6, 13)):
                self._charmap = cm
                return True
        for cm in reversed(maps):
            if cm.encoding == "unicode":
                self._charmap = cm
                return True
        return False

    def set_charmap(self, index: int) -> None:
        """CFX_Face::SetCharMapByIndex = FT_Set_Charmap, which refuses a format 14 table."""
        cm = self.charmaps()[index]
        if cm.format != 14:
            self._charmap = cm

    def charmap_encoding(self, index: int) -> str:
        return self.charmaps()[index].encoding

    def use_tt_charmap(self, pid: int, eid: int) -> bool:
        """CPDF_Font::UseTTCharmap."""
        for i, cm in enumerate(self.charmaps()):
            if (cm.pid, cm.eid) == (pid, eid):
                self.set_charmap(i)
                return True
        return False

    def use_tt_charmap_unicode(self) -> bool:
        """CPDF_Font::UseTTCharmapUnicode."""
        unicode_index, symbol = None, False
        for i, cm in enumerate(self.charmaps()):
            if (cm.pid, cm.eid) == (3, 1):
                self.set_charmap(i)
                return True
            if (cm.pid, cm.eid) == (3, 0):
                symbol = True
                continue
            if unicode_index is None and cm.encoding == "unicode":
                unicode_index = i
        if unicode_index is not None and not symbol:
            self.set_charmap(unicode_index)
            return True
        return False

    def char_index(self, code: int) -> int:
        """FT_Get_Char_Index through the selected charmap (0 without one)."""
        self.charmaps()
        cm = self._charmap
        if cm is None or not 0 <= code <= 0xFFFFFFFF:
            return 0
        g = cm.lookup(code)
        return g if 0 <= g < (self.sfnt_face.num_glyphs if self.sfnt_face is not None else len(self.order)) else 0

    def builtin_index(self, code: int) -> int:
        """The glyph the program's own encoding gives a code (FreeType's Type 1 charmap)."""
        if self.encoding is None or not 0 <= code < len(self.encoding):
            return 0
        name = self.encoding[code]
        return self.index.get(name, 0) if name and name != NOTDEF else 0

    def glyph_name(self, index: int) -> str:
        """FT_Get_Glyph_Name: '' where the face has no names."""
        if self.sfnt_face is not None:
            return self.sfnt_face.glyph_name(index)
        if not (self.glyph_names and self.ps_names):
            return ""
        return self.order[index] if 0 <= index < len(self.order) else ""

    def cid_index(self, cid: int) -> int:
        """A CID-keyed font's glyph for a CID (FreeType maps CIDs through the charset)."""
        if not self.cid_keyed:
            return cid
        return self.index.get(f"cid{cid:05d}", 0)

    def glyph_box(self, index: int) -> tuple[int, int, int, int] | None:
        """(left, bottom, right, top) in 1/1000 em, or None for a glyph that won't load."""
        if index in self._boxes:
            return self._boxes[index]
        box = None
        if 0 <= index < len(self.order):
            try:
                from fontTools.pens.boundsPen import ControlBoundsPen
                pen = ControlBoundsPen(self.glyphs)
                self.glyphs[self.order[index]].draw(pen)
                b = pen.bounds or (0, 0, 0, 0)
                # FreeType's unscaled outline points are whole font units (Adobe engine: floored)
                l, bt, r, t = (math.floor(v) for v in b)
                u = self.upem
                box = (normalize_metric(l, u), normalize_metric(bt, u), normalize_metric(r, u), normalize_metric(t, u))
            except Exception:  # noqa: BLE001 - a glyph fontTools cannot draw: none, as FreeType would fail
                box = None
        self._boxes[index] = box
        return box

    def advance(self, index: int) -> int:
        """The glyph's advance in 1/1000 em (GetGlyphTTWidth), 0 when unknown."""
        try:
            name = self.order[index]
            if self.kind == "truetype":
                return normalize_metric(self.glyphs.hmtx[name][0], self.upem) if hasattr(self.glyphs, "hmtx") \
                    else normalize_metric(self.glyphs[name].width, self.upem)
            g = self.glyphs[name]
            if getattr(g, "width", None) is None:
                from fontTools.pens.basePen import NullPen
                g.draw(NullPen())
            return normalize_metric(g.width or 0, self.upem)
        except Exception:  # noqa: BLE001
            return 0


def _upem_from_matrix(matrix) -> int:
    try:
        scale = abs(float(matrix[3])) or abs(float(matrix[0]))
        return int(round(1.0 / scale)) if scale else 1000
    except (TypeError, ValueError, IndexError):
        return 1000


def load_type1(data: bytes) -> Program | None:
    from fontTools import t1Lib
    # T1Font wants a file; parse() only needs the data (cleartext + binary eexec part, as in FontFile)
    font = t1Lib.T1Font.__new__(t1Lib.T1Font)
    font.data, font.encoding = data, "ascii"
    font.parse()
    d = font.font
    charstrings = d["CharStrings"]
    names = list(charstrings.keys())
    if NOTDEF in names:  # FreeType swaps .notdef with glyph 0 (t1load.c parse_charstrings)
        i = names.index(NOTDEF)
        names[0], names[i] = names[i], names[0]
    enc = d.get("Encoding")
    kind = "custom"
    if isinstance(enc, str) and enc == "StandardEncoding" or enc is None:
        enc = [_predefined_name(STANDARD, c) or NOTDEF for c in range(256)]
        kind = "standard"
    elif isinstance(enc, list):
        enc = [str(n) if n else NOTDEF for n in enc] + [NOTDEF] * (256 - len(enc))
    bbox = _ft_bbox(d.get("FontBBox"))
    prog = Program("type1", charstrings, names, _upem_from_matrix(d.get("FontMatrix", [0.001, 0, 0, 0.001])),
                   encoding=enc, bbox=bbox, ascender=bbox[3] if bbox else 0, descender=bbox[1] if bbox else 0)
    prog.encoding_kind = kind
    return prog


class GenericProgram(Program):
    """PDFium's built-in multiple master face (FoxitSansMM / FoxitSerifMM), read by `type1.py` and
    loaded by `ftoutline.py`'s port of FreeType's glyph loader at the font's default blend: fontTools
    cannot build these fonts (their PostScript blends the designs at run time)."""

    def __init__(self, t1):
        from .ftoutline import Face
        enc = t1.encoding or [_predefined_name(STANDARD, c) or NOTDEF for c in range(256)]
        super().__init__("type1", None, t1.order, 1000, encoding=enc, bbox=t1.bbox,
                         ascender=t1.bbox[3], descender=t1.bbox[1])
        self.encoding_kind = "custom" if t1.encoding else "standard"
        self.face = Face.from_type1(t1)

    def glyph_box(self, index: int):
        """FT_Load_Glyph(FT_LOAD_NO_SCALE)'s metrics: the outline's control box (t1gload)."""
        if index in self._boxes:
            return self._boxes[index]
        box = None
        if 0 <= index < len(self.order):
            units = self.face.units(index)
            if units is not None:
                xs = [x for pts, _tags in units for x, _y in pts]
                ys = [y for pts, _tags in units for _x, y in pts]
                l, b, r, t = (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)
                box = tuple(normalize_metric(v, 1000) for v in (l, b, r, t))
        self._boxes[index] = box
        return box

    def advance(self, index: int) -> int:
        a = self.face.advance(index) if 0 <= index < len(self.order) else None
        return normalize_metric(a, 1000) if a else 0


def load_generic(data: bytes) -> Program | None:
    from . import type1
    try:
        return GenericProgram(type1.parse(data))
    except Exception:  # noqa: BLE001 - a face that does not parse: none, as FreeType would refuse it
        return None


def load_cff(data: bytes) -> Program | None:
    from fontTools.cffLib import CFFFontSet
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(data), None)
    top = cff[cff.fontNames[0]]
    charstrings = top.CharStrings
    order = list(top.charset)
    cid_keyed = hasattr(top, "ROS")
    enc = None
    kind = "standard"
    if not cid_keyed:
        e = getattr(top, "Encoding", "StandardEncoding")
        if e == "StandardEncoding":
            enc = [_predefined_name(STANDARD, c) or NOTDEF for c in range(256)]
        elif e == "ExpertEncoding":
            enc = [_predefined_name(MACEXPERT, c) or NOTDEF for c in range(256)]
            kind = "expert"
        elif isinstance(e, list):
            enc = [n or NOTDEF for n in e] + [NOTDEF] * (256 - len(e))
            kind = "custom"
    matrix = getattr(top, "FontMatrix", [0.001, 0, 0, 0.001, 0, 0])
    bbox = _ft_bbox(getattr(top, "FontBBox", None))
    prog = Program("cff", charstrings, order, _upem_from_matrix(matrix), encoding=enc, cid_keyed=cid_keyed,
                   bbox=bbox, ascender=bbox[3] if bbox else 0, descender=bbox[1] if bbox else 0)
    prog.encoding_kind = kind
    return prog


def load_truetype(data: bytes, font_number: int = 0) -> Program | None:
    from fontTools.ttLib import TTFont
    tt = TTFont(io.BytesIO(data), lazy=True, fontNumber=font_number)
    if "CFF " in tt:
        prog = load_cff(tt.getTableData("CFF "))
        if prog:
            prog.cmaps = _tt_cmaps(tt)
            prog.sfnt = True
            gids = [prog.builtin_index(c) for c in range(256)]
            eid = {"standard": 0, "expert": 1}.get(prog.encoding_kind, 2) if any(gids) else None
            prog.attach_face(data, (prog.order, prog.cid_keyed, eid, gids), font_number)
        return prog
    # glyphs are known by index: fontTools would name them from `post`, which FreeType only reads
    # for FT_Get_Name_Index (sfnt.Face), and which may be broken where the outlines are fine
    order = ["glyph%05d" % i for i in range(tt["maxp"].numGlyphs)]
    tt.setGlyphOrder(order)
    glyphs = tt.getGlyphSet()
    head = tt["head"]
    hhea = tt["hhea"] if "hhea" in tt else None
    prog = Program("truetype", glyphs, order, head.unitsPerEm, bbox=(head.xMin, head.yMin, head.xMax, head.yMax),
                   ascender=hhea.ascent if hhea else 0, descender=hhea.descent if hhea else 0, cmaps=_tt_cmaps(tt))
    prog.sfnt = True
    prog.attach_face(data, None, font_number)
    return prog


def _tt_cmaps(tt) -> dict:
    out = {}
    try:
        if "cmap" in tt:
            for sub in tt["cmap"].tables:
                out.setdefault((sub.platformID, sub.platEncID), sub.cmap)
    except Exception:  # noqa: BLE001 - a cmap fontTools cannot read; the charmaps are sfnt.Face's
        pass
    return out


def load_program(stream: Stream | None, data: bytes, subtype_key: str) -> Program | None:
    """The font program of a FontFile/FontFile2/FontFile3 stream; None if it can't be read."""
    if not data:
        return None
    try:
        if subtype_key == "FontFile":
            return load_type1(data)
        if subtype_key == "FontFile2" or data[:4] in (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf"):
            return load_truetype(data)
        if data[:1] == b"\x01":
            return load_cff(data)
        if data[:2] == b"%!":
            return load_type1(data)
    except Exception:  # noqa: BLE001 - fontTools missing or a program it can't read
        return None
    return None


# ---------------------------------------------------------------------- the base 14 (CFX_StandardFont)

BASE14 = ("Courier", "Courier-Bold", "Courier-BoldOblique", "Courier-Oblique",
          "Helvetica", "Helvetica-Bold", "Helvetica-BoldOblique", "Helvetica-Oblique",
          "Times-Roman", "Times-Bold", "Times-BoldItalic", "Times-Italic", "Symbol", "ZapfDingbats")
BASE14_SYMBOL, BASE14_DINGBATS = 12, 13

# kAltFontNames: every name PDFium knows a base 14 font by (FXSYS_stricmp: ASCII case ignored)
_ALT_FONT_NAMES = {name.lower(): index for index, names in enumerate((
    "Courier CourierNew CourierNewPSMT CourierStd",
    "Courier,Bold Courier-Bold CourierBold CourierNew,Bold CourierNew-Bold CourierNewBold CourierNewPS-BoldMT "
    "CourierStd-Bold",
    "Courier,BoldItalic Courier-BoldOblique CourierBoldItalic CourierNew,BoldItalic CourierNew-BoldItalic "
    "CourierNewBoldItalic CourierNewPS-BoldItalicMT CourierStd-BoldOblique",
    "Courier,Italic Courier-Oblique CourierItalic CourierNew,Italic CourierNew-Italic CourierNewItalic "
    "CourierNewPS-ItalicMT CourierStd-Oblique",
    "Arial ArialMT Helvetica",
    "Arial,Bold Arial-Bold Arial-BoldMT ArialBold ArialMT,Bold ArialRoundedMTBold Helvetica,Bold Helvetica-Bold "
    "HelveticaBold",
    "Arial,BoldItalic Arial-BoldItalic Arial-BoldItalicMT ArialBoldItalic ArialMT,BoldItalic Helvetica,BoldItalic "
    "Helvetica-BoldItalic Helvetica-BoldOblique HelveticaBoldItalic",
    "Arial,Italic Arial-Italic Arial-ItalicMT ArialItalic ArialMT,Italic Helvetica,Italic Helvetica-Italic "
    "Helvetica-Oblique HelveticaItalic",
    "Times-Roman TimesNewRoman TimesNewRomanPS TimesNewRomanPSMT",
    "Times-Bold TimesBold TimesNewRoman,Bold TimesNewRoman-Bold TimesNewRomanBold TimesNewRomanPS-Bold "
    "TimesNewRomanPS-BoldMT TimesNewRomanPSMT,Bold",
    "Times-BoldItalic TimesBoldItalic TimesNewRoman,BoldItalic TimesNewRoman-BoldItalic TimesNewRomanBoldItalic "
    "TimesNewRomanPS-BoldItalic TimesNewRomanPS-BoldItalicMT TimesNewRomanPSMT,BoldItalic",
    "Times-Italic TimesItalic TimesNewRoman,Italic TimesNewRoman-Italic TimesNewRomanItalic TimesNewRomanPS-Italic "
    "TimesNewRomanPS-ItalicMT TimesNewRomanPSMT,Italic",
    "Symbol SymbolMT",
    "ZapfDingbats",
)) for name in names.split()}


def _ascii_lower(s: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def standard_font_index(name: str) -> int | None:
    """CFX_StandardFont::GetStandardFontIndex."""
    return _ALT_FONT_NAMES.get(_ascii_lower(name))


def _without_subset_prefix(name: str) -> str:
    """MaybeRemoveSubsettedFontPrefix: 'ABCDEF+Name' -> 'Name'."""
    if len(name) > 7 and name[6] == "+" and all("A" <= c <= "Z" for c in name[:6]):
        return name[7:]
    return name


def subst_font_index(name: str, truetype: bool) -> int | None:
    """The base 14 font CFX_FontMapper::FindSubstFace settles on for a font with no program
    (GetSubstName, then the family before a comma), or None for any other name."""
    subst = name[1:] if truetype and name[:1] == "@" else name.replace(" ", "")
    subst = _without_subset_prefix(subst)
    index = standard_font_index(subst)
    if index is None and "," in subst:
        return None   # a family with a style PDFium parses (ParseStyles): not ported
    return index


# CFX_Win32FontInfo::MapFont (kBase14Substs): on Windows, GDI hands PDFium these system files for the
# twelve fonts that are not Symbol or ZapfDingbats; those two always get PDFium's built-in Foxit faces,
# which the pure reader does not carry (they would be third-party binaries in the tree).
_WIN32_FACES = ("cour.ttf", "courbd.ttf", "courbi.ttf", "couri.ttf", "arial.ttf", "arialbd.ttf", "arialbi.ttf",
                "ariali.ttf", "times.ttf", "timesbd.ttf", "timesbi.ttf", "timesi.ttf")
_system_faces: dict[int, Program | None] = {}


def system_face(index: int) -> Program | None:
    """The face PDFium's system font info gives a base 14 font, None where there is none to read
    (another platform, a Symbol or ZapfDingbats font, a file missing): such a font keeps no face."""
    if index not in _system_faces:
        program = None
        if sys.platform == "win32" and index < len(_WIN32_FACES):
            path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / _WIN32_FACES[index]
            try:
                program = load_truetype(path.read_bytes())
            except Exception:  # noqa: BLE001 - no such file, or fontTools missing
                program = None
        _system_faces[index] = program
    return _system_faces[index]


_MAPPER_ACTIVE: bool | None = None


def _mapper_active() -> bool:
    """Whether fonts without a program go through `fontmapper` (PDFium's Windows chain, with the
    Foxit faces in the user cache); else the older rules above (`subst_font_index`, `system_face`)."""
    global _MAPPER_ACTIVE
    if _MAPPER_ACTIVE is None:
        from . import fontmapper
        _MAPPER_ACTIVE = fontmapper.active()
    return _MAPPER_ACTIVE


# ---------------------------------------------------------------------- fonts


class Font:
    """One font dictionary as PDFium loads it (CPDF_Font::Create)."""

    subtype = "Type1"
    vertical = False

    def __init__(self, doc, d: dict):
        self.doc, self.dict = doc, d
        r = doc.resolve
        self.base_name = str(r(d.get("BaseFont")) or "")
        self.flags = FLAG_NONSYMBOLIC
        self.italic_angle = 0
        self.stem_v = 0
        self.font_weight: int | None = None
        self.subst_generic = False       # drawn with PDFium's multiple master face
        self.ascent = self.descent = 0
        self.font_bbox = (0, 0, 0, 0)   # l, b, r, t
        self.program_data = b""
        self.program: Program | None = None
        self.embedded = False
        tu = r(d.get("ToUnicode"))
        self.to_unicode = ToUnicodeMap(doc.stream_data(tu)) if isinstance(tu, Stream) else None

    # CPDF_Font::LoadFontDescriptor
    def _descriptor(self, desc) -> None:
        r = self.doc.resolve
        if isinstance(desc, Stream):     # GetDictFor: a stream answers with its dictionary
            desc = desc.dict
        if not isinstance(desc, dict):
            return
        self.flags = _int(r(desc.get("Flags")), FLAG_NONSYMBOLIC) if "Flags" in desc else FLAG_NONSYMBOLIC
        angle = _int(r(desc.get("ItalicAngle")))
        if angle < 0:
            self.flags |= FLAG_ITALIC
            self.italic_angle = angle
        if "StemV" in desc:
            self.stem_v = _int(r(desc.get("StemV")))
        valid_weight = False
        if "FontWeight" in desc:
            weight = _int(r(desc.get("FontWeight")))
            valid_weight = weight > 0
            if valid_weight:
                self.font_weight = weight
        if "Ascent" in desc:
            self.ascent = _int(r(desc.get("Ascent")))
        if "Descent" in desc:
            self.descent = _int(r(desc.get("Descent")))
        if all(k in desc for k in ("ItalicAngle", "Ascent", "CapHeight", "Descent")) and \
                ("StemV" in desc or valid_weight):
            self.flags |= FLAG_USE_EXTERN_ATTR
        if self.descent > 10:
            self.descent = -self.descent
        bbox = r(desc.get("FontBBox"))
        if isinstance(bbox, list):       # GetIntegerAt: 0 past the end of a short array
            self.font_bbox = tuple(_int(r(bbox[k])) if k < len(bbox) else 0 for k in range(4))
        for key in ("FontFile", "FontFile2", "FontFile3"):
            stream = r(desc.get(key))
            if isinstance(stream, Stream):
                self.embedded = True
                self.program_data = self.doc.stream_data(stream)
                self.program = load_program(stream, self.program_data, key)
                break

    def font_weight_value(self) -> int | None:
        """CPDF_Font::GetFontWeight: /FontWeight, else from /StemV (None when that overflows)."""
        if self.font_weight is not None:
            return self.font_weight
        v = self.stem_v * 5 if self.stem_v < 140 else self.stem_v * 4 + 140
        return v if -0x80000000 <= v <= 0x7FFFFFFF else None

    # CPDF_Font::CheckFontMetrics
    def _check_metrics(self) -> None:
        if self.font_bbox == (0, 0, 0, 0):
            p = self.program
            if p is not None and p.bbox:
                u = p.upem
                l, b, rt, t = p.bbox
                # PDFium's "flip" is FX_RECT's: GetBBox's `top` is yMin and `bottom` yMax (y down),
                # so font_bbox_.bottom <- top is yMin again: the box comes out the right way up
                self.font_bbox = (normalize_metric(l, u), normalize_metric(b, u), normalize_metric(rt, u),
                                  normalize_metric(t, u))
                self.ascent = normalize_metric(p.ascender, u)
                self.descent = normalize_metric(p.descender, u)
            elif p is None:
                first = None
                for code in range(256):
                    l, b, rt, t = self.char_bbox(code)
                    if l == rt:
                        continue
                    if first is None:
                        first = [l, b, rt, t]
                    else:
                        first = [min(first[0], l), min(first[1], b), max(first[2], rt), max(first[3], t)]
                if first:
                    self.font_bbox = tuple(first)
        if self.ascent == 0 and self.descent == 0:
            l, b, rt, t = self.char_bbox(ord("A"))
            self.ascent = self.font_bbox[3] if b == t else t
            l, b, rt, t = self.char_bbox(ord("g"))
            self.descent = self.font_bbox[1] if b == t else b

    # --- the interface the text layer uses
    def codes(self, data: bytes) -> list[int]:
        return list(data)

    def char_width(self, code: int) -> int:
        raise NotImplementedError

    def char_bbox(self, code: int) -> tuple[int, int, int, int]:
        raise NotImplementedError

    def unicode(self, code: int) -> list[int]:
        """UTF-16 units (UnicodeFromCharCode); [] for none."""
        return self.to_unicode.lookup(code) if self.to_unicode else []

    def code_from_unicode(self, u: int) -> int:
        return self.to_unicode.reverse_lookup(u) if self.to_unicode else 0

    def glyph_width(self, u: int, size: float) -> float:
        """FPDFFont_GetGlyphWidth: `width * font_size / 1000.f` in C floats, the size a float too,
        rounded after the product and again after the division."""
        return float32(float32(self.char_width(self.code_from_unicode(u)) * float32(size)) / 1000.0)

    @property
    def is_type3(self) -> bool:
        return False


class SimpleFont(Font):
    """CPDF_FaceBasedSimpleFont: Type1 and TrueType with up to 256 codes."""

    def __init__(self, doc, d: dict):
        super().__init__(doc, d)
        self.subtype = str(doc.resolve(d.get("Subtype")) or "Type1")
        self.widths = [0xFFFF] * 256
        self.use_font_width = False
        self.base_encoding = BUILTIN
        self.char_names: list[str | None] = []
        self.glyphs: list = [NO_GLYPH] * 256      # glyph index per code; NO_GLYPH = 0xffff
        self.enc_unicode = [0] * 256              # encoding_ (CPDF_FontEncoding)
        self._boxes: dict[int, tuple] = {}
        self._load()

    def _load(self) -> None:
        r = self.doc.resolve
        d = self.dict
        desc = r(d.get("FontDescriptor"))
        if isinstance(desc, Stream):     # GetDictFor: a stream answers with its dictionary
            desc = desc.dict
        self.base14 = None
        if self.subtype != "TrueType":
            # CPDF_Type1Font::Load: a base 14 name takes its canonical name, flags and base encoding
            self.base14 = standard_font_index(self.base_name)
            if self.base14 is not None:
                self.base_name = BASE14[self.base14]
                if not isinstance(desc, dict) and self.base14 >= BASE14_SYMBOL:
                    self.flags = FLAG_SYMBOLIC
                if self.base14 < 4:   # Courier
                    self.widths = [600] * 256
                if self.base14 == BASE14_SYMBOL:
                    self.base_encoding = SYMBOL
                elif self.base14 == BASE14_DINGBATS:
                    self.base_encoding = ZAPF
                elif not self.flags & FLAG_SYMBOLIC:
                    self.base_encoding = STANDARD
        self._descriptor(desc)
        mapped = _mapper_active()
        if mapped and self.embedded and self.program is None:
            # LoadFontDescriptor: a program FreeType cannot open is purged (MaybePurgeFontFileStreamAcc),
            # and the font is then as good as one without a program
            self.embedded = False
        # LoadCharWidths
        widths = r(d.get("Widths"))
        self.use_font_width = not isinstance(widths, list)
        if isinstance(widths, list):
            # char_width_ is a uint16_t array: a negative width wraps, and 0xFFFF reads as "not loaded"
            if isinstance(desc, dict) and "MissingWidth" in desc:
                self.widths = [_int(r(desc.get("MissingWidth"))) & 0xFFFF] * 256
            start, end = _int(r(d.get("FirstChar"))), _int(r(d.get("LastChar")))
            if 0 <= start <= 255 and widths:
                if end == 0 or end >= start + len(widths):
                    end = start + len(widths) - 1
                end = min(end, 255)
                for i in range(start, end + 1):
                    self.widths[i] = _int(r(widths[i - start])) & 0xFFFF
        if self.embedded and len(self.base_name) > 7 and self.base_name[6] == "+" and self.base_name[:6].isupper():
            self.base_name = self.base_name[7:]
        elif not self.embedded:
            self._subst_font()
        if not self.flags & FLAG_SYMBOLIC:
            self.base_encoding = STANDARD
        self._pdf_encoding(self.embedded, self.program is not None and self.program.sfnt)
        self._glyph_map()
        if mapped and self.program is None:
            return          # LoadCommon: no face, no AllCaps and no CheckFontMetrics
        if self.program is not None and self.flags & FLAG_ALLCAPS:
            for lo, hi in ((0x61, 0x7A), (0xE0, 0xF6), (0xF8, 0xFD)):
                for i in range(lo, hi + 1):
                    if self.glyphs[i] is not NO_GLYPH and self.embedded:
                        continue
                    j = i - 32
                    self.glyphs[i] = self.glyphs[j]
                    if self.widths[j]:
                        self.widths[i] = self.widths[j]
                        if j in self._boxes:
                            self._boxes[i] = self._boxes[j]
        self._check_metrics()

    # CPDF_FaceBasedSimpleFont::LoadSubstFont
    def _subst_font(self) -> None:
        if not self.use_font_width and not self.flags & FLAG_FIXED:
            width = 0
            for w in self.widths:
                if w in (0, 0xFFFF):
                    continue
                if width == 0:
                    width = w
                elif width != w:
                    break
            else:
                if width:
                    self.flags |= FLAG_FIXED
        if _mapper_active():
            from . import fontmapper
            weight = self.font_weight_value()
            if weight is None or not 100 <= weight <= 800:    # kFontWeightExtraLight .. ExtraBold
                weight = 400
            face = fontmapper.load_subst_face(self.base_name, self.subtype == "TrueType", self.flags & 0xFFFFFFFF,
                                              weight, self.italic_angle)
            if face is not None:
                self.program, self.subst_generic = face.program, face.generic
            return
        index = subst_font_index(self.base_name, self.subtype == "TrueType")
        if index is not None:
            self.program = system_face(index)

    # CPDF_SimpleFont::LoadPDFEncoding
    def _pdf_encoding(self, embedded: bool, truetype: bool) -> None:
        enc = self.doc.resolve(self.dict.get("Encoding"))
        if enc is None:
            if self.base_name == "Symbol":
                self.base_encoding = MSSYMBOL if truetype else SYMBOL
            elif not embedded and self.base_encoding == BUILTIN:
                self.base_encoding = WINANSI
            return
        if isinstance(enc, Name):
            if self.base_encoding in (SYMBOL, ZAPF):
                return
            if self.flags & FLAG_SYMBOLIC and self.base_name == "Symbol":
                if not truetype:
                    self.base_encoding = SYMBOL
                return
            name = str(enc)
            if name == "MacExpertEncoding":
                name = "WinAnsiEncoding"
            self.base_encoding = _PREDEFINED.get(name, self.base_encoding)
            return
        if not isinstance(enc, dict):
            return
        if self.base_encoding not in (SYMBOL, ZAPF):
            name = str(self.doc.resolve(enc.get("BaseEncoding")) or "")
            if truetype and name == "MacExpertEncoding":
                name = "WinAnsiEncoding"
            self.base_encoding = _PREDEFINED.get(name, self.base_encoding)
        if (not embedded or truetype) and self.base_encoding == BUILTIN:
            self.base_encoding = STANDARD
        diffs = self.doc.resolve(enc.get("Differences"))
        if isinstance(diffs, list):
            self.char_names = [None] * 256
            code = 0
            for item in diffs:
                item = self.doc.resolve(item)
                if isinstance(item, Name):
                    if 0 <= code < 256:
                        self.char_names[code] = str(item)
                    code += 1
                elif item is not None:
                    code = _int(item)       # GetInteger: 0 for anything that is not a number

    def char_name(self, code: int, encoding: str | None = None) -> str | None:
        """CPDF_Font::GetAdobeCharName(base_encoding, char_names_, code); the font's own base
        encoding unless another is given."""
        if encoding is None:
            encoding = self.base_encoding
        if not 0 <= code < 256:
            return None
        if self.char_names and self.char_names[code]:
            return self.char_names[code]
        if encoding == BUILTIN:
            return None
        return _predefined_name(encoding, code)

    def _glyph_map(self) -> None:
        """LoadGlyphMap: CPDF_TrueTypeFont's for a /TrueType font, CPDF_Type1Font's for the rest,
        whatever the program inside is. Both work on the face's charmaps (`Program.charmaps`), whose
        selection is state of the face."""
        p = self.program
        if p is None:
            # no face: PDFium keeps no glyphs, and encoding_ stays empty
            return
        if self.subtype == "TrueType":
            self._truetype_glyph_map(p)
        else:
            self._type1_glyph_map(p)

    @staticmethod
    def _ms_symbol_index(p: Program, code: int) -> int:
        """GetGlyphIndexForMSSymbol: the code under the prefixes 00, F0, F1, F2."""
        for prefix in (0x00, 0xF0, 0xF1, 0xF2):
            g = p.char_index(((prefix * 256) + code) & 0xFFFF)
            if g:
                return g
        return 0

    # CPDF_Type1Font::LoadGlyphMap
    def _type1_glyph_map(self, p: Program) -> None:
        if not self.embedded and self.base14 not in (BASE14_SYMBOL, BASE14_DINGBATS) and p.sfnt:
            if p.use_tt_charmap(3, 0):
                got_one = False
                for code in range(256):
                    for prefix in (0x00, 0xF0, 0xF1, 0xF2):
                        self.glyphs[code] = p.char_index(prefix * 256 + code)
                        if self.glyphs[code]:
                            got_one = True
                            break
                if got_one:
                    return
            p.select_unicode()
            if self.base_encoding == BUILTIN:
                self.base_encoding = STANDARD
            for code in range(256):
                name = self.char_name(code)
                if not name:
                    continue
                self.enc_unicode[code] = unicode_from_adobe_name(name)
                self.glyphs[code] = p.char_index(self.enc_unicode[code])
                if self.glyphs[code] == 0 and name == NOTDEF:
                    self.enc_unicode[code] = 0x20
                    self.glyphs[code] = p.char_index(0x20)
            return
        # UseType1Charmap
        maps = p.charmaps()
        if maps:
            first_unicode = maps[0].encoding == "unicode"
            if not (len(maps) == 1 and first_unicode):
                p.set_charmap(1 if first_unicode else 0)
        if self.flags & FLAG_SYMBOLIC:
            for code in range(256):
                name = self.char_name(code)
                if name:
                    self.enc_unicode[code] = unicode_from_adobe_name(name)
                    self.glyphs[code] = p.name_index(name)
                else:
                    g = p.char_index(code)
                    self.glyphs[code] = g
                    if g:
                        gname = p.glyph_name(g)
                        self.enc_unicode[code] = unicode_from_adobe_name(gname) if gname else 0
            return
        unicode = p.select_unicode()
        for code in range(256):
            name = self.char_name(code)
            if not name:
                continue
            self.enc_unicode[code] = unicode_from_adobe_name(name)
            g = p.name_index(name)
            self.glyphs[code] = g
            if g:
                continue
            if name not in (NOTDEF, "space"):
                self.glyphs[code] = p.char_index(self.enc_unicode[code] if unicode else code)
            else:
                self.enc_unicode[code] = 0x20
                self.glyphs[code] = NO_GLYPH

    # CPDF_TrueTypeFont::DetermineEncoding
    def _determine_encoding(self, p: Program) -> str:
        if not self.embedded or not self.flags & FLAG_SYMBOLIC or self.base_encoding not in (WINANSI, MACROMAN):
            return self.base_encoding
        maps = p.charmaps()
        if not maps:
            return self.base_encoding
        support_win = any(cm.pid in (0, 3) for cm in maps)
        support_mac = any(cm.pid == 1 for cm in maps)
        if self.base_encoding == WINANSI and not support_win:
            return MACROMAN if support_mac else BUILTIN
        if self.base_encoding == MACROMAN and not support_mac:
            return WINANSI if support_win else BUILTIN
        return self.base_encoding

    # CPDF_TrueTypeFont::DetermineCharmapType
    def _charmap_type(self, p: Program) -> str:
        if p.use_tt_charmap_unicode():
            return "ms_unicode"
        order = ((1, 0), (3, 0)) if self.flags & FLAG_NONSYMBOLIC else ((3, 0), (1, 0))
        for ids in order:
            if p.use_tt_charmap(*ids):
                return "mac_roman" if ids == (1, 0) else "ms_symbol"
        return "other"

    def _any_glyph(self) -> bool:
        """HasAnyGlyphIndex: 0xffff counts as a glyph there."""
        return any(g != 0 for g in self.glyphs)

    # CPDF_TrueTypeFont::LoadGlyphMap
    def _truetype_glyph_map(self, p: Program) -> None:
        base = self._determine_encoding(p)
        if (base in (WINANSI, MACROMAN) and not self.char_names) or self.flags & FLAG_NONSYMBOLIC:
            if p.glyph_names and not p.charmaps():
                # SetGlyphIndicesFromFirstChar
                start = _int(self.doc.resolve(self.dict.get("FirstChar")))
                if 0 <= start <= 255:
                    for code in range(256):
                        self.glyphs[code] = 0 if code < start else (code - start + 3) & 0xFFFF
                return
            kind = self._charmap_type(p)
            to_unicode_key = "ToUnicode" in self.dict
            mac_unicodes = UNICODES[MACROMAN]
            for code in range(256):
                name = self.char_name(code, base)
                if not name:
                    self.glyphs[code] = p.char_index(code) if self.embedded else NO_GLYPH
                    continue
                u = unicode_from_adobe_name(name)
                self.enc_unicode[code] = u
                if kind == "ms_symbol":
                    self.glyphs[code] = self._ms_symbol_index(p, code)
                elif u:
                    if kind == "ms_unicode":
                        self.glyphs[code] = p.char_index(u)
                    elif kind == "mac_roman":
                        u16 = u & 0xFFFF
                        mac = next((i for i, v in enumerate(mac_unicodes) if v == u16), 0)   # PDF_FindCode
                        self.glyphs[code] = p.name_index(name) if not mac else p.char_index(mac)
                g = self.glyphs[code]
                if g not in (0, NO_GLYPH):
                    continue
                if name == NOTDEF:
                    self.glyphs[code] = p.char_index(32)
                    continue
                self.glyphs[code] = p.name_index(name)
                if self.glyphs[code] or not to_unicode_key:
                    continue
                units = self.unicode(code)
                if units:
                    self.glyphs[code] = p.char_index(units[0])
                    self.enc_unicode[code] = units[0]
            return
        if p.use_tt_charmap(3, 0):
            for code in range(256):
                self.glyphs[code] = self._ms_symbol_index(p, code)
            if self._any_glyph():
                if base != BUILTIN:
                    for code in range(256):
                        name = self.char_name(code, base)
                        if name:
                            self.enc_unicode[code] = unicode_from_adobe_name(name)
                elif p.use_tt_charmap(1, 0):
                    for code in range(256):
                        self.enc_unicode[code] = UNICODES[MACROMAN][code]
                return
        if p.use_tt_charmap(1, 0):
            for code in range(256):
                self.glyphs[code] = p.char_index(code)
                self.enc_unicode[code] = UNICODES[MACROMAN][code]
            if self.embedded or self._any_glyph():
                return
        if p.select_unicode():
            for code in range(256):
                if self.embedded:
                    self.enc_unicode[code] = code
                else:
                    name = self.char_name(code, BUILTIN)
                    if name:
                        self.enc_unicode[code] = unicode_from_adobe_name(name)
                    elif base != BUILTIN:
                        self.enc_unicode[code] = _predefined_unicode(base, code)
                self.glyphs[code] = p.char_index(self.enc_unicode[code])
            if self._any_glyph():
                return
        for code in range(256):
            self.glyphs[code] = code

    # CPDF_FaceBasedSimpleFont::GetCharWidth / GetCharBBox / LoadCharMetrics
    def _load_metrics(self, code: int) -> None:
        if self.program is None or not 0 <= code <= 0xFF:
            return
        g = self.glyphs[code]
        if g is NO_GLYPH:
            if not self.embedded and code != 32:
                self._load_metrics(32)
                self._boxes[code] = self._boxes.get(32, (-1, -1, -1, -1))
                if self.use_font_width:
                    self.widths[code] = self.widths[32]
            return
        box = self.program.glyph_box(g)
        if box is None:
            return
        if self.use_font_width:
            tt = self.program.advance(g)
            if self.widths[code] == 0xFFFF:
                self.widths[code] = tt & 0xFFFF
            elif tt and not self.embedded:
                # a substitute face's box is stretched to the width the font says (C++ int division)
                w = self.widths[code]
                box = (_cdiv(box[0] * w, tt), box[1], _cdiv(box[2] * w, tt), box[3])
        self._boxes[code] = box

    def char_width(self, code: int) -> int:
        if code > 0xFF:
            code = 0
        if self.widths[code] == 0xFFFF:
            self._load_metrics(code)
            if self.widths[code] == 0xFFFF:
                self.widths[code] = 0
        return self.widths[code]

    def char_bbox(self, code: int) -> tuple[int, int, int, int]:
        if code > 0xFF:
            code = 0
        if code not in self._boxes:
            self._load_metrics(code)
        return self._boxes.get(code, (-1, -1, -1, -1))

    def unicode(self, code: int) -> list[int]:
        units = super().unicode(code)
        if units:
            return units
        u = self.enc_unicode[code & 0xFF] if 0 <= code < 256 else 0
        if not u:
            return []
        return [u] if u <= 0xFFFF else list(struct_utf16(u))

    def code_from_unicode(self, u: int) -> int:
        code = super().code_from_unicode(u)
        if code:
            return code
        try:
            return self.enc_unicode.index(u)
        except ValueError:
            return INVALID_CODE  # CPDF_FontEncoding::CharCodeFromUnicode's -1


def struct_utf16(u: int):
    u -= 0x10000
    return 0xD800 + (u >> 10), 0xDC00 + (u & 0x3FF)


class Type3Font(SimpleFont):
    """CPDF_Type3Font: glyphs are content streams; widths and boxes are in glyph space."""

    subtype = "Type3"

    def __init__(self, doc, d: dict):
        Font.__init__(self, doc, d)
        r = doc.resolve
        self.char_names = []
        self.base_encoding = BUILTIN
        self.enc_unicode = [0] * 256
        self.glyphs = [NO_GLYPH] * 256
        m = r(d.get("FontMatrix"))
        self.matrix = tuple(_num(r(v)) for v in m[:6]) if isinstance(m, list) and len(m) >= 6 else \
            (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        xs, ys = self.matrix[0], self.matrix[3]
        bbox = r(d.get("FontBBox"))
        if isinstance(bbox, list) and len(bbox) >= 4:
            v = [_num(r(x)) for x in bbox[:4]]
            # in floats, like PDFium: 6 * 0.011f * 1000 is 66 there, 65.9999996 in doubles
            box = tuple(float32(float32(v[k] * (xs, ys)[k % 2]) * 1000) for k in range(4))
            # CFX_FloatRect::ToFxRect: outer integers
            self.font_bbox = (math.floor(min(box[0], box[2])), math.floor(min(box[1], box[3])),
                              math.ceil(max(box[0], box[2])), math.ceil(max(box[1], box[3])))
        self.widths = [0] * 256
        start = _int(r(d.get("FirstChar")))
        widths = r(d.get("Widths"))
        if 0 <= start < 256 and isinstance(widths, list):
            for i in range(min(len(widths), 256 - start)):
                self.widths[start + i] = round_half_away(_num(r(widths[i])) * xs * 1000)
        procs = r(d.get("CharProcs"))
        self.procs = procs if isinstance(procs, dict) else {}
        if r(d.get("Encoding")) is not None:
            self._pdf_encoding(False, False)
        self._chars: dict[int, tuple[int, tuple] | None] = {}
        self.metrics_checked = False

    @property
    def is_type3(self) -> bool:
        return True

    def check_metrics(self) -> None:
        """CheckType3FontMetrics, which PDFium runs when a content stream selects the font."""
        if not self.metrics_checked:
            self.metrics_checked = True
            self._check_metrics()

    def _check_metrics(self) -> None:
        if self.font_bbox == (0, 0, 0, 0):
            # CheckFontMetrics with no face: the union of the char boxes that have a width
            union = None
            for code in range(256):
                l, b, rt, t = self.char_bbox(code)
                if l == rt:
                    continue
                union = [l, b, rt, t] if union is None else \
                    [min(union[0], l), min(union[1], b), max(union[2], rt), max(union[3], t)]
            if union:
                self.font_bbox = tuple(union)
        if self.ascent == 0 and self.descent == 0:
            l, b, rt, t = self.char_bbox(ord("A"))
            self.ascent = self.font_bbox[3] if b == t else t
            l, b, rt, t = self.char_bbox(ord("g"))
            self.descent = self.font_bbox[1] if b == t else b

    def _load_char(self, code: int):
        """CPDF_Type3Font::LoadChar + CPDF_Type3Char: (width, bbox) or None."""
        if code in self._chars:
            return self._chars[code]
        result = None
        name = self.char_name(code)
        stream = self.doc.resolve(self.procs.get(name)) if name else None
        if isinstance(stream, Stream):
            width, bbox = 0, (0, 0, 0, 0)
            data = self.doc.stream_data(stream)
            for op, args in operations(data):
                if op in ("d0", "d1"):
                    nums = [_num(a) for a in args]
                    if op == "d0" and len(nums) >= 2:
                        width = round_half_away(nums[0] * 1000)
                    elif op == "d1" and len(nums) >= 6:
                        width = round_half_away(nums[0] * 1000)
                        bbox = tuple(round_half_away(v * 1000) for v in nums[2:6])
                    break
            a, b, c, d, e, f = self.matrix
            xunit = abs(a) if b == 0 else abs(b) if a == 0 else math.hypot(a, b)
            width = int(width * xunit + 0.5)
            l, bt, rt, t = bbox
            if rt <= l or bt >= t:  # no box of its own: the glyph's form's (CalcBoundingBox)
                l, bt, rt, t = (v * 1000 for v in self._form_box(data))
            pts = [(a * x + c * y + e * 1000, b * x + d * y + f * 1000) for x in (l, rt) for y in (bt, t)]
            xs_, ys_ = [p[0] for p in pts], [p[1] for p in pts]
            result = (width, (round_half_away(min(xs_)), round_half_away(min(ys_)),
                              round_half_away(max(xs_)), round_half_away(max(ys_))))
        self._chars[code] = result
        return result

    def _form_box(self, data: bytes) -> tuple:
        """CPDF_PageObjectHolder::CalcBoundingBox of the glyph procedure parsed as a form: the
        union of its objects' rects, glyph space (nothing drawn: an empty rect)."""
        from .content import Parser  # content.py imports this module
        objects: list = []
        res = self.doc.resolve(self.dict.get("Resources"))
        try:
            Parser(self.doc, res if isinstance(res, dict) else {}, objects, {}, {}).parse_page(data, (0, 0, 0, 0))
        except RecursionError:
            pass
        rects = [o.rect for o in objects if o.parent is None]
        if not rects:
            return 0.0, 0.0, 0.0, 0.0
        return (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects),
                max(r[3] for r in rects))

    def char_width(self, code: int) -> int:
        if code >= 256:
            code = 0
        if self.widths[code]:
            return self.widths[code]
        ch = self._load_char(code)
        return ch[0] if ch else 0

    def char_bbox(self, code: int) -> tuple[int, int, int, int]:
        ch = self._load_char(code)
        return ch[1] if ch else (0, 0, 0, 0)

    def unicode(self, code: int) -> list[int]:
        return Font.unicode(self, code)

    def code_from_unicode(self, u: int) -> int:
        return Font.code_from_unicode(self, u)


class CIDFont(Font):
    """CPDF_CIDFont (Type0 with one descendant)."""

    subtype = "Type0"

    def __init__(self, doc, d: dict):
        super().__init__(doc, d)
        r = doc.resolve
        fonts = r(d.get("DescendantFonts"))
        desc_font = r(fonts[0]) if isinstance(fonts, list) and len(fonts) == 1 else None
        if isinstance(desc_font, Stream):
            desc_font = desc_font.dict
        if not isinstance(desc_font, dict):
            raise ValueError("CPDF_CIDFont::Load: no single descendant font")
        self.base_name = str(r(desc_font.get("BaseFont")) or "")
        enc = r(d.get("Encoding"))
        if not isinstance(enc, (Name, Stream)):
            raise ValueError("CPDF_CIDFont::Load: an /Encoding that is neither a name nor a stream")
        if isinstance(enc, Stream):
            self.cmap = CMap("", doc.stream_data(enc))
            self._cmap_name = None
        else:
            self._cmap_name = str(enc) if isinstance(enc, Name) else "Identity-H"
            self.cmap = CMap(self._cmap_name)
        self.vertical = self.cmap.vertical
        self.cid_type0 = str(r(desc_font.get("Subtype")) or "") == "CIDFontType0"
        self._descriptor(r(desc_font.get("FontDescriptor")))
        self.default_width = _int(r(desc_font.get("DW")), 1000) if "DW" in desc_font else 1000
        self.width_list: list[tuple[int, int, int]] = []
        w = r(desc_font.get("W"))
        if isinstance(w, list):
            self._metrics_array([r(v) for v in w])
        gid = r(desc_font.get("CIDToGIDMap"))
        self.cid_to_gid = doc.stream_data(gid) if isinstance(gid, Stream) else None
        self._boxes: dict[int, tuple] = {}
        self._check_metrics()

    def _metrics_array(self, items: list) -> None:
        """LoadMetricsArray with nElements 1: `c [w...]` and `c1 c2 w`."""
        r = self.doc.resolve
        pending: list[int] = []
        for item in items:
            item = r(item)
            if isinstance(item, list):
                if not pending:
                    continue
                first = pending[-1]
                for k, v in enumerate(item):
                    self.width_list.append((first + k, first + k, _int(r(v))))
                pending = []
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                pending.append(int(item))
                if len(pending) == 3:
                    self.width_list.append((pending[0], pending[1], pending[2]))
                    pending = []

    def codes(self, data: bytes) -> list[int]:
        return self.cmap.codes(data)

    def char_size(self, code: int) -> int:
        return self.cmap.char_size(code)

    def char_width(self, code: int) -> int:
        cid = self.cmap.cid(code)
        for lo, hi, w in self.width_list:
            if lo <= cid <= hi:
                return w
        return self.default_width

    def _glyph(self, code: int) -> int:
        cid = self.cmap.cid(code)
        p = self.program
        if p is None:
            return -1
        if self.cid_to_gid is not None and not self.cid_type0:
            k = cid * 2
            return int.from_bytes(self.cid_to_gid[k:k + 2], "big") if k + 2 <= len(self.cid_to_gid) else cid
        if self.cid_type0 or p.kind != "truetype":
            return p.cid_index(cid)
        return cid

    def char_bbox(self, code: int) -> tuple[int, int, int, int]:
        if code in self._boxes:
            return self._boxes[code]
        rect = (0, 0, 0, 0)
        g = self._glyph(code)
        if g >= 0 and self.program is not None:
            box = self.program.glyph_box(g)
            if box is not None:
                l, b, rt, t = box
                t += int(t / 64)  # CFX_Face::GetCharBBox: C integer division
                rect = (l, b, rt, t)
        self._boxes[code] = rect
        return rect

    def unicode(self, code: int) -> list[int]:
        return super().unicode(code)

    def code_from_unicode(self, u: int) -> int:
        """CPDF_CIDFont::CharCodeFromUnicode: the ToUnicode map reversed, else by the CMap's coding.
        An embedded CMap is kUNKNOWN and Identity-H/V kCID with no CID-to-Unicode map loaded (the
        Identity ordering has none, and the CJK maps are not ported): both answer 0. Only another
        predefined CMap reaches the `unicode < 0x80` rule."""
        code = super().code_from_unicode(u)
        if code:
            return code
        if self._cmap_name is None or self._cmap_name in ("Identity-H", "Identity-V"):
            return 0
        return u if u < 0x80 else 0


def load_font(doc, d) -> Font | None:
    """CPDF_Font::Create."""
    if not isinstance(d, dict):
        return None
    subtype = str(doc.resolve(d.get("Subtype")) or "")
    try:
        if subtype == "Type3":
            return Type3Font(doc, d)
        if subtype == "Type0":
            return CIDFont(doc, d)
        return SimpleFont(doc, d)
    except Exception:  # noqa: BLE001 - a font PDFium would refuse too: its text is not drawn
        return None
