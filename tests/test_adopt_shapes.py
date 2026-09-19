"""Shapes, lines and turned elements of a foreign deck (deck_ir.frame / foreign_shape / line_element and
adopt_shapes). Offline: hand-made `presentations.get` answers, no TeX and no Google."""

import math

import pytest

from beamer2slides import adopt, adopt_shapes
from beamer2slides.deck_ir import deck_ir, frame
from beamer2slides.inverse import Context

EMU = 12700


@pytest.fixture(autouse=True)
def no_machine_fonts(monkeypatch, tmp_path):
    monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))


def pt(v: float) -> dict:
    return {"magnitude": v * EMU, "unit": "EMU"}


def solid(hexc: str, alpha: float = 1.0) -> dict:
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return {"solidFill": {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}, "alpha": alpha}}


def transform(x: float, y: float, deg: float = 0.0, flip_h: bool = False, flip_v: bool = False) -> dict:
    """Slides' transform for an element turned `deg` clockwise (y down) about its corner, mirrored first."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    fx, fy = (-1 if flip_h else 1), (-1 if flip_v else 1)
    return {"scaleX": c * fx, "shearX": -s * fy, "shearY": s * fx, "scaleY": c * fy,
            "translateX": x * EMU, "translateY": y * EMU, "unit": "EMU"}


def shape(oid, kind, w, h, tf, fill="4285F4", outline=None, **props) -> dict:
    sp = {"shapeBackgroundFill": solid(fill) if isinstance(fill, str) else (fill or {"propertyState": "NOT_RENDERED"})}
    if outline:
        sp["outline"] = {"outlineFill": solid(outline), "weight": pt(3), "propertyState": "RENDERED", **props}
    pe = {"objectId": oid, "size": {"width": pt(w), "height": pt(h)}, "transform": tf,
          "shape": {"shapeProperties": sp}}
    if kind:
        pe["shape"]["shapeType"] = kind
    return pe


def line(oid, w, h, tf, **lp) -> dict:
    kind, category = lp.pop("line_type", "STRAIGHT_CONNECTOR_1"), lp.pop("category", "STRAIGHT")
    props = {"lineFill": solid("EA4335"), "weight": pt(2), **lp}
    return {"objectId": oid, "size": {"width": pt(w), "height": pt(h)}, "transform": tf,
            "line": {"lineType": kind, "lineCategory": category, "lineProperties": props}}


def deck(*elements) -> dict:
    """A 720 x 405 pt deck with one slide holding `elements` (IR units are pt / 720 * 453.54)."""
    return {"presentationId": "p", "title": "t", "pageSize": {"width": pt(720), "height": pt(405)},
            "masters": [{"objectId": "m", "pageElements": []}],
            "layouts": [{"objectId": "L", "layoutProperties": {"masterObjectId": "m"}, "pageElements": []}],
            "slides": [{"objectId": "s", "slideProperties": {"layoutObjectId": "L"},
                        "pageElements": list(elements)}]}


def elements(*pes) -> list[dict]:
    return deck_ir(deck(*pes), foreign=True)["slides"][0]["elements"]


# ---------------------------------------------------------------- the frame of an element

def test_an_upright_element_is_its_own_box():
    fr = frame([2.0, 0.0, 10.0, 0.0, 3.0, 20.0], 5.0, 4.0, 1.0)
    assert fr["size"] == [10.0, 12.0] and fr["rotation"] == 0.0 and fr["flip"] is False
    assert fr["box"] == [10.0, 20.0, 20.0, 32.0]


def test_a_turned_element_keeps_its_size_and_its_centre():
    """Turned 30 degrees, its bounding box is bigger than it; the frame is what was turned."""
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    m = [c * 2, -s * 1, 100.0, s * 2, c * 1, 50.0]         # a 40 x 10 box scaled (2, 1), turned 30 clockwise
    fr = frame(m, 20.0, 10.0, 1.0)
    assert fr["size"] == [40.0, 10.0] and fr["rotation"] == pytest.approx(30.0)
    cx = 100 + c * 20 - s * 5
    cy = 50 + s * 20 + c * 5
    assert fr["box"] == pytest.approx([cx - 20, cy - 5, cx + 20, cy + 5], abs=1e-3)


def test_a_mirrored_element_says_so_and_an_upside_down_one_is_turned():
    """flipH keeps a text box's words upright (PowerPoint and Slides do this); flipV is flipH turned 180."""
    h = frame([-1.0, 0.0, 30.0, 0.0, 1.0, 0.0], 30.0, 10.0, 1.0)
    assert h["flip"] is True and h["rotation"] == pytest.approx(0.0)
    v = frame([1.0, 0.0, 0.0, 0.0, -1.0, 10.0], 30.0, 10.0, 1.0)
    assert v["flip"] is True and abs(v["rotation"]) == pytest.approx(180.0)
    assert v["box"] == [0.0, 0.0, 30.0, 10.0]


def test_a_line_with_no_height_still_has_a_frame():
    """Most straight connectors are stored with height 0: the missing axis is the other's normal."""
    fr = frame([0.0, 0.0, 5.0, 1.0, 0.0, 5.0], 40.0, 0.0, 1.0)   # 40 long, pointing down the page
    assert fr["size"] == [40.0, 0.0] and fr["rotation"] == pytest.approx(90.0) and fr["flip"] is False


# ---------------------------------------------------------------- what foreign_shape reads

def test_a_shape_keeps_its_preset_fill_alpha_outline_and_dashes():
    fill = solid("34A853", 0.5)
    el, = elements(shape("a", "STAR_5", 100, 100, transform(10, 10), fill=fill, outline="000000",
                         dashStyle="DASH"))
    assert el["shape_type"] == "STAR_5" and el["fill"] == "#34a853" and el["fill_alpha"] == 0.5
    assert el["outline"] == "#000000" and el["dash"] == "DASH"
    assert el["weight"] == pytest.approx(3 * 453.54 / 720, abs=1e-3)
    assert "frame" not in el, "an upright shape needs no frame"


def test_a_fill_at_alpha_zero_is_no_fill():
    """How Slides hides a fill without switching it off (sc-memphis' rings, sc-dark-modern's frames):
    drawn, it was a black square."""
    el, = elements(shape("a", "RECTANGLE", 50, 50, transform(0, 0), fill=solid("000000", 0.0), outline="FFFFFF"))
    assert el["fill"] is None and el["outline"] == "#ffffff"
    assert elements(shape("b", "RECTANGLE", 50, 50, transform(0, 0), fill=solid("000000", 0.0))) == []


def test_a_freeform_is_a_custom_shape():
    el, = elements(shape("a", None, 40, 40, transform(0, 0)))
    assert el["shape_type"] == "CUSTOM"


def test_a_turned_shape_carries_its_frame_and_a_bbox_on_the_page():
    el, = elements(shape("a", "RECTANGLE", 100, 20, transform(200, 100, deg=90)))
    assert el["frame"]["rotation"] == pytest.approx(90.0)
    x0, y0, x1, y1 = el["bbox"]
    assert (x1 - x0) < (y1 - y0), "the bounding box is the turned one"


def test_a_text_box_on_a_panel_keeps_its_fill_and_outline():
    pe = shape("t", "TEXT_BOX", 200, 50, transform(10, 10), fill="FFEEAA", outline="333333")
    pe["shape"]["text"] = {"textElements": [{"paragraphMarker": {"style": {}}},
                                            {"textRun": {"content": "words\n", "style": {}}}]}
    el, = elements(pe)
    assert el["kind"] == "text" and el["fill"] == "#ffeeaa" and el["outline_color"] == "#333333"


def test_pull_reads_shapes_as_before():
    """Nothing of this reaches a converted deck's read: no frame, no alpha, no shape_type on panels."""
    pe = shape("a", "RECTANGLE", 100, 20, transform(200, 100, deg=30), fill=solid("000000", 0.0))
    for el in deck_ir(deck(pe))["slides"][0]["elements"]:
        assert "frame" not in el and "fill_alpha" not in el


# ---------------------------------------------------------------- lines

def test_a_line_says_what_its_heads_dashes_and_route_are():
    el, = elements(line("l", 100, 50, transform(10, 10), startArrow="OPEN_CIRCLE", endArrow="STEALTH_ARROW",
                        dashStyle="DOT", line_type="BENT_CONNECTOR_3", category="BENT"))
    assert el["start_arrow"] == "OPEN_CIRCLE" and el["end_arrow"] == "STEALTH_ARROW"
    assert el["dash"] == "DOT" and el["line_type"] == "BENT_CONNECTOR_3" and el["category"] == "BENT"
    assert el["frame"]["size"][0] > 0


def test_an_elbow_connector_is_drawn_in_its_frame_with_its_heads(tmp_path):
    text = adopt.bootstrap(deck_ir(deck(line("l", 100, 50, transform(10, 10), endArrow="FILL_ARROW",
                                             line_type="BENT_CONNECTOR_3", category="BENT")), foreign=True),
                           tmp_path / "main.tex")
    path = next(l for l in text.splitlines() if "\\path" in l)
    assert path.count("--") == 3, "three legs"
    assert "-{Triangle" in path and "\\usetikzlibrary{arrows.meta}" in text


def test_a_line_that_says_nothing_of_its_kind_is_a_freeform_and_not_drawn(tmp_path):
    """journey-maps s2: the corner-to-corner segment of a filled polygon's box is ink it does not have."""
    pe = line("l", 100, 50, transform(10, 10))
    del pe["line"]["lineType"], pe["line"]["lineCategory"]
    text = adopt.bootstrap(deck_ir(deck(pe), foreign=True), tmp_path / "main.tex")
    assert "\\path" not in text


# ---------------------------------------------------------------- what adopt_shapes draws

@pytest.mark.parametrize("kind", ["RECTANGLE", "ROUND_RECTANGLE", "ELLIPSE", "TRIANGLE", "DIAMOND", "STAR_5",
                                  "RIGHT_ARROW", "CHEVRON", "HEXAGON", "DONUT", "PIE", "ARC", "CAN", "CUBE",
                                  "WEDGE_ROUND_RECTANGLE_CALLOUT", "FLOW_CHART_DOCUMENT", "HEART", "PLUS",
                                  "SNIP_2_DIAGONAL_RECTANGLE", "IRREGULAR_SEAL_1", "BRACE_PAIR", "CLOUD"])
def test_a_preset_is_drawn_inside_its_box(kind):
    """Every point a preset's paths pass through stays (nearly) in the frame (0,0)-(w,-h), callout
    tails aside. (Bezier control points may lie outside: OOXML's document wave and heart have them.)"""
    import re
    paths = adopt_shapes.preset(kind, 120.0, 60.0)
    assert paths, kind
    for path, mode in paths:
        assert mode in ("fs", "f", "s", "fs-eo", "shade+", "shade-")
        on_curve = re.sub(r"controls \([^)]*\)( and \([^)]*\))?", "", path)
        nums = [(float(x), float(y)) for x, y in re.findall(r"\((-?[\d.]+)pt,(-?[\d.]+)pt\)", on_curve)]
        assert nums, path
        slack = 60 if "CALLOUT" in kind else 1
        assert all(-slack <= x <= 120 + slack and -60 - slack <= y <= slack for x, y in nums), (kind, path)


def test_an_unknown_preset_is_a_rectangle():
    assert adopt_shapes.preset("NOT_A_SHAPE", 10, 10) is None
    out = adopt_shapes.shape_block({"kind": "shape", "bbox": [0, 0, 10, 10], "shape_type": "NOT_A_SHAPE",
                                    "fill": "#ff0000"}, Context(), "")
    assert "cycle" in out


def test_a_turned_shape_is_drawn_through_its_transform():
    el, = elements(shape("a", "RECTANGLE", 100, 20, transform(200, 100, deg=30), outline="000000"))
    out = adopt_shapes.shape_block(el, Context(), "")
    assert "cm={" in out and "fill=" in out and "draw=" in out
    # the frame's corner is inside the picture's box: the cm shift is from the box's top-left
    shift = out.split("cm={")[1].split("(")[1].split(")")[0]
    x, y = (float(v.rstrip("pt")) for v in shift.split(","))
    w, h = el["bbox"][2] - el["bbox"][0], el["bbox"][3] - el["bbox"][1]
    assert -0.1 <= x <= w + 0.1 and -h - 0.1 <= y <= 0.1


def test_transparency_and_dashes_become_tikz_options():
    ctx = Context()
    fo, so = adopt_shapes.style_options({"fill": "#00ff00", "fill_alpha": 0.25, "outline": "#000000",
                                         "outline_alpha": 0.5, "weight": 2.0, "dash": "DASH"}, ctx)
    assert "fill opacity=0.250" in fo and "draw opacity=0.500" in so
    assert "dash pattern=on 8pt off 6pt" in so and "line width=2.00pt" in so


def test_a_freeform_is_drawn_as_its_box():
    out = adopt_shapes.shape_block({"kind": "shape", "bbox": [0, 0, 10, 10], "shape_type": "CUSTOM",
                                    "fill": "#ff0000"}, Context(), "")
    assert "cycle" in out and "controls" not in out


# ---------------------------------------------------------------- turned text

def turned_text_deck(deg: float, **kw) -> dict:
    pe = shape("t", "TEXT_BOX", 200, 120, transform(100, 100, deg=deg, **kw), fill=None)
    pe["shape"]["text"] = {"textElements": [{"paragraphMarker": {"style": {}}},
                                            {"textRun": {"content": "turned words\n", "style": {}}}]}
    return deck(pe)


def test_turned_words_are_written_upright_and_set_turned(tmp_path):
    """The text writer lays the words out in the box the element would have upright (its own width,
    not the bounding box's), and `\\adoptturned` sets that turned about the centre."""
    ir = deck_ir(turned_text_deck(30), foreign=True)
    el = ir["slides"][0]["elements"][0]
    text = adopt.bootstrap(ir, tmp_path / "main.tex")
    assert "\\adoptturned{-30.00}" in text and "\\newsavebox\\adopt@box" in text
    width = float(text.split("\\begin{textblock*}{")[1].split("pt}")[0])
    upright, across = el["frame"]["size"][0], el["bbox"][2] - el["bbox"][0]
    assert upright - 12 < width <= upright < across, "the upright width less the insets, not the bbox's"
    cx = (el["frame"]["box"][0] + el["frame"]["box"][2]) / 2
    assert f"{{{cx:.2f}pt}}" in text


def test_upright_and_mirrored_words_are_not_wrapped(tmp_path):
    for ir in (deck_ir(turned_text_deck(0), foreign=True), deck_ir(turned_text_deck(0, flip_h=True), foreign=True)):
        assert "adoptturned" not in adopt.bootstrap(ir, tmp_path / "main.tex")


def test_upside_down_words_are_turned_half_way(tmp_path):
    text = adopt.bootstrap(deck_ir(turned_text_deck(0, flip_v=True), foreign=True), tmp_path / "main.tex")
    assert "\\adoptturned{180.00}" in text or "\\adoptturned{-180.00}" in text
