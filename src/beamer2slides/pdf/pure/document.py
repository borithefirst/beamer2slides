"""The file layer: cross-reference tables and streams, object streams, the page tree, page labels,
named destinations, text strings, and writing a new file (ISO 32000-1, 7.5, 7.7, 12.3, 12.4.2)."""

from __future__ import annotations

import codecs
import re
from pathlib import Path

from .filters import decode as decode_filters
from .syntax import END, Lexer, Name, Op, PdfSyntaxError, Ref, Stream, String, parse_object

_OBJ_HEADER = re.compile(rb"(?<![0-9])(\d+)[\x00\t\n\x0c\r ]+(\d+)[\x00\t\n\x0c\r ]+obj\b")
_STARTXREF = re.compile(rb"startxref[\x00\t\n\x0c\r ]+(\d+)")
_INHERITED = ("Resources", "MediaBox", "CropBox", "Rotate")


class PdfFileError(Exception):
    pass


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
        try:
            self._read_xref()
        except Exception:  # noqa: BLE001 - a broken table: rebuild it from the objects themselves
            self.offsets, self.trailer = {}, {}
        if not self.trailer.get("Root") or not self._root_ok():
            self._reconstruct()
        if self.trailer.get("Encrypt") is not None:
            raise PdfFileError("encrypted PDFs are not supported")
        self.pages = self._pages()

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
                return parse_object(lexer)
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
        lexer = Lexer(self.data, pos)
        num, gen, kw = lexer.next(), lexer.next(), lexer.next()
        if kw != "obj":
            raise PdfFileError("no xref stream")
        stream = self._stream_after(lexer)
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
        try:
            root = self.resolve(self.trailer.get("Root"))
            return isinstance(root, dict) and isinstance(self.resolve(root.get("Pages")), dict)
        except Exception:  # noqa: BLE001
            return False

    def _reconstruct(self) -> None:
        """Every `n g obj` in the file, the last one of a number winning; the trailer from the
        last trailer dictionary or cross-reference stream, else the catalog found."""
        self.offsets, self._cache = {}, {}
        for m in _OBJ_HEADER.finditer(self.data):
            self.offsets[int(m.group(1))] = ("f", m.start(), int(m.group(2)))
        trailer: dict = {}
        for m in re.finditer(rb"trailer[\x00\t\n\x0c\r ]*<<", self.data):
            try:
                t = parse_object(Lexer(self.data, m.start() + 7))
                if isinstance(t, dict):
                    trailer.update(t)
            except Exception:  # noqa: BLE001
                pass
        if not trailer.get("Root"):
            for num in list(self.offsets):
                try:
                    obj = self.get(num)
                except Exception:  # noqa: BLE001
                    continue
                d = obj.dict if isinstance(obj, Stream) else obj
                if isinstance(d, dict):
                    if d.get("Type") == "Catalog":
                        trailer["Root"] = Ref(num, 0)
                    elif d.get("Type") == "XRef" and "Info" in d:
                        trailer.setdefault("Info", d["Info"])
                    # objects inside object streams
                    if isinstance(obj, Stream) and d.get("Type") == "ObjStm":
                        self._index_objstm(num)
        if not trailer.get("Root"):
            raise PdfFileError("no document catalog")
        self.trailer = trailer

    def _index_objstm(self, num: int) -> None:
        nums, _, _ = self._load_objstm(num)
        for i, n in enumerate(nums):
            self.offsets.setdefault(n, ("s", num, i))

    # ------------------------------------------------------------------ objects

    def _stream_after(self, lexer: Lexer):
        """The object after `n g obj`: a stream when the dictionary is followed by `stream`."""
        obj = parse_object(lexer)
        save = lexer.pos
        t = lexer.next()
        if not (isinstance(obj, dict) and t == "stream" and type(t) is Op):
            lexer.pos = save
            return obj
        data = self.data
        start = lexer.pos
        if data[start:start + 2] == b"\r\n":
            start += 2
        elif data[start:start + 1] in (b"\n", b"\r"):
            start += 1
        length = obj.get("Length")
        if isinstance(length, Ref):
            try:
                length = self.get(length.num)
            except Exception:  # noqa: BLE001
                length = None
        end = None
        if isinstance(length, int) and length >= 0:
            end = start + length
            tail = data[end:end + 30].lstrip()
            if not tail.startswith(b"endstream"):
                end = None
        if end is None:
            found = data.find(b"endstream", start)
            end = found if found >= 0 else len(data)
            while end > start and data[end - 1:end] in (b"\n", b"\r"):
                end -= 1
                if data[end - 1:end] == b"\r" and data[end:end + 1] == b"\n":
                    continue
                break
        return Stream(obj, data[start:end])

    def get(self, num: int):
        if num in self._cache:
            return self._cache[num]
        entry = self.offsets.get(num)
        obj = None
        if entry and entry[0] == "f":
            lexer = Lexer(self.data, entry[1])
            n, g, kw = lexer.next(), lexer.next(), lexer.next()
            if n == num and kw == "obj":
                obj = self._stream_after(lexer)
            else:  # a wrong offset: look for the object nearby, then anywhere
                m = re.compile(rb"(?<![0-9])%d[\x00\t\n\x0c\r ]+%d[\x00\t\n\x0c\r ]+obj\b" % (num, entry[2])).search(self.data)
                if m:
                    lexer = Lexer(self.data, m.end())
                    obj = self._stream_after(lexer)
        elif entry and entry[0] == "s":
            nums, data, first = self._load_objstm(entry[1])
            if entry[2] < len(nums):
                offsets = self._objstm[entry[1]][3]
                lexer = Lexer(data, first + offsets[entry[2]])
                obj = parse_object(lexer)
        if type(obj) is Op:
            obj = None
        self._cache[num] = obj
        return obj

    def _load_objstm(self, num: int):
        if num not in self._objstm:
            stream = self.get(num)
            if not isinstance(stream, Stream):
                self._objstm[num] = ([], b"", 0, [])
            else:
                data, _ = decode_filters(stream.raw, stream.dict, self.resolve)
                n, first = stream.get("N", 0), stream.get("First", 0)
                lexer = Lexer(data)
                nums, offsets = [], []
                for _ in range(n):
                    a, b = lexer.next(), lexer.next()
                    if not isinstance(a, int) or not isinstance(b, int):
                        break
                    nums.append(a)
                    offsets.append(b)
                self._objstm[num] = (nums, data, first, offsets)
        nums, data, first, _ = self._objstm[num]
        return nums, data, first

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
        root = self.resolve(self.trailer.get("Root"))
        return root if isinstance(root, dict) else {}

    def _pages(self) -> list[tuple[Ref | None, dict]]:
        """(reference, page dictionary with inherited attributes filled in) in page order."""
        out: list[tuple[Ref | None, dict]] = []
        seen: set = set()

        def walk(ref, node, inherited, depth):
            if not isinstance(node, dict) or depth > 64:
                return
            key = ref if ref is not None else id(node)
            if key in seen:
                return
            seen.add(key)
            here = dict(inherited)
            for k in _INHERITED:
                if k in node:
                    here[k] = node[k]
            kids = self.resolve(node.get("Kids"))
            if node.get("Type") == "Pages" or (isinstance(kids, list) and node.get("Type") != "Page"):
                for kid in kids or []:
                    walk(kid if isinstance(kid, Ref) else None, self.resolve(kid), here, depth + 1)
            else:
                page = dict(node)
                for k in _INHERITED:
                    if k not in page and k in here:
                        page[k] = here[k]
                out.append((ref, page))

        pages = self.catalog.get("Pages")
        walk(pages if isinstance(pages, Ref) else None, self.resolve(pages), {}, 0)
        return out

    def page_index(self, ref) -> int:
        if not hasattr(self, "_page_numbers"):
            self._page_numbers = {r.num: i for i, (r, _) in enumerate(self.pages) if r is not None}
        if isinstance(ref, Ref):
            return self._page_numbers.get(ref.num, -1)
        if isinstance(ref, int) and not isinstance(ref, bool):  # a page number (remote-style destinations)
            return ref if 0 <= ref < len(self.pages) else -1
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
            return [""] * len(self.pages)
        entries.sort(key=lambda e: e[0])
        out = []
        for i in range(len(self.pages)):
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
    kept = sorted(set(keep_pages))
    page_refs = {pdf.pages[i][0].num for i in range(len(pdf.pages)) if pdf.pages[i][0] is not None}
    kept_refs = {pdf.pages[i][0].num for i in kept if pdf.pages[i][0] is not None}
    pages_num = max(pdf.offsets or {0: None}) + 1
    objects: dict[int, object] = {}

    def keep(ref: Ref) -> bool:
        return ref.num == pages_num or ref.num not in page_refs or ref.num in kept_refs

    # the new page dictionaries: inherited attributes written out, one parent
    new_pages: dict[int, dict] = {}
    extra = pages_num + 1
    kids = []
    for i in kept:
        ref, page = pdf.pages[i]
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
