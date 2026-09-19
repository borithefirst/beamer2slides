"""The file layer: cross-reference tables and streams, object streams, the page tree, page labels,
named destinations, text strings, and writing a new file (ISO 32000-1, 7.5, 7.7, 12.3, 12.4.2)."""

from __future__ import annotations

import codecs
import contextlib
import re
import sys
from pathlib import Path

from .filters import decode as decode_filters
from .syntax import END, Lexer, Name, Op, PdfSyntaxError, Ref, Stream, String, _literal_string, _name, _number

_STARTXREF = re.compile(rb"startxref[\x00\t\n\x0c\r ]+(\d+)")
_INHERITED = ("Resources", "MediaBox", "CropBox", "Rotate")
_PAGE_MAX = 0xFFFFF        # CPDF_Document::kPageMaxNum
_MAX_PAGE_LEVEL = 1024     # kMaxPageLevel
_MAX_OBJECT_NUMBER = 1048576  # kMaxObjectNumber


class PdfFileError(Exception):
    pass


def _dict(value) -> dict | None:
    """GetDict: a dictionary, or a stream's dictionary."""
    if isinstance(value, Stream):
        return value.dict
    return value if isinstance(value, dict) else None


def _integer(value) -> int:
    """CPDF_Object::GetInteger: numbers (reals truncated) and booleans, else 0."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return max(-2 ** 31, min(2 ** 31 - 1, int(value)))
        except (OverflowError, ValueError):
            return 0
    return 0


_GAP = re.compile(rb"(?:[\x00\t\n\x0c\r ]+|%[^\r\n]*)*")
_REGULAR = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")
_NUMERIC = frozenset(b"0123456789+-.")


class _Words:
    """CPDF_SyntaxParser::GetNextWord over file syntax: (word, is_number, start) or None."""

    def __init__(self, data: bytes, pos: int):
        self.data, self.pos, self.last = data, pos, b""

    def next(self):
        data = self.data
        pos = _GAP.match(data, self.pos).end()
        if pos >= len(data):
            self.pos = pos
            return None
        c = data[pos]
        if c in b"()<>[]{}/":
            if c == 0x2F:  # '/'
                end = _REGULAR.match(data, pos + 1).end()
            elif c in b"<>" and data[pos + 1:pos + 2] == bytes([c]):
                end = pos + 2
            else:
                end = pos + 1
            word, is_number = data[pos:end], False
        else:
            end = _REGULAR.match(data, pos).end()
            word = data[pos:end]
            is_number = all(ch in _NUMERIC for ch in word)
        self.pos = end
        word = word[:256]   # the word buffer holds 256 bytes
        self.last = word    # m_WordBuffer: kept when a position is restored, and at the end
        return word, is_number, end - len(word)


def _atoui(word: bytes) -> int:
    """FXSYS_atoui: an optional sign and decimal digits; past 2^32 - 1 the maximum, a minus 0."""
    i = 1 if word[:1] in (b"-", b"+") else 0
    neg = word[:1] == b"-"
    num = 0
    while i < len(word) and 0x30 <= word[i] <= 0x39:
        num = num * 10 + word[i] - 0x30
        if num > 0xFFFFFFFF:
            return 0 if neg else 0xFFFFFFFF
        i += 1
    return 0 if neg and num else num


_NOTHING = object()   # GetObjectBodyInternal's nullptr
_HEX = frozenset(b"0123456789abcdefABCDEF")


def _hex_body(data: bytes, pos: int) -> tuple[bytes, int]:
    """CPDF_SyntaxParser::ReadHexString: hex digits up to `>` (others skipped)."""
    end = data.find(b">", pos)
    end = len(data) if end < 0 else end
    digits = bytes(c for c in data[pos:end] if c in _HEX)
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode("ascii")), min(end + 1, len(data))


def _body(scan: _Words, strict: bool, stream_at, depth: int = 0):
    """CPDF_SyntaxParser::GetObjectBodyInternal: one object, or _NOTHING (nullptr). Strict only
    at the top: an array must end on `]`, and a dictionary value that is no object ends it.
    `stream_at(dict, pos)` reads the stream after a dictionary: (Stream or _NOTHING, position)."""
    if depth > 64:
        return _NOTHING
    data = scan.data
    saved = scan.pos
    w = scan.next()
    if w is None:
        return _NOTHING
    word, is_number, _ = w
    if is_number:
        after = scan.pos
        w2 = scan.next()
        if w2 is not None and w2[1]:
            w3 = scan.next()
            if w3 is not None and w3[0] == b"R":
                num = _atoui(word)
                return Ref(num, _atoui(w2[0]) & 0xFFFF) if num else _NOTHING
        scan.pos = after
        return _number(word)
    if word in (b"true", b"false"):
        return word == b"true"
    if word == b"null":
        return None
    if word == b"(":
        value, scan.pos = _literal_string(data, scan.pos)
        return String(value)
    if word == b"<":
        value, scan.pos = _hex_body(data, scan.pos)
        s = String(value)
        s.hex = True
        return s
    if word == b"[":
        out = []
        while (item := _body(scan, False, stream_at, depth + 1)) is not _NOTHING:
            out.append(item)
        return out if not strict or scan.last[:1] == b"]" else _NOTHING   # the word that ended it
    if word[:1] == b"/":
        return _name(word)
    if word == b"<<":
        d: dict = {}
        while True:
            w = scan.next()
            if w is None:
                return _NOTHING
            inner = w[0]
            if inner == b">>":
                break
            if inner == b"endobj":
                scan.pos = w[2]
                break
            if inner[:1] != b"/":
                continue
            value = _body(scan, False, stream_at, depth + 1)
            if value is _NOTHING:
                if not strict:
                    continue
                nl = [p for p in (data.find(b"\n", scan.pos), data.find(b"\r", scan.pos)) if p >= 0]
                scan.pos = min(nl) + 1 if nl else len(data)   # ToNextLine
                return _NOTHING
            if len(inner) > 1:
                d[_name(inner)] = value
        after = scan.pos
        w = scan.next()
        if w is None or w[0] != b"stream":
            scan.pos = after
            return d
        stream, scan.pos = stream_at(d, scan.pos)
        return stream
    if word == b">>":
        scan.pos = saved
    return _NOTHING


@contextlib.contextmanager
def _deep_recursion():
    """The page tree may be 1024 levels deep, as deep as PDFium follows it."""
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 4 * _MAX_PAGE_LEVEL + 200))
    try:
        yield
    finally:
        sys.setrecursionlimit(limit)


# PDFDocEncoding differs from Latin-1 in 0x18-0x1F and 0x80-0x9F
_PDFDOC = {0x18: "˘", 0x19: "ˇ", 0x1A: "ˆ", 0x1B: "˙", 0x1C: "˝", 0x1D: "˛",
           0x1E: "˚", 0x1F: "˜", 0x80: "•", 0x81: "†", 0x82: "‡", 0x83: "…",
           0x84: "—", 0x85: "–", 0x86: "ƒ", 0x87: "⁄", 0x88: "‹", 0x89: "›",
           0x8A: "−", 0x8B: "‰", 0x8C: "„", 0x8D: "“", 0x8E: "”", 0x8F: "‘",
           0x90: "’", 0x91: "‚", 0x92: "™", 0x93: "ﬁ", 0x94: "ﬂ", 0x95: "Ł",
           0x96: "Œ", 0x97: "Š", 0x98: "Ÿ", 0x99: "Ž", 0x9A: "ı", 0x9B: "ł",
           0x9C: "œ", 0x9D: "š", 0x9E: "ž", 0xA0: "€"}


def text_string(value) -> str:
    """A PDF text string: UTF-16BE with a BOM, UTF-8 with a BOM, else PDFDocEncoding."""
    if isinstance(value, str):
        return str(value)
    if not isinstance(value, bytes):
        return ""
    if value[:2] == codecs.BOM_UTF16_BE:
        return value[2:].decode("utf-16-be", "replace")
    if value[:2] == codecs.BOM_UTF16_LE:
        return value[2:].decode("utf-16-le", "replace")
    if value[:3] == codecs.BOM_UTF8:
        return value[3:].decode("utf-8", "replace")
    return "".join(_PDFDOC.get(b, chr(b)) for b in value)


class PdfFile:
    def __init__(self, data: bytes):
        if not data.lstrip()[:5].startswith(b"%PDF") and b"%PDF" not in data[:1024]:
            raise PdfFileError("not a PDF file")
        self.data = data
        self.offsets: dict[int, tuple] = {}    # num -> ("f", offset, gen) or ("s", objstm, index)
        self.trailer: dict = {}
        self._cache: dict[int, object] = {}
        self._objstm: dict[int, tuple[list, bytes, int]] = {}
        self._page_list: list | None = None
        self._parsing: set[int] = set()
        loaded = True
        try:
            self._read_xref()
            if not self._verify_xref():
                raise PdfFileError("the table's first object is not where it says")
        except Exception:  # noqa: BLE001 - a broken table: rebuild it from the objects themselves
            self.offsets, self.trailer, loaded = {}, {}, False
        if not loaded or not self._root_ok():
            # CPDF_Parser::StartParseInternal: the table rebuilt onto what was read, then a
            # catalog is all it takes (a document with no pages opens)
            self._reconstruct(dict(self.trailer))
            self._page_list = None
            if not self.catalog:
                raise PdfFileError("no document catalog")
        if self.trailer.get("Encrypt") is not None:
            raise PdfFileError("encrypted PDFs are not supported")
        self.page_count  # noqa: B018 - counted now, as CPDF_Document::LoadPages does

    # ------------------------------------------------------------------ cross-reference

    def _read_xref(self) -> None:
        tail = self.data[-4096:] if len(self.data) > 4096 else self.data
        found = list(_STARTXREF.finditer(tail))
        if not found:
            raise PdfFileError("no startxref")
        pos = int(found[-1].group(1))
        seen = set()
        while pos is not None and pos not in seen and 0 <= pos < len(self.data):
            seen.add(pos)
            lexer = Lexer(self.data, pos)
            first = lexer.next()
            if first == "xref" and type(first) is Op:
                trailer = self._xref_table(lexer)
            else:
                trailer = self._xref_stream(pos)
            for k, v in trailer.items():
                self.trailer.setdefault(k, v)
            if isinstance(trailer.get("XRefStm"), int):
                try:
                    self._xref_stream(trailer["XRefStm"])
                except Exception:  # noqa: BLE001
                    pass
            prev = trailer.get("Prev")
            pos = prev if isinstance(prev, int) else None

    def _xref_table(self, lexer: Lexer) -> dict:
        data = self.data
        while True:
            save = lexer.pos
            t = lexer.next()
            if t == "trailer" and type(t) is Op:
                with _deep_recursion():
                    trailer = _body(_Words(data, lexer.pos), False, self._held_stream(data))
                if not isinstance(trailer, dict):
                    raise PdfFileError("no trailer dictionary")
                return trailer
            if not isinstance(t, int):
                raise PdfFileError("bad xref table")
            count = lexer.next()
            # entries are 20 bytes each, but be lenient about their line ends
            pos = lexer.pos
            for k in range(count):
                m = re.compile(rb"[\x00\t\n\x0c\r ]*(\d{1,10})[ ]+(\d{1,5})[ ]+([nf])").match(data, pos)
                if not m:
                    raise PdfFileError("bad xref entry")
                pos = m.end()
                num = t + k
                if num not in self.offsets:
                    if m.group(3) == b"n":
                        self.offsets[num] = ("f", int(m.group(1)), int(m.group(2)))
                    else:
                        self.offsets[num] = ("free",)
            lexer.pos = pos
            del save

    def _xref_stream(self, pos: int) -> dict:
        with _deep_recursion():
            stream = self._indirect_at(pos)[0]
        if not isinstance(stream, Stream):
            raise PdfFileError("no xref stream")
        d = stream.dict
        data, _ = decode_filters(stream.raw, d)
        w = d.get("W") or [1, 2, 1]
        size = d.get("Size", 0)
        index = d.get("Index") or [0, size]
        step = sum(w)
        at = 0

        def field(chunk: bytes, default: int) -> int:
            return int.from_bytes(chunk, "big") if chunk else default

        for start, count in zip(index[::2], index[1::2]):
            for k in range(count):
                row = data[at:at + step]
                at += step
                if len(row) < step:
                    break
                a = field(row[:w[0]], 1)
                b = field(row[w[0]:w[0] + w[1]], 0)
                c = field(row[w[0] + w[1]:], 0)
                n = start + k
                if n in self.offsets:
                    continue
                if a == 1:
                    self.offsets[n] = ("f", b, c)
                elif a == 2:
                    self.offsets[n] = ("s", b, c)
                else:
                    self.offsets[n] = ("free",)
        return {k: v for k, v in d.items() if k not in ("Filter", "DecodeParms", "W", "Index", "Length", "Type")}

    def _root_ok(self) -> bool:
        """CPDF_Document::TryInit: a catalog, and at least one page counted."""
        try:
            return bool(self.catalog) and self.page_count > 0
        except Exception:  # noqa: BLE001
            return False

    def _verify_xref(self) -> bool:
        """CPDF_Parser::VerifyCrossRefTable: the first object the table places (lowest number
        with an offset) must start with its own number there."""
        for num in sorted(self.offsets):
            entry = self.offsets[num]
            if entry[0] != "f" or entry[1] <= 0:
                continue
            scan = _Words(self.data, entry[1])
            word = scan.next()
            return word is not None and word[1] and _atoui(word[0]) == num
        return True

    def _reconstruct(self, trailer: dict) -> None:
        """CPDF_Parser::RebuildCrossRef: the file read word by word from the start (strings and
        hex strings skipped whole, an unbalanced `(` swallowing the rest), every `n g obj` read
        as an object and stepped over, stream data included (so objects inside a stream that
        parsed are not seen, and those after a broken header's stream data may be lost); a
        higher generation kept over a later lower one, an object stream's members over
        generation 0; the trailer merged in file order from each `trailer` dictionary and
        cross-reference stream onto `trailer`. The catalog is only what a trailer's /Root
        refers to: PDFium does not go looking for one."""
        self.offsets, self._cache, self._objstm = {}, {}, {}
        data = self.data
        compressed: set[int] = set()
        archives: set[int] = set()
        found = False
        scan = _Words(data, 0)
        numbers: list[tuple[int, int]] = []
        while (w := scan.next()) is not None:
            word, is_number, start = w
            if is_number:
                numbers.append((_atoui(word), start))
                if len(numbers) > 2:
                    numbers.pop(0)
                continue
            if word == b"(":
                scan.pos = _literal_string(data, scan.pos)[1]
            elif word == b"<":
                scan.pos = _hex_body(data, scan.pos)[1]
            elif word == b"trailer":
                t = _body(scan, False, self._rebuild_stream)
                t = t.dict if isinstance(t, Stream) else t
                if isinstance(t, dict):
                    trailer.update(t)
                    found = True
            elif word == b"obj" and len(numbers) == 2:
                (num, pos), (gen, _) = numbers
                gen &= 0xFFFF
                obj, scan.pos = self._object_at(pos)
                if isinstance(obj, Stream) and obj.dict.get("Type") == "XRef":
                    trailer.update(obj.dict)
                    found = True
                if num < _MAX_OBJECT_NUMBER:
                    known = self.offsets.get(num)
                    if not (known is not None and (known[0] == "f" and known[2] > gen
                                                   or num in compressed and gen == 0)):
                        self.offsets[num] = ("f", pos, gen)
                        compressed.discard(num)
                    if isinstance(obj, Stream) and obj.dict.get("Type") == "ObjStm":
                        try:
                            members = self._objstm_from(obj)[0]
                        except Exception:  # noqa: BLE001
                            members = []
                        if members:
                            archives.add(num)
                        for i, n in enumerate(members):
                            info = self.offsets.get(n)
                            if n >= _MAX_OBJECT_NUMBER or n in archives or (
                                    info is not None and info[0] == "f" and info[2] > 0):
                                continue
                            self.offsets[n] = ("s", num, i)
                            compressed.add(n)
            numbers.clear()
        self._cache, self._objstm = {}, {}
        if not found and not trailer or not self.offsets:
            raise PdfFileError("no trailer to rebuild the cross-reference table with")
        self.trailer = trailer

    def _object_at(self, pos: int):
        """CPDF_SyntaxParser::GetIndirectObject as the rebuild calls it (no object holder, so
        an indirect /Length is unknown): the object and where reading stopped."""
        scan = _Words(self.data, pos)
        for _ in range(3):
            scan.next()
        obj = _body(scan, True, self._rebuild_stream)
        return (None if obj is _NOTHING else obj), scan.pos

    def _rebuild_stream(self, d: dict, pos: int):
        """CPDF_SyntaxParser::ReadStream with no object holder: (Stream or _NOTHING, position)."""
        return self._read_stream(self.data, d, pos, False)

    def _held_stream(self, data: bytes):
        """ReadStream as an object read through the parser calls it: an indirect /Length counts."""
        return lambda d, pos: self._read_stream(data, d, pos, True)

    def _read_stream(self, data: bytes, d: dict, pos: int, held: bool):
        """CPDF_SyntaxParser::ReadStream: (Stream or _NOTHING, position). /Length must be a
        number, directly or (`held`) through one reference; an object whose /Length refers to
        an object being parsed gets none (CPDF_Parser's m_ParsingObjNums)."""
        obj = d
        start = pos   # ToNextLine
        while start < len(data) and data[start] not in b"\r\n":
            start += 1
        if data[start:start + 2] == b"\r\n":
            start += 2
        elif start < len(data):
            start += 1
        length = obj.get("Length")
        if isinstance(length, Ref):
            length = self.get(length.num) if held and length.num not in self._parsing else None
        length = _integer(length) if isinstance(length, (int, float)) and not isinstance(length, bool) else -1
        if length > 0 and start + length >= len(data):
            length = -1
        if length >= 0:
            # a zero length is checked too; the word only has to begin with endstream (a memcmp)
            after = _Words(data, start + length).next()
            if after is None or after[0][:9] != b"endstream":
                length = -1
        if length >= 0:
            end = start + length
        else:
            ends = [e for e in (data.find(b"endstream", start), data.find(b"endobj", start)) if e >= 0]
            if not ends:
                return _NOTHING, start
            end = min(ends)
            if data[end - 2:end] == b"\r\n" and end - 2 >= start:
                end -= 2
            elif data[end - 1:end] in (b"\r", b"\n") and end - 1 >= start:
                end -= 1
        after = _Words(data, end)
        word = after.next()
        stop = end if word is None or word[0] == b"endobj" else after.pos
        return Stream(obj, data[start:end]), stop

    # ------------------------------------------------------------------ objects

    def _indirect_at(self, pos: int):
        """CPDF_SyntaxParser::GetIndirectObject (loose, with the document as holder): the
        object after `n g obj` at pos with its number, or (None, None)."""
        scan = _Words(self.data, pos)
        words = [scan.next() for _ in range(3)]
        if None in words or not words[0][1] or not words[1][1] or words[2][0] != b"obj":
            return None, None
        obj = _body(scan, False, self._held_stream(self.data))
        return (None if obj is _NOTHING else obj), _atoui(words[0][0])

    def get(self, num: int):
        if num in self._cache:
            return self._cache[num]
        if num in self._parsing:
            return None
        entry = self.offsets.get(num)
        obj = None
        self._parsing.add(num)
        try:
            with _deep_recursion():
                if entry and entry[0] == "f":
                    obj, found = self._indirect_at(entry[1])
                    if found != num:   # CPDF_Parser::ParseIndirectObjectAt: another object is none
                        obj = None
                elif entry and entry[0] == "s":
                    nums, data, first = self._load_objstm(entry[1])
                    if entry[2] < len(nums) and nums[entry[2]] == num:
                        offsets = self._objstm[entry[1]][3]
                        obj = _body(_Words(data, first + offsets[entry[2]]), False, self._held_stream(data))
                        obj = None if obj is _NOTHING else obj
        finally:
            self._parsing.discard(num)
        self._cache[num] = obj
        return obj

    def _load_objstm(self, num: int):
        if num not in self._objstm:
            stream = self.get(num)
            self._objstm[num] = self._objstm_from(stream) if isinstance(stream, Stream) else ([], b"", 0, [])
        nums, data, first, _ = self._objstm[num]
        return nums, data, first

    def _objstm_from(self, stream: Stream) -> tuple[list, bytes, int, list]:
        """An object stream's (numbers, decoded data, /First, offsets)."""
        data, _ = decode_filters(stream.raw, stream.dict, self.resolve)
        n, first = stream.get("N", 0), stream.get("First", 0)
        lexer = Lexer(data)
        nums, offsets = [], []
        for _ in range(n if isinstance(n, int) else 0):
            a, b = lexer.next(), lexer.next()
            if not isinstance(a, int) or not isinstance(b, int):
                break
            nums.append(a)
            offsets.append(b)
        return nums, data, first, offsets

    def resolve(self, value, depth: int = 0):
        while isinstance(value, Ref) and depth < 32:
            value = self.get(value.num)
            depth += 1
        return value

    def stream_data(self, stream: Stream) -> bytes:
        """A stream's bytes decoded up to an image codec (whose data stays encoded)."""
        if stream._decoded is None:
            try:
                stream._decoded = decode_filters(stream.raw, {k: self.resolve(v) for k, v in stream.dict.items()},
                                                 self.resolve)[0]
            except Exception:  # noqa: BLE001 - an unknown or broken filter: no data
                stream._decoded = b""
        return stream._decoded

    # ------------------------------------------------------------------ document structure

    @property
    def catalog(self) -> dict:
        """GetRoot: what the trailer's /Root refers to (a direct dictionary does not count)."""
        root = self.trailer.get("Root")
        return (_dict(self.resolve(root)) or {}) if isinstance(root, Ref) else {}

    # The page tree as CPDF_Document reads it. The page count is the root's /Count when it is
    # sane (RetrievePageCount, CountPages), whatever the kids say; a page is found by a traversal
    # that keeps its place between calls (GetMutablePageDictionary, TraversePDFPages), where a kid
    # that is no dictionary uses up a page, a node with no /Kids array is a page, and a leaf
    # found remembers its object number (m_PageList). So a /Count larger than the tree gives
    # pages that don't load, one smaller hides the rest, and which pages load can depend on the
    # order they are asked for. Inherited attributes come from the /Parent chain (GetPageAttr).

    def _pages_node(self) -> tuple[int, dict | None]:
        pages = self.catalog.get("Pages")
        return (pages.num if isinstance(pages, Ref) else 0), _dict(self.resolve(pages))

    def _count_pages(self) -> int:
        _, pages = self._pages_node()
        if pages is None:
            return 0
        if "Kids" not in pages:
            return 1
        with _deep_recursion():
            count = self._count_node(pages, [pages])
        return 0 if count is None else count

    @staticmethod
    def _is_branch(node: dict) -> bool:
        """GetNodeType: /Type /Pages or /Page decides; anything else (a reference included, since
        GetNameFor reads only a direct name) is guessed from /Kids and written in, as PDFium
        fixes its in-memory copy."""
        kind = node.get("Type")
        if isinstance(kind, Name) and kind in ("Pages", "Page"):
            return kind == "Pages"
        branch = "Kids" in node
        node[Name("Type")] = Name("Pages" if branch else "Page")
        return branch

    def _count_node(self, node: dict, visited: list) -> int | None:
        """CountPages: None when the tree holds too many pages."""
        count = _integer(self.resolve(node.get("Count")))
        if 0 < count < _PAGE_MAX:
            return count
        kids = self.resolve(node.get("Kids"))
        if not isinstance(kids, list):
            return 0
        count = 0
        for kid in kids:
            kid = _dict(self.resolve(kid))
            if kid is None or any(kid is v for v in visited):
                continue
            if self._is_branch(kid):
                visited.append(kid)
                inner = self._count_node(kid, visited)
                visited.pop()
                if inner is None:
                    return None
                count += inner
            else:
                count += 1
            if count >= _PAGE_MAX:
                return None
        node[Name("Count")] = count  # CountPages writes it back
        return count

    @property
    def page_count(self) -> int:
        if self._page_list is None:
            self._page_list = [None] * self._count_pages()
            self._traversal: list[list] = []   # [object number, node, next kid] per level
            self._next_page = 0
            self._max_level = False
        return len(self._page_list)

    def page_dict(self, index: int) -> tuple[int | None, dict] | None:
        """(object number, page dictionary) of page `index`, or None where PDFium finds none.
        The number is None for a page written directly in /Kids, 0 when it has none."""
        if not 0 <= index < self.page_count:
            return None
        known = self._page_list[index]
        if known is not None and known[0] != 0:
            if known[0] is None:
                return known
            obj = self.get(known[0])
            if isinstance(obj, dict):
                return known[0], obj
        num, pages = self._pages_node()
        if pages is None:
            return None
        if not self._traversal:
            self._traversal = [[num, pages, 0]]
            self._next_page = 0
            self._max_level = False
        togo = [index - self._next_page + 1]
        with _deep_recursion():
            page = self._traverse(index, togo, 0)
        self._next_page = index + 1
        return page

    def _kid(self, value) -> tuple[int | None, dict | None]:
        """CPDF_Array::ConvertToIndirectObjectAt, then GetMutableDictAt."""
        if isinstance(value, Ref):
            obj = self.get(value.num)
            if isinstance(obj, Stream):
                return 0, obj.dict   # the stream's dictionary has no object number of its own
            return value.num, obj if isinstance(obj, dict) else None
        return None, _dict(value)

    def _traverse(self, index: int, togo: list, level: int):
        if togo[0] < 0 or self._max_level:
            return None
        entry = self._traversal[level]
        node = entry[1]
        kids = self.resolve(node.get("Kids"))
        if not isinstance(kids, list):
            self._traversal.pop()
            if togo[0] != 1 or self._is_branch(node):
                return None
            self._page_list[index] = (entry[0], node)
            return entry[0], node
        if level >= _MAX_PAGE_LEVEL:
            self._traversal.pop()
            self._max_level = True
            return None
        page = None
        for k in range(entry[2], len(kids)):
            if togo[0] == 0:
                break
            num, kid = self._kid(kids[k])
            if kid is None:
                togo[0] -= 1
                entry[2] += 1
                continue
            if kid is node:
                entry[2] += 1
                continue
            if "Kids" not in kid:
                self._page_list[index - togo[0] + 1] = (num, kid)
                togo[0] -= 1
                entry[2] += 1
                if togo[0] == 0:
                    page = (num, kid)
                    break
            else:
                if len(self._traversal) == level + 1:
                    self._traversal.append([num, kid, 0])
                found = self._traverse(index, togo, level + 1)
                if len(self._traversal) == level + 1:
                    entry[2] += 1
                if len(self._traversal) != level + 1 or togo[0] == 0 or self._max_level:
                    page = found
                    break
        if entry[2] == len(kids):
            self._traversal.pop()
        return page

    def page_attr(self, node: dict, key: str):
        """CPDF_Page::GetPageAttr: the page's own value, else its parents'."""
        seen: list = []
        while node is not None and not any(node is v for v in seen):
            if key in node:
                value = self.resolve(node[key])
                if value is not None:
                    return value
            seen.append(node)
            node = _dict(self.resolve(node.get("Parent")))
        return None

    def inherited_page(self, index: int) -> tuple[Ref | None, dict] | None:
        """(reference, page dictionary with the inherited attributes written in), or None."""
        found = self.page_dict(index)
        if found is None:
            return None
        num, node = found
        page = dict(node)
        for k in _INHERITED:
            value = self.page_attr(node, k)
            if value is None:
                page.pop(k, None)
            else:
                page[Name(k)] = value
        return (Ref(num, 0) if num else None), page

    def page_index(self, ref) -> int:
        """CPDF_Document::GetPageIndex for a reference; a number is the page number."""
        if isinstance(ref, int) and not isinstance(ref, bool):  # a page number (remote-style destinations)
            return ref if 0 <= ref < self.page_count else -1
        if not isinstance(ref, Ref):
            return -1
        objnum = ref.num
        count = self.page_count
        skip, skipped = 0, False
        for i, known in enumerate(self._page_list):
            num = known[0] if known is not None else 0
            if num == objnum:
                return i
            if not skipped and not num:
                skip, skipped = i, True
        num, pages = self._pages_node()
        if pages is None:
            return -1
        state = [skip, 0]
        with _deep_recursion():
            found = self._find_page_index(num, pages, state, objnum, 0)
        if not 0 <= found < count:
            return -1
        obj = self.get(objnum)
        if isinstance(obj, dict) and self.resolve(obj.get("Type")) == "Page":
            self._page_list[found] = (objnum, obj)
        return found

    def _find_page_index(self, num: int, node: dict, state: list, objnum: int, level: int) -> int:
        """CPDF_Document::FindPageIndex; state = [pages still to skip, index so far]."""
        if "Kids" not in node:
            if objnum == num:
                return state[1]
            if state[0]:
                state[0] -= 1
            state[1] += 1
            return -1
        kids = self.resolve(node.get("Kids"))
        if not isinstance(kids, list) or level >= _MAX_PAGE_LEVEL:
            return -1
        count = _integer(self.resolve(node.get("Count"))) & 0xFFFFFFFFFFFFFFFF  # a size_t
        if count <= state[0]:
            state[0] -= count
            state[1] += count
            return -1
        if count and count == len(kids):
            for i, kid in enumerate(kids):
                if isinstance(kid, Ref) and kid.num == objnum:
                    return state[1] + i
        for kid in kids:
            kid_num, kid = self._kid(kid) if isinstance(kid, Ref) else (0, _dict(kid))
            if kid is None or kid is node:
                continue
            found = self._find_page_index(kid_num, kid, state, objnum, level + 1)
            if found >= 0:
                return found
        return -1

    def info(self) -> dict:
        info = self.resolve(self.trailer.get("Info"))
        return info if isinstance(info, dict) else {}

    def _number_tree(self, node, out: list, depth: int = 0) -> None:
        node = self.resolve(node)
        if not isinstance(node, dict) or depth > 32:
            return
        nums = self.resolve(node.get("Nums"))
        if isinstance(nums, list):
            for k in range(0, len(nums) - 1, 2):
                if isinstance(nums[k], int):
                    out.append((nums[k], self.resolve(nums[k + 1])))
        for kid in self.resolve(node.get("Kids")) or []:
            self._number_tree(kid, out, depth + 1)

    def name_tree(self, node, out: list, depth: int = 0) -> None:
        node = self.resolve(node)
        if not isinstance(node, dict) or depth > 32:
            return
        names = self.resolve(node.get("Names"))
        if isinstance(names, list):
            for k in range(0, len(names) - 1, 2):
                out.append((names[k], names[k + 1]))
        for kid in self.resolve(node.get("Kids")) or []:
            self.name_tree(kid, out, depth + 1)

    def page_labels(self) -> list[str]:
        entries: list = []
        self._number_tree(self.catalog.get("PageLabels"), entries)
        if not entries:
            return [""] * self.page_count
        entries.sort(key=lambda e: e[0])
        out = []
        for i in range(self.page_count):
            start, spec = None, None
            for s, d in entries:
                if s <= i:
                    start, spec = s, d
            if spec is None or not isinstance(spec, dict):
                out.append("")
                continue
            n = i - start + (spec.get("St", 1) if isinstance(spec.get("St"), int) else 1)
            style = spec.get("S")
            prefix = text_string(self.resolve(spec.get("P"))) if spec.get("P") is not None else ""
            out.append(prefix + _label_number(n, style))
        return out

    def dest_page(self, dest, depth: int = 0) -> int:
        dest = self.resolve(dest)
        if isinstance(dest, dict):
            dest = self.resolve(dest.get("D"))
        if isinstance(dest, (String, Name, bytes, str)) and depth < 4:
            return self.dest_page(self.named_dest(dest), depth + 1)
        if isinstance(dest, list) and dest:
            return self.page_index(dest[0])
        return -1

    def named_dest(self, name):
        if not hasattr(self, "_dest_map"):
            self._dest_map = {}
            for key, value in self.named_dests_raw():
                self._dest_map.setdefault(bytes(key) if isinstance(key, bytes) else str(key).encode("latin-1"), value)
        key = bytes(name) if isinstance(name, bytes) else str(name).encode("latin-1", "replace")
        return self._dest_map.get(key)

    def named_dests_raw(self) -> list[tuple]:
        """(name, destination) from the Dests name tree, then from the catalog's Dests dictionary."""
        out: list = []
        names = self.resolve(self.catalog.get("Names"))
        if isinstance(names, dict):
            self.name_tree(names.get("Dests"), out)
        dests = self.resolve(self.catalog.get("Dests"))
        if isinstance(dests, dict):
            out += [(Name(k), v) for k, v in dests.items()]
        return out


_ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
          (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]


def _label_number(n: int, style) -> str:
    if style == "D":
        return str(n)
    if style in ("R", "r"):
        out = ""
        for v, s in _ROMAN:
            while n >= v:
                out += s
                n -= v
        return out.upper() if style == "R" else out
    if style in ("A", "a"):
        if n < 1:
            return ""
        letter = chr(ord("A") + (n - 1) % 26) * ((n - 1) // 26 + 1)
        return letter if style == "A" else letter.lower()
    return ""


# ---------------------------------------------------------------------- writing


def _write_name(n: str) -> bytes:
    out = bytearray(b"/")
    for b in n.encode("utf-8"):
        if b < 0x21 or b > 0x7E or b in b"()<>[]{}/%#":
            out += b"#%02X" % b
        else:
            out.append(b)
    return bytes(out)


def _write_number(v) -> bytes:
    if isinstance(v, bool):
        return b"true" if v else b"false"
    if isinstance(v, int):
        return str(v).encode()
    text = f"{v:.6f}".rstrip("0").rstrip(".")
    return (text if text not in ("", "-", "-0") else "0").encode()


def serialize(v, keep) -> bytes:
    """An object in file syntax; references pass through `keep` (None drops them to null)."""
    if v is None:
        return b"null"
    if isinstance(v, (bool, int, float)):
        return _write_number(v)
    if isinstance(v, Name):
        return _write_name(v)
    if isinstance(v, Ref):
        return b"%d %d R" % (v.num, v.gen) if keep(v) else b"null"
    if isinstance(v, bytes):
        return b"<" + bytes(v).hex().encode() + b">"
    if isinstance(v, str):
        return _write_name(v)
    if isinstance(v, list):
        return b"[" + b" ".join(serialize(x, keep) for x in v) + b"]"
    if isinstance(v, dict):
        return b"<<" + b"".join(_write_name(k) + b" " + serialize(x, keep) + b"\n" for k, x in v.items()) + b">>"
    return b"null"


def write_file(pdf: PdfFile, keep_pages: list[int], boxes: dict[int, tuple]) -> bytes:
    """A new file with the pages `keep_pages` (in document order), media and crop box replaced
    for those in `boxes` (PDF user space rectangles). Every other object keeps its number; a
    reference to a page that was left out becomes null."""
    pages = [pdf.inherited_page(i) for i in range(pdf.page_count)]
    kept = sorted(i for i in set(keep_pages) if pages[i] is not None)
    page_refs = {p[0].num for p in pages if p is not None and p[0] is not None}
    kept_refs = {pages[i][0].num for i in kept if pages[i][0] is not None}
    pages_num = max(pdf.offsets or {0: None}) + 1
    objects: dict[int, object] = {}

    def keep(ref: Ref) -> bool:
        return ref.num == pages_num or ref.num not in page_refs or ref.num in kept_refs

    # the new page dictionaries: inherited attributes written out, one parent
    new_pages: dict[int, dict] = {}
    extra = pages_num + 1
    kids = []
    for i in kept:
        ref, page = pages[i]
        d = {k: v for k, v in page.items() if k != "Parent"}
        d["Parent"] = Ref(pages_num, 0)
        if i in boxes:
            d["MediaBox"] = list(boxes[i])
            d["CropBox"] = list(boxes[i])
        if ref is None:
            ref = Ref(extra, 0)
            extra += 1
        new_pages[ref.num] = d
        kids.append(ref)
    objects[pages_num] = {Name("Type"): Name("Pages"), Name("Kids"): kids, Name("Count"): len(kids)}
    catalog = dict(pdf.catalog)
    catalog[Name("Pages")] = Ref(pages_num, 0)
    root_ref = pdf.trailer.get("Root")
    root_num = root_ref.num if isinstance(root_ref, Ref) else extra
    if not isinstance(root_ref, Ref):
        extra += 1
    objects[root_num] = catalog
    objects.update(new_pages)

    # everything reachable from the catalog and the info dictionary
    todo = [catalog, pdf.trailer.get("Info")] + list(new_pages.values())
    while todo:
        v = todo.pop()
        if isinstance(v, Ref):
            if v.num in objects or not keep(v):
                continue
            obj = pdf.get(v.num)
            if v.num in page_refs:
                continue
            objects[v.num] = obj
            todo.append(obj.dict if isinstance(obj, Stream) else obj)
        elif isinstance(v, dict):
            todo.extend(x for k, x in v.items() if not (k == "Parent" and v.get("Type") == "Page"))
        elif isinstance(v, list):
            todo.extend(v)
        elif isinstance(v, Stream):
            todo.append(v.dict)

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objects):
        obj = objects[num]
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num
        if isinstance(obj, Stream):
            d = dict(obj.dict)
            d[Name("Length")] = len(obj.raw)
            out += serialize(d, keep) + b"\nstream\n" + obj.raw + b"\nendstream"
        else:
            out += serialize(obj, keep)
        out += b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for num in range(1, size):
        out += (b"%010d 00000 n \n" % offsets[num]) if num in offsets else b"0000000000 65535 f \n"
    trailer = {Name("Size"): size, Name("Root"): Ref(root_num, 0)}
    if isinstance(pdf.trailer.get("Info"), Ref) and pdf.trailer["Info"].num in objects:
        trailer[Name("Info")] = pdf.trailer["Info"]
    out += b"trailer\n" + serialize(trailer, lambda r: True) + b"\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def read(source) -> PdfFile:
    data = source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
    try:
        return PdfFile(bytes(data))
    except (PdfFileError, PdfSyntaxError):
        raise
    except Exception as e:  # noqa: BLE001
        raise PdfFileError(f"unreadable PDF: {e}") from e


__all__ = ["PdfFile", "PdfFileError", "read", "text_string", "write_file", "serialize", "END"]
