"""Crashes the typed classifier found (classify_state / raw_types). A crash in a page's classifier
turns the whole page into a picture (`classify_page`'s catch-all), so each was a slide lost, not an
error seen. No built deck reached them; these pages do."""

import unicodedata

from beamer2slides import classify
from beamer2slides.classify_text import bullet_shape
from beamer2slides.fonts import font_info
from beamer2slides.raw_types import RawDrawing


def span(text: str, x0: float, x1: float) -> classify.Span:
    return classify.Span(id="s", text=text, font="Arial", size=10.0,
                         color="#000000", rect=classify.Rect(x0, 0.0, x1, 10.0),
                         baseline=10.0, horizontal=True, info=font_info("Arial"))


def test_a_bullet_drawn_in_more_than_twenty_pieces_has_no_path_and_no_shape():
    # extract writes no path for a drawing of more than 20 pieces: `d.get("path", [])` gave None
    drawing: RawDrawing = {"type": "f", "fill": "#336699", "stroke": None, "width": None,
                           "stroke_opacity": 1.0, "items": ["l"] * 21, "path": None}
    assert bullet_shape(drawing) == {}


def test_a_curved_bullet_without_a_path_is_still_a_disc():
    drawing: RawDrawing = {"type": "f", "fill": "#336699", "stroke": None, "width": None,
                           "stroke_opacity": 1.0, "items": ["c"] * 24, "path": None}
    assert bullet_shape(drawing) == {"shape": "disc", "color": "#336699"}


def test_an_accent_carried_onto_a_span_with_leading_spaces_lands_on_its_letter():
    # "(xˆ" then " y" drawn under it: the accent goes on to y. Its count of leading spaces was
    # kept in `lead`, the name of the cell's direction mark, which was then prepended to the text:
    # a TypeError for any count but 0.
    runs = classify.span_runs([span("(xˆ", 0, 10), span(" y", 8, 14)])
    text = unicodedata.normalize("NFC", "".join(r["text"] for r in runs))
    assert "ŷ" in text and "ˆ" not in text
