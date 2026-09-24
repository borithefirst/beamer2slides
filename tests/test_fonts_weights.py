"""Fonts, weights and spacing the visual hunt found wrong after waves 1 and 2 (out/hunt/WAVE3.md,
fixer X): a 6 pt footline set in a lighter face than the PDF's, Japanese in a fallback face that
Slides draws bold, a CM math-italic letter among sans words in a heavy sans italic, word spaces
lost at span joins. Synthetic runs and spans, no PDF."""

import pytest

from beamer2slides import emit
from beamer2slides.fonts import google_font

from .test_emit_hunt import styled, text_element
from .test_emit_requests import FONTS, SCALE, run_of


# ---------------------------------------------------------------- optical weight

def test_a_six_point_sans_cut_is_set_heavier_than_regular():
    # sci v1, control c1 (r6): beamer's \tiny footline in SFSS0600 ('M. Keller', 'June 2026') came
    # out in Lato Regular, visibly lighter than CM's 6 pt cut, whose stems are 1.38x the 10.95 pt one's.
    style, fields = FONTS.text_style(run_of("June 2026", 5.98, font="SFSS0600"), SCALE)
    assert style["weightedFontFamily"] == {"fontFamily": "Lato", "weight": emit.OPTICAL_WEIGHT}
    assert "weightedFontFamily" in fields and "fontFamily" not in fields
    # r8: Slides draws Lato 500 and 600 as its Regular (probe_font_weights: ink 79.0 per pt at 400
    # and 600, 106.9 at 700 and 800): the footline came out as light as before. 800 draws Bold,
    # and reads back as a weight our own bold (700) never writes. No `bold` field: the API
    # applies it after the weight, and a `bold: false` could take the weight back to 400.
    assert emit.OPTICAL_WEIGHT == 800 and "bold" not in fields and "bold" not in style
    assert FONTS.text_style(run_of("June 2026", 5.98, font="ECSS0600"), SCALE)[0].get("weightedFontFamily")  # Type 3
    # The body text, an 8 pt cut and a bold footline keep the plain family.
    for run in (run_of("June 2026"), run_of("June 2026", 7.97, font="SFSS0800"), run_of("June 2026", 5.98, font="LMSans8-Regular"),
                run_of("June 2026", 5.98, font="SFSX0600", bold=True), run_of("June 2026", 5.98, font="SFRM0600", family="serif")):
        style = FONTS.text_style(run, SCALE)[0]
        assert "weightedFontFamily" not in style and style["bold"] is run["bold"], run["font"]


def test_an_optically_heavier_run_is_measured_in_the_face_slides_draws():
    tiny = run_of("Fatigue of Welded Joints", 5.98, font="SFSS0600")
    table = emit.ADVANCES["Lato"]["bold"]
    size = FONTS(tiny, SCALE)[1]
    assert emit.slides_width([tiny], SCALE, FONTS) == pytest.approx(sum(table[c] for c in tiny["text"]) * size)


def read_back_runs(style: dict, foreign: bool = False) -> list[dict]:
    from beamer2slides.deck_ir import StyleResolver, text_paragraphs
    text = {"textElements": [{"startIndex": 0, "endIndex": 10, "paragraphMarker": {"style": {}}},
                             {"startIndex": 0, "endIndex": 10, "textRun": {"content": "June 2026\n", "style": style}}]}
    pe = {"objectId": "b2s_s001_t0", "shape": {"shapeType": "TEXT_BOX", "text": text}}
    return [r for p in text_paragraphs(pe, text, StyleResolver({}), FONTS, SCALE, foreign=foreign) for r in p["runs"]]


def test_an_optically_heavier_run_reads_back_regular():
    # deck_ir counted weight 600 as bold: pull would have written the footline as \textbf. Slides
    # reads our 800 back as `bold: true` with the weight as written (probe_font_weights).
    style = {**FONTS.text_style(run_of("June 2026", 5.98, font="SFSS0600"), SCALE)[0], "bold": True}
    runs = read_back_runs(style)
    assert runs and not any(r["bold"] for r in runs)
    assert all(r["bold"] for r in read_back_runs(style, foreign=True))   # someone's extra bold in another deck
    # what decks converted before wrote (600, drawn Regular) stays regular; our own bold (700) is bold
    legacy = {**style, "weightedFontFamily": {"fontFamily": "Lato", "weight": 600}, "bold": False}
    assert not any(r["bold"] for r in read_back_runs(legacy))
    bold = {**style, "weightedFontFamily": {"fontFamily": "Lato", "weight": 700}}
    assert all(r["bold"] for r in read_back_runs(bold))


def test_an_optically_heavier_footline_pulls_back_with_no_residual():
    # the whole round trip on a deck whose footline is a 6 pt EC sans cut: emit's requests replayed
    # the way Google stores them (a weight of 700 and up reads back bold), read by deck_ir, compared
    import copy
    from beamer2slides.compare import compare
    from beamer2slides.deck_ir import deck_ir
    from .slides_sim import simulate
    from .test_inverse import TEXT_KINDS, built_pdf
    from beamer2slides.classify import classify
    from beamer2slides.extract import extract, select_overlays
    deck = copy.deepcopy(classify(select_overlays(extract(built_pdf("01_basic")), "last")))
    tiny = 0
    for s in deck["slides"]:
        for el in s["elements"]:
            if el["kind"] == "text" and el.get("role") != "title":
                for p in el["paragraphs"]:
                    for r in p["runs"]:
                        if r["family"] == "sans" and not r["bold"] and not r.get("script"):
                            r["font"], r["size"] = "SFSI0600" if r["italic"] else "SFSS0600", 5.98
                            tiny += 1
    assert tiny
    pres = simulate(deck)
    styles = [te["textRun"]["style"] for s in pres["slides"] for pe in s["pageElements"]
              for te in pe.get("shape", {}).get("text", {}).get("textElements", []) if "textRun" in te]
    assert any((st.get("weightedFontFamily") or {}).get("weight") == emit.OPTICAL_WEIGHT and st.get("bold")
               for st in styles)
    comp = compare(deck, deck_ir(pres, deck["slides"][0]["size"]))
    assert [r for r in comp.open() if r["kind"] in TEXT_KINDS] == []


def test_an_optically_heavier_run_is_laid_out_in_bold_widths():
    # text_layout (the layout oracle, sync's refit) measures the face Slides draws: a weight of 700
    # or more is the bold face even where the read-back says nothing of `bold`
    from beamer2slides import text_layout
    heavy = {"fontFamily": "Lato", "weight": emit.OPTICAL_WEIGHT}
    assert text_layout.advance("M", heavy, 10) == pytest.approx(emit.ADVANCES["Lato"]["bold"]["M"] * 10)
    assert text_layout.advance("M", {"fontFamily": "Lato", "weight": 600}, 10) == \
        pytest.approx(emit.ADVANCES["Lato"]["regular"]["M"] * 10)


def test_cm_sans_cuts_below_eight_points_have_their_own_width():
    # a 6 pt EC sans cut is 1.17 times as wide per em as the 10 pt one (sfss0600.pfb), not the
    # 8 pt cut's 1.06: the footline came out ~10% narrower than the PDF's
    six = run_of("Nanoparticle catalysis", 5.98, font="SFSS0600")
    ten = {**six, "font": "SFSS1000"}
    assert FONTS(six, SCALE)[1] / FONTS(ten, SCALE)[1] == pytest.approx(emit.OPTICAL_WIDTH_MAX, abs=0.02)
    seven = {**six, "font": "SFSS0700"}
    assert FONTS(seven, SCALE)[1] / FONTS(ten, SCALE)[1] == pytest.approx(emit.DESIGN_WIDTH["sans"][7], abs=0.02)


# ---------------------------------------------------------------- CJK faces

@pytest.mark.parametrize("name, face", [
    ("ABCDEF+YuGothic-Regular", ("Noto Sans JP", 400, False)),     # scripts ja: luatexja with Yu Gothic
    ("YuGothic-Bold", ("Noto Sans JP", 700, False)),
    ("YuMincho-Regular", ("Noto Serif JP", 400, False)),
    ("HaranoAjiMincho-Regular", ("Noto Serif JP", 400, False)),    # luatexja's default
    ("HiraKakuProN-W3", ("Noto Sans JP", 400, False)), ("HiraKakuProN-W6", ("Noto Sans JP", 700, False)),
    ("MS-Gothic", ("Noto Sans JP", 400, False)),
    ("FandolSong-Regular", ("Noto Serif SC", 400, False)),         # ctex's default
    ("SourceHanSansSC-Bold", ("Noto Sans SC", 700, False)),
    ("MalgunGothic", ("Noto Sans KR", 400, False)),
])
def test_a_cjk_face_is_its_noto_face(name, face):
    # scripts ja (r6): YuGothic was set in Lato, whose CJK fallback Slides draws bold and with
    # proportional full-width brackets: every Japanese run looked bold, （医療） became (医療)
    assert google_font(name) == face
    style = FONTS.text_style(run_of("分類（BERT）", font=name, bold=face[1] >= 700), SCALE)[0]
    assert style["weightedFontFamily"] == {"fontFamily": face[0], "weight": face[1]}


def test_a_math_letter_among_japanese_words_keeps_its_substitute():
    words = dict(font="YuGothic-Regular", family="sans")
    el = text_element([run_of("入力文 ", **words), run_of("x", font="CMMI10", family="sans", italic=True),
                       run_of(" に対し", **words)])
    x = next(style for text, style in styled(emit.text_box_requests(el, "b2s_s001", "b2s_s001_t0", SCALE, FONTS))
             if text == "x")
    assert x.get("fontFamily") == "Lato" and x["italic"] is True
