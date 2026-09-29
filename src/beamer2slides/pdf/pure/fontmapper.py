"""The face PDFium draws a font with when the PDF carries no usable program for it.

A port of `CFX_FontMapper::FindSubstFace` (core/fxge/cfx_fontmapper.cpp) over the system font info
PDFium uses on Windows, `CFX_Win32FontInfo` (core/fxge/win32/cwin32_platform.cpp), whose GDI calls
are made for real through ctypes: `CreateFontA` + `GetTextFaceA` decide whether Windows has a face
of that name, `EnumFontFamiliesExA` lists the installed families, `GetFontData` hands over the very
bytes FreeType would open. When nothing is found, `UseInternalSubst` gives one of PDFium's built-in
faces (`foxit.py`): the fourteen standard faces for a base 14 font, else the multiple master
FoxitSerifMM (a serif pitch family) or FoxitSansMM, read by `type1.py` and blended at their
default weight vector by `ftoutline.py` (PDFium's `AdjustVariationParams` only runs for rendering
and glyph widths asked of the face, never for `LoadCharMetrics`, so the metrics PDFium reports are
the default blend's - as long as nothing drew the shared face first).

Everything below keeps PDFium's names and order; a function says so when it leaves a branch out.
Names are `str` holding one byte per character (latin-1), as `ByteString`s hold them.

The mapper is process-wide in PDFium (`CFX_GEModule`'s font manager), and so are its caches:
the installed-font list is enumerated once, and faces are cached by (substitute name, weight,
italic) - two fonts with the same /BaseFont and style share the first one's face even if GDI
would now give another. The same here.

A system face is cached *weakly*, though: `face_map_` and `ttc_face_map_` hold `ObservedPtr`s
(cfx_fontmapper.h) and a `FontCacheEntry`'s `ttc_faces_` are `ObservedPtr`s too, so the face and the
bytes behind it live only while some `CFX_Font` - that is, some open document's font - holds them,
and the next document gets a face freshly opened from GDI's bytes. That matters because a face
carries mutable state between the fonts that share it: the selected charmap
(`CPDF_TrueTypeFont::LoadGlyphMap` leaves behind the one it used) and, through it, what
`CFX_Font::GetCharIndex` answers at drawing time - a leftover Mac charmap otherwise decides the next
document's fallback glyphs. `hold` and `release` keep that lifetime: the document whose font took a
face holds its cache entry (`load_subst_face`'s `doc`), and closing the document drops every entry
no other open document holds, as the last `CFX_Font` on it being destroyed does in PDFium. PDFium's
built-in faces are the exception - `standard_faces_`, `generic_sans_face_` and `generic_serif_face_`
are `RetainPtr`s, so the multiple master blend really is process-wide - and `standard_faces` /
`generic` below are never dropped for that reason.

Off Windows PDFium's font info is `CFX_FolderFontInfo` (cfx_folderfontinfo.cpp): the .ttf/.ttc/.otf
files under a few folders, named by their 'name' table (family, plus the style unless it is
"Regular"), their charsets from OS/2's code page bits; `LinuxFontInfo` and `MacFontInfo` are its two
MapFonts. Its quirks come along: a face inside a collection is always loaded as the collection's
first face (GetFontData gives 0 for the face alone, so GetTTCIndex finds no offset), and the first
face of a name wins.

Only with the Foxit faces in their cache (`foxit.available()`): without them `active()` is False and
`fonts.py` keeps its older, narrower substitution.
"""
from __future__ import annotations

import ctypes
import os
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Generic, Literal, Protocol, TypeAlias, TypeVar

from ...typing_compat import override
from . import foxit
from .crt import cdiv

if TYPE_CHECKING:
    from .document import PdfFile
    from .fonts import GenericProgram, Program

# pdfium::kFontStyle* (core/fxge/fx_font.h) and CPDF_Font's own flag
STYLE_NORMAL = 0
STYLE_FIXED_PITCH = 1 << 0
STYLE_SERIF = 1 << 1
STYLE_SYMBOLIC = 1 << 2
STYLE_SCRIPT = 1 << 3
STYLE_NONSYMBOLIC = 1 << 5
STYLE_ITALIC = 1 << 6
STYLE_FORCE_BOLD = 1 << 18
USE_EXTERN_ATTR = 0x80000

# pdfium::kFontPitchFamily*
PITCH_FIXED = 1 << 0
PITCH_ROMAN = 1 << 4
PITCH_SCRIPT = 4 << 4

WEIGHT_EXTRA_LIGHT, WEIGHT_NORMAL, WEIGHT_BOLD, WEIGHT_EXTRA_BOLD = 100, 400, 700, 900

# FX_Charset
CHARSET_ANSI, CHARSET_DEFAULT, CHARSET_SYMBOL = 0, 1, 2
_CJK_CHARSETS = (128, 129, 134, 136)     # ShiftJIS, Hangul, ChineseSimplified, ChineseTraditional

# CFX_StandardFont::Index
COURIER, HELVETICA, TIMES, SYMBOL, DINGBATS = 0, 4, 8, 12, 13
CANONICAL = ("Courier", "Courier-Bold", "Courier-BoldOblique", "Courier-Oblique",
             "Helvetica", "Helvetica-Bold", "Helvetica-BoldOblique", "Helvetica-Oblique",
             "Times-Roman", "Times-Bold", "Times-BoldItalic", "Times-Italic", "Symbol", "ZapfDingbats")

_ALT_FONT_FAMILIES = (("AGaramondPro", "Adobe Garamond Pro"), ("BankGothicBT-Medium", "BankGothic Md BT"),
                      ("ForteMT", "Forte"))
NARROW_FAMILY = "ArialNarrow"             # kNarrowFamily on Windows

# kFontStyles: name, style (the length is the name's)
_FONT_STYLES = (("Regular", STYLE_NORMAL), ("Reg", STYLE_NORMAL),
                ("BoldItalic", STYLE_FORCE_BOLD | STYLE_ITALIC), ("Italic", STYLE_ITALIC),
                ("Bold", STYLE_FORCE_BOLD))

# CFX_Win32FontInfo's kBase14Substs: name, Windows face, bold, italic
_BASE14_SUBSTS = (("Courier", "Courier New", False, False), ("Courier-Bold", "Courier New", True, False),
                  ("Courier-BoldOblique", "Courier New", True, True), ("Courier-Oblique", "Courier New", False, True),
                  ("Helvetica", "Arial", False, False), ("Helvetica-Bold", "Arial", True, False),
                  ("Helvetica-BoldOblique", "Arial", True, True), ("Helvetica-Oblique", "Arial", False, True),
                  ("Times-Roman", "Times New Roman", False, False), ("Times-Bold", "Times New Roman", True, False),
                  ("Times-BoldItalic", "Times New Roman", True, True),
                  ("Times-Italic", "Times New Roman", False, True))
_VARIANT_NAMES = (("DFKai-SB", "標楷體"),)

FW_NORMAL, FW_BOLD = 400, 700
OUT_TT_ONLY_PRECIS = 7
TRUETYPE_FONTTYPE, DEVICE_FONTTYPE = 4, 2
GDI_ERROR = 0xFFFFFFFF
TABLE_NONE = 0
TABLE_TTCF = int.from_bytes(b"ttcf", "little")   # FromBE32(FXBSTR_ID('t','t','c','f'))
TABLE_NAME = int.from_bytes(b"name", "little")


def _lower(s: str) -> str:
    """ByteString::MakeLower / FXSYS_tolower: ASCII only."""
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def _equal_no_case(a: str, b: str) -> bool:
    return len(a) == len(b) and _lower(a) == _lower(b)


def tt_normalize_name(norm: str) -> str:
    """TT_NormalizeName: no ' ', '-' or ',', cut at a '+' that is not the first character, lowercase."""
    norm = norm.replace(" ", "").replace("-", "").replace(",", "")
    pos = norm.find("+")
    if pos > 0:
        norm = norm[:pos]
    return _lower(norm)


def _get_font_family(style: int, fontname: str) -> str | None:
    """GetFontFamily."""
    if "Script" in fontname:
        if style & STYLE_FORCE_BOLD:
            return "ScriptMTBold"
        if "Palace" in fontname:
            return "PalaceScriptMT"
        if "French" in fontname:
            return "FrenchScriptMT"
        if "FreeStyle" in fontname:
            return "FreeStyleScript"
        return None
    for name, family in _ALT_FONT_FAMILIES:
        if name in fontname:
            return family
    return None


def _parse_style(s: str, start: int) -> str:
    region = s[start:]
    k = region.find(",")
    return region if k < 0 else region[:k]


def _get_style_type(name: str, reverse: bool) -> tuple[str, int] | None:
    """GetStyleType: the first kFontStyles entry `name` starts (or, reversed, ends) with."""
    if not name:
        return None
    for style_name, style in _FONT_STYLES:
        n = len(style_name)
        if n > len(name):
            continue
        view = name[-n:] if reverse else name[:n]
        if view == style_name:
            return style_name, style
    return None


@dataclass(frozen=True, kw_only=True)
class _Styles:
    """What ParseStyles leaves in its in-out arguments, and whether it gave the family up."""
    give_up: bool
    available: bool
    weight: int
    style: int


def _parse_styles(style_str: str, weight: int, style: int) -> _Styles:
    """ParseStyles: the style availability, weight and style after the style string (PDFium's
    in-out arguments keep what it set before giving up, and so does the answer)."""
    available = False
    i = 0
    first_item = True
    while i < len(style_str):
        buf = _parse_style(style_str, i)
        result = _get_style_type(buf, False)
        if (i and not available) or (not i and not result):
            return _Styles(give_up=True, available=available, weight=weight, style=style)
        if result:
            available = True
            parsed = result[1]
        else:
            parsed = STYLE_NORMAL
        if parsed & STYLE_FORCE_BOLD:
            if style & STYLE_FORCE_BOLD:
                weight = WEIGHT_EXTRA_BOLD
            else:
                weight = WEIGHT_BOLD
                style |= STYLE_FORCE_BOLD
            first_item = False
        if parsed & STYLE_ITALIC and parsed & STYLE_FORCE_BOLD:
            style |= STYLE_ITALIC
        elif parsed & STYLE_ITALIC:
            if not first_item:
                return _Styles(give_up=True, available=available, weight=weight, style=style)
            style |= STYLE_ITALIC
            break
        i += len(buf) + 1
    return _Styles(give_up=False, available=available, weight=weight, style=style)


def _style_from_base_font(base: int) -> int:
    style = STYLE_NORMAL
    if base < SYMBOL:
        pos = base % 4
        if pos in (1, 2):
            style |= STYLE_FORCE_BOLD
        if pos // 2:
            style |= STYLE_ITALIC
    return style


def _pitch_from_base_font(base: int) -> int:
    if base < 4:
        return PITCH_FIXED
    if base >= 8:
        return PITCH_ROMAN
    return 0


def _pitch_from_flags(flags: int) -> int:
    pitch = 0
    if flags & STYLE_SERIF:
        pitch |= PITCH_ROMAN
    if flags & STYLE_SCRIPT:
        pitch |= PITCH_SCRIPT
    if flags & STYLE_FIXED_PITCH:
        pitch |= PITCH_FIXED
    return pitch


def _adjust_base_font_for_style(base: int, style: int) -> int:
    if not style or base not in (COURIER, HELVETICA, TIMES):
        return base
    if style & STYLE_FORCE_BOLD and style & STYLE_ITALIC:
        return base + 2
    if style & STYLE_FORCE_BOLD:
        return base + 1
    if style & STYLE_ITALIC:
        return base + 3
    return base


def _without_subset_prefix(name: str) -> str:
    """MaybeRemoveSubsettedFontPrefix."""
    if len(name) > 7 and name[6] == "+" and all("A" <= c <= "Z" for c in name[:6]):
        return name[7:]
    return name


def get_subst_name(name: str, truetype: bool) -> str:
    """GetSubstName."""
    from .fonts import standard_font_index
    if truetype and name[:1] == "@":
        subst = name[1:]
    else:
        subst = name.replace(" ", "")
    subst = _without_subset_prefix(subst)
    std = standard_font_index(subst)
    return CANONICAL[std] if std is not None else subst


def _is_narrow_font_name(name: str) -> bool:
    return any(name.find(n) > 0 for n in ("Narrow", "Condensed"))


def _name_from_tt(table: bytes, name_id: int) -> str:
    """GetNameFromTT (fx_font.cpp): the Mac Roman or Windows Unicode record, UTF-8 for the latter."""
    if len(table) < 6:
        return ""
    count = int.from_bytes(table[2:4], "big")
    string_offset = int.from_bytes(table[4:6], "big")
    if len(table) < string_offset:
        return ""
    strings = table[string_offset:]
    records = table[6:]
    if len(records) < count * 12:
        return ""
    for i in range(count):
        rec = records[12 * i:12 * i + 12]
        if int.from_bytes(rec[6:8], "big") != name_id:
            continue
        platform, encoding = int.from_bytes(rec[0:2], "big"), int.from_bytes(rec[2:4], "big")
        length, offset = int.from_bytes(rec[8:10], "big"), int.from_bytes(rec[10:12], "big")
        if platform == 1 and encoding == 0:
            return strings[offset:offset + length].decode("latin-1") if len(strings) >= offset + length else ""
        if platform == 3 and encoding == 1:
            raw = strings[offset:offset + length] if len(strings) >= offset + length else b""
            if not raw or len(raw) % 2:
                return ""
            return raw.decode("utf-16-be", "replace").encode("utf-8", "replace").decode("latin-1")
    return ""


# ---------------------------------------------------------------------- CFX_Win32FontInfo


H = TypeVar("H")


class FontInfo(Protocol[H]):
    """SystemFontInfoIface, over the handle type `H` its fonts are named by."""

    @property
    def narrow_family(self) -> str:
        """kNarrowFamily: "ArialNarrow" on Windows, "LiberationSansNarrow" on Linux."""
        ...

    @property
    def symbol_internal(self) -> bool:
        """FindSubstFace's `#if !BUILDFLAG(IS_WIN)` Symbol branch."""
        ...

    def enum_font_list(self, mapper: FontMapper[H]) -> None: ...

    def map_font(self, weight: int, italic: bool, charset: int, pitch_family: int, face: str) -> H | None: ...

    def get_font(self, face: str) -> H | None: ...

    def get_font_data(self, font: H, table: int, size: int | None) -> tuple[int, bytes]: ...

    def get_face_name(self, font: H) -> str | None: ...

    def delete_font(self, font: H) -> None: ...

    def font_path(self, font: H) -> bytes | None:
        """The file a font handle reads, where there is one (the folder scan's)."""
        ...


class _LogFont(Protocol):
    """The LOGFONTA fields the enumeration callback reads."""
    lfFaceName: bytes
    lfCharSet: int


class _LogFontPointer(Protocol):
    @property
    def contents(self) -> _LogFont: ...


_EnumCallback: TypeAlias = Callable[[_LogFontPointer, object, int, int], int]


def _windll(name: str) -> ctypes.CDLL:
    """ctypes.WinDLL (stdcall, the last error kept): Windows only."""
    if sys.platform == "win32":
        return ctypes.WinDLL(name, use_last_error=True)
    raise OSError(f"{name}: a Windows library")


class Win32FontInfo:
    """CFX_Win32FontInfo: GDI through ctypes. Font handles are HFONTs (ints)."""

    narrow_family = NARROW_FAMILY
    symbol_internal = False
    gdi: ctypes.CDLL
    dc: int | None
    LOGFONTA: type[ctypes.Structure]
    ENUMPROC: Callable[[_EnumCallback], object]

    def __init__(self) -> None:
        from ctypes import wintypes
        gdi = _windll("gdi32")
        HFONT, HDC, HGDIOBJ = wintypes.HANDLE, wintypes.HDC, wintypes.HGDIOBJ
        gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [HDC], HDC
        gdi.CreateFontA.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [ctypes.c_char_p]
        gdi.CreateFontA.restype = HFONT
        gdi.SelectObject.argtypes, gdi.SelectObject.restype = [HDC, HGDIOBJ], HGDIOBJ
        gdi.DeleteObject.argtypes, gdi.DeleteObject.restype = [HGDIOBJ], wintypes.BOOL
        gdi.GetTextFaceA.argtypes, gdi.GetTextFaceA.restype = [HDC, ctypes.c_int, ctypes.c_char_p], ctypes.c_int
        gdi.GetFontData.argtypes = [HDC, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        gdi.GetFontData.restype = wintypes.DWORD

        class LOGFONTA(ctypes.Structure):
            _fields_ = [("lfHeight", wintypes.LONG), ("lfWidth", wintypes.LONG), ("lfEscapement", wintypes.LONG),
                        ("lfOrientation", wintypes.LONG), ("lfWeight", wintypes.LONG), ("lfItalic", wintypes.BYTE),
                        ("lfUnderline", wintypes.BYTE), ("lfStrikeOut", wintypes.BYTE),
                        ("lfCharSet", wintypes.BYTE), ("lfOutPrecision", wintypes.BYTE),
                        ("lfClipPrecision", wintypes.BYTE), ("lfQuality", wintypes.BYTE),
                        ("lfPitchAndFamily", wintypes.BYTE), ("lfFaceName", ctypes.c_char * 32)]

        self.LOGFONTA = LOGFONTA
        functype = ctypes.WINFUNCTYPE if sys.platform == "win32" else ctypes.CFUNCTYPE   # stdcall on Windows
        self.ENUMPROC = functype(ctypes.c_int, ctypes.POINTER(LOGFONTA), ctypes.c_void_p, wintypes.DWORD,
                                 wintypes.LPARAM)
        gdi.EnumFontFamiliesExA.argtypes = [HDC, ctypes.POINTER(LOGFONTA), self.ENUMPROC, wintypes.LPARAM,
                                            wintypes.DWORD]
        gdi.EnumFontFamiliesExA.restype = ctypes.c_int
        gdi.CreateFontIndirectA.argtypes, gdi.CreateFontIndirectA.restype = [ctypes.POINTER(LOGFONTA)], HFONT
        self.gdi = gdi
        self.dc = gdi.CreateCompatibleDC(None)

    # Win32CreateFont
    def _create(self, weight: int, italic: bool, charset: int, pitch_family: int, face: str) -> int | None:
        hfont: int | None = self.gdi.CreateFontA(-10, 0, 0, 0, weight, int(italic), 0, 0, charset & 0xFF,
                                                 OUT_TT_ONLY_PRECIS, 0, 0, pitch_family & 0xFF,
                                                 face.encode("latin-1", "replace"))
        return hfont

    def _select(self, hfont: int | None) -> int | None:
        old: int | None = self.gdi.SelectObject(self.dc, hfont)
        return old

    def get_font_data(self, font: int, table: int, size: int | None) -> tuple[int, bytes]:
        """GetFontData: (the size GDI answers, 0 on GDI_ERROR; the bytes read when `size` is given)."""
        old = self._select(font)
        try:
            if size is None:
                n: int = self.gdi.GetFontData(self.dc, table, 0, None, 0)
                return (0 if n == GDI_ERROR else n), b""
            buf = ctypes.create_string_buffer(max(size, 1))
            n = self.gdi.GetFontData(self.dc, table, 0, buf, size)
            n = 0 if n == GDI_ERROR else n
            return n, buf.raw[:min(n, size)]
        finally:
            self._select(old)

    def get_face_name(self, font: int | None) -> str | None:
        old = self._select(font)
        try:
            buf = ctypes.create_string_buffer(100)
            if self.gdi.GetTextFaceA(self.dc, 100, buf) == 0:
                return None
            return buf.value.decode("latin-1")
        finally:
            self._select(old)

    def delete_font(self, font: int | None) -> None:
        self.gdi.DeleteObject(font)

    def get_font(self, face: str) -> int | None:
        return None

    def font_path(self, font: int) -> bytes | None:
        return None

    def _is_supported_font(self, plf: _LogFontPointer) -> bool:
        hfont: int = self.gdi.CreateFontIndirectA(plf) or 0     # NULL comes back as None; 0 is NULL too
        ret = False
        size, _ = self.get_font_data(hfont, TABLE_NONE, None)
        if size >= 4:
            _, head = self.get_font_data(hfont, TABLE_NONE, 4)
            header = int.from_bytes(head[:4].ljust(4, b"\0"), "big")
            ret = header in (int.from_bytes(b"OTTO", "big"), int.from_bytes(b"ttcf", "big"),
                             int.from_bytes(b"true", "big"), 0x00010000, 0x00020000) \
                or header & 0xFFFF0000 in (0x80010000, int.from_bytes(b"%!\0\0", "big"))
        self.delete_font(hfont)
        return ret

    def enum_font_list(self, mapper: FontMapper[int]) -> None:
        """EnumFontList + FontEnumCallback + AddInstalledFont."""
        last = [""]

        def callback(plf: _LogFontPointer, _tm: object, font_type: int, _lparam: int) -> int:
            lf = plf.contents
            name = lf.lfFaceName.decode("latin-1")
            charset = lf.lfCharSet & 0xFF
            if name[:1] == "@":
                return 1
            if name == last[0]:
                mapper.add_installed_font(name, charset)
                return 1
            if not font_type & TRUETYPE_FONTTYPE:
                if not font_type & DEVICE_FONTTYPE or not self._is_supported_font(plf):
                    return 1
            mapper.add_installed_font(name, charset)
            last[0] = name
            return 1

        lf = self.LOGFONTA()
        lf.lfCharSet = CHARSET_DEFAULT
        proc = self.ENUMPROC(callback)
        self.gdi.EnumFontFamiliesExA(self.dc, ctypes.byref(lf), proc, 0, 0)

    def map_font(self, weight: int, italic: bool, charset: int, pitch_family: int, face: str) -> int | None:
        """CFX_Win32FontInfo::MapFont. The CJK preferences after a failed match are not ported: a
        simple font always asks for the ANSI or the symbol charset, which ends at DEFAULT_CHARSET."""
        new_face = face
        for name, win_name, bold, it in _BASE14_SUBSTS:
            if new_face == name:
                new_face, weight, italic = win_name, (FW_BOLD if bold else FW_NORMAL), it
                break
        if charset in (CHARSET_ANSI, CHARSET_SYMBOL):
            charset = CHARSET_DEFAULT
        if charset == 128:                        # ShiftJIS: FF_ROMAN
            subst_pitch = PITCH_ROMAN
        elif charset in (136, 129, 134):
            subst_pitch = 0
        else:
            subst_pitch = pitch_family
        hfont = self._create(weight, italic, charset, subst_pitch, new_face)
        actual = self.get_face_name(hfont)
        if actual is not None and _equal_no_case(new_face, actual):
            return hfont
        for variant_face, variant_name in _VARIANT_NAMES:
            if new_face == variant_face and actual is not None:
                try:
                    wide = actual.encode("latin-1").decode("mbcs")
                except (UnicodeError, LookupError):
                    wide = ""
                if wide == variant_name:
                    return hfont
        self.delete_font(hfont)
        if charset == CHARSET_DEFAULT:
            return None
        raise NotImplementedError("CJK font preferences")     # unreachable for simple fonts


# ---------------------------------------------------------------------- CFX_FolderFontInfo

# FX_Charset of each ulCodePageRange1 bit ReportFace reads, in its order
_CODEPAGE_CHARSETS = ((1, 238), (2, 204), (3, 161), (4, 162), (5, 177), (6, 178), (7, 186), (8, 163),
                      (16, 222), (17, 128), (18, 134), (19, 129), (20, 136), (21, 130), (30, 255), (31, 2))

# CFX_FolderFontInfo's (and CFX_MacFontInfo's) kBase14Substs: name -> face name in the folders
_FOLDER_BASE14 = {"Courier": "Courier New", "Courier-Bold": "Courier New Bold",
                  "Courier-BoldOblique": "Courier New Bold Italic", "Courier-Oblique": "Courier New Italic",
                  "Helvetica": "Arial", "Helvetica-Bold": "Arial Bold", "Helvetica-BoldOblique": "Arial Bold Italic",
                  "Helvetica-Oblique": "Arial Italic", "Times-Roman": "Times New Roman",
                  "Times-Bold": "Times New Roman Bold", "Times-BoldItalic": "Times New Roman Bold Italic",
                  "Times-Italic": "Times New Roman Italic"}


def _table_location(tables: bytes, tag: bytes) -> tuple[int, int] | None:
    """FindFontTableLocation: (offset, size) of the first directory entry with that tag."""
    for i in range(len(tables) // 16):
        entry = tables[16 * i:16 * i + 16]
        if entry[:4] == tag:
            return int.from_bytes(entry[8:12], "big"), int.from_bytes(entry[12:16], "big")
    return None


@dataclass(frozen=True, kw_only=True, eq=False)
class FolderFace:
    """CFX_FolderFontInfo::FontFaceInfo: the font handle (compared by identity, as a pointer)."""
    path: bytes
    face_name: str
    tables: bytes
    offset: int
    file_size: int
    charsets: frozenset[int]
    styles: int

    def eligible(self, charset: int) -> bool:
        return charset in self.charsets or charset == CHARSET_DEFAULT

    def similarity(self, weight: int, italic: bool, pitch_family: int, exact_bonus: bool) -> int:
        score = 0
        if bool(self.styles & STYLE_FORCE_BOLD) == (weight > 400):
            score += 16
        if bool(self.styles & STYLE_ITALIC) == italic:
            score += 16
        if bool(self.styles & STYLE_SERIF) == bool(pitch_family & PITCH_ROMAN):
            score += 16
        if bool(self.styles & STYLE_SCRIPT) == bool(pitch_family & PITCH_SCRIPT):
            score += 8
        if bool(self.styles & STYLE_FIXED_PITCH) == bool(pitch_family & PITCH_FIXED):
            score += 8
        if exact_bonus:
            score += 4
        return score


_SCORE_MAX = 68      # FontFaceInfo::kSimilarityScoreMax


def _family_name_match(family: str, installed: str) -> bool:
    """FindFamilyNameMatch: `family` in the name, not followed by a lowercase ASCII letter."""
    k = installed.find(family)
    if k < 0:
        return False
    nxt = k + len(family)
    return not (nxt < len(installed) and "a" <= installed[nxt] <= "z")


class FolderFontInfo:
    """CFX_FolderFontInfo: the fonts found in a list of folders (PDFium's font info off Windows).
    Handles are `FolderFace`s. Folders are read as fx_folder_posix reads them: readdir order,
    `stat` through links, and a name `stat` refuses ends that folder's scan."""

    narrow_family = "ArialNarrow"
    symbol_internal = True           # FindSubstFace's `#if !BUILDFLAG(IS_WIN)` Symbol branch

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = [os.fsencode(p) for p in paths]
        self.font_list: dict[str, FolderFace] = {}

    # EnumFontList, ScanPath, ScanFile
    def enum_font_list(self, mapper: FontMapper[FolderFace]) -> None:
        for path in self.paths:
            self._scan_path(mapper, path)
        self.font_list = dict(sorted(self.font_list.items()))     # a std::map, ordered bytewise

    def _scan_path(self, mapper: FontMapper[FolderFace], path: bytes) -> None:
        try:
            it = os.scandir(path)
        except OSError:
            return
        with it:
            for entry in it:
                full = path + b"/" + os.fsencode(entry.name)
                try:
                    is_dir = stat.S_ISDIR(os.stat(full).st_mode)
                except OSError:
                    return
                name = os.fsencode(entry.name)
                if is_dir:
                    if name in (b".", b".."):
                        continue
                    self._scan_path(mapper, full)
                elif name[-4:].lower() in (b".ttf", b".ttc", b".otf"):
                    self._scan_file(mapper, full)

    def _scan_file(self, mapper: FontMapper[FolderFace], path: bytes) -> None:
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(0)
                head = f.read(12)
                if len(head) != 12:
                    return
                if head[:4] != b"ttcf":
                    self._report_face(mapper, path, f, size, 0)
                    return
                n = int.from_bytes(head[8:12], "big")
                offsets = f.read(4 * n)
                if len(offsets) != 4 * n:
                    return
                for i in range(n):
                    self._report_face(mapper, path, f, size, int.from_bytes(offsets[4 * i:4 * i + 4], "big"))
        except OSError:
            return

    @staticmethod
    def _at(f: BinaryIO, size: int, loc: tuple[int, int]) -> bytes | None:
        """DataVectorAtLocation."""
        offset, length = loc
        if offset + length > size:
            return None
        f.seek(offset)
        data = f.read(length)
        return data if len(data) == length else None

    def _report_face(self, mapper: FontMapper[FolderFace], path: bytes, f: BinaryIO, size: int, offset: int) -> None:
        f.seek(offset)
        head = f.read(12)
        if len(head) != 12:
            return
        n = int.from_bytes(head[4:6], "big") * 16
        tables = f.read(n)
        if len(tables) != n or not tables:
            return
        loc = _table_location(tables, b"name")
        names = self._at(f, size, loc) if loc else None
        if names is None:
            return
        facename = _name_from_tt(names, 1)
        if not facename:
            return
        style = _name_from_tt(names, 2)
        if style != "Regular":
            facename += " " + style
        if facename in self.font_list:
            return
        loc = _table_location(tables, b"OS/2")
        os2 = self._at(f, size, loc) if loc else None
        codepages = int.from_bytes(os2[78:82], "big") if os2 is not None and len(os2) >= 86 else 0
        charsets: set[int] = set()
        for bit, charset in _CODEPAGE_CHARSETS:
            if codepages & (1 << bit):
                mapper.add_installed_font(facename, charset)
                charsets.add(charset)
        mapper.add_installed_font(facename, CHARSET_ANSI)
        charsets.add(CHARSET_ANSI)
        styles = 0
        if "Bold" in style:
            styles |= STYLE_FORCE_BOLD
        if "Italic" in style or "Oblique" in style:
            styles |= STYLE_ITALIC
        if "Serif" in facename:
            styles |= STYLE_SERIF
        self.font_list[facename] = FolderFace(path=path, face_name=facename, tables=tables, offset=offset,
                                              file_size=size, charsets=frozenset(charsets), styles=styles)

    def get_subst_font(self, face: str) -> FolderFace | None:
        subst = _FOLDER_BASE14.get(face)
        return self.get_font(subst) if subst is not None else None

    def find_font(self, weight: int, italic: bool, charset: int, pitch_family: int, family: str,
                  must_match_name: bool) -> FolderFace | None:
        found: FolderFace | None = None
        best = 0
        if must_match_name:
            font = self.font_list.get(family)
            if font is not None and font.eligible(charset):
                best, found = font.similarity(weight, italic, pitch_family, True), font
                if best == _SCORE_MAX:
                    return font
        for font in self.font_list.values():
            if not font.eligible(charset):
                continue
            score = font.similarity(weight, italic, pitch_family,
                                    must_match_name and len(family) == len(font.face_name))
            if score > best and (not must_match_name or _family_name_match(family, font.face_name)):
                best, found = score, font
        if found is not None:
            return found
        if charset == CHARSET_ANSI and pitch_family & PITCH_FIXED:
            return self.get_font("Courier New")
        return None

    def map_font(self, weight: int, italic: bool, charset: int, pitch_family: int, face: str) -> FolderFace | None:
        return None

    def get_font(self, face: str) -> FolderFace | None:
        return self.font_list.get(face)

    def font_path(self, font: FolderFace) -> bytes | None:
        return font.path

    def get_font_data(self, font: FolderFace, table: int, size: int | None) -> tuple[int, bytes]:
        """GetFontData: the size alone when `size` is None or too small (nothing is read then)."""
        offset = 0
        if table == TABLE_NONE:
            datasize = 0 if font.offset else font.file_size
        elif table == TABLE_TTCF:
            datasize = font.file_size if font.offset else 0
        else:
            loc = _table_location(font.tables, table.to_bytes(4, "little"))
            offset, datasize = loc if loc else (0, 0)
        if not datasize or size is None or size < datasize:
            return datasize, b""
        try:
            with open(font.path, "rb") as f:
                f.seek(offset)
                data = f.read(datasize)
        except OSError:
            return 0, b""
        return (datasize, data) if len(data) == datasize else (0, b"")

    def delete_font(self, font: FolderFace) -> None:
        pass

    def get_face_name(self, font: FolderFace) -> str | None:
        return font.face_name


class LinuxFontInfo(FolderFontInfo):
    """CFX_LinuxFontInfo (core/fxge/linux/fx_linux_impl.cpp). Its CJK tables are not ported: a
    simple font asks for the ANSI or the symbol charset."""

    narrow_family = "LiberationSansNarrow"
    PATHS = ("/usr/share/fonts", "/usr/share/X11/fonts/Type1", "/usr/share/X11/fonts/TTF", "/usr/local/share/fonts")

    @override
    def map_font(self, weight: int, italic: bool, charset: int, pitch_family: int, face: str) -> FolderFace | None:
        font = self.get_subst_font(face)
        if font is not None:
            return font
        if charset in _CJK_CHARSETS:
            raise NotImplementedError("CJK font preferences")     # unreachable for simple fonts
        return self.find_font(weight, italic, charset, pitch_family, face, True)


class MacFontInfo(FolderFontInfo):
    """CFX_MacFontInfo (core/fxge/apple/capple_platform.cpp). "~" is not expanded: PDFium's first
    folder is opened as written and never found."""

    PATHS = ("~/Library/Fonts", "/Library/Fonts", "/System/Library/Fonts")

    @override
    def map_font(self, weight: int, italic: bool, charset: int, pitch_family: int, face: str) -> FolderFace | None:
        if face in _FOLDER_BASE14:
            return self.get_font(_FOLDER_BASE14[face])
        if "Bold" not in face and "Italic" not in face:
            new_face = face + (" Bold" if weight > 400 else "") + (" Italic" if italic else "")
            if new_face in self.font_list:
                return self.font_list[new_face]
        if face in self.font_list:
            return self.font_list[face]
        if charset == CHARSET_ANSI and pitch_family & PITCH_FIXED:
            return self.get_font("Courier New")
        if charset in (CHARSET_ANSI, CHARSET_SYMBOL):
            return None
        if charset in _CJK_CHARSETS:
            raise NotImplementedError("CJK font preferences")     # unreachable for simple fonts
        return self.font_list.get(face)


# ---------------------------------------------------------------------- CFX_FontMapper


# CFX_SubstFont's tables (cfx_substfont.cpp)
_WEIGHT_POW = (
    0, 6, 12, 14, 16, 18, 22, 24, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50, 52, 54, 56, 58, 60, 62, 64, 66,
    68, 70, 70, 72, 72, 74, 74, 74, 76, 76, 76, 78, 78, 78, 80, 80, 80, 82, 82, 82, 84, 84, 84, 84, 86, 86, 86, 88,
    88, 88, 88, 90, 90, 90, 90, 92, 92, 92, 92, 94, 94, 94, 94, 96, 96, 96, 96, 96, 98, 98, 98, 98, 100, 100, 100,
    100, 100, 102, 102, 102, 102, 102, 104, 104, 104, 104, 104, 106, 106, 106, 106, 106)
_WEIGHT_POW_11 = (
    0, 4, 7, 8, 9, 10, 12, 13, 15, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37,
    39, 39, 40, 40, 41, 41, 41, 42, 42, 42, 43, 43, 43, 44, 44, 44, 45, 45, 45, 46, 46, 46, 46, 43, 47, 47, 48, 48,
    48, 48, 45, 50, 50, 50, 46, 51, 51, 51, 52, 52, 52, 52, 53, 53, 53, 53, 53, 54, 54, 54, 54, 55, 55, 55, 55, 55,
    56, 56, 56, 56, 56, 57, 57, 57, 57, 57, 58, 58, 58, 58, 58)
_WEIGHT_POW_SHIFT_JIS = (
    0, 0, 2, 4, 6, 8, 10, 14, 16, 20, 22, 26, 28, 32, 34, 38, 42, 44, 48, 52, 56, 60, 64, 66, 70, 74, 78, 82, 86, 90,
    96, 96, 96, 96, 98, 98, 98, 100, 100, 100, 100, 102, 102, 102, 102, 104, 104, 104, 104, 104, 106, 106, 106, 106,
    106, 108, 108, 108, 108, 108, 110, 110, 110, 110, 110, 112, 112, 112, 112, 112, 112, 114, 114, 114, 114, 114, 114,
    114, 116, 116, 116, 116, 116, 116, 116, 118, 118, 118, 118, 118, 118, 118, 120, 120, 120, 120, 120, 120, 120, 120)
_ANGLE_SKEW = (0, -2, -3, -5, -7, -9, -11, -12, -14, -16, -18, -19, -21, -23, -25, -27, -29, -31, -32, -34, -36, -38,
               -40, -42, -45, -47, -49, -51, -53, -55)
CHARSET_SHIFT_JIS = 128


def skew_from_angle(angle: int) -> int:
    """GetSkewFromAngle."""
    if angle > 0 or angle == -0x80000000 or -angle >= len(_ANGLE_SKEW):
        return -58
    return _ANGLE_SKEW[-angle]


class SubstFont:
    """CFX_SubstFont: how PDFium varies a substitute face when it draws it. It starts as the
    constructor leaves it and FindSubstFace fills it in as it goes."""

    def __init__(self) -> None:
        self.family = ""
        self.charset = CHARSET_ANSI
        self.weight = 0
        self.italic_angle = 0
        self.weight_cjk = 0
        self.subst_cjk = False
        self.italic_cjk = False
        self.flag_mm = False                  # IsBuiltInGenericFont: drawn with a multiple master face

    def use_chrome_serif(self) -> None:
        self.weight = cdiv(self.weight * 4, 5)
        self.family = "Chrome Serif"

    def skew(self) -> int:
        return skew_from_angle(self.italic_angle)

    def effective_skew(self, is_cid: bool) -> int:
        if self.subst_cjk and is_cid:
            return skew_from_angle(-15 if self.italic_cjk else 0)
        return self.skew()

    def effective_weight(self, is_cid: bool) -> int:
        return self.weight_cjk if self.subst_cjk and is_cid else self.weight

    def embolden_level_for_render(self, is_cid: bool, xx: int, xy: int) -> int:
        """GetEmboldenLevelForRender: the FT_Outline_Embolden strength, or -1 (no glyph)."""
        if self.flag_mm:
            return 0
        weight = self.effective_weight(is_cid)
        if weight <= 400:
            return 0
        index = cdiv(weight - 400, 10)
        if index >= len(_WEIGHT_POW_11):
            return -1
        level = (_WEIGHT_POW_SHIFT_JIS if self.charset == CHARSET_SHIFT_JIS else _WEIGHT_POW_11)[index]
        v = cdiv(level * (abs(xx) + abs(xy)), 36655)
        return v if -0x80000000 <= v <= 0x7FFFFFFF else 0            # ValueOrDefault(0)

    def embolden_level_for_load(self) -> int:
        if self.flag_mm or self.weight <= 400:
            return 0
        index = min(cdiv(self.weight - 400, 10), len(_WEIGHT_POW) - 1)
        if self.charset == CHARSET_SHIFT_JIS:
            return cdiv(_WEIGHT_POW_SHIFT_JIS[index] * 65536, 36655)
        return _WEIGHT_POW[index]

    def configure_external(self, face_name: str, charset: int, weight: int, is_italic: bool, italic_angle: int,
                           face_is_bold: bool, face_is_italic: bool) -> None:
        """ConfigureExternalSubst."""
        self.family = face_name
        self.charset = charset
        if weight != (WEIGHT_BOLD if face_is_bold else WEIGHT_NORMAL):
            self.weight = weight
        if is_italic and not face_is_italic:
            if italic_angle == 0:
                italic_angle = -12
            elif abs(italic_angle) < 5:
                italic_angle = 0
            self.italic_angle = italic_angle


TtcKey: TypeAlias = "tuple[int, int, bytes | None]"      # (ttc size, checksum, the file where there is one)
FaceKey: TypeAlias = "tuple[str, int, bool]"             # (subst name, weight, italic)
CacheKey: TypeAlias = "tuple[Literal['ttc'], TtcKey] | tuple[Literal['face'], FaceKey]"


@dataclass(frozen=True, kw_only=True)
class _Face:
    """What FindSubstFace hands back: the face, whether it is PDFium's generic multiple master face
    (CFX_SubstFont::IsBuiltInGenericFont), and the CFX_SubstFont it filled in. `key` names the cache
    entry a system face came from, for `hold` and `release` (None: a built-in face, never dropped)."""
    program: Program
    generic: bool
    subst: SubstFont
    key: CacheKey | None


class _TtcEntry:
    """CFX_FontMapper::FontCacheEntry for a collection: the bytes GDI gave, and the faces cut out of
    them. It lives as long as the rest of the cache does (see the module docstring)."""

    __slots__ = ("data", "faces", "failed")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.faces: dict[int, Program] = {}
        self.failed: set[int] = set()


def _held_faces(doc: PdfFile) -> set[CacheKey]:
    """The cache entries a document's fonts hold (kept on the document, which closes them)."""
    held: set[CacheKey] = doc.__dict__.setdefault("_b2s_held_faces", set())
    return held


class FontMapper(Generic[H]):
    """CFX_FontMapper over a font info whose fonts are named by handles of type `H`."""

    def __init__(self, font_info: FontInfo[H] | None) -> None:
        self.font_info = font_info
        self.list_loaded = False
        self.installed: list[str] = []
        self.localized: list[tuple[str, str]] = []      # (PS name, family)
        self.face_array: list[tuple[str, int]] = []     # (name, charset)
        self.last_family = ""
        self.standard_faces: dict[int, Program | None] = {}
        self.generic: dict[str, GenericProgram | None] = {}
        # ObservedPtr entries in PDFium: an entry is dropped when the last document holding it closes
        self.face_map: dict[FaceKey, Program] = {}
        self.face_failed: set[FaceKey] = set()          # keys whose bytes FreeType would not open
        self.ttc_face_map: dict[TtcKey, _TtcEntry] = {}
        self.holders: dict[CacheKey, int] = {}          # cache key -> how many open documents hold it

    def hold(self, face: _Face | None, doc: PdfFile | None) -> None:
        """A document's font took this face; PDFium's CFX_Font retains it while the font lives."""
        if face is None or face.key is None or doc is None:
            return
        held = _held_faces(doc)
        if face.key not in held:
            held.add(face.key)
            self.holders[face.key] = self.holders.get(face.key, 0) + 1

    def release(self, doc: PdfFile) -> None:
        """A document is gone, and so are its CPDF_Fonts and the CFX_Fonts under them: an entry no
        other open document holds is observed away, bytes and all."""
        held: set[CacheKey] = doc.__dict__.pop("_b2s_held_faces", set())
        for key in held:
            left = self.holders.get(key, 0) - 1
            if left > 0:
                self.holders[key] = left
                continue
            self.holders.pop(key, None)
            if key[0] == "ttc":
                self.ttc_face_map.pop(key[1], None)
            else:
                self.face_map.pop(key[1], None)
                self.face_failed.discard(key[1])

    def add_installed_font(self, name: str, charset: int) -> None:
        info = self.font_info
        if info is None:
            return
        self.face_array.append((name, charset))
        if name == self.last_family:
            return
        if any(ord(c) > 0x80 for c in name):
            hfont = info.get_font(name)
            if not hfont:
                hfont = info.map_font(0, False, CHARSET_DEFAULT, 0, name)
                if not hfont:
                    return
            try:
                new_name = _ps_name_from_tt(info, hfont)
            finally:
                info.delete_font(hfont)
            if new_name:
                self.localized.append((new_name, name))
        self.installed.append(name)
        self.last_family = name

    def _load_installed_fonts(self) -> None:
        if self.font_info is None or self.list_loaded:
            return
        self.font_info.enum_font_list(self)
        self.list_loaded = True

    def match_installed_fonts(self, norm_name: str) -> str:
        self._load_installed_fonts()
        for font in reversed(self.installed):
            if tt_normalize_name(font) == norm_name:
                return font
        for ps_name, family in reversed(self.localized):
            if tt_normalize_name(ps_name) == norm_name:
                return family
        return ""

    def use_internal_subst(self, base_font: int | None, pitch_family: int, weight: int,
                           italic_angle: int, subst: SubstFont) -> _Face | None:
        from .fonts import load_cff, load_generic
        if base_font is not None:
            if base_font not in self.standard_faces:
                data = foxit.face_data(foxit.STANDARD[base_font])
                standard: Program | None = None
                if data is not None:
                    try:
                        standard = load_cff(data)
                        standard.face_data = standard.platform_data = data   # what render_text draws the glyphs from
                    except Exception:  # noqa: BLE001
                        standard = None
                self.standard_faces[base_font] = standard
            prog = self.standard_faces[base_font]
            return _Face(program=prog, generic=False, subst=subst, key=None) if prog is not None else None
        subst.flag_mm = True
        subst.italic_angle = italic_angle
        if weight:
            subst.weight = weight
        if pitch_family & PITCH_ROMAN:
            subst.use_chrome_serif()
            name = foxit.GENERIC_SERIF
        else:
            subst.family = "Chrome Sans"
            name = foxit.GENERIC_SANS
        if name not in self.generic:
            data = foxit.face_data(name)
            generic = load_generic(data) if data is not None else None
            self.generic[name] = generic
            if generic is not None:
                generic.platform_data = data
        mm = self.generic[name]
        return _Face(program=mm, generic=True, subst=subst, key=None) if mm is not None else None

    def use_external_subst(self, info: FontInfo[H], hfont: H, face_name: str, weight: int, italic: bool,
                           italic_angle: int, charset: int, subst: SubstFont) -> _Face | None:
        from .fonts import load_truetype
        cache_key: CacheKey
        prog: Program | None
        try:
            actual = info.get_face_name(hfont)          # the cache key is the face GDI gave
            if actual is not None:
                face_name = actual
            ttc_size, _ = info.get_font_data(hfont, TABLE_TTCF, None)
            font_size, _ = info.get_font_data(hfont, TABLE_NONE, None)
            if font_size == 0 and ttc_size == 0:
                return None
            if ttc_size:
                _, head = info.get_font_data(hfont, TABLE_TTCF, 1024)
                head = head.ljust(1024, b"\0")
                checksum = sum(int.from_bytes(head[k:k + 4], "little") for k in range(0, 1024, 4)) & 0xFFFFFFFF
                # a folder font info reads nothing into a buffer smaller than the file: PDFium then sums
                # whatever the stack held, so only the file itself tells two collections apart
                ttc_key = (ttc_size, checksum, info.font_path(hfont))
                cache_key = ("ttc", ttc_key)
                entry = self.ttc_face_map.get(ttc_key)
                if entry is None:
                    n, data = info.get_font_data(hfont, TABLE_TTCF, ttc_size)
                    if n != ttc_size:
                        return None
                    entry = self.ttc_face_map[ttc_key] = _TtcEntry(data)
                data = entry.data
                index = _ttc_index(data, ttc_size - font_size)
                prog = entry.faces.get(index)
                if prog is None and index not in entry.failed:
                    try:
                        prog = load_truetype(data, index)
                    except Exception:  # noqa: BLE001 - a face FreeType would not open either
                        prog = None
                    if prog is None:
                        entry.failed.add(index)
                    else:
                        entry.faces[index] = prog
                if prog is not None:
                    prog.platform_data = data                # CFX_Font's span: the whole collection
            else:
                face_key = (face_name, weight, italic)
                cache_key = ("face", face_key)
                prog = self.face_map.get(face_key)
                if prog is None and face_key not in self.face_failed:
                    n, data = info.get_font_data(hfont, TABLE_NONE, font_size)
                    if n != font_size:
                        return None
                    try:
                        prog = load_truetype(data, 0)
                    except Exception:  # noqa: BLE001
                        prog = None
                    if prog is None:    # FT_Open_Face refused the bytes (an exception, or sfnt.FaceError)
                        self.face_failed.add(face_key)
                    else:
                        prog.platform_data = data
                        self.face_map[face_key] = prog
        finally:
            info.delete_font(hfont)
        if prog is None:
            return None
        # GetFaceName succeeds on every font info, so the family is its face name, never the face's own
        bold, italic_face = _face_style(prog)
        subst.configure_external(face_name, charset, weight, italic, italic_angle, bold, italic_face)
        return _Face(program=prog, generic=False, subst=subst, key=cache_key)

    def find_subst_face(self, name: str, truetype: bool, flags: int, weight: int, italic_angle: int,
                        subst: SubstFont) -> _Face | None:
        """CFX_FontMapper::FindSubstFace for code page kDefANSI (simple fonts)."""
        from .fonts import standard_font_index
        if weight == 0:
            weight = WEIGHT_NORMAL
        if not flags & USE_EXTERN_ATTR:
            weight = WEIGHT_NORMAL
            italic_angle = 0
        subst_name = get_subst_name(name, truetype)
        if subst_name == "Symbol" and not truetype:
            subst.family, subst.charset = "Chrome Symbol", CHARSET_SYMBOL
            return self.use_internal_subst(SYMBOL, 0, weight, italic_angle, subst)
        if subst_name == "ZapfDingbats":
            subst.family, subst.charset = "Chrome Dingbats", CHARSET_SYMBOL
            return self.use_internal_subst(DINGBATS, 0, weight, italic_angle, subst)
        style = ""
        has_comma = False
        pos = subst_name.find(",")
        if pos >= 0:
            family = subst_name[:pos]
            std_font = standard_font_index(family)
            if std_font is not None:
                family = CANONICAL[std_font]
            style = subst_name[pos + 1:]
            has_comma = True
        else:
            family = subst_name
            std_font = standard_font_index(family)
        has_hyphen = False
        base_font = None
        if std_font is not None and std_font < SYMBOL:
            base_font = std_font
            n_style = _style_from_base_font(base_font)
            pitch_family = _pitch_from_base_font(base_font)
        else:
            n_style = STYLE_NORMAL
            if not has_comma:
                pos = family.rfind("-")
                if pos >= 0:
                    style = family[pos + 1:]
                    family = family[:pos]
                    has_hyphen = True
            if not has_hyphen:
                result = _get_style_type(family, True)
                if result:
                    family = family[:len(family) - len(result[0])]
                    n_style |= result[1]
            pitch_family = _pitch_from_flags(flags)
        old_weight = weight
        if n_style & STYLE_FORCE_BOLD:
            weight = WEIGHT_BOLD
        styles = _parse_styles(style, weight, n_style)
        if styles.give_up:
            family = subst_name
            base_font = None
        weight, n_style, style_available = styles.weight, styles.style, styles.available
        info = self.font_info
        if info is None:
            return self.use_internal_subst(base_font, pitch_family, old_weight, italic_angle, subst)
        charset = CHARSET_SYMBOL if flags & STYLE_SYMBOLIC and base_font is None else CHARSET_ANSI
        is_cjk = charset in _CJK_CHARSETS
        is_italic = bool(n_style & STYLE_ITALIC)
        maybe = _get_font_family(n_style, family)
        if maybe:
            family = maybe
        match = self.match_installed_fonts(tt_normalize_name(family))
        if not match and family != subst_name and \
                (not has_comma and (not has_hyphen or (has_hyphen and not style_available))):
            match = self.match_installed_fonts(tt_normalize_name(subst_name))
        if not match and base_font is None:
            if not is_cjk:
                if family == "MyriadPro":                    # CheckSupportThirdPartFont
                    pitch_family &= ~PITCH_ROMAN
                else:
                    is_italic = italic_angle != 0
                    weight = old_weight               # skip_font_enumeration_ is false on Windows
                if _is_narrow_font_name(subst_name):
                    family = info.narrow_family
            if flags & STYLE_ITALIC:
                is_italic = True
        else:
            italic_angle = 0
            if n_style == STYLE_NORMAL:
                weight = WEIGHT_NORMAL
            if match:
                family = match
            if base_font is not None:
                base_font = _adjust_base_font_for_style(base_font, n_style)
                family = CANONICAL[base_font]
        hfont = info.map_font(weight, is_italic, charset, pitch_family, family)
        if hfont:
            return self.use_external_subst(info, hfont, subst_name, weight, is_italic, italic_angle, charset, subst)
        if match:
            hfont = info.get_font(match)
            if not hfont:
                return self.use_internal_subst(base_font, pitch_family, old_weight, italic_angle, subst)
            return self.use_external_subst(info, hfont, subst_name, weight, is_italic, italic_angle, charset, subst)
        if charset == CHARSET_SYMBOL:
            if info.symbol_internal and subst_name == "Symbol":
                subst.family, subst.charset = "Chrome Symbol", CHARSET_SYMBOL
                return self.use_internal_subst(SYMBOL, pitch_family, old_weight, italic_angle, subst)
            return self.find_subst_face(family, truetype, flags & ~STYLE_SYMBOLIC, weight, italic_angle, subst)
        # charset is ANSI here: the face_array search for other charsets is CJK only
        return self.use_internal_subst(base_font, pitch_family, old_weight, italic_angle, subst)


def _ps_name_from_tt(info: FontInfo[H], hfont: H) -> str:
    size, _ = info.get_font_data(hfont, TABLE_NAME, None)
    if not size:
        return ""
    n, data = info.get_font_data(hfont, TABLE_NAME, size)
    return _name_from_tt(data, 6) if n == size else ""


def _face_style(prog: Program) -> tuple[bool, bool]:
    """CFX_Face::IsBold / IsItalic of a system face: FreeType's style flags (sfnt_load_face) - OS/2's
    fsSelection when the face has outlines and an OS/2 table (bit 9 or bit 0 italic, bit 5 bold),
    else head's macStyle."""
    face = prog.sfnt_face
    if face is None:
        return False, False
    if face.has_outline and face.os2 is not None:
        sel = face.os2["fsSelection"]
        return bool(sel & 32), bool(sel & 512 or sel & 1)
    head = face.table(b"head")
    mac = int.from_bytes(face.data[head[0] + 44:head[0] + 46], "big") if head else 0
    return bool(mac & 1), bool(mac & 2)


def _ttc_index(data: bytes, offset: int) -> int:
    """GetTTCIndex."""
    count = int.from_bytes(data[8:12], "big")
    for i in range(count):
        if int.from_bytes(data[12 + 4 * i:16 + 4 * i], "big") == offset:
            return i
    return 0


# The process's mapper, over GDI's handles or the folder scan's
AnyMapper: TypeAlias = "FontMapper[int] | FontMapper[FolderFace]"
_mapper: AnyMapper | None = None


def active() -> bool:
    """Whether substitution follows PDFium's mapper (else fonts.py's older rules)."""
    return foxit.available()


def platform_font_info() -> Win32FontInfo | FolderFontInfo:
    """The font info PDFium's platform creates (CreateDefaultSystemFontInfo, no user font paths)."""
    if sys.platform == "win32":
        return Win32FontInfo()
    if sys.platform == "darwin":
        return MacFontInfo(MacFontInfo.PATHS)
    return LinuxFontInfo(LinuxFontInfo.PATHS)


def mapper() -> AnyMapper:
    global _mapper
    if _mapper is None:
        info = platform_font_info()
        _mapper = FontMapper[int](info) if isinstance(info, Win32FontInfo) else FontMapper[FolderFace](info)
    return _mapper


def document_closed(doc: PdfFile) -> None:
    """A document was closed: the system faces it was the last to hold go with it."""
    if _mapper is not None:
        _mapper.release(doc)


def load_subst_face(name: str, truetype: bool, flags: int, weight: int, italic_angle: int,
                    doc: PdfFile | None) -> _Face | None:
    """CFX_Font::LoadSubstFace for a simple font (code page kDefANSI, horizontal). `doc` is the
    document whose font will hold the face (see the module docstring)."""
    m = mapper()
    face = m.find_subst_face(name, truetype, flags, weight, italic_angle, SubstFont())
    m.hold(face, doc)
    return face
