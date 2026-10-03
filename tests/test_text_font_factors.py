"""TeX text faces other than Computer Modern sized by their own metrics (calibration/
text_advances.json, tools/text_font_advances.py): Linux Libertine in PT Serif came out 7-9% wider
than the PDF (beamer-derived-cat, pnuc-intro, curemodel-wip), Bera Sans and DejaVu Sans in Lato
13-17% narrower (frisem-201410, lecture-phylogenetics, a criterion chart's DejaVu). EC's bold
extended sans titles (SFSX1200, SFSX1440) came out 8-11% narrower (phylogenetics, esi-dev1).
Code set on a column grid in a proportional face is sized to the grid's pitch. Computer Modern's
own factors are unchanged. Synthetic runs, no PDF."""

import pytest

from beamer2slides import emit_metrics, ir
from beamer2slides.compare import read_back_size
from beamer2slides.emit_metrics import (ADVANCES, BOLD_SANS_DESIGN_WIDTH, CM_ADVANCES, CMTT_ADVANCE_EM, DESIGN_WIDTH,
                                        ROBOTO_MONO_ADVANCE_EM, SHAPE_REFERENCE, SHAPE_TITLE_REFERENCE, TEXT_ADVANCES,
                                        FontMapper, advance_widths, design_width, optical_width, parse_text_advances,
                                        text_face)
from beamer2slides.emit_model import run_of, set_run
from beamer2slides.fonts import font_info, metrics_family
from beamer2slides.ir_types import At, Run, run, run_json
from beamer2slides.json_types import JsonObject, JsonShapeError

from .test_code_columns import CODE, fixed_columns, raw_spans, text_boxes

FONTS = FontMapper()


def tex_run(text: str, font: str, size: float) -> JsonObject:
    """A run as classify writes it, in `font` (its family, weight and slant from the name)."""
    info = font_info(font)
    return {"text": text, "font": font, "family": info.family, "size": size, "bold": info.bold,
            "italic": info.italic, "smallcaps": False, "color": "#000000", "link": None}


def factor(font: str) -> float:
    """PDF size / Slides size of an ordinary run in `font` (at 1000 pt: the 0.1 pt rounding out of the way)."""
    return 1000.0 / FONTS(tex_run("The quick brown fox jumps over the lazy dog", font, 1000.0), 1.0)[1]


def cm_factor(font: str) -> float:
    """The factor a run in `font` had before: Computer Modern's, bold and italic half corrected."""
    info = font_info(font)
    out = FONTS.factors[info.family][0]
    for key, on in (("bold", info.bold), ("italic", info.italic)):
        if on:
            out *= 1 + (FONTS.style[info.family][key] - 1) / 2
    return out


def slides_over_pdf(font: str, sentences: tuple[str, ...], size_factor: float) -> float:
    """Predicted Slides width / PDF width of `sentences` set in `font` at `size_factor`."""
    found = text_face(font)
    assert found is not None, font
    _, face = found
    info = font_info(font)
    slides = ADVANCES[emit_metrics.FONT_FOR_FAMILY[info.family]][emit_metrics.STYLE_KEY[(info.bold, info.italic)]]
    s_em = p_em = 0.0
    for text in sentences:
        s, p, _, _ = advance_widths(text, face, slides)
        s_em, p_em = s_em + s, p_em + p
    return s_em / size_factor / p_em


@pytest.mark.parametrize(("name", "family"), [
    ("ABCDEF+LinLibertineT", "libertine"), ("LinLibertineTB", "libertine"), ("LinLibertineTI", "libertine"),
    ("LinLibertineDisplayT", "libertine"), ("LinBiolinumTB", "biolinum"), ("LinBiolinumTBO", "biolinum"),
    ("LibertinusSerif-Regular", "libertine"), ("BeraSans-Roman", "bera_sans"), ("BeraSans-Bold", "bera_sans"),
    ("BeraSerif-Roman", "bera_serif"), ("BitstreamVeraSans-Roman", "bera_sans"), ("DejaVuSans", "dejavu_sans"),
    ("DejaVuSansCondensed-Bold", "dejavu_sans_condensed"), ("DejaVuSerif-Italic", "dejavu_serif"),
    ("TeXGyrePagellaX-Regular", "palatino"), ("URWPalladioL-Roma", "palatino"), ("Utopia-Regular", "utopia"),
    ("Inconsolatazi4-Regular", "inconsolata"),
    # monospaced cuts are Roboto Mono at their own size (google_font), math fonts no text face, and
    # Computer Modern, EC and Latin Modern keep their measured factors
    ("LinLibertineM", None), ("BeraSansMono-Roman", None), ("DejaVuSansMono", None),
    ("TeXGyrePagellaMath-Regular", None), ("CMSS10", None), ("SFSX1200", None), ("LMSans10-Regular", None),
    ("ArialMT", None)])
def test_a_tex_text_face_is_known_by_its_name(name: str, family: str | None) -> None:
    assert metrics_family(name) == family


@pytest.mark.parametrize(("font", "before"), [
    ("LinLibertineT", 1.0706),      # set 7% wider than the PDF
    ("LinLibertineTB", 1.0874),
    ("LinBiolinumT", 1.0175),
    ("BeraSans-Roman", 0.8410),     # 16% narrower
    ("BeraSans-Bold", 0.7842),
    ("DejaVuSans", 0.8366),
    ("DejaVuSansCondensed", 0.9302),
    ("TeXGyrePagellaX-Regular", 0.9966),
    ("Utopia-Regular", 1.0702),
])
def test_a_tex_text_face_comes_out_as_wide_as_the_pdf(font: str, before: float) -> None:
    # `before`: the predicted Slides / PDF width of the calibration sentences at CM's factor; now a
    # regular face matches them, and a bold one keeps half its substitute's difference, as CM's do
    assert slides_over_pdf(font, SHAPE_REFERENCE, cm_factor(font)) == pytest.approx(before, abs=0.002)
    ratio = slides_over_pdf(font, SHAPE_REFERENCE, factor(font))
    if font_info(font).bold:
        assert abs(ratio - 1) < 0.06, ratio   # (Lato Bold is 10% narrower against Vera Sans Bold: 5% left)
    else:
        assert ratio == pytest.approx(1.0, abs=0.002)
    assert abs(ratio - 1) < abs(before - 1)


def test_libertine_and_bera_factors() -> None:
    assert factor("LinLibertineT") == pytest.approx(1.0756, abs=0.0015)
    assert factor("LinBiolinumT") == pytest.approx(1.0382, abs=0.0015)
    assert factor("BeraSans-Roman") == pytest.approx(0.8581, abs=0.0015)
    assert factor("DejaVuSans") == pytest.approx(0.8536, abs=0.0015)
    assert factor("DejaVuSerif") == pytest.approx(0.8709, abs=0.0015)


def test_computer_modern_keeps_its_measured_factors() -> None:
    text, title = FONTS.factors["sans"]
    serif, _ = FONTS.factors["serif"]
    bold = 1 + (FONTS.style["sans"]["bold"] - 1) / 2
    for font, expected in (("CMSS10", text), ("SFSS1000", text), ("LMSans10-Regular", text), ("CMR10", serif),
                           ("SFSS0800", text / optical_width("sans", 8)), ("CMSS17", text / optical_width("sans", 17)),
                           ("SFSS1200", title), ("CMSSBX10", text * bold), ("SFSX1000", text * bold),
                           ("ArialUnknownFace", text)):
        assert factor(font) == pytest.approx(expected, rel=2e-4), font
    tt = FONTS(tex_run("x = 1", "CMTT10", 10.0), 1.0)[1]
    assert tt == round(10.0 * CMTT_ADVANCE_EM * design_width(DESIGN_WIDTH["mono"], 10) / ROBOTO_MONO_ADVANCE_EM, 1)


@pytest.mark.parametrize(("font", "before"), [("SFSX1200", 0.918), ("SFSX1440", 0.892)])
def test_a_bold_extended_sans_title_is_as_wide_as_its_own_cut(font: str, before: float) -> None:
    # SFSX is wider per em above 10 pt (BOLD_SANS_DESIGN_WIDTH), where CM Sans's regular cuts are
    # narrower: taken for CMSS's widths a 12 pt title came out 8% narrow, a 14.4 pt one 11%
    design = font_info(font).design_size
    assert design is not None
    slides = ADVANCES["Lato"]["bold"]
    sentences = SHAPE_TITLE_REFERENCE if 11.5 <= design < 14 else SHAPE_REFERENCE
    s_em = p_em = 0.0
    for text in sentences:
        s, p, _, _ = advance_widths(text, CM_ADVANCES["cmssbx10"], slides)
        s_em, p_em = s_em + s, p_em + p
    pdf = p_em * design_width(BOLD_SANS_DESIGN_WIDTH, design)
    ratio = s_em / factor(font) / pdf
    assert 0.95 < ratio < 1.0, ratio      # (bold is half corrected: Lato Bold runs 7% narrower than CMSSBX)
    assert ratio > before + 0.03


def test_a_long_run_of_capitals_is_shaped_in_its_own_face() -> None:
    caps = FONTS(tex_run("THE QUICK BROWN FOX JUMPS OVER", "BeraSans-Roman", 10.0), 1.0)[1]
    prose = FONTS(tex_run("The quick brown fox jumps over the lazy dog", "BeraSans-Roman", 10.0), 1.0)[1]
    assert caps < prose - 0.5   # Lato's capitals are wider against Vera's than its lower case


def test_bold_libertine_is_half_corrected() -> None:
    rel = slides_over_pdf("LinLibertineTB", SHAPE_REFERENCE, factor("LinLibertineTB")) / \
        slides_over_pdf("LinLibertineT", SHAPE_REFERENCE, factor("LinLibertineT"))
    assert FONTS.width_ratio("LinLibertineTB", "serif", True, False) == pytest.approx(rel, abs=0.003)
    assert FONTS.width_ratio("LinLibertineT", "serif", False, False) == 1.0


def test_inconsolata_columns_are_its_half_em() -> None:
    size = FONTS(tex_run("for (int i = 0; i < n; i++)", "Inconsolatazi4-Regular", 10.0), 1.0)
    assert size == ("Roboto Mono", round(10.0 * 0.5 / ROBOTO_MONO_ADVANCE_EM, 1))


def test_code_on_a_column_grid_is_sized_to_its_pitch() -> None:
    # esi-dev1-slides, talks: listings' columns=fixed in CM sans, 0.6 em columns. Sized as CMTT's
    # 0.525 em advances, Roboto Mono's columns came out 12.5% narrower than the PDF's
    spans = raw_spans(fixed_columns(CODE, "CMSS10"))
    [box] = text_boxes(spans)
    runs = [r for p in box["paragraphs"] for r in p["runs"] if r["text"].strip()]
    assert runs and {r["family"] for r in runs} == {"mono"}
    assert {r.get("pitch") for r in runs} == {0.6}
    for r in runs:
        family, z = FONTS(ir.run_json(r), 1.0)
        assert family == "Roboto Mono"
        assert z * ROBOTO_MONO_ADVANCE_EM == pytest.approx(0.6 * r["size"], abs=0.05)


def test_a_pitch_goes_through_the_ir() -> None:
    d: JsonObject = {**tex_run("x = 1", "CMSS10", 10.0), "family": "mono", "script": None, "underline": False,
                     "highlight": None, "pitch": 0.6}
    parsed = run(d, At(where="deck.json", path="run"))
    assert isinstance(parsed, Run) and parsed.pitch == 0.6
    assert run_json(parsed)["pitch"] == 0.6
    assert set_run(parsed).pitch == 0.6 and run_of(d).pitch == 0.6
    plain = run({k: v for k, v in d.items() if k != "pitch"}, At(where="deck.json", path="run"))
    assert "pitch" not in run_json(plain)


def test_pull_compares_a_tex_text_face_as_the_deck_reads_it_back() -> None:
    # deck_ir reads Slides' PT Serif back through CM's factors: a 10 pt Libertine run reads back
    # smaller, and the loop's current side says so too; CM keeps its own size
    libertine = tex_run("The quick brown fox jumps over the lazy dog", "LinLibertineT", 10.0)
    back = read_back_size(libertine, 10.0)
    assert back is not None and 8.9 < back < 9.6
    assert read_back_size(tex_run("The quick brown fox", "CMSS10", 10.0), 10.0) == 10.0
    assert read_back_size(tex_run("The quick brown fox", "ArialMT", 10.0), 10.0) == 10.0


def test_the_text_advances_table_parses() -> None:
    assert set(TEXT_ADVANCES) >= {"libertine", "biolinum", "bera_sans", "dejavu_sans", "palatino", "inconsolata"}
    assert set(TEXT_ADVANCES["libertine"]) == {"regular", "bold", "italic", "bold_italic"}
    with pytest.raises(JsonShapeError):
        parse_text_advances({"fonts": {"libertine": {"regular": {"advances": {"a": "wide"}, "kerns": {},
                                                                 "space": 0.25, "extra_space": 0.0}}}}, "test")
