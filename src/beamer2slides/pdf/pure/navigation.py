"""Document navigation as PDFium reads it: link annotations and their actions, destinations, the
/Dests name tree and dictionary, page labels and the document information dictionary.

A port of what pdfium_backend calls (fpdf_doc.cpp: FPDFLink_Enumerate, FPDFLink_GetAnnotRect,
FPDFLink_GetDest, FPDFLink_GetAction, FPDFAction_GetType, FPDFAction_GetDest, FPDFAction_GetURIPath,
FPDFDest_GetDestPageIndex, FPDF_GetMetaText, FPDF_GetPageLabel; fpdf_view.cpp: FPDF_CountNamedDests,
FPDF_GetNamedDest) and of what those read: CPDF_Link, CPDF_Action, CPDF_Dest, CPDF_NameTree,
CPDF_NumberTree, CPDF_PageLabel, and the object accessors of CPDF_Dictionary, CPDF_Array and
CPDF_Reference. Those accessors are the point: each one resolves exactly as far as PDFium's does.

- `GetDirectObjectFor` / `GetDirectObjectAt` follow one reference, and give that object even when it
  is a reference itself; `GetDictFor` goes one step further through `FastGetDirect`, which gives
  nothing for a reference to a reference. A stream counts as its dictionary for `GetDictFor` /
  `GetDictAt`, never for `ToDictionary`.
- `GetNameFor` reads a direct name only; `GetByteStringFor`, `GetIntegerFor` and `GetFloatAt` read the
  raw value, where a reference is followed once (`FastGetDirect`); `GetUnicodeTextAt` does not follow
  a reference at all, `GetUnicodeTextFor` follows one (and a stream's text is its decoded data).
- `GetString` of a number is its text: an integer as int32, a real through SkFloatToDecimal; a
  boolean is "true"/"false". `GetInteger` wraps an unsigned integer into int32 and saturates a real.
- A missing or unreadable object is nothing (`NOTHING`, nullptr), a `null` object is None
  (CPDF_Null), and they differ: a null value in a name tree ends a search.

Text is PDFium's WideString on Windows, where wchar_t holds one UTF-16 unit: a str of code units
(lone surrogates kept), compared unit by unit (`WideString::Compare`). Where wchar_t is 32 bits
the comparison sees code points instead (`wide`); the API gives UTF-16 back either way.
"""

from __future__ import annotations

import enum
import math
import struct
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

from .syntax import Name, PdfArray, PdfDict, PdfObject, Ref, Stream, String

if TYPE_CHECKING:
    from .document import PdfFile


class Nothing(enum.Enum):
    """nullptr: no object at all (a missing, unreadable or cyclic indirect object)."""
    NOTHING = "nothing"


NOTHING = Nothing.NOTHING
Found = Union[PdfObject, Nothing]   # what an accessor that may find nothing gives

_INT_MIN, _INT_MAX = -2 ** 31, 2 ** 31 - 1
_FLT_MAX = 3.4028234663852886e38
_MAX_NAME_TREE_LEVEL = 32        # kNameTreeMaxRecursion
_MAX_NUMBER_TREE_LEVEL = 256     # PDFium has none (a cyclic tree overflows its stack)
_INDEX_SEARCH_BUDGET = 200_000   # PDFium has none (a node listing itself twice takes 2^32 steps)


def _f32(v: float) -> float:
    try:
        return struct.unpack("f", struct.pack("f", v))[0]
    except OverflowError:
        return math.copysign(math.inf, v)


def _wrap32(n: int) -> int:
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n > _INT_MAX else n


def _c_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _c_mod(a: int, b: int) -> int:
    return a - b * _c_div(a, b)


# ---------------------------------------------------------------------- text

# kPDFDocEncoding: Latin-1 except 0x18-0x1F, 0x7F-0xA0 and 0xAD
_PDFDOC = list(range(256))
_PDFDOC[0x18:0x20] = [0x02D8, 0x02C7, 0x02C6, 0x02D9, 0x02DD, 0x02DB, 0x02DA, 0x02DC]
_PDFDOC[0x7F:0xA1] = [0x0000, 0x2022, 0x2020, 0x2021, 0x2026, 0x2014, 0x2013, 0x0192, 0x2044, 0x2039,
                      0x203A, 0x2212, 0x2030, 0x201E, 0x201C, 0x201D, 0x2018, 0x2019, 0x201A, 0x2122,
                      0xFB01, 0xFB02, 0x0141, 0x0152, 0x0160, 0x0178, 0x017D, 0x0131, 0x0142, 0x0153,
                      0x0161, 0x017E, 0x0000, 0x20AC]
_PDFDOC[0xAD] = 0x0000
_PDFDOC_TABLE = "".join(map(chr, _PDFDOC))


def _strip_language_codes(units: str) -> str:
    """StripLanguageCodes: ESC ... ESC regions go, an unterminated one to the end."""
    out: list[str] = []
    i, n = 0, len(units)
    while i < n:
        if units[i] == "\x1b":
            i += 1
            while i < n and units[i] != "\x1b":
                i += 1
            i += 1
            continue
        out.append(units[i])
        i += 1
    return "".join(out)


def _utf8_decode(data: bytes) -> str:
    """UTF8Decode with 16-bit wchar_t: broken sequences dropped, overlong ones and encoded
    surrogates accepted, code points past U+10FFFF dropped."""
    out: list[str] = []
    remaining, code = 0, 0
    for b in data:
        if b < 0x80:
            remaining = 0
            out.append(chr(b))
        elif b < 0xC0:
            if remaining > 0:
                remaining -= 1
                code = (code << 6) | (b & 0x3F)
                if remaining == 0 and code <= 0x10FFFF:
                    if code < 0x10000:
                        out.append(chr(code))
                    else:
                        code -= 0x10000
                        out.append(chr(0xD800 + (code >> 10)) + chr(0xDC00 + (code & 0x3FF)))
        elif b < 0xE0:
            remaining, code = 1, b & 0x1F
        elif b < 0xF0:
            remaining, code = 2, b & 0x0F
        elif b < 0xF8:
            remaining, code = 3, b & 0x07
        else:
            remaining = 0
    return "".join(out)


def pdf_decode_text(data: bytes) -> str:
    """PDF_DecodeText: UTF-16 with either byte order mark, UTF-8 with its mark, else
    PDFDocEncoding; NULs are kept. A str of UTF-16 code units."""
    if len(data) >= 2 and data[:2] in (b"\xfe\xff", b"\xff\xfe"):
        body = data[2:]
        body = body[:len(body) & ~1]
        units = body.decode("utf-16-be" if data[0] == 0xFE else "utf-16-le", "surrogatepass")
        return _strip_language_codes(units_of(units))
    if data[:3] == b"\xef\xbb\xbf":
        return _strip_language_codes(_utf8_decode(data[3:]))
    return data.decode("latin-1").translate(_PDFDOC_TABLE)


def units_of(s: str) -> str:
    """A str with astral characters split into UTF-16 surrogate pairs."""
    if all(ord(c) < 0x10000 for c in s):
        return s
    return "".join(c if ord(c) < 0x10000 else
                   chr(0xD800 + ((ord(c) - 0x10000) >> 10)) + chr(0xDC00 + ((ord(c) - 0x10000) & 0x3FF))
                   for c in s)


def utf16le(units: str) -> bytes:
    """WideString::ToUTF16LE without the terminator: the code units as they are."""
    return units_of(units).encode("utf-16-le", "surrogatepass")


def text(units: str) -> str:
    """What the reference backend makes of a UTF-16LE buffer: decoded, errors replaced."""
    return utf16le(units).decode("utf-16-le", "replace")


# ---------------------------------------------------------------------- numbers as text


def _pow10(e: int) -> float:
    if 0 <= e <= 15:
        return float(10 ** e)
    if e > 15:
        value = 1e15
        while e > 15:
            value *= 10.0
            e -= 1
        return value
    value = 1.0
    while e < 0:
        value /= 10.0
        e += 1
    return value


def sk_float_to_decimal(value: float) -> bytes:
    """SkFloatToDecimal (cpdf_contentstream_write_utils.cpp): no exponent, 9 significant digits
    at most, at most 48 characters."""
    value = _f32(value)
    if value == math.inf:
        value = _FLT_MAX
    elif value == -math.inf:
        value = -_FLT_MAX
    if not math.isfinite(value) or value == 0.0:
        return b"0"
    out = ""
    if value < 0.0:
        out = "-"
        value = -value
    _, binary_exponent = math.frexp(value)
    decimal_exponent = math.floor(0.3010299956639812 * binary_exponent)
    shift = decimal_exponent - 8
    power = _pow10(-shift)
    d = int(value * power + 0.5)
    if d > 167772159:
        shift = decimal_exponent - 7
        d = int(value * (power * 0.1) + 0.5)
    while d % 10 == 0:
        d //= 10
        shift += 1
    digits = str(d)
    if shift >= 0:
        return (out + digits + "0" * shift).encode("ascii")
    before = len(digits) + shift
    if before > 0:
        out += digits[:before] + "."
        rest = digits[before:]
    else:
        out += "." + "0" * -before
        rest = digits
    for ch in rest:
        out += ch
        if len(out) == 48:
            break
    return out.encode("ascii")


# ---------------------------------------------------------------------- object accessors


def direct(pdf: PdfFile, value: PdfObject) -> Found:
    """GetDirect: a reference's object (possibly a reference itself), else the value."""
    if isinstance(value, Ref):
        return pdf.indirect(value.num)
    return value


def fast_direct(pdf: PdfFile, ref: Ref) -> Found:
    """CPDF_Reference::FastGetDirect: nothing when the object is a reference too."""
    obj = pdf.indirect(ref.num)
    return NOTHING if isinstance(obj, Ref) else obj


def dict_of(pdf: PdfFile, value: PdfObject) -> PdfDict | None:
    """GetDictInternal: a dictionary, a stream's dictionary, through one FastGetDirect."""
    obj = fast_direct(pdf, value) if isinstance(value, Ref) else value
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, Stream):
        return obj.dict
    return None


def is_number(value: Found) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def get_string(pdf: PdfFile, value: PdfObject) -> bytes:
    """CPDF_Object::GetString by type."""
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, Name):
        return str.__str__(value).encode("latin-1", "replace")
    if isinstance(value, String):
        return bytes(value)
    if isinstance(value, int):
        return str(_wrap32(value)).encode("ascii")
    if isinstance(value, float):
        return sk_float_to_decimal(value)
    if isinstance(value, Ref):
        obj = fast_direct(pdf, value)
        return b"" if obj is NOTHING else get_string(pdf, obj)
    return b""


def get_integer(pdf: PdfFile, value: PdfObject) -> int:
    """CPDF_Object::GetInteger: FX_Number::GetSigned (uint32 wrapped, reals saturated), booleans."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return _wrap32(value)
    if isinstance(value, float):
        if math.isnan(value):
            return 0
        if value >= 2 ** 31:
            return _INT_MAX
        if value <= -2 ** 31:
            return _INT_MIN
        return int(value)
    if isinstance(value, Ref):
        obj = fast_direct(pdf, value)
        return 0 if obj is NOTHING else get_integer(pdf, obj)
    return 0


def get_number(pdf: PdfFile, value: PdfObject) -> float:
    """CPDF_Object::GetNumber: a number as float32 (a boolean is 0)."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return _f32(float(value))
    if isinstance(value, Ref):
        obj = fast_direct(pdf, value)
        return 0.0 if obj is NOTHING else get_number(pdf, obj)
    return 0.0


def unicode_text(pdf: PdfFile, value: PdfObject) -> str:
    """CPDF_Object::GetUnicodeText: strings and names decoded, a stream's data decoded; a
    reference is not followed."""
    if isinstance(value, Name):
        return pdf_decode_text(str.__str__(value).encode("latin-1", "replace"))
    if isinstance(value, String):
        return pdf_decode_text(bytes(value))
    if isinstance(value, Stream):
        return pdf_decode_text(pdf.stream_data(value))
    return ""


# CPDF_Dictionary


def direct_for(pdf: PdfFile, d: PdfDict, key: str) -> Found:
    return direct(pdf, d[key]) if key in d else NOTHING


def dict_for(pdf: PdfFile, d: PdfDict | None, key: str) -> PdfDict | None:
    """GetDictFor."""
    if d is None or key not in d:
        return None
    obj = direct(pdf, d[key])
    return None if obj is NOTHING else dict_of(pdf, obj)


def array_for(pdf: PdfFile, d: PdfDict | None, key: str) -> PdfArray | None:
    """GetArrayFor: ToArray(GetDirectObjectFor)."""
    if d is None or key not in d:
        return None
    obj = direct(pdf, d[key])
    return obj if isinstance(obj, list) else None


def name_for(d: PdfDict, key: str) -> bytes:
    """GetNameFor: a direct name only."""
    v = d.get(key)
    return str.__str__(v).encode("latin-1", "replace") if isinstance(v, Name) else b""


def byte_string_for(pdf: PdfFile, d: PdfDict, key: str) -> bytes:
    """GetByteStringFor: empty when the key is missing."""
    return get_string(pdf, d[key]) if key in d else b""


def integer_for(pdf: PdfFile, d: PdfDict, key: str) -> int:
    """GetIntegerFor: 0 when the key is missing."""
    return integer_or(pdf, d, key, 0)


def integer_or(pdf: PdfFile, d: PdfDict, key: str, missing: int) -> int:
    """GetIntegerFor with a default: `missing` when the key is missing."""
    return get_integer(pdf, d[key]) if key in d else missing


def unicode_text_for(pdf: PdfFile, d: PdfDict, key: str) -> str:
    """GetUnicodeTextFor: a reference is followed once (to whatever it holds)."""
    if key not in d:
        return ""
    v = d[key]
    found = pdf.indirect(v.num) if isinstance(v, Ref) else v
    return "" if found is NOTHING else unicode_text(pdf, found)


def rect_for(pdf: PdfFile, d: PdfDict, key: str) -> tuple[float, float, float, float]:
    """GetRectFor: (left, bottom, right, top) from exactly four numbers, else zeros; as written."""
    a = array_for(pdf, d, key)
    if a is None or len(a) != 4:
        return (0.0, 0.0, 0.0, 0.0)
    return get_number(pdf, a[0]), get_number(pdf, a[1]), get_number(pdf, a[2]), get_number(pdf, a[3])


# CPDF_Array


def direct_at(pdf: PdfFile, a: PdfArray, i: int) -> Found:
    return direct(pdf, a[i]) if 0 <= i < len(a) else NOTHING


def dict_at(pdf: PdfFile, a: PdfArray, i: int) -> PdfDict | None:
    """GetDictAt: a dictionary or a stream's, after GetDirectObjectAt (a reference to a reference
    is nothing)."""
    obj = direct_at(pdf, a, i)
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, Stream):
        return obj.dict
    return None


def integer_at(pdf: PdfFile, a: PdfArray, i: int) -> int:
    return get_integer(pdf, a[i]) if 0 <= i < len(a) else 0


def unicode_text_at(pdf: PdfFile, a: PdfArray, i: int) -> str:
    return unicode_text(pdf, a[i]) if 0 <= i < len(a) else ""


# ---------------------------------------------------------------------- name trees


def _traversed(pdf: PdfFile, obj: PdfObject, seen: set[int]) -> bool:
    """IsTraversedObject: an indirect object met before (direct ones have no number)."""
    num = pdf.objnum(obj)
    if not num:
        return False
    if num in seen:
        return True
    seen.add(num)
    return False


def _traversed_array(pdf: PdfFile, a: PdfArray, seen: set[int]) -> bool:
    """IsArrayWithTraversedObject: the array's items are never indirect objects of their own."""
    return _traversed(pdf, a, seen) or any(_traversed(pdf, item, seen) for item in a
                                           if isinstance(item, (dict, list, Stream)))


WCHAR_32 = sys.platform != "win32"


def wide(units: str) -> str:
    """What a WideString holds (and Compare compares): the UTF-16 units where wchar_t is 16 bits
    (Windows), code points elsewhere, where FromUTF16LE/BE fuse each surrogate pair (lone ones
    are kept)."""
    if not WCHAR_32 or not any("\ud800" <= c <= "\udbff" for c in units):
        return units
    out: list[str] = []
    i = 0
    while i < len(units):
        c = units[i]
        if "\ud800" <= c <= "\udbff" and i + 1 < len(units) and "\udc00" <= units[i + 1] <= "\udfff":
            out.append(chr(0x10000 + ((ord(c) - 0xD800) << 10) + (ord(units[i + 1]) - 0xDC00)))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _node_limits(pdf: PdfFile, limits: PdfArray) -> tuple[str, str]:
    """GetNodeLimits: pads /Limits to two empty strings and puts its ends in order, in place."""
    while len(limits) < 2:
        limits.append(String(b""))
    obj0, obj1 = limits[0], limits[1]
    left, right = wide(unicode_text(pdf, obj0)), wide(unicode_text(pdf, obj1))
    if left > right:
        left, right = right, left
        limits[0], limits[1] = obj1, obj0
    return left, right


def _search_by_name(pdf: PdfFile, node: PdfDict, name: str, level: int, seen: set[int]) -> Found:
    """SearchNameNodeByNameInternal, looking up only."""
    if level > _MAX_NAME_TREE_LEVEL:
        return NOTHING
    limits = array_for(pdf, node, "Limits")
    names = array_for(pdf, node, "Names")
    if names is not None and _traversed_array(pdf, names, seen):
        names = None
    if limits is not None and _traversed_array(pdf, limits, seen):
        limits = None
    name = wide(name)
    if limits is not None:
        left, right = _node_limits(pdf, limits)
        if name < left or name > right:
            return NOTHING
    if names is not None:
        for i in range(len(names) // 2):
            value = wide(unicode_text_at(pdf, names, 2 * i))
            if value > name:
                break
            if value < name:
                continue
            return direct_at(pdf, names, 2 * i + 1)
        return NOTHING
    kids = array_for(pdf, node, "Kids")
    if kids is None or _traversed(pdf, kids, seen):
        return NOTHING
    i = 0
    while i < len(kids):
        kid = dict_at(pdf, kids, i)
        i += 1
        if kid is None or _traversed(pdf, kid, seen):
            continue
        found = _search_by_name(pdf, kid, name, level + 1, seen)
        if found is not NOTHING:
            return found
    return NOTHING


def _count_names(pdf: PdfFile, node: PdfDict, level: int, seen: dict[int, PdfDict]) -> int:
    """CountNamesInternal: a node counted once; a /Names array ends the node."""
    if level > _MAX_NAME_TREE_LEVEL or id(node) in seen:
        return 0
    seen[id(node)] = node
    names = array_for(pdf, node, "Names")
    if names is not None:
        return len(names) // 2
    kids = array_for(pdf, node, "Kids")
    if kids is None:
        return 0
    count = 0
    for i in range(len(kids)):
        kid = dict_at(pdf, kids, i)
        if kid is not None:
            count += _count_names(pdf, kid, level + 1, seen)
    return count


@dataclass(slots=True, kw_only=True)
class _IndexSearch:
    """Where a search by index stands: the pairs passed, the steps left."""
    passed: int
    steps: int


def _search_by_index(pdf: PdfFile, node: PdfDict, target: int, level: int,
                     state: _IndexSearch) -> tuple[str, PdfObject] | None:
    """SearchNameNodeByIndexInternal: (key, value) of the target-th pair, or None. A pair whose
    value is nothing is no answer, and does not count either (so a later node may give one for
    the same index)."""
    state.steps -= 1
    if level > _MAX_NAME_TREE_LEVEL or state.steps < 0:
        return None
    names = array_for(pdf, node, "Names")
    if names is not None:
        count = len(names) // 2
        if target >= state.passed + count:
            state.passed += count
            return None
        index = 2 * (target - state.passed)
        value = direct_at(pdf, names, index + 1)
        if value is NOTHING:
            return None
        return unicode_text_at(pdf, names, index), value
    kids = array_for(pdf, node, "Kids")
    if kids is None:
        return None
    for i in range(len(kids)):
        kid = dict_at(pdf, kids, i)
        if kid is None:
            continue
        found = _search_by_index(pdf, kid, target, level + 1, state)
        if found is not None:
            return found
    return None


def _dests_tree(pdf: PdfFile) -> PdfDict | None:
    """CPDF_NameTree::Create(doc, "Dests")."""
    return dict_for(pdf, dict_for(pdf, pdf.catalog, "Names"), "Dests")


def _dest_from_object(pdf: PdfFile, obj: Found) -> PdfArray | None:
    """GetNamedDestFromObject: an array, or a dictionary's /D array."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        return array_for(pdf, obj, "D")
    return None


def lookup_named_dest(pdf: PdfFile, name: bytes) -> PdfArray | None:
    """CPDF_NameTree::LookupNamedDest: the name tree (by the name's text), then the catalog's
    /Dests dictionary (by its bytes)."""
    found = None
    tree = _dests_tree(pdf)
    if tree is not None:
        found = _dest_from_object(pdf, _search_by_name(pdf, tree, pdf_decode_text(name), 0, set()))
    if found is None:
        dests = dict_for(pdf, pdf.catalog, "Dests")
        if dests is not None:
            found = _dest_from_object(pdf, direct_for(pdf, dests, name.decode("latin-1")))
    return found


def _key_bytes(item: tuple[str, PdfObject]) -> bytes:
    return str.__str__(item[0]).encode("latin-1", "replace")


def named_dests(pdf: PdfFile) -> list[tuple[str, PdfArray]]:
    """FPDF_CountNamedDests and FPDF_GetNamedDest for every index, as the reference backend asks:
    (name, destination array) for each index that gives a destination."""
    tree = _dests_tree(pdf)
    tree_count = _count_names(pdf, tree, 0, {}) if tree is not None else 0
    old = dict_for(pdf, pdf.catalog, "Dests")
    old_items: list[tuple[str, PdfObject]] = sorted(old.items(), key=_key_bytes) if old is not None else []
    out: list[tuple[str, PdfArray]] = []
    for index in range(tree_count + len(old_items)):
        obj: PdfObject
        if index >= tree_count:
            key, obj = old_items[index - tree_count]   # the raw value: a reference is no destination
            name = pdf_decode_text(str.__str__(key).encode("latin-1", "replace"))
        elif tree is not None:
            found = _search_by_index(pdf, tree, index, 0, _IndexSearch(passed=0, steps=_INDEX_SEARCH_BUDGET))
            if found is None:
                continue
            name, obj = found
        else:   # no tree counts no names
            continue
        dest = array_for(pdf, obj, "D") if isinstance(obj, dict) else obj
        if not isinstance(dest, list):
            continue
        out.append((name, dest))
    return out


# ---------------------------------------------------------------------- destinations, actions, links

_ACTION_TYPES = {b"GoTo", b"GoToR", b"GoToE", b"Launch", b"Thread", b"URI", b"Sound", b"Movie", b"Hide",
                 b"Named", b"SubmitForm", b"ResetForm", b"ImportData", b"JavaScript", b"SetOCGState",
                 b"Rendition", b"Trans", b"GoTo3DView"}


def dest_create(pdf: PdfFile, obj: Found) -> PdfArray | None:
    """CPDF_Dest::Create: a string or a name is looked up, an array is the destination."""
    if isinstance(obj, (String, Name)):
        return lookup_named_dest(pdf, get_string(pdf, obj))
    return obj if isinstance(obj, list) else None


def dest_page_index(pdf: PdfFile, dest: PdfArray | None) -> int:
    """CPDF_Dest::GetDestPageIndex: a number is the page index as written, a dictionary is looked
    up by its object number (0 for a direct one), anything else is -1."""
    if dest is None:
        return -1
    page = direct_at(pdf, dest, 0)
    if page is NOTHING:
        return -1
    if is_number(page):
        return get_integer(pdf, page)
    if not isinstance(page, dict):
        return -1
    return pdf.page_index(pdf.objnum(page))


def action_type(action: PdfDict | None) -> bytes:
    """CPDF_Action::GetType (as its /S name; b"" for an unknown action)."""
    if action is None:
        return b""
    if "Type" in action and name_for(action, "Type") != b"Action":   # ValidateDictOptionalType
        return b""
    kind = name_for(action, "S")
    return kind if kind in _ACTION_TYPES else b""


def action_dest(pdf: PdfFile, action: PdfDict) -> PdfArray | None:
    """CPDF_Action::GetDest."""
    if action_type(action) not in (b"GoTo", b"GoToR", b"GoToE"):
        return None
    return dest_create(pdf, direct_for(pdf, action, "D"))


def action_uri(pdf: PdfFile, action: PdfDict) -> bytes:
    """CPDF_Action::GetURI: /URI, behind the catalog's /URI /Base when it has no scheme."""
    uri = byte_string_for(pdf, action, "URI")
    base_dict = dict_for(pdf, pdf.catalog, "URI")
    if base_dict is not None:
        colon = uri.find(b":")
        if colon <= 0:
            base = direct_for(pdf, base_dict, "Base")
            if isinstance(base, (String, Stream)):
                uri = get_string(pdf, base) + uri
    return uri


def link_dest(pdf: PdfFile, link: PdfDict) -> PdfArray | None:
    """FPDFLink_GetDest: the link's /Dest, else its action's."""
    dest = dest_create(pdf, direct_for(pdf, link, "Dest"))
    if dest is not None:
        return dest
    action = dict_for(pdf, link, "A")
    return None if action is None else action_dest(pdf, action)


Rect = tuple[float, float, float, float]   # (left, bottom, right, top) as written


@dataclass(frozen=True, kw_only=True)
class PageTarget:
    """A link to a page of this document."""
    rect: Rect
    page: int


@dataclass(frozen=True, kw_only=True)
class UriTarget:
    """A link to a URI."""
    rect: Rect
    uri: str


LinkTarget = Union[PageTarget, UriTarget]


def links(pdf: PdfFile, page: PdfDict) -> list[LinkTarget]:
    """The reference backend's Page.links over FPDFLink_Enumerate: a page for a link with a
    destination whose page index is >= 0, a URI for a URI action without a destination."""
    out: list[LinkTarget] = []
    annots = array_for(pdf, page, "Annots")
    if annots is None:
        return out
    i = 0
    while i < len(annots):
        annot = direct_at(pdf, annots, i)
        i += 1
        if not isinstance(annot, dict) or byte_string_for(pdf, annot, "Subtype") != b"Link":
            continue
        rect = rect_for(pdf, annot, "Rect")
        dest = link_dest(pdf, annot)
        action = dict_for(pdf, annot, "A")
        if dest is None and action is not None and action_type(action) == b"GoTo":
            dest = action_dest(pdf, action)
        if dest is not None:
            index = dest_page_index(pdf, dest)
            if index >= 0:
                out.append(PageTarget(rect=rect, page=index))
        elif action is not None and action_type(action) == b"URI":
            uri = action_uri(pdf, action).decode("utf-8", "replace")
            if uri:
                out.append(UriTarget(rect=rect, uri=uri))
    return out


# ---------------------------------------------------------------------- page labels


def _find_number_node(pdf: PdfFile, node: PdfDict, num: int, level: int) -> Found:
    """FindNumberNode."""
    if level > _MAX_NUMBER_TREE_LEVEL:
        return NOTHING
    limits = array_for(pdf, node, "Limits")
    if limits is not None and (num < integer_at(pdf, limits, 0) or num > integer_at(pdf, limits, 1)):
        return NOTHING
    nums = array_for(pdf, node, "Nums")
    if nums is not None:
        for i in range(len(nums) // 2):
            index = integer_at(pdf, nums, 2 * i)
            if num == index:
                return direct_at(pdf, nums, 2 * i + 1)
            if index > num:
                break
        return NOTHING
    kids = array_for(pdf, node, "Kids")
    if kids is None:
        return NOTHING
    for i in range(len(kids)):
        kid = dict_at(pdf, kids, i)
        if kid is None:
            continue
        found = _find_number_node(pdf, kid, num, level + 1)
        if found is not NOTHING:
            return found
    return NOTHING


def _find_lower_bound(pdf: PdfFile, node: PdfDict, num: int, level: int) -> tuple[int, Found] | None:
    """FindLowerBound: (key, value) of the last entry at or below num, or None."""
    if level > _MAX_NUMBER_TREE_LEVEL:
        return None
    limits = array_for(pdf, node, "Limits")
    if limits is not None:
        if num < integer_at(pdf, limits, 0):
            return None
        top = integer_at(pdf, limits, 1)
        if num >= top:
            return top, _find_number_node(pdf, node, top, 0)
    nums = array_for(pdf, node, "Nums")
    if nums is not None:
        for i in range(len(nums) // 2, 0, -1):
            key = integer_at(pdf, nums, 2 * (i - 1))
            if num >= key:
                return key, direct_at(pdf, nums, 2 * (i - 1) + 1)
        return None
    kids = array_for(pdf, node, "Kids")
    if kids is None:
        return None
    for i in range(len(kids), 0, -1):
        kid = dict_at(pdf, kids, i - 1)
        if kid is None:
            continue
        found = _find_lower_bound(pdf, kid, num, level + 1)
        if found is not None:
            return found
    return None


_ROMAN = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
          (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))


def _roman(num: int) -> str:
    num = _c_mod(num, 1000000)
    out: list[str] = []
    for value, letters in _ROMAN:
        while num >= value:
            num -= value
            out.append(letters)
    return "".join(out)


def _letters(num: int) -> str:
    if num == 0:
        return ""
    num = _wrap32(num - 1)
    count = _c_mod(_c_div(num, 26) + 1, 1000)
    if count <= 0:
        return ""     # a negative count is a crash in PDFium
    return chr(ord("a") + _c_mod(num, 26)) * count


def _label_number(num: int, style: bytes) -> str:
    """GetLabelNumPortion."""
    if style == b"D":
        return str(num)
    if style in (b"R", b"r"):
        roman = _roman(num)
        return roman.upper() if style == b"R" else roman
    if style in (b"A", b"a"):
        letters = _letters(num)
        return letters.upper() if style == b"A" else letters
    return ""


def page_label(pdf: PdfFile, index: int) -> str | None:
    """CPDF_PageLabel::GetLabel: None where FPDF_GetPageLabel returns nothing. Text as code units."""
    if not 0 <= index < pdf.page_count:
        return None
    labels = dict_for(pdf, pdf.catalog, "PageLabels")
    if labels is None:
        return None
    bound = _find_lower_bound(pdf, labels, index, 0)
    if bound is None:
        return str(index + 1)
    start, found = bound
    value = NOTHING if found is NOTHING else direct(pdf, found)
    if not isinstance(value, dict):
        return str(index + 1)
    label = unicode_text_for(pdf, value, "P") if "P" in value else ""
    style = byte_string_for(pdf, value, "S")
    number = _wrap32(index - start + integer_or(pdf, value, "St", 1))
    return label + _label_number(number, style)


# ---------------------------------------------------------------------- document information


def info(pdf: PdfFile) -> PdfDict | None:
    """CPDF_Document::GetInfo: the trailer's /Info reference, followed once, to a dictionary."""
    trailer = pdf.trailer
    ref = trailer.get("Info") if trailer is not None else None
    if not isinstance(ref, Ref):
        return None
    obj = pdf.indirect(ref.num)
    return obj if isinstance(obj, dict) else None


def meta_text(pdf: PdfFile, key: str) -> str:
    """FPDF_GetMetaText as the reference backend decodes it."""
    d = info(pdf)
    return "" if d is None else text(unicode_text_for(pdf, d, key))
