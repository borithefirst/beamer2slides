"""Math operators Lato and PT Serif lack, which Slides draws from a fallback face far from TeX's
(tools/probe_math_operators.py, 2026-10-04: ⊤ 38% short, ⊙⊗⊕ 0.105 em above the axis, ≤ 30%
short): a math font's run writes them in the served face nearest TeX's (fonts.OPERATOR_FACES), its
widths count that face's measured advance, and deck_ir reads them back as their run's, as it does
script capitals (test_math_glyphs)."""

import copy

import pytest

from beamer2slides.deck_ir import ReadRun, capitals_as_their_runs, merge_runs
from beamer2slides.emit import FontMapper, text_box_requests
from beamer2slides.emit_metrics import letter_faces, u16
from beamer2slides.emit_widths import set_runs_of, slides_width
from beamer2slides.fonts import (LETTER_FACE_NAMES, OPERATOR_FACES, face_advance, google_font, written_advance,
                                 written_face)
from beamer2slides.google_types import slides_json
from beamer2slides.json_types import JsonObject
from beamer2slides.text_layout import advance

from .deck_records import runs as target_runs
from .json_reads import jint, jobj, jobjs, jstr
from .test_inverse import TEXT_KINDS, built_pdf

SCALE = 1.98
STIX, LIBERTINUS, NOTO = "STIX Two Math", "Libertinus Math", "Noto Sans Math"


# ------------------------------------------------------------------ the table


def test_the_table_writes_what_the_probe_found_nearest():
    assert {c for c, o in OPERATOR_FACES.items() if o.face == LIBERTINUS} == set("⊙⊗⊕∏∫")
    assert {c for c, o in OPERATOR_FACES.items() if o.face == NOTO} == {"×"}
    for c in "⊤⊥∈∉⊂⊆∪∩∀∃∇∞≤≥≠≈≡⇒↦·∑":
        assert OPERATOR_FACES[c].face == STIX, c


def test_where_lato_is_nearest_it_stays():
    """∂ (Lato's as high and on the axis), → and ⟶ (STIX Two Math's no nearer), ℝℕℤℂ (Lato's own)."""
    for c in "∂→⟶ℝℕℤℂ":
        assert c not in OPERATOR_FACES
        assert written_face(c, "CMSY10") is None


def test_every_face_is_read_back_as_a_letter_face():
    for o in OPERATOR_FACES.values():
        assert o.face in LETTER_FACE_NAMES
        assert 0.2 < o.advance < 1.1


@pytest.mark.parametrize("font", ["CMSY10", "ABCDEF+CMSY8", "LMMathSymbols10-Regular", "txsys", "STIXTwoMath-Regular"])
def test_a_math_fonts_operators_take_their_face(font: str):
    assert google_font(font) is None  # (set in Lato or PT Serif, whose fallback the probe measured)
    assert written_face("≤", font) == STIX
    assert written_face("⊙", font) == LIBERTINUS
    assert written_face("×", font) == NOTO
    assert written_face("x", font) is None


@pytest.mark.parametrize("font", ["CMSS10", "CMR10", "LMSans12-Regular", "TeXGyreHeros-Regular", "FiraSans-Bold",
                                  "FiraMath-Regular"])
def test_a_text_fonts_or_a_google_fonts_operators_stay(font: str):
    """A footline's 'A · B', '50 × 50 cm' in a text face; Fira Math is set in Fira Sans, whose
    operators nobody measured."""
    for c in "·×≤⊙":
        assert written_face(c, font) is None, (font, c)


def test_letter_faces_are_utf16_ranges_one_per_face():
    """𝒜 is astral (two units); ∇ℒ are both STIX Two Math: one range; ⊤⊕ two faces: two ranges."""
    text = "𝒜 ≤ ⊤⊕ ∇ℒ x"
    assert letter_faces(text, "CMSY10", 5) == [(5, 7, STIX), (8, 9, STIX), (10, 11, STIX), (11, 12, LIBERTINUS),
                                               (13, 15, STIX)]
    assert letter_faces("x ≤ y", "CMSS10", 0) == []


def test_an_operator_is_as_wide_as_its_face_draws_it():
    assert written_advance("≤", "CMSY10") == face_advance("≤", STIX) == pytest.approx(0.714)
    assert written_advance("≤", "CMSS10") is None
    assert face_advance("≤", LIBERTINUS) is None  # (not the face emit writes it in)
    plain = slides_width([run_json("x", "CMSY10", "serif")], SCALE, FontMapper())
    with_ops = slides_width([run_json("x≤⊙", "CMSY10", "serif")], SCALE, FontMapper())
    assert plain is not None and with_ops is not None
    (run,) = set_runs_of([run_json("x", "CMSY10", "serif")])
    size = FontMapper().size_of(run, SCALE)[1]
    assert with_ops - plain == pytest.approx((0.714 + 0.731) * size, abs=0.01)
    assert advance("≤", {"fontFamily": STIX}, 10.0) == pytest.approx(7.14)
    assert advance("×", {"fontFamily": NOTO}, 10.0) == pytest.approx(5.68)


# ------------------------------------------------------------------ emit


def run_json(text: str, font: str, family: str) -> JsonObject:
    return {"text": text, "font": font, "family": family, "size": 10.91, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None}


def faced(reqs: list[JsonObject]) -> list[tuple[int, int, str, int]]:
    out: list[tuple[int, int, str, int]] = []
    for r in reqs:
        if "updateTextStyle" in r and jstr(r, "updateTextStyle", "fields") == "weightedFontFamily":
            u = jobj(r, "updateTextStyle")
            out.append((jint(u, "textRange", "startIndex"), jint(u, "textRange", "endIndex"),
                        jstr(u, "style", "weightedFontFamily", "fontFamily"), jint(u, "style", "weightedFontFamily", "weight")))
    return out


def test_emit_writes_leq_and_top_in_their_face_over_exactly_them():
    """'𝔼 ' before the run (astral: two units) moves every index after it by one more than its
    code points: the ranges are UTF-16, ≤ and ⊤ each alone, at the run's weight."""
    words = run_json("Let 𝔼 hold:", "CMSS10", "sans")
    rel = {**run_json(" x\xa0≤\xa0y\xa0⊤", "CMSY10", "sans"), "bold": True}
    par: JsonObject = {"align": "left", "level": 0, "size": 10.91, "text_x0": 30.0, "tab_x0": None, "wrap_limit": None,
                       "bullet": None, "lines": [{"baseline": 100.0, "x0": 30.0, "x1": 160.0}], "runs": [words, rel]}
    el: JsonObject = {"id": "t", "kind": "text", "role": "body", "paragraphs": [par], "bbox": [30.0, 90.0, 160.0, 104.0]}
    reqs = [slides_json(r) for r in text_box_requests(el, "s", "b", SCALE, FontMapper())]
    inserted = "".join(jstr(r, "insertText", "text") for r in reqs if "insertText" in r)
    leq, top = (u16(inserted[:inserted.index(c)]) for c in "≤⊤")
    assert faced(reqs) == [(leq, leq + 1, STIX, 700), (top, top + 1, STIX, 700)]


def test_operators_in_a_text_font_get_no_face():
    words = run_json("2025 · pen plotter, 50 × 50 cm", "TeXGyreHeros-Regular", "sans")
    par: JsonObject = {"align": "left", "level": 0, "size": 10.91, "text_x0": 30.0, "tab_x0": None, "wrap_limit": None,
                       "bullet": None, "lines": [{"baseline": 100.0, "x0": 30.0, "x1": 200.0}], "runs": [words]}
    el: JsonObject = {"id": "t", "kind": "text", "role": "body", "paragraphs": [par], "bbox": [30.0, 90.0, 200.0, 104.0]}
    assert faced([slides_json(r) for r in text_box_requests(el, "s", "b", SCALE, FontMapper())]) == []


# ------------------------------------------------------------------ deck_ir


def read_run(text: str, face: str, font: str) -> ReadRun:
    (r,) = target_runs([{"text": text, "font": font, "family": "serif", "size": 10.91}])
    return ReadRun(run=r, slides_font=face, slides_size=21.6)


def test_deck_ir_reads_an_operator_back_as_its_run():
    read = [read_run("x ", "PT Serif", "CMR10"), read_run("≤", STIX, "STIXTwoMath"),
            read_run(" y ", "PT Serif", "CMR10"), read_run("×", NOTO, "NotoSansMath"),
            read_run(" z ", "PT Serif", "CMR10"), read_run("⊙", LIBERTINUS, "LibertinusMath")]
    (one,) = merge_runs(capitals_as_their_runs(read, FontMapper(), SCALE))
    assert (one.run.text, one.run.font, one.slides_font) == ("x ≤ y × z ⊙", "CMR10", "PT Serif")


def test_deck_ir_keeps_an_operator_in_a_face_emit_does_not_write_it_in():
    """⊙ is written in Libertinus Math: one in STIX Two Math is a person's."""
    read = [read_run("x ", "PT Serif", "CMR10"), read_run("⊙", STIX, "STIXTwoMath")]
    assert [r.slides_font for r in capitals_as_their_runs(read, FontMapper(), SCALE)] == ["PT Serif", STIX]


def test_operators_pull_back_with_no_residual():
    """The whole round trip: 01_basic with a math run of ≤ ⊤ × ⊙ appended to a body paragraph,
    emit's requests replayed as Google stores them, read by deck_ir, compared."""
    from beamer2slides.classify import classify
    from beamer2slides.compare import TOL, compare, without_keys
    from beamer2slides.extract import extract, select_overlays
    from beamer2slides.ir import deck_json

    from .irs import deck_ir
    from .slides_sim import presentation_of
    deck = copy.deepcopy(classify(select_overlays(extract(built_pdf("01_basic"), None), "last")))
    added = 0
    for s in deck["slides"]:
        for el in s["elements"]:
            if el["kind"] == "text" and el.get("role") != "title" and not added:
                for p in el["paragraphs"]:
                    last = p["runs"][-1]
                    if not added and not last.get("script") and p.get("bullet") is None:
                        p["runs"].append({**last, "text": " a ≤ b\xa0⊤ c\xa0×\xa0d\xa0⊙\xa0e", "font": "CMSY10"})
                        added += 1
    assert added
    data = deck_json(deck)
    pres = presentation_of(data)
    faces = {jstr(te, "textRun", "style", "weightedFontFamily", "fontFamily")
             for s in jobjs(pres, "slides") for pe in jobjs(s, "pageElements") if "text" in jobj(pe, "shape")
             for te in jobjs(pe, "shape", "text", "textElements")
             if "textRun" in te and "weightedFontFamily" in jobj(te, "textRun", "style")}
    assert {STIX, NOTO, LIBERTINUS} <= faces
    comp = compare(without_keys(data), deck_ir(pres, deck["slides"][0]["size"]), TOL, {})
    assert [r for r in comp.open() if r.kind in TEXT_KINDS] == []
