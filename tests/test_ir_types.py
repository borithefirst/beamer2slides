"""deck.json as typed values (`ir_types`): parsed and written back, it is the same JSON.

`deck_json(parse_deck(d)) == d` for every deck a producer writes, with one canonicalisation: a
flag key (`overlay`, `justified`, `title_page`, `composite`, a number bullet's `patch`) holding
`false` is written as no key. No producer writes one, and none of the 2,046 conforming deck.json
files and 57,069 base element `ir`s under out/ held one (2026-09-29): all came back equal, so no
key changes form and no `identity.ir_fields` hash moves.

Why each optional key has the type it has (counts over those 2,046 decks and the producers' code):
  * nullable (always written, null when there is nothing): a run's `link`, `script`, `highlight`;
    a hole run's `next_x0`; an image bullet's `label` and, rendered, its `color` (9 null); a
    paragraph's `tab_x0` and `wrap_limit` outside theme text; a text element's `panel`; a marked
    shape's `fill` (50 panels) and `outline`; a slide's `label`, `notes`, `background_color`; a
    node's `shape`, `fill`, `stroke`, `width`, `text`.
  * None: absent (written only when there is something to say): everything the classifier adds
    for some elements only (`rotation`, 117 of them ints; `mark`, `parts`, `fill`, a table's
    `fills`, ...), a run's `strike` (absent on hole runs, fraction slashes and literal list
    numbers, never null), a glyph bullet's `ink`+`fill` (together; 783 rendered bullets lack them:
    render could not measure).
  * variants, not optional keys: a hole run (`hole`, `hole_x0`, `before`, `next_x0` always
    together, never `strike`), a drawn bullet (marked pages: no colour, no label), a marked shape
    (`drawings` + `mark` + an always-said `outline`, where a beamer shape has `drawing` and an
    outline only when drawn), a fallback picture (no spans, no px), theme text (`tab_x0` and
    `wrap_limit` never written: 0 of the theme paragraphs has them, every other paragraph has both).
  * pairs nested: a column's `head`+`body`, a diagram line's `via`+`bend`, a bullet's `ink`+`fill`.
  * stages: what render adds is a field of the rendered type only (a picture's `file`/`px`, a
    ball's `color`, a glyph's ink, a slide's `background`).
The 148 older deck.json files (no slide `label`, 5-item hole words, ...) are refused by both this
parser and `ir.problems`, the same 148.
"""

import copy
import dataclasses
import json
import tempfile
import typing
from functools import lru_cache
from pathlib import Path

import pytest

from beamer2slides import emit, identity, ir, ir_types, render
from beamer2slides.classify import classify

from .test_ir import RENDERED, TESTS, marked_pages, pdfs, read, rendered, small_deck
from .test_marks import raw_of


def round_trip(deck: dict, stage: ir_types.Stage) -> dict:
    return ir_types.deck_json(ir_types.parse_deck(deck, stage))


def first_difference(a: object, b: object, path: str = "") -> str:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}: {'added' if k not in a else 'dropped'}"
            if found := first_difference(a[k], b[k], f"{path}.{k}"):
                return found
        return ""
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} items became {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            if found := first_difference(x, y, f"{path}[{i}]"):
                return found
        return ""
    return "" if a == b and type(a) is type(b) else f"{path}: {a!r} became {b!r}"


def assert_same(deck: dict, stage: ir_types.Stage) -> None:
    back = round_trip(deck, stage)
    assert back == deck, first_difference(deck, back)
    # and so the hashes a sync base compares are the same (json.dumps writes a tuple as a list)
    back_again = ir_types.deck_json(ir_types.parse_deck(json.loads(json.dumps(back)), stage))
    assert back_again == deck


def hashes(deck: dict) -> list[str]:
    return [identity.ir_fields(e, None, None, None)[0] for s in deck["slides"] for e in s["elements"]]


# ------------------------------------------------------------------------------ what producers write

@lru_cache(maxsize=None)
def classified() -> tuple[tuple[str, dict], ...]:
    out = []
    for path in pdfs():
        with tempfile.TemporaryDirectory() as tmp:
            out.append((str(path.relative_to(TESTS)).replace("\\", "/"), read(path, Path(tmp))[1]))
    return tuple(out)


@pytest.mark.needs_decks("out")
def test_every_built_deck_comes_back_as_it_was_written():
    decks = classified()
    if not decks:
        pytest.skip("no PDFs built")
    for name, deck in decks:
        back = round_trip(deck, "classified")
        assert back == deck, f"{name}: {first_difference(deck, back)}"
        assert hashes(back) == hashes(deck), name


@pytest.mark.parametrize("name", RENDERED)
def test_a_rendered_deck_and_its_emit_plan_come_back_as_they_were_written(name, tmp_path):
    """The rendered deck emit uploads, and the planned elements a sync base stores as `ir`
    (`DeckPlan.deck`: blocks merged, holes fitted), whose hashes the next sync compares."""
    pdf = TESTS / "decks" / name
    if not pdf.exists():
        pytest.skip(f"not built: {name}")
    raw, deck, read_pdf = read(pdf, tmp_path)
    render.render_backgrounds(read_pdf, raw, deck, tmp_path / "out")
    assert_same(deck, "rendered")
    planned = emit.DeckPlan(deck).deck
    assert_same(planned, "rendered")
    for s in planned["slides"]:
        for e in s["elements"]:
            back = ir_types.element_json(ir_types.parse_rendered_element(e, f"slide page {s['page']}"))
            assert identity.ir_fields(back, None, None, None) == identity.ir_fields(e, None, None, None)


def test_marked_pages_come_back_at_both_stages(tmp_path):
    """Adopt's read-back: a freeform and a line are marked shapes as classified, pictures once
    rendered (`marked.pictured_shapes`: no `mark_n`), and a marked table keeps its mark."""
    raw = raw_of(tmp_path, marked_pages())
    deck = classify(raw)
    typed = ir_types.parse_deck(deck, "classified")
    kinds = {type(e).__name__ for s in typed.slides for e in s.elements}
    assert {"MarkedShape", "TextElement", "ImageElement", "TableElement"} <= kinds
    assert_same(deck, "classified")
    render.render_backgrounds(tmp_path / "p.pdf", raw, deck, tmp_path / "out")
    assert_same(deck, "rendered")
    shapes = [e for s in ir_types.parse_deck(deck, "rendered").slides for e in s.elements
              if isinstance(e, ir_types.RenderedMarkedShape)]
    assert shapes and all(e.shape in ir_types.TEMPLATE_KINDS for e in shapes)


# ------------------------------------------------------------------------------ the types

def test_each_closed_set_is_the_literal_ir_names():
    """The parser's tuples spell out ir.py's Literals (the checker reads a tuple's values, not
    `get_args`), so a value added there must be added here."""
    pairs = [(ir_types.ALIGNS, ir.Align), (ir_types.FAMILIES, ir.Family), (ir_types.SCRIPTS, ir.Script),
             (ir_types.TEXT_ROLES, ir.TextRole), (ir_types.SHAPE_ROLES, ir.ShapeRole),
             (ir_types.TEMPLATE_KINDS, ir.TemplateKind), (ir_types.PRODUCER_SHAPE_KINDS, ir.ProducerShapeKind),
             (ir_types.BULLET_SHAPES, ir.BulletShape), (ir_types.POSITIONS, ir.Position),
             (ir_types.RULE_POSITIONS, ir.RulePosition), (ir_types.ARROWS, ir.Arrow), (ir_types.BENDS, ir.Bend),
             (ir_types.PICTURE_ROUTES, ir.PictureRoute), (ir_types.ELEMENT_KINDS, ir.ElementKind),
             (ir_types.IMAGE_ROLES + ("fallback",), ir.ImageRole)]
    for values, literal in pairs:
        assert values == typing.get_args(literal), literal
    assert set(ir_types.BULLET_KINDS) == {typing.get_args(typing.get_type_hints(t)["kind"])[0]
                                          for t in typing.get_args(ir.Bullet)}


def test_a_parsed_deck_is_frozen_tuples_of_records():
    deck = ir_types.parse_deck(small_deck(), "classified")
    assert isinstance(deck, ir_types.Deck)
    shape, text = deck.slides[0].elements
    assert isinstance(shape, ir_types.ShapeElement) and isinstance(text, ir_types.TextElement)
    assert shape.bbox == (10.0, 10.0, 100.0, 60.0) and isinstance(text.paragraphs, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        shape.fill = ir_types.Color("#ffffff")
    assert isinstance(ir_types.parse_deck(rendered(small_deck()), "rendered"), ir_types.RenderedDeck)


def test_numbers_keep_their_json_type():
    """An int where a float goes stays an int: `identity.ir_fields` hashes 3 and 3.0 apart."""
    deck = small_deck()
    deck["slides"][0]["elements"][0]["radius"] = 3
    deck["slides"][0]["elements"][1]["rotation"] = 90
    back = round_trip(deck, "classified")
    assert back == deck and type(back["slides"][0]["elements"][1]["rotation"]) is int
    assert hashes(back) == hashes(deck)


def test_a_false_flag_is_written_as_no_key():
    """The one canonicalisation: flags are written only when true (as every producer does)."""
    deck = small_deck()
    deck["slides"][0]["title_page"] = False
    deck["slides"][0]["elements"][1]["paragraphs"][0]["justified"] = False
    back = round_trip(deck, "classified")
    assert "title_page" not in back["slides"][0]
    assert "justified" not in back["slides"][0]["elements"][1]["paragraphs"][0]
    deck["slides"][0]["title_page"] = True
    assert round_trip(deck, "classified")["slides"][0]["title_page"] is True


def test_variants_are_told_by_their_keys():
    deck = rendered(small_deck())
    para = deck["slides"][0]["elements"][1]["paragraphs"][0]
    hole = dict(para["runs"][0], text="  ", hole=12.5, hole_x0=40.0, next_x0=None,
                before=[[10.0, "CMSS10", "sans", False, False, "Hello", 20.0]])
    del hole["strike"]
    para["runs"].append(hole)
    para["bullet"] = {"kind": "glyph", "text": "•", "bbox": [10.0, 32.0, 14.0, 40.0], "drawn": True}
    deck["slides"][0]["elements"].append({"kind": "image", "id": "fb0", "role": "fallback",
                                          "bbox": [0.0, 0.0, 10.0, 10.0], "file": "figures/fb0.png"})
    typed = ir_types.parse_deck(deck, "rendered")
    elements = typed.slides[0].elements
    text = elements[1]
    assert isinstance(text, ir_types.RenderedText)
    runs = text.paragraphs[0].runs
    assert isinstance(runs[1], ir_types.HoleRun) and runs[1].before[0][5] == "Hello"
    assert isinstance(text.paragraphs[0].bullet, ir_types.RenderedDrawnBullet)
    assert isinstance(elements[2], ir_types.FallbackImage)
    assert ir_types.deck_json(typed) == deck
    with pytest.raises(ir_types.IRError, match="slide page 0, element fb0: is a fallback picture"):
        ir_types.parse_deck(deck, "classified")


# ------------------------------------------------------------------------------ what a refusal says

def refusal(deck: dict, stage: ir_types.Stage) -> str:
    with pytest.raises(ir_types.IRError) as caught:
        ir_types.parse_deck(deck, stage)
    return str(caught.value)


def test_a_refusal_names_its_slide_element_and_key():
    deck = small_deck()
    deck["slides"][0]["page"] = 4
    text = deck["slides"][0]["elements"][1]
    text["id"] = "p4t0"
    bad = copy.deepcopy(deck)
    bad["slides"][0]["elements"][1]["paragraphs"][0]["align"] = "justify"
    assert refusal(bad, "classified") == ("slide page 4, element p4t0: paragraphs[0].align: is 'justify', "
                                          "expected one of 'left' | 'center' | 'right'")
    bad = copy.deepcopy(deck)
    bad["slides"][0]["elements"][1]["paragraphs"][0]["runs"][0]["color"] = "#FFF"
    assert refusal(bad, "classified") == ("slide page 4, element p4t0: paragraphs[0].runs[0].color: is '#FFF' "
                                          "(str), expected a colour '#rrggbb', lowercase")
    bad = copy.deepcopy(deck)
    bad["slides"][0]["elements"][1]["paragraphs"][0]["bullet"] = {"kind": "glyph", "text": "x", "bbox": [1, 2, 3]}
    assert refusal(bad, "classified") == "slide page 4, element p4t0: paragraphs[0].bullet.bbox: has 3 items, expected 4"
    bad = copy.deepcopy(deck)
    bad["slides"][0]["elements"][1]["colour"] = "#000000"
    assert refusal(bad, "classified") == "slide page 4, element p4t0: has unknown key 'colour' (not in text element)"
    bad = copy.deepcopy(deck)
    del bad["slides"][0]["label"]
    assert refusal(bad, "classified") == "slide page 4: lacks required key 'label'"
    bad = copy.deepcopy(deck)
    bad["slides"][0]["elements"][0]["flip"] = 0
    assert refusal(bad, "classified") == "slide page 4, element p0s0: flip: is 0 (int), expected bool"
    bad = copy.deepcopy(deck)
    del bad["body_size"]
    assert refusal(bad, "classified") == "deck: lacks required key 'body_size'"


def test_old_forms_are_refused_as_ir_refuses_them():
    """What `marked.shape_element` wrote before 688ebf4 (no flip, an outline as a bare colour), and
    a freeform left a shape at the rendered stage (an old adopt base's `keep`): no emit input."""
    deck = small_deck()
    deck["slides"][0]["elements"][0] = {
        "id": "p0m0", "kind": "shape", "role": "panel", "bbox": [70.0, 10.0, 110.0, 45.0], "fill": None,
        "outline": "#000000", "shape": "custom", "radius": 0.0, "drawings": ["p0d0"], "spans": [], "mark": "f"}
    assert refusal(deck, "classified") == "slide page 0, element p0m0: lacks required key 'flip'"
    deck["slides"][0]["elements"][0].update(flip=False, outline=None)
    assert ir_types.deck_json(ir_types.parse_deck(deck, "classified")) == deck
    assert refusal(rendered(deck), "rendered") == (
        "slide page 0, element p0m0: shape: is 'custom', expected one of 'ROUND_RECTANGLE' | "
        "'ROUND_2_SAME_RECTANGLE' | 'RECTANGLE' | 'ELLIPSE' | 'DIAMOND' | 'TRIANGLE'")


def test_theme_text_writes_no_tab_or_wrap_limit():
    deck = small_deck()
    para = copy.deepcopy(deck["slides"][0]["elements"][1]["paragraphs"][0])
    del para["tab_x0"], para["wrap_limit"]
    theme = {"kind": "text", "role": "layout", "bbox": [0.0, 260.0, 60.0, 270.0], "panel": None, "code": False,
             "key": ["Hello", 0, 265, "#000000"], "chars": 5, "paragraphs": [para], "spans": ["s9"]}
    deck["layout_texts"] = [theme]
    assert round_trip(deck, "classified") == deck
    deck["layout_texts"][0]["paragraphs"][0]["tab_x0"] = None
    assert refusal(deck, "classified") == "deck: layout_texts[0].paragraphs[0]: has key 'tab_x0', which a theme " \
                                          "Paragraph never has"
