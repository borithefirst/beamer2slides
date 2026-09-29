"""`compare` given parsed decks (`ir_types`) answers as it does given deck.json's dicts.

A built deck is compared with an edited copy of itself (a word changed, a picture moved, a panel
recoloured), once as dicts and once parsed at the rendered stage, picture hashes keyed by the
dicts' ids in the one and by the typed elements' in the other: the pairs, the residuals and the
element map must be the same, key for key.
"""

import copy
import json
from pathlib import Path

import pytest

from beamer2slides import checks
from beamer2slides.compare import compare, text_anchor
from beamer2slides.ir_types import parse_deck

HERE = Path(__file__).resolve().parent
DECKS = ("03_figures", "04_theme_blocks", "15_plain_tabular", "21_bullet_shapes")


def edited(deck: dict) -> dict:
    out = copy.deepcopy(deck)
    for s in out["slides"]:
        for e in s["elements"]:
            if e["kind"] == "text" and e.get("paragraphs"):
                run = e["paragraphs"][0]["runs"][0]
                if not run.get("hole"):
                    run["text"] = run["text"] + " edited"
                    break
        for e in s["elements"]:
            if e["kind"] == "image":
                e["bbox"] = [e["bbox"][0] + 12, e["bbox"][1], e["bbox"][2] + 12, e["bbox"][3]]
            if e["kind"] == "shape" and e.get("fill"):
                e["fill"] = "#ff0000" if e["fill"].lower() != "#ff0000" else "#0000ff"
    return out


def hashes_of(decks: list, elements_of) -> dict:
    """A different fake picture per image, keyed by id() of what `elements_of(slide)` yields."""
    out = {}
    n = 0
    for deck in decks:
        for s in deck["slides"] if isinstance(deck, dict) else deck.slides:
            for e in elements_of(s):
                n += 1
                out[id(e)] = [(n * 37) % 256] * 256
    return out


def plain(comp) -> str:
    return json.dumps({"slides": comp.slides, "residuals": comp.residuals,
                       "elements": sorted(comp.elements.items())}, default=str)


@pytest.mark.parametrize("name", DECKS)
@pytest.mark.needs_decks(*(f"out/{n}.pdf" for n in DECKS))
def test_typed_decks_compare_as_their_dicts(name):
    cur = checks.convert_locally(HERE / "decks" / "out" / f"{name}.pdf").deck
    tgt = edited(cur)
    typed_cur, typed_tgt = parse_deck(cur, "rendered"), parse_deck(tgt, "rendered")
    by_dict = compare(cur, tgt, None, hashes_of([cur, tgt], lambda s: s["elements"]))
    by_type = compare(typed_cur, typed_tgt, None, hashes_of([typed_cur, typed_tgt], lambda s: s.elements))
    assert plain(by_type) == plain(by_dict)
    kinds = {r["kind"] for r in by_dict.open()}
    assert kinds, "the edits made no difference compare saw"


def test_text_anchor_of_no_element_says_so():
    """inverse looks the target element up and may find none: that is no element, said as such
    (it was an AttributeError on `None.get`)."""
    with pytest.raises(TypeError, match="no text element"):
        text_anchor(None)
