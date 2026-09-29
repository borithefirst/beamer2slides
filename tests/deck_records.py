"""Test elements as `deck_ir_types` records, written as the few keys a test is about.

The thumbnail passes (`deck_fills`, `deck_freeforms`, `deck_thumbs`) take and hand back
`TargetElement` records. A test says only what its case needs (a bbox, a fill, one run's words),
so `record` fills in every other key `deck_ir` always writes with a plain value, then parses it
the one strict way (`parse_element`): a key no version of `deck_ir` writes is still refused.
`as_dict` is the JSON form again, for what still reads dicts (adopt)."""

import hashlib
import json

from beamer2slides.deck_ir_types import (TableCell, TargetDeck, TargetElement, TargetParagraph, TargetRun,
                                         TargetTable, TargetText, element_json, parse_element, parse_target)

# The keys each kind takes as null. Any other key a test sets to None is one deck_ir leaves out.
_NULLABLE = {"text": {"group", "key", "placeholder", "shape_type", "outline_color"},
             "shape": {"group", "key", "fill", "outline", "weight", "line_type", "category"},
             "image": {"group", "key", "alt"},
             "table": {"group", "key"},
             "diagram": {"group", "key"}}


def _pruned(d: dict, nullable: set[str]) -> dict:
    return {k: v for k, v in d.items() if v is not None or k in nullable}


def _run(r: dict, size: float) -> dict:
    out = {"text": "", "font": "Arial", "family": "sans", "size": size, "bold": False, "italic": False,
           "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False,
           "strike": False, "highlight": None}
    out.update(_pruned(r, {"link", "script", "highlight"}))
    return out


def _paragraph(p: dict, x0: float) -> dict:
    runs = p.get("runs", [])
    size = p.get("size", runs[0].get("size", 12.0) if runs else 12.0)
    out = {"align": "left", "level": 0, "bullet": None, "size": size, "text_x0": x0, "tab_x0": None,
           "lines": [], "runs": []}
    out.update(_pruned(p, {"bullet", "tab_x0"}))
    out["runs"] = [_run(r, size) for r in runs]
    if out["bullet"] is not None:
        # a bullet that says no kind is a glyph: only "number" changes what adopt writes
        out["bullet"] = {"kind": "glyph", **out["bullet"]}
    if "slides" in out:
        # What the dict readers took a measure Slides left unsaid to be.
        out["slides"] = {"font": runs[0].get("font", "Arial") if runs else "Arial", "size": size,
                         "indent_start": 0, "indent_first": 0, "line_spacing": 1.0, "space_above": 0,
                         **_pruned(out["slides"], {"spacing_mode"})}
    return out


def _text_box(b: dict | list) -> dict | list:
    if isinstance(b, list):
        return b
    return {"valign": "top", "scale": 1.0, "font_scale": 1.0, **{k: v for k, v in b.items() if v is not None}}


def _frame(f: dict, bbox: list) -> dict:
    x0, y0, x1, y1 = bbox
    out = {"size": [x1 - x0, y1 - y0], "origin": [x0, y0], "matrix": [1.0, 0.0, 0.0, 1.0], "rotation": 0.0,
           "flip": False, "box": list(bbox)}
    out.update(f)
    return out


def _cell_paragraph(p: dict) -> dict:
    runs = p.get("runs", [])
    size = p.get("size", runs[0].get("size", 12.0) if runs else 12.0)
    out = {"align": "left", "level": 0, "bullet": None, "size": size, "line_spacing": 1.0, "runs": []}
    out.update(_pruned(p, {"bullet"}))
    out["runs"] = [_run(r, size) for r in runs]
    return out


def _cell(c: dict) -> dict:
    out = {"rowspan": 1, "colspan": 1, "fill": None, "fill_alpha": None, "valign": "top", "paragraphs": []}
    out.update(_pruned(c, {"fill", "fill_alpha"}))
    out["paragraphs"] = [_cell_paragraph(p) for p in out["paragraphs"]]
    return out


def _border(b: dict) -> dict:
    out = {"color": "#000000", "alpha": 1.0, "weight": 1.0, "dash": "SOLID"}
    out.update(b)
    return out


def filled(d: dict) -> dict:
    """`d` with every key `deck_ir` always writes, as JSON."""
    kind = d["kind"]
    ident = "el-" + hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:8]
    out = {"id": ident, "role": "body" if kind == "text" else "figure" if kind == "image" else "panel",
           "group": None}
    out.update(_pruned(d, _NULLABLE[kind]))
    bbox = list(out["bbox"])
    out["bbox"] = bbox
    if kind == "text":
        out.setdefault("anchor", bbox[:2])
        out.setdefault("wrap_width", bbox[2] - bbox[0])
        out.setdefault("placeholder", None)
        out.setdefault("shape_type", None)
        out["paragraphs"] = [_paragraph(p, bbox[0]) for p in out.get("paragraphs", [])]
        if "box" in out:
            out["box"] = _text_box(out["box"])
    elif kind == "shape":
        out.setdefault("shape", (out.get("shape_type") or "RECTANGLE").lower())
        out.setdefault("fill", None)
        out.setdefault("outline", None)
    elif kind == "image":
        out.setdefault("alt", None)
        if "video" in out:
            out["video"] = {"source": None, "id": None, "url": None, "start": None, "end": None, **out["video"]}
    elif kind == "table":
        if "rows" not in out:
            cells = out.get("table_cells", [])
            n = max((c["row"] + c.get("rowspan", 1) for c in cells), default=0)
            m = max((c["col"] + c.get("colspan", 1) for c in cells), default=0)
            words = {(c["row"], c["col"]): " ".join(r.get("text", "") for p in c.get("paragraphs", [])
                                                   for r in p.get("runs", [])) for c in cells}
            out["rows"] = [[words.get((i, j), "") for j in range(m)] for i in range(n)]
        if "table_cells" in out:
            out["table_cells"] = [_cell(c) for c in out["table_cells"]]
        if "table_borders" in out:
            out["table_borders"] = [_border(b) for b in out["table_borders"]]
    if "frame" in out:
        out["frame"] = _frame(out["frame"], bbox)
    return out


def slide_filled(s: dict, n: int) -> dict:
    """A slide dict with every key `deck_ir` always writes; its elements `filled`."""
    out = {"page": n, "frame": f"f{n}", "size": [720.0, 405.0], "objectId": f"slide{n}", "key": None,
           "notes": None, "background_color": None, "background_picture": None}
    out.update(s)
    out["elements"] = [filled(e) for e in s.get("elements", [])]
    return out


def target_filled(d: dict) -> dict:
    """A target dict (`{"slides": [...]}` and what else a test says), with every key filled."""
    out = {"version": 1, "source": {"title": None}, "page_size": [720.0, 405.0], "scale": 1.0}
    out.update(d)
    out["slides"] = [slide_filled(s, n) for n, s in enumerate(d.get("slides", []), 1)]
    return out


def deck(d: dict) -> TargetDeck:
    """The record of a test's partial target dict."""
    return parse_target(target_filled(d))


def paragraph(p: dict) -> TargetParagraph:
    """One text paragraph's record, from the keys a test says."""
    el = record({"kind": "text", "bbox": [0, 0, 100, 20], "paragraphs": [p]})
    assert isinstance(el, TargetText)
    return el.paragraphs[0]


def runs(rs: list[dict]) -> tuple[TargetRun, ...]:
    """Run records, from the keys a test says (a size a run leaves out is 12)."""
    return paragraph({"runs": rs}).runs


def text(d: dict) -> TargetText:
    """A text element's record, from the keys a test says."""
    el = record({"kind": "text", **d})
    assert isinstance(el, TargetText)
    return el


def table(d: dict) -> TargetTable:
    """A table's record, from the keys a test says (a bbox is filled in when it says none)."""
    el = record({"kind": "table", "bbox": [0, 0, 100, 20], **d})
    assert isinstance(el, TargetTable)
    return el


def cell(c: dict) -> TableCell:
    """One table cell's record, from the keys a test says (row and column 0 unless it says)."""
    cells = table({"table_cells": [{"row": 0, "col": 0, **c}]}).table_cells
    assert cells is not None
    return cells[0]


def record(d: dict) -> TargetElement:
    """The record a test's partial element dict stands for."""
    return parse_element(filled(d), "test element")


def parsed(d: dict) -> TargetElement:
    """The record of a whole element dict, as `deck_ir` wrote it: nothing filled in."""
    return parse_element(d, "test element")


def records(ds: list[dict]) -> list[TargetElement]:
    return [record(d) for d in ds]


def as_dict(el: TargetElement) -> dict:
    """A record's JSON form, as target.json and adopt read it."""
    return element_json(el)


def dicts(els: list[TargetElement]) -> list[dict]:
    return [as_dict(e) for e in els]
