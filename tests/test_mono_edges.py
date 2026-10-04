"""The word space at an edge of inline code is the prose font's in TeX (0.333 em in CM Sans) and
Lato's 0.193 em in Slides: six-per-em spaces before it, in the prose run, bring it back
(`mono_edges`, `classify_paragraphs.code_edge`; 'cvsimport,git', real_gittalk-gittalk s13; the
widths from tools/probe_mono_edges.py). emit keeps them, and a sentence's U+2008, only on a line
they leave no wider than the PDF's (`emit_text.within_budget`: 'Mistake:  string types',
real_talksx s47)."""

import pytest

from beamer2slides.compare import norm_text
from beamer2slides.emit_metrics import SOFT_BREAK, SYMBOL_ADVANCE_EM, FontMapper
from beamer2slides.emit_model import SetParagraph, SetRun, run_of
from beamer2slides.emit_text import FIXED_ADVANCE_EM, SPACE_BUDGET, within_budget
from beamer2slides.emit_widths import slides_width_of
from beamer2slides.inverse import latex_escape
from beamer2slides.ir import Run
from beamer2slides.ir_types import Line as PdfLine
from beamer2slides.merge import collapse_holes
from beamer2slides.mono_edges import EDGE_MAX, EDGE_SPACE, EDGE_SPACE_EM, LATO_SIZE, PLAIN_SPACE_EM, edge_fill, edge_width
from beamer2slides.text_layout import advance

from .test_inline_math_spacing import BODY, SANS, THIN, WORD, W, Line, runs, text

TT = "CMTT10"
SPACE = 0.333 * BODY  # TeX's word space beside \texttt, the prose font's (real decks: 0.326-0.334 em)
E = EDGE_SPACE


def code_line(x0: float) -> Line:
    """'set the max_iter option', the code word in CMTT10."""
    return Line(x0).add("set the", SANS, 0.0, False).add("max_iter", TT, SPACE, False).add("option", SANS, SPACE, False)


def ends_at(x1: float, pieces: list[tuple[str, str, float]]) -> Line:
    """A line of (text, font, gap before) pieces ending at `x1`."""
    probe = Line(0.0)
    for words, font, gap in pieces:
        probe.add(words, font, gap, False)
    line = Line(x1 - probe.x)
    for words, font, gap in pieces:
        line.add(words, font, gap, False)
    return line


def mono_runs(rs: list[Run]) -> list[str]:
    return [r["text"] for r in rs if r["family"] == "mono"]


def test_a_tex_word_space_gets_one_edge_space_and_a_double_one_three() -> None:
    assert edge_fill(0.333) == E
    assert edge_fill(0.313) == E  # (CM Sans 17 pt)
    assert edge_fill(0.627) == E * 3  # (real_talksx's \code adds a space of its own)
    assert edge_fill(0.19) == ""  # (a gap a plain space already makes)
    assert edge_fill(0.0) == ""
    assert edge_fill(5.0) == E * EDGE_MAX
    # what a fill adds is what emit measures it as, at Lato's size
    assert edge_width(E * 2) == pytest.approx(2 * EDGE_SPACE_EM * LATO_SIZE)
    # one six-per-em space brings Slides' gap nearer TeX's than the plain space alone
    plain = PLAIN_SPACE_EM * LATO_SIZE
    assert abs(plain + edge_width(E) - 0.333) < abs(plain - 0.333)


def test_both_edges_of_inline_code_get_their_width_in_the_prose_run() -> None:
    rs = runs(code_line(40.0).spans)
    assert text(rs) == f"set the{E} max_iter{E} option", text(rs)
    # the code's run holds the code alone: a space in Roboto Mono would be 0.525 em
    assert mono_runs(rs) == ["max_iter"]
    assert all(r["family"] == "sans" for r in rs if E in r["text"])


def test_the_thin_space_stands_before_the_space_a_line_may_break_at() -> None:
    # (a line Slides breaks there leaves it at the end of the line before, not before the next word)
    t = text(runs(code_line(40.0).spans))
    assert f" {E}" not in t and t.count(f"{E} ") == 2, t


def test_punctuation_against_the_code_gets_none() -> None:
    line = Line(40.0).add("Always compile with", SANS, 0.0, False).add("-Wall", TT, SPACE, False) \
        .add(".", SANS, THIN, False)
    assert text(runs(line.spans)) == f"Always compile with{E} -Wall.", text(runs(line.spans))


def test_a_line_with_no_room_keeps_its_plain_spaces() -> None:
    # alone, left-aligned, its right end near the page's edge: wider gaps would push it past it
    line = ends_at(W - 4.0, [("set the", SANS, 0.0), ("max_iter", TT, SPACE), ("option", SANS, SPACE)])
    assert E not in text(runs(line.spans))


def test_a_line_takes_all_its_edges_or_none() -> None:
    # room for one edge's thin space, not for two: none, so that the code words on it stand alike
    room = (edge_width(E) * 1.5 + 1.0) * BODY  # (WIDE_ROOM_EM besides)
    one = ends_at(W - room, [("set the", SANS, 0.0), ("max_iter", TT, SPACE)])
    assert text(runs(one.spans)) == f"set the{E} max_iter", text(runs(one.spans))
    two = ends_at(W - room, [("set the", SANS, 0.0), ("max_iter", TT, SPACE), ("option", SANS, SPACE)])
    assert E not in text(runs(two.spans))


def test_serif_prose_and_words_without_code_keep_plain_spaces() -> None:
    # (PT Serif's space is 0.239 em, and nobody measured U+2009 in it)
    serif = Line(40.0).add("set the", "CMR10", 0.0, False).add("max_iter", TT, SPACE, False) \
        .add("option", "CMR10", SPACE, False)
    assert text(runs(serif.spans)) == "set the max_iter option"
    prose = Line(40.0).add("set the", SANS, 0.0, False).add("max", SANS, WORD, False).add("option", SANS, WORD, False)
    assert E not in text(runs(prose.spans))


def test_readers_take_the_thin_space_and_its_space_for_one_space() -> None:
    assert latex_escape(f"set the{E} max{E}{E} option") == "set the max option"
    assert norm_text(f"set the{E} max") == "set the max"


def test_emit_measures_the_thin_space() -> None:
    assert SYMBOL_ADVANCE_EM[E] == EDGE_SPACE_EM
    assert advance(E, {"fontFamily": "Lato"}, 10.0) == pytest.approx(10 * EDGE_SPACE_EM)
    fonts = FontMapper()

    def width(t: str) -> float:
        w = slides_width_of([run_of({"text": t, "font": SANS, "family": "sans", "size": BODY, "bold": False,
                                     "italic": False, "smallcaps": False, "color": "#000000", "link": None,
                                     "script": None})], 1.0, fonts)
        assert w is not None
        return w
    size = fonts.size_of(run_of({"text": "a", "font": SANS, "family": "sans", "size": BODY, "bold": False,
                                 "italic": False, "smallcaps": False, "color": "#000000", "link": None,
                                 "script": None}), 1.0)[1]
    assert width(f"a{E} b") - width("a b") == pytest.approx(EDGE_SPACE_EM * size)


# emit's budget: an added space only on a line Slides sets no wider than the PDF's

FONTS = FontMapper()
TH = " "  # classify_text.widened's thick space, before a word space


def set_run(t: str, font: str, family: str) -> SetRun:
    return run_of({"text": t, "font": font, "family": family, "size": BODY, "bold": False, "italic": False,
                   "smallcaps": False, "color": "#000000", "link": None, "script": None})


def sans(t: str) -> SetRun:
    return set_run(t, SANS, "sans")


def para(rs: list[SetRun], extents: list[float], starts: tuple[int, ...] | None) -> SetParagraph:
    lines = tuple(PdfLine(baseline=100.0 + 20 * k, x0=40.0, x1=40.0 + w) for k, w in enumerate(extents))
    return SetParagraph(runs=tuple(rs), lines=lines, size=BODY, text_x0=40.0, align="center", level=0,
                        bullet=None, direction=None, justified=False, wrap_limit=None, tab_x0=None,
                        line_starts=starts)


def slides(t: str) -> float:
    w = slides_width_of([sans(t)], 1.0, FONTS)
    assert w is not None
    return w


def written(p: SetParagraph) -> str:
    return "".join(r.text for r in within_budget(p, 1.0, FONTS).runs)


def test_a_centred_title_keeps_its_thick_space_only_within_the_pdf_line() -> None:
    title = f"Mistake:{TH} string types"
    wide = slides(title)
    assert written(para([sans(title)], [wide], None)) == title
    assert written(para([sans(title)], [wide * (1 + SPACE_BUDGET) - 0.3], None)) == title
    # real_talksx s47: 4.1% wider than the PDF's line with it, every word shifted
    assert written(para([sans(title)], [wide / 1.04], None)) == "Mistake: string types"


def test_code_edges_go_and_their_word_spaces_stay() -> None:
    rs = [sans(f"set the{E}{E} "), set_run("max_iter", TT, "mono"), sans(f"{E} option")]
    wide = slides_width_of(rs, 1.0, FONTS)
    assert wide is not None
    assert written(para(rs, [wide + 0.5], None)) == f"set the{E}{E} max_iter{E} option"
    tight = within_budget(para(rs, [wide - 3.0], None), 1.0, FONTS)
    assert [r.text for r in tight.runs] == ["set the ", "max_iter", " option"]


def test_each_line_is_judged_alone() -> None:
    first, second = f"Mistake:{TH} strings", f"Questions?{TH} Comments?"
    t = first + SOFT_BREAK + second
    assert written(para([sans(t)], [slides(first) + 1.0, slides(second) / 1.04], None)) == \
        first + SOFT_BREAK + "Questions? Comments?"


def test_the_recorded_line_starts_follow_the_text() -> None:
    one, two = f"Done.{TH} Then more words ", "on the second line"
    t = one + two
    p = within_budget(para([sans(t)], [slides(one.rstrip()) / 1.04, slides(two)], (len(one), len(t))), 1.0, FONTS)
    kept = "".join(r.text for r in p.runs)
    assert kept == "Done. Then more words on the second line"
    assert p.line_starts == (len(one) - 1, len(kept))


def test_lines_that_cannot_be_told_keep_no_added_space() -> None:
    t = f"Mistake:{TH} strings{SOFT_BREAK}and more"  # two lines written, three on the page
    assert TH not in written(para([sans(t)], [500.0, 500.0, 500.0], None))


def test_a_courier_line_is_measured_at_its_one_advance() -> None:
    # (real_talksx's \code: Nimbus Mono, written Courier New, which slides_width never probed)
    rs = [sans(f"use{E} "), set_run("String", "NimbusMonL-Regu", "mono"), sans(f"{E} here")]
    assert slides_width_of(rs, 1.0, FONTS) is None
    code = 6 * FIXED_ADVANCE_EM["Courier New"] * FONTS.size_of(rs[1], 1.0)[1]
    wide = slides(f"use{E} ") + code + slides(f"{E} here")
    assert written(para(rs, [wide + 0.5], None)) == f"use{E} String{E} here"
    assert written(para(rs, [wide - 3.0], None)) == "use String here"


def test_sync_reads_a_dropped_space_and_a_kept_one_alike() -> None:
    assert collapse_holes(f"Mistake:{TH} string types") == collapse_holes("Mistake: string types")
    assert collapse_holes(f"set the{E}{E} max_iter{E} option") == "set the max_iter option"
