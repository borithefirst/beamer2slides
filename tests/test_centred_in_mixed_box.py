"""A centred paragraph in a text box that also holds a left-aligned one (real_decision-tree-lect
s30: a TikZ node's centred title "Example simulated output" over a left-aligned file name): the
box's slack all lay right of its words, and Slides centred the title 13.5 PDF pt right of its PDF
middle. The box now takes half its slack on the left, the left paragraph's indent giving it back,
and the centred one is centred between indents equally far from its PDF middle (`centred_indents`)."""

from beamer2slides import emit
from beamer2slides.emit import EMU_PER_PT
from beamer2slides.emit_model import text_of
from beamer2slides.emit_text import PAD_X, centred_indents, paragraph_dict, text_box_requests_of
from beamer2slides.google_types import slides_json
from beamer2slides.json_types import Json, JsonObject

from .json_reads import jnum, jobj
from .test_emit_requests import FONTS

PAGE_W = 364.19
SCALE = emit.SLIDE_W / PAGE_W


def run(text: str, font: str, family: str, size: float) -> JsonObject:
    return {"text": text, "font": font, "family": family, "size": size, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False,
            "strike": False, "highlight": None}


def paragraph(align: str, x0: float, x1: float, baseline: float, words: JsonObject, size: float) -> JsonObject:
    lines: list[Json] = [{"baseline": baseline, "x0": x0, "x1": x1}]
    return {"align": align, "level": 0, "size": size, "text_x0": x0, "tab_x0": None, "wrap_limit": None,
            "bullet": None, "runs": [words], "lines": lines}


def card(second_align: str) -> JsonObject:
    paras: list[Json] = [
        paragraph("center", 104.87, 257.83, 51.95, run("Example simulated output", "CMSS12", "sans", 14.35), 14.35),
        paragraph(second_align, 108.51, 188.7, 118.43, run("testcost-2.png", "CMTT10", "mono", 10.91), 10.91)]
    return {"id": "p50dg2_x0", "kind": "text", "role": "body", "code": False, "bbox": [104.87, 40.0, 257.83, 121.0],
            "paragraphs": paras}


def written(el: JsonObject) -> tuple[float, float, list[JsonObject]]:
    """(text left, text right) of the box (slide pt) and each paragraph's style."""
    reqs = [slides_json(r) for r in text_box_requests_of(text_of(el), "b2s_s050", "b2s_s050_x0", SCALE, FONTS,
                                                         None, None, None, None, None)]
    props = jobj(next(r for r in reqs if "createShape" in r), "createShape", "elementProperties")
    x = jnum(props, "transform", "translateX") / EMU_PER_PT
    w = jnum(props, "size", "width", "magnitude") / EMU_PER_PT
    styles = [jobj(r, "updateParagraphStyle", "style") for r in reqs if "updateParagraphStyle" in r]
    return x + PAD_X, x + w - PAD_X, styles


def indent(style: JsonObject, name: str) -> float:
    return jnum(style, name, "magnitude") if name in style else 0.0


def test_the_centred_title_is_centred_on_its_pdf_middle() -> None:
    left, right, styles = written(card("left"))
    title = styles[0]
    middle = (left + indent(title, "indentStart") + right - indent(title, "indentEnd")) / 2
    assert abs(middle - (104.87 + 257.83) / 2 * SCALE) < 0.01


def test_the_left_paragraph_stays_where_it_was() -> None:
    left, _, styles = written(card("left"))
    assert abs(left + indent(styles[1], "indentStart") - 108.51 * SCALE) < 0.01
    assert abs(left + indent(styles[1], "indentFirstLine") - 108.51 * SCALE) < 0.01


def test_the_title_keeps_as_much_room_as_before() -> None:
    # its centring region is its PDF line and all the box's slack, as when that lay on the right
    left, right, styles = written(card("left"))
    left_alone, right_alone, _ = written(card("center"))
    title = styles[0]
    room = right - indent(title, "indentEnd") - left - indent(title, "indentStart")
    assert abs(room - (right_alone - left_alone)) < 0.01


def test_a_box_of_centred_paragraphs_is_unchanged() -> None:
    # (only a box mixing alignments moves; a centred box already shares its slack)
    _, _, styles = written(card("center"))
    assert all(indent(s, "indentEnd") == 0.0 and indent(s, "indentStart") == 0.0 for s in styles)


def test_centred_indents_are_equally_far_from_the_middle() -> None:
    p = paragraph_dict(jobj(card("left"), "paragraphs", 0))
    middle = (104.87 + 257.83) / 2 * SCALE
    start, end = centred_indents(p, 100.0 * SCALE, 300.0 * SCALE, SCALE)
    assert start == 0.0
    assert abs((100.0 * SCALE + start + 300.0 * SCALE - end) / 2 - middle) < 1e-6
