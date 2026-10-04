"""Operators and script capitals emit writes in a served math face of their own, with a measured
advance (fonts.written_advance: OPERATOR_FACES, LETTER_FACE_ADVANCE_EM), are measured wherever
classify and emit ask whether Slides was measured on a character: a prose-free wrapped formula
holding ⊤ ⊙ ⊗ ↦ ≡ stays runs (classify_text.unmeasured_symbols), and a paragraph of script capitals
is sized by its recorded lines (emit_widths.guessed_chars). Only in a run of a math font that gets
that face (fonts.math_operator_faces): a text font's operators, and a Google math face's (Fira
Math, set in Fira Sans), stay unmeasured."""

import pytest

from beamer2slides import ir
from beamer2slides.classify import Rect, Span, new_span
from beamer2slides.classify_text import unmeasured_symbols
from beamer2slides.emit import FontMapper
from beamer2slides.emit_widths import guessed_chars, set_runs_of
from beamer2slides.fonts import font_info
from beamer2slides.json_types import JsonObject

from .test_charts_diagrams import Page, body_text, deck
from .test_math_arrows import holes

SCALE = 1.98
MEASURED_NOW = "⊤⊙⊗⊕↦≡"  # (the OPERATOR_FACES operators Slides' fallback was never measured on)


def span(text: str, font: str) -> Span:
    return new_span(id="", text=text, font=font, size=10.91, color="#000000", rect=Rect(30.0, 90.0, 90.0, 101.0),
                    baseline=99.0, horizontal=True, info=font_info(font), link=None, drawn=False, visual=None)


def run_json(text: str, font: str, family: str) -> JsonObject:
    return {"text": text, "font": font, "family": family, "size": 10.91, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None}


# ------------------------------------------------------------------ classify


@pytest.mark.parametrize("font", ["CMSY10", "ABCDEF+CMSY8", "LMMathSymbols10-Regular", "txsys"])
def test_an_operator_written_in_its_face_is_measured(font: str):
    assert unmeasured_symbols([span(" ".join(MEASURED_NOW), font)]) == set()


def test_an_operator_no_face_is_written_for_stays_unmeasured():
    """\\sqcup's ⊔ (r1_math_v3 s6) has no OPERATOR_FACES face: Slides' fallback draws it half as tall."""
    assert unmeasured_symbols([span("⊔ ⊤", "CMSY10")]) == {"⊔"}


def test_a_google_math_faces_operators_stay_unmeasured():
    """Fira Math is set in Fira Sans (fonts.google_font): its operators are written in no face of
    their own, and nobody measured them."""
    assert unmeasured_symbols([span("⊤ ⊗ ↦", "FiraMath-Regular")]) == set("⊤⊗↦")


def symbols_line(symbol: str) -> ir.Slide:
    """r1_math_v3 s6's 'q0 x1 · · · xn ⊔ · · · ;' wrapped under '(b) φstart: row 1 is', its ⊔
    `symbol` (test_math_arrows.test_a_wrapped_formula_line_of_symbols_is_one_hole)."""
    p = Page()
    for text, font, size, bbox, baseline in (
            ("(b)", "SFRM0600", 5.98, [14.97, 161.09, 24.76, 167.07], 165.76),
            ("φ", "CMMI10", 10.91, [33.64, 159.29, 40.77, 170.2], 167.82),
            ("start", "SFRM0800", 7.97, [40.77, 163.23, 58.23, 171.2], 169.45),
            (":", "SFRM1095", 10.91, [58.73, 159.3, 61.74, 170.21], 167.82),
            ("row", "SFRM1095", 10.91, [66.56, 159.3, 83.72, 170.21], 167.82),
            ("1", "CMR10", 10.91, [87.34, 159.29, 92.8, 170.2], 167.82),
            ("is", "SFRM1095", 10.91, [96.41, 159.3, 103.7, 170.21], 167.82),
            ("q", "CMMI10", 10.91, [33.64, 172.84, 38.5, 183.75], 181.37),
            ("0", "CMR8", 7.97, [38.51, 176.77, 42.74, 184.74], 183.0),
            ("x", "CMMI10", 10.91, [45.06, 172.84, 51.29, 183.75], 181.37),
            ("1", "CMR8", 7.97, [51.29, 176.77, 55.52, 184.74], 183.0),
            ("· · ·", "CMSY10", 10.91, [57.84, 172.7, 70.54, 183.61], 181.37),
            (" x", "CMMI10", 10.91, [70.54, 172.84, 78.59, 183.75], 181.37),
            ("n", "CMMI8", 7.97, [78.62, 176.77, 83.76, 184.74], 183.0),
            (f"{symbol} · · ·", "CMSY10", 10.91, [88.5, 172.7, 110.89, 183.61], 181.37),
            (" ;", "SFRM1095", 10.91, [110.89, 172.85, 115.72, 183.76], 181.37)):
        s = p.text(text, bbox[0], baseline, size, font=font)
        s["bbox"] = bbox
    body_text(p, 250)
    return deck(p)["slides"][0]


def said(slide: ir.Slide) -> list[str]:
    return ["".join(r["text"] for r in par["runs"]) for e in slide["elements"] if e["kind"] == "text"
            for par in e["paragraphs"]]


def test_a_wrapped_formula_of_measured_operators_stays_runs():
    """With ⊗ for ⊔ every symbol of the line is written in a face Slides was measured on: its
    letters, scripts and operators stay runs, no picture."""
    slide = symbols_line("⊗")
    assert not holes(slide)
    assert any("⊗" in t and "·" in t and t.rstrip().endswith(";") for t in said(slide))


def test_a_wrapped_formula_with_an_unmeasured_symbol_is_still_one_hole():
    slide = symbols_line("⊔")
    assert len(holes(slide)) == 1
    assert not any("⊔" in t for t in said(slide))


# ------------------------------------------------------------------ emit


def guessed(text: str, font: str, family: str) -> int:
    return guessed_chars(set_runs_of([run_json(text, font, family)]), SCALE, FontMapper())


def test_operators_and_script_capitals_of_a_math_run_are_not_guessed():
    """big-o-for-weighted s4: '𝒩s(𝒲): the non-deterministic finite automaton ... as 𝒲': three
    script capitals were three guesses, so the recorded lines went unused and the box kept the
    PDF's extent; they are written in STIX Two Math at its measured advance."""
    assert guessed("x ⊤ y ⊙ z ↦ w ≡ v ⊗ u ⊕", "CMSY10", "serif") == 0
    assert guessed("𝒩 𝒲 ℒ", "CMSY10", "sans") == 0


def test_a_text_fonts_operators_are_still_guessed():
    """A text font's ⊤ and ⊙ are drawn by Slides' fallback, which was never measured on them."""
    assert guessed("a ⊤ b ⊙ c", "CMSS10", "sans") == 2
