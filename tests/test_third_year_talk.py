"""Defects a blind hunter found on a real deck (real_third-year-talk-2017-lmcarvalh), rebuilt on
synthetic pages: a tree standing on its caption, a footnote's raised asterisk, a legend's line
swatches on their key boxes."""

from beamer2slides.classify_reasons import footnote_mark
from beamer2slides.emit import FontMapper, diagram_requests
from beamer2slides.emit_diagrams import on_filled_node
from beamer2slides.google_types import slides_json
from beamer2slides.ir_types import Color, Node
from beamer2slides.json_types import JsonObject

from .test_charts_diagrams import Page, body_text, elements, ellipse, images, lines, span, text_elements


def test_a_tree_standing_on_its_caption_is_a_picture_not_an_overlay() -> None:
    """SubTreeLeap (p10): a tree whose leaves end on the caption under it became an overlay
    anchored to the caption; Slides set the caption at another width, the picture moved and
    stretched with it, and the leaves' dots and curve outlines it did not hold stayed behind in
    the background: hollow ovals and doubled curves."""
    p = Page()
    root = (150.0, 40.0)
    for leaf_x in (122.0, 136.0, 164.0, 175.0):
        p.draw(lines(root, (leaf_x, 82.0)), type="s", stroke="#000000", width=0.6)
        p.draw(ellipse(leaf_x, 79.0, 1.5, 1.5), fill="#000000")
    p.words("Disconnect its parent", 118, 88, 6.0)
    body_text(p)
    els = elements(p)
    assert not any(e.get("overlay") for e in images(els)), [e.get("anchor") for e in images(els)]
    assert any("Disconnect" in "".join(r["text"] for par in e["paragraphs"] for r in par["runs"])
               for e in text_elements(els))


def test_a_raised_asterisk_opening_a_line_is_no_hanging_label() -> None:
    """'Phylogenies$^*$!' over '$^*$ plus complicated models' (p2): the small raised asterisk was
    read as the line's hanging label, `∗<TAB>plus...` in a centred paragraph, and Slides tabbed
    it far to the left."""
    p = Page()
    p.text("Phylogenies", 137.29, 91.64, 14.35, font="TeXGyrePagellaX-Bold", w=79.7)
    p.text("∗", 216.99, 86.44, 10.46, font="pxsys", w=4.07)
    p.text("!", 221.56, 91.64, 14.35, font="TeXGyrePagellaX-Regular", w=3.99)
    p.text("∗", 146.33, 98.79, 5.48, font="pxsys", w=2.13)
    p.text("plus complicated models", 150.46, 101.14, 5.98, font="TeXGyrePagellaX-Regular", w=66.04)
    body_text(p)
    runs = "".join(r["text"] for e in text_elements(elements(p)) for par in e["paragraphs"] for r in par["runs"])
    assert "*plus" in runs.replace("∗", "*").replace(" ", "") and "\t" not in runs, runs


def test_footnote_mark_is_a_smaller_raised_glyph() -> None:
    star, words = span("∗", 146.33, 5.48, 98.79), span("plus", 150.46, 5.98, 101.14)  # raised 0.39 em
    assert footnote_mark(star, words)
    # a bullet glyph near the baseline of its words (beamer raises its triangle 0.11 em) is no mark
    assert not footnote_mark(span("∗", 146.33, 5.48, 100.5), words)
    assert not footnote_mark(span("∗", 146.33, 5.98, 98.79), words)  # as large as its words


def key_box(x0: float, y0: float, x1: float, y1: float) -> JsonObject:
    return {"baselines": [], "bbox": [x0, y0, x1, y1], "fill": "#ffffff", "label_w": 0.0, "paragraphs": [],
            "shape": "RECTANGLE", "stroke": None, "text": None, "width": None}


def segment(a: tuple[float, float], b: tuple[float, float], stroke: str) -> JsonObject:
    return {"arrow_from": None, "arrow_to": None, "from": [a[0], a[1]], "to": [b[0], b[1]], "stroke": stroke, "width": 0.56}


def test_a_legend_swatch_is_drawn_over_its_key_box() -> None:
    """A ggplot legend (p14): each line swatch lies on a white key box. Lines were created
    before nodes, so the boxes hid the red and teal swatches."""
    d: JsonObject = {"id": "p14dg1", "kind": "diagram", "role": "figure", "bbox": [286.0, 144.0, 332.0, 174.0],
                     "spans": [], "nodes": [key_box(290.64, 154.79, 301.14, 162.56)],
                     "lines": [segment((280.0, 140.0), (290.64, 158.67), "#000000"),
                               segment((291.69, 158.67), (300.09, 158.67), "#f8766d")]}
    order = [next(iter(slides_json(r))) for r in diagram_requests(d, "s", "d", 1.0, FontMapper(), None)]
    made = [k for k in order if k in ("createLine", "createShape")]
    assert made == ["createLine", "createShape", "createLine"], made  # the edge under, the swatch over


def test_on_filled_node_needs_both_ends_inside_a_filled_shape() -> None:
    def node(fill: Color | None) -> Node:
        return Node(bbox=(10.0, 10.0, 20.0, 20.0), shape="RECTANGLE", fill=fill, stroke=None, width=None,
                    paragraphs=(), baselines=(), label_w=0.0, text=None, radius=None, dash=None, adjust=None,
                    rotation=None)
    white = Color("#ffffff")
    assert on_filled_node((11.0, 15.0), (19.0, 15.0), [node(white)])
    assert not on_filled_node((11.0, 15.0), (19.0, 15.0), [node(None)])
    assert not on_filled_node((11.0, 15.0), (25.0, 15.0), [node(white)])  # an edge leaving it
    assert not on_filled_node((10.0, 15.0), (20.0, 15.0), [node(white)])  # rim to rim: its border
