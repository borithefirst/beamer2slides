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
    return {"named": "NORMAL_TEXT", "align": "START", "bullet": None}


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
        return {"body": {"content": content}, "lists": lists, "inlineObjects": objects,
                "namedRanges": named}

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
        style = _ir_style(arg.get("textStyle", {}))
        for u, _ in self._chars(tab, arg["range"], request):
            for field in fields:
                key = {"foregroundColor": "color", "backgroundColor": "highlight",
                       "weightedFontFamily": "code", "link": "link"}.get(
                           field, STYLE_FIELDS.get(field, field))
                if style.get(key):
                    u["s"][key] = style[key]
                else:
                    u["s"].pop(key, None)

    def _do_updateParagraphStyle(self, arg: dict, request: dict) -> None:
        style = arg.get("paragraphStyle", {})
        fields = [f.strip() for f in arg.get("fields", "").split(",") if f.strip()]
        for para in self._paragraphs(arg["range"]):
            if "namedStyleType" in fields and style.get("namedStyleType"):
                para["named"] = style["namedStyleType"]
            if "alignment" in fields and style.get("alignment"):
                para["align"] = style["alignment"]

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
        tab, grid, at = self._grid(arg["tableCellLocation"], request)
        cell = arg["tableCellLocation"]
        row = cell.get("rowIndex", 0) + (1 if arg.get("insertBelow") else 0)
        grid["rows"].insert(row, [[mark()] for _ in grid["rows"][0]])
        self._shift(tab, at, 1 + len(grid["rows"][0]) * 2)

    def _do_insertTableColumn(self, arg: dict, request: dict) -> None:
        tab, grid, at = self._grid(arg["tableCellLocation"], request)
        cell = arg["tableCellLocation"]
        column = cell.get("columnIndex", 0) + (1 if arg.get("insertRight") else 0)
        for row in grid["rows"]:
            row.insert(column, [mark()])
        self._shift(tab, at, len(grid["rows"]) * 2)

    def _do_deleteTableRow(self, arg: dict, request: dict) -> None:
        tab, grid, at = self._grid(arg["tableCellLocation"], request)
        if len(grid["rows"]) <= 1:
            raise Refused(request, "a table's last row cannot be deleted")
        grid["rows"].pop(arg["tableCellLocation"].get("rowIndex", 0))
        self._shift(tab, at, 0)

    def _do_deleteTableColumn(self, arg: dict, request: dict) -> None:
        tab, grid, at = self._grid(arg["tableCellLocation"], request)
        if len(grid["rows"][0]) <= 1:
            raise Refused(request, "a table's last column cannot be deleted")
        for row in grid["rows"]:
            row.pop(arg["tableCellLocation"].get("columnIndex", 0))
        self._shift(tab, at, 0)

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
    """A Docs textStyle as the IR spells it — `doc_ir._style_of` without the document."""
    out = {}
    for api, key in STYLE_FIELDS.items():
        if text_style.get(api):
            out[key] = True
    if text_style.get("weightedFontFamily", {}).get("fontFamily") in doc_ir.MONO:
        out["code"] = True
    for api, key in (("foregroundColor", "color"), ("backgroundColor", "highlight")):
        rgb = text_style.get(api, {}).get("color", {}).get("rgbColor")
        if rgb is not None:
            out[key] = doc_ir._hex(rgb)
    if text_style.get("link", {}).get("url"):
        out["link"] = text_style["link"]["url"]
    return out


def _api_style(style: dict) -> dict:
    out: dict = {}
    for api, key in STYLE_FIELDS.items():
        if style.get(key):
            out[api] = True
    if style.get("code"):
        out["weightedFontFamily"] = {"fontFamily": "Courier New"}
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
    out = {"startIndex": start, "endIndex": at,
           "paragraph": {"elements": elements,
                         "paragraphStyle": {"namedStyleType": para["named"],
                                            "alignment": para["align"]}}}
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
        if block["kind"] == "heading":
            para["named"] = f"HEADING_{block.get('level', 1)}"
        if block.get("align"):
            para["align"] = doc_ir.TO_ALIGNMENT[block["align"]]
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
