"""emit plans the parsed IR (`ir_types`): its planners take each element's record, and the dict
entries other modules call (theme_sync, deck_ir, checks, sync, the tests) read the same dicts into
the same records. Both roads must write the same requests, and an element that does not parse is
contained like one emit could not plan. No Google API.
"""

from pathlib import Path

import pytest

from beamer2slides import emit
from beamer2slides.emit import parse_slide_element
from beamer2slides.emit_model import (
    ObjectMap, Template, TemplateKey, number_box, number_box_of, run_of, set_runs, set_shape, set_text, shape_of,
    template_of, text_of,
)
from beamer2slides.emit_places import slides_texts
from beamer2slides.emit_pptx import shape_element_requests, shape_requests, template_key
from beamer2slides.emit_text import (
    hole_runs, hole_runs_of, in_sentence, in_sentence_of, text_box_requests, text_element_requests,
)
from beamer2slides.emit_widths import pdf_line_breaks, set_runs_of
from beamer2slides.ir_types import ImageElement, IRError, MarkedShape, ShapeElement, TextElement
from beamer2slides.json_types import Json, JsonObject

from . import ir_sources as S
from .json_reads import jobj, jobjs
from .test_classify import deck
from .test_emit_hunt import lm
from .test_emit_requests import FONTS
from .test_sync import text_ir

# Decks with every run, bullet, hole and shape form the built decks have.
DECKS = ["01_basic", "02_math", "04_theme_blocks", "13_inline_math", "21_bullet_shapes", "25_hole_placement",
         "27_text_fit"]
TEMPLATE: JsonObject = {"id": "tpl", "w": 100.0, "h": 50.0}


def planned_slides(classified: ObjectMap) -> tuple[list[JsonObject], float, emit.FontMapper]:
    plan = emit.plan_offline(classified)["plan"]
    return plan.slides(), plan.scale, plan.fonts


def the_template(key: TemplateKey) -> Template:
    """Every shape's template, whatever its key: TEMPLATE."""
    return template_of(TEMPLATE)


def agree(slides: list[JsonObject], scale: float, fonts: emit.FontMapper) -> int:
    """Asserts both roads agree on every text and shape of `slides`; how many elements it compared."""
    seen = 0
    for slide in slides:
        rendered, where = "background" in slide, f"slide page {slide['page']}"
        for el in jobjs(slide, "elements"):
            if el.get("role") == "fallback":
                continue
            typed = parse_slide_element(el, rendered, where)
            if isinstance(typed, TextElement):
                assert set_text(typed) == text_of(el), el["id"]
                assert text_element_requests(typed, "s", "o", scale, fonts, None, None, None, None, None) == \
                    text_box_requests(el, "s", "o", scale, fonts), el["id"]
                assert slides_texts(el, scale, fonts) == ["".join(r.text for r in hole_runs_of(set_runs(p.runs), scale, fonts))
                                                          for p in typed.paragraphs]
                for p, d in zip(typed.paragraphs, jobjs(el, "paragraphs")):
                    runs = set_runs(p.runs)
                    assert runs == set_runs_of(jobjs(d, "runs"))
                    assert [run_of(r) for r in in_sentence(jobjs(d, "runs"))] == in_sentence_of(runs)
                    assert [run_of(r) for r in hole_runs(jobjs(d, "runs"), scale, fonts)] == hole_runs_of(runs, scale, fonts)
                seen += 1
            elif isinstance(typed, (ShapeElement, MarkedShape)):
                assert set_shape(typed) == shape_of(el), el["id"]
                key = template_key(el, scale)
                assert shape_element_requests(typed, "s", "o", scale, the_template) == \
                    shape_requests(el, "s", "o", scale, None if key is None else TEMPLATE), el["id"]
                seen += 1
            elif isinstance(typed, ImageElement) and typed.number is not None:
                assert number_box(typed.number) == number_box_of(jobj(el, "number")), el["id"]
                seen += 1
    return seen


@pytest.mark.parametrize("name", DECKS)
def test_the_dict_entries_and_the_typed_planners_agree_on_a_classified_deck(name: str) -> None:
    if not (S.DECKS / f"{name}-handout.pdf").exists():
        pytest.skip(f"build tests/decks/{name}.tex first")
    assert agree(*planned_slides(deck(name))) > 0


@pytest.mark.parametrize("name", ["21_bullet_shapes", "25_hole_placement"])
def test_the_dict_entries_and_the_typed_planners_agree_on_a_rendered_deck(name: str, tmp_path: Path) -> None:
    """Rendered: glyph bullets carry their ink, holes their pictures, pictures their files."""
    pdf = S.DECKS / f"{name}-handout.pdf"
    if not pdf.exists():
        pytest.skip(f"build tests/decks/{name}.tex first")
    rendered, _raw, _pdf = S._convert_pdf(pdf, tmp_path)
    slides, scale, fonts = planned_slides(rendered)
    assert all("background" in s for s in slides)
    assert agree(slides, scale, fonts) > 0


# ---------------------------------------------------------------- an element that does not parse

def synthetic(*elements: JsonObject) -> JsonObject:
    listed: list[Json] = [e for e in elements]
    return {"source": {"pdf": "nowhere.pdf"}, "slides": [{"page": 0, "size": [360.0, 270.0], "elements": listed}]}


def codeless() -> JsonObject:
    """A text box a producer wrote without `code` (every classify text has it)."""
    el = text_ir("A line", [20, 60, 90, 72], "p0t1", role="body")
    del el["code"]
    return el


def test_a_text_that_does_not_parse_is_the_picture_of_its_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parsing is part of planning: its IRError is contained as a KeyError of the planner was
    (`DeckPlan(contain=True)`), the element a fallback picture with a warning, its neighbour kept."""
    monkeypatch.setenv(emit.STRICT_ENV, "0")
    planned = emit.plan_offline(synthetic(codeless(), text_ir("Kept", [20, 100, 90, 112], "p0t2", role="body")))
    assert planned["contained"] == [{"page": 0, "id": "p0t1", "kind": "text",
                                     "error": "IRError: slide page 0, element p0t1: lacks required key 'code'"}]
    kinds = {el["id"]: (el["kind"], el.get("role")) for el in jobjs(planned["plan"].slides()[0], "elements")}
    assert kinds == {"p0t1": ("image", "fallback"), "p0t2": ("text", "body")}


def test_under_strict_a_text_that_does_not_parse_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(emit.STRICT_ENV, "1")
    with pytest.raises(IRError, match="lacks required key 'code'"):
        emit.plan_offline(synthetic(codeless()))


def test_a_rendered_picture_on_a_classified_slide_is_refused() -> None:
    """The stage is the slide's (render gives every slide its `background`): a picture carrying
    render's `file` on a slide render never saw is no deck any producer writes."""
    picture: JsonObject = {"id": "p0f2", "kind": "image", "role": "figure", "bbox": [200, 55, 300, 80], "spans": [],
                           "file": "figures/x.png", "px": [200, 50]}
    assert parse_slide_element(picture, True, "slide page 0").id == "p0f2"
    with pytest.raises(IRError, match="unknown key"):
        parse_slide_element(picture, False, "slide page 0")


# ---------------------------------------------------------------- what the types showed

def test_line_breaks_with_fonts_and_no_scale_are_unmeasured() -> None:
    """emit_widths.pdf_line_breaks took `fonts` and `scale` as separate defaults: fonts alone reached
    `slides_width(..., None, fonts)` and `s / scale` for a symbol CM has no advance for, a
    TypeError. No caller passed one without the other; the typed twin reads a missing scale as
    unmeasurable, as a missing `fonts` always was. (econ v3 s6's paragraph, test_emit_hunt.)"""
    sans, oblique, symbols = "LMSans10-Regular", "LMSans10-Oblique", "LMMathSymbols10-Regular"
    runs: list[Json] = [lm("ITT; LATE via ", sans), lm("D̄", oblique, italic=True),
                        lm("v", "LMSans8-Oblique", italic=True, script="sub"), lm(" instrument is ", sans),
                        lm("≈", symbols), lm(" 1", sans), lm(".", "LMMathItalic10-Regular"), lm("2", sans),
                        lm("× ", symbols), lm("larger", sans)]
    econ: JsonObject = {"lines": [{"x0": 207.58, "x1": 332.04, "baseline": 180.45},
                                  {"x0": 207.58, "x1": 274.45, "baseline": 192.41}],
                        "runs": runs}
    assert pdf_line_breaks(econ, None, FONTS) is None  # (was: TypeError, float * None)
    assert pdf_line_breaks(econ, emit.SLIDE_W / 362.83, FONTS) == [29]


def test_a_template_record_is_what_the_dict_says() -> None:
    assert template_of(TEMPLATE) == Template(id="tpl", w=100.0, h=50.0)
