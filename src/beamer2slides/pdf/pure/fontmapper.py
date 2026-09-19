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

Only on Windows, and only with the Foxit faces in their cache (`foxit.available()`): anywhere else
`active()` is False and `fonts.py` keeps its older, narrower substitution.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from . import foxit

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

WEIGHT_EXTRA_LIGHT, WEIGHT_NORMAL, WEIGHT_BOLD, WEIGHT_EXTRA_BOLD = 100, 400, 700, 800

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


def _get_style_type(name: str, reverse: bool):
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


def _parse_styles(style_str: str, state: dict) -> bool:
    """ParseStyles: updates state['available'], ['weight'], ['style']; True = give the family up."""
    if not style_str:
        return False
    i = 0
    first_item = True
    while i < len(style_str):
        buf = _parse_style(style_str, i)
        result = _get_style_type(buf, False)
        if (i and not state["available"]) or (not i and not result):
            return True
        if result:
            state["available"] = True
            parsed = result[1]
        else:
            parsed = STYLE_NORMAL
        if parsed & STYLE_FORCE_BOLD:
            if state["style"] & STYLE_FORCE_BOLD:
                state["weight"] = WEIGHT_EXTRA_BOLD
            else:
                state["weight"] = WEIGHT_BOLD
                state["style"] |= STYLE_FORCE_BOLD
            first_item = False
        if parsed & STYLE_ITALIC and parsed & STYLE_FORCE_BOLD:
            state["style"] |= STYLE_ITALIC
        elif parsed & STYLE_ITALIC:
            if not first_item:
                return True
            state["style"] |= STYLE_ITALIC
            break
        i += len(buf) + 1
    return False


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


class Win32FontInfo:
    """CFX_Win32FontInfo: GDI through ctypes. Font handles are HFONTs."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
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
        self.ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.POINTER(LOGFONTA), ctypes.c_void_p,
                                           wintypes.DWORD, wintypes.LPARAM)
        gdi.EnumFontFamiliesExA.argtypes = [HDC, ctypes.POINTER(LOGFONTA), self.ENUMPROC, wintypes.LPARAM,
                                            wintypes.DWORD]
        gdi.EnumFontFamiliesExA.restype = ctypes.c_int
        gdi.CreateFontIndirectA.argtypes, gdi.CreateFontIndirectA.restype = [ctypes.POINTER(LOGFONTA)], HFONT
        self.gdi = gdi
        self.dc = gdi.CreateCompatibleDC(None)

    # Win32CreateFont
    def _create(self, weight: int, italic: bool, charset: int, pitch_family: int, face: str):
        return self.gdi.CreateFontA(-10, 0, 0, 0, weight, int(bool(italic)), 0, 0, charset & 0xFF,
                                    OUT_TT_ONLY_PRECIS, 0, 0, pitch_family & 0xFF,
                                    face.encode("latin-1", "replace"))

    def _select(self, hfont):
        return self.gdi.SelectObject(self.dc, hfont)

    def get_font_data(self, hfont, table: int, size: int | None) -> tuple[int, bytes]:
        """GetFontData: (the size GDI answers, 0 on GDI_ERROR; the bytes read when `size` is given)."""
        old = self._select(hfont)
        try:
            if size is None:
                n = self.gdi.GetFontData(self.dc, table, 0, None, 0)
                return (0 if n == GDI_ERROR else n), b""
            buf = self.ct.create_string_buffer(max(size, 1))
            n = self.gdi.GetFontData(self.dc, table, 0, buf, size)
            n = 0 if n == GDI_ERROR else n
            return n, buf.raw[:min(n, size)]
        finally:
            self._select(old)

    def get_face_name(self, hfont) -> str | None:
        old = self._select(hfont)
        try:
            buf = self.ct.create_string_buffer(100)
            if self.gdi.GetTextFaceA(self.dc, 100, buf) == 0:
                return None
            return buf.value.decode("latin-1")
        finally:
            self._select(old)

    def delete_font(self, hfont) -> None:
        self.gdi.DeleteObject(hfont)

    def get_font(self, face: str):
        return None

    def _is_supported_font(self, plf) -> bool:
        hfont = self.gdi.CreateFontIndirectA(plf)
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

    def enum_font_list(self, mapper: "FontMapper") -> None:
        """EnumFontList + FontEnumCallback + AddInstalledFont."""
        last = [""]

        def callback(plf, _tm, font_type, _lparam):
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
        self.gdi.EnumFontFamiliesExA(self.dc, self.ct.byref(lf), proc, 0, 0)

    def map_font(self, weight: int, italic: bool, charset: int, pitch_family: int, face: str):
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


# ---------------------------------------------------------------------- CFX_FontMapper


@dataclass
class _Face:
    """What FindSubstFace hands back: the face (a fonts.Program) and whether it is PDFium's
    generic multiple master face (CFX_SubstFont::IsBuiltInGenericFont)."""
    program: object
    generic: bool = False


@dataclass
class FontMapper:
    font_info: Win32FontInfo | None
    list_loaded: bool = False
    installed: list = field(default_factory=list)
    localized: list = field(default_factory=list)      # (PS name, family)
    face_array: list = field(default_factory=list)     # (name, charset)
    last_family: str = ""
    standard_faces: dict = field(default_factory=dict)
    generic: dict = field(default_factory=dict)
    face_map: dict = field(default_factory=dict)       # (subst name, weight, italic) -> program
    ttc_face_map: dict = field(default_factory=dict)   # (ttc size, checksum) -> (data, {index: program})

    def add_installed_font(self, name: str, charset: int) -> None:
        if self.font_info is None:
            return
        self.face_array.append((name, charset))
        if name == self.last_family:
            return
        if any(ord(c) > 0x80 for c in name):
            hfont = self.font_info.get_font(name)
            if not hfont:
                hfont = self.font_info.map_font(0, False, CHARSET_DEFAULT, 0, name)
                if not hfont:
                    return
            try:
                new_name = self._ps_name_from_tt(hfont)
            finally:
                self.font_info.delete_font(hfont)
            if new_name:
                self.localized.append((new_name, name))
        self.installed.append(name)
        self.last_family = name

    def _ps_name_from_tt(self, hfont) -> str:
        size, _ = self.font_info.get_font_data(hfont, TABLE_NAME, None)
        if not size:
            return ""
        n, data = self.font_info.get_font_data(hfont, TABLE_NAME, size)
        return _name_from_tt(data, 6) if n == size else ""

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

    def use_internal_subst(self, base_font: int | None, pitch_family: int) -> _Face | None:
        from .fonts import load_cff, load_generic
        if base_font is not None:
            if base_font not in self.standard_faces:
                data = foxit.face_data(foxit.STANDARD[base_font])
                prog = None
                if data is not None:
                    try:
                        prog = load_cff(data)
                    except Exception:  # noqa: BLE001
                        prog = None
                self.standard_faces[base_font] = prog
            prog = self.standard_faces[base_font]
            return _Face(prog) if prog is not None else None
        name = foxit.GENERIC_SERIF if pitch_family & PITCH_ROMAN else foxit.GENERIC_SANS
        if name not in self.generic:
            data = foxit.face_data(name)
            self.generic[name] = load_generic(data) if data is not None else None
        prog = self.generic[name]
        return _Face(prog, generic=True) if prog is not None else None

    def use_external_subst(self, hfont, face_name: str, weight: int, italic: bool) -> _Face | None:
        from .fonts import load_truetype
        info = self.font_info
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
                key = (ttc_size, checksum)
                if key not in self.ttc_face_map:
                    n, data = info.get_font_data(hfont, TABLE_TTCF, ttc_size)
                    if n != ttc_size:
                        return None
                    self.ttc_face_map[key] = (data, {})
                data, faces = self.ttc_face_map[key]
                index = _ttc_index(data, ttc_size - font_size)
                if index not in faces:
                    try:
                        faces[index] = load_truetype(data, index)
                    except Exception:  # noqa: BLE001 - a face FreeType would not open either
                        faces[index] = None
                prog = faces[index]
            else:
                key = (face_name, weight, bool(italic))
                if key not in self.face_map:
                    n, data = info.get_font_data(hfont, TABLE_NONE, font_size)
                    if n != font_size:
                        return None
                    try:
                        self.face_map[key] = load_truetype(data)
                    except Exception:  # noqa: BLE001
                        self.face_map[key] = None
                prog = self.face_map[key]
        finally:
            info.delete_font(hfont)
        return _Face(prog) if prog is not None else None

    def find_subst_face(self, name: str, truetype: bool, flags: int, weight: int, italic_angle: int) -> _Face | None:
        """CFX_FontMapper::FindSubstFace for code page kDefANSI (simple fonts)."""
        from .fonts import standard_font_index
        if weight == 0:
            weight = WEIGHT_NORMAL
        if not flags & USE_EXTERN_ATTR:
            weight = WEIGHT_NORMAL
            italic_angle = 0
        subst_name = get_subst_name(name, truetype)
        if subst_name == "Symbol" and not truetype:
            return self.use_internal_subst(SYMBOL, 0)
        if subst_name == "ZapfDingbats":
            return self.use_internal_subst(DINGBATS, 0)
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
        state = {"available": False, "weight": weight, "style": n_style}
        if _parse_styles(style, state):
            family = subst_name
            base_font = None
        weight, n_style, style_available = state["weight"], state["style"], state["available"]
        if self.font_info is None:
            return self.use_internal_subst(base_font, pitch_family)
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
                    family = NARROW_FAMILY
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
        hfont = self.font_info.map_font(weight, is_italic, charset, pitch_family, family)
        if hfont:
            return self.use_external_subst(hfont, subst_name, weight, is_italic)
        if match:
            hfont = self.font_info.get_font(match)
            if not hfont:
                return self.use_internal_subst(base_font, pitch_family)
            return self.use_external_subst(hfont, subst_name, weight, is_italic)
        if charset == CHARSET_SYMBOL:
            return self.find_subst_face(family, truetype, flags & ~STYLE_SYMBOLIC, weight, italic_angle)
        # charset is ANSI here: the face_array search for other charsets is CJK only
        return self.use_internal_subst(base_font, pitch_family)


def _ttc_index(data: bytes, offset: int) -> int:
    """GetTTCIndex."""
    count = int.from_bytes(data[8:12], "big")
    for i in range(count):
        if int.from_bytes(data[12 + 4 * i:16 + 4 * i], "big") == offset:
            return i
    return 0


_mapper: FontMapper | None = None


def active() -> bool:
    """Whether substitution follows PDFium's Windows mapper (else fonts.py's older rules)."""
    return sys.platform == "win32" and foxit.available()


def mapper() -> FontMapper:
    global _mapper
    if _mapper is None:
        _mapper = FontMapper(Win32FontInfo())
    return _mapper


def load_subst_face(name: str, truetype: bool, flags: int, weight: int, italic_angle: int) -> _Face | None:
    """CFX_Font::LoadSubstFace for a simple font (code page kDefANSI, horizontal)."""
    return mapper().find_subst_face(name, truetype, flags, weight, italic_angle)
