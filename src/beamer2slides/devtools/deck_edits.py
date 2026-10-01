"""Human-like edits of a converted deck through the Slides API, for the sync tests.

Every edit finds its target by content, the way a person would (the slide titled "…", the
text box containing "…", the largest picture), never by our object ids, and returns an
expectation: what must still hold after a later sync, as tools/sync_check.py checks.
  {"edit": name, "args": {...}, "slides": [SEL, ...], "checks": [CHECK, ...]}
`slides` are the slides the edit touched (their selectors hold after the edit).

An edit is called with every argument (`replace_word(deck, slide, ...)`) or given as JSON
(`apply(deck, {"edit": name, "args": {...}})`), where an argument the JSON leaves out takes its
documented value (`nth`, `context`, `sy`, `dx`/`dy`, `body`, `new_title`); `EditName` is the
closed set of kinds, matched to `assert_never` in `apply`.

  python tools/deck_edits.py <deck> catalogue [--out expectations.json]   every edit kind, verified
  python tools/deck_edits.py <deck> apply '{"edit": "replace_word", "args": {...}}' [--out exp.json]
  python tools/deck_edits.py <deck> apply @edits.json [--out exp.json]            a list of edits from a file
<deck>: presentation id, URL or converted out folder.
"""

import argparse
import json
import re
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..google_types import (AffineTransform, BatchUpdateResponse, OpaqueColor, PlaceholderType, Presentation,
                            SlidesInsertTextRequest, SlidesPageElementProperties, SlidesRange, SlidesRequest,
                            SlidesService, SlidesTableCellLocation, SlidesTextStyle, SlidesUpdateTextStyleRequest,
                            children, image_url, is_placeholder_type, is_shape_type, object_id, slides_request_kind)
from ..json_types import Json, JsonObject, as_array, as_int, as_object, as_objects, as_str
from ..typing_compat import assert_never
from .sync_check import (EMU_PER_PT, Cell, CheckError, Element, Model, Point, Slide, check_all, norm, number, part,
                         phrase_span, presentation_id, raw_text, text_elements, utf16)


def _jlist(xs: Sequence[Json]) -> list[Json]:
    return [x for x in xs]


def _jnums(xs: Sequence[float]) -> list[Json]:
    return [x for x in xs]


def _get(api: SlidesService, pid: str) -> JsonObject:
    from beamer2slides.google_types import as_json
    from beamer2slides.gslides import execute
    return as_json(execute(api.presentations().get(presentationId=pid)), pid)


class LiveDeck:
    """A presentation being edited: the API and the latest read-back.

    Made by `open_deck`, every `batch` is sent at once and the deck read again, so `model` is always
    the deck as it stands. `defer=True` is the fuzzer's cheaper way (devtools/fuzz_sync.py): a batch
    is queued, `model` stays the read it was, and `dirty` / `reshaped` say what the queued requests
    touched - the slides whose objects or text they change, and whether slides came, went or moved
    (which is what makes an index stale). An edit found by content on a slide nothing queued has
    touched is found exactly as a fresh read would find it, so the caller only has to `flush()` and
    `read()` before an edit on a dirty slide. `flush` sends the queue as one batchUpdate and, when
    Google refuses it, each queued edit alone, so one edit that no longer fits costs only itself.
    No edit reads the model after its own batch: every expectation is worked out from the read
    the edit was found in, which is what makes the two ways give the same answers."""

    def __init__(self, pid: str, api: SlidesService, pres: JsonObject, defer: bool) -> None:
        self.pid, self.api, self.defer = pid, api, defer
        self.pending: list[list[SlidesRequest]] = []
        self.dirty: set[str] = set()
        self.reshaped = False
        self.reads = 0
        self.writes = 0
        self.model = Model(pres)

    def read(self) -> Model:
        if self.pending:
            self.flush()
        self.reads += 1
        return self.adopt(_get(self.api, self.pid))

    def adopt(self, pres: JsonObject) -> Model:
        """Take a presentations.get somebody else made of this deck as the current read."""
        self.model = Model(pres)
        self.dirty, self.reshaped = set(), False
        return self.model

    def batch(self, requests: list[SlidesRequest]) -> BatchUpdateResponse:
        from beamer2slides.gslides import execute
        if self.defer:
            self.pending.append(requests)
            self._touched(requests)
            return BatchUpdateResponse()
        self.writes += 1
        done = execute(self.api.presentations().batchUpdate(presentationId=self.pid, body={"requests": requests}))
        self.read()
        return done

    def flush(self) -> list[int]:
        """Send what `batch` queued. Returns the indices (in queue order) of the batches Google
        refused, which were not applied; everything else was."""
        from beamer2slides.gapi import HttpError
        from beamer2slides.gslides import execute
        queue = self.pending
        self.pending = []
        if not queue:
            return []

        def send(reqs: list[SlidesRequest]) -> None:
            self.writes += 1
            execute(self.api.presentations().batchUpdate(presentationId=self.pid, body={"requests": reqs}))
        try:
            send([r for reqs in queue for r in reqs])
            return []
        except HttpError:
            if len(queue) == 1:
                return [0]
        refused: list[int] = []   # (a refused batch changed nothing: each edit is sent again on its own)
        for i, reqs in enumerate(queue):
            try:
                send(reqs)
            except HttpError:
                refused.append(i)
        return refused

    def _touched(self, requests: list[SlidesRequest]) -> None:
        """Mark the slides `requests` change (and `reshaped` for slides added, removed or moved)."""
        slide_ids = {s.id for s in self.model.slides}
        where = {e.id: s.id for s in self.model.slides for e in s.elements}
        for r in requests:
            name = slides_request_kind(r)
            for oid in _named(r):
                if not oid:
                    continue
                if oid in slide_ids:
                    self.dirty.add(oid)
                    if name in ("deleteObject", "duplicateObject"):
                        self.reshaped = True
                elif oid in where:
                    self.dirty.add(where[oid])
            if name in ("createSlide", "updateSlidesPosition"):
                self.reshaped = True


def _made_on(props: SlidesPageElementProperties | None) -> list[str | None]:
    """The page a created element goes on (none said: none)."""
    return [] if props is None else [props["pageObjectId"]]


def _named(r: SlidesRequest) -> list[str | None]:
    """The objects a request names: the one it changes or makes, the page it makes an element on,
    a group's children, a duplication's originals (its `objectIds` map, by the ids it copies), the
    slides it moves, the elements it restacks."""
    if "createImage" in r:
        return [r["createImage"].get("objectId"), *_made_on(r["createImage"].get("elementProperties"))]
    if "createLine" in r:
        return [r["createLine"].get("objectId"), *_made_on(r["createLine"].get("elementProperties"))]
    if "createShape" in r:
        return [r["createShape"].get("objectId"), *_made_on(r["createShape"].get("elementProperties"))]
    if "createTable" in r:
        return [r["createTable"].get("objectId"), *_made_on(r["createTable"].get("elementProperties"))]
    if "createSlide" in r:
        return [r["createSlide"].get("objectId")]
    if "duplicateObject" in r:
        return [r["duplicateObject"]["objectId"], *r["duplicateObject"].get("objectIds", {})]
    if "groupObjects" in r:
        return [r["groupObjects"].get("groupObjectId"), *r["groupObjects"]["childrenObjectIds"]]
    if "ungroupObjects" in r:
        return [*r["ungroupObjects"]["objectIds"]]
    if "updateSlidesPosition" in r:
        return [*r["updateSlidesPosition"]["slideObjectIds"]]
    if "updatePageElementsZOrder" in r:
        return [*r["updatePageElementsZOrder"]["pageElementObjectIds"]]
    if "replaceImage" in r:
        return [r["replaceImage"]["imageObjectId"]]
    if "insertTableRows" in r:
        return [r["insertTableRows"]["tableObjectId"]]
    if "insertTableColumns" in r:
        return [r["insertTableColumns"]["tableObjectId"]]
    if "deleteTableRow" in r:
        return [r["deleteTableRow"]["tableObjectId"]]
    if "deleteTableColumn" in r:
        return [r["deleteTableColumn"]["tableObjectId"]]
    return [_changed(r)]


def _changed(r: SlidesRequest) -> str:
    """The object a request of the kinds that change one object names (`objectId`)."""
    if "deleteObject" in r:
        return r["deleteObject"]["objectId"]
    if "deleteText" in r:
        return r["deleteText"]["objectId"]
    if "insertText" in r:
        return r["insertText"]["objectId"]
    if "updateImageProperties" in r:
        return r["updateImageProperties"]["objectId"]
    if "updateLineProperties" in r:
        return r["updateLineProperties"]["objectId"]
    if "updatePageElementAltText" in r:
        return r["updatePageElementAltText"]["objectId"]
    if "updatePageElementTransform" in r:
        return r["updatePageElementTransform"]["objectId"]
    if "updatePageProperties" in r:
        return r["updatePageProperties"]["objectId"]
    if "updateShapeProperties" in r:
        return r["updateShapeProperties"]["objectId"]
    if "updateSlideProperties" in r:
        return r["updateSlideProperties"]["objectId"]
    if "updateTableBorderProperties" in r:
        return r["updateTableBorderProperties"]["objectId"]
    if "updateTableCellProperties" in r:
        return r["updateTableCellProperties"]["objectId"]
    if "updateTableRowProperties" in r:
        return r["updateTableRowProperties"]["objectId"]
    if "updateTableColumnProperties" in r:
        return r["updateTableColumnProperties"]["objectId"]
    if "createParagraphBullets" in r:
        return r["createParagraphBullets"]["objectId"]
    if "deleteParagraphBullets" in r:
        return r["deleteParagraphBullets"]["objectId"]
    if "mergeTableCells" in r:
        return r["mergeTableCells"]["objectId"]
    if "updateParagraphStyle" in r:
        return r["updateParagraphStyle"]["objectId"]
    if "updateTextStyle" in r:
        return r["updateTextStyle"]["objectId"]
    raise ValueError(f"a Slides request of {sorted(r)} names no object `_named` reads")


def open_deck(pid: str, *, defer: bool) -> LiveDeck:
    """The deck as it stands, through the owner's own client (`LiveDeck`; `defer`: queue the edits)."""
    from beamer2slides.google_auth import slides_service
    api = slides_service(None)
    deck = LiveDeck(pid, api, _get(api, pid), defer)
    deck.reads += 1   # (the read it was opened with)
    return deck


def new_id() -> str:
    return "u" + uuid.uuid4().hex[:16]


def rgb(color: str) -> OpaqueColor:
    """`#rrggbb` as a request's colour."""
    return {"rgbColor": {"red": int(color[1:3], 16) / 255, "green": int(color[3:5], 16) / 255,
                         "blue": int(color[5:7], 16) / 255}}


EditName = Literal["replace_word", "append_sentence", "add_paragraph", "insert_before_hole", "delete_paragraph",
                   "insert_table_row", "insert_table_column", "bold", "recolour", "resize_font", "move", "resize",
                   "delete_element", "delete_group", "add_text_box", "add_shape", "add_image", "duplicate", "group",
                   "ungroup", "add_slide", "duplicate_slide", "delete_slide", "move_slide", "set_notes",
                   "set_background"]
EDITS: tuple[EditName, ...] = (
    "replace_word", "append_sentence", "add_paragraph", "insert_before_hole", "delete_paragraph",
    "insert_table_row", "insert_table_column", "bold", "recolour", "resize_font", "move", "resize",
    "delete_element", "delete_group", "add_text_box", "add_shape", "add_image", "duplicate", "group", "ungroup",
    "add_slide", "duplicate_slide", "delete_slide", "move_slide", "set_notes", "set_background")


def edit_name(v: Json) -> EditName:
    for name in EDITS:
        if v == name:
            return name
    raise CheckError(f"unknown edit {v!r}")


@dataclass(frozen=True, kw_only=True)
class Expectation:
    """What an edit did and what must hold after it: its kind and arguments (as the JSON spec gave
    them), the selectors of the slides it touched, and sync_check checks."""
    edit: EditName
    args: JsonObject
    slides: tuple[Json, ...]
    checks: tuple[JsonObject, ...]

    def json(self) -> JsonObject:
        return {"edit": self.edit, "args": self.args, "slides": _jlist(self.slides), "checks": _jlist(self.checks)}


def expectation(edit: EditName, args: JsonObject, slides: Sequence[Json], checks: Sequence[JsonObject]) -> Expectation:
    return Expectation(edit=edit, args=args, slides=tuple(slides), checks=tuple(checks))


def locate(deck: LiveDeck, slide: Json, phrase: str) -> tuple[Slide, Element, str, Cell | None, tuple[int, int]]:
    """(slide, element, raw text, cell, (start, end)) of the one text holding `phrase`."""
    s = deck.model.one(slide)
    el = deck.model.element(s, {"text": phrase})
    for raw, cell in el.texts:
        span = phrase_span(raw, phrase)
        if span:
            return s, el, raw, cell, span
    raise CheckError(f"{phrase!r} not found in {el.id}")


def _location(cell: Cell) -> SlidesTableCellLocation:
    """The cell as a request's `cellLocation`."""
    return {"rowIndex": cell.row, "columnIndex": cell.column}


def _range(raw: str, a: int, b: int) -> SlidesRange:
    return {"type": "FIXED_RANGE", "startIndex": utf16(raw, a), "endIndex": utf16(raw, b)}


def _delete_text(el: Element, cell: Cell | None, text_range: SlidesRange) -> SlidesRequest:
    """Delete `text_range` of the element's text (of its cell `cell`, in a table)."""
    if cell:
        return {"deleteText": {"objectId": el.id, "cellLocation": _location(cell), "textRange": text_range}}
    return {"deleteText": {"objectId": el.id, "textRange": text_range}}


def _insert_text(el: Element, cell: Cell | None, text: str, index: int) -> SlidesRequest:
    """Type `text` at `index` (UTF-16) of the element's text (of its cell `cell`, in a table)."""
    body: SlidesInsertTextRequest = {"objectId": el.id, "cellLocation": _location(cell), "text": text,
                                     "insertionIndex": index} if cell else \
        {"objectId": el.id, "text": text, "insertionIndex": index}
    return {"insertText": body}


def _text_style(el: Element, cell: Cell | None, text_range: SlidesRange, style: SlidesTextStyle,
                fields: str) -> SlidesRequest:
    """Style `text_range` of the element's text (of its cell `cell`, in a table)."""
    body: SlidesUpdateTextStyleRequest = {
        "objectId": el.id, "cellLocation": _location(cell), "textRange": text_range, "style": style,
        "fields": fields} if cell else \
        {"objectId": el.id, "textRange": text_range, "style": style, "fields": fields}
    return {"updateTextStyle": body}


def _word(raw: str, span: tuple[int, int], word: str) -> tuple[int, int]:
    m = re.search(rf"(?<!\w){re.escape(word)}(?!\w)", raw[span[0]:span[1]])
    if not m:
        raise CheckError(f"{word!r} not in {raw[span[0]:span[1]]!r}")
    return span[0] + m.start(), span[0] + m.end()


def _target_text(target: Json) -> str | None:
    """The words a target finds its element by (None: it finds it otherwise)."""
    if isinstance(target, dict) and "text" in target:
        return as_str(target["text"], "target text")
    return None


def _target_check(center: Point, target: Json) -> JsonObject:
    """A target that finds the element again after sync: its text, or the picture's centre (where
    the edit puts it - worked out, not read back: see `LiveDeck`)."""
    text = _target_text(target)
    if text is not None:
        return {"text": text}
    return {"image_near": _jnums([round(v, 1) for v in center])}


# ---------------------------------------------------------------- text

def replace_word(deck: LiveDeck, slide: Json, text: str, old: str, new: str) -> Expectation:
    """Replace one word inside the phrase `text` (select it, type over it)."""
    s, el, raw, cell, span = locate(deck, slide, text)
    a, b = _word(raw, span, old)
    after = norm(raw[span[0]:a] + new + raw[b:span[1]])
    deck.batch([_delete_text(el, cell, _range(raw, a, b)), _insert_text(el, cell, new, utf16(raw, a))])
    return expectation("replace_word", {"slide": slide, "text": text, "old": old, "new": new}, [slide],
                       [{"check": "text", "slide": slide, "text": after, "count": 1},
                        {"check": "text", "slide": slide, "text": text, "count": 0}])


def append_sentence(deck: LiveDeck, slide: Json, text: str, sentence: str) -> Expectation:
    """Type a sentence at the end of the paragraph holding `text`."""
    s, el, raw, cell, span = locate(deck, slide, text)
    end = raw.find("\n", span[1])
    end = len(raw) if end < 0 else end
    deck.batch([_insert_text(el, cell, " " + sentence, utf16(raw, end))])
    return expectation("append_sentence", {"slide": slide, "text": text, "sentence": sentence}, [slide],
                       [{"check": "text", "slide": slide, "text": f"{text} {sentence}", "count": 1}])


def add_paragraph(deck: LiveDeck, slide: Json, text: str, paragraph: str) -> Expectation:
    """Press Enter at the end of the paragraph holding `text` and type a new one: the box's text
    grows by a line (a new bullet, in a list) while the box keeps the size the converter gave it."""
    s, el, raw, cell, span = locate(deck, slide, text)
    end = raw.find("\n", span[1])
    end = len(raw.rstrip("\n")) if end < 0 else end
    deck.batch([_insert_text(el, cell, "\n" + paragraph, utf16(raw, end))])
    return expectation("add_paragraph", {"slide": slide, "text": text, "paragraph": paragraph}, [slide],
                       [{"check": "text", "slide": slide, "text": paragraph, "count": 1},
                        {"check": "text", "slide": slide, "text": text, "count": 1}])


HOLE = re.compile("​?\xa0+")  # (with the zero-width break emit writes in front of it, emit.HOLE_BREAK)


def insert_before_hole(deck: LiveDeck, slide: Json, text: str, words: str) -> Expectation:
    """Type `words` right in front of the first inline-formula hole of the paragraph holding `text`
    (a hole is a run of no-break spaces with the formula's picture over it, emit's `holes`): every
    letter typed there moves the hole, and the picture is placed by where the hole was."""
    s, el, raw, cell, span = locate(deck, slide, text)
    start, end = raw.rfind("\n", 0, span[0]) + 1, raw.find("\n", span[1])
    end = len(raw) if end < 0 else end
    hole = HOLE.search(raw, start, end)
    if not hole:
        raise CheckError(f"no formula hole in the paragraph holding {text!r}")
    before = raw[start:hole.start()].split()
    deck.batch([_insert_text(el, cell, words + " ", utf16(raw, hole.start()))])
    checks: list[JsonObject] = [{"check": "text", "slide": slide, "text": words, "count": 1}]
    if before:
        checks.append({"check": "text", "slide": slide, "text": f"{before[-1]} {words}", "count": 1})
    return expectation("insert_before_hole", {"slide": slide, "text": text, "words": words}, [slide], checks)


def _table_cell(deck: LiveDeck, slide: Json, text: str, nth: int | None) -> tuple[Element, Cell]:
    """(table, cell) of the cell holding `text`; with several tables holding it (a copy the person
    made), `nth` picks one, top to bottom."""
    s = deck.model.one(slide)
    tables = sorted((e for e in s.elements if e.kind == "table" and norm(text) in e.text), key=lambda e: (e.box[1], e.box[0]))
    if nth is None and len(tables) != 1 or nth is not None and nth >= len(tables):
        raise CheckError(f"slide {s.index + 1}: {len(tables)} tables hold {text!r}")
    el = tables[nth or 0]
    cell = next((c for raw, c in el.texts if c and phrase_span(raw, text)), None)
    if not cell:
        raise CheckError(f"{text!r} is not inside one cell")
    return el, cell


def _cell_text(el: Element, cell: Cell, text: str) -> SlidesRequest:
    """Type `text` into an empty cell."""
    return _insert_text(el, cell, text, 0)


def insert_table_row(deck: LiveDeck, slide: Json, text: str, cells: Sequence[str], nth: int | None) -> Expectation:
    """Insert a row below the one holding `text` (right-click > Insert row below) and type `cells`
    into it, left to right: the table grows down by a row towards whatever is under it. `nth`: which
    of several tables holding `text` (None: there must be one)."""
    el, cell = _table_cell(deck, slide, text, nth)
    row, cols = cell.row + 1, el.table_size[1]
    insert: SlidesRequest = {"insertTableRows": {"tableObjectId": el.id, "cellLocation": _location(cell),
                                                 "insertBelow": True, "number": 1}}
    typed: list[SlidesRequest] = [_cell_text(el, Cell(row=row, column=i), t) for i, t in enumerate(cells[:cols]) if t]
    deck.batch([insert, *typed])
    return expectation("insert_table_row", {"slide": slide, "text": text, "cells": _jlist(cells), "nth": nth}, [slide],
                       [{"check": "text", "slide": slide, "text": t, "count": 1} for t in cells[:cols] if t])


def insert_table_column(deck: LiveDeck, slide: Json, text: str, cells: Sequence[str], nth: int | None) -> Expectation:
    """Insert a column right of the one holding `text` and type `cells` into it, top to bottom:
    the table grows to the right, over whatever is beside it. `nth` as for `insert_table_row`."""
    el, cell = _table_cell(deck, slide, text, nth)
    col, rows = cell.column + 1, el.table_size[0]
    insert: SlidesRequest = {"insertTableColumns": {"tableObjectId": el.id, "cellLocation": _location(cell),
                                                    "insertRight": True, "number": 1}}
    typed: list[SlidesRequest] = [_cell_text(el, Cell(row=i, column=col), t) for i, t in enumerate(cells[:rows]) if t]
    deck.batch([insert, *typed])
    return expectation("insert_table_column", {"slide": slide, "text": text, "cells": _jlist(cells), "nth": nth}, [slide],
                       [{"check": "text", "slide": slide, "text": t, "count": 1} for t in cells[:rows] if t])


def delete_paragraph(deck: LiveDeck, slide: Json, text: str) -> Expectation:
    """Delete the paragraph (bullet) holding `text`; its neighbours stay."""
    s, el, raw, cell, span = locate(deck, slide, text)
    start = raw.rfind("\n", 0, span[0]) + 1
    end = raw.find("\n", span[1])
    end = len(raw) - 1 if end < 0 else end
    paragraphs = [p for p in raw.split("\n") if norm(p) and norm(text) not in norm(p)]
    a, b = (start, end + 1) if end < len(raw) - 1 else (max(0, start - 1), end)
    deck.batch([_delete_text(el, cell, _range(raw, a, b))])
    checks: list[JsonObject] = [{"check": "text", "slide": slide, "text": text, "count": 0}]
    if paragraphs:
        checks.append({"check": "text", "slide": slide, "text": norm(paragraphs[0]), "count": 1})
    return expectation("delete_paragraph", {"slide": slide, "text": text}, [slide], checks)


# ---------------------------------------------------------------- style

def _style(deck: LiveDeck, name: Literal["bold", "recolour"], slide: Json, word: str, context: str | None,
           style: SlidesTextStyle, fields: str, check: JsonObject, more_args: JsonObject) -> Expectation:
    s, el, raw, cell, span = locate(deck, slide, context or word)
    a, b = _word(raw, span, word) if context else span
    deck.batch([_text_style(el, cell, _range(raw, a, b), style, fields)])
    args: JsonObject = {"slide": slide, "word": word, "context": context, **more_args}
    return expectation(name, args, [slide], [{"check": "style", "slide": slide, "text": word,
                                              **({"context": context} if context else {}), **check}])


def bold(deck: LiveDeck, slide: Json, word: str, context: str | None) -> Expectation:
    """Make `word` bold (the first whole word of it inside the phrase `context`; None: `word` is a
    phrase of its own)."""
    return _style(deck, "bold", slide, word, context, {"bold": True}, "bold", {"bold": True}, {})


def recolour(deck: LiveDeck, slide: Json, word: str, color: str, context: str | None) -> Expectation:
    """Give `word` (as `bold` finds it) the colour `color` (#rrggbb)."""
    return _style(deck, "recolour", slide, word, context, {"foregroundColor": {"opaqueColor": rgb(color)}},
                  "foregroundColor", {"color": color.lower()}, {"color": color})


def resize_font(deck: LiveDeck, slide: Json, text: str, size: float) -> Expectation:
    """Set the font size of the whole paragraph holding `text`."""
    s, el, raw, cell, span = locate(deck, slide, text)
    start, end = raw.rfind("\n", 0, span[0]) + 1, raw.find("\n", span[1])
    end = len(raw) if end < 0 else end
    deck.batch([_text_style(el, cell, _range(raw, start, end), {"fontSize": {"magnitude": size, "unit": "PT"}},
                            "fontSize")])
    return expectation("resize_font", {"slide": slide, "text": text, "size": size}, [slide],
                       [{"check": "style", "slide": slide, "text": text, "size": size}])


# ---------------------------------------------------------------- geometry

def _relative(oid: str, sx: float, sy: float, dx: float, dy: float) -> SlidesRequest:
    transform: AffineTransform = {"scaleX": sx, "shearX": 0, "translateX": dx * EMU_PER_PT,
                                  "shearY": 0, "scaleY": sy, "translateY": dy * EMU_PER_PT, "unit": "EMU"}
    return {"updatePageElementTransform": {"objectId": oid, "applyMode": "RELATIVE", "transform": transform}}


def move(deck: LiveDeck, slide: Json, target: Json, dx: float, dy: float) -> Expectation:
    """Drag an element (a grouped one moves with its group, as a click selects the group)."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    x0, y0 = el.box[0], el.box[1]
    deck.batch([_relative(el.top, 1, 1, dx, dy)])
    return expectation("move", {"slide": slide, "target": target, "dx": dx, "dy": dy}, [slide],
                       [{"check": "box", "slide": slide, "target": _target_check((el.center[0] + dx, el.center[1] + dy), target),
                         "origin": [round(x0 + dx, 2), round(y0 + dy, 2)]}])


def resize(deck: LiveDeck, slide: Json, target: Json, sx: float, sy: float) -> Expectation:
    """Resize an element (or its group) about its top-left corner."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    top = next(e for e in s.elements if e.id == el.top)
    x0, y0 = top.box[0], top.box[1]
    w, h = el.box[2] - el.box[0], el.box[3] - el.box[1]
    ox, oy = el.box[0] - x0, el.box[1] - y0
    deck.batch([_relative(el.top, sx, sy, x0 * (1 - sx), y0 * (1 - sy))])
    center = x0 + (el.center[0] - x0) * sx, y0 + (el.center[1] - y0) * sy
    return expectation("resize", {"slide": slide, "target": target, "sx": sx, "sy": sy}, [slide],
                       [{"check": "box", "slide": slide, "target": _target_check(center, target),
                         "origin": [round(x0 + ox * sx, 2), round(y0 + oy * sy, 2)],
                         "size": [round(w * sx, 2), round(h * sy, 2)]}])


# ---------------------------------------------------------------- objects

def delete_element(deck: LiveDeck, slide: Json, target: Json) -> Expectation:
    """Select an element (inside its group if need be) and delete it."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    deck.batch([{"deleteObject": {"objectId": el.id}}])
    text = _target_text(target)
    check: JsonObject = {"check": "text", "slide": slide, "text": text, "count": 0} if text is not None else \
        {"check": "image", "slide": slide, "near": _jnums([round(v, 1) for v in el.center]), "count": 0}
    return expectation("delete_element", {"slide": slide, "target": target}, [slide], [check])


def delete_group(deck: LiveDeck, slide: Json, target: Json) -> Expectation:
    """Click an element (which selects its outermost group) and delete: the whole group goes."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    if el.parent is None:
        raise CheckError(f"{target} is not in a group")
    top = next(e for e in s.elements if e.id == el.top)
    gone = [c for c in s.elements if c.groups[:1] == (top.id,) and c.kind != "group"]
    deck.batch([{"deleteObject": {"objectId": top.id}}])
    checks: list[JsonObject] = [{"check": "text", "slide": slide, "text": c.text[:60], "count": 0}
                                for c in gone if c.kind in ("shape", "table") and c.text]
    checks += [{"check": "image", "slide": slide, "near": _jnums([round(v, 1) for v in c.center]), "count": 0}
               for c in gone if c.kind == "image"]
    return expectation("delete_group", {"slide": slide, "target": target}, [slide], checks)


def _props(page_id: str, box: Sequence[float]) -> SlidesPageElementProperties:
    x, y, w, h = box
    return {"pageObjectId": page_id, "size": {"width": {"magnitude": w * EMU_PER_PT, "unit": "EMU"},
                                              "height": {"magnitude": h * EMU_PER_PT, "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x * EMU_PER_PT, "translateY": y * EMU_PER_PT,
                          "unit": "EMU"}}


def add_text_box(deck: LiveDeck, slide: Json, text: str, box: Sequence[float]) -> Expectation:
    """Draw a text box ([x, y, w, h] pt) and type into it."""
    s, oid = deck.model.one(slide), new_id()
    deck.batch([{"createShape": {"objectId": oid, "shapeType": "TEXT_BOX", "elementProperties": _props(s.id, box)}},
                {"insertText": {"objectId": oid, "text": text}}])
    return expectation("add_text_box", {"slide": slide, "text": text, "box": _jnums(box)}, [slide],
                       [{"check": "text", "slide": slide, "text": text, "count": 1},
                        {"check": "box", "slide": slide, "target": {"text": text}, "origin": _jnums([round(v, 2) for v in box[:2]])}])


def add_shape(deck: LiveDeck, slide: Json, shape_type: str, box: Sequence[float], color: str) -> Expectation:
    if not is_shape_type(shape_type):
        raise CheckError(f"{shape_type!r} is no Slides shape type")
    s, oid = deck.model.one(slide), new_id()
    deck.batch([{"createShape": {"objectId": oid, "shapeType": shape_type, "elementProperties": _props(s.id, box)}},
                {"updateShapeProperties": {"objectId": oid, "fields": "shapeBackgroundFill.solidFill.color",
                                           "shapeProperties": {"shapeBackgroundFill": {"solidFill": {"color": rgb(color)}}}}}])
    return expectation("add_shape", {"slide": slide, "shape_type": shape_type, "box": _jnums(box), "color": color}, [slide],
                       [{"check": "shape", "slide": slide, "shape_type": shape_type, "color": color.lower(),
                         "near": [round(box[0] + box[2] / 2, 1), round(box[1] + box[3] / 2, 1)], "count": 1}])


def donor_image_url(api: SlidesService, pid: str) -> str:
    """contentUrl of a picture in another deck (createImage accepts it; it expires after a while)."""
    from beamer2slides.gslides import execute
    return donor_from(execute(api.presentations().get(presentationId=pid, fields="slides(pageElements)")), pid)


def donor_from(pres: Presentation, name: str) -> str:
    """contentUrl of the first picture of a presentations.get already made (`name`: the deck, for
    the message when it has none)."""
    for s in pres.get("slides", []):
        stack = list(s.get("pageElements", []))
        while stack:
            e = stack.pop(0)
            url = image_url(e)
            if url:
                return url
            stack += children(e, name)
    raise CheckError(f"no picture in {name}")


def add_image(deck: LiveDeck, slide: Json, url: str, box: Sequence[float]) -> Expectation:
    s, oid = deck.model.one(slide), new_id()
    deck.batch([{"createImage": {"objectId": oid, "url": url, "elementProperties": _props(s.id, box)}}])
    # (createImage letterboxes a picture into the box it is given, about the box's centre)
    return expectation("add_image", {"slide": slide, "box": _jnums(box)}, [slide],
                       [{"check": "image", "slide": slide, "near": [round(box[0] + box[2] / 2, 1), round(box[1] + box[3] / 2, 1)],
                         "count": 1}])


def duplicate(deck: LiveDeck, slide: Json, target: Json, dx: float, dy: float) -> Expectation:
    """Ctrl+D on the element (or its group) and drag the copy (dx, dy) pt away."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    text = _target_text(target)
    oid = new_id()
    deck.batch([{"duplicateObject": {"objectId": el.top, "objectIds": {el.top: oid}}}, _relative(oid, 1, 1, dx, dy)])
    checks: list[JsonObject]
    if text is not None:
        n = sum(norm(t).count(norm(text)) for e in s.elements for t, _ in e.texts)
        checks = [{"check": "text", "slide": slide, "text": text, "count": n * 2}]
    else:
        x, y = round(el.center[0], 1), round(el.center[1], 1)
        checks = [{"check": "image", "slide": slide, "near": [x, y], "count": 1},
                  {"check": "image", "slide": slide, "near": [round(x + dx, 1), round(y + dy, 1)], "count": 1}]
    return expectation("duplicate", {"slide": slide, "target": target, "dx": dx, "dy": dy}, [slide], checks)


def _member(el: Element) -> JsonObject:
    near = _jnums([round(v, 1) for v in el.center])
    return {"text": el.text[:60], "near": near} if el.texts and el.text else {"image_near": near}


def group(deck: LiveDeck, slide: Json, targets: Sequence[Json]) -> Expectation:
    """Select several top-level elements and group them."""
    s = deck.model.one(slide)
    els = [deck.model.element(s, t) for t in targets]
    tops = list(dict.fromkeys(e.top for e in els))
    deck.batch([{"groupObjects": {"groupObjectId": new_id(), "childrenObjectIds": tops}}])
    members = [_member(e) for e in els]   # (grouping moves nothing: text and centres stay)
    return expectation("group", {"slide": slide, "targets": _jlist(targets)}, [slide],
                       [{"check": "grouped", "slide": slide, "members": _jlist(members), "grouped": True}])


def ungroup(deck: LiveDeck, slide: Json, target: Json) -> Expectation:
    """Ungroup the group holding the target."""
    s = deck.model.one(slide)
    el = deck.model.element(s, target)
    parent = el.parent
    if parent is None:
        raise CheckError(f"{target} is not in a group")
    members = [_member(c) for c in s.elements if c.parent == parent and c.kind != "group"]
    deck.batch([{"ungroupObjects": {"objectIds": [parent]}}])
    return expectation("ungroup", {"slide": slide, "target": target}, [slide],
                       [{"check": "grouped", "slide": slide, "members": _jlist(members), "grouped": False}])


# ---------------------------------------------------------------- slides

def _title_placeholder(s: Slide) -> tuple[PlaceholderType, int] | None:
    """The slide's title placeholder as (type, index), or None. `createSlide` can only map a
    placeholder the layout really has - it answers *"The placeholder (15_0_0) is not on the page"*
    and refuses the whole batch otherwise - and a converted deck has layouts without a plain TITLE:
    the title page's carries CENTERED_TITLE, and a "(no theme)" copy may carry neither."""
    for e in s.elements:
        kind = e.placeholder_type
        if kind is not None and is_placeholder_type(kind) and kind in ("TITLE", "CENTERED_TITLE"):
            return kind, as_int(part(e.shape.get("placeholder"), "placeholder").get("index", 0), "placeholder.index")
    return None


def add_slide(deck: LiveDeck, after: Json, title: str, body: str | None) -> Expectation:
    """A new slide after `after`, with a title (and a text box holding `body`, unless None). It takes
    `after`'s layout when that one offers a title placeholder to write in, else the layout of a slide
    that does - which is what a person does too, and what keeps the slide findable by its title
    afterwards. Without it, every such edit after the title page was refused by the API and silently
    dropped from the round."""
    s = deck.model.one(after)
    host = s if _title_placeholder(s) else next((x for x in deck.model.slides if _title_placeholder(x)), s)
    title_placeholder: tuple[PlaceholderType, int] = _title_placeholder(host) or ("TITLE", 0)
    kind, index = title_placeholder
    props = host.obj.get("slideProperties")
    layout = None if props is None else props.get("layoutObjectId")
    if layout is None:
        raise CheckError(f"slide {host.index + 1} names no layout")
    sid, tid = new_id(), new_id()
    reqs: list[SlidesRequest] = [
        {"createSlide": {"objectId": sid, "insertionIndex": s.index + 1, "slideLayoutReference": {"layoutId": layout},
                         "placeholderIdMappings": [{"layoutPlaceholder": {"type": kind, "index": index}, "objectId": tid}]}},
        {"insertText": {"objectId": tid, "text": title}}]
    if body:
        bid = new_id()
        box: list[SlidesRequest] = [
            {"createShape": {"objectId": bid, "shapeType": "TEXT_BOX", "elementProperties": _props(sid, [40, 120, 600, 60])}},
            {"insertText": {"objectId": bid, "text": body}}]
        reqs += box
    deck.batch(reqs)
    sel: JsonObject = {"title": title}
    checks: list[JsonObject] = [{"check": "slides", "order": [after, sel]}, {"check": "slide_count", "slide": sel, "count": 1}]
    if body:
        checks.append({"check": "text", "slide": sel, "text": body, "count": 1})
    return expectation("add_slide", {"after": after, "title": title, "body": body}, [sel], checks)


def duplicate_slide(deck: LiveDeck, slide: Json, new_title: str | None) -> Expectation:
    """Duplicate a slide (the copy comes right after it), retitling the copy `new_title` (None: not)."""
    s = deck.model.one(slide)
    sid = new_id()
    if not new_title:
        deck.batch([{"duplicateObject": {"objectId": s.id, "objectIds": {s.id: sid}}}])
        return expectation("duplicate_slide", {"slide": slide}, [],
                           [{"check": "slide_count", "slide": slide, "count": 2}])
    # The copy's title is named in the duplication itself (`objectIds` maps the children of what is
    # duplicated too), so the retitling goes in the same batch and needs no read of the copy.
    title = next(e for e in s.elements if e.placeholder_type in ("TITLE", "CENTERED_TITLE"))
    tid, raw = new_id(), title.texts[0][0]
    first = raw.split("\n")[0]
    deck.batch([{"duplicateObject": {"objectId": s.id, "objectIds": {s.id: sid, title.id: tid}}},
                {"deleteText": {"objectId": tid, "textRange": _range(raw, 0, len(first))}},
                {"insertText": {"objectId": tid, "text": new_title, "insertionIndex": 0}}])
    sel: JsonObject = {"title": new_title}
    return expectation("duplicate_slide", {"slide": slide, "new_title": new_title}, [slide, sel],
                       [{"check": "slides", "order": [slide, sel], "adjacent": True},
                        {"check": "slide_count", "slide": sel, "count": 1}])


def delete_slide(deck: LiveDeck, slide: Json) -> Expectation:
    s = deck.model.one(slide)
    deck.batch([{"deleteObject": {"objectId": s.id}}])
    return expectation("delete_slide", {"slide": slide}, [], [{"check": "slide_count", "slide": slide, "count": 0}])


def move_slide(deck: LiveDeck, slide: Json, after: Json) -> Expectation:
    """Drag a slide in the filmstrip to right after `after`."""
    s, a = deck.model.one(slide), deck.model.one(after)
    deck.batch([{"updateSlidesPosition": {"slideObjectIds": [s.id], "insertionIndex": a.index + 1}}])
    return expectation("move_slide", {"slide": slide, "after": after}, [slide],
                       [{"check": "slides", "order": [after, slide], "adjacent": True}])


def set_notes(deck: LiveDeck, slide: Json, text: str) -> Expectation:
    """Replace the speaker notes."""
    s = deck.model.one(slide)
    shape = s.notes_shape()
    if shape is None:
        raise CheckError(f"slide {s.index + 1} ({s.title}) has no speaker notes shape")
    oid = object_id(shape)
    reqs: list[SlidesRequest] = [{"deleteText": {"objectId": oid, "textRange": {"type": "ALL"}}}] \
        if norm(raw_text(text_elements(part(shape.get("shape"), "shape")))) else []
    typed: SlidesRequest = {"insertText": {"objectId": oid, "text": text, "insertionIndex": 0}}
    deck.batch([*reqs, typed])
    return expectation("set_notes", {"slide": slide, "text": text}, [slide],
                       [{"check": "notes", "slide": slide, "text": text}])


def set_background(deck: LiveDeck, slide: Json, color: str) -> Expectation:
    s = deck.model.one(slide)
    deck.batch([{"updatePageProperties": {"objectId": s.id, "fields": "pageBackgroundFill.solidFill.color",
                                          "pageProperties": {"pageBackgroundFill": {"solidFill": {"color": rgb(color)}}}}}])
    return expectation("set_background", {"slide": slide, "color": color}, [slide],
                       [{"check": "background", "slide": slide, "color": color.lower()}])


# ---------------------------------------------------------------- edits as JSON

DUPLICATE_OFFSET: float = 12   # pt: where `duplicate` drags the copy when the spec says nothing (JSON `12`)


def apply(deck: LiveDeck, spec: JsonObject) -> Expectation:
    """Run one edit given as {"edit": name, "args": {...}}."""
    name = edit_name(spec.get("edit"))
    args = as_object(spec.get("args"), f"{name} args")

    def arg(key: str) -> Json:
        if key not in args:
            raise CheckError(f"{name}: no {key!r}")
        return args[key]

    def text(key: str) -> str:
        return as_str(arg(key), f"{name} {key}")

    def optional_text(key: str) -> str | None:
        v = args.get(key)
        return None if v is None else as_str(v, f"{name} {key}")

    def num(key: str) -> float:
        return number(arg(key), f"{name} {key}")

    def nums(key: str) -> list[float]:
        return [number(v, f"{name} {key}") for v in as_array(arg(key), f"{name} {key}")]

    def strs(key: str) -> list[str]:
        return [as_str(v, f"{name} {key}") for v in as_array(arg(key), f"{name} {key}")]

    def nth() -> int | None:
        v = args.get("nth")
        return None if v is None else as_int(v, f"{name} nth")

    match name:
        case "replace_word":
            return replace_word(deck, arg("slide"), text("text"), text("old"), text("new"))
        case "append_sentence":
            return append_sentence(deck, arg("slide"), text("text"), text("sentence"))
        case "add_paragraph":
            return add_paragraph(deck, arg("slide"), text("text"), text("paragraph"))
        case "insert_before_hole":
            return insert_before_hole(deck, arg("slide"), text("text"), text("words"))
        case "delete_paragraph":
            return delete_paragraph(deck, arg("slide"), text("text"))
        case "insert_table_row":
            return insert_table_row(deck, arg("slide"), text("text"), strs("cells"), nth())
        case "insert_table_column":
            return insert_table_column(deck, arg("slide"), text("text"), strs("cells"), nth())
        case "bold":
            return bold(deck, arg("slide"), text("word"), optional_text("context"))
        case "recolour":
            return recolour(deck, arg("slide"), text("word"), text("color"), optional_text("context"))
        case "resize_font":
            return resize_font(deck, arg("slide"), text("text"), num("size"))
        case "move":
            return move(deck, arg("slide"), arg("target"), num("dx"), num("dy"))
        case "resize":
            sx = num("sx")
            return resize(deck, arg("slide"), arg("target"), sx, sx if args.get("sy") is None else num("sy"))
        case "delete_element":
            return delete_element(deck, arg("slide"), arg("target"))
        case "delete_group":
            return delete_group(deck, arg("slide"), arg("target"))
        case "add_text_box":
            return add_text_box(deck, arg("slide"), text("text"), nums("box"))
        case "add_shape":
            return add_shape(deck, arg("slide"), text("shape_type"), nums("box"), text("color"))
        case "add_image":
            return add_image(deck, arg("slide"), text("url"), nums("box"))
        case "duplicate":
            return duplicate(deck, arg("slide"), arg("target"), num("dx") if "dx" in args else DUPLICATE_OFFSET,
                             num("dy") if "dy" in args else DUPLICATE_OFFSET)
        case "group":
            return group(deck, arg("slide"), as_array(arg("targets"), f"{name} targets"))
        case "ungroup":
            return ungroup(deck, arg("slide"), arg("target"))
        case "add_slide":
            return add_slide(deck, arg("after"), text("title"), optional_text("body"))
        case "duplicate_slide":
            return duplicate_slide(deck, arg("slide"), optional_text("new_title"))
        case "delete_slide":
            return delete_slide(deck, arg("slide"))
        case "move_slide":
            return move_slide(deck, arg("slide"), arg("after"))
        case "set_notes":
            return set_notes(deck, arg("slide"), text("text"))
        case "set_background":
            return set_background(deck, arg("slide"), text("color"))
        case unreachable:
            assert_never(unreachable)


def _verified(deck: LiveDeck, spec: JsonObject) -> tuple[Expectation, list[str]]:
    before = deck.model
    exp = apply(deck, spec)
    problems = check_all(deck.model, exp.checks)
    if not check_all(before, exp.checks):
        problems.append(f"{exp.edit}: every check already held before the edit")
    return exp, [f"{exp.edit}: {p}" for p in problems]


def verified(deck: LiveDeck, spec: JsonObject) -> tuple[JsonObject, list[str]]:
    """Apply an edit and read it back: its checks must fail before (the edit changes something)
    and hold after. (expectation as JSON, problems)"""
    exp, problems = _verified(deck, spec)
    return exp.json(), problems


def catalogue(donor_url: str | None) -> list[JsonObject]:
    """Every edit kind once, on a deck converted from tests/decks/sync v1, not interfering."""
    why, algo, merging, conv, policy, results = ("Why decks and sources diverge", "The sync algorithm", "Merging text",
                                                  "Convergence", "Merge policy", "Results")
    versions, identity, concl = "Three versions", "Finding the same slide", "Conclusions"
    edits: list[tuple[EditName, JsonObject]] = [
        ("replace_word", {"slide": why, "text": "People polish the converted deck by hand", "old": "polish", "new": "refine"}),
        ("delete_paragraph", {"slide": why, "text": "adding their own slides"}),
        ("append_sentence", {"slide": merging, "text": "writes the merged paragraph back into the deck.",
                             "sentence": "Nothing is lost."}),
        ("insert_before_hole", {"slide": merging, "text": "The merge is clean when the changed words",
                                "words": "quite literally"}),
        ("add_paragraph", {"slide": algo, "text": "Merge and write the changes", "paragraph": "Check the result by hand"}),
        ("bold", {"slide": concl, "word": "survive", "context": "Deck edits survive every sync"}),
        ("recolour", {"slide": concl, "word": "both versions", "context": "Conflicts are reported with both versions",
                      "color": "#c00000"}),
        ("resize_font", {"slide": concl, "text": "Conflicts are reported with both versions", "size": 20}),
        ("move", {"slide": conv, "target": {"text": "Conflicts disappear once"}, "dx": 0, "dy": 40}),
        ("resize", {"slide": conv, "target": {"image": "largest"}, "sx": 0.8}),
        ("delete_element", {"slide": conv, "target": {"text": "open conflicts"}}),
        ("add_text_box", {"slide": results, "text": "Measured on the test decks", "box": [460, 60, 220, 30]}),
        ("add_shape", {"slide": results, "shape_type": "STAR_5", "box": [640, 100, 50, 50], "color": "#ffc000"}),
        ("duplicate", {"slide": results, "target": {"text": "Same element"}, "dx": 0, "dy": 110}),
        # (after the copy: two tables hold every cell's words, `nth` picks one from the top)
        ("insert_table_row", {"slide": results, "text": "Conflicts", "cells": ["Renames", "97%", "5.5 s"], "nth": 0}),
        ("insert_table_column", {"slide": results, "text": "Time", "cells": ["Repeats", "x12", "x9", "x30"], "nth": 1}),
        ("group", {"slide": policy, "targets": [{"text": "Both versions go into the report."}, {"text": "Deck edits win"}]}),
        ("ungroup", {"slide": algo, "target": {"text": "Read the base snapshot"}}),
        ("delete_group", {"slide": versions, "target": {"text": "Merged"}}),
        ("add_slide", {"after": versions, "title": "Reviewer questions", "body": "What happens to comments?"}),
        # ...and once after the title page, whose layout has no plain TITLE placeholder: the API
        # refuses a mapping for a placeholder the layout hasn't got, and the campaign lost every
        # such edit to that (`_title_placeholder`).
        ("add_slide", {"after": "Keeping Slides and Source in Sync", "title": "Agenda for today",
                       "body": "Written on the title page's own layout"}),
        ("duplicate_slide", {"slide": concl, "new_title": "Conclusions (short)"}),
        ("move_slide", {"slide": policy, "after": results}),
        ("delete_slide", {"slide": identity}),
        ("set_notes", {"slide": merging, "text": "Mention diff3 here."}),
        ("set_background", {"slide": merging, "color": "#fff2cc"}),
    ]
    if donor_url:
        edits.insert(12, ("add_image", {"slide": versions, "url": donor_url, "box": [540, 250, 150, 100]}))
    return [{"edit": name, "args": args} for name, args in edits]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck", help="presentation id, URL or converted out folder")
    ap.add_argument("command", choices=["catalogue", "apply"])
    ap.add_argument("spec", nargs="?", help="apply: the edit as JSON")
    ap.add_argument("--donor", help="presentation id of another deck to take a picture URL from")
    ap.add_argument("--out", type=Path, help="write the expectations here (JSON list)")
    args = ap.parse_args()
    command: str = args.command
    given: str | None = args.spec
    donor: str | None = args.donor
    out: Path | None = args.out
    deck = open_deck(presentation_id(args.deck), defer=False)
    if command == "apply":
        if given is None:
            ap.error("apply needs the edit")
        spec: Json = json.loads(Path(given[1:]).read_text(encoding="utf-8-sig") if given.startswith("@") else given)
        specs = as_objects(spec if isinstance(spec, list) else [spec], "the edits")
    else:
        specs = catalogue(donor_image_url(deck.api, donor) if donor else None)
    expectations: list[Expectation] = []
    problems: list[str] = []
    for one in specs:
        exp, bad = _verified(deck, one)
        expectations.append(exp)
        problems += bad
        print(f"{'FAIL' if bad else 'ok  '} {exp.edit}" + "".join(f"\n     {p}" for p in bad))
    final = check_all(deck.model, [c for e in expectations for c in e.checks])
    problems += [f"at the end: {p}" for p in final]
    print("".join(f"at the end: {p}\n" for p in final), end="")
    if out is not None:
        out.write_text(json.dumps([e.json() for e in expectations], indent=1, ensure_ascii=False), encoding="utf-8")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
