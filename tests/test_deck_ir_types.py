"""`deck_ir_types`: a pull's or adopt's target (`deck_ir`'s read of a deck) as typed values, parsed
from and written back to the JSON exactly, and what compare reads through it.

The round trip was measured on every target a checkout holds under out/ (2026-09-29: 4,381
target.json files and 128 `adopt_replay` target caches, every deck_ir version since the corpus was
captured: all exact) and on every `deck_ir()` output the offline suite makes (278 calls: all
exact). Here it is held on the showcase fixtures and on `deck_ir` of hand-made answers.
"""

import copy
import json

import pytest

from beamer2slides.compare import TOL, compare, residual_json, without_keys
from beamer2slides.deck_ir import deck_ir
from beamer2slides.deck_ir_types import (ABSENT, TargetImage, TargetShape, element_json, is_target, parse_target,
                                         target_json)
from beamer2slides.inverse import target_pictures, typed_target
from beamer2slides.ir_types import IRError

from .test_adopt import blank_line_deck, presentation
from .test_adopt_compiles import NAMES, SHOWCASE


def dumped(v: object) -> str:
    return json.dumps(v, sort_keys=True)


def round_trip(d: dict) -> None:
    assert dumped(target_json(parse_target(json.loads(json.dumps(d))))) == dumped(d)


def small() -> dict:
    return {"version": 1, "source": {"presentationId": "p", "title": None, "revisionId": None},
            "page_size": [720, 405.0], "scale": 1.0,
            "slides": [{"page": 0, "frame": "s1", "size": [720, 405.0], "objectId": "g1", "key": None,
                        "notes": None, "background_color": None, "background_picture": None,
                        "elements": [{"kind": "shape", "role": "panel", "bbox": [0, 0, 10, 10.5],
                                      "shape": "RECTANGLE", "fill": "#ffffff", "outline": None,
                                      "id": "e1", "group": None}]}]}


@pytest.mark.needs_decks(*(f"foreign/showcase/{n}/target.json" for n in NAMES))
@pytest.mark.parametrize("name", NAMES)
def test_a_showcase_target_round_trips(name):
    round_trip(json.loads((SHOWCASE / name / "target.json").read_text(encoding="utf-8")))


@pytest.mark.parametrize("foreign", [True, False], ids=["adopt", "pull"])
def test_what_deck_ir_reads_round_trips(foreign):
    round_trip(deck_ir(presentation(), foreign=foreign))
    round_trip(deck_ir(blank_line_deck(), foreign=foreign))


def test_a_key_left_out_and_a_null_one_stay_apart():
    d = small()
    typed = parse_target(d)
    shape = typed.slides[0].elements[0]
    assert isinstance(shape, TargetShape) and shape.key is ABSENT and shape.weight is ABSENT
    assert typed.slides[0].background_file is ABSENT and typed.slides[0].key is None
    d["slides"][0]["elements"][0].update(key=None, weight=None)
    d["slides"][0]["background_file"] = None
    typed = parse_target(d)
    shape = typed.slides[0].elements[0]
    assert isinstance(shape, TargetShape) and shape.key is None and shape.weight is None
    round_trip(d)
    round_trip(small())
    assert isinstance(parse_target(small()).page_size[0], int)      # numbers kept as they came


def test_a_key_deck_ir_never_wrote_is_refused():
    d = small()
    d["slides"][0]["elements"][0]["colour"] = "#000000"
    with pytest.raises(IRError, match="slides\\[0\\].elements\\[0\\].*'colour'"):
        parse_target(d)


def test_only_deck_irs_read_is_parsed_as_a_target():
    assert is_target(small())
    stand_in = {"slides": []}                        # a test's target, or classify's deck.json
    assert not is_target(stand_in) and typed_target(stand_in) is stand_in


@pytest.mark.needs_decks("foreign/showcase/talk/target.json")
def test_compare_reads_a_target_as_it_reads_its_json():
    d = json.loads((SHOWCASE / "talk" / "target.json").read_text(encoding="utf-8"))
    cur = without_keys(copy.deepcopy(d))
    plain = compare(cur, d, TOL, {})
    typed = compare(cur, parse_target(d), TOL, {})
    assert [residual_json(r) for r in typed.residuals] == [residual_json(r) for r in plain.residuals]
    assert typed.slides == plain.slides and dict(typed.elements) == dict(plain.elements)


@pytest.mark.needs_decks("foreign/showcase/talk/target.json")
def test_a_targets_pictures_are_keyed_by_the_typed_elements():
    d = json.loads((SHOWCASE / "talk" / "target.json").read_text(encoding="utf-8"))
    typed = parse_target(d)
    images = [e for s in typed.slides for e in s.elements if isinstance(e, TargetImage)]
    assert images
    pictures = target_pictures(typed)
    assert [ref for ref, _ in pictures] == [id(e) for e in images]
    assert [dumped(j) for _, j in pictures] == [dumped(element_json(e)) for e in images] \
        == [dumped(e) for s in d["slides"] for e in s["elements"] if e["kind"] == "image"]
