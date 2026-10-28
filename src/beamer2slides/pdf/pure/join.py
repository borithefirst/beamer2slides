"""One PDF out of several, page after page: what a compile split into pieces gives back as the
document a whole compile would have written (`inverse.Workspace.piece_pass`, docs/compile-latency.md).

Each file's objects keep their own numbering moved past the files before it, so nothing is
shared and nothing is renamed but references. The pages hang from one new page tree with their
inherited attributes written in (as `write_file` does). The first file gives the catalog and the
document information; named destinations of every file are one name tree, and the page labels one
number tree.

The files are pieces of one document. Their page labels already count pages document-wide, so a
file's entries are taken for its own pages as they stand. A link to a destination its file does
not hold makes LuaTeX write a stand-in, `[last page /Fit]`: a destination some file holds for real
wins over every stand-in, and one that only stand-ins name is the last file's, as a whole compile
would have put it on its last page. Every piece builds its outline from the bookmarks the last pass
wrote, so all hold the same one, its items naming their destinations: the first file's is taken
when every file's is alike and goes by name only, else the join is refused."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .document import PdfFile, read, serialize_refs
from .syntax import Name, PdfArray, PdfDict, PdfObject, Ref, Stream, String

# what the joined catalog says itself, or leaves out, instead of the first file's
_CATALOG_OWN = ("Pages", "Names", "Dests", "PageLabels", "Outlines", "StructTreeRoot", "MarkInfo")


@dataclass(frozen=True, kw_only=True)
class Joined:
    data: bytes   # the PDF
    pages: int


class JoinError(Exception):
    """Files this join cannot make one of."""


def join(paths: Sequence[str]) -> Joined:
    """The files at `paths` as one PDF, in that order."""
    files = [read(p) for p in paths]
    items = outline(files[0])
    for path, pdf in zip(paths, files):
        mine = outline(pdf)
        if mine is None:
            raise JoinError(f"{path} has an outline item that goes to a page, not a name")
        if mine != items:
            raise JoinError(f"{path} has an outline unlike the first file's")
    bases: list[int] = []
    base = 0
    for pdf in files:
        bases.append(base)
        base += max(pdf.offsets or {0: None}) + 1
    pages_num = base
    root_num = base + 1
    extra = base + 2
    # every object written, numbered as joined, its references moved already
    written: dict[int, PdfObject] = {}
    todo: list[tuple[int, int]] = []   # (file, its own number) reached and not yet written
    page_refs: list[set[int]] = []

    def moved(v: PdfObject, k: int) -> PdfObject:
        """`v` of file `k` with its references numbered as joined; a reference to one of its pages
        is to that page, which `join` writes itself."""
        if isinstance(v, Ref):
            num = v.num + bases[k]
            if num not in written and v.num not in page_refs[k]:
                written[num] = None   # taken: written once read
                todo.append((k, v.num))
            return Ref(num, 0)
        if isinstance(v, list):
            return [moved(x, k) for x in v]
        if isinstance(v, dict):
            return {key: moved(x, k) for key, x in v.items()}
        if isinstance(v, Stream):
            return Stream(moved_dict(v.dict, k), v.raw)
        return v

    def moved_dict(d: PdfDict, k: int) -> PdfDict:
        return {key: moved(x, k) for key, x in d.items()}

    kids: PdfArray = []
    starts: list[int] = []
    pages: list[tuple[int, int, PdfDict]] = []   # (joined number, file, page dictionary)
    for k, pdf in enumerate(files):
        starts.append(len(kids))
        found = [f for f in (pdf.inherited_page(i) for i in range(pdf.page_count)) if f is not None]
        page_refs.append({ref.num for ref, _ in found if ref is not None})
        for ref, page in found:
            if ref is None:
                num = extra
                extra += 1
            else:
                num = ref.num + bases[k]
            pages.append((num, k, page))
            kids.append(Ref(num, 0))
    for num, k, page in pages:
        d = moved_dict({key: v for key, v in page.items() if key != "Parent"}, k)
        d[Name("Parent")] = Ref(pages_num, 0)
        written[num] = d
    written[pages_num] = {Name("Type"): Name("Pages"), Name("Kids"): kids, Name("Count"): len(kids)}

    first = files[0]
    catalog = moved_dict({key: v for key, v in first.catalog.items() if key not in _CATALOG_OWN}, 0)
    names: PdfDict = {}
    first_names = first.resolve(first.catalog.get("Names"))
    if isinstance(first_names, dict):
        names = moved_dict({key: v for key, v in first_names.items() if key != "Dests"}, 0)
    dests: dict[bytes, PdfObject] = {}
    stand_ins: dict[bytes, PdfObject] = {}
    labels: PdfArray = []
    for k, pdf in enumerate(files):
        found_dests: list[tuple[PdfObject, PdfObject]] = []
        tree = pdf.resolve(pdf.catalog.get("Names"))
        if isinstance(tree, dict):
            found_dests += leaves(pdf, tree.get("Dests"), "Names")
        old = pdf.resolve(pdf.catalog.get("Dests"))
        if isinstance(old, dict):
            found_dests += [(String(key.encode("latin-1", "replace")), value) for key, value in old.items()]
        last = pdf.page_dict(pdf.page_count - 1) if pdf.page_count else None
        last_num = last[0] if last is not None and last[0] is not None else -1
        for key, value in found_dests:
            if not isinstance(key, bytes):
                continue
            if _stand_in(pdf, value, last_num):
                stand_ins[bytes(key)] = moved(value, k)
            elif bytes(key) not in dests:
                dests[bytes(key)] = moved(value, k)
        own = range(starts[k], starts[k] + pdf.page_count)
        numbered = [(key, value) for key, value in leaves(pdf, pdf.catalog.get("PageLabels"), "Nums")
                    if isinstance(key, int)]
        before = [value for key, value in numbered if key < own.start]
        inside = [(key, value) for key, value in numbered if key in own]
        if pdf.page_count and (not inside or inside[0][0] != own.start):
            # the range the file's first page is in: the entry before it, else no label
            inside.insert(0, (own.start, before[-1] if before else {}))
        for at, value in inside:
            labels += [at, moved(value, k)]
    for key, value in stand_ins.items():
        dests.setdefault(key, value)

    catalog[Name("Pages")] = Ref(pages_num, 0)
    if items:
        catalog[Name("Outlines")] = moved(first.catalog.get("Outlines"), 0)
    if dests:
        flat: PdfArray = []
        for key in sorted(dests):
            flat += [String(key), dests[key]]
        written[extra] = {Name("Names"): flat}
        names[Name("Dests")] = Ref(extra, 0)
        extra += 1
    if names:
        catalog[Name("Names")] = names
    if labels:
        catalog[Name("PageLabels")] = {Name("Nums"): labels}
    written[root_num] = catalog
    info = first.trailer.get("Info") if first.trailer is not None else None
    info_ref = moved(info, 0) if isinstance(info, Ref) else None

    # everything the pages, the catalog and the document information reach, file by file
    while todo:
        k, num = todo.pop()
        written[num + bases[k]] = moved(files[k].get(num), k)

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num in sorted(written):
        obj = written[num]
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num
        if isinstance(obj, Stream):
            d = dict(obj.dict)
            d[Name("Length")] = len(obj.raw)
            out += serialize_refs(d, _ref) + b"\nstream\n" + obj.raw + b"\nendstream"
        else:
            out += serialize_refs(obj, _ref)
        out += b"\nendobj\n"
    size = max(written) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for num in range(1, size):
        out += (b"%010d 00000 n \n" % offsets[num]) if num in offsets else b"0000000000 65535 f \n"
    trailer: PdfDict = {Name("Size"): size, Name("Root"): Ref(root_num, 0)}
    if info_ref is not None:
        trailer[Name("Info")] = info_ref
    out += b"trailer\n" + serialize_refs(trailer, _ref) + b"\nstartxref\n%d\n%%%%EOF\n" % xref
    return Joined(data=bytes(out), pages=len(kids))


def _ref(ref: Ref) -> bytes:
    return b"%d 0 R" % ref.num


def _stand_in(pdf: PdfFile, value: PdfObject, last_page: int) -> bool:
    """Whether a destination is the stand-in LuaTeX writes for one its file does not hold."""
    dest = pdf.resolve(value)
    return (isinstance(dest, list) and len(dest) == 2 and isinstance(dest[0], Ref) and dest[0].num == last_page
            and dest[1] == "Fit")


def outline(pdf: PdfFile) -> list[tuple[int, bytes, bytes]] | None:
    """The items of a file's outline, in order: (depth, title, destination name); None when one
    goes to its destination other than by name."""
    out: list[tuple[int, bytes, bytes]] = []
    seen: set[int] = set()

    def walk(first: PdfObject, depth: int) -> bool:
        item = first
        while isinstance(item, Ref) and item.num not in seen and depth <= 32:
            seen.add(item.num)
            d = pdf.resolve(item)
            if not isinstance(d, dict):
                return False
            dest = pdf.resolve(d.get("Dest"))
            action = pdf.resolve(d.get("A"))
            if dest is None and isinstance(action, dict) and action.get("S") == "GoTo":
                dest = pdf.resolve(action.get("D"))
            title = pdf.resolve(d.get("Title"))
            if not isinstance(dest, bytes) or not isinstance(title, bytes):
                return False
            out.append((depth, bytes(title), bytes(dest)))
            if not walk(d.get("First"), depth + 1):
                return False
            item = d.get("Next")
        return item is None

    root = pdf.resolve(pdf.catalog.get("Outlines"))
    if isinstance(root, dict) and not walk(root.get("First"), 0):
        return None
    return out


def leaves(pdf: PdfFile, node: PdfObject, kind: str) -> list[tuple[PdfObject, PdfObject]]:
    """The (key, value) pairs of a name tree (`kind` "Names") or number tree ("Nums"), in order."""
    out: list[tuple[PdfObject, PdfObject]] = []
    seen: set[int] = set()

    def walk(value: PdfObject, depth: int) -> None:
        if isinstance(value, Ref):
            if value.num in seen or depth > 32:
                return
            seen.add(value.num)
        d = pdf.resolve(value)
        if not isinstance(d, dict):
            return
        pairs = pdf.resolve(d.get(kind))
        if isinstance(pairs, list):
            for i in range(0, len(pairs) - 1, 2):
                out.append((pdf.resolve(pairs[i]), pairs[i + 1]))
        kids = pdf.resolve(d.get("Kids"))
        if isinstance(kids, list):
            for kid in kids:
                walk(kid, depth + 1)

    walk(node, 0)
    return out
