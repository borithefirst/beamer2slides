"""Wave 5, fixer C (text, formulas, layout): the regressions wave 4 brought and two open ones."""

from beamer2slides import emit
from beamer2slides.emit import SLIDE_W

from .test_columns import paragraphs, span, text
from .test_emit_requests import FONTS, run_of


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
    limit = got[0]["wrap_limit"]
    assert limit < 30.0 + 250.0 + 0.25 * size + 0.6 * whole, limit


def test_a_word_whose_hyphen_comes_before_a_digit_is_taken_whole():
    from beamer2slides.classify import hyphen_cut
    assert hyphen_cut("Saint-Petersburg") == 6
    assert hyphen_cut("COVID-19") is None and hyphen_cut("-5") is None and hyphen_cut("word-") is None


def test_slides_lines_joins_the_next_line_up_to_its_hyphen():
    """emit's measured box ends between the widest line and where a line would take its next
    word; Slides takes 'Saint-' alone."""
    scale = SLIDE_W / 362.83
    first, second = "The institute of physics and technology of the ", "Saint-Petersburg State University"
    p = {"lines": [{"x0": 30.0, "x1": 250.0, "baseline": 100.0}, {"x0": 30.0, "x1": 180.0, "baseline": 113.5}],
         "runs": [run_of(first + second)], "line_starts": [len(first), len(first + second)]}
    widest, joins = emit.slides_lines(p, scale, FONTS)
    upto = emit.slides_width([run_of(first + "Saint-")], scale, FONTS)
    assert abs(joins - (30.0 * scale + upto)) < 0.01, (joins, 30.0 * scale + upto)
    assert emit.first_break("a b-c d", 2, 7) == 4 and emit.first_break("a b-5 d", 2, 7) == 5


# -- a short inline formula is not broken by Slides ---------------------------------------------

MI, SY, RM = "LMMathItalic10-Regular", "LMMathSymbols10-Regular", "LMRoman10-Regular"


def formula_line(x: float = 30.0, y: float = 100.0, size: float = 11.0) -> list:
    """'and the mean wait is W = C − λ.': prose, then math spans 3 pt (0.27 em) apart."""
    out = [span("and the mean wait is", x, y, size, w=100.0)]
    x += 103.0
    for t, font in (("W", MI), ("=", RM), ("C", MI), ("−", SY), ("λ", MI), (".", "LMSans10-Regular")):
        out.append(span(t, x, y, size, w=7.0, font=font))
        x += 10.0 if t not in "λ" else 7.0
    return out


def test_spaces_inside_a_short_formula_are_no_break_spaces():
    """r2_fonts_segoe s3: Slides broke 'W_q = C/(cμ' from '− λ).' at the space before the minus,
    which TeX never does; TeX kept the formula on one line, so Slides must too."""
    got = [text(p) for p in paragraphs(formula_line())]
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
    holes = [r for r in got[0]["runs"] if r.get("hole")]
    assert len(holes) == 1 and "b N" not in text(got[0]), text(got[0])
    assert holes[0]["hole_x0"] + holes[0]["hole"] >= 184.0, holes


def test_a_display_crop_leaves_out_the_hanging_ink_of_a_hole_above(tmp_path):
    """r1_math_v2 s5: 'As ∫2g dμ' over the display 'lim sup ∫|f_n − f| dμ ≤ 0.'; the display's
    picture reached into the line above and showed the tail of the inline integral's hook at
    the PDF place, a piece floating under the hole's own integral once Slides set the words a
    few points off. A glyph another picture owns is left out of a crop when one of them is a
    hole. (Here a 'g' hangs into the box of an 'x' below it.)"""
    import numpy as np
    from PIL import Image
    from beamer2slides.render import render_backgrounds
    from .test_hidden_text import one_page
    from .test_pictures_hunt import raw_of

    content = (b"BT /F1 12 Tf 10 120 Td (As) Tj ET\nBT /F1 30 Tf 60 120 Td (g) Tj ET\n"
               b"BT /F1 20 Tf 60 95 Td (x) Tj ET\n")
    path = tmp_path / "hook.pdf"
    path.write_bytes(one_page(content))
    raw = raw_of(path)
    words, g, x = raw["pages"][0]["spans"]
    assert (g["text"], x["text"]) == ("g", "x")
    text_el = {"id": "t0", "kind": "text", "role": "body", "bbox": words["bbox"], "spans": [words["id"]],
               "paragraphs": []}
    hole = {"id": "h0", "kind": "image", "role": "math", "anchor": "t0", "bbox": list(g["bbox"]), "spans": [g["id"]]}
    top = g["bbox"][3] - 3.0  # the display's box reaches 3 pt into the g's descender
    display = {"id": "m0", "kind": "image", "role": "math", "bbox": [50.0, top, 120.0, x["bbox"][3] + 1],
               "spans": [x["id"]]}
    deck = {"slides": [{"page": 0, "size": [400, 200], "elements": [display, hole, text_el], "on_layout": []}]}
    render_backgrounds(path, raw, deck, tmp_path / "out")
    crop = np.array(Image.open(tmp_path / "out" / display["file"]).convert("L")).astype(int)
    zoom = crop.shape[1] / (display["bbox"][2] - display["bbox"][0])
    rows = crop[:max(1, int((x["bbox"][1] - display["bbox"][1]) * zoom) - 2)]  # above the x
    assert rows.size and (rows > 200).all(), rows.min()


# -- a word space before a hole may break (emit.HOLE_BREAK) -------------------------------------

def holes_box() -> dict:
    from .test_emit_requests import three_holes
    el = three_holes()["elements"][-1]
    el.update({"bbox": [10.91, 92.0, 152.25, 106.0], "role": "body"})
    for p in el["paragraphs"]:
        p.update({"size": 10.91, "bullet": None, "text_x0": 10.91})
    return el


def written(el: dict) -> tuple[str, list[tuple[str, int, int]]]:
    from .test_emit_requests import SCALE
    reqs = emit.text_box_requests(el, "s", "t", SCALE, FONTS)
    text = "".join(r["insertText"]["text"] for r in reqs if "insertText" in r)
    fonts = [(r["updateTextStyle"]["style"]["fontFamily"], r["updateTextStyle"]["textRange"]["startIndex"],
              r["updateTextStyle"]["textRange"]["endIndex"]) for r in reqs
             if "updateTextStyle" in r and r["updateTextStyle"]["style"].get("fontFamily")
             and r["updateTextStyle"]["textRange"]["type"] == "FIXED_RANGE"]
    return text, fonts


def test_a_word_space_before_a_hole_is_followed_by_a_zero_width_break(monkeypatch):
    """r1_math_v2 s6 'but', r3_textfx_v1 s3 'than': Slides keeps a space and the no-break spaces
    after it together, so the word before a formula went down with it. A zero-width space after
    the word space would allow a break there (it did not, live: HOLE_BREAK is off); switched on,
    it is set in the word's font, so words typed in front of the hole are too."""
    monkeypatch.setattr(emit, "HOLE_BREAK", emit.ZWSP)
    text, fonts = written(holes_box())
    assert text.startswith("A ​\xa0") and text.count("​") == 3, repr(text)
    k = text.index("​")
    assert [f for f, a, b in fonts if a <= k < b][-1] == "Lato", fonts
    glued = holes_box()
    glued["paragraphs"][0]["runs"][0]["text"] = "A"  # no word space: no break before its formula
    assert written(glued)[0].count("​") == 2


def test_the_break_before_a_hole_is_off():
    """Written live (r10), Slides still took 'but' and 'than' down with their holes."""
    assert emit.HOLE_BREAK == ""
    assert "​" not in written(holes_box())[0]


def test_the_word_before_a_hole_stays_on_its_line_in_the_layout_model():
    """text_layout (sync's and the layout oracle's line model) on the text as written: 'A' stays
    on the first line, only the formula goes down."""
    from beamer2slides import text_layout
    from .test_emit_requests import SCALE
    el = holes_box()
    [chars] = emit.slides_texts(el, SCALE, FONTS)
    runs = emit.hole_runs(el["paragraphs"][0]["runs"], SCALE, FONTS)
    styles = [{"fontFamily": emit.HOLE_FONT, "fontSize": r["hole_size"]} if r.get("hole") else
              {"fontFamily": FONTS(r, SCALE)[0], "fontSize": FONTS(r, SCALE)[1]} for r in runs for _ in r["text"]]
    a = text_layout.advance("A", styles[0], styles[0]["fontSize"]) + text_layout.advance(" ", styles[1], styles[1]["fontSize"])
    lines = text_layout.wrap(chars, styles, a + 5.0)  # 'A' and its space fit, its formula does not
    assert chars[lines[0][0]:lines[0][1]].rstrip("​ ") == "A", [chars[s:e] for s, e, _ in lines]


def test_pull_and_merge_read_the_break_before_a_hole_as_nothing():
    """deck_ir drops it (the IR, compare and pull never see it), merge.collapse_holes too (a deck
    converted before the break and one after say the same), and LaTeX escaping writes nothing."""
    from beamer2slides import inverse, merge
    from beamer2slides.deck_ir import deck_ir
    from beamer2slides.devtools import deck_edits
    from .test_adopt import pt
    from .test_adopt_text import box, deck, para, text_of
    lato, mono = {"fontFamily": "Lato", "fontSize": pt(14)}, {"fontFamily": "Roboto Mono", "fontSize": pt(14)}
    d = deck(box("s_c", para("x", runs=[("pointwise, but ​", lato), ("\xa0" * 6, mono), (" is finite", lato)])))
    runs = text_of(deck_ir(d), "s_c")["paragraphs"][0]["runs"]
    assert "​" not in "".join(r["text"] for r in runs)
    assert [r["text"] for r in runs if r.get("hole")] == [" "] and runs[0]["text"] == "pointwise, but "
    assert merge.collapse_holes("but ​\xa0\xa0\xa0 is\n") == merge.collapse_holes("but \xa0\xa0 is\n") == "but \xa0 is\n"
    assert inverse.latex_escape("but ​~") == r"but \textasciitilde{}"
    assert deck_edits.HOLE.search("but ​\xa0\xa0 is").start() == 4  # typed in front of the break


def test_one_math_letter_among_words_keeps_the_word_spaces_around_it():
    spans = [span("each of", 30.0, 100.0, 11.0, w=40.0), span("c", 73.0, 100.0, 11.0, w=5.0, font=MI),
             span("physicians treats", 81.0, 100.0, 11.0, w=90.0)]
    assert [text(p) for p in paragraphs(spans)] == ["each of c physicians treats"]
