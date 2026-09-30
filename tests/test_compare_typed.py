"""`compare` given parsed decks (`ir_types`) answers as it does given deck.json's dicts.

A built deck is compared with an edited copy of itself (a word changed, a picture moved, a panel
recoloured), once as dicts and once parsed at the rendered stage, picture hashes keyed by the
dicts' ids in the one and by the typed elements' in the other: the pairs, the residuals and the
element map must be the same, key for key.
"""

import copy
import json
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from beamer2slides import checks
from beamer2slides.compare import TOL, Comparison, PictureHash, compare, residual_json, text_anchor, without_keys
from beamer2slides.ir_types import RenderedDeck, parse_deck
from beamer2slides.json_types import JsonObject

from .json_reads import jnums, jobj, jobjs, jstr

HERE = Path(__file__).resolve().parent
DECKS = ("03_figures", "04_theme_blocks", "15_plain_tabular", "21_bullet_shapes")


def edited(deck: JsonObject) -> JsonObject:
    out = copy.deepcopy(deck)
    for s in jobjs(out, "slides"):
        for e in jobjs(s, "elements"):
            if e["kind"] == "text" and e.get("paragraphs"):
                run = jobj(e, "paragraphs", 0, "runs", 0)
                if not run.get("hole"):
                    run["text"] = jstr(run, "text") + " edited"
                    break
        for e in jobjs(s, "elements"):
            if e["kind"] == "image":
                x0, y0, x1, y1 = jnums(e, "bbox")
                e["bbox"] = [x0 + 12, y0, x1 + 12, y1]
            if e["kind"] == "shape" and e.get("fill"):
                e["fill"] = "#ff0000" if jstr(e, "fill").lower() != "#ff0000" else "#0000ff"
    return out


def hashes_of(elements: Iterable[object]) -> dict[int, PictureHash]:
    """A different fake picture per element, keyed by its id()."""
    out: dict[int, PictureHash] = {}
    for n, e in enumerate(elements, start=1):
        out[id(e)] = [(n * 37) % 256] * 256
    return out


def dict_elements(decks: Sequence[JsonObject]) -> list[JsonObject]:
    """Every element of the decks, deck by deck and slide by slide."""
    return [e for deck in decks for s in jobjs(deck, "slides") for e in jobjs(s, "elements")]


def typed_elements(decks: Sequence[RenderedDeck]) -> list[object]:
    return [e for deck in decks for s in deck.slides for e in s.elements]


def plain(comp: Comparison) -> str:
    return json.dumps({"slides": comp.slides, "residuals": [residual_json(r) for r in comp.residuals],
                       "elements": sorted(comp.elements.items())}, default=str)


@pytest.mark.parametrize("name", DECKS)
@pytest.mark.needs_decks(*(f"out/{n}.pdf" for n in DECKS))
def test_typed_decks_compare_as_their_dicts(name: str) -> None:
    cur = checks.convert_locally(HERE / "decks" / "out" / f"{name}.pdf").deck
    tgt = edited(cur)
    typed_cur, typed_tgt = parse_deck(cur, "rendered"), parse_deck(tgt, "rendered")
    by_dict = compare(without_keys(cur), tgt, TOL, hashes_of(dict_elements([cur, tgt])))
    by_type = compare(without_keys(typed_cur), typed_tgt, TOL, hashes_of(typed_elements([typed_cur, typed_tgt])))
    assert plain(by_type) == plain(by_dict)
    kinds = {r.kind for r in by_dict.open()}
    assert kinds, "the edits made no difference compare saw"


def test_text_anchor_of_no_element_says_so() -> None:
    """inverse looks the target element up and may find none: that is no element, said as such
    (it was an AttributeError on `None.get`)."""
    with pytest.raises(TypeError, match="no text element"):
        text_anchor(None)
