"""After a sub- or superscript Slides' advance falls short of where TeX sets the next glyph
(\\scriptspace, the 0.7 script size against Slides' 0.665, the faces' widths: 0.12 em of the line
on the real decks), so a comma or bracket right after a script ran into it ('D_{k,i},',
real_lecture-phylogenet s15; 'K_Y.', beamer-derived-cat s19). Hair spaces (U+200A) closing the
script run bring the next glyph back (`script_space`, `classify_paragraphs.script_tail`); emit
keeps them only on a line no wider than the PDF's (`emit_text.within_budget`), and every reader
takes them for nothing."""

import pytest

from beamer2slides.compare import norm_text
from beamer2slides.emit_metrics import SYMBOL_ADVANCE_EM, FontMapper
from beamer2slides.emit_model import SetParagraph, SetRun, run_of
from beamer2slides.emit_text import within_budget
from beamer2slides.emit_widths import SCRIPT_SIZE, first_break, slides_lines_of, slides_width_of
from beamer2slides.inverse import latex_escape
from beamer2slides.ir import Run
from beamer2slides.ir_types import Line as PdfLine
from beamer2slides.json_types import JsonObject
from beamer2slides.merge import collapse_holes
from beamer2slides.script_space import SCRIPT_SPACE, SCRIPT_SPACE_EM, SCRIPT_SPACE_MAX, script_fill
from beamer2slides.texmap import WORD_RE
from beamer2slides.text_layout import advance, wrap

from .test_inline_math_spacing import BODY, SANS, SCRIPT, THIN, WORD, Line, runs, text

HS = SCRIPT_SPACE
MATH, MATH_SCRIPT = "CMMI10", "CMMI7"


def scripted(after: str, gap: float) -> list[Run]:
    """'the sets D_{k,i}' and then `after`, `gap` pt after the script."""
    line = Line(40.0).add("the sets", SANS, 0.0, False).add("D", MATH, WORD, False).add("k,i", MATH_SCRIPT, THIN, True)
    return runs(line.add(after, MATH if after.strip() in ",)" else SANS, gap, False).spans)


def test_a_shortfall_is_hair_spaces_of_the_drawn_size_and_never_more_than_the_cap() -> None:
    assert script_fill(SCRIPT_SPACE_EM * 10.0, 10.0) == HS
    assert script_fill(SCRIPT_SPACE_EM * 10.0 * 2.4, 10.0) == HS * 2
    assert script_fill(SCRIPT_SPACE_EM * 10.0 * 0.4, 10.0) == ""  # (less than half a hair space)
    assert script_fill(100.0, 10.0) == HS * SCRIPT_SPACE_MAX
    assert script_fill(-1.0, 10.0) == ""  # Slides' script already reaches the next glyph
    assert script_fill(1.0, 0.0) == ""


def test_a_comma_right_after_a_script_stands_where_tex_set_it() -> None:
    rs = scripted(",", THIN)
    (sub,) = [r for r in rs if r["script"] == "sub"]
    assert sub["text"].rstrip(HS) == "k,i" and sub["text"].endswith(HS), text(rs)
    assert text(rs).replace(HS, "") == "the sets Dk,i,"


def test_the_fill_is_what_slides_falls_short_of_the_pdf() -> None:
    rs = scripted(",", THIN)
    (sub,) = [r for r in rs if r["script"] == "sub"]
    fonts = FontMapper()
    word = run_of({"text": "k,i", "font": sub["font"], "family": sub["family"], "size": sub["size"], "bold": False,
                   "italic": sub["italic"], "smallcaps": False, "color": "#000000", "link": None, "script": "sub"})
    width = slides_width_of([word], 1.0, fonts)
    assert width is not None
    distance = 3 * 0.5 * SCRIPT  # (test_inline_math_spacing.Line: half an em of the span's size a letter)
    assert distance > width  # (Slides falls short here, as on the real decks)
    assert sub["text"].count(HS) == len(script_fill(distance - width, fonts.size_of(word, 1.0)[1] * SCRIPT_SIZE))


def test_a_script_before_a_word_space_gets_none() -> None:
    rs = scripted("are disjoint", WORD)
    assert HS not in text(rs), text(rs)


def test_a_script_ending_its_line_gets_none() -> None:
    # (only a glyph of the line after the script takes the fill, never the script's own letters)
    line = Line(40.0).add("then", SANS, 0.0, False).add("f", MATH, WORD, False).add("s", MATH_SCRIPT, THIN, True) \
        .add("t", MATH_SCRIPT, THIN, True)
    assert HS not in text(runs(line.spans))


def test_readers_take_the_hair_space_for_nothing() -> None:
    assert latex_escape(f"D_k{HS}{HS},") == latex_escape("D_k,")
    assert norm_text(f"Dk,i{HS}{HS},") == norm_text("Dk,i,")
    assert collapse_holes(f"Dk,i{HS},") == "Dk,i,"
    assert WORD_RE.findall(f"the sets Dk,i{HS}{HS}, are") == ["the", "sets", f"Dk,i{HS}{HS},", "are"]


def test_emit_measures_the_hair_space_at_the_drawn_size() -> None:
    assert SYMBOL_ADVANCE_EM[HS] == SCRIPT_SPACE_EM
    assert advance(HS, {"fontFamily": "Lato"}, 10.0) == pytest.approx(10 * SCRIPT_SPACE_EM)
    fonts = FontMapper()

    def sub(t: str) -> SetRun:
        return run_of({"text": t, "font": MATH_SCRIPT, "family": "serif", "size": BODY, "bold": False,
                       "italic": True, "smallcaps": False, "color": "#000000", "link": None, "script": "sub"})
    with_fill, without = slides_width_of([sub(f"k{HS}{HS}")], 1.0, fonts), slides_width_of([sub("k")], 1.0, fonts)
    assert with_fill is not None and without is not None
    drawn = fonts.size_of(sub("k"), 1.0)[1] * SCRIPT_SIZE
    assert with_fill - without == pytest.approx(2 * SCRIPT_SPACE_EM * drawn)


def test_a_line_wider_than_the_pdf_drops_its_hair_spaces() -> None:
    fonts = FontMapper()

    def run(t: str, script: str | None) -> SetRun:
        return run_of({"text": t, "font": SANS, "family": "sans", "size": BODY, "bold": False, "italic": False,
                       "smallcaps": False, "color": "#000000", "link": None, "script": "sub" if script else None})
    rs = [run("the sets D", None), run(f"k{HS}{HS}", "sub"), run(", are", None)]
    wide = slides_width_of(rs, 1.0, fonts)
    assert wide is not None

    def para(extent: float) -> SetParagraph:
        return SetParagraph(runs=tuple(rs), lines=(PdfLine(baseline=100.0, x0=40.0, x1=40.0 + extent),), size=BODY,
                            text_x0=40.0, align="left", level=0, bullet=None, direction=None, justified=False,
                            wrap_limit=None, tab_x0=None, line_starts=None)
    assert "".join(r.text for r in within_budget(para(wide + 0.5), 1.0, fonts).runs) == f"the sets Dk{HS}{HS}, are"
    assert "".join(r.text for r in within_budget(para(wide - 3.0), 1.0, fonts).runs) == "the sets Dk, are"


def test_the_next_word_joins_a_line_up_to_its_hair_spaces() -> None:
    """Slides breaks after the hair spaces closing a script as after a space and lets what cannot
    start a line go along: 'x_t' + two hair spaces + '.' stayed at the end of a line it ran 5.7 pt
    past, where 'x_t.' wrapped (real_linear-attention-a s28). The box is sized against 'x_t'."""
    fonts = FontMapper()

    def run(t: str, script: str | None) -> SetRun:
        return run_of({"text": t, "font": SANS, "family": "sans", "size": BODY, "bold": False, "italic": False,
                       "smallcaps": False, "color": "#000000", "link": None, "script": script})

    def para(fill: str, after: str) -> SetParagraph:
        rs = (run("a decay term that is a function of ", None), run("x", None), run(f"t{fill}", "sub"), run(after, None))
        text = "".join(r.text for r in rs)
        return SetParagraph(runs=rs, lines=(PdfLine(baseline=100.0, x0=40.0, x1=200.0),
                                            PdfLine(baseline=112.0, x0=40.0, x1=50.0)),
                            size=BODY, text_x0=40.0, align="left", level=0, bullet=None, direction=None,
                            justified=False, wrap_limit=None, tab_x0=None,
                            line_starts=(text.index("x"), len(text)))
    filled, plain, cut = (slides_lines_of(para(fill, after), 1.0, fonts)
                          for fill, after in ((HS * 2, "."), ("", "."), ("", "")))
    assert filled is not None and plain is not None and cut is not None
    assert filled[1] == pytest.approx(cut[1]) and filled[1] < plain[1]
    assert first_break(f"x{HS}{HS}.", 0, 4) == 1


def test_the_layout_lets_a_word_go_along_past_its_hair_spaces() -> None:
    style: JsonObject = {"fontFamily": "Lato", "fontSize": 20.0}
    room = advance("a", style, 20.0) * 2 + advance(" ", style, 20.0) + advance("x", style, 20.0) + 1.0
    for word, first in ((f"x{HS}{HS}.", f"aa x{HS}{HS}."), ("xy.", "aa ")):
        text = "aa " + word + " b"
        lines = wrap(text, [style] * len(text), room)
        assert text[lines[0][0]:lines[0][1]].rstrip(" ") == first.rstrip(" "), lines
