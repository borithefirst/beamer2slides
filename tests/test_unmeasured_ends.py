"""Paragraphs nobody measured line by line in a box a wider paragraph sizes (real_thesis-defense
s4): an item whose next word joins short of the box's edge ends at its own edge (indentEnd,
`emit_text.unmeasured_end`), between its PDF lines as the face may set them and that join."""

from collections.abc import Sequence

from beamer2slides import emit, emit_text
from beamer2slides.google_types import slides_json
from beamer2slides.json_types import Json, JsonObject

from .json_reads import jnum, jobj
from .test_emit_hunt import box_right
from .test_emit_requests import FONTS

PAGE_W = 453.54
SCALE = emit.SLIDE_W / PAGE_W
X0 = 33.16
SIZE = 10.91
FIRST = "How can the almost complete transition from fossil fuels to renewable energy sources for electricity generation be accomplished by 2050?"
WIDE = "How can the electricity and heating/cooling markets be more closely integrated [. . . ]?"


def run(text: str) -> JsonObject:
    """A run in NimbusSanL (Helvetica), a face whose Slides advances nobody measured."""
    return {"text": text, "font": "NimbusSanL-Regu", "family": "sans", "size": SIZE, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False,
            "strike": False, "highlight": None}


def paragraph(text: str, baseline: float, ends: Sequence[float], wrap_limit: float | None) -> JsonObject:
    lines: list[Json] = [{"baseline": baseline + 13.55 * i, "x0": X0, "x1": x1} for i, x1 in enumerate(ends)]
    return {"align": "left", "level": 0, "size": SIZE, "text_x0": X0, "tab_x0": None, "wrap_limit": wrap_limit,
            "bullet": None, "runs": [run(text)], "lines": lines, "justified": False}


def element(first_ends: Sequence[float], wrap_limit: float) -> JsonObject:
    """Item 1 of the slide (two lines, its next word joining at `wrap_limit`) over item 5 (one line, 442.12 wide)."""
    paras: list[Json] = [paragraph(FIRST, 89.12, first_ends, wrap_limit), paragraph(WIDE, 120.0, [442.12], None)]
    return {"id": "p3t3", "kind": "text", "role": "body", "code": False, "bbox": [X0, 80.0, 442.12, 125.0],
            "paragraphs": paras}


def indent_ends(el: JsonObject) -> list[float]:
    styles = [jobj(r, "updateParagraphStyle", "style")
              for r in map(slides_json, emit.text_box_requests(el, "b2s_s004", "b2s_s004_t1", SCALE, FONTS))
              if "updateParagraphStyle" in r]
    return [jnum(s, "indentEnd", "magnitude") if "indentEnd" in s else 0.0 for s in styles]


def test_the_face_is_unmeasured() -> None:
    el = element([403.01, 322.49], 443.87)
    para = emit_text.paragraph_dict(jobj(el, "paragraphs", 0))
    assert emit_text.slides_lines_of(para, SCALE, FONTS) is None


def test_an_item_ends_short_of_its_next_word_in_a_box_a_wider_item_sizes() -> None:
    # The box ended at 447.6 pt for item 5 (442.12 wide, room to item 1's join under 4 pt); item
    # 1's next word joins at 443.87, so 'sources' came up onto its first line in Slides.
    el = element([403.01, 322.49], 443.87)
    right = box_right(el, SCALE)
    assert right > 443.87 * SCALE  # (the box's edge alone would join it)
    ends = indent_ends(el)
    assert ends[1] == 0.0
    own = right - ends[0]
    assert own < 443.87 * SCALE - emit.LINE_MARGIN
    # its PDF lines kept whole as an unmeasured face may set them: 5% of a line past it
    assert own >= (403.01 + emit_text.UNMEASURED_PAD * (403.01 - X0)) * SCALE
    assert abs(own - (403.01 + (443.87 - 403.01) / 2) * SCALE) < 0.01  # the middle of its range


def test_the_box_itself_is_unchanged() -> None:
    el = element([403.01, 322.49], 443.87)
    plain = element([403.01, 322.49], 443.87)
    jobj(plain, "paragraphs", 0)["wrap_limit"] = None
    assert abs(box_right(el, SCALE) - box_right(plain, SCALE)) < 0.01


def test_an_item_whose_next_word_joins_past_the_box_keeps_its_edge() -> None:
    # item 3 of the slide: its next word joins at 459.57, past the box's edge
    assert indent_ends(element([423.58, 155.93], 459.57)) == [0.0, 0.0]


def test_no_edge_short_of_the_join_and_past_the_lines_keeps_the_box_edge() -> None:
    # a line 438 pt wide joins at 443.87: 5% more for the unmeasured face reaches past the join,
    # so the line is kept whole by the box's edge (a word joined up costs no line)
    assert indent_ends(element([438.0, 322.49], 443.87)) == [0.0, 0.0]
