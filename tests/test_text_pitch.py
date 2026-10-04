"""Where Slides puts the lines of a list whose items wrap (`emit_text.vertical_layout_of`,
`list_spacing`), offline: each item keeps its own lines' pitch and the gap to the next item is that
item's spaceAbove, written `emit_text.LIST_SPACING` so a list does not collapse it.

Before, the gap between two items had to come from the upper item's lineSpacing, and a wrapped
item spread it over its inner lines: monodromy s7's continuation lines came out 10.3 pt low on a
26.9 pt pitch, thesis-defense s4's 7.0, linear-attention s5's 8.4, rxjs s6's 6.3 (the visual hunt's
wrapped-list-item-inner-pitch-spread, wrapped-line-pitch-looser, wrapped-line-pitch-bold-run).
Checked on emit's requests, on `text_layout` over the read-back `slides_sim` makes of them, and on
the built decks' wrapped items."""

from collections.abc import Sequence

import pytest

from beamer2slides import emit, text_layout
from beamer2slides.emit import SLIDE_W
from beamer2slides.emit_model import text_of
from beamer2slides.emit_text import LIST_SPACING, list_spacing, text_box_requests_of
from beamer2slides.google_types import presentation, slides_json
from beamer2slides.ir import Deck
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.snapshot import read_presentation

from . import slides_sim
from .json_reads import jnum, jobj, jobjs, jstr
from .test_classify import deck as built
from .test_emit_hunt import column_list_of
from .test_emit_requests import FONTS

COLUMN_SCALE = SLIDE_W / 362.83  # column_list_of's 4:3 page
BULLETED: JsonObject = {"bullet": {"kind": "glyph"}}


def predicted(baselines: Sequence[Sequence[float]], size: float, ratios: Sequence[float],
              above: Sequence[float]) -> list[float]:
    """Every line's baseline as Slides sets paragraphs of one size, each spaceAbove kept."""
    out: list[float] = []
    first = baselines[0][0]
    for i, bl in enumerate(baselines):
        lines = [first + k * emit.line_pitch(size, ratios[i], size) for k in range(len(bl))]
        out += lines
        if i + 1 < len(baselines):
            first = lines[-1] + emit.pitch_between(size, ratios[i], size, ratios[i + 1], above[i + 1])
    return out


def test_a_wrapped_item_keeps_its_lines_pitch_and_the_next_item_its_gap() -> None:
    # monodromy s7: three items, two of them wrapped, 26.89 pt apart inside an item and 48.5 pt
    # from an item's last line to the next. The first item's ratio covered both: its second line
    # came out 10.3 pt low.
    baselines = [[181.29, 208.18], [256.70, 283.59], [332.13]]
    size = 21.2
    ratios, above = emit.vertical_layout([BULLETED, BULLETED, BULLETED], baselines, [size, size, size])
    for r, bl in zip(ratios, baselines):
        if len(bl) > 1:
            assert emit.line_pitch(size, r, size) == pytest.approx(bl[1] - bl[0], abs=emit.PX_PT / 2)
    assert above[0] == 0.0 and above[1] > 10.0 and above[2] > 10.0
    want = [b for bl in baselines for b in bl]
    for got, pdf in zip(predicted(baselines, size, ratios, above), want):
        assert got == pytest.approx(pdf, abs=emit.PX_PT)


def test_only_an_item_right_after_an_item_is_written_never_collapse() -> None:
    assert list_spacing([True, True, False, True, True, True]) == [False, True, False, False, True, True]
    assert list_spacing([False, False]) == [False, False]


def paragraph_styles(el: JsonObject, scale: float) -> list[JsonObject]:
    """The updateParagraphStyle of each paragraph emit writes for a text element, in order."""
    reqs = [slides_json(r) for r in
            text_box_requests_of(text_of(el), "b2s_s002", "b2s_s002_t0", scale, FONTS, None, None, None, None, None)]
    return [jobj(r, "updateParagraphStyle") for r in reqs
            if "updateParagraphStyle" in r and jstr(r, "updateParagraphStyle", "textRange", "type") == "FIXED_RANGE"]


def test_the_lower_item_says_never_collapse_and_carries_the_gap() -> None:
    # themes v4 s3: two wrapped ▶ items, 13.54 pt apart inside an item and 16.56 pt between them.
    first, second = paragraph_styles(column_list_of(), COLUMN_SCALE)
    assert "spacingMode" not in jobj(first, "style") and "spacingMode" not in jstr(first, "fields").split(",")
    assert jstr(second, "style", "spacingMode") == LIST_SPACING
    assert "spacingMode" in jstr(second, "fields").split(",")
    # Both items have the same pitch inside, and so the same lineSpacing; the gap is spaceAbove.
    assert jnum(first, "style", "lineSpacing") == jnum(second, "style", "lineSpacing")
    assert jnum(second, "style", "spaceAbove", "magnitude") > 3.0


def laid_out(deck: Deck | JsonObject) -> dict[str, text_layout.Layout]:
    """Each text box of a deck as `text_layout` sets it from the read-back `slides_sim` makes."""
    pres = read_presentation(presentation(slides_sim.presentation_of(deck), "the simulated deck"))
    out: dict[str, text_layout.Layout] = {}
    for slide in jobjs(pres, "slides"):
        for oid, rb in jobj(slide, "objects").items():
            lay = text_layout.layout(jobj(rb))
            if lay is not None:
                out[oid] = lay
    return out


def worst(lay: text_layout.Layout, pdf: Sequence[float]) -> float:
    got = [ln.baseline for ln in lay.lines]
    assert len(got) == len(pdf), f"{len(got)} lines laid out, the PDF has {len(pdf)}"
    return max(abs(a - b) for a, b in zip(got, pdf))


def whole(el: JsonObject) -> JsonObject:
    """`column_list_of` with what a deck's element also carries (no spans or strokes, labelled bullets)."""
    paras: list[Json] = []
    for p in jobjs(el, "paragraphs"):
        b = jobj(p, "bullet")
        label: JsonObject = {"x0": jnum(b, "bbox", 0), "baseline": jnum(p, "lines", 0, "baseline"), "font": "CMSS10",
                             "family": "sans", "size": 10.91, "bold": False, "italic": False, "color": jstr(b, "color")}
        runs: list[Json] = [{**r, "script": None, "underline": False, "highlight": None} for r in jobjs(p, "runs")]
        paras.append({**p, "bullet": {**b, "label": label}, "runs": runs})
    return {**el, "spans": [], "panel": None, "strokes": [], "paragraphs": paras}


def test_the_column_list_lands_on_the_pdf_lines() -> None:
    el = column_list_of()
    elements: list[Json] = [whole(el)]
    deck: JsonObject = {"source": {"pdf": "nowhere.pdf"},
                        "slides": [{"page": 2, "size": [362.83, 272.12], "elements": elements}]}
    (lay,) = laid_out(deck).values()
    pdf = [jnum(ln, "baseline") * COLUMN_SCALE for p in jobjs(el, "paragraphs") for ln in jobjs(p, "lines")]
    assert worst(lay, pdf) <= 1.0  # (spread over the first item's lines: 3.3 pt off)


@pytest.mark.needs_decks("out/01_basic-handout.pdf", "out/27_text_fit-handout.pdf")
@pytest.mark.parametrize("name", ["01_basic", "27_text_fit"])
def test_wrapped_items_of_the_built_decks_land_on_the_pdf_lines(name: str) -> None:
    # 01_basic p1 (an item wrapped over two lines, then four): its second line was 5.1 pt low.
    d = built(name)
    boxes = laid_out(d)
    checked = 0
    for slide in d["slides"]:
        scale = SLIDE_W / slide["size"][0]
        for i, e in enumerate(slide["elements"]):
            if e["kind"] != "text":
                continue
            paras = e["paragraphs"]
            listed = [p["bullet"] is not None for p in paras]
            if not any(listed[k] and listed[k + 1] and len(paras[k]["lines"]) > 1 for k in range(len(paras) - 1)):
                continue
            lay = boxes.get(f"b2s_s{slide['page']:03}_t{i}")
            assert lay is not None, f"page {slide['page']} {e['id']}: no layout"
            pdf = [ln["baseline"] * scale for p in paras for ln in p["lines"]]
            assert worst(lay, pdf) <= 1.0, f"page {slide['page']} {e['id']}"
            checked += 1
    assert checked >= 1
