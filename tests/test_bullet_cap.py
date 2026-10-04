"""Beamer's ball bullets, real_ansible-meetup-201-beamer 23: a bullet's cap is its item's prose
size, not the size most of its letters are set at (\\texttt words outnumbering the prose made the
second ball 25% smaller than its neighbours', `emit_text.bullet_cap`), and a ball is sized by its
ink, not its rounded-out image box (every ball came out 18% too large, `render.glyph_ink`)."""

from beamer2slides.emit_metrics import BULLET_SHAPES, bullet_extent_of, bullet_size_of
from beamer2slides.emit_model import bullet_of, run_of
from beamer2slides.emit_text import bullet_cap
from beamer2slides.json_types import JsonObject


def test_texttt_words_do_not_shrink_the_bullet() -> None:
    runs = tuple(run_of({"text": t, "size": z, "font": f}) for t, z, f in (
        ("Keys ", 14.35, "CMSS12"), ("DOCUMENTATION", 11.96, "CMTT12"), (" and ", 14.35, "CMSS12"),
        ("RETURN", 11.96, "CMTT12"), (" are YAML", 14.35, "CMSS12")))
    assert bullet_cap(runs, [26.9, 20.3, 26.9, 20.3, 26.9]) == 26.9


def test_one_large_word_does_not_grow_the_bullet() -> None:
    runs = tuple(run_of({"text": t, "size": z, "font": "CMSS12"}) for t, z in (
        ("Big", 20.0, ), (" and the rest of a long item in body text", 14.35)))
    assert bullet_cap(runs, [37.5, 26.9]) == 26.9


BALL: JsonObject = {"kind": "image", "image": "p22i2", "text": "", "bbox": [39.0, 72.0, 45.0, 78.0], "label": None,
                    "color": "#37378a"}


def test_a_ball_is_sized_by_its_ink() -> None:
    """A beamer ball's image box is rounded out to whole points around a transparent margin (6 pt
    for 4.8 pt of ink): the Slides disc (0.413 em of ink) matches the ink, and stands where it does."""
    inked = bullet_of({**BALL, "ink": [39.69, 72.44, 44.5, 77.25], "fill": 0.72})
    z = bullet_size_of(inked, 26.9, 1.875, False)
    assert abs(z * BULLET_SHAPES["disc"][2] - 4.81 * 1.875) < 0.1
    assert bullet_extent_of(inked, 26.9, 1.875, False)[:2] == (39.69, 44.5)


def test_a_ball_without_ink_keeps_its_box() -> None:
    """An older deck.json (no `ink`) emits as before: the box's height, capped at the text's."""
    plain = bullet_of(BALL)
    assert bullet_size_of(plain, 26.9, 1.875, False) == 26.9
    assert bullet_extent_of(plain, 26.9, 1.875, False)[:2] == (39.0, 45.0)


def test_no_letters_no_cap() -> None:
    assert bullet_cap((run_of({"text": " ", "size": 14.35, "font": "CMSS12"}),), [26.9]) is None
