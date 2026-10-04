"""Wave 5, fixer C (text, formulas, layout): the regressions wave 4 brought and two open ones."""

from pathlib import Path

import pytest

from beamer2slides import emit
from beamer2slides.classify import Span
from beamer2slides.emit import SLIDE_W
from beamer2slides.json_types import JsonObject, as_str
from beamer2slides.google_types import slides_json

from .json_reads import jint, jnum, jobj, jobjs, jstr
from .test_columns import paragraphs, span, text
from .test_emit_requests import FONTS, text_run


# -- a hyphen is where Slides may break ---------------------------------------------------------

def test_wrap_limit_takes_the_next_word_only_to_its_hyphen():
    """r3_scripts_ruxe s1: the next line opens with 'Санкт-Петербургский'. TeX's check (would the
    whole word have fitted?) joins the lines; but Slides breaks after the hyphen, so the box must
    end before the line above plus 'Санкт-', not plus the whole word (it took '2Санкт-' up)."""
    size = 11.0
    word = "Saint-Petersburg"
    spans = [span("The institute of physics and technology of the", 30.0, 100.0, size, w=250.0),
             span(word + " State University", 30.0, 113.5, size, w=180.0)]
    got = paragraphs(spans)
    assert len(got) == 1 and "the Saint-Petersburg" in text(got[0]), [text(p) for p in got]
    whole = 180.0 * len(word) / len(word + " State University")
    limit = got[0].get("wrap_limit")
    assert limit is not None and limit < 30.0 + 250.0 + 0.25 * size + 0.6 * whole, limit


def test_a_word_whose_hyphen_comes_before_a_digit_is_taken_whole():
    from beamer2slides.classify import hyphen_cut
    assert hyphen_cut("Saint-Petersburg") == 6
    assert hyphen_cut("COVID-19") is None and hyphen_cut("-5") is None and hyphen_cut("word-") is None


def test_slides_lines_joins_the_next_line_up_to_its_hyphen():
    """emit's measured box ends between the widest line and where a line would take its next
    word; Slides takes 'Saint-' alone."""
    scale = SLIDE_W / 362.83
    first, second = "The institute of physics and technology of the ", "Saint-Petersburg State University"
    p: JsonObject = {
        "lines": [{"x0": 30.0, "x1": 250.0, "baseline": 100.0}, {"x0": 30.0, "x1": 180.0, "baseline": 113.5}],
        "runs": [text_run(first + second, 10.91)], "line_starts": [len(first), len(first + second)]}
    measured = emit.slides_lines(p, scale, FONTS)
    assert measured is not None
    widest, joins = measured
    upto = emit.slides_width([text_run(first + "Saint-", 10.91)], scale, FONTS)
    assert upto is not None
    assert abs(joins - (30.0 * scale + upto)) < 0.01, (joins, 30.0 * scale + upto)
    assert emit.first_break("a b-c d", 2, 7) == 4 and emit.first_break("a b-5 d", 2, 7) == 5


def test_slides_breaks_before_a_bracket_after_a_greek_letter():
    """tools/probe_script_break.py, live: 'and π[γ]([x])' and 'and π([x])' end a narrow line on π;
    'πy([x])' and 'π2([x])' stay whole (real_beamer-monodromy s17 item 5 set π's subscript [γ]
    on the next line, the box sized as if the word could not break)."""
    from beamer2slides.emit_widths import breaks_before
    from beamer2slides.text_layout import wrap
    text = "and π[γ]([x])"
    assert emit.first_break(text, 4, len(text)) == 5 and emit.first_break("and πy([x])", 4, 11) == 11
    assert emit.first_break("and π2([x])", 4, 11) == 11 and emit.first_break("and x([x])", 4, 10) == 10
    assert not breaks_before("[x]", 0) and breaks_before("π(x)", 1)
    style: JsonObject = {"fontFamily": "Lato", "fontSize": 20.0}
    lines = wrap(text, [style] * len(text), 60.0)
    assert [text[a:b] for a, b, _ in lines][:2] == ["and π", "[γ]([x])"], lines


def test_a_word_joiner_keeps_a_greek_letter_with_its_bracket():
    """tools/probe_script_break.py, live: a WORD JOINER after π keeps 'π[γ]([x])' whole at every
    width and takes no room (U+FEFF does not). real_beamer-monodromy s17 item 5: the PDF's second
    line 'π[γ]([x]) = ...' is wider than the first plus 'π', so no box width kept π down."""
    from beamer2slides.emit_model import run_of
    from beamer2slides.emit_widths import WORD_JOINER, held_index, joined_runs, slides_width_of
    from beamer2slides.merge import collapse_holes
    from beamer2slides.text_layout import wrap
    ir = tuple(run_of(r) for r in (text_run("and π", 11.0), text_run("[γ]", 8.0, script="sub"),
                                   text_run("([x]) and λ(t)", 11.0)))
    held = joined_runs(ir)
    assert [r.text for r in held] == ["and π" + WORD_JOINER, "[γ]", "([x]) and λ" + WORD_JOINER + "(t)"]
    assert joined_runs(held) == held  # (a joiner already there is no break left)
    scale = SLIDE_W / 362.83
    assert slides_width_of(held, scale, FONTS) == slides_width_of(ir, scale, FONTS)
    # classify's offsets into the IR text land on the same characters of the held text
    ir_text, held_text = "".join(r.text for r in ir), "".join(r.text for r in held)
    for at in range(len(ir_text)):
        assert held_text[held_index(ir, held, at)] == ir_text[at], at
    assert emit.first_break(held_text, 4, len(held_text)) == held_text.index(" ", 4)
    style: JsonObject = {"fontFamily": "Lato", "fontSize": 20.0}
    lines = wrap(held_text, [style] * len(held_text), 60.0)
    assert held_text[lines[0][0]:lines[0][1]].strip() == "and", lines
    # what reads the deck back reads no joiner
    assert collapse_holes(held_text) == ir_text


# -- a short inline formula is not broken by Slides ---------------------------------------------

MI, SY, RM = "LMMathItalic10-Regular", "LMMathSymbols10-Regular", "LMRoman10-Regular"


def formula_line(x: float) -> list[Span]:
    """'and the mean wait is W = C − λ.': prose, then math spans 3 pt (0.27 em) apart, at 11 pt on
    the baseline y = 100."""
    y, size = 100.0, 11.0
    out = [span("and the mean wait is", x, y, size, w=100.0)]
    x += 103.0
    for t, font in (("W", MI), ("=", RM), ("C", MI), ("−", SY), ("λ", MI), (".", "LMSans10-Regular")):
        out.append(span(t, x, y, size, w=7.0, font=font))
        x += 10.0 if t not in "λ" else 7.0
    return out


def test_spaces_inside_a_short_formula_are_no_break_spaces():
    """r2_fonts_segoe s3: Slides broke 'W_q = C/(cμ' from '− λ).' at the space before the minus,
    which TeX never does; TeX kept the formula on one line, so Slides must too."""
    got = [text(p) for p in paragraphs(formula_line(30.0))]
    assert got == ["and the mean wait is W = C − λ."], got


def test_a_formula_as_wide_as_half_the_line_keeps_its_spaces():
    """A long formula keeps breakable spaces, or Slides would cut it inside a word."""
    spans = formula_line(x=30.0)[:1]
    x = 133.0
    for t, font in (("W", MI), ("=", RM), ("C", MI), ("−", SY), ("λ", MI)):
        spans.append(span(t, x, 100.0, 11.0, w=30.0, font=font))
        x += 33.0
    got = [text(p) for p in paragraphs(spans)]
    assert len(got) == 1 and " " not in got[0], got


def test_text_italic_letters_inside_a_formula_are_in_its_hole():
    """r2_fonts_helvet s3: helvet's math sets its letters in the text italic, 'b N' one span of
    two letters; the formula hole stopped at the minus before it and ' b N)' became words, a gap
    each side of them."""
    size = 11.0
    y = 100.0
    spans = [span("We fit the capacity as", 30.0, y, size, w=110.0),
             span("Q", 143.0, y, size, w=7.0, font=MI), span("0", 150.0, y + 2.0, 5.0, w=3.0, font=RM),
             span("−", 156.0, y, size, w=8.0, font=SY),
             span(" b N", 164.0, y, size, w=16.0, font="LMSans10-Oblique"),
             span(")", 180.5, y, size, w=4.0, font=RM),
             span(", where the time is", 184.5, y, size, w=90.0)]
    got = paragraphs(spans)
    assert len(got) == 1, [text(p) for p in got]
    holes = [(r.get("hole_x0", 0.0), r.get("hole", 0.0)) for r in got[0]["runs"] if r.get("hole")]
    assert len(holes) == 1 and "b N" not in text(got[0]), text(got[0])
    assert holes[0][0] + holes[0][1] >= 184.0, holes


def test_a_display_crop_leaves_out_the_hanging_ink_of_a_hole_above(tmp_path: Path) -> None:
    """r1_math_v2 s5: 'As ∫2g dμ' over the display 'lim sup ∫|f_n − f| dμ ≤ 0.'; the display's
    picture reached into the line above and showed the tail of the inline integral's hook at
    the PDF place, a piece floating under the hole's own integral once Slides set the words a
    few points off. A glyph another picture owns is left out of a crop when one of them is a
    hole. (Here a 'g' hangs into the box of an 'x' below it.)"""
    import numpy as np
    from PIL import Image
    from beamer2slides import pdf
    from beamer2slides.extract import extract_page
    from beamer2slides.raw_types import RawDoc
    from beamer2slides.render import render_backgrounds
    from .test_hidden_text import one_page
    from .test_pictures_hunt import num_list

    content = (b"BT /F1 12 Tf 10 120 Td (As) Tj ET\nBT /F1 30 Tf 60 120 Td (g) Tj ET\n"
               b"BT /F1 20 Tf 60 95 Td (x) Tj ET\n")
    path = tmp_path / "hook.pdf"
    path.write_bytes(one_page(content))
    doc = pdf.Document(path)  # raw.json as extract writes it (test_pictures_hunt's raw_of, typed)
    try:
        page = extract_page(doc[0], "1")
    finally:
        doc.close()
    page["frame_label"] = None
    raw: RawDoc = {"version": 1, "source": {"pdf": str(path), "producer": "", "pages": 1, "title": ""},
                   "pages": [page]}
    words, g, x = page["spans"]
    assert (g["text"], x["text"]) == ("g", "x")
    text_el: JsonObject = {"id": "t0", "kind": "text", "role": "body", "bbox": [float(v) for v in words["bbox"]],
                           "spans": [words["id"]], "paragraphs": []}
    hole: JsonObject = {"id": "h0", "kind": "image", "role": "math", "anchor": "t0",
                        "bbox": [float(v) for v in g["bbox"]], "spans": [g["id"]]}
    top = g["bbox"][3] - 3.0  # the display's box reaches 3 pt into the g's descender
    display: JsonObject = {"id": "m0", "kind": "image", "role": "math", "bbox": [50.0, top, 120.0, x["bbox"][3] + 1],
                           "spans": [x["id"]]}
    deck: JsonObject = {"slides": [{"page": 0, "size": [400, 200], "elements": [display, hole, text_el],
                                    "on_layout": []}]}
    render_backgrounds(path, raw, deck, tmp_path / "out", frozenset())
    crop = np.array(Image.open(tmp_path / "out" / as_str(display["file"], "file")).convert("L")).astype(int)
    box = num_list(display["bbox"])
    zoom = crop.shape[1] / (box[2] - box[0])
    rows = crop[:max(1, int((x["bbox"][1] - box[1]) * zoom) - 2)]  # above the x
    assert rows.size and (rows > 200).all(), rows.min()


# -- a word space before a hole may break (emit.HOLE_BREAK) -------------------------------------

def holes_box() -> JsonObject:
    from .test_emit_requests import three_holes_json
    el = jobj(three_holes_json(), "elements", -1)
    el.update({"bbox": [10.91, 92.0, 152.25, 106.0], "role": "body"})
    for p in jobjs(el, "paragraphs"):
        p.update({"size": 10.91, "bullet": None, "text_x0": 10.91})
    return el


def written(el: JsonObject) -> tuple[str, list[tuple[str, int, int]]]:
    from .test_emit_requests import SCALE
    reqs = [slides_json(r) for r in emit.text_box_requests(el, "s", "t", SCALE, FONTS)]
    text = "".join(jstr(r, "insertText", "text") for r in reqs if "insertText" in r)
    fonts: list[tuple[str, int, int]] = []
    for r in reqs:
        if "updateTextStyle" not in r:
            continue
        family = jobj(r, "updateTextStyle", "style").get("fontFamily")
        if family and jstr(r, "updateTextStyle", "textRange", "type") == "FIXED_RANGE":
            fonts.append((as_str(family, "fontFamily"), jint(r, "updateTextStyle", "textRange", "startIndex"),
                          jint(r, "updateTextStyle", "textRange", "endIndex")))
    return text, fonts


def test_a_word_space_before_a_hole_is_followed_by_a_line_separator() -> None:
    """r1_math_v2 s6 'but', r3_textfx_v1 s3 'than', real_beamer-monodromy s3 'of': Slides keeps a
    space and the no-break spaces after it together, so the word before a formula went down with
    it. A zero-width space did not break there live (r10); a LINE SEPARATOR does, taking no room
    (tools/probe_hole_break.py). It is set in the word's font, so words typed in front of the
    hole are too, and only after a word space."""
    assert emit.HOLE_BREAK == " "
    text, fonts = written(holes_box())
    assert text.startswith("A  \xa0") and text.count(" ") == 3, repr(text)
    k = text.index(" ")
    assert [f for f, a, b in fonts if a <= k < b][-1] == "Lato", fonts
    glued = holes_box()
    jobj(glued, "paragraphs", 0, "runs", 0)["text"] = "A"  # no word space: no break before its formula
    assert written(glued)[0].count(" ") == 2


def test_the_break_before_a_hole_takes_no_room() -> None:
    """Measured at 0 (probe_hole_break: 130.0 pt with and without it), so box widths stay."""
    from beamer2slides import text_layout
    from beamer2slides.emit_widths import wide_advance
    assert wide_advance(" ", 0.5) == 0.0
    assert text_layout.advance(" ", {"fontFamily": "Lato"}, 14.0) == 0.0


def test_the_word_before_a_hole_stays_on_its_line_in_the_layout_model():
    """text_layout (sync's and the layout oracle's line model) on the text as written: 'A' stays
    on the first line, only the formula goes down."""
    from beamer2slides import text_layout
    from .test_emit_requests import SCALE
    el = holes_box()
    [chars] = emit.slides_texts(el, SCALE, FONTS)
    runs = emit.hole_runs(jobjs(el, "paragraphs", 0, "runs"), SCALE, FONTS)
    styles: list[JsonObject] = [
        {"fontFamily": emit.HOLE_FONT, "fontSize": r["hole_size"]} if r.get("hole") else
        {"fontFamily": FONTS(r, SCALE)[0], "fontSize": FONTS(r, SCALE)[1]}
        for r in runs for _ in as_str(r["text"], "text")]
    a = (text_layout.advance("A", styles[0], jnum(styles[0], "fontSize"))
         + text_layout.advance(" ", styles[1], jnum(styles[1], "fontSize")))
    lines = text_layout.wrap(chars, styles, a + 5.0)  # 'A' and its space fit, its formula does not
    assert chars[lines[0][0]:lines[0][1]].rstrip("  ") == "A", [chars[s:e] for s, e, _ in lines]
    assert chars[lines[1][0]] == "\xa0"  # (the next line opens on the hole)


@pytest.mark.parametrize("brk", [" ", "​"])  # (and the ZWSP r10's decks carry)
def test_pull_and_merge_read_the_break_before_a_hole_as_nothing(brk: str):
    """deck_ir drops it (the IR, compare and pull never see it), merge.collapse_holes too (a deck
    converted before the break and one after say the same), and LaTeX escaping writes nothing."""
    from beamer2slides import compare, inverse, merge
    from .irs import deck_ir
    from beamer2slides.devtools import deck_edits, sync_check
    from .test_adopt import pt
    from .test_adopt_text import box, deck, para, text_of
    lato, mono = {"fontFamily": "Lato", "fontSize": pt(14)}, {"fontFamily": "Roboto Mono", "fontSize": pt(14)}
    d = deck(box("s_c", para("x", runs=[(f"pointwise, but {brk}", lato), ("\xa0" * 6, mono), (" is finite", lato)])))
    runs = text_of(deck_ir(d), "s_c")["paragraphs"][0]["runs"]
    assert brk not in "".join(r["text"] for r in runs)
    assert [r["text"] for r in runs if r.get("hole")] == [" "] and runs[0]["text"] == "pointwise, but "
    assert merge.collapse_holes(f"but {brk}\xa0\xa0\xa0 is\n") == merge.collapse_holes("but \xa0\xa0 is\n") == "but \xa0 is\n"
    assert inverse.latex_escape(f"but {brk}~") == r"but \textasciitilde{}"
    assert brk not in f"but {brk}x".translate(compare.NORMALISE)
    assert sync_check.norm(f"but {brk}\xa0is") == "but is"
    found = deck_edits.HOLE.search(f"but {brk}\xa0\xa0 is")
    assert found is not None and found.start() == 4  # typed in front of the break


def test_one_math_letter_among_words_keeps_the_word_spaces_around_it():
    spans = [span("each of", 30.0, 100.0, 11.0, w=40.0), span("c", 73.0, 100.0, 11.0, w=5.0, font=MI),
             span("physicians treats", 81.0, 100.0, 11.0, w=90.0)]
    assert [text(p) for p in paragraphs(spans)] == ["each of c physicians treats"]
