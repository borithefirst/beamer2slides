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
"""

from __future__ import annotations

import copy

from .. import doc_ir, doc_merge

# What the API answers a request it will not take. Google throws out the whole batch
# with it, so this is never caught request by request (docs/google-docs.md).
class Refused(RuntimeError):
    def __init__(self, request: dict, why: str):
        self.request, self.why = request, why
        super().__init__(f"{next(iter(request), '?')}: {why}")


# The glyphs `createParagraphBullets` leaves behind, by preset (measured: after one,
# the list reads back as what it is, where an imported one never could).
ORDERED_PRESET = "NUMBERED_DECIMAL_ALPHA_ROMAN"
STYLE_FIELDS = {"bold": "bold", "italic": "italic", "underline": "underline",
                "strikethrough": "strike"}
MARK_KEYS = {key for key, _ in doc_ir.MARK_FIELDS}


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


def text_of(units: list[str]) -> str:
    return b"".join(u.encode("utf-16-le", "surrogatepass")
                    for u in units).decode("utf-16-le", "surrogatepass")


# ---------------------------------------------------------------- the units of a tab

def char(c: str, style: dict | None = None) -> dict:
    return {"k": "c", "c": c, "s": dict(style or {})}


def mark(style: dict | None = None, para: dict | None = None) -> dict:
    """A paragraph mark: the newline that ends a paragraph, and where its style lives."""
    return {"k": "m", "c": "\n", "s": dict(style or {}), "p": dict(para or plain())}


def plain() -> dict:
    # `measures` holds the six properties of `doc_merge.PARAGRAPH_FIELDS` by their
    # IR key, absent meaning inherited rather than zero (`doc_ir._paragraph_measures`).
    # `align` is None when the paragraph sets none of its own, which is not the same
    # as START: it is whatever the document's named style says, and saying it out loud
    # is the only way this world can hold a theme (`THEME`).
    return {"named": "NORMAL_TEXT", "align": None, "bullet": None, "measures": {}}


def obj(kind: str, **fields) -> dict:
    """A chip, an equation or a picture: one index unit, except an equation."""
    return {"k": "o", "o": {"chip": kind, "size": 13 if kind == "equation" else 1, **fields}}


def table(rows: list[list[list[dict]]]) -> dict:
    return {"k": "t", "rows": rows}


def toc(size: int = 4) -> dict:
    """A table of contents: generated content no request can make (docs/google-docs.md)."""
    return {"k": "toc", "size": size}


def unit_size(u: dict) -> int:
    if u["k"] == "t":
        return 1 + sum(1 + sum(1 + size(cell) for cell in row) for row in u["rows"])
    if u["k"] == "toc":
        return u["size"]
    if u["k"] == "o":
        return u["o"].get("size", 1)
    return 1


def size(units: list[dict]) -> int:
    return sum(unit_size(u) for u in units)


def _row_size(row: list) -> int:
    """What a table row costs in document indices: the row itself, then each cell."""
    return 1 + sum(1 + size(cell) for cell in row)


def _row_start(grid: dict, at: int, index: int) -> int:
    """Where a row begins: the table's own unit, then every row in front of it."""
    return at + 1 + sum(_row_size(row) for row in grid["rows"][:index])


def _cell_start(grid: dict, at: int, row: int, column: int) -> int:
    """Where a cell begins: its row, then every cell in front of it in that row."""
    return (_row_start(grid, at, row) + 1
            + sum(1 + size(cell) for cell in grid["rows"][row][:column]))


def text_units(text: str, style: dict | None = None) -> list[dict]:
    return [char(c, style) for c in utf16_units(text)]


# ---------------------------------------------------------------- finding an index

def locate(units: list[dict], start: int, index: int) -> tuple[list, int] | None:
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
            if u["k"] == "t":
                return _in_table(u, at, index)
            return None            # inside an equation, or inside a TOC
        at += n
    return (units, len(units)) if index == at else None


def _in_table(t: dict, start: int, index: int) -> tuple[list, int] | None:
    at = start + 1                                   # past the table's own unit
    for row in t["rows"]:
        at += 1                                      # the row's own unit
        for cell in row:
            at += 1                                  # the cell's own unit
            n = size(cell)
            if at <= index <= at + n:
                return locate(cell, at, index)
            at += n
    return None


def writable_spot(units: list[dict], index: int, request: dict) -> tuple[list, int]:
    """Where a request may write text, or `Refused`.

    Nothing can be inserted at a table's own index (measured), nor at a row's or a
    cell's, nor past the segment's last paragraph mark, nor inside an equation.
    """
    spot = locate(units, 1, index)
    if spot is None or index < 1:
        raise Refused(request, f"index {index} is not inside a paragraph")
    cont, offset = spot
    if offset == len(cont):
        raise Refused(request, f"index {index} is past the segment's last paragraph mark")
    if cont[offset]["k"] in ("t", "toc"):
        what = "a table of contents" if cont[offset]["k"] == "toc" else "a table"
        raise Refused(request, f"index {index} is {what}'s own index: nothing can be "
                               f"inserted there")
    return cont, offset


def containers(units: list[dict], start: int):
    """(unit list, its first index) for a body and for every table cell inside it."""
    yield units, start
    at = start
    for u in units:
        if u["k"] == "t":
            inner = at + 1
            for row in u["rows"]:
                inner += 1
                for cell in row:
                    inner += 1
                    yield from containers(cell, inner)
                    inner += size(cell)
        at += unit_size(u)


def paragraphs(units: list[dict], start: int):
    """(container, first offset, mark offset, first index, mark index) per paragraph,
    in the body and in every cell."""
    for cont, at in containers(units, start):
        low, index = 0, at
        for i, u in enumerate(cont):
            index += unit_size(u)
            if u["k"] == "m":
                yield cont, low, i, index - 1 - _run_len(cont[low:i]), index - 1
                low = i + 1


def _run_len(units: list[dict]) -> int:
    return sum(unit_size(u) for u in units)


# ---------------------------------------------------------------- the document

class Tab:
    """One tab: its units, its named ranges, its lists. The first tab is the body."""

    def __init__(self, tab_id: str, title: str = "", parent: str | None = None,
                 units: list | None = None):
        self.id, self.title, self.parent = tab_id, title, parent
        self.units: list[dict] = units if units is not None else [mark()]
        self.named: list[dict] = []      # {"id", "name", "start", "end"}
        self.lists: dict[str, dict] = {}  # listId -> {"ordered": bool | None}

    def size(self) -> int:
        return size(self.units)

    def end(self) -> int:
        return 1 + self.size()


class World:
    """A whole document: tabs, named ranges, inline objects and a revision id."""

    def __init__(self, title: str = "doc"):
        self.title = title
        self.tabs: list[Tab] = [Tab("t.0")]
        # The document's theme: `namedStyleType` -> what that style says, which is
        # what a paragraph setting nothing of its own shows. No request can write one
        # (there is none in the API), so nothing here ever changes it — it is here so
        # that a paragraph *inheriting* a property can be represented at all, which is
        # the one thing a document's look is made of. Empty for most corpus shapes.
        self.theme: dict[str, dict] = {}
        self.revision = 1
        # A plain counter, not itertools.count: a world is deep-copied all the time (the
        # campaign tries a second sync on a copy), and copying an iterator is deprecated.
        self._ids = 0

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

    def read(self) -> dict:
        """The document as `documents.get(includeTabsContent=True)` reports it.

        With the flag the `body` key disappears and `tabs` takes its place, nested by
        parent — measured, and the reason every read in the sync passes the flag.
        """
        by_parent: dict[str | None, list] = {}
        for t in self.tabs:
            by_parent.setdefault(t.parent, []).append(t)

        def branch(t: Tab, index: int) -> dict:
            props = {"tabId": t.id, "title": t.title, "index": index}
            if t.parent:
                props["parentTabId"] = t.parent
            return {"tabProperties": props, "documentTab": self._tab_json(t),
                    "childTabs": [branch(c, i) for i, c in enumerate(by_parent.get(t.id, []))]}

        return {"title": self.title, "revisionId": f"r{self.revision}",
                "tabs": [branch(t, i) for i, t in enumerate(by_parent.get(None, []))]}

    def _tab_json(self, t: Tab) -> dict:
        objects: dict = {}
        content = [{"startIndex": 0, "endIndex": 1, "sectionBreak": {"sectionStyle": {}}}]
        content += _content_json(t.units, 1, objects)
        named: dict = {}
        for ranged in t.named:
            entry = named.setdefault(ranged["name"], {"name": ranged["name"], "namedRanges": []})
            entry["namedRanges"].append({"namedRangeId": ranged["id"], "name": ranged["name"],
                                         "ranges": [{"startIndex": ranged["start"],
                                                     "endIndex": ranged["end"]}]})
        lists = {lid: {"listProperties": {"nestingLevels": _levels(info)}}
                 for lid, info in t.lists.items()}
        out = {"body": {"content": content}, "lists": lists, "inlineObjects": objects,
               "namedRanges": named}
        if self.theme:
            out["namedStyles"] = {"styles": [
                {"namedStyleType": name} | dict(style) for name, style in self.theme.items()]}
        return out

    def latex(self) -> dict[tuple, str]:
        """Every equation's LaTeX, by (tab, start) — what `doc_ir.latex_of` digs out of
        the Markdown export on a live document, since `documents.get` says `{}`."""
        out = {}
        for t in self.tabs:
            for cont, at in containers(t.units, 1):
                index = at
                for u in cont:
                    if u["k"] == "o" and u["o"]["chip"] == "equation":
                        out[(None if t is self.tabs[0] else t.id, index)] = u["o"].get("latex", "")
                    index += unit_size(u)
        return out

    # -- writing

    def apply(self, requests: list[dict]) -> dict:
        """One batch, all or nothing.

        Google applies a batch as a transaction: one request it refuses throws out
        every other one with it. A sync that plans a request the API will not take
        therefore writes *nothing* and dies, which is exactly the failure the Slides
        campaign could not see until its applier started consuming requests.
        """
        spare = copy.deepcopy(self.tabs)
        replies = []
        try:
            for request in requests:
                replies.append(self._one(request) or {})
        except Refused:
            self.tabs = spare
            raise
        self.revision += 1
        return {"replies": replies}

    def _one(self, request: dict) -> dict | None:
        name = next(iter(request), None)
        handler = getattr(self, f"_do_{name}", None)
        if handler is None:
            raise Refused(request, "no such request in the v1 API")
        return handler(request[name], request)

    # -- text

    def _do_insertText(self, arg: dict, request: dict) -> None:
        tab = self.tab(arg["location"].get("tabId"))
        self._insert(tab, arg["location"]["index"], arg["text"], request)

    def _insert(self, tab: Tab, index: int, text: str, request: dict) -> None:
        cont, offset = writable_spot(tab.units, index, request)
        style = _style_in_front(cont, offset)
        para = _paragraph_at(cont, offset)
        new: list[dict] = []
        pieces = text.split("\n")
        for i, piece in enumerate(pieces):
            if i:
                new.append(mark(style, para))
            new += text_units(piece, style)
        cont[offset:offset] = new
        self._shift(tab, index, len(new))

    def _do_deleteContentRange(self, arg: dict, request: dict) -> None:
        tab = self.tab(arg["range"].get("tabId"))
        start, end = arg["range"]["startIndex"], arg["range"]["endIndex"]
        if end <= start or start < 1:
            raise Refused(request, f"the range [{start}, {end}) is empty or before the body")
        if end > tab.end():
            raise Refused(request, f"the end index ({end}) is past the body ({tab.end()})")
        low, high = locate(tab.units, 1, start), locate(tab.units, 1, end)
        if low is None or high is None or low[0] is not high[0]:
            raise Refused(request, f"the range [{start}, {end}) is not one run of text")
        cont, i = low
        j = high[1]
        if j >= len(cont):
            raise Refused(request, "Invalid deletion range: the segment's last paragraph "
                                   "mark cannot be deleted")
        for k in range(i, j):
            if cont[k]["k"] == "m" and k + 1 < len(cont) \
                    and cont[k + 1]["k"] in ("t", "toc") and k + 1 >= j:
                what = "a table of contents" if cont[k + 1]["k"] == "toc" else "a table"
                raise Refused(request, f"Invalid deletion range: the newline in front of "
                                       f"{what} cannot be deleted")
        # Docs merges the two paragraphs keeping the *first* one's style: the mark that
        # survives takes the paragraph style of the first one deleted. That is the rule
        # `doc_merge._delete_range` leans on when a block gives up its neighbour's mark.
        gone = next((u["p"] for u in cont[i:j] if u["k"] == "m"), None)
        del cont[i:j]
        if gone is not None:
            after = next((u for u in cont[i:] if u["k"] == "m"), None)
            if after is not None:
                after["p"] = dict(gone)
        self._shift(tab, start, -(end - start), end)

    # -- objects

    def _do_insertInlineImage(self, arg: dict, request: dict) -> None:
        picture = {"uri": arg["uri"]}
        if arg.get("objectSize"):
            picture["size_pt"] = [arg["objectSize"]["width"]["magnitude"],
                                  arg["objectSize"]["height"]["magnitude"]]
        self._object(arg["location"], obj("image", id=self.fresh("kix.i"), **picture), request)

    def _do_insertPerson(self, arg: dict, request: dict) -> None:
        email = arg["personProperties"]["email"]
        self._object(arg["location"], obj("person", email=email, name=email.split("@")[0]),
                     request)

    def _do_insertDate(self, arg: dict, request: dict) -> None:
        stamp = arg["dateElementProperties"]["timestamp"]
        self._object(arg["location"], obj("date", timestamp=stamp, display=stamp[:10]), request)

    def _object(self, location: dict, unit: dict, request: dict) -> None:
        tab = self.tab(location.get("tabId"))
        index = location["index"]
        cont, offset = writable_spot(tab.units, index, request)
        cont.insert(offset, unit)
        self._shift(tab, index, unit_size(unit))

    # -- styling

    def _do_updateTextStyle(self, arg: dict, request: dict) -> None:
        tab = self.tab(arg["range"].get("tabId"))
        fields = [f.strip() for f in arg.get("fields", "").split(",") if f.strip()]
        given = arg.get("textStyle", {})
        style = _ir_style(given)
        for u, _ in self._chars(tab, arg["range"], request):
            for field in fields:
                key = API_TO_IR.get(field, field)
                if key in MARK_KEYS and field in given:
                    # A mark named *with* a value, False included: `_ir_style` reads
                    # a document, where False against no named style is nothing, but
                    # a request means what it says.
                    u["s"][key] = bool(given[field])
                elif style.get(key):
                    u["s"][key] = style[key]
                else:
                    u["s"].pop(key, None)
                    # A face named with no value puts the paragraph's own back, and
                    # the file's older `code` spelling is a face like any other.
                    if key == "font":
                        u["s"].pop("code", None)

    def _do_updateParagraphStyle(self, arg: dict, request: dict) -> None:
        style = arg.get("paragraphStyle", {})
        fields = [f.strip() for f in arg.get("fields", "").split(",") if f.strip()]
        for para in self._paragraphs(arg["range"]):
            if "namedStyleType" in fields and style.get("namedStyleType"):
                para["named"] = style["namedStyleType"]
            if "alignment" in fields:
                # Named with no value: back to what the paragraph inherits, which is
                # None here — not START. The difference is the whole of a theme.
                para["align"] = style.get("alignment")
            # A field the merge names without a value means "back to the default"
            # (`doc_merge.paragraph_style`), which is how a property the source
            # dropped goes away. Naming it and not applying that would make the
            # merge write it again for ever.
            for key, api in doc_merge.PARAGRAPH_FIELDS:
                if api not in fields:
                    continue
                if api in style:
                    para.setdefault("measures", {})[key] = _ir_measure(key, style[api])
                else:
                    para.get("measures", {}).pop(key, None)

    def _do_createParagraphBullets(self, arg: dict, request: dict) -> None:
        tab = self.tab(arg["range"].get("tabId"))
        ordered = arg.get("bulletPreset") == ORDERED_PRESET
        fresh = None
        for para in self._paragraphs(arg["range"]):
            if para.get("bullet"):
                # Measured: over a list the importer built, this keeps every item's
                # nesting level and gives the list glyphs it can report from then on.
                tab.lists[para["bullet"]["list"]] = {"ordered": ordered}
                continue
            if fresh is None:
                fresh = self.fresh("kix.l")
                tab.lists[fresh] = {"ordered": ordered}
            para["bullet"] = {"list": fresh, "level": 0}

    def _do_deleteParagraphBullets(self, arg: dict, request: dict) -> None:
        for para in self._paragraphs(arg["range"]):
            para["bullet"] = None

    def _chars(self, tab: Tab, span: dict, request: dict):
        start, end = span["startIndex"], span["endIndex"]
        for cont, at in containers(tab.units, 1):
            index = at
            for u in cont:
                if start <= index < end and u["k"] in ("c", "m"):
                    yield u, index
                index += unit_size(u)

    def _paragraphs(self, span: dict):
        """The paragraph styles of every paragraph the range touches."""
        tab = self.tab(span.get("tabId"))
        start, end = span["startIndex"], span["endIndex"]
        for cont, low, i, first, at in paragraphs(tab.units, 1):
            if first < end and start <= at:
                yield cont[i]["p"]

    # -- tables

    def _do_insertTable(self, arg: dict, request: dict) -> None:
        rows, columns = arg["rows"], arg["columns"]
        if rows < 1 or columns < 1:
            raise Refused(request, "a table needs a row and a column")
        grid = table([[[mark()] for _ in range(columns)] for _ in range(rows)])
        if "endOfSegmentLocation" in arg:
            tab = self.tab(arg["endOfSegmentLocation"].get("tabId"))
            # Measured: a table written after everything keeps a paragraph after it,
            # because a document ends on one.
            tab.units += [grid, mark()]
            return
        tab = self.tab(arg["location"].get("tabId"))
        index = arg["location"]["index"]
        cont, offset = writable_spot(tab.units, index, request)
        # `insertTable` splits the paragraph its index is in: what was before the index
        # stays a paragraph of its own, then comes the table, then the rest (measured).
        para = _paragraph_at(cont, offset)
        cont[offset:offset] = [mark(_style_in_front(cont, offset), para), grid]
        self._shift(tab, index, 1 + unit_size(grid))

    def _do_insertTableRow(self, arg: dict, request: dict) -> None:
        # A row is written at its own index, not the table's: a named range on a cell
        # of a row *above* the new one does not move, and one below does. Shifting the
        # whole table by the row's size, which this did, moved every anchor in it.
        tab, grid, at = self._grid(arg["tableCellLocation"], request)
        cell = arg["tableCellLocation"]
        row = cell.get("rowIndex", 0) + (1 if arg.get("insertBelow") else 0)
        start = _row_start(grid, at, row)
        grid["rows"].insert(row, [[mark()] for _ in grid["rows"][0]])
        self._shift(tab, start, _row_size(grid["rows"][row]))

    def _do_insertTableColumn(self, arg: dict, request: dict) -> None:
        tab, grid, at = self._grid(arg["tableCellLocation"], request)
        cell = arg["tableCellLocation"]
        column = cell.get("columnIndex", 0) + (1 if arg.get("insertRight") else 0)
        # One cell per row, back to front: a cell written low down leaves the indices
        # above it alone, so each `_cell_start` is still the one the grid has.
        for r in reversed(range(len(grid["rows"]))):
            start = _cell_start(grid, at, r, column)
            grid["rows"][r].insert(column, [mark()])
            self._shift(tab, start, 1 + size(grid["rows"][r][column]))

    def _do_deleteTableRow(self, arg: dict, request: dict) -> None:
        """A row's content goes out of the document, so everything after it moves up.
        Shifting by 0, which this did, left every anchor below the table one row's
        worth of units too high: the keys below a table the source regridded all slid
        onto the block above (chain-8 seeds 5099, 5167). Google moves them."""
        tab, grid, at = self._grid(arg["tableCellLocation"], request)
        if len(grid["rows"]) <= 1:
            raise Refused(request, "a table's last row cannot be deleted")
        row = arg["tableCellLocation"].get("rowIndex", 0)
        start = _row_start(grid, at, row)
        self._shift(tab, start, -_row_size(grid["rows"][row]))
        grid["rows"].pop(row)

    def _do_deleteTableColumn(self, arg: dict, request: dict) -> None:
        tab, grid, at = self._grid(arg["tableCellLocation"], request)
        if len(grid["rows"][0]) <= 1:
            raise Refused(request, "a table's last column cannot be deleted")
        column = arg["tableCellLocation"].get("columnIndex", 0)
        for r in reversed(range(len(grid["rows"]))):
            start = _cell_start(grid, at, r, column)
            self._shift(tab, start, -(1 + size(grid["rows"][r][column])))
            grid["rows"][r].pop(column)

    def _grid(self, where: dict, request: dict) -> tuple[Tab, dict, int]:
        tab = self.tab(where["tableStartLocation"].get("tabId"))
        at = where["tableStartLocation"]["index"]
        index = 1
        for u in tab.units:
            if index == at and u["k"] == "t":
                return tab, u, at
            index += unit_size(u)
        raise Refused(request, f"no table starts at {at}")

    # -- named ranges

    def _do_createNamedRange(self, arg: dict, request: dict) -> dict:
        tab = self.tab(arg["range"].get("tabId"))
        start, end = arg["range"]["startIndex"], arg["range"]["endIndex"]
        if end <= start or start < 1 or end > tab.end():
            raise Refused(request, f"the range [{start}, {end}) is not in the body")
        ident = self.fresh("nr.")
        tab.named.append({"id": ident, "name": arg["name"], "start": start, "end": end})
        return {"createNamedRange": {"namedRangeId": ident}}

    def _do_deleteNamedRange(self, arg: dict, request: dict) -> None:
        """By id, or every range of a name: the two the API takes, one of them required.
        A range id is the document's, not a tab's, so it is looked for everywhere; a
        name goes by `tabsCriteria` where the request gives one, else every tab."""
        ident, name = arg.get("namedRangeId"), arg.get("name")
        if not ident and not name:
            raise Refused(request, "deleteNamedRange needs a namedRangeId or a name")
        wanted = set((arg.get("tabsCriteria") or {}).get("tabIds", []))
        tabs = [t for t in self.tabs if not wanted or t.id in wanted]
        hit = False
        for tab in tabs:
            kept = [r for r in tab.named
                    if not (r["id"] == ident if ident else r["name"] == name)]
            hit = hit or len(kept) != len(tab.named)
            tab.named = kept
        if not hit:
            raise Refused(request, f"no named range {ident or name!r} to delete")

    # -- tabs

    def _do_addDocumentTab(self, arg: dict, request: dict) -> dict:
        props = arg.get("tabProperties", {})
        ident = self.fresh("t.")
        self.tabs.append(Tab(ident, props.get("title", ""), props.get("parentTabId")))
        return {"addDocumentTab": {"tabProperties": {"tabId": ident,
                                                     "title": props.get("title", "")}}}

    def _do_updateDocumentTabProperties(self, arg: dict, request: dict) -> None:
        props = arg.get("tabProperties", {})
        self.tab(props["tabId"]).title = props.get("title", "")

    def _do_deleteTab(self, arg: dict, request: dict) -> None:
        ident = arg["tabId"]
        if self.tabs[0].id == ident:
            raise Refused(request, "the first tab cannot be deleted")
        self.tabs = [t for t in self.tabs if t.id != ident]

    # -- named ranges follow the text

    def _shift(self, tab: Tab, index: int, delta: int, end: int | None = None) -> None:
        """Google keeps every named range up to date as the text moves (measured).

        Half-open `[start, end)`: text typed at an anchor's first index falls outside
        it and pushes it along, text typed inside grows it, and an anchor whose text is
        all deleted disappears — which is the signal `doc_merge` reads as a block the
        reader deleted.
        """
        alive = []
        for ranged in tab.named:
            low, high = ranged["start"], ranged["end"]
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
                alive.append(ranged | {"start": low, "end": high})
        tab.named = alive


# ---------------------------------------------------------------- style translation

def _style_in_front(cont: list[dict], offset: int) -> dict:
    """The style Docs gives text typed here: the character in front of it (measured)."""
    if offset and cont[offset - 1]["k"] in ("c", "m"):
        return dict(cont[offset - 1]["s"])
    if offset < len(cont) and cont[offset]["k"] in ("c", "m"):
        return dict(cont[offset]["s"])
    return {}


def _paragraph_at(cont: list[dict], offset: int) -> dict:
    """The paragraph style at this offset: the mark that terminates it."""
    for u in cont[offset:]:
        if u["k"] == "m":
            return dict(u["p"])
    return plain()


def _ir_style(text_style: dict) -> dict:
    """A Docs textStyle as the IR spells it.

    `doc_ir._style_of` itself, never a copy of it. A copy is what this was, and it
    drifted the moment the dialect grew a face and a size: the world went on
    calling four families `code` while the reader had stopped, so every styling
    comparison in the oracle was against a reader nobody uses. The world's whole
    claim is that it reads what `doc_ir` reads, so it has to call it.

    No `default`: a named style is subtracted from what a *document* reports, and
    the world's documents carry no `namedStyles` — nothing here sets a property
    that only repeats one.
    """
    return doc_ir._style_of(text_style)


# The `updateTextStyle` fields the merge writes, and the IR key each one sets
# (`doc_merge.MANAGED`). The world applies a request field by field, so a field it
# does not know would be silently ignored and the read-back would never show it.
API_TO_IR = {"bold": "bold", "italic": "italic", "underline": "underline",
             "strikethrough": "strike", "foregroundColor": "color",
             "backgroundColor": "highlight", "link": "link",
             "weightedFontFamily": "font", "fontSize": "fontsize",
             "smallCaps": "smallcaps", "baselineOffset": "script"}


def _ir_measure(key: str, value):
    """One `paragraphStyle` property as the IR spells it — the inverse of
    `doc_merge._paragraph_value`, rounded as `doc_ir._paragraph_measures` rounds it,
    so a value written and then read back is the same number and the merge does not
    see a change nobody made."""
    if key == "line_spacing":
        return round(float(value) / 100, 3)
    if key == "shading":
        rgb = (value or {}).get("backgroundColor", {}).get("color", {}).get("rgbColor")
        return doc_ir._hex(rgb) if rgb else None
    return doc_ir._points(value)


def _api_measures(measures: dict) -> dict:
    """The six paragraph properties as `documents.get` reports them."""
    return {api: doc_merge._paragraph_value(key, measures[key])
            for key, api in doc_merge.PARAGRAPH_FIELDS
            if measures.get(key) is not None}


def _api_style(style: dict) -> dict:
    """The inverse of `_ir_style`: what `documents.get` would report for it."""
    out: dict = {}
    for key, api in doc_ir.MARK_FIELDS:
        # A mark is True, absent — or False, which a reader leaves behind by turning
        # off a mark the named style puts on. `documents.get` reports all three, and
        # collapsing the last two is how a theme's bold would come back unasked.
        if style.get(key) is not None:
            out[api] = bool(style[key])
    # `code` is the file's older spelling for a monospaced face and still means
    # Courier New (`doc_ir.CODE_FAMILY`); an explicit face wins over it.
    family = style.get("font") or (doc_ir.CODE_FAMILY if style.get("code") else None)
    if family:
        out["weightedFontFamily"] = {"fontFamily": family}
    if style.get("script"):
        # Not a mark: one of three values, and "none" is the third rather than the
        # absence of the other two (`doc_ir.SCRIPTS`).
        out["baselineOffset"] = {"super": "SUPERSCRIPT", "sub": "SUBSCRIPT",
                                 "none": "NONE"}[style["script"]]
    if style.get("fontsize"):
        out["fontSize"] = {"magnitude": float(style["fontsize"]), "unit": "PT"}
    for api, key in (("foregroundColor", "color"), ("backgroundColor", "highlight")):
        if style.get(key):
            out[api] = {"color": {"rgbColor": doc_merge._rgb(style[key])}}
    if style.get("link"):
        out["link"] = {"url": style["link"]}
    return out


def _levels(info: dict) -> list[dict]:
    """A list's nesting levels as `documents.get` reports them.

    `ordered: None` is the list Drive's HTML importer built: every level comes back
    `GLYPH_TYPE_UNSPECIFIED`, for `<ul>` and `<ol>` alike, which is why the file has to
    say which it is (`doc_ir._ordered`, `doc_merge.restore_unreadable`).
    """
    if info.get("ordered") is None:
        return [{"glyphType": "GLYPH_TYPE_UNSPECIFIED"} for _ in range(9)]
    if info["ordered"]:
        return [{"glyphType": g, "glyphFormat": "%0."}
                for g in ("DECIMAL", "ALPHA", "ROMAN") for _ in range(3)]
    return [{"glyphSymbol": g} for g in "●○■" for _ in range(3)]


# ---------------------------------------------------------------- documents.get JSON

def _content_json(units: list[dict], start: int, objects: dict) -> list[dict]:
    out: list[dict] = []
    at, low, run = start, start, []
    for u in units:
        if u["k"] in ("t", "toc"):
            if run:
                out.append(_paragraph_json(run, low, objects))
                run = []
            n = unit_size(u)
            out.append(_table_json(u, at, objects) if u["k"] == "t" else
                       {"startIndex": at, "endIndex": at + n,
                        "tableOfContents": {"content": []}})
            at += n
            low = at
            continue
        run.append(u)
        at += unit_size(u)
        if u["k"] == "m":
            out.append(_paragraph_json(run, low, objects))
            run, low = [], at
    if run:
        out.append(_paragraph_json(run, low, objects))
    return out


def _paragraph_json(run: list[dict], start: int, objects: dict) -> dict:
    elements: list[dict] = []
    held: list[str] = []          # code units waiting to become one text run
    held_style: dict | None = None
    at = start

    def flush(end: int) -> None:
        nonlocal held, held_style
        if held:
            # `text_of`, not "".join: the two halves of an astral character are separate
            # index units here and only become one Python character again together.
            elements.append({"startIndex": end - len(held), "endIndex": end,
                             "textRun": {"content": text_of(held),
                                         "textStyle": _api_style(held_style or {})}})
        held, held_style = [], None

    for u in run:
        if u["k"] == "o":
            flush(at)
            elements.append(_object_json(u["o"], at, objects))
            at += unit_size(u)
            continue
        style = _api_style(u["s"])
        if held and style != _api_style(held_style or {}):
            flush(at)
        held.append(u["c"])
        held_style = u["s"]
        at += 1
    flush(at)
    para = run[-1]["p"] if run and run[-1]["k"] == "m" else plain()
    # A paragraph reports what is set on it, never what it inherits: no `alignment`
    # key at all when it sets none, which is how Docs answers and what lets the
    # reader subtract the named style (`doc_ir._named_defaults`).
    para_style = {"namedStyleType": para["named"]}
    if para.get("align"):
        para_style["alignment"] = para["align"]
    out = {"startIndex": start, "endIndex": at,
           "paragraph": {"elements": elements, "paragraphStyle":
                         para_style | _api_measures(para.get("measures") or {})}}
    if para.get("bullet"):
        out["paragraph"]["bullet"] = {"listId": para["bullet"]["list"],
                                      "nestingLevel": para["bullet"]["level"]}
    return out


def _object_json(o: dict, at: int, objects: dict) -> dict:
    span = {"startIndex": at, "endIndex": at + o.get("size", 1)}
    kind = o["chip"]
    if kind == "equation":
        return span | {"equation": {}}
    if kind == "person":
        return span | {"person": {"personProperties": {"name": o.get("name", ""),
                                                       "email": o.get("email", "")}}}
    if kind == "date":
        return span | {"dateElement": {"dateElementProperties": {
            "displayText": o.get("display", ""), "timestamp": o.get("timestamp", ""),
            "dateFormat": o.get("format", ""), "locale": o.get("locale", "")}}}
    if kind == "link":
        return span | {"richLink": {"richLinkProperties": {"title": o.get("title", ""),
                                                           "uri": o.get("uri", "")}}}
    if kind == "image":
        embedded: dict = {"imageProperties": {"contentUri": o.get("uri", "")}}
        if o.get("size_pt"):
            embedded["size"] = {"width": {"magnitude": o["size_pt"][0], "unit": "PT"},
                                "height": {"magnitude": o["size_pt"][1], "unit": "PT"}}
        if o.get("alt"):
            embedded["description"] = o["alt"]
        objects[o["id"]] = {"inlineObjectProperties": {"embeddedObject": embedded}}
        return span | {"inlineObjectElement": {"inlineObjectId": o["id"]}}
    # A dropdown chip: an element with a span and no content key of any kind (measured).
    return span


def _table_json(t: dict, start: int, objects: dict) -> dict:
    at = start + 1
    rows = []
    for row in t["rows"]:
        row_start = at
        at += 1
        cells = []
        for cell in row:
            cell_start = at
            at += 1
            content = _content_json(cell, at, objects)
            at += size(cell)
            cells.append({"startIndex": cell_start, "endIndex": at, "content": content})
        rows.append({"startIndex": row_start, "endIndex": at, "tableCells": cells})
    return {"startIndex": start, "endIndex": at,
            "table": {"rows": len(t["rows"]),
                      "columns": len(t["rows"][0]) if t["rows"] else 0,
                      "tableRows": rows}}


# ---------------------------------------------------------------- IR -> a world

def build(parts: list[dict], title: str = "doc") -> World:
    """A world from IR-shaped blocks, one `parts` entry per tab.

    `{"title": ..., "parent": ..., "blocks": [...]}`, where a block is what the IR
    calls one: paragraph / heading / item / table / toc, with `runs`. A run with
    `chip` becomes the object of that kind; everything else becomes text.
    """
    world = World(title)
    for i, part in enumerate(parts):
        if i:
            world.tabs.append(Tab(world.fresh("t."), part.get("title", ""),
                                  part.get("parent")))
        tab = world.tabs[i] if i < len(world.tabs) else world.tabs[-1]
        tab.title = part.get("title", tab.title)
        tab.units = _units_of(part["blocks"], tab, world)
        if not tab.units or tab.units[-1]["k"] != "m":
            tab.units.append(mark())     # a body ends on a paragraph: the `trailer`
        if tab.units[0]["k"] in ("t", "toc"):
            # And a table cannot be the first thing in a body: one that is has an empty
            # paragraph in front of it that no request can delete (`doc_ir._hide_trailer`
            # calls it the `lead`). A corpus without it would put the merge at index 0.
            tab.units.insert(0, mark())
    return world


def _units_of(blocks: list[dict], tab: Tab, world: World) -> list[dict]:
    out: list[dict] = []
    for block in blocks:
        if block["kind"] == "table":
            out.append(table([[_units_of(cell, tab, world) or [mark()] for cell in row]
                              for row in block["rows"]]))
            continue
        if block["kind"] == "toc":
            out.append(toc())
            continue
        para = plain()
        if block["kind"] != "item":
            para["named"] = doc_merge.named_style(block)
        if block.get("align"):
            para["align"] = doc_ir.TO_ALIGNMENT[block["align"]]
        para["measures"] = {key: block[key] for key, _ in doc_merge.PARAGRAPH_FIELDS
                            if block.get(key) is not None}
        if block["kind"] == "item":
            lid = block.get("list") or "kix.imported"
            para["bullet"] = {"list": lid, "level": block.get("level", 0)}
            # An imported list cannot say whether it is numbered: that is the state the
            # merge has to cope with, so it is the corpus's default.
            tab.lists.setdefault(lid, {"ordered": block.get("glyphs")})
        for run in block.get("runs", []):
            if run.get("chip"):
                out.append(_object_unit(run, world))
            else:
                out += text_units(run["text"], {k: v for k, v in run.items()
                                                if k not in ("text", "width")})
        out.append(mark(para=para))
    return out


def _object_unit(run: dict, world: World) -> dict:
    kind = run["chip"]
    if kind == "image":
        return obj("image", id=run.get("value") or world.fresh("kix.i"),
                   uri=run.get("uri", "https://example.invalid/pic"),
                   alt=run.get("alt", ""), size_pt=run.get("size"))
    if kind == "person":
        return obj("person", email=run.get("value", "someone@example.com"),
                   name=run.get("text", "Someone"))
    if kind == "date":
        return obj("date", timestamp=run.get("value", "2026-09-20T12:00:00Z"),
                   display=run.get("text", "Sep 20, 2026"))
    if kind == "equation":
        return obj("equation", latex=run.get("text", "E=m{c}^{2}"))
    return obj(kind)


# ---------------------------------------------------------------- reading it as a sync does

def part_ir(world: World, tab: str | None, ours: dict | None = None,
            base: dict | None = None) -> dict:
    """One tab's IR, filled in the way a sync's read fills it in.

    This mirrors `doc_sync._part_of` and calls the same public functions, so the read
    side of the pipeline is under test here too: the keys come from the named ranges, a
    list's ordered-ness from the file and the base (an imported list cannot say its
    own), and a picture's file name from the base.
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
            if each.id == tab:
                part["title"] = each.title
                if each.parent:
                    part["parent"] = each.parent
    return part


def read_ir(world: World, ours: dict | None = None, base: dict | None = None) -> dict:
    """The whole document's IR: the first tab, with the others under `tabs`."""
    ir = part_ir(world, None, ours, base)
    extra = [part_ir(world, tab.id, ours, base) for tab in world.tabs[1:]]
    if extra:
        ir["tabs"] = extra
    ir["document"] = "world"
    return ir


def settled_ir(world: World, ours: dict | None = None, base: dict | None = None) -> dict:
    """The read that becomes the file and the base: the same, plus every equation's
    LaTeX, which on a live document comes from the Markdown export (`doc_sync.settle`
    → `doc_ir.attach_latex`) and never from `documents.get`."""
    ir = read_ir(world, ours, base)
    doc_ir.attach_latex(ir, world.latex())
    return ir
