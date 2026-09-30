"""Test elements as `deck_ir_types` records, written as the few keys a test is about.

The thumbnail passes (`deck_fills`, `deck_freeforms`, `deck_thumbs`) take and hand back
`TargetElement` records. A test says only what its case needs (a bbox, a fill, one run's words),
so `record` fills in every other key `deck_ir` always writes with a plain value, then parses it
the one strict way (`parse_element`): a key no version of `deck_ir` writes is still refused.
`as_dict` is the JSON form again, for what still reads dicts (adopt).

What a test says is a `Given`: any mapping of JSON values (lists, tuples, nested mappings), taken
into `JsonObject` once at the entry (`_object`); a value JSON has no form for is refused there."""

import hashlib
import json
from collections.abc import Mapping, Sequence

from beamer2slides.deck_ir_types import (TableCell, TargetDeck, TargetElement, TargetParagraph, TargetRun,
                                         TargetTable, TargetText, element_json, parse_element, parse_target)
from beamer2slides.json_types import Json, JsonArray, JsonObject, as_array, as_int, as_object, as_objects, as_str

# What a test writes: the keys it is about, their values JSON (a tuple is an array).
Given = Mapping[str, object]

# The keys each kind takes as null. Any other key a test sets to None is one deck_ir leaves out.
_NULLABLE = {"text": {"group", "key", "placeholder", "shape_type", "outline_color"},
             "shape": {"group", "key", "fill", "outline", "weight", "line_type", "category"},
             "image": {"group", "key", "alt"},
             "table": {"group", "key"},
             "diagram": {"group", "key"}}


def _json(v: object, where: str) -> Json:
    """`v` as JSON: a mapping an object, a list or tuple an array."""
    if v is None:
        return None
    if isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Mapping):
        out: JsonObject = {}
        for k, x in v.items():
            if not isinstance(k, str):
                raise TypeError(f"{where}: key {k!r} is not a string")
            out[k] = _json(x, f"{where}.{k}")
        return out
    if isinstance(v, (list, tuple)):
        return [_json(x, f"{where}[{i}]") for i, x in enumerate(v)]
    raise TypeError(f"{where}: {type(v).__name__} has no JSON form")


def _object(d: Given, where: str) -> JsonObject:
    return as_object(_json(d, where), where)


def json_object(d: Given) -> JsonObject:
    """`d` as a `JsonObject` (a copy): for a test building a Google answer out of mappings it wrote."""
    return _object(d, "$")


def _objects(d: JsonObject, key: str) -> list[JsonObject]:
    """The objects under `key`, none when it is left out."""
    return as_objects(d[key], key) if key in d else []


def _number(v: Json, where: str) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise TypeError(f"{where}: a number was expected, found {v!r}")


def _pruned(d: JsonObject, nullable: set[str]) -> JsonObject:
    return {k: v for k, v in d.items() if v is not None or k in nullable}


def _run(r: JsonObject, size: Json) -> JsonObject:
    out: JsonObject = {"text": "", "font": "Arial", "family": "sans", "size": size, "bold": False, "italic": False,
                       "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False,
                       "strike": False, "highlight": None}
    out.update(_pruned(r, {"link", "script", "highlight"}))
    return out


def _runs(rs: list[JsonObject], size: Json) -> JsonArray:
    return [_run(r, size) for r in rs]


def _size(p: JsonObject, runs: list[JsonObject]) -> Json:
    """A paragraph's size: its own, else its first run's, else 12."""
    return p.get("size", runs[0].get("size", 12.0) if runs else 12.0)


def _paragraph(p: JsonObject, x0: float) -> JsonObject:
    runs = _objects(p, "runs")
    size = _size(p, runs)
    out: JsonObject = {"align": "left", "level": 0, "bullet": None, "size": size, "text_x0": x0, "tab_x0": None,
                       "lines": [], "runs": []}
    out.update(_pruned(p, {"bullet", "tab_x0"}))
    out["runs"] = _runs(runs, size)
    bullet = out["bullet"]
    if bullet is not None:
        # a bullet that says no kind is a glyph: only "number" changes what adopt writes
        out["bullet"] = {"kind": "glyph", **as_object(bullet, "bullet")}
    if "slides" in out:
        # What the dict readers took a measure Slides left unsaid to be.
        slides: JsonObject = {"font": runs[0].get("font", "Arial") if runs else "Arial", "size": size,
                              "indent_start": 0, "indent_first": 0, "line_spacing": 1.0, "space_above": 0}
        slides.update(_pruned(as_object(out["slides"], "slides"), {"spacing_mode"}))
        out["slides"] = slides
    return out


def _text_box(b: Json) -> Json:
    if isinstance(b, list):
        return b
    out: JsonObject = {"valign": "top", "scale": 1.0, "font_scale": 1.0}
    out.update({k: v for k, v in as_object(b, "box").items() if v is not None})
    return out


def _frame(f: JsonObject, bbox: Sequence[float], box: JsonArray) -> JsonObject:
    x0, y0, x1, y1 = bbox
    out: JsonObject = {"size": [x1 - x0, y1 - y0], "origin": [x0, y0], "matrix": [1.0, 0.0, 0.0, 1.0],
                       "rotation": 0.0, "flip": False, "box": list(box)}
    out.update(f)
    return out


def _cell_paragraph(p: JsonObject) -> JsonObject:
    runs = _objects(p, "runs")
    size = _size(p, runs)
    out: JsonObject = {"align": "left", "level": 0, "bullet": None, "size": size, "line_spacing": 1.0, "runs": []}
    out.update(_pruned(p, {"bullet"}))
    out["runs"] = _runs(runs, size)
    return out


def _cell(c: JsonObject) -> JsonObject:
    out: JsonObject = {"rowspan": 1, "colspan": 1, "fill": None, "fill_alpha": None, "valign": "top",
                       "paragraphs": []}
    out.update(_pruned(c, {"fill", "fill_alpha"}))
    paragraphs: JsonArray = [_cell_paragraph(p) for p in _objects(out, "paragraphs")]
    out["paragraphs"] = paragraphs
    return out


def _border(b: JsonObject) -> JsonObject:
    out: JsonObject = {"color": "#000000", "alpha": 1.0, "weight": 1.0, "dash": "SOLID"}
    out.update(b)
    return out


def _span(c: JsonObject, key: str, at: str) -> int:
    """A cell's first row or column plus its span there (1 when it says none)."""
    return as_int(c[at], at) + (as_int(c[key], key) if key in c else 1)


def _rows(cells: list[JsonObject]) -> JsonArray:
    """The words of each cell, row by row, as deck_ir writes a table's `rows`."""
    n = max((_span(c, "rowspan", "row") for c in cells), default=0)
    m = max((_span(c, "colspan", "col") for c in cells), default=0)
    words = {(as_int(c["row"], "row"), as_int(c["col"], "col")):
             " ".join(as_str(r.get("text", ""), "text") for p in _objects(c, "paragraphs")
                      for r in _objects(p, "runs")) for c in cells}
    rows: JsonArray = []
    for i in range(n):
        row: JsonArray = [words.get((i, j), "") for j in range(m)]
        rows.append(row)
    return rows


def filled(d: Given) -> JsonObject:
    """`d` with every key `deck_ir` always writes, as JSON."""
    given = _object(d, "element")
    kind = as_str(given["kind"], "kind")
    ident = "el-" + hashlib.sha1(json.dumps(given, sort_keys=True, default=str).encode()).hexdigest()[:8]
    out: JsonObject = {"id": ident, "role": "body" if kind == "text" else "figure" if kind == "image" else "panel",
                       "group": None}
    out.update(_pruned(given, _NULLABLE[kind]))
    box = list(as_array(out["bbox"], "bbox"))
    bbox = [_number(v, "bbox") for v in box]
    out["bbox"] = box
    if kind == "text":
        out.setdefault("anchor", box[:2])
        out.setdefault("wrap_width", bbox[2] - bbox[0])
        out.setdefault("placeholder", None)
        out.setdefault("shape_type", None)
        paragraphs: JsonArray = [_paragraph(p, bbox[0]) for p in _objects(out, "paragraphs")]
        out["paragraphs"] = paragraphs
        if "box" in out:
            out["box"] = _text_box(out["box"])
    elif kind == "shape":
        if "shape" not in out:
            out["shape"] = as_str(out.get("shape_type") or "RECTANGLE", "shape_type").lower()
        out.setdefault("fill", None)
        out.setdefault("outline", None)
    elif kind == "image":
        out.setdefault("alt", None)
        if "video" in out:
            video: JsonObject = {"source": None, "id": None, "url": None, "start": None, "end": None}
            video.update(as_object(out["video"], "video"))
            out["video"] = video
    elif kind == "table":
        if "rows" not in out:
            out["rows"] = _rows(_objects(out, "table_cells"))
        if "table_cells" in out:
            cells: JsonArray = [_cell(c) for c in _objects(out, "table_cells")]
            out["table_cells"] = cells
        if "table_borders" in out:
            borders: JsonArray = [_border(b) for b in _objects(out, "table_borders")]
            out["table_borders"] = borders
    if "frame" in out:
        out["frame"] = _frame(as_object(out["frame"], "frame"), bbox, box)
    return out


def slide_filled(s: Given, n: int) -> JsonObject:
    """A slide dict with every key `deck_ir` always writes; its elements `filled`."""
    given = _object(s, "slide")
    out: JsonObject = {"page": n, "frame": f"f{n}", "size": [720.0, 405.0], "objectId": f"slide{n}", "key": None,
                       "notes": None, "background_color": None, "background_picture": None}
    out.update(given)
    elements: JsonArray = [filled(e) for e in _objects(given, "elements")]
    out["elements"] = elements
    return out


def target_filled(d: Given) -> JsonObject:
    """A target dict (`{"slides": [...]}` and what else a test says), with every key filled."""
    given = _object(d, "target")
    out: JsonObject = {"version": 1, "source": {"title": None}, "page_size": [720.0, 405.0], "scale": 1.0}
    out.update(given)
    slides: JsonArray = [slide_filled(s, n) for n, s in enumerate(_objects(given, "slides"), 1)]
    out["slides"] = slides
    return out


def deck(d: Given) -> TargetDeck:
    """The record of a test's partial target dict."""
    return parse_target(target_filled(d))


def paragraph(p: Given) -> TargetParagraph:
    """One text paragraph's record, from the keys a test says."""
    el = record({"kind": "text", "bbox": [0, 0, 100, 20], "paragraphs": [p]})
    assert isinstance(el, TargetText)
    return el.paragraphs[0]


def runs(rs: Sequence[Given]) -> tuple[TargetRun, ...]:
    """Run records, from the keys a test says (a size a run leaves out is 12)."""
    return paragraph({"runs": rs}).runs


def text(d: Given) -> TargetText:
    """A text element's record, from the keys a test says."""
    el = record({"kind": "text", **d})
    assert isinstance(el, TargetText)
    return el


def table(d: Given) -> TargetTable:
    """A table's record, from the keys a test says (a bbox is filled in when it says none)."""
    el = record({"kind": "table", "bbox": [0, 0, 100, 20], **d})
    assert isinstance(el, TargetTable)
    return el


def cell(c: Given) -> TableCell:
    """One table cell's record, from the keys a test says (row and column 0 unless it says)."""
    cells = table({"table_cells": [{"row": 0, "col": 0, **c}]}).table_cells
    assert cells is not None
    return cells[0]


def record(d: Given) -> TargetElement:
    """The record a test's partial element dict stands for."""
    return parse_element(filled(d), "test element")


def parsed(d: object) -> TargetElement:
    """The record of a whole element dict, as `deck_ir` wrote it: nothing filled in."""
    return parse_element(d, "test element")


def records(ds: Sequence[Given]) -> list[TargetElement]:
    return [record(d) for d in ds]


def as_dict(el: TargetElement) -> JsonObject:
    """A record's JSON form, as target.json and adopt read it."""
    return element_json(el)


def dicts(els: Sequence[TargetElement]) -> list[JsonObject]:
    return [as_dict(e) for e in els]
