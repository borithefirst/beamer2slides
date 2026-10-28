"""One PDF out of several, page after page: what a compile split into pieces gives back as the
document a whole compile would have written (`inverse.Workspace.piece_pass`, docs/compile-latency.md).

Each file's objects keep their own numbering moved past the files before it, so nothing is
shared and nothing is renamed but references. The pages hang from one new page tree with their
inherited attributes written in (as `write_file` does). The file of the first page gives the catalog
and the document information; named destinations of every file are one name tree, and the page labels one
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
    return joined(paths, files, [(k, i) for k, pdf in enumerate(files) for i in range(pdf.page_count)
                                 if pdf.inherited_page(i) is not None])


def join_pages(paths: Sequence[str], order: Sequence[tuple[int, int]]) -> Joined:
    """Pages of the files at `paths` as one PDF: `order` names each, (file, its page index), in the
    joined order - a compile that typeset some frames again keeps the others' pages from the last
    one (`inverse.Workspace.compile_patched`). A destination on a page left out is left out with
    it; a kept page or the catalog naming one refuses the join."""
    return joined(paths, [read(p) for p in paths], order)


def joined(paths: Sequence[str], files: Sequence[PdfFile], order: Sequence[tuple[int, int]]) -> Joined:
    if not order:
        raise JoinError("no pages to join")
    lead = order[0][0]
    items = outline(files[lead])
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
    page_refs: list[set[int]] = []   # each file's page objects
    kept: list[set[int]] = []        # those `order` takes
    position: list[dict[int, int]] = []   # where in the joined pages each of those stands

    def moved(v: PdfObject, k: int) -> PdfObject:
        """`v` of file `k` with its references numbered as joined; a reference to one of its pages
        is to that page, which `join` writes itself."""
        if isinstance(v, Ref):
            num = v.num + bases[k]
            if v.num in page_refs[k] and v.num not in kept[k]:
                raise JoinError(f"{paths[k]} names a page the join leaves out")
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
    pages: list[tuple[int, int, PdfDict]] = []   # (joined number, file, page dictionary)
    found = [[pdf.inherited_page(i) for i in range(pdf.page_count)] for pdf in files]
    for k in range(len(files)):
        page_refs.append({f[0].num for f in found[k] if f is not None and f[0] is not None})
        kept.append(set())
        position.append({})
    runs: list[tuple[int, int, int]] = []   # (file, first joined position, after its last) in order
    for k, i in order:
        if not 0 <= k < len(files) or not 0 <= i < files[k].page_count:
            raise JoinError(f"no page {i} in file {k}")
        f = found[k][i]
        if f is None:
            raise JoinError(f"{paths[k]}: page {i} does not read")
        ref, page = f
        if ref is None:
            num = extra
            extra += 1
        else:
            if ref.num in kept[k]:
                raise JoinError(f"{paths[k]}: page {i} twice")
            kept[k].add(ref.num)
            position[k][ref.num] = len(kids)
            num = ref.num + bases[k]
        if runs and runs[-1][0] == k and runs[-1][2] == len(kids):
            runs[-1] = (k, runs[-1][1], len(kids) + 1)
        else:
            runs.append((k, len(kids), len(kids) + 1))
        pages.append((num, k, page))
        kids.append(Ref(num, 0))
    for num, k, page in pages:
        d = moved_dict({key: v for key, v in page.items() if key != "Parent"}, k)
        d[Name("Parent")] = Ref(pages_num, 0)
        written[num] = d
    written[pages_num] = {Name("Type"): Name("Pages"), Name("Kids"): kids, Name("Count"): len(kids)}

    first = files[lead]
    catalog = moved_dict({key: v for key, v in first.catalog.items() if key not in _CATALOG_OWN}, lead)
    names: PdfDict = {}
    first_names = first.resolve(first.catalog.get("Names"))
    if isinstance(first_names, dict):
        names = moved_dict({key: v for key, v in first_names.items() if key != "Dests"}, lead)
    # a name two files give goes to the earlier page: a file compiled from part of a document
    # (`pieces`) puts the anchors of what it skipped on its first page, after the true ones
    found_at: dict[bytes, tuple[int, int, PdfObject]] = {}   # name: (joined position, file, value)
    stand_ins: dict[bytes, tuple[int, PdfObject]] = {}
    labels: PdfArray = []
    # a file's stand-ins after those of the files whose last page comes before its own
    for k in sorted({k for k, _ in order}, key=lambda k: max(e for f, _, e in runs if f == k)):
        pdf = files[k]
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
            on = _dest_page(pdf, value)
            if on is not None and on in page_refs[k] and on not in kept[k]:
                continue   # its page is left out, and the destination with it
            if _stand_in(pdf, value, last_num):
                stand_ins[bytes(key)] = (k, value)
                continue
            at = position[k].get(on, len(kids)) if on is not None else len(kids)
            if bytes(key) not in found_at or at < found_at[bytes(key)][0]:
                found_at[bytes(key)] = (at, k, value)
    for k, start, end in runs:
        pdf = files[k]
        own = range(start, end)
        numbered = [(key, value) for key, value in leaves(pdf, pdf.catalog.get("PageLabels"), "Nums")
                    if isinstance(key, int)]
        before = [value for key, value in numbered if key < own.start]
        inside = [(key, value) for key, value in numbered if key in own]
        if not inside or inside[0][0] != own.start:
            # the range the run's first page is in: the entry before it, else no label
            inside.insert(0, (own.start, before[-1] if before else {}))
        for at, value in inside:
            labels += [at, moved(value, k)]
    dests = {key: moved(value, k) for key, (_, k, value) in found_at.items()}
    for key, (k, value) in stand_ins.items():
        if key not in dests:
            dests[key] = moved(value, k)

    catalog[Name("Pages")] = Ref(pages_num, 0)
    if items:
        catalog[Name("Outlines")] = moved(first.catalog.get("Outlines"), lead)
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
    info_ref = moved(info, lead) if isinstance(info, Ref) else None

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


def _dest_page(pdf: PdfFile, value: PdfObject) -> int | None:
    """The number of the page object a destination names, if it names one by reference."""
    dest = pdf.resolve(value)
    if isinstance(dest, dict):
        dest = pdf.resolve(dest.get("D"))
    if isinstance(dest, list) and dest and isinstance(dest[0], Ref):
        return dest[0].num
    return None


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
