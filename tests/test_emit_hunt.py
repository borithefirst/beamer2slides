"""Defects the visual hunt found in what emit writes (out/hunt/verified), pinned offline: dot
leaders and runs inside a sentence sized like their neighbours, small optical cuts no taller than
the small-caps compromise, text ranges in UTF-16 units, XML-safe alt texts, table indents per
column and tables kept on the page."""


from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from beamer2slides import emit
from beamer2slides.emit import EMU_PER_PT, SLIDE_W
from beamer2slides.emit_model import JsonMap, table_of, text_of
from beamer2slides.emit_tables import TableLayout, pptx_table_of, table_layout_of
from beamer2slides.emit_text import text_box_requests_of
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.google_types import SlidesRequest, slides_json

from .json_reads import jarr, jat, jint, jnum, jobj, jobjs, jstr
from .test_emit_requests import FONTS, SCALE, column_widths, pt_of, slides_w, table_requests, text_requests, text_run

BODY = 10.91  # the size of every run here that names none (CMSS10 at 11 pt)


def text_element_of(paragraphs: Sequence[list[JsonObject]], bullet: JsonObject | None) -> JsonObject:
    """A text element of one-line paragraphs (runs each), 15 pt apart."""
    paras: list[Json] = []
    for i, runs in enumerate(paragraphs):
        b = 60.0 + 15 * i
        mark: Json = None if bullet is None else {**bullet, "bbox": [60.0, b - 6, 64.0, b - 2]}
        items: list[Json] = [r for r in runs]
        paras.append({"align": "left", "level": 0, "bullet": mark,
                      "size": 10.91, "text_x0": 70.0, "tab_x0": None, "wrap_limit": None, "runs": items,
                      "lines": [{"baseline": b, "x0": 70.0, "x1": 300.0}]})
    return {"id": "p0t0", "kind": "text", "role": "body", "code": False,
            "bbox": [60.0, 50.0, 300.0, 65.0 + 15 * len(paras)], "paragraphs": paras}


def text_element(*paragraphs: list[JsonObject], bullet: JsonObject | None = None) -> JsonObject:
    """`text_element_of`, as test_fonts_weights and test_fonts_wave5 call it."""
    return text_element_of(paragraphs, bullet)


def typed_text_requests(el: JsonObject, scale: float) -> list[SlidesRequest]:
    """`text_requests` as emit plans them, before they are JSON."""
    return text_box_requests_of(text_of(el), "b2s_s001", "b2s_s001_t0", scale, FONTS, None, None, None, None, None)


def styled(planned: Sequence[SlidesRequest]) -> list[tuple[str, JsonObject]]:
    """(the text each updateTextStyle range covers, its style), ranges read as Slides reads them:
    UTF-16 code units of the inserted text."""
    reqs = [slides_json(r) for r in planned]
    text = next(jstr(r, "insertText", "text") for r in reqs if "insertText" in r)
    units = text.encode("utf-16-le", "surrogatepass")
    out: list[tuple[str, JsonObject]] = []
    for r in reqs:
        st = r.get("updateTextStyle")
        if not st:
            continue
        style = jobj(st, "style")
        if jstr(st, "textRange", "type") == "FIXED_RANGE" and "baselineOffset" in style:
            a, b = jint(st, "textRange", "startIndex"), jint(st, "textRange", "endIndex")
            out.append((units[2 * a:2 * b].decode("utf-16-le", "surrogatepass"), style))
    return out


def measured(p: JsonMap, scale: float) -> tuple[float, float]:
    """`emit.slides_lines` of a paragraph it can measure: (its widest line, where the nearest
    next word joins)."""
    got = emit.slides_lines(p, scale, FONTS)
    assert got is not None, "a paragraph emit cannot measure"
    return got


# ---------------------------------------------------------------- sizes

def test_dot_leaders_and_ellipses_keep_the_size_factor() -> None:
    # textfx v3 s2 / v2 s10 / v3 s3: \dotfill and \ldots reach the text layer as '. . . .', read
    # as sentence ends with TeX's extra space: the runs were set 1.3-1.7 times too large.
    prose = FONTS(text_run("Monitor", BODY), SCALE)[1]
    for text in ("Welcome and coffee . . . . . . . . . . . . . . . . . . . . . . . . . .",
                 "Comments: . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . ",
                 "A: “Well . . . on the ones we tried, yes. On the others . . . we don’t",
                 "Things to bring: laptop, adapter, clicker, . . . , and a backup",
                 "A pause . . . or three dots... or an ellipsis at the end. . ."):
        assert FONTS(text_run(text, BODY), SCALE)[1] == prose, text


def test_a_run_among_others_keeps_their_size() -> None:
    # ml v3 s10, control c2 s12, sci v3 s9: an author list or a journal abbreviation of 15+
    # characters, sized to its own PDF width, came out 10% larger than the title beside it. (Their
    # letters are ordinary: since runs are sized by their letters alone, never their spaces, such
    # a list keeps the deck's size anyway; serif capitals still are sized.)
    authors = text_run("SPEAKER NOTES AND CAPITALS: ", BODY, font="CMR10", family="serif")
    title = text_run("Graph networks for crystal property prediction", BODY, font="CMSSI10", italic=True)
    prose = FONTS(text_run("Monitor", BODY, font="CMR10", family="serif"), SCALE)[1]
    assert FONTS(authors, SCALE)[1] != prose  # alone: sized to its width
    el = text_element_of([[authors, title], [text_run("SPEAKER NOTES AND CAPITALS: ", BODY, font="CMR10", family="serif")]], None)
    sizes = [(t, pt_of(s["fontSize"])) for t, s in styled(typed_text_requests(el, SCALE))]
    assert sizes[0] == ("SPEAKER NOTES AND CAPITALS: ", prose)
    assert sizes[-1] == ("SPEAKER NOTES AND CAPITALS: ", FONTS(authors, SCALE)[1])  # its own paragraph: as before
    assert jobj(el, "paragraphs", 0, "runs", 0) is authors and "in_sentence" not in authors  # the IR is left alone
    # In a table cell too.
    table: JsonObject = {"id": "p0b0", "kind": "table", "frame": [100.0, 50.0, 400.0, 70.0], "size": 10.91,
                         "columns": [{"x0": 106.0, "x1": 390.0, "align": "left"}],
                         "cells": [[[authors, {**title, "text": "Graph nets"}]]],
                         "row_baselines": [60.0], "row_heights": [15.0], "rules": []}
    cell = [pt_of(jat(r, "updateTextStyle", "style", "fontSize"))
            for r in table_requests(table, "s", "b2s_s001_b0", SCALE, False) if "updateTextStyle" in r]
    assert cell[0] == prose


def test_a_math_letter_among_words_in_a_google_font_takes_that_font() -> None:
    # lang v2 s6 (r6): Calibri moved to the sans family (fonts.font_info), and so the β between
    # Carlito words came out in Lato Italic, heavier than its words.
    beta_run = text_run("β", BODY, font="CMMI10", family="sans", italic=True)
    el = text_element_of([[text_run("The rate ", BODY, font="ABCDEF+Calibri", family="sans"), beta_run,
                           text_run(" is fitted per cohort", BODY, font="ABCDEF+Calibri", family="sans")]], None)
    beta = next(style for text, style in styled(typed_text_requests(el, SCALE)) if text == "β")
    assert jstr(beta, "weightedFontFamily", "fontFamily") == "Carlito" and beta["italic"] is True
    # Among TeX words it keeps the substitute, as before.
    el = text_element_of([[text_run("The rate ", BODY), beta_run, text_run(" is fitted", BODY)]], None)
    beta = next(style for text, style in styled(typed_text_requests(el, SCALE)) if text == "β")
    assert beta["fontFamily"] == "Lato"


def test_a_small_optical_cut_is_set_no_taller_than_the_small_caps_compromise() -> None:
    # dense v2 s1: a Boadilla footline in LMRoman5 at 4.98 pt. CM's 5 pt cut is 1.38 times as wide
    # per em as the 10 pt one; sized to that width, PT Serif's letters filled the footline bar.
    footline = text_run("Econometrics II", 4.98, font="LMRoman5-Regular", family="serif")
    ten = text_run("Econometrics II", 4.98, font="LMRoman10-Regular", family="serif")
    assert FONTS(footline, SCALE)[1] / FONTS(ten, SCALE)[1] == pytest.approx(emit.OPTICAL_WIDTH_MAX, abs=0.02)
    # 8 and 9 pt cuts are matched in full, as before.
    eight = text_run("Econometrics II", 7.97, font="CMR8", family="serif")
    assert FONTS(eight, SCALE)[1] / FONTS({**eight, "font": "CMR10"}, SCALE)[1] == \
        pytest.approx(emit.DESIGN_WIDTH["serif"][8], abs=0.02)


# ---------------------------------------------------------------- wrapped lines

def column_list_of() -> JsonObject:
    """themes v4 s3: two triangle items in a 0.48 textwidth column (Goettingen, a 4:3 page)."""
    def item(text: str, top: float, x1s: Sequence[float], limit: float) -> JsonObject:
        lines: list[Json] = [{"baseline": top + 12.16 + 13.54 * i, "x0": 34.97, "x1": x1} for i, x1 in enumerate(x1s)]
        return {"align": "left", "level": 0, "size": 10.91, "text_x0": 34.97, "tab_x0": None, "wrap_limit": limit,
                "bullet": {"kind": "glyph", "text": "▶", "color": "#3333b3", "bbox": [21.03, top, 29.51, top + 10.91]},
                "runs": [text_run(text, BODY)], "lines": lines}
    return {"id": "p2t1", "kind": "text", "role": "body", "code": False, "bbox": [21.03, 70.06, 144.54, 155.0],
            "paragraphs": [item("Mass bleaching in 2016, 2017, 2020, 2022 and 2024", 70.06, [144.54, 133.87, 56.79], 158.42),
                           item("Recovery between events now takes longer than the gap between", 113.7,
                                [116.29, 143.5, 133.92], 147.48)]}


def box_right(el: JsonMap, scale: float) -> float:
    """Where the box's text ends (Slides pt): its right edge less the inset."""
    reqs = text_box_requests_of(text_of(el), "b2s_s003", "b2s_s003_t1", scale, FONTS, None, None, None, None, None)
    props = jobj(next(slides_json(r) for r in reqs if "createShape" in r), "createShape", "elementProperties")
    return jnum(props, "transform", "translateX") / EMU_PER_PT + pt_of(jat(props, "size", "width")) - emit.PAD_X


def test_a_wrapped_line_has_room_for_its_words_as_slides_sets_them() -> None:
    # themes v4 s3, v2 s5, v3 s8: the box ended 2.9 pt past the widest PDF line (half the room to
    # the next word); Lato set 'events now takes longer' 7.5 pt wider than TeX did, the item
    # wrapped one word early and grew a line, over the paragraph below.
    scale = SLIDE_W / 362.83
    el = column_list_of()
    right = box_right(el, scale)
    paras = jobjs(el, "paragraphs")
    assert [emit.pdf_line_breaks(p, None, None) for p in paras] == [[24, 45], [17, 41]]
    for p in paras:
        text = jstr(p, "runs", 0, "text")
        breaks = emit.pdf_line_breaks(p, None, None)
        assert breaks is not None
        cuts = [0, *breaks, len(text)]
        for k, (a, b) in enumerate(zip(cuts, cuts[1:])):
            line = text[a:b].rstrip()
            x0 = jnum(p, "lines", k, "x0") * scale
            assert x0 + slides_w([text_run(line, BODY)], scale) <= right - emit.WRAP_MARGIN, line
            if b < len(text):  # and the next word still goes onto the next line
                joined = text[a:text.find(" ", b) if " " in text[b:] else len(text)]
                assert x0 + slides_w([text_run(joined, BODY)], scale) > right, joined
    # A paragraph whose words do not come out as its lines (a hyphenated line end) is not measured.
    hyphen = jobj(column_list_of(), "paragraphs", 1)
    jobj(hyphen, "lines", 0)["x1"] = 140.0
    assert emit.pdf_line_breaks(hyphen, None, None) is None


def test_a_full_line_keeps_its_margin_when_a_neighbours_next_word_is_close() -> None:
    # dense v1 s3, themes v4 s4 (r6): another paragraph's next word would join 1 pt past the
    # widest line, so the box ended 1.0 pt past it (WRAP_MARGIN) - and Slides, which sets a line
    # up to 0.6 pt wider than slides_width, wrapped "if" over "any." and a TOC entry's "Design".
    scale = SLIDE_W / 362.83
    el = column_list_of()
    paras = jobjs(el, "paragraphs")
    lines = [measured(p, scale) for p in paras]
    widest, joins = [w for w, _ in lines], [j for _, j in lines]
    line = "Mass bleaching in 2016"  # a one-line paragraph ending 1 pt short of the join
    x0 = (min(joins) - 1.0 - slides_w([text_run(line, BODY)], scale)) / scale
    jarr(el, "paragraphs").append({**paras[0], "bullet": None, "text_x0": x0, "wrap_limit": None,
                                   "runs": [text_run(line, BODY)], "lines": [{"baseline": 170.0, "x0": x0, "x1": x0 + 100.0}]})
    need = max(max(widest), min(joins) - 1.0)
    assert box_right(el, scale) >= need + emit.LINE_MARGIN - 0.01


def test_a_paragraph_ending_at_its_own_edge_leaves_the_others_their_room() -> None:
    # real_beamer-monodromy s14: the first paragraph, beside a picture, would join its next word
    # far left of a longer line below, so that line kept only LINE_MARGIN; the math in it ran
    # 2-3 pt wider in Slides than measured and "homotopy." wrapped. The short paragraph ends at
    # its own edge (indentEnd); the box goes on to the PDF's edge.
    scale = SLIDE_W / 362.83
    line = "Mass bleaching in 2016 and 2017 across the reef"
    w = slides_w([text_run(line, BODY)], scale)

    def with_line_ending_at(right: float) -> tuple[JsonObject, float]:
        """The two items and the line, ending at `right` as Slides sets it; and its PDF edge."""
        el = column_list_of()
        paras = jobjs(el, "paragraphs")
        x0 = (right - w) / scale
        x1 = x0 + w / scale + 8.0  # (TeX set it 8 pt wider than Lato does)
        jarr(el, "paragraphs").append({**paras[0], "bullet": None, "text_x0": x0, "wrap_limit": None,
                                       "runs": [text_run(line, BODY)], "lines": [{"baseline": 170.0, "x0": x0, "x1": x1}]})
        return el, x1 * scale

    (_, first), (_, second) = [measured(p, scale) for p in jobjs(column_list_of(), "paragraphs")]
    assert second < first
    # Past the second item's next word: the box has room to the first's.
    widest = second + 6.0
    el, _ = with_line_ending_at(widest)
    assert widest + 2 * emit.LINE_MARGIN < box_right(el, scale) < first
    # Past both: to the PDF's edge.
    widest = first + 6.0
    el, edge = with_line_ending_at(widest)
    assert box_right(el, scale) >= edge - 0.01 > widest + emit.LINE_MARGIN


def test_an_unmeasured_paragraph_leaves_the_box_as_wide_as_the_measured_lines_need() -> None:
    # figures v1 s2 (r6): one item could not be measured, so the box came from the PDF's
    # extents and ended 0.01 pt short of a one-line item's words as Slides sets them: it wrapped.
    scale = SLIDE_W / 362.83
    el = column_list_of()
    digits = "2016, 2017, 2020, 2022 and 2024"  # Lato's digits are wider than CM's
    wide = text_run(digits, BODY)
    extent = emit.pdf_width([wide])
    assert extent is not None
    first, second = jobjs(el, "paragraphs")
    first["runs"] = [text_run(jstr(first, "runs", 0, "text"), BODY, font="ArialMT")]
    assert emit.slides_lines(first, scale, FONTS) is None
    jarr(el, "paragraphs").append({**second, "bullet": None, "wrap_limit": None, "runs": [wide],
                                   "lines": [{"baseline": 170.0, "x0": 34.97, "x1": 34.97 + extent}]})
    need = 34.97 * scale + slides_w([wide], scale)
    assert need > jnum(first, "lines", 0, "x1") * scale  # wider than any PDF line
    assert box_right(el, scale) >= need + emit.LINE_MARGIN - 0.01


def lm(text: str, font: str, **extra: Json) -> JsonObject:
    """A 9.96 pt Latin Modern run (econ v3 s6)."""
    return text_run(text, 9.96, font=font, **extra)


def test_a_thin_space_and_a_math_symbol_leave_a_paragraph_measurable() -> None:
    # figures v1 s2: "1\,mM" reaches the text as "1 mM", a word space where TeX put a thin one,
    # and the line came out 1.8 pt wider than its extent. econ v3 s6: "≈", "×" and a math-italic
    # "." have no CM advances. Either left the whole paragraph unmeasured, sized from the PDF.
    gfp: JsonObject = {"lines": [{"x0": 255.1, "x1": 436.88, "baseline": 50.02}, {"x0": 255.1, "x1": 308.89, "baseline": 63.57}],
                       "runs": [text_run("GFP (green): lac reporter, induced with 1 mM IPTG", BODY, font="LMSans10-Regular")]}
    assert emit.pdf_line_breaks(gfp, None, None) == [40]
    sans, oblique, symbols = "LMSans10-Regular", "LMSans10-Oblique", "LMMathSymbols10-Regular"
    runs: list[Json] = [lm("ITT; LATE via ", sans), lm("D̄", oblique, italic=True),
                        lm("v", "LMSans8-Oblique", italic=True, script="sub"), lm(" instrument is ", sans),
                        lm("≈", symbols), lm(" 1", sans), lm(".", "LMMathItalic10-Regular"), lm("2", sans),
                        lm("× ", symbols), lm("larger", sans)]
    econ: JsonObject = {"lines": [{"x0": 207.58, "x1": 332.04, "baseline": 180.45}, {"x0": 207.58, "x1": 274.45, "baseline": 192.41}],
                        "runs": runs}
    scale = SLIDE_W / 362.83
    assert emit.pdf_line_breaks(econ, None, None) is None  # CM advances alone cannot say
    assert emit.pdf_line_breaks(econ, scale, FONTS) == [29]  # "... instrument " | "is ≈ 1.2× larger"
    assert emit.slides_lines(econ, scale, FONTS) is not None
    # (a combining macron has no advance of its own in Slides either)
    assert emit.slides_width([lm("D̄", oblique, italic=True)], scale, FONTS) == \
        emit.slides_width([lm("D", oblique, italic=True)], scale, FONTS)


def palatino_quote(**extra: Json) -> JsonObject:
    """r1_lang_v1 s3: a Palatino quotation of two lines (no CM metrics: TeX's widths unknown)."""
    text = "„Erhaben ist, was auch nur denken zu können ein Vermögen des Gemüts beweiset, das jeden Maßstab der Sinne übertrifft.“"
    return {"id": "p2t1", "kind": "text", "role": "body", "code": False, "bbox": [50.17, 70.43, 403.6, 95.06],
            "paragraphs": [{"align": "left", "level": 0, "bullet": None, "size": 10.91, "text_x0": 50.17, "tab_x0": None,
                            "lines": [{"baseline": 78.41, "x0": 50.17, "x1": 403.6}, {"baseline": 91.96, "x0": 50.17, "x1": 227.42}],
                            "wrap_limit": 422.39, **extra,
                            "runs": [text_run(text, BODY, font="PalatinoLinotype-Italic", family="serif", italic=True)]}]}


def test_lines_classify_found_measure_a_paragraph_tex_widths_cannot() -> None:
    # r1_lang_v1 s3 (judge-v1-s3-quote): the box came from the PDF's extents, 589 pt, and PT
    # Serif, wider than Palatino, broke the first line after 'Gemüts'. Where each line starts
    # (classify.line_starts) is enough to set its words as Slides will.
    scale = SLIDE_W / 453.54
    el = palatino_quote()
    assert emit.slides_lines(jobj(el, "paragraphs", 0), scale, FONTS) is None  # (no TeX widths to find the lines with)
    el = palatino_quote(line_starts=[78, 118])
    widest, joins = measured(jobj(el, "paragraphs", 0), scale)
    assert widest + emit.LINE_MARGIN <= box_right(el, scale) < joins
    # ... and never for another text than the one they were counted in (merged words)
    edited = palatino_quote(line_starts=[78, 118])
    jobj(edited, "paragraphs", 0, "runs", 0)["text"] = "„Erhaben ist, was nur denken zu können ein Vermögen des Gemüts beweiset, das jeden Maßstab der Sinne übertrifft.“"
    assert emit.slides_lines(jobj(edited, "paragraphs", 0), scale, FONTS) is None


def test_a_symbol_the_face_lacks_is_as_wide_as_slides_fallback_sets_it() -> None:
    # V-control-16: '⊂' at 0.6 em (unmeasured) where Slides' fallback font sets 0.981 em
    # (probe_symbols): the line came out 8 pt wider than predicted and its last word wrapped.
    scale = SLIDE_W / 362.83
    run = text_run("⊂", BODY, font="CMSY10", family="math")
    _family, size = FONTS(run, scale)
    assert emit.slides_width([run], scale, FONTS) == pytest.approx(emit.SYMBOL_ADVANCE_EM["⊂"] * size)


# ---------------------------------------------------------------- UTF-16 ranges

def test_text_ranges_count_utf16_units() -> None:
    # fonts newtx s4, scripts el s3: 𝛽 (U+1D6FD) and \mathbb's 𝔼 (U+1D53C) are two UTF-16 units;
    # counted as one, every later style range started a unit early and split the next pair.
    runs = [text_run("For ", BODY), text_run("𝔼", BODY, font="MSBM10", family="math"), text_run("[aX + b] = a ", BODY),
            text_run("𝔼", BODY, font="MSBM10", family="math"), text_run("[X] with ", BODY),
            text_run("𝛽", BODY, font="CMMI10", italic=True), text_run("⊤", BODY, font="CMSY7", script="super"),
            text_run(" upright.", BODY)]
    el = text_element_of([runs, [text_run("ends on ", BODY), text_run("𝔼", BODY, font="MSBM10", family="math")]],
                         {"kind": "glyph", "text": "•", "color": "#000000"})
    reqs = text_requests(el, SCALE)
    got = styled(typed_text_requests(el, SCALE))
    want = [jstr(r, "text") for r in runs] + ["ends on ", "𝔼"]
    assert [t for t, _ in got] == want
    assert [s["baselineOffset"] for t, s in got if t == "⊤"] == ["SUPERSCRIPT"]
    text = next(jstr(q, "insertText", "text") for q in reqs if "insertText" in q)
    for r in reqs:  # every range ends inside the text, never inside a surrogate pair
        rng = next((jobj(r, k).get("textRange") for k in ("updateParagraphStyle", "createParagraphBullets") if r.get(k)),
                   None)
        if rng:
            assert jint(rng, "endIndex") <= emit.u16(text) + 1


def test_table_cell_ranges_count_utf16_units() -> None:
    table: JsonObject = {"id": "p0b0", "kind": "table", "frame": [100.0, 50.0, 300.0, 70.0], "size": 10.91,
                         "columns": [{"x0": 106.0, "x1": 290.0, "align": "left"}],
                         "cells": [[[text_run("𝔼", BODY, font="MSBM10", family="math"),
                                     text_run("[X]", BODY, italic=True, font="CMMI10")]]],
                         "row_baselines": [60.0], "row_heights": [15.0], "rules": []}
    ranges = [(jint(r, "updateTextStyle", "textRange", "startIndex"), jint(r, "updateTextStyle", "textRange", "endIndex"))
              for r in table_requests(table, "s", "b2s_s001_b0", SCALE, False) if "updateTextStyle" in r]
    assert ranges == [(0, 2), (2, 5)]


def test_the_emit_models_count_utf16_units() -> None:
    from .slides_sim import joined, units
    assert units("a𝔼b") == ["a", "\ud835", "\udd3c", "b"] and joined(units("a𝔼b")) == "a𝔼b"


# ---------------------------------------------------------------- the .pptx

def test_control_characters_in_an_alt_text_do_not_break_the_pptx(tmp_path: Path) -> None:
    # econ v1: a Type 3 T1 font without ToUnicode gives raw codes (0x10 and 0x11 are quotes, 0x1d
    # a ligature); a figure's alt text holding one made lxml refuse the .pptx: no deck at all.
    from PIL import Image
    from pptx import Presentation
    png = tmp_path / "f.png"
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8)).save(png)
    alt = "\x10A living wage\x11 is a \x1door\x00 \ud835 plan"
    page: dict[str, object] = {"layout": "BLANK", "fill": None, "templates": False, "tables": [],
                               "pictures": [{"file": png, "bbox": [10, 10, 50, 50], "alt": alt, "title": "b2s:\x1a1/f0"}]}
    prs = Presentation(emit.build_pptx(453.54, 340.16, [], [page], {"color": "#ffffff"}, None))
    pic = next(s for s in prs.slides[0].shapes if s.shape_type == 13)
    assert pic._element._nvXxPr.cNvPr.get("descr") == "A living wage is a oor  plan"
    assert pic._element._nvXxPr.cNvPr.get("title") == "b2s:1/f0"
    assert emit.xml_text("tab\tnew\nline\r") == "tab\tnew\nline\r"


# ---------------------------------------------------------------- tables

def signed_table() -> JsonObject:
    """econ v2 s9: a tight right-aligned column of signed estimates under a wider header."""
    rows = [["Variable", "Estimate"], ["Wage", "-0.021"], ["Tenure", "0.034"], ["Union", "-12.345"]]
    cells: list[Json] = [cell_row([[text_run(t, BODY)] for t in row]) for row in rows]
    return {"id": "p0b0", "kind": "table", "frame": [100.0, 50.0, 196.0, 110.0], "size": 10.91,
            "columns": [{"x0": 100.0, "x1": 140.0, "align": "left"}, {"x0": 150.0, "x1": 190.0, "align": "right"}],
            "bounds": [100.0, 145.0, 196.0],
            "cells": cells,
            "row_baselines": [60.0, 75.0, 90.0, 105.0], "row_heights": [15.0] * 4, "rules": []}


def cell_row(cells: Sequence[Sequence[JsonObject]]) -> list[Json]:
    """A table row as JSON: its cells, each a list of runs."""
    row: list[Json] = []
    for runs in cells:
        cell: list[Json] = [r for r in runs]
        row.append(cell)
    return row


def indents(reqs: Sequence[JsonObject], col: int) -> list[tuple[str, float, float]]:
    out: list[tuple[str, float, float]] = []
    for r in reqs:
        p = r.get("updateParagraphStyle")
        if p and jint(p, "cellLocation", "columnIndex") == col:
            out.append((jstr(p, "style", "alignment"), jnum(p, "style", "indentStart", "magnitude"),
                        jnum(p, "style", "indentEnd", "magnitude")))
    return out


def test_a_right_aligned_column_keeps_one_indent() -> None:
    # Capped cell by cell, every cell of a tight column reached its own cap and started at the
    # same x: right alignment became left alignment.
    reqs = table_requests(signed_table(), "s", "b2s_s001_b0", SCALE, False)
    got = indents(reqs, 1)
    assert len(set(got)) == 1 and got[0][0] == "END" and got[0][2] > 0
    widths = column_widths(reqs)
    widest = max(slides_w([text_run(t, BODY)], SCALE) for t in ("Estimate", "-0.021", "0.034", "-12.345"))
    assert widths[1] - 2 * emit.TABLE_CELL_PAD - got[0][2] >= widest  # and the widest still fits


def test_an_unmeasured_font_cell_keeps_room_for_its_words() -> None:
    # scripts ruxe s5, lang v2 s6: Arial and Calibri have no measured advances; the alignment
    # indent was not capped at all, and the widest header wrapped.
    table = signed_table()
    table["cells"] = [cell_row([[{**jobj(r), "font": "ArialMT"} for r in jarr(cell)] for cell in jarr(row)])
                      for row in jarr(table, "cells")]
    reqs = table_requests(table, "s", "b2s_s001_b0", SCALE, False)
    got = indents(reqs, 1)
    widths = column_widths(reqs)
    assert len(set(got)) == 1
    assert widths[1] - 2 * emit.TABLE_CELL_PAD - emit.WRAP_MARGIN - got[0][2] >= (190.0 - 150.0) * SCALE - 0.01


def test_wide_characters_are_an_em() -> None:
    # scripts ja s5: 'マクロ F1' - kana measured at 0.6 em, as an unmeasured Latin letter - wrapped.
    z = FONTS(text_run("マクロ", BODY), SCALE)[1]
    assert emit.slides_width([text_run("マクロ", BODY)], SCALE, FONTS) == pytest.approx(3 * z)


def eleven_columns() -> JsonObject:
    """tables v1 s10 (its first rows): eleven 8 pt columns with 3 pt \\tabcolsep, the table just
    inside a 4:3 page (x 15-348 of 363)."""
    edges = [(18.25, 49.79), (55.79, 81.31), (87.31, 111.57), (117.54, 139.65), (145.62, 167.72),
             (173.7, 195.8), (201.85, 227.41), (233.33, 255.44), (261.5, 283.96), (289.96, 317.0), (323.0, 344.56)]
    cols: list[Json] = [{"x0": a, "x1": b, "align": "center" if i else "left"} for i, (a, b) in enumerate(edges)]

    def run(s: str, font: str) -> JsonObject:
        return text_run(s, 7.97, font=font)

    def cell(t: str) -> list[JsonObject]:
        sans = "LMSans8-Regular"
        return [run("−", "LMMathSymbols8-Regular"), run(t[1:], sans)] if t.startswith("−") else [run(t, sans)] if t else []
    rows = [["Model", "TREC-", "NF-", "Fi", "Argu-", "Sci-", "Touché", "DB-", "SCI-", "Climate", "Quora"],
            ["", "COVID", "Corpus", "QA", "Ana", "Fact", "2020", "Pedia", "DOCS", "FEVER", ""],
            ["DPR", "−67%", "−30%", "−6%", "−58%", "−65%", "−37%", "−24%", "−47%", "+24%", "−0%"]]
    cells: list[Json] = [cell_row([cell(t) for t in row]) for row in rows]
    return {"id": "p9tab0", "kind": "table", "frame": [15.26, 95.31, 347.58, 134.0], "size": 7.97, "columns": cols,
            "bounds": [15.26, 52.79, 84.31, 114.56, 142.63, 170.71, 198.82, 230.37, 258.47, 286.96, 320.0, 347.58],
            "cells": cells, "row_baselines": [105.52, 114.99, 130.09],
            "row_heights": [9.47, 15.1, 9.46], "rules": [],
            "merges": [{"row": 0, "col": 0, "rows": 2, "cols": 1, "align": "left"},
                       {"row": 0, "col": 10, "rows": 2, "cols": 1, "align": "center"}]}


def layout(el: JsonObject, scale: float, imported: bool) -> TableLayout:
    """Where a table element goes on a deck SLIDE_W wide (`table_layout_of` its record)."""
    return table_layout_of(table_of(el), scale, FONTS, imported, SLIDE_W / scale)


def test_a_wide_table_stays_on_the_page() -> None:
    # tables v1 s10, v2 s3: room for the substitute and both cell paddings in every column added
    # up past the page edge, and the last column was lost off the slide.
    page_scale = SLIDE_W / 362.83
    el = eleven_columns()
    assert jnum(el, "frame", 2) < 362.83
    lay = layout(el, page_scale, True)
    assert lay.bounds[-1] <= 362.83 - emit.TABLE_MARGIN * lay.bounds[0] + 0.02
    assert emit.TABLE_MIN_SHRINK <= lay.shrink < 1
    # Every cell still fits its column on one line, at the size it is written in.
    for (r, c), w in lay.cell_width.items():
        assert w is not None, (r, c)
        assert w + 2 * emit.TABLE_CELL_PAD <= lay.widths[c] + 0.01, (r, c)
    assert lay.cells[0][0][0].size == pytest.approx(7.97 * lay.shrink, rel=1e-3)
    assert jnum(el, "cells", 0, 0, 0, "size") == 7.97  # the IR is left alone
    reqs = table_requests(el, "s", "b2s_s001_b0", page_scale, True)
    written = {pt_of(jat(r, "updateTextStyle", "style", "fontSize")) for r in reqs
               if "updateTextStyle" in r and jstr(r, "updateTextStyle", "textRange", "type") == "FIXED_RANGE"}
    full = {FONTS({**jobj(r), "cell": True}, page_scale)[1] for row in jarr(el, "cells") for c in jarr(row) for r in jarr(c)}
    assert max(written) < max(full) and min(written) < min(full)
    assert list(pptx_table_of(table_of(el), page_scale, FONTS, SLIDE_W / page_scale).widths) == list(lay.widths)
    # A table that fits keeps its size and its room.
    ok = signed_table()
    assert layout(ok, SCALE, False).shrink == 1.0
