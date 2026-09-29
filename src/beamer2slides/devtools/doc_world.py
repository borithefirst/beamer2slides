"""A Google Doc that lives in memory and applies the API's own requests to itself.

This is the Docs twin of `fuzz_world.py`, and it is deliberately *not* built the same
way. The Slides reference applier merges state: it works out what a correct sync
leaves behind and never looks at a request. That blindness cost the campaign the one
bug that actually killed syncs (CLAUDE.md, seeds 608/616: the newline a text ends on
cannot be deleted, so the batch was refused whole and nothing offline could see it).

So this world consumes the requests `doc_merge.plan` produces, and applies them under
Google's rules — every one of them measured and written down in docs/google-docs.md,
which is this module's specification:

* **Indices are UTF-16 code units**, not characters, and index 0 is the section break:
  a body's text begins at 1. An astral character is two units, so a text model over
  Python characters would put every edit after an emoji one unit early (`utf16_units`).
* **A chip is one index unit** however long its words look; **an equation is several**
  (13 on the document that was measured). A request may never cut one in half.
* **The newline a body ends on cannot be deleted** — nor the one that ends a table
  cell, nor **the one in front of a table** ("Invalid deletion range").
* **Nothing can be inserted at a table's own index**, nor at a row's or a cell's.
* **`insertTable` splits the paragraph its index is in**, and that index must be
  inside a paragraph; at the end of a segment Docs keeps a paragraph after the table.
* **Deleting a paragraph mark merges the two paragraphs, keeping the first one's
  style** — which is what makes `doc_merge._delete_range`'s borrowing work.
* **Inserted text inherits the style of the character in front of it.**
* **A refused request throws out the whole batch**: `apply` works on a copy and puts
  nothing at all into the world when one request is refused (`Refused`). That failure
  mode is the point of this module.
* Named ranges are half-open `[start, end)`: text typed at an anchor's first index
  falls outside it, text typed inside grows it, and a range whose text is all deleted
  **disappears** — which is the signal the merge reads as "this block is gone".

What is *not* measured, and is modelled the most plausible way instead: how many
index units a table's own structure costs. Here a table costs one, each row one and
each cell one, which is the shape `documents.get` reports (every row and every cell
has a start index of its own). Nothing in the merge depends on the number — every
index it writes is one it read back from here — so the model only has to be
self-consistent, and it is.

The world renders itself as `documents.get` JSON (`read`), so the campaign reads it
with the real `doc_ir.from_document`, the real `apply_keys` and the real
`restore_unreadable`: the read side is under test too, not stubbed.

**Every unit is immutable** (frozen dataclasses; a style or a measures dict is never
written once it is in a unit). A request that restyles text or a paragraph puts a new
unit where the old one stood. That is what lets two paragraphs share one `Para` after
a split, and what lets `apply` take its spare copy by copying lists alone
(`Tab.copied`): the copy shares every unit with the world, and only the lists a
request splices are the copy's own.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from .. import doc_ir, doc_merge
from ..doc_ir import Block, Ir, Mark, MarkApi, Measures, Run, Script, Style
from ..google_types import (AddDocumentTabRequest, CreateNamedRangeRequest,
                            CreateParagraphBulletsRequest, DeleteContentRangeRequest,
                            DeleteNamedRangeRequest, DeleteParagraphBulletsRequest,
                            DeleteTabRequest, DeleteTableLineRequest, DocsBatchUpdateResponse,
                            DocsDimension, DocsDocumentTab, DocsEmbeddedObject,
                            DocsInlineObject, DocsList, DocsLocation, DocsNamedRanges,
                            DocsNamedStyle, DocsNestingLevel, DocsParagraph,
                            DocsParagraphElement, DocsParagraphStyle, DocsRangeWrite,
                            DocsRequest, DocsStructuralElement, DocsTab, DocsTableCell,
                            DocsTableCellLocation, DocsTableRow, DocsTabProperties,
                            DocsTextStyle, Document, InsertDateRequest,
                            InsertInlineImageRequest, InsertPersonRequest, InsertTableRequest,
                            InsertTableColumnRequest, InsertTableRowRequest, InsertTextRequest,
                            UpdateDocumentTabPropertiesRequest, UpdateParagraphStyleRequest,
                            UpdateTextStyleRequest)
from ..json_types import JsonObject
from ..typing_compat import assert_never


# What the API answers a request it will not take. Google throws out the whole batch
# with it, so this is never caught request by request (docs/google-docs.md).
class Refused(RuntimeError):
    def __init__(self, request: Mapping[str, object], why: str) -> None:
        self.request, self.why = request, why
        super().__init__(f"{next(iter(request), '?')}: {why}")


# The glyphs `createParagraphBullets` leaves behind, by preset (measured: after one,
# the list reads back as what it is, where an imported one never could).
ORDERED_PRESET = "NUMBERED_DECIMAL_ALPHA_ROMAN"
# What an equation costs in index units (measured on one document), and a table of
# contents where the corpus does not say.
EQUATION_SIZE: Final = 13
TOC_SIZE: Final = 4


# ---------------------------------------------------------------- UTF-16 code units

def utf16_units(text: str) -> list[str]:
    """`text` as the Docs API counts it: one string per UTF-16 code unit.

    An astral character (an emoji, a mathematical letter) is two of them, and a model
    over Python characters would put every index after one a unit early. A unit may be
    a lone surrogate, which is what a delete cutting an astral character in half would
    leave — `text_of` gives it back unchanged rather than hiding it, so the campaign
    can see that it happened.
    """
    raw = text.encode("utf-16-le", "surrogatepass")
    return [raw[i:i + 2].decode("utf-16-le", "surrogatepass") for i in range(0, len(raw), 2)]


def text_of(units: Sequence[str]) -> str:
    return b"".join(u.encode("utf-16-le", "surrogatepass")
                    for u in units).decode("utf-16-le", "surrogatepass")


# ---------------------------------------------------------------- the units of a tab

@dataclass(frozen=True, kw_only=True)
class Bullet:
    """The list a paragraph is an item of, and how deep in it."""
    list_id: str
    level: int


@dataclass(frozen=True, kw_only=True)
class Para:
    """A paragraph's own properties, held by the mark that ends it.

    Shared, never copied: a paragraph split off another starts with the very same
    `Para`, and a request that restyles one gives its mark a new one. Written in place,
    as the dicts this used to be were, one `updateParagraphStyle` on one block set the
    line spacing of every paragraph ever split from the same ancestor — and appending a
    block writes "\\ntext", which is a split — while the next request to clear a measure
    cleared it everywhere too, which is what made it invisible: the document stayed
    self-consistent, so nothing but a judge asking "did the source's restyle arrive
    *here*?" could see it (offline seed 110149).

    `measures` holds the properties of `doc_merge.PARAGRAPH_FIELDS` by their IR key,
    absent meaning inherited rather than zero (`doc_ir._paragraph_measures`), and is
    never written once it is here. `align` is the API's word (`START`, `CENTER`, ...),
    None when the paragraph sets none of its own, which is not the same as START: it is
    whatever the document's named style says, and saying it out loud is the only way
    this world can hold a theme (`fuzz_docs.THEME`).
    """
    named: str
    align: str | None
    bullet: Bullet | None
    measures: Measures


@dataclass(frozen=True, kw_only=True)
class Char:
    """One UTF-16 code unit of text, and the style it wears (never written in place)."""
    unit: str
    style: Style


@dataclass(frozen=True, kw_only=True)
class ParaMark:
    """A paragraph mark: the newline that ends a paragraph, and where its style lives."""
    style: Style
    para: Para


@dataclass(frozen=True, kw_only=True)
class Picture:
    """An inline picture. `size_pt` is what `insertInlineImage` or the corpus asked for."""
    id: str
    uri: str
    alt: str
    size_pt: Sequence[float] | None


@dataclass(frozen=True, kw_only=True)
class Person:
    email: str
    name: str


@dataclass(frozen=True, kw_only=True)
class DateChip:
    timestamp: str
    display: str


@dataclass(frozen=True, kw_only=True)
class Equation:
    """Several index units (`EQUATION_SIZE`) that no request may cut into."""
    latex: str


@dataclass(frozen=True, kw_only=True)
class OtherChip:
    """A chip the world only holds a place for: a rich link, a dropdown, a footnote."""
    kind: str


@dataclass(frozen=True, kw_only=True)
class Grid:
    """A table. Its rows are the one place units hold units, and the lists a table
    request splices: frozen as a unit, but its cells are written like a body."""
    rows: list[list[list[Unit]]]


@dataclass(frozen=True, kw_only=True)
class Toc:
    """A table of contents: generated content no request can make (docs/google-docs.md)."""
    size: int


Chip = Picture | Person | DateChip | Equation | OtherChip
Unit = Char | ParaMark | Chip | Grid | Toc


@dataclass(frozen=True, kw_only=True)
class Ranged:
    """One named range of a tab, `[start, end)` in document indices."""
    id: str
    name: str
    start: int
    end: int


def plain() -> Para:
    return Para(named="NORMAL_TEXT", align=None, bullet=None, measures={})


def new_mark() -> ParaMark:
    """The mark of an empty paragraph with nothing set on it."""
    return ParaMark(style={}, para=plain())


def unit_size(u: Unit) -> int:
    if isinstance(u, (Char, ParaMark)):
        return 1
    if isinstance(u, Grid):
        return 1 + sum(1 + sum(1 + size(cell) for cell in row) for row in u.rows)
    if isinstance(u, Toc):
        return u.size
    if isinstance(u, Equation):
        return EQUATION_SIZE
    return 1


def size(units: Sequence[Unit]) -> int:
    return sum(unit_size(u) for u in units)


def _row_size(row: Sequence[Sequence[Unit]]) -> int:
    """What a table row costs in document indices: the row itself, then each cell."""
    return 1 + sum(1 + size(cell) for cell in row)


def _row_start(grid: Grid, at: int, index: int) -> int:
    """Where a row begins: the table's own unit, then every row in front of it."""
    return at + 1 + sum(_row_size(row) for row in grid.rows[:index])


def _cell_start(grid: Grid, at: int, row: int, column: int) -> int:
    """Where a cell begins: its row, then every cell in front of it in that row."""
    return (_row_start(grid, at, row) + 1
            + sum(1 + size(cell) for cell in grid.rows[row][:column]))


def text_units(text: str, style: Style) -> list[Char]:
    return [Char(unit=c, style=style) for c in utf16_units(text)]


def _copied(units: Sequence[Unit]) -> list[Unit]:
    """A unit list the copy owns: every list a request may splice is new, every unit
    the world's own (they never change, so sharing them is safe)."""
    return [Grid(rows=[[_copied(cell) for cell in row] for row in u.rows])
            if isinstance(u, Grid) else u for u in units]


# ---------------------------------------------------------------- finding an index

def locate(units: list[Unit], start: int, index: int) -> tuple[list[Unit], int] | None:
    """The unit list that holds document index `index`, and the offset in it.

    None when the index names no place at all: a row's or a cell's own unit, or a spot
    inside an equation. A table's own index *is* a place — a delete may start or end
    there, which is how a whole table goes — but nothing may be inserted at it
    (measured, docs/google-docs.md), and the writers check that for themselves.
    """
    at = start
    for i, u in enumerate(units):
        n = unit_size(u)
        if index == at:
            return units, i
        if at < index < at + n:
            if isinstance(u, Grid):
                return _in_table(u, at, index)
            return None            # inside an equation, or inside a TOC
        at += n
    return (units, len(units)) if index == at else None


def _in_table(t: Grid, start: int, index: int) -> tuple[list[Unit], int] | None:
    at = start + 1                                   # past the table's own unit
    for row in t.rows:
        at += 1                                      # the row's own unit
        for cell in row:
            at += 1                                  # the cell's own unit
            n = size(cell)
            if at <= index <= at + n:
                return locate(cell, at, index)
            at += n
    return None


def splits_a_pair(cont: Sequence[Unit], offset: int) -> bool:
    """Whether an index falls *between* the two code units of an astral character.

    A judgement, not a measurement: `documents.get` answers in JSON, so a document
    holding half a surrogate pair could not be read back at all, and no cursor can be
    put inside one. Google therefore does something here — refuse, or move the index
    — and refusing is the half that cannot corrupt. It matters because the alternative
    is silent: the world would go on holding a lone surrogate, every judge would be
    handed a document no reader could have made, and the crash would surface far away
    (`doc_ir.utf16_len`, which encodes strictly, as the API's own JSON does).
    """
    if offset >= len(cont):
        return False
    u = cont[offset]
    return isinstance(u, Char) and 0xDC00 <= ord(u.unit) < 0xE000


def writable_spot(units: list[Unit], index: int,
                  request: Mapping[str, object]) -> tuple[list[Unit], int]:
    """Where a request may write text, or `Refused`.

    Nothing can be inserted at a table's own index (measured), nor at a row's or a
    cell's, nor past the segment's last paragraph mark, nor inside an equation, nor
    between the two halves of an astral character (`splits_a_pair`).
    """
    spot = locate(units, 1, index)
    if spot is None or index < 1:
        raise Refused(request, f"index {index} is not inside a paragraph")
    cont, offset = spot
    if splits_a_pair(cont, offset):
        raise Refused(request, f"index {index} is inside an astral character")
    if offset == len(cont):
        raise Refused(request, f"index {index} is past the segment's last paragraph mark")
    here = cont[offset]
    if isinstance(here, (Grid, Toc)):
        what = "a table of contents" if isinstance(here, Toc) else "a table"
        raise Refused(request, f"index {index} is {what}'s own index: nothing can be "
                               f"inserted there")
    return cont, offset


def containers(units: list[Unit], start: int) -> Iterator[tuple[list[Unit], int]]:
    """(unit list, its first index) for a body and for every table cell inside it."""
    yield units, start
    at = start
    for u in units:
        if isinstance(u, Grid):
            inner = at + 1
            for row in u.rows:
                inner += 1
                for cell in row:
                    inner += 1
                    yield from containers(cell, inner)
                    inner += size(cell)
        at += unit_size(u)


@dataclass(frozen=True, kw_only=True)
class ParagraphAt:
    """One paragraph where it stands: its container, the offsets of its first unit and
    of its mark there, the document indices of both, and the mark itself."""
    units: list[Unit]
    low: int
    offset: int
    first: int
    at: int
    mark: ParaMark


def paragraphs(units: list[Unit], start: int) -> Iterator[ParagraphAt]:
    """Every paragraph, in the body and in every cell."""
    for cont, at in containers(units, start):
        low, index = 0, at
        for i, u in enumerate(cont):
            index += unit_size(u)
            if isinstance(u, ParaMark):
                yield ParagraphAt(units=cont, low=low, offset=i,
                                  first=index - 1 - _run_len(_own(cont[low:i])),
                                  at=index - 1, mark=u)
                low = i + 1


def _own(run: Sequence[Unit]) -> Sequence[Unit]:
    """The units of a paragraph's own run: whatever follows the last structural
    element in it.

    A table or a table of contents *ends* the paragraph in front of it, so the
    paragraph after one begins where it ends, and `documents.get` says so - the
    named range this world plants on such a block starts after the table. Counting
    the table into the run put the paragraph's first index back at the table's own
    start, so `_paragraphs` answered a range aimed at a **cell** with the paragraph
    standing after the whole table: a source restyle of one cell took the alignment
    a reader had given that paragraph, and `createParagraphBullets` inside a cell
    would have bulleted it. Nothing had ever styled a cell until `read_cell_style`
    and `src_restyle_cell` were drawn, which is why the world could be wrong here
    for as long as it liked.
    """
    for i in range(len(run) - 1, -1, -1):
        if isinstance(run[i], (Grid, Toc)):
            return run[i + 1:]
    return run


def _run_len(units: Sequence[Unit]) -> int:
    return sum(unit_size(u) for u in units)


# ---------------------------------------------------------------- the document

class Tab:
    """One tab: its units, its named ranges, its lists. The first tab is the body."""

    def __init__(self, tab_id: str, title: str, parent: str | None,
                 units: list[Unit]) -> None:
        self.id, self.title, self.parent = tab_id, title, parent
        self.units = units
        self.named: list[Ranged] = []
        # listId -> whether it is numbered; None is the list an import built, which
        # cannot say (`_levels`).
        self.lists: dict[str, bool | None] = {}

    def copied(self) -> Tab:
        """A copy a batch can be applied to and thrown away (`World.apply`)."""
        out = Tab(self.id, self.title, self.parent, _copied(self.units))
        out.named = list(self.named)
        out.lists = dict(self.lists)
        return out

    def size(self) -> int:
        return size(self.units)

    def end(self) -> int:
        return 1 + self.size()


class World:
    """A whole document: tabs, named ranges, inline objects and a revision id."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.tabs: list[Tab] = [Tab("t.0", "", None, [new_mark()])]
        # The document's theme: `namedStyleType` -> what that style says, which is
        # what a paragraph setting nothing of its own shows. No request can write one
        # (there is none in the API), so nothing here ever changes it — it is here so
        # that a paragraph *inheriting* a property can be represented at all, which is
        # the one thing a document's look is made of. Empty for most corpus shapes.
        self.theme: dict[str, DocsNamedStyle] = {}
        self.revision = 1
        # A plain counter, not itertools.count: a world is copied all the time (the
        # campaign tries a second sync on a copy), and copying an iterator is deprecated.
        self._ids = 0

    def copied(self) -> World:
        """A world of its own, as `copy.deepcopy` would make it, at the cost of its
        lists: the units are shared, since nothing ever changes one."""
        out = World(self.title)
        out.tabs = [t.copied() for t in self.tabs]
        out.theme = dict(self.theme)
        out.revision, out._ids = self.revision, self._ids
        return out

    # -- building

    def tab(self, tab_id: str | None) -> Tab:
        if tab_id is None:
            return self.tabs[0]
        for t in self.tabs:
            if t.id == tab_id:
                return t
        raise Refused({"tabId": tab_id}, f"no tab {tab_id}")

    def fresh(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids}"

    # -- reading

    def read(self) -> Document:
        """The document as `documents.get(includeTabsContent=True)` reports it.

        With the flag the `body` key disappears and `tabs` takes its place, nested by
        parent — measured, and the reason every read in the sync passes the flag.
        """
        by_parent: dict[str | None, list[Tab]] = {}
        for t in self.tabs:
            by_parent.setdefault(t.parent, []).append(t)

        def branch(t: Tab, index: int) -> DocsTab:
            props: DocsTabProperties = {"tabId": t.id, "title": t.title, "index": index}
            if t.parent:
                props["parentTabId"] = t.parent
            return {"tabProperties": props, "documentTab": self._tab_json(t),
                    "childTabs": [branch(c, i) for i, c in enumerate(by_parent.get(t.id, []))]}

        return {"title": self.title, "revisionId": f"r{self.revision}",
                "tabs": [branch(t, i) for i, t in enumerate(by_parent.get(None, []))]}

    def _tab_json(self, t: Tab) -> DocsDocumentTab:
        objects: dict[str, DocsInlineObject] = {}
        content: list[DocsStructuralElement] = [
            {"startIndex": 0, "endIndex": 1, "sectionBreak": {"sectionStyle": {}}}]
        content += _content_json(t.units, 1, objects)
        named: dict[str, DocsNamedRanges] = {}
        for ranged in t.named:
            entry = named.setdefault(ranged.name, {"name": ranged.name, "namedRanges": []})
            entry.setdefault("namedRanges", []).append(
                {"namedRangeId": ranged.id, "name": ranged.name,
                 "ranges": [{"startIndex": ranged.start, "endIndex": ranged.end}]})
        lists: dict[str, DocsList] = {lid: {"listProperties": {"nestingLevels": _levels(ordered)}}
                                      for lid, ordered in t.lists.items()}
        out: DocsDocumentTab = {"body": {"content": content}, "lists": lists,
                                "inlineObjects": objects, "namedRanges": named}
        if self.theme:
            styles: list[DocsNamedStyle] = []
            for name, style in self.theme.items():
                said: DocsNamedStyle = {"namedStyleType": name}
                said.update(style)
                styles.append(said)
            out["namedStyles"] = {"styles": styles}
        return out

    def latex(self) -> dict[tuple[str | None, int], str]:
        """Every equation's LaTeX, by (tab, start) — what `doc_ir.latex_of` digs out of
        the Markdown export on a live document, since `documents.get` says `{}`."""
        out: dict[tuple[str | None, int], str] = {}
        for t in self.tabs:
            for cont, at in containers(t.units, 1):
                index = at
                for u in cont:
                    if isinstance(u, Equation):
                        out[(None if t is self.tabs[0] else t.id, index)] = u.latex
                    index += unit_size(u)
        return out

    # -- writing

    def apply(self, requests: Sequence[DocsRequest]) -> DocsBatchUpdateResponse:
        """One batch, all or nothing.

        Every request is a `DocsRequest`, whether a sync planned it or a reader in
        `fuzz_docs` sent it as a browser would: what the world has no handler for is
        refused, as Google refuses it.

        Google applies a batch as a transaction: one request it refuses throws out
        every other one with it. A sync that plans a request the API will not take
        therefore writes *nothing* and dies, which is exactly the failure the Slides
        campaign could not see until its applier started consuming requests.
        """
        spare = [t.copied() for t in self.tabs]
        replies: list[JsonObject] = []
        try:
            for request in requests:
                replies.append(self._one(request) or {})
        except Refused:
            self.tabs = spare
            raise
        self.revision += 1
        return {"replies": replies}

    def _one(self, request: DocsRequest) -> JsonObject | None:
        """The one request a `Request` holds, handed to its handler."""
        if (insert := request.get("insertText")) is not None:
            return self._do_insertText(insert, request)
        if (delete := request.get("deleteContentRange")) is not None:
            return self._do_deleteContentRange(delete, request)
        if (text := request.get("updateTextStyle")) is not None:
            return self._do_updateTextStyle(text, request)
        if (paragraph := request.get("updateParagraphStyle")) is not None:
            return self._do_updateParagraphStyle(paragraph, request)
        if (bullets := request.get("createParagraphBullets")) is not None:
            return self._do_createParagraphBullets(bullets, request)
        if (unbullets := request.get("deleteParagraphBullets")) is not None:
            return self._do_deleteParagraphBullets(unbullets, request)
        if (create := request.get("createNamedRange")) is not None:
            return self._do_createNamedRange(create, request)
        if (unname := request.get("deleteNamedRange")) is not None:
            return self._do_deleteNamedRange(unname, request)
        if (table := request.get("insertTable")) is not None:
            return self._do_insertTable(table, request)
        if (row := request.get("insertTableRow")) is not None:
            return self._do_insertTableRow(row, request)
        if (column := request.get("insertTableColumn")) is not None:
            return self._do_insertTableColumn(column, request)
        if (unrow := request.get("deleteTableRow")) is not None:
            return self._do_deleteTableRow(unrow, request)
        if (uncolumn := request.get("deleteTableColumn")) is not None:
            return self._do_deleteTableColumn(uncolumn, request)
        if (image := request.get("insertInlineImage")) is not None:
            return self._do_insertInlineImage(image, request)
        if (person := request.get("insertPerson")) is not None:
            return self._do_insertPerson(person, request)
        if (date := request.get("insertDate")) is not None:
            return self._do_insertDate(date, request)
        if (add := request.get("addDocumentTab")) is not None:
            return self._do_addDocumentTab(add, request)
        if (props := request.get("updateDocumentTabProperties")) is not None:
            return self._do_updateDocumentTabProperties(props, request)
        if (drop := request.get("deleteTab")) is not None:
            return self._do_deleteTab(drop, request)
        raise Refused(request, "no such request in the v1 API")

    # -- text

    def _do_insertText(self, arg: InsertTextRequest, request: DocsRequest) -> None:
        location = _location(arg.get("location"), "insertText")
        tab = self.tab(location.get("tabId"))
        self._insert(tab, location["index"], arg["text"], request)

    def _insert(self, tab: Tab, index: int, text: str, request: DocsRequest) -> None:
        cont, offset = writable_spot(tab.units, index, request)
        style = _style_in_front(cont, offset)
        para = _paragraph_at(cont, offset)
        new: list[Unit] = []
        for i, piece in enumerate(text.split("\n")):
            if i:
                new.append(ParaMark(style=style, para=para))
            new += text_units(piece, style)
        cont[offset:offset] = new
        self._shift(tab, index, len(new), None)

    def _do_deleteContentRange(self, arg: DeleteContentRangeRequest,
                               request: DocsRequest) -> None:
        span = arg["range"]
        tab = self.tab(span.get("tabId"))
        start, end = span["startIndex"], span["endIndex"]
        if end <= start or start < 1:
            raise Refused(request, f"the range [{start}, {end}) is empty or before the body")
        if end > tab.end():
            raise Refused(request, f"the end index ({end}) is past the body ({tab.end()})")
        low, high = locate(tab.units, 1, start), locate(tab.units, 1, end)
        if low is None or high is None or low[0] is not high[0]:
            raise Refused(request, f"the range [{start}, {end}) is not one run of text")
        cont, i = low
        j = high[1]
        if splits_a_pair(cont, i) or splits_a_pair(cont, j):
            raise Refused(request, f"the range [{start}, {end}) ends inside an astral "
                                   f"character")
        if j >= len(cont):
            raise Refused(request, "Invalid deletion range: the segment's last paragraph "
                                   "mark cannot be deleted")
        for k in range(i, j):
            if isinstance(cont[k], ParaMark) and k + 1 < len(cont) and k + 1 >= j:
                after = cont[k + 1]
                if isinstance(after, (Grid, Toc)):
                    what = "a table of contents" if isinstance(after, Toc) else "a table"
                    raise Refused(request, f"Invalid deletion range: the newline in front "
                                           f"of {what} cannot be deleted")
        # Docs merges the two paragraphs keeping the *first* one's style: the mark that
        # survives takes the paragraph style of the first one deleted. That is the rule
        # `doc_merge._delete_range` leans on when a block gives up its neighbour's mark.
        gone = next((u.para for u in cont[i:j] if isinstance(u, ParaMark)), None)
        del cont[i:j]
        if gone is not None:
            for k in range(i, len(cont)):
                survivor = cont[k]
                if isinstance(survivor, ParaMark):
                    cont[k] = ParaMark(style=survivor.style, para=gone)
                    break
        self._shift(tab, start, -(end - start), end)

    # -- objects

    def _do_insertInlineImage(self, arg: InsertInlineImageRequest,
                              request: DocsRequest) -> None:
        size_pt: tuple[float, float] | None = None
        if dimensions := arg.get("objectSize"):
            size_pt = (_magnitude(dimensions.get("width")),
                       _magnitude(dimensions.get("height")))
        self._object(_location(arg.get("location"), "insertInlineImage"),
                     Picture(id=self.fresh("kix.i"), uri=arg["uri"], alt="", size_pt=size_pt),
                     request)

    def _do_insertPerson(self, arg: InsertPersonRequest, request: DocsRequest) -> None:
        email = arg["personProperties"].get("email")
        if email is None:
            raise ValueError("an insertPerson with no email: the world makes a chip of one")
        self._object(arg["location"], Person(email=email, name=email.split("@")[0]), request)

    def _do_insertDate(self, arg: InsertDateRequest, request: DocsRequest) -> None:
        stamp = arg["dateElementProperties"].get("timestamp")
        if stamp is None:
            raise ValueError("an insertDate with no timestamp: the world makes a chip of one")
        self._object(arg["location"], DateChip(timestamp=stamp, display=stamp[:10]), request)

    def _object(self, location: DocsLocation, unit: Chip, request: DocsRequest) -> None:
        tab = self.tab(location.get("tabId"))
        index = location["index"]
        cont, offset = writable_spot(tab.units, index, request)
        cont.insert(offset, unit)
        self._shift(tab, index, unit_size(unit), None)

    # -- styling

    def _do_updateTextStyle(self, arg: UpdateTextStyleRequest, request: DocsRequest) -> None:
        span = arg["range"]
        tab = self.tab(span.get("tabId"))
        # A field the world does not know is left alone: a request can name only the
        # fields of `API_TO_IR`, which the merge writes and a reader's menus set.
        fields = [field for f in arg["fields"].split(",")
                  if (field := _TEXT_FIELD.get(f.strip())) is not None]
        given = arg["textStyle"]
        read = _ir_style(given)
        for cont, k in _chars(tab, span["startIndex"], span["endIndex"]):
            u = cont[k]
            if not isinstance(u, (Char, ParaMark)):
                continue
            style: Style = {**u.style}
            for field in fields:
                _restyle(style, field, given, read)
            cont[k] = (Char(unit=u.unit, style=style) if isinstance(u, Char)
                       else ParaMark(style=style, para=u.para))

    def _do_updateParagraphStyle(self, arg: UpdateParagraphStyleRequest,
                                 request: DocsRequest) -> None:
        style = arg["paragraphStyle"]
        fields = {f.strip() for f in arg["fields"].split(",") if f.strip()}
        # Every property as the IR spells it, rounded as a read rounds it, so a value
        # written and then read back is the same number and the merge sees no change
        # nobody made.
        said = doc_ir._paragraph_measures(style)
        for found in self._paragraphs(arg["range"]):
            para = found.mark.para
            named, align = para.named, para.align
            if "namedStyleType" in fields and (asked := style.get("namedStyleType")):
                named = asked
            if "alignment" in fields:
                # Named with no value: back to what the paragraph inherits, which is
                # None here — not START. The difference is the whole of a theme.
                align = style.get("alignment")
            # A field the merge names without a value means "back to the default"
            # (`doc_merge.paragraph_style`), which is how a property the source
            # dropped goes away. Naming it and not applying that would make the
            # merge write it again for ever.
            measures: Measures = {**para.measures}
            for key, api in doc_merge.PARAGRAPH_FIELDS:
                if api not in fields:
                    continue
                value = doc_ir.read_measure(said, key) if api in style else None
                if value is None:
                    doc_ir.pop_measure(measures, key)
                else:
                    doc_ir.set_measure(measures, key, value)
            found.units[found.offset] = ParaMark(
                style=found.mark.style,
                para=Para(named=named, align=align, bullet=para.bullet, measures=measures))

    def _do_createParagraphBullets(self, arg: CreateParagraphBulletsRequest,
                                   request: DocsRequest) -> None:
        tab = self.tab(arg["range"].get("tabId"))
        ordered = arg["bulletPreset"] == ORDERED_PRESET
        fresh: str | None = None
        for found in self._paragraphs(arg["range"]):
            para = found.mark.para
            if para.bullet is not None:
                # Measured: over a list the importer built, this keeps every item's
                # nesting level and gives the list glyphs it can report from then on.
                tab.lists[para.bullet.list_id] = ordered
                continue
            if fresh is None:
                fresh = self.fresh("kix.l")
                tab.lists[fresh] = ordered
            _repara(found, Para(named=para.named, align=para.align,
                                bullet=Bullet(list_id=fresh, level=0), measures=para.measures))

    def _do_deleteParagraphBullets(self, arg: DeleteParagraphBulletsRequest,
                                   request: DocsRequest) -> None:
        for found in self._paragraphs(arg["range"]):
            para = found.mark.para
            _repara(found, Para(named=para.named, align=para.align, bullet=None,
                                measures=para.measures))

    def _paragraphs(self, span: DocsRangeWrite) -> Iterator[ParagraphAt]:
        """Every paragraph the range touches."""
        tab = self.tab(span.get("tabId"))
        start, end = span["startIndex"], span["endIndex"]
        for found in paragraphs(tab.units, 1):
            if found.first < end and start <= found.at:
                yield found

    # -- tables

    def _do_insertTable(self, arg: InsertTableRequest, request: DocsRequest) -> None:
        rows, columns = arg["rows"], arg["columns"]
        if rows < 1 or columns < 1:
            raise Refused(request, "a table needs a row and a column")
        grid = Grid(rows=[[[new_mark()] for _ in range(columns)] for _ in range(rows)])
        if (at_end := arg.get("endOfSegmentLocation")) is not None:
            tab = self.tab(at_end.get("tabId"))
            # Measured: a table written after everything keeps a paragraph after it,
            # because a document ends on one.
            tab.units += [grid, new_mark()]
            return
        location = _location(arg.get("location"), "insertTable")
        tab = self.tab(location.get("tabId"))
        index = location["index"]
        cont, offset = writable_spot(tab.units, index, request)
        # `insertTable` splits the paragraph its index is in: what was before the index
        # stays a paragraph of its own, then comes the table, then the rest (measured).
        para = _paragraph_at(cont, offset)
        cont[offset:offset] = [ParaMark(style=_style_in_front(cont, offset), para=para), grid]
        self._shift(tab, index, 1 + unit_size(grid), None)

    def _do_insertTableRow(self, arg: InsertTableRowRequest, request: DocsRequest) -> None:
        # A row is written at its own index, not the table's: a named range on a cell
        # of a row *above* the new one does not move, and one below does. Shifting the
        # whole table by the row's size, which this did, moved every anchor in it.
        cell = arg["tableCellLocation"]
        tab, grid, at = self._grid(cell, request)
        row = cell.get("rowIndex", 0) + (1 if arg.get("insertBelow") else 0)
        start = _row_start(grid, at, row)
        grid.rows.insert(row, [[new_mark()] for _ in grid.rows[0]])
        self._shift(tab, start, _row_size(grid.rows[row]), None)

    def _do_insertTableColumn(self, arg: InsertTableColumnRequest,
                              request: DocsRequest) -> None:
        cell = arg["tableCellLocation"]
        tab, grid, at = self._grid(cell, request)
        column = cell.get("columnIndex", 0) + (1 if arg.get("insertRight") else 0)
        # One cell per row, back to front: a cell written low down leaves the indices
        # above it alone, so each `_cell_start` is still the one the grid has.
        for r in reversed(range(len(grid.rows))):
            start = _cell_start(grid, at, r, column)
            grid.rows[r].insert(column, [new_mark()])
            self._shift(tab, start, 1 + size(grid.rows[r][column]), None)

    def _do_deleteTableRow(self, arg: DeleteTableLineRequest, request: DocsRequest) -> None:
        """A row's content goes out of the document, so everything after it moves up.
        Shifting by 0, which this did, left every anchor below the table one row's
        worth of units too high: the keys below a table the source regridded all slid
        onto the block above (chain-8 seeds 5099, 5167). Google moves them."""
        cell = arg["tableCellLocation"]
        tab, grid, at = self._grid(cell, request)
        if len(grid.rows) <= 1:
            raise Refused(request, "a table's last row cannot be deleted")
        row = cell.get("rowIndex", 0)
        start = _row_start(grid, at, row)
        self._shift(tab, start, -_row_size(grid.rows[row]), None)
        grid.rows.pop(row)

    def _do_deleteTableColumn(self, arg: DeleteTableLineRequest,
                              request: DocsRequest) -> None:
        cell = arg["tableCellLocation"]
        tab, grid, at = self._grid(cell, request)
        if len(grid.rows[0]) <= 1:
            raise Refused(request, "a table's last column cannot be deleted")
        column = cell.get("columnIndex", 0)
        for r in reversed(range(len(grid.rows))):
            start = _cell_start(grid, at, r, column)
            self._shift(tab, start, -(1 + size(grid.rows[r][column])), None)
            grid.rows[r].pop(column)

    def _grid(self, where: DocsTableCellLocation,
              request: DocsRequest) -> tuple[Tab, Grid, int]:
        tab = self.tab(where["tableStartLocation"].get("tabId"))
        at = where["tableStartLocation"]["index"]
        index = 1
        for u in tab.units:
            if index == at and isinstance(u, Grid):
                return tab, u, at
            index += unit_size(u)
        raise Refused(request, f"no table starts at {at}")

    # -- named ranges

    def _do_createNamedRange(self, arg: CreateNamedRangeRequest,
                             request: DocsRequest) -> JsonObject:
        span = arg["range"]
        tab = self.tab(span.get("tabId"))
        start, end = span["startIndex"], span["endIndex"]
        if end <= start or start < 1 or end > tab.end():
            raise Refused(request, f"the range [{start}, {end}) is not in the body")
        ident = self.fresh("nr.")
        tab.named.append(Ranged(id=ident, name=arg["name"], start=start, end=end))
        return {"createNamedRange": {"namedRangeId": ident}}

    def _do_deleteNamedRange(self, arg: DeleteNamedRangeRequest,
                             request: DocsRequest) -> None:
        """By id, or every range of a name: the two the API takes, one of them required.
        Which tabs it reaches is `tabsCriteria` where the request gives one. Where it
        does not, a *name* goes to every tab (the reference; unmeasured, and nothing
        here sends one) and an **id** to the first tab and no other - measured, against
        the reference, which says an omitted criteria applies to every tab: the live API
        answers "No named range with ID" for a range on a second tab whose id had just
        been read back from that tab (`doc_merge.on_tab`). A refusal throws out the
        whole batch, so that is the rule a plan has to be written for."""
        ident, name = arg.get("namedRangeId"), arg.get("name")
        if not ident and not name:
            raise Refused(request, "deleteNamedRange needs a namedRangeId or a name")
        criteria = arg.get("tabsCriteria")
        wanted = set(criteria.get("tabIds", [])) if criteria else set()
        tabs = ([t for t in self.tabs if t.id in wanted] if wanted
                else self.tabs[:1] if ident else self.tabs)
        hit = False
        for tab in tabs:
            kept = [r for r in tab.named
                    if not (r.id == ident if ident else r.name == name)]
            hit = hit or len(kept) != len(tab.named)
            tab.named = kept
        if not hit:
            raise Refused(request, f"no named range {ident or name!r} to delete")

    # -- tabs

    def _do_addDocumentTab(self, arg: AddDocumentTabRequest, request: DocsRequest) -> JsonObject:
        """A new tab, at the index it asks for among its parent's ("when a tab is
        added at a given index, all subsequent tabs' indexes are incremented" — the
        discovery document). No index means the end, which is where one lands.

        `self.tabs` is flat and `read` groups it by parent keeping this order, so
        the new tab goes in front of the sibling whose place it takes.

        The refusal at root index 0 is a guard, not a measurement: the first tab is
        the body, it cannot be deleted, and a tab in front of it would be a body
        nothing has ever written to. `doc_merge.tab_index` never asks for it — that
        is what this makes sure of.
        """
        props = arg["tabProperties"]
        parent = props.get("parentTabId")
        ident = self.fresh("t.")
        siblings = [t for t in self.tabs if t.parent == parent]
        index = props.get("index")
        if parent is None and index == 0:
            raise Refused(request, "no tab can go in front of the first tab")
        fresh = Tab(ident, props.get("title", ""), parent, [new_mark()])
        if index is None or index >= len(siblings):
            self.tabs.append(fresh)
        else:
            self.tabs.insert(self.tabs.index(siblings[index]), fresh)
        return {"addDocumentTab": {"tabProperties": {"tabId": ident,
                                                     "title": props.get("title", "")}}}

    def _do_updateDocumentTabProperties(self, arg: UpdateDocumentTabPropertiesRequest,
                                        request: DocsRequest) -> None:
        props = arg["tabProperties"]
        ident = props.get("tabId")
        if ident is None:
            raise ValueError("an updateDocumentTabProperties that names no tab")
        self.tab(ident).title = props.get("title", "")

    def _do_deleteTab(self, arg: DeleteTabRequest, request: DocsRequest) -> None:
        ident = arg["tabId"]
        if self.tabs[0].id == ident:
            raise Refused(request, "the first tab cannot be deleted")
        self.tabs = [t for t in self.tabs if t.id != ident]

    # -- named ranges follow the text

    def _shift(self, tab: Tab, index: int, delta: int, end: int | None) -> None:
        """Google keeps every named range up to date as the text moves (measured).

        Half-open `[start, end)`: text typed at an anchor's first index falls outside
        it and pushes it along, text typed inside grows it, and an anchor whose text is
        all deleted disappears — which is the signal `doc_merge` reads as a block the
        reader deleted. `end` is where a delete ends, when it is not `index - delta`.
        """
        alive: list[Ranged] = []
        for ranged in tab.named:
            low, high = ranged.start, ranged.end
            if delta >= 0:
                if index <= low:
                    low, high = low + delta, high + delta
                elif index < high:
                    high += delta
            else:
                cut_low, cut_high = index, end if end is not None else index - delta
                low = low - max(0, min(low, cut_high) - cut_low)
                high = high - max(0, min(high, cut_high) - cut_low)
            if high > low:
                alive.append(ranged if (low, high) == (ranged.start, ranged.end)
                             else Ranged(id=ranged.id, name=ranged.name, start=low, end=high))
        tab.named = alive


def _location(location: DocsLocation | None, what: str) -> DocsLocation:
    """The index a request writes at. The API also takes the end of a segment for
    these; nothing the world is sent says that, so a request that does is not modelled."""
    if location is None:
        raise ValueError(f"an {what} with no location: the world writes at an index only")
    return location


def _magnitude(dimension: DocsDimension | None) -> float:
    if dimension is None or (magnitude := dimension.get("magnitude")) is None:
        raise ValueError("an objectSize without both its magnitudes")
    return magnitude


def _chars(tab: Tab, start: int, end: int) -> Iterator[tuple[list[Unit], int]]:
    """Where every character and mark in `[start, end)` stands: its container and offset."""
    for cont, at in containers(tab.units, 1):
        index = at
        for k, u in enumerate(cont):
            if start <= index < end and isinstance(u, (Char, ParaMark)):
                yield cont, k
            index += unit_size(u)


def _repara(found: ParagraphAt, para: Para) -> None:
    """Give a paragraph new properties: a new mark where its mark stood."""
    found.units[found.offset] = ParaMark(style=found.mark.style, para=para)


# ---------------------------------------------------------------- style translation

def _style_in_front(cont: Sequence[Unit], offset: int) -> Style:
    """The style Docs gives text typed here: the character in front of it (measured)."""
    if offset:
        before = cont[offset - 1]
        if isinstance(before, (Char, ParaMark)):
            return before.style
    if offset < len(cont):
        here = cont[offset]
        if isinstance(here, (Char, ParaMark)):
            return here.style
    return {}


def _paragraph_at(cont: Sequence[Unit], offset: int) -> Para:
    """The paragraph style at this offset: the mark that terminates it."""
    for u in cont[offset:]:
        if isinstance(u, ParaMark):
            return u.para
    return plain()


def _ir_style(text_style: DocsTextStyle) -> Style:
    """A Docs textStyle as the IR spells it.

    `doc_ir._style_of` itself, never a copy of it. A copy is what this was, and it
    drifted the moment the dialect grew a face and a size: the world went on
    calling four families `code` while the reader had stopped, so every styling
    comparison in the oracle was against a reader nobody uses. The world's whole
    claim is that it reads what `doc_ir` reads, so it has to call it.

    No named style: one is subtracted from what a *document* reports, and the world's
    documents carry no `namedStyles` — nothing here sets a property that only repeats
    one.
    """
    return doc_ir._style_of(text_style, None)


# The `updateTextStyle` fields the merge writes, and the IR key each one sets
# (`doc_merge.MANAGED`). The world applies a request field by field, so a field it
# does not know would be silently ignored and the read-back would never show it.
TextField = Literal["bold", "italic", "underline", "strikethrough", "foregroundColor",
                    "backgroundColor", "link", "weightedFontFamily", "fontSize", "smallCaps",
                    "baselineOffset"]
API_TO_IR: Final[dict[TextField, str]] = {
    "bold": "bold", "italic": "italic", "underline": "underline",
    "strikethrough": "strike", "foregroundColor": "color",
    "backgroundColor": "highlight", "link": "link",
    "weightedFontFamily": "font", "fontSize": "fontsize",
    "smallCaps": "smallcaps", "baselineOffset": "script"}
_TEXT_FIELD: Final[dict[str, TextField]] = {field: field for field in API_TO_IR}
_MARK_OF: Final[dict[MarkApi, Mark]] = {api: key for key, api in doc_ir.MARK_FIELDS}


def _restyle(style: Style, field: TextField, given: DocsTextStyle, read: Style) -> None:
    """What one named field of an `updateTextStyle` does to one character's style.

    A mark named *with* a value is set to it, False included: `_ir_style` reads a
    document, where False against no named style is nothing, but a request means what
    it says. Anything named with no value is taken off, which is the API's "back to
    what you inherit"."""
    match field:
        case "bold" | "italic" | "underline" | "strikethrough" | "smallCaps":
            key = _MARK_OF[field]
            if field in given:
                doc_ir._set_mark(style, key, bool(doc_ir._mark_api(given, field)))
            else:
                _drop_mark(style, key)
        case "foregroundColor":
            if color := read.get("color"):
                style["color"] = color
            else:
                style.pop("color", None)
        case "backgroundColor":
            if highlight := read.get("highlight"):
                style["highlight"] = highlight
            else:
                style.pop("highlight", None)
        case "link":
            if link := read.get("link"):
                style["link"] = link
            else:
                style.pop("link", None)
        case "weightedFontFamily":
            if font := read.get("font"):
                style["font"] = font
            else:
                # A face named with no value puts the paragraph's own back, and the
                # file's older `code` spelling is a face like any other.
                style.pop("font", None)
                style.pop("code", None)
        case "fontSize":
            if fontsize := read.get("fontsize"):
                style["fontsize"] = fontsize
            else:
                style.pop("fontsize", None)
        case "baselineOffset":
            if script := read.get("script"):
                style["script"] = script
            else:
                style.pop("script", None)
        case _:
            assert_never(field)


def _drop_mark(style: Style, key: Mark) -> None:
    match key:
        case "bold":
            style.pop("bold", None)
        case "italic":
            style.pop("italic", None)
        case "underline":
            style.pop("underline", None)
        case "strike":
            style.pop("strike", None)
        case "smallcaps":
            style.pop("smallcaps", None)
        case _:
            assert_never(key)


def _api_measures(measures: Measures) -> DocsParagraphStyle:
    """Every paragraph property of the dialect as `documents.get` reports it."""
    out: DocsParagraphStyle = {}
    # Every read of a paragraph comes through here, so the typed accessor is asked
    # only about what the block sets: the fuzz spends its time in this loop.
    present: Mapping[str, object] = measures
    for key, _ in doc_merge.PARAGRAPH_FIELDS:
        if key in present and (value := doc_ir.measure_of(measures, key)) is not None:
            doc_merge._set_paragraph(out, key, value)
    return out


def _api_style(style: Style) -> DocsTextStyle:
    """The inverse of `_ir_style`: what `documents.get` would report for it."""
    out: DocsTextStyle = {}
    present: Mapping[str, object] = style    # asked only about the marks it has (hot)
    for key, api in doc_ir.MARK_FIELDS:
        # A mark is True, absent — or False, which a reader leaves behind by turning
        # off a mark the named style puts on. `documents.get` reports all three, and
        # collapsing the last two is how a theme's bold would come back unasked.
        if key in present and (said := doc_ir.mark_of(style, key)) is not None:
            doc_merge._set_mark_api(out, api, said)
    # `code` is the file's older spelling for a monospaced face and still means
    # Courier New (`doc_ir.CODE_FAMILY`); an explicit face wins over it.
    family = style.get("font") or (doc_ir.CODE_FAMILY if style.get("code") else None)
    if family:
        out["weightedFontFamily"] = {"fontFamily": family}
    if script := style.get("script"):
        # Not a mark: one of three values, and "none" is the third rather than the
        # absence of the other two (`doc_ir.SCRIPTS`).
        out["baselineOffset"] = SCRIPT_API[script]
    if fontsize := style.get("fontsize"):
        out["fontSize"] = {"magnitude": float(fontsize), "unit": "PT"}
    if color := style.get("color"):
        out["foregroundColor"] = {"color": {"rgbColor": doc_merge._rgb(color)}}
    if highlight := style.get("highlight"):
        out["backgroundColor"] = {"color": {"rgbColor": doc_merge._rgb(highlight)}}
    if link := style.get("link"):
        out["link"] = {"url": link}
    return out


# What `documents.get` says for each raised or lowered run: the world's own table, not
# `doc_ir.TO_SCRIPT`, so a reader that misread one would not be agreed with here.
SCRIPT_API: dict[Script, str] = {"super": "SUPERSCRIPT", "sub": "SUBSCRIPT", "none": "NONE"}


def nest(world: World, span: DocsRangeWrite, level: int) -> bool:
    """Tab and Shift-Tab in a list — the one reader act that is no request at all.

    `createParagraphBullets` says nothing about a nesting level: Docs reads one off
    the paragraph's leading tabs, and there is no field for it anywhere in the v1 API
    (`doc_merge.unwritten_levels` is the whole of what the merge can do about that).
    So a person indenting an item in the browser is a change to the document that no
    batch could have made, and the harness has to reach the world directly rather than
    pretend it sent something. It is kept here, beside the state it changes, so a
    reader op stays a reader op: the campaign's ops write requests, and this one door
    is named as the exception it is.

    Answers whether anything moved, since the caller counts a draw that did nothing.
    """
    moved = False
    for found in world._paragraphs(span):
        para = found.mark.para
        if para.bullet is not None and para.bullet.level != level:
            _repara(found, Para(named=para.named, align=para.align,
                                bullet=Bullet(list_id=para.bullet.list_id, level=level),
                                measures=para.measures))
            moved = True
    return moved


def _levels(ordered: bool | None) -> list[DocsNestingLevel]:
    """A list's nesting levels as `documents.get` reports them.

    `ordered: None` is the list Drive's HTML importer built: every level comes back
    `GLYPH_TYPE_UNSPECIFIED`, for `<ul>` and `<ol>` alike, which is why the file has to
    say which it is (`doc_ir._ordered`, `doc_merge.restore_unreadable`).
    """
    if ordered is None:
        return [{"glyphType": "GLYPH_TYPE_UNSPECIFIED"} for _ in range(9)]
    if ordered:
        return [{"glyphType": g, "glyphFormat": "%0."}
                for g in ("DECIMAL", "ALPHA", "ROMAN") for _ in range(3)]
    return [{"glyphSymbol": g} for g in "●○■" for _ in range(3)]


# ---------------------------------------------------------------- documents.get JSON

def _content_json(units: Sequence[Unit], start: int,
                  objects: dict[str, DocsInlineObject]) -> list[DocsStructuralElement]:
    out: list[DocsStructuralElement] = []
    at, low = start, start
    run: list[Unit] = []
    for u in units:
        if isinstance(u, (Grid, Toc)):
            if run:
                out.append(_paragraph_json(run, low, objects))
                run = []
            n = unit_size(u)
            out.append(_table_json(u, at, objects) if isinstance(u, Grid) else
                       {"startIndex": at, "endIndex": at + n,
                        "tableOfContents": {"content": []}})
            at += n
            low = at
            continue
        run.append(u)
        at += unit_size(u)
        if isinstance(u, ParaMark):
            out.append(_paragraph_json(run, low, objects))
            run = []
            low = at
    if run:
        out.append(_paragraph_json(run, low, objects))
    return out


def _paragraph_json(run: Sequence[Unit], start: int,
                    objects: dict[str, DocsInlineObject]) -> DocsStructuralElement:
    elements: list[DocsParagraphElement] = []
    held: list[str] = []          # code units waiting to become one text run
    held_style: DocsTextStyle = {}
    at = start

    def flush(end: int) -> None:
        nonlocal held, held_style
        if held:
            # `text_of`, not "".join: the two halves of an astral character are separate
            # index units here and only become one Python character again together.
            elements.append({"startIndex": end - len(held), "endIndex": end,
                             "textRun": {"content": text_of(held),
                                         "textStyle": held_style}})
        held = []
        held_style = DocsTextStyle()

    for u in run:
        if isinstance(u, (Char, ParaMark)):
            style = _api_style(u.style)
            if held and style != held_style:
                flush(at)
            held.append(u.unit if isinstance(u, Char) else "\n")
            held_style = style
            at += 1
            continue
        if isinstance(u, (Grid, Toc)):
            raise AssertionError("a table inside a paragraph's run")
        flush(at)
        elements.append(_object_json(u, at, objects))
        at += unit_size(u)
    flush(at)
    last = run[-1] if run else None
    para = last.para if isinstance(last, ParaMark) else plain()
    # A paragraph reports what is set on it, never what it inherits: no `alignment`
    # key at all when it sets none, which is how Docs answers and what lets the
    # reader subtract the named style (`doc_ir._named_defaults`).
    para_style: DocsParagraphStyle = {"namedStyleType": para.named}
    if para.align:
        para_style["alignment"] = para.align
    para_style.update(_api_measures(para.measures))
    paragraph: DocsParagraph = {"elements": elements, "paragraphStyle": para_style}
    if para.bullet is not None:
        paragraph["bullet"] = {"listId": para.bullet.list_id,
                               "nestingLevel": para.bullet.level}
    return {"startIndex": start, "endIndex": at, "paragraph": paragraph}


def _object_json(o: Chip, at: int, objects: dict[str, DocsInlineObject]) -> DocsParagraphElement:
    out: DocsParagraphElement = {"startIndex": at, "endIndex": at + unit_size(o)}
    if isinstance(o, Equation):
        out["equation"] = {}
    elif isinstance(o, Person):
        out["person"] = {"personProperties": {"name": o.name, "email": o.email}}
    elif isinstance(o, DateChip):
        out["dateElement"] = {"dateElementProperties": {
            "displayText": o.display, "timestamp": o.timestamp,
            "dateFormat": "", "locale": ""}}
    elif isinstance(o, Picture):
        embedded: DocsEmbeddedObject = {"imageProperties": {"contentUri": o.uri}}
        if o.size_pt:
            embedded["size"] = {"width": {"magnitude": o.size_pt[0], "unit": "PT"},
                                "height": {"magnitude": o.size_pt[1], "unit": "PT"}}
        if o.alt:
            embedded["description"] = o.alt
        objects[o.id] = {"inlineObjectProperties": {"embeddedObject": embedded}}
        out["inlineObjectElement"] = {"inlineObjectId": o.id}
    elif o.kind == "link":
        out["richLink"] = {"richLinkProperties": {"title": "", "uri": ""}}
    # A dropdown chip: an element with a span and no content key of any kind (measured).
    return out


def _table_json(t: Grid, start: int,
                objects: dict[str, DocsInlineObject]) -> DocsStructuralElement:
    at = start + 1
    rows: list[DocsTableRow] = []
    for row in t.rows:
        row_start = at
        at += 1
        cells: list[DocsTableCell] = []
        for cell in row:
            cell_start = at
            at += 1
            content = _content_json(cell, at, objects)
            at += size(cell)
            cells.append({"startIndex": cell_start, "endIndex": at, "content": content})
        rows.append({"startIndex": row_start, "endIndex": at, "tableCells": cells})
    return {"startIndex": start, "endIndex": at,
            "table": {"rows": len(t.rows),
                      "columns": len(t.rows[0]) if t.rows else 0,
                      "tableRows": rows}}


# ---------------------------------------------------------------- IR -> a world

def build(parts: Sequence[Ir], title: str) -> World:
    """A world from IR-shaped blocks, one `parts` entry per tab: its `blocks`, and its
    `title` and `parent` where it has them.

    A block is what the IR calls one: paragraph / heading / item / table / toc, with
    `runs`. A run with `chip` becomes the object of that kind; everything else becomes
    text.
    """
    world = World(title)
    for i, part in enumerate(parts):
        if i:
            world.tabs.append(Tab(world.fresh("t."), part.get("title", ""),
                                  part.get("parent"), [new_mark()]))
        tab = world.tabs[i] if i < len(world.tabs) else world.tabs[-1]
        tab.title = part.get("title", tab.title)
        tab.units = _units_of(part["blocks"], tab, world)
        if not tab.units or not isinstance(tab.units[-1], ParaMark):
            tab.units.append(new_mark())     # a body ends on a paragraph: the `trailer`
        if isinstance(tab.units[0], (Grid, Toc)):
            # And a table cannot be the first thing in a body: one that is has an empty
            # paragraph in front of it that no request can delete (`doc_ir._hide_trailer`
            # calls it the `lead`). A corpus without it would put the merge at index 0.
            tab.units.insert(0, new_mark())
    return world


def _units_of(blocks: Sequence[Block], tab: Tab, world: World) -> list[Unit]:
    out: list[Unit] = []
    for block in blocks:
        if block["kind"] == "table":
            out.append(Grid(rows=[[_units_of(cell, tab, world) or [new_mark()] for cell in row]
                                  for row in block.get("rows", [])]))
            continue
        if block["kind"] == "toc":
            out.append(Toc(size=TOC_SIZE))
            continue
        align = block.get("align")
        measures: Measures = {}
        for key, _ in doc_merge.PARAGRAPH_FIELDS:
            if (value := doc_ir.measure_of(block, key)) is not None:
                doc_ir.set_measure(measures, key, value)
        indent, indent_first = block.get("indent"), block.get("indent_first")
        if indent is not None or indent_first is not None:
            # A measure is kept as `documents.get` says it, so the first line is from the
            # page margin: `margin-left` plus a `text-indent`, a negative one dropped
            # (measured 2026-09-24: 36 + 18 imports as 54, 36 - 18 as 36).
            measures.pop("indent_first", None)
            first = (indent or 0.0) + max(indent_first or 0.0, 0.0)
            if first:
                measures["indent_first"] = first
        bullet: Bullet | None = None
        if block["kind"] == "item":
            lid = block.get("list") or "kix.imported"
            bullet = Bullet(list_id=lid, level=block.get("level", 0))
            # An imported list cannot say whether it is numbered: that is the state the
            # merge has to cope with, so it is the corpus's default.
            tab.lists.setdefault(lid, _glyphs(block))
        para = Para(named="NORMAL_TEXT" if block["kind"] == "item"
                    else doc_merge.named_style(block),
                    align=doc_ir.TO_ALIGNMENT[align] if align else None,
                    bullet=bullet, measures=measures)
        for run in block.get("runs", []):
            if chip := run.get("chip"):
                out.append(_object_unit(chip, run, world))
            else:
                out += text_units(run["text"], _run_style(run))
        out.append(ParaMark(style={}, para=para))
    return out


def _run_style(run: Run) -> Style:
    """What a run wears, and nothing else it says (its text, its width)."""
    style: Style = {}
    doc_ir.apply_style(style, run)
    return style


def _glyphs(block: Mapping[str, object]) -> bool | None:
    """Whether a corpus list is numbered (a test's `glyphs`): a key of the corpus's own,
    which no IR block carries. None is the list an import built."""
    said = block.get("glyphs")
    return said if isinstance(said, bool) else None


def _object_unit(kind: str, run: Run, world: World) -> Chip:
    if kind == "image":
        return Picture(id=run.get("value") or world.fresh("kix.i"),
                       uri=run.get("uri", "https://example.invalid/pic"),
                       alt=run.get("alt", ""), size_pt=run.get("size"))
    if kind == "person":
        return Person(email=run.get("value", "someone@example.com"),
                      name=run.get("text", "Someone"))
    if kind == "date":
        return DateChip(timestamp=run.get("value", "2026-09-20T12:00:00Z"),
                        display=run.get("text", "Sep 20, 2026"))
    if kind == "equation":
        return Equation(latex=run.get("text", "E=m{c}^{2}"))
    return OtherChip(kind=kind)


# ---------------------------------------------------------------- reading it as a sync does

def part_ir(world: World, tab: str | None, ours: Ir | None, base: Ir | None) -> Ir:
    """One tab's IR, filled in the way a sync's read fills it in.

    This mirrors `doc_sync._part_of` and calls the same public functions, so the read
    side of the pipeline is under test here too: the keys come from the named ranges, a
    list's ordered-ness from the file and the base (an imported list cannot say its
    own), and a picture's file name from the base. `ours` and `base` are the file and
    the base, None where a read has neither (`fuzz_docs.bootstrap`).
    """
    if tab:
        mine, was = doc_ir.tab_part(ours, tab), doc_ir.tab_part(base, tab)
    else:
        mine, was = ours, base
    doc = world.read()
    part = doc_ir.from_document(doc, tab)
    doc_ir.apply_keys(part, doc_ir.named_ranges_of(doc, part.get("tab")))
    doc_merge.restore_unreadable(part, *[s for s in (mine, was) if s])
    doc_merge.restore_pictures(part, was, mine)
    if tab:
        part.pop("title", None)
    for each in world.tabs:
        if each.id != (tab or part.get("tab")):
            continue
        if not tab:
            part["tab_title"] = each.title
        else:
            part["title"] = each.title
            if each.parent:
                part["parent"] = each.parent
    return part


def read_ir(world: World, ours: Ir | None, base: Ir | None) -> Ir:
    """The whole document's IR: the first tab, with the others under `tabs`."""
    ir = part_ir(world, None, ours, base)
    extra = [part_ir(world, tab.id, ours, base) for tab in world.tabs[1:]]
    if extra:
        ir["tabs"] = extra
    ir["document"] = "world"
    return ir


def settled_ir(world: World, ours: Ir | None, base: Ir | None) -> Ir:
    """The read that becomes the file and the base: the same, plus every equation's
    LaTeX, which on a live document comes from the Markdown export (`doc_sync.settle`
    → `doc_ir.attach_latex`) and never from `documents.get`."""
    ir = read_ir(world, ours, base)
    doc_ir.attach_latex(ir, world.latex())
    return ir
