"""Diagram lines as drawn: dashes from the PDF through every layer to Slides' dashStyle, arrow
heads of every shape TikZ draws, and the long arrows of a sequence diagram (frames 6, 7, 18 and
28 of tests/decks/29_tikz_diagrams.tex, and synthetic tips)."""

import json
import math
from functools import lru_cache
from pathlib import Path

import pytest

from beamer2slides import ir, ir_types, pdf
from beamer2slides.classify import classify
from beamer2slides.classify_figures import closed_path, dash_style, on_rim, tip_corners, tip_head
from beamer2slides.classify_graphics import arrow_shaft
from beamer2slides.classify_model import Rect
from beamer2slides.emit import FontMapper, diagram_requests
from beamer2slides.extract import extract, select_overlays
from beamer2slides.google_types import slides_json
from beamer2slides.ir import element_json
from beamer2slides.json_types import JsonArray, JsonObject, JsonShapeError
from beamer2slides.raw_types import DrawingType, PathItem, parse_drawing, parse_raw

from .json_reads import jobj, jstr
from .test_charts_diagrams import Page, body_text, ellipse, lines, rect
from .test_diagrams_overlays import diagram_of
from .test_pdf_backend import same
from .test_pure_pdf import close

PDF = Path(__file__).parent / "decks" / "out" / "29_tikz_diagrams.pdf"
needs_tikz = pytest.mark.needs_decks("out/29_tikz_diagrams.pdf")


# ---------------------------------------------------------------- dash styles

@pytest.mark.parametrize(("name", "dash", "width", "style"), [
    # TikZ's patterns, at thin (0.4 pt, its default) and thick (0.8 pt)
    ("dashed, thick", [3.0, 3.0], 0.8, "DASH"),
    ("dashed, thin", [3.0, 3.0], 0.4, "LONG_DASH"),
    ("densely dashed, thick", [3.0, 2.0], 0.8, "DASH"),
    ("loosely dashed, thick", [3.0, 6.0], 0.8, "DASH"),
    ("loosely dashed, thin", [3.0, 6.0], 0.4, "LONG_DASH"),
    ("dotted, thick", [0.8, 2.0], 0.8, "DOT"),
    ("dotted, thin", [0.4, 2.0], 0.4, "DOT"),
    ("densely dotted, thin", [0.4, 1.0], 0.4, "DOT"),
    ("dash dot, thick", [3.0, 2.0, 0.8, 2.0], 0.8, "DASH_DOT"),
    ("dash dot, thin", [3.0, 2.0, 0.4, 2.0], 0.4, "LONG_DASH_DOT"),
    ("dash dot dot, thick", [3.0, 2.0, 0.8, 2.0, 0.8, 2.0], 0.8, "DASH_DOT"),
    ("one length: as long on as off", [2.4], 0.8, "DASH"),
    ("round dots: on 0", [0.0, 2.4], 0.8, "DOT"),
    ("solid: no array", [], 0.8, None),
    ("solid: all zeros", [0.0, 0.0], 0.8, None),
])
def test_a_pdf_dash_is_the_nearest_slides_dash_style(name: str, dash: list[float], width: float,
                                                    style: ir.Dash | None) -> None:
    assert dash_style(dash, width) == style, name


# ---------------------------------------------------------------- arrow heads

def polygon(*points: tuple[float, float]) -> list[PathItem]:
    return lines(*points, closed=True)


def turned(points: list[tuple[float, float]], degrees: float) -> list[tuple[float, float]]:
    a = math.radians(degrees)
    return [(x * math.cos(a) - y * math.sin(a) + 50, x * math.sin(a) + y * math.cos(a) + 50) for x, y in points]


LATEX: list[PathItem] = [("c", [[0.0, 1.77], [0.55, 1.91], [3.0, 2.69], [4.53, 3.54]]),
                         ("l", [[4.53, 3.54], [4.53, 0.0]]),
                         ("c", [[4.53, 0.0], [3.0, 0.85], [0.55, 1.63], [0.0, 1.77]])]
STEALTH = polygon((4.66, 1.76), (0.0, 0.0), (1.54, 1.76), (0.0, 3.52))
TRIANGLE = polygon((3.61, 2.19), (0.0, 0.0), (0.0, 2.19), (0.0, 4.37))  # (TikZ passes the back's middle)
SQUARE = polygon(*turned([(0, 0), (3, 0), (3, 3), (0, 3)], 30))
DIAMOND = polygon((0.0, 2.0), (3.0, 0.0), (6.0, 2.0), (3.0, 4.0))


@pytest.mark.parametrize(("name", "kind", "fill", "path", "round_", "head"), [
    ("Latex", "fs", "#000000", LATEX, False, "FILL_ARROW"),
    ("Latex[open]", "s", None, LATEX, False, "OPEN_ARROW"),
    ("Stealth", "fs", "#000000", STEALTH, False, "STEALTH_ARROW"),
    ("Stealth, filled only", "f", "#000000", STEALTH, False, "STEALTH_ARROW"),
    ("Triangle", "fs", "#000000", TRIANGLE, False, "FILL_ARROW"),
    ("Triangle[open]", "s", None, TRIANGLE, False, "OPEN_ARROW"),
    ("Circle", "fs", "#000000", ellipse(2, 2, 2, 2), True, "FILL_CIRCLE"),
    ("Circle[open]", "s", None, ellipse(2, 2, 2, 2), True, "OPEN_CIRCLE"),
    ("Circle[fill=white]", "fs", "#ffffff", ellipse(2, 2, 2, 2), True, "OPEN_CIRCLE"),
    ("Square on a line turned 30 degrees", "fs", "#000000", SQUARE, False, "FILL_SQUARE"),
    ("Square[open]", "s", None, SQUARE, False, "OPEN_SQUARE"),
    ("Diamond", "fs", "#000000", DIAMOND, False, "FILL_DIAMOND"),
    ("Diamond[open]", "s", None, DIAMOND, False, "OPEN_DIAMOND"),
    ("Diamond[fill=white]", "fs", "#ffffff", DIAMOND, False, "OPEN_DIAMOND"),
    ("-> (to): an open stroke", "s", None, [("c", [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 2.5]]),
                                           ("c", [[3.0, 2.5], [2.0, 3.0], [1.0, 4.0], [0.0, 5.0]])], False, "OPEN_ARROW"),
])
def test_an_arrow_tip_is_the_nearest_slides_head(name: str, kind: DrawingType, fill: str | None, path: list[PathItem],
                                                round_: bool, head: ir.Arrow) -> None:
    assert tip_head(kind, fill, "#000000" if "s" in kind else None, path, round_) == head, name


def test_tip_corners_leave_out_points_on_a_straight_run():
    assert tip_corners(TRIANGLE) == [(3.61, 2.19), (0.0, 0.0), (0.0, 4.37)]
    assert len(tip_corners(STEALTH)) == 4 and closed_path(STEALTH) and not closed_path(lines((0, 0), (1, 1)))


def test_a_line_end_on_a_small_circles_rim_away_from_it():
    circle = Rect(10, 10, 14, 14)
    assert on_rim(circle, [14.0, 12.0], [60.0, 12.0])        # the line leaves the circle's back
    assert not on_rim(circle, [14.0, 12.0], [11.0, 12.0])    # the line runs across it
    assert not on_rim(circle, [12.0, 12.0], [60.0, 12.0])    # a dot the line runs into: its centre


def node_page() -> Page:
    p = Page()
    for x0, name in ((40, "Lexer"), (200, "Parser")):
        p.draw(rect(x0, 80, x0 + 60, 110), type="s", stroke="#000000", width=0.4)
        p.text(name, x0 + 8, 98, 7.97)
    return p


def test_a_circle_drawn_after_a_line_at_its_rim_is_its_circle_tip():
    p = node_page()
    p.draw(lines((104, 95), (200, 95)), type="s", stroke="#000000", width=0.8)
    p.draw(ellipse(102, 95, 2, 2), type="fs", fill="#000000", stroke="#000000", width=0.8)
    body_text(p)
    el = diagram_of(p)
    (ln,) = el["lines"]
    assert (ln["arrow_from"], ln["arrow_to"]) == ("FILL_CIRCLE", None)
    assert ln["from"] == [102, 95]  # Slides centres a circle head on the line's end
    assert [n["shape"] for n in el["nodes"] if n["shape"]] == ["RECTANGLE", "RECTANGLE"]


def test_a_dot_drawn_before_the_line_stays_a_node_in_its_place():
    p = node_page()
    p.draw(ellipse(102, 95, 2, 2), type="fs", fill="#000000", stroke="#000000", width=0.8)
    p.draw(lines((104, 95), (200, 95)), type="s", stroke="#000000", width=0.8)
    body_text(p)
    el = diagram_of(p)
    assert [n["shape"] for n in el["nodes"] if n["shape"]] == ["RECTANGLE", "RECTANGLE", "ELLIPSE"]
    assert el["lines"][0]["arrow_from"] is None


def test_a_hollow_triangle_reaches_its_point_like_a_filled_one():
    p = node_page()
    p.draw(lines((100, 95), (196, 95)), type="s", stroke="#000000", width=0.4)
    p.draw(polygon((196, 93), (200, 95), (196, 97)), type="s", stroke="#000000", width=0.4)
    body_text(p)
    (ln,) = diagram_of(p)["lines"]
    assert ln["arrow_to"] == "OPEN_ARROW" and ln["to"][0] > 199.9


def test_a_dashed_line_and_a_dashed_frame_keep_their_dash():
    p = node_page()
    shaft = p.draw(lines((100, 95), (196, 95)), type="s", stroke="#000000", width=0.4)
    shaft["dash"] = [3.0, 3.0]
    p.draw(polygon((196, 93), (200, 95), (196, 97)), type="fs", fill="#000000", stroke="#000000", width=0.4)
    frame = p.draw(rect(30, 70, 270, 120), type="s", stroke="#808080", width=0.4)
    frame["dash"] = [0.4, 2.0]
    body_text(p)
    el = diagram_of(p)
    (ln,) = el["lines"]
    assert ln.get("dash") == "LONG_DASH"
    assert [n.get("dash") for n in el["nodes"] if n["shape"]] == [None, None, "DOT"]


# ---------------------------------------------------------------- the IR and emit

def diagram_dict(dash: ir.Dash | None) -> ir.DiagramElement:
    line: ir.DiagramLine = {"from": [10.0, 10.0], "to": [60.0, 10.0], "arrow_from": "FILL_CIRCLE",
                            "arrow_to": "OPEN_DIAMOND", "stroke": "#000000", "width": 0.8}
    node: ir.Node = {"bbox": [60.0, 0.0, 100.0, 20.0], "shape": "RECTANGLE", "fill": None, "stroke": "#000000",
                     "width": 0.4, "paragraphs": [], "baselines": [], "label_w": 0.0, "text": None}
    if dash is not None:
        line["dash"] = dash
        node["dash"] = dash
    return {"id": "p0dg0", "kind": "diagram", "role": "figure", "bbox": [0.0, 0.0, 100.0, 20.0],
            "nodes": [node], "lines": [line], "spans": []}


def test_a_deck_json_without_dashes_parses_and_writes_back_without_them():
    for dash in (None, "LONG_DASH_DOT"):
        el = element_json(diagram_dict(dash))
        typed = ir_types.parse_element(el, "diagram")
        assert isinstance(typed, ir_types.DiagramElement)
        assert typed.lines[0].dash == dash and typed.nodes[0].dash == dash
        assert ir_types.element_json(typed) == el


def test_emit_writes_a_dash_style_only_for_a_dashed_line_or_outline():
    for dash in (None, "DOT"):
        reqs = [slides_json(r) for r in diagram_requests(element_json(diagram_dict(dash)), "s", "d", 1.0,
                                                         FontMapper(), template=None)]
        line = next(jobj(r, "updateLineProperties") for r in reqs if "updateLineProperties" in r)
        shape = next(jobj(r, "updateShapeProperties") for r in reqs if "updateShapeProperties" in r)
        props, outline = jobj(line, "lineProperties"), jobj(shape, "shapeProperties", "outline")
        assert (props["startArrow"], props["endArrow"]) == ("FILL_CIRCLE", "OPEN_DIAMOND")
        if dash is None:
            assert "dashStyle" not in props and "dashStyle" not in outline
            assert jstr(line, "fields") == "lineFill.solidFill.color,weight,startArrow,endArrow"
            assert "dashStyle" not in jstr(shape, "fields")
        else:
            assert props["dashStyle"] == outline["dashStyle"] == dash
            assert jstr(line, "fields").endswith(",dashStyle") and jstr(shape, "fields").endswith(",outline.dashStyle")


# ---------------------------------------------------------------- raw.json and the backends

def raw_drawing(did: str, kind: str, bbox: JsonArray, path: list[JsonArray]) -> JsonObject:
    """A drawing as raw.json has it: `path` a polyline (closed when it ends where it starts)."""
    pieces: JsonArray = [["l", [a, b]] for a, b in zip(path, path[1:])]
    return {"id": did, "type": kind, "items": "l" * len(pieces), "bbox": bbox,
            "fill": "#000000" if "f" in kind else None, "stroke": "#000000" if "s" in kind else None,
            "width": 0.8 if "s" in kind else None, "fill_opacity": 1, "stroke_opacity": 1, "soft_mask": False,
            "corners": {}, "path": pieces}


def test_raw_json_reads_a_dash_and_a_drawing_older_than_it():
    d = raw_drawing("p0d0", "s", [0, 0, 10, 0], [[0, 0], [10, 0]])
    assert "dash" not in parse_drawing(d, "old")
    d["dash"] = [3, 3]
    assert parse_drawing(d, "new").get("dash") == [3, 3]
    d["dash"] = ["3"]
    with pytest.raises(JsonShapeError, match="dash"):
        parse_drawing(d, "bad")


@needs_tikz
def test_raw_json_with_dashes_reads_back_byte_for_byte():
    raw = select_overlays(extract(PDF, None), "last")
    assert sum(1 for p in raw["pages"] for d in p["drawings"] if "dash" in d) >= 8
    text = json.dumps(raw, indent=1, ensure_ascii=False)
    assert json.dumps(parse_raw(json.loads(text), "raw.json"), indent=1, ensure_ascii=False) == text


def test_a_long_arrow_is_no_hairline_decoration():
    shaft = parse_drawing(raw_drawing("a", "s", [80, 100, 280, 100], [[80, 100], [280, 100]]), "shaft")
    head = parse_drawing(raw_drawing("h", "fs", [280, 98, 284, 102],
                                     [[284, 100], [280, 98], [281, 100], [280, 102], [284, 100]]), "head")
    far = parse_drawing(raw_drawing("f", "fs", [300, 98, 304, 102],
                                    [[304, 100], [300, 98], [301, 100], [300, 102], [304, 100]]), "far")
    assert arrow_shaft(shaft, [shaft, head]) and not arrow_shaft(shaft, [shaft, far])
    assert not arrow_shaft(head, [shaft, head])


@lru_cache(maxsize=1)
def tikz_deck() -> ir.Deck:
    return classify(select_overlays(extract(PDF, None), "last"))


def diagrams_titled(title: str) -> list[ir.DiagramElement]:
    for s in tikz_deck()["slides"]:
        heads = ["".join(r["text"] for p in e["paragraphs"] for r in p["runs"])
                 for e in s["elements"] if e["kind"] == "text" and e["role"] == "title"]
        if heads and heads[0].strip() == title:
            return [e for e in s["elements"] if e["kind"] == "diagram"]
    raise AssertionError(f"no frame {title!r}")


@needs_tikz
def test_tikz_dashes_reach_the_deck():
    (dashed,) = diagrams_titled("Dashed and dotted edges")
    assert [ln.get("dash") for ln in dashed["lines"]] == ["DASH", "DOT", "DASH_DOT", "DASH"]
    (fit,) = diagrams_titled("A dashed container around nodes (fit)")
    assert [n.get("dash") for n in fit["nodes"] if n["shape"]] == ["LONG_DASH"] + [None] * 5


@needs_tikz
def test_tikz_arrow_tips_reach_the_deck():
    latex, circle, bar = diagrams_titled("Arrow tips: double, Latex, circle, bar")
    assert [(ln["arrow_from"], ln["arrow_to"]) for ln in latex["lines"]] == [("FILL_ARROW", "FILL_ARROW")]
    assert [(ln["arrow_from"], ln["arrow_to"]) for ln in circle["lines"]] == [("FILL_CIRCLE", "OPEN_ARROW")]
    assert len([n for n in circle["nodes"] if n["shape"]]) == 2  # the circle is no node
    assert [(ln["arrow_from"], ln["arrow_to"]) for ln in bar["lines"]] == [(None, "FILL_ARROW"), (None, None)]


@needs_tikz
def test_a_sequence_diagram_is_one_native_diagram():
    (seq,) = diagrams_titled("A sequence diagram")
    assert [ln.get("dash") for ln in seq["lines"]].count("LONG_DASH") == 3
    assert [ln["arrow_to"] for ln in seq["lines"]].count("STEALTH_ARROW") == 4


@needs_tikz
def test_the_pure_reader_reads_dashes_as_pdfium_does():
    ours, theirs = pdf.resolve("pure").open(PDF), pdf.resolve("pdfium").open(PDF)
    try:
        dashes = 0
        for a, b in zip(ours, theirs):
            drawings = b.drawings()
            close(a.drawings(), drawings, f"page {a.index} drawings")
            dashes += sum(1 for d in drawings if d.get("dash"))
        assert dashes >= 8
    finally:
        ours.close()
        theirs.close()


@needs_tikz
@pytest.mark.xdist_group("pdf_backend")
def test_the_sandbox_carries_dashes():
    ours, theirs = pdf.resolve("pdfium").open(PDF), pdf.resolve("sandbox").open(PDF)
    try:
        a, b = ours[5], theirs[5]
        drawings = a.drawings()
        same(drawings, b.drawings(), "page 5 drawings")
        dashes = [d.get("dash", ()) for d in drawings if d.get("dash")]
        assert len(dashes) == 4 and all(isinstance(v, float) for dash in dashes for v in dash)
    finally:
        ours.close()
        theirs.close()
