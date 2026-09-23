"""Font names and encodings the visual hunt found read wrong (docs/project-notes.md "Visual hunt",
fonts family): unknown font names lost their class and style, OpenType math Greek became the words
of its Unicode name, OT1's accents stayed beside their letters, \\not stayed a free slash, \\|
came out as "//", \\mathcal as a plain capital, and a bitmap TS1 euro as an inverted question mark.
Synthetic names and spans, no PDF."""

import pytest

from beamer2slides.classify import (Line, PageClassifier, Paragraph, Rect, Span, compose_accents, math_family,
                                    math_pieces, math_text, negate, type3_symbol)
from beamer2slides.fonts import FontInfo, font_info, google_font


@pytest.mark.parametrize("name, family, bold, italic, smallcaps", [
    ("TeXGyreTermesX-BoldItalic", "serif", True, True, False),       # newtx text
    ("t1xtt", "mono", False, False, False),                          # newtx typewriter
    ("URWPalladioL-Ital", "serif", False, True, False),              # mathpazo: Ital is italic
    ("URWPalladioL-BoldItal", "serif", True, True, False),
    ("TeXPalladioL-SC", "serif", False, False, True),                # and SC small caps
    ("LinBiolinumT", "sans", False, False, False),                   # libertine's sans
    ("LinBiolinumTB", "sans", True, False, False),
    ("LinBiolinumTBO", "sans", True, True, False),
    ("LinBiolinumTI", "sans", False, True, False),
    ("LinLibertineT", "serif", False, False, False),
    ("LinLibertineMT", "mono", False, False, False),
    ("SegoeUI", "sans", False, False, False),                        # fontspec system fonts
    ("SegoeUI-BoldItalic", "sans", True, True, False),
    ("YuGothic-Bold", "sans", True, False, False),
    ("BeraSansMono-Oblique", "mono", False, True, False),
    ("NimbusSanL-ReguItal", "sans", False, True, False),             # helvet
    ("NimbusRomNo9L-Medi", "serif", True, False, False),             # URW's Medi is Times' bold
    ("SourceSansPro-It", "sans", False, True, False),
    ("RobotoSlab-Regular", "serif", False, False, False),
    ("ArialUnicodeMS", "sans", False, False, False),                 # "code" in Unicode is not code
    ("FiraSans-Medium", "sans", False, False, False),                # Medium is not bold
    ("LMRomanCaps10-Regular", "serif", False, False, True),
])
def test_a_font_name_says_its_class_and_style(name, family, bold, italic, smallcaps):
    info = font_info(name)
    assert (info.family, info.bold, info.italic, info.smallcaps) == (family, bold, italic, smallcaps)


@pytest.mark.parametrize("name", ["NewTXMI", "txsys", "txmiaX", "PazoMath-Italic", "LibertinusT1Math"])
def test_newtx_and_friends_math_fonts_are_math(name):
    assert font_info(name).family == "math"


def test_stand_ins_set_the_pdfs_widths():
    # newtx's Termes with extra glyphs is Termes: Times New Roman, no CM size factor
    assert google_font("TeXGyreTermesX-Regular") == ("Times New Roman", 400, False)
    assert google_font("texgyretermesx-bold") == ("Times New Roman", 700, False)
    # a 0.6 em monospaced face is set by any 0.6 em monospaced one at the PDF's size
    assert google_font("BeraSansMono-Bold") == ("Roboto Mono", 700, False)
    assert google_font("LinLibertineMT") == ("Roboto Mono", 400, False)
    # a math font drawn to match a text face is shown in that face
    assert google_font("FiraMath-Regular")[0] == "Fira Sans"
    # CMTT-like typewriters stay the calibrated mono substitute
    assert google_font("t1xtt") is None and google_font("CMTT10") is None


@pytest.mark.parametrize("raw, composed", [
    ("Schr¨odinger", "Schrödinger"), ("Garc´ıa", "García"), ("Fran¸cois", "François"),
    ("S¸.", "Ş."), ("´Ecole", "École"), ("F´ed´erale", "Fédérale"), ("J¨org", "Jörg"),
    ("Var(¯", "Var(¯"),  # an accent for the next span's letter is the runs' business
    ("x ˆ y", "x ˆ y"),  # an accent that is over no letter stays
])
def test_ot1_accents_are_composed_with_their_letter(raw, composed):
    assert compose_accents(raw) == composed


def test_a_backquote_in_code_is_no_accent():
    assert compose_accents("`ls`", mono=True) == "`ls`"


def test_not_before_a_relation_is_the_negated_relation():
    assert negate(" ̸ = 0.") == " ≠ 0."
    assert negate("̸⊆") == "⊈"
    assert negate("̸∈") == "∉"
    assert negate("̸≺") == "⊀"
    assert negate("̸⋈") == "⋈̸"  # no precomposed form: the relation, then the slash


def test_opentype_math_letters_are_letters_not_their_unicode_names():
    text, italic = math_text("CambriaMath", "𝜌 = 𝜆/(𝑐𝜇) < 1")
    assert text == "ρ = λ/(cμ) < 1" and italic
    assert math_text("FiraMath-Regular", "𝜃 𝜕 𝜙 ∇ ℎ")[0] == "θ ∂ φ ∇ h"
    assert math_text("LibertinusT1Math", " 𝛽")[0] == " β"


def test_opentype_math_slants_its_letters_only():
    assert math_pieces("CambriaMath", "𝜌 = 𝜆/(𝑐𝜇) < 1") == [
        ("ρ ", True), ("= ", False), ("λ", True), ("/(", False), ("cμ", True), (") < 1", False)]
    # the conditioning bar stays upright (italic, it read as a slash)
    assert ("| ", False) in math_pieces("FiraMath-Regular", "𝑥 ∣ 𝑧")
    assert math_pieces("LibertinusT1Math", " 𝛼") == [(" α", True)]


@pytest.mark.parametrize("font", ["CMMI10", "LMMathItalic10-Regular", "NewTXMI", "PazoMath-Italic"])
def test_tex_math_italic_fonts_are_italic(font):
    assert math_text(font, "τ") == ("τ", True)


def test_cmsy_letters_are_calligraphic_and_norm_bars_upright():
    assert math_text("CMSY10", "A") == ("𝒜", False)
    assert math_text("LMMathSymbols10-Regular", "L F O N") == ("ℒ ℱ 𝒪 𝒩", False)
    assert math_text("rsfs10", "P") == ("𝒫", False)
    assert math_text("EUFM10", "g") == ("𝔤", False)
    assert math_text("CMSY10", "∥")[0] == "||"
    assert math_text("MSBM10", "R")[0] == "ℝ"


def test_a_bitmap_ts1_glyph_is_its_symbol_unless_the_page_text_is_bitmap():
    assert type3_symbol("¿", type3_words=False) == "€"
    assert type3_symbol("¿", type3_words=True) == "¿"  # T1's 0xBF is £: not a TS1 euro
    assert type3_symbol("\x88", type3_words=True) == "•"


def raw_span(i, text, font, x0, x1, baseline=100.0, size=10.0):
    return {"id": f"s{i}", "text": text, "font": font, "size": size, "color": "#000000", "alpha": 255,
            "origin": [x0, baseline], "bbox": [x0, baseline - 0.75 * size, x1, baseline + 0.25 * size],
            "dir": [1.0, 0.0], "smallcaps": False}


def runs_of(raw_spans: list[dict]) -> list[dict]:
    page = PageClassifier({"size": [364, 273], "spans": raw_spans, "links": []}, 10)
    line = Line(page.spans())
    return PageClassifier.runs(Paragraph([line]))


def test_page_spans_compose_accents_and_ts1_symbols():
    runs = runs_of([raw_span(0, "Schr¨odinger", "CMSS10", 10, 60), raw_span(1, "1.42", "CMSS10", 64, 80),
                    raw_span(2, "M", "CMSS10", 82, 90), raw_span(3, "¿", "Type3", 90, 97)])
    assert "".join(r["text"] for r in runs) == "Schrödinger 1.42 M€"


def test_not_in_one_font_and_its_relation_in_another_are_one_symbol():
    runs = runs_of([raw_span(0, "Cov(Z,", "CMR10", 10, 40), raw_span(1, "D)", "CMMI10", 42, 52),
                    raw_span(2, " ̸", "CMSY10", 52, 60), raw_span(3, "=", "CMR10", 55, 63),
                    raw_span(4, "0", "CMR10", 66, 71)])
    text = "".join(r["text"] for r in runs)
    assert "≠" in text and "̸" not in text


def test_math_takes_the_family_of_the_words_around_it_not_the_longest_span():
    # the formula is the line's longest span, the prose around it is sans
    runs = runs_of([raw_span(0, "z", "CMMI10", 10, 15), raw_span(1, " = μ(x) + σ(x) ⊙ ε", "CMMI10", 15, 95),
                    raw_span(2, " with", "CMSS10", 95, 115)])
    assert {r["family"] for r in runs} == {"sans"}
    # after inline code the formula is not code
    runs = runs_of([raw_span(0, "Use", "CMSS10", 10, 25), raw_span(1, " torch.distributions", "CMTT10", 25, 110),
                    raw_span(2, " θ", "CMMI10", 112, 118)])
    assert [r["family"] for r in runs if "θ" in r["text"]] == ["sans"]


def test_math_family_falls_back_to_the_paragraph_then_serif():
    info = FontInfo("math")
    formula = Span("m", "x", "CMMI10", 10, "#000000", Rect(0, 0, 5, 10), 10, True, info)
    words = Span("w", "some words", "CMSS10", 10, "#000000", Rect(0, 20, 50, 30), 30, True, font_info("CMSS10"))
    alone = Line([formula])
    assert math_family(alone) == "serif"
    assert math_family(alone, Paragraph([alone, Line([words])])) == "sans"
