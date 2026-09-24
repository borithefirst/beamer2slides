"""Regression tests for extraction and classification (local only, no Google API).

The PDFs come from `python tests/decks/build.py` and `python tests/themes/sweep.py --build`;
tests whose PDF is missing are skipped.
"""

from pathlib import Path

import pytest

from beamer2slides.classify import Rect, classify
from beamer2slides.extract import extract, select_overlays
from beamer2slides.notes import prepare

DECKS = Path(__file__).resolve().parent / "decks" / "out"
THEMES = Path(__file__).resolve().parent / "themes" / "out"


def load(pdf: Path, overlays: str = "last") -> dict:
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not built")
    return classify(select_overlays(extract(pdf), overlays))


def deck(name: str) -> dict:
    return load(DECKS / f"{name}-handout.pdf")


def texts(slide: dict) -> list[dict]:
    return [e for e in slide["elements"] if e["kind"] == "text"]


def paragraph_text(p: dict) -> str:
    return "".join(r["text"] for r in p["runs"])


def ball_labels(slide: dict) -> list[str]:
    """List labels drawn on balls or boxes, in reading order (classify.literal_list_numbers)."""
    return [e["number"]["text"] for e in sorted((e for e in slide["elements"] if e.get("number")), key=lambda e: e["bbox"][1])]


def kinds(slide: dict) -> list[str]:
    return [e["kind"] for e in slide["elements"]]


def test_basic_deck_is_fully_native():
    d = deck("01_basic")
    assert d["stats"]["native_share"] == 1.0
    assert [e["role"] for e in texts(d["slides"][1]) if e["role"] == "title"] == ["title"]


def test_nested_list_levels_and_wrapping():
    slide = deck("01_basic")["slides"][1]
    lists = [e for e in texts(slide) if any(p["bullet"] for p in e["paragraphs"])]
    assert len(lists) == 1, "the whole nested list is one text box"
    levels = [p["level"] for p in lists[0]["paragraphs"]]
    assert levels == [0, 0, 1, 1, 2, 0]
    wrapped = lists[0]["paragraphs"][1]
    assert len(wrapped["lines"]) == 2 and paragraph_text(wrapped).endswith("second line of the slide")


def test_inline_styles_and_links():
    slide = deck("01_basic")["slides"][3]
    runs = [r for e in texts(slide) for p in e["paragraphs"] for r in p["runs"]]
    assert any(r["bold"] and r["text"] == "bold" for r in runs)
    assert any(r["italic"] and r["text"] == "emphasis" for r in runs)
    assert any(r["family"] == "mono" for r in runs)
    assert any(r["link"] == "https://example.com" for r in runs)


def test_inline_math_becomes_text_display_math_pictures():
    d = deck("02_math")
    runs = [r for e in texts(d["slides"][0]) for p in e["paragraphs"] for r in p["runs"]]
    assert any(r["text"].strip() == "ℝ" for r in runs)
    assert any(r["script"] == "super" and r["text"].strip() == "2" for r in runs)
    assert any(r["script"] == "sub" for r in runs)
    assert kinds(d["slides"][1]).count("image") >= 2  # display equations as pictures
    numbers = [paragraph_text(p) for e in texts(d["slides"][1]) for p in e["paragraphs"]]
    assert "(1)" in numbers and "(2)" in numbers  # their equation numbers as text
    assert not any(l["reason"] == "math" for s in d["slides"] for l in s["left_in_background"])


def test_complex_inline_formulas_leave_holes_in_text():
    slide = deck("13_inline_math")["slides"][0]
    items = [p for e in texts(slide) for p in e["paragraphs"] if p["bullet"]]
    assert len(items) == 4, "items with formulas stay in the list"
    holes = [r for p in items for r in p["runs"] if r.get("hole")]
    assert len(holes) == 2 and all(r["hole"] > 50 for r in holes)
    assert paragraph_text(items[0]).startswith("The norm ") and paragraph_text(items[0]).endswith("is always nonnegative.")
    pictures = [e for e in slide["elements"] if e["kind"] == "image"]
    assert len(pictures) == 2 and all(e["role"] == "math" for e in pictures)
    assert not slide["left_in_background"], "the radical sign travels with its formula"


def test_simple_inline_fraction_becomes_text():
    slide = deck("02_math")["slides"][0]
    lists = [e for e in texts(slide) if any(p["bullet"] for p in e["paragraphs"])]
    assert len(lists) == 1 and len(lists[0]["paragraphs"]) == 4  # the fraction item stays in the list
    runs = [r for p in lists[0]["paragraphs"] for r in p["runs"]]
    i = next(i for i, r in enumerate(runs) if r["text"] == "⁄")
    assert (runs[i - 1]["text"], runs[i - 1]["script"]) == ("a", "super")
    assert (runs[i + 1]["text"], runs[i + 1]["script"]) == ("b", "sub")
    assert lists[0]["strokes"], "the fraction bar leaves the background"


def test_figures_are_pictures_with_their_labels():
    d = deck("03_figures")
    plot = [e for e in d["slides"][2]["elements"] if e["kind"] == "image"]
    assert len(plot) == 1 and len(plot[0]["spans"]) >= 6  # tick labels travel with the plot
    assert kinds(d["slides"][3]).count("image") == 2  # side-by-side images stay separate


def test_simple_tikz_diagram_becomes_native():
    slide = deck("03_figures")["slides"][1]
    diagrams = [e for e in slide["elements"] if e["kind"] == "diagram"]
    assert len(diagrams) == 1
    d = diagrams[0]
    assert [n["shape"] for n in d["nodes"]] == ["ROUND_RECTANGLE"] * 4
    labels = sorted("".join(r["text"] for r in n["paragraphs"][0]) for n in d["nodes"])
    assert labels == ["Classify", "Emit", "Extract", "Render"]
    assert len(d["lines"]) == 3 and all(l["arrow_to"] and not l["arrow_from"] for l in d["lines"])
    # the pgfplots plot on the next slide has curves and axis labels: still a picture
    assert kinds(deck("03_figures")["slides"][2]).count("image") == 1


def test_underline_and_colorbox_become_text_styles():
    slide = deck("11_research_talk")["slides"][6]
    runs = [r for e in texts(slide) for p in e["paragraphs"] for r in p["runs"]]
    assert [r["text"] for r in runs if r["underline"]] == ["underlined"]
    # CMYK yellow as PDFium converts it; \colorbox's padding a highlighted no-break space (not at the item's start)
    assert [(r["text"], r["highlight"]) for r in runs if r["highlight"]] == [("Highlighted\xa0", "#fff101")]
    assert sum(len(e["strokes"]) for e in texts(slide)) == 2  # both drawings leave the background


def test_diagram_with_circles_edge_labels_and_stealth_tips():
    slide = deck("11_research_talk")["slides"][4]
    d = [e for e in slide["elements"] if e["kind"] == "diagram"][0]
    assert [n["shape"] for n in d["nodes"]] == ["ELLIPSE", "ELLIPSE", "RECTANGLE", None, None]
    free = sorted("".join(r["text"] for r in n["paragraphs"][0]) for n in d["nodes"] if n["shape"] is None)
    assert free == ["build", "parse"]
    assert [l["arrow_to"] for l in d["lines"]] == ["STEALTH_ARROW"] * 2
    # the lines reach the circle/rectangle outlines, where the tips' mitred points end: the outer
    # edge of the 0.4 pt outline, half its width before the node's path
    ends = [d["nodes"][1]["bbox"][0], d["nodes"][2]["bbox"][0]]
    assert all(abs(l["to"][0] - (x - 0.2)) < 0.1 for l, x in zip(d["lines"], ends))


def test_flowchart_with_diamond_and_orthogonal_edge():
    slide = deck("11_research_talk")["slides"][5]
    d = [e for e in slide["elements"] if e["kind"] == "diagram"][0]
    assert [n["shape"] for n in d["nodes"] if n["shape"]] == ["ROUND_RECTANGLE", "DIAMOND", "RECTANGLE", "RECTANGLE"]
    assert len(d["lines"]) == 4  # three edges plus the |- connector
    assert [l.get("bend") for l in d["lines"]] == [None, None, None, "vh"], "one elbow line, vertical first"
    assert sum(1 for l in d["lines"] if l["arrow_to"]) == 4


def test_algorithm_line_numbers_use_tabs():
    slide = deck("11_research_talk")["slides"][3]
    boxes = [e for e in texts(slide) if e["role"] != "title"]
    assert len(boxes) == 1, "numbers and indented statements stay in one box"
    paras = boxes[0]["paragraphs"]
    assert [paragraph_text(p).split("\t")[0] for p in paras[1:]] == ["1:", "2:", "3:", "4:", "5:", "6:"]
    tabs = [p["tab_x0"] for p in paras[1:]]
    assert tabs[1] == tabs[2] == tabs[4] and tabs[3] > tabs[1] > tabs[0]  # nesting depth survives


def test_description_items_use_tabs():
    slide = deck("01_basic")["slides"][2]
    paras = [p for e in texts(slide) for p in e["paragraphs"] if p["tab_x0"]]
    assert [paragraph_text(p) for p in paras] == ["Term\tIts definition", "Longer term\tAnother definition"]
    assert abs(paras[0]["tab_x0"] - paras[1]["tab_x0"]) < 0.1 and paras[0]["text_x0"] > paras[1]["text_x0"]


def test_custom_item_labels_stay_literal():
    slide = deck("12_metropolis_talk")["slides"][4]
    paras = [paragraph_text(p) for e in texts(slide) for p in e["paragraphs"] if p.get("tab_x0")]
    assert paras == ["–\tDash item", "+\tPlus item", "✓\tCheck item"]


def test_lettered_labels_and_title_breaks():
    slide = deck("14_misc")["slides"][0]
    paras = [paragraph_text(p) for e in texts(slide) for p in e["paragraphs"]]
    assert paras[0] == "A rather long frame title that does not fit on a single line" + chr(11) + "of the slide"
    assert "First lettered item" in paras and "Second lettered item" in paras
    # The balls under the labels are pictures, each label centred on its ball.
    assert ball_labels(slide) == ["a)", "b)", "1", "2"]  # then an inner numbered list


def test_words_hyphenated_at_line_ends_are_whole_again():
    """A justified \\parbox narrower than the frame: the hyphens TeX added at line ends go (the
    text page used to hand them over as U+0002, which the join never recognised), and the short
    last line stays in its paragraph - the column's edge is where its lines end, not the frame's."""
    slide = next(s for s in deck("14_misc")["slides"]
                 if any(paragraph_text(p) == "Hyphenation" for e in texts(s) for p in e["paragraphs"]))
    paras = [paragraph_text(p) for e in texts(slide) for p in e["paragraphs"] if "Justified" in paragraph_text(p)]
    assert paras == ["Justified paragraphs hyphenate: extraordinarily incomprehensible characteristics of "
                     "typesetting in narrow columns, demonstrably."]


def test_icon_font_glyphs_are_pictures():
    slide = deck("12_metropolis_talk")["slides"][4]
    assert kinds(slide).count("image") == 1  # \ccbysa
    text = "".join(paragraph_text(p) for e in texts(slide) for p in e["paragraphs"])
    assert "cba" not in text


def test_metropolis_progress_bar_is_a_shape():
    slide = deck("12_metropolis_talk")["slides"][5]
    assert kinds(slide).count("image") == 0
    bars = [e for e in slide["elements"] if e["kind"] == "shape"]
    assert [b["fill"] for b in bars] == ["#d6c6b7", "#eb811b"], "the track below the bar"


def test_madrid_blocks_tables_and_footer():
    d = deck("04_theme_blocks")
    assert kinds(d["slides"][1]).count("shape") >= 6  # block title bars and bodies
    balls = [p["bullet"]["kind"] for e in texts(d["slides"][1]) for p in e["paragraphs"] if p["bullet"]]
    assert balls == ["image", "image"], "a ball touching the block shadow is still a bullet"
    table = [e for e in d["slides"][3]["elements"] if e["kind"] == "table"]
    assert len(table) == 1
    cells = [[paragraph_text({"runs": c}) for c in row] for row in table[0]["cells"]]
    assert cells == [["Method", "Precision", "Recall"], ["Baseline", "0.71", "0.64"], ["Ours", "0.89", "0.83"]]
    assert [c["align"] for c in table[0]["columns"]] == ["left", "right", "right"]
    assert {t["key"][0] for t in d["layout_texts"]} == {"B. Tester (TU)", "Themed", "2026"}


def test_grid_table_with_merged_cells():
    slide = deck("11_research_talk")["slides"][2]
    assert kinds(slide).count("image") == 0
    table = [e for e in slide["elements"] if e["kind"] == "table"][0]
    cells = [[paragraph_text({"runs": c}) for c in row] for row in table["cells"]]
    assert cells == [["Model", "Score", ""], ["", "Train", "Test"], ["Linear", "0.80", "0.78"], ["Tree", "0.95", "0.81"]]
    assert sorted((m["row"], m["col"], m["rows"], m["cols"]) for m in table["merges"]) == [(0, 0, 2, 1), (0, 1, 1, 2)]
    left = {(b["row"], b["col"]) for b in table["borders"] if b["position"] == "LEFT"}
    assert left == {(r, c) for r in range(4) for c in range(3)} - {(0, 2)}  # no rule inside "Score"
    assert {(b["row"], b["col"]) for b in table["borders"] if b["position"] == "TOP"} == {(1, 1), (1, 2)}  # \cline{2-3}


def test_shaded_table_cells_become_fills():
    slide = deck("16_colored_table")["slides"][0]
    assert kinds(slide).count("diagram") == 0 and kinds(slide).count("table") == 1
    table = [e for e in slide["elements"] if e["kind"] == "table"][0]
    fills = {(f["row"], f["col"]): f["color"] for f in table["fills"]}
    assert fills[(0, 0)] == fills[(0, 2)] == "#ccccff"  # \rowcolor header
    assert fills[(2, 1)] == "#bfffbf" and fills[(2, 0)] == "#ececec"  # \cellcolor inside a zebra row
    assert (1, 0) not in fills  # white rows stay unfilled


def test_simple_math_in_table_cells():
    slide = deck("16_colored_table")["slides"][1]
    table = [e for e in slide["elements"] if e["kind"] == "table"]
    assert len(table) == 1, "inline math in cells keeps the table native"
    cells = [[paragraph_text({"runs": c}) for c in row] for row in table[0]["cells"]]
    assert cells[1:] == [["α", "0.5", "±0.01"], ["λmax", "10−3", "±2 × 10−4"]]
    scripts = [("".join(r["text"] for r in c if r["script"]), {r["script"] for r in c if r["script"]})
               for c in table[0]["cells"][2]]
    assert scripts == [("max", {"sub"}), ("−3", {"super"}), ("−4", {"super"})]


def table_cells(slide: dict) -> tuple[list[list[str]], list[dict]]:
    table = [e for e in slide["elements"] if e["kind"] == "table"]
    assert len(table) == 1
    return [[paragraph_text({"runs": c}) for c in row] for row in table[0]["cells"]], table[0]["merges"]


def test_columns_closer_than_a_word_space_are_still_columns():
    # 4 pt between columns: a row reads as one phrase, which spanned the table as one merged
    # cell - and wrapped in Slides, being wider than any column.
    cells, merges = table_cells(deck("16_colored_table")["slides"][2])
    assert cells == [["Monitor", "Workstation", "Mainframe"], ["Mouse", "Webcam", "Microwave"]] and merges == []


def test_a_header_centred_over_two_columns_stays_one_cell():
    # Its word space sits on the column gap, which is what cutting a tight row asks for.
    cells, merges = table_cells(deck("16_colored_table")["slides"][3])
    assert cells[0] == ["", "Test results", ""]
    assert merges == [{"row": 0, "col": 1, "rows": 1, "cols": 2, "align": "center"}]


def body_texts(slide: dict) -> list[str]:
    return [paragraph_text(p) for e in texts(slide) if e.get("role") not in ("title", "footer")
            for p in e["paragraphs"]]


def test_a_long_tick_row_belongs_to_its_chart():
    # "200  400  600  800  1,000" is longer than a short label; as text it drew the chart after it.
    slide = deck("03_figures")["slides"][4]
    assert body_texts(slide) == []
    figures = [e for e in slide["elements"] if e["kind"] == "image"]
    assert len(figures) == 1 and not figures[0].get("overlay")


def test_titles_pushed_off_a_plot_are_the_plots_but_its_caption_is_text():
    raw = select_overlays(extract(DECKS / "03_figures-handout.pdf"), "last")
    slide = classify(raw)["slides"][5]
    assert body_texts(slide) == ["Figure: Measured in the cold room."]
    figure = next(e for e in slide["elements"] if e["kind"] == "image")
    by_id = {s["id"]: s["text"] for s in raw["pages"][5]["spans"]}
    words = " ".join(by_id[i] for i in figure["spans"])  # in its picture, not left in the background
    assert all(w in words for w in ("Phase", "Temperature", "Pressure"))


def test_an_underscore_the_page_draws_as_a_rule_is_a_character():
    # OT1 draws \_ as a 0.3 em rule: "x86\_64" came out as a hole, the typewriter line as pictures.
    slide = deck("27_text_fit")["slides"][17]
    assert [e for e in slide["elements"] if e["kind"] == "image"] == []
    words = " ".join(body_texts(slide))
    assert "x86_64" in words and "0x7fff_ffff" in words
    body = next(e for e in texts(slide) if e.get("role") == "body")
    assert len(body["strokes"]) == 2  # the rules leave the background with the text


def test_a_quad_in_typewriter_text_is_plain_spaces():
    # Roboto Mono draws every space at one advance, an em space too: a \quad written as em spaces
    # came out half as wide (text_fit: the typewriter line 0.912 of the PDF's width).
    slide = deck("27_text_fit")["slides"][17]
    assert "0x7fff_ffff   1e-9   [0, 1)   a->b" in body_texts(slide)


def test_an_inline_sum_between_words_is_part_of_its_formula_hole():
    # A CMEX glyph hangs from its origin, 8 pt above the words' baseline: it was a line of its
    # own, left in the background while the words around it moved.
    slide = deck("27_text_fit")["slides"][16]
    assert slide["left_in_background"] == []
    holes_in = [e for e in slide["elements"] if e["kind"] == "image" and e.get("role") == "math"]
    assert len(holes_in) == 2 and all(e.get("anchor") for e in holes_in)


def math_images(slide: dict) -> list[dict]:
    return [e for e in slide["elements"] if e["kind"] == "image" and e.get("role") == "math"]


def body_text(slide: dict) -> str:
    return " | ".join(paragraph_text(p) for e in texts(slide) if e["role"] != "title" for p in e["paragraphs"])


@pytest.fixture(scope="module")
def display_math():
    return deck("28_display_math")["slides"]


def test_an_inline_sum_at_the_end_of_a_line_is_one_hole_with_its_limits(display_math):
    # The \sum's box widened the gap past what build_lines joins, so its words were two lines
    # and the sign a third: the formula became a loose picture beside the words, no hole.
    slide = display_math[0]
    (hole,) = math_images(slide)
    assert hole.get("anchor") and "v ∈ V" in hole["alt"] and "2 | E |" in hole["alt"]
    assert slide["left_in_background"] == []


def test_an_inline_fraction_is_one_hole_with_both_parts(display_math):
    # The bar is exactly as wide as the numerator: it was no fraction bar, the numerator went
    # into the hole and the denominator stayed in the background.
    slide = display_math[1]
    (hole,) = math_images(slide)
    assert hole.get("anchor") and hole["alt"].split() == ["1+2+3+4+5+6", "6"]
    assert slide["left_in_background"] == []


def test_a_display_with_big_delimiters_is_one_picture(display_math):
    slide = display_math[2]
    (pic,) = math_images(slide)
    assert pic["alt"].endswith("1 / p .") and body_text(slide) == "The norm is | for every measurable function."
    assert slide["left_in_background"] == []


def test_signs_hanging_between_lines_of_prose_go_to_the_line_below(display_math):
    # A \displaystyle\int alone between two lines stayed in the background; a radical's box in
    # the line above took two of that line's words into its hole picture.
    slide = display_math[3]
    holes = math_images(slide)
    assert len(holes) == 3 and all(e.get("anchor") for e in holes)
    assert [e["alt"] for e in holes] == ["X ρ d µ", "2 √ d − 1", "√ n"]
    assert "random regular graph" in body_text(slide) and "total mass" in body_text(slide)
    assert slide["left_in_background"] == []


def test_the_sentence_before_a_display_stays_text(display_math):
    # The \left( ended under the sentence's last word, continued it right-aligned, and the
    # paragraph, math now, became a picture of the words.
    slide = display_math[4]
    (pic,) = math_images(slide)
    assert pic["alt"].startswith("SSIM( x , y ) = min 1") and body_text(slide) == "The structural similarity is"
    assert slide["left_in_background"] == []


def test_aligned_rows_and_cases_are_whole_pictures(display_math):
    # The cases' two rows started at the brace, one above the other like a paragraph: they
    # stayed text, "f(x) =" and the brace in the background.
    slide = display_math[5]
    pics = math_images(slide)
    assert len(pics) == 2 and not body_text(slide)
    assert pics[1]["alt"].startswith("f ( x ) =") and "otherwise." in pics[1]["alt"]
    assert slide["left_in_background"] == []


def test_a_formula_picture_holds_the_ink_hanging_below_its_glyph_boxes():
    # A CMEX glyph's box is an em from its origin; a display integral's ink hangs another em
    # below it: the picture cut the integral off at its box (render.grow_to_ink).
    import numpy as np

    from beamer2slides import checks
    pdf = DECKS / "28_display_math-handout.pdf"
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not built")
    r = checks.convert_locally(pdf)
    for index in (5, 7):
        slide = r.deck["slides"][index]
        img, k = r.originals[slide["page"]], r.px_per_pt(slide)
        for pic in math_images(slide):
            x0, y0, x1, y1 = pic["bbox"]
            others = [e["bbox"] for e in slide["elements"] if e is not pic]
            X0, Y0 = int((x0 - 15) * k), int((y0 - 15) * k)
            ys, xs = np.nonzero(img[Y0:int((y1 + 15) * k), X0:int((x1 + 15) * k)].min(axis=2) < 128)
            outside = [(x, y) for x, y in zip((xs + X0 + 0.5) / k, (ys + Y0 + 0.5) / k)
                       if not (x0 <= x <= x1 and y0 <= y <= y1)
                       and not any(b[0] - 1 <= x <= b[2] + 1 and b[1] - 1 <= y <= b[3] + 1 for b in others)]
            assert outside == [], (index, pic["alt"], len(outside))


def cell_texts(table: dict) -> list[list[str]]:
    return [[paragraph_text({"runs": c}) for c in row] for row in table["cells"]]


def test_a_booktabs_table_wider_than_half_the_page_is_a_table():
    # Its rules looked like theme hairlines (is_decoration), so they stayed in the background
    # and the cells became text boxes: "Monitor Workstation Mainframe" in one box, the "val"
    # of one row inside the first column's box.
    d = deck("27_text_fit")
    for index, first, last in ((9, ["Dataset", "Split", "Images", "Classes"], ["Monitor", "Workstation", "12,345,678", "99"]),
                               (10, ["Monitor", "Workstation", "Mainframe", "Total"], ["iii", "lll", "9,999", "0,000"]),
                               (12, ["Quarter", "Revenue", "Cost", "Margin"], ["Q2 2026", "13,579,246.80", "10,864,197.53", "19.99%"])):
        slide = d["slides"][index]
        tables = [e for e in slide["elements"] if e["kind"] == "table"]
        assert len(tables) == 1 and len(tables[0]["rules"]) == 3, index
        assert cell_texts(tables[0])[0] == first and cell_texts(tables[0])[-1] == last
        assert body_texts(slide)[:1] and all(t.startswith(("Table", "Text right")) for t in body_texts(slide)), body_texts(slide)
        assert slide["left_in_background"] == []


def test_a_wrapped_paragraph_cell_keeps_to_its_column():
    # A p{3.2cm} cell wraps, justified: its word spaces are wider than the half em that parts
    # two cells, so the table was refused (overlapping cells) and, as text boxes, the cell's
    # first line joined the numbers beside it and reflowed across their column in Slides.
    # Its lines are one cell, one paragraph (a hyphenated word whole again) that Slides wraps in
    # its column - also when a person types into it - and not a row per line.
    slide = deck("27_text_fit")["slides"][11]
    tables = [e for e in slide["elements"] if e["kind"] == "table"]
    assert len(tables) == 1
    assert cell_texts(tables[0]) == [
        ["", "", "Test results", ""],
        ["Method", "Notes (wrapped on purpose)", "BLEU", "Time"],
        ["Baseline", "A cell set in a paragraph column, which wraps in the PDF too", "27.3", "12h"],
        ["Ours", "Short note", "31.0", "14h"]]
    notes, cell = cell_texts(tables[0])[1][1], cell_texts(tables[0])[2][1]
    assert tables[0]["row_lines"] == [1, 2, 3, 1]
    assert tables[0]["wrapped"] == [[1, 1, [notes.index("purpose")]], [2, 1, [cell.index("graph"), cell.index("wraps")]]]
    # Each row as tall as its lines in the PDF; the last one as one line of the wrapped cells.
    heights, lead = tables[0]["row_heights"], 11.95
    assert heights[2] == pytest.approx(3 * lead, abs=0.3) and heights[3] == pytest.approx(lead, abs=0.1)
    assert tables[0]["merges"] == [{"row": 0, "col": 2, "rows": 1, "cols": 2, "align": "center"}]
    assert [b["col"] for b in tables[0]["borders"]] == [2, 3]  # the \cmidrule under "Test results"
    assert [e["kind"] for e in slide["elements"] if e["kind"] == "image"] == []
    assert body_texts(slide) == ["Table 3: Translation quality on newstest2014, with a caption long enough to fill the line."]


def test_tabular_without_rules_is_a_borderless_table():
    slide = deck("15_plain_tabular")["slides"][0]
    tables = [e for e in slide["elements"] if e["kind"] == "table"]
    assert len(tables) == 1 and not tables[0]["rules"] and not tables[0]["borders"]
    cells = [[paragraph_text({"runs": c}) for c in row] for row in tables[0]["cells"]]
    assert cells[0] == ["Name", "Role", "Hours"] and cells[-1] == ["Carol", "Designer", "40"]
    assert [c["align"] for c in tables[0]["columns"]] == ["left", "center", "right"]
    assert [paragraph_text(p) for e in texts(slide) for p in e["paragraphs"]][-1] == "Some text after the table."


def test_image_bullets_numbered_on_balls():
    slide = deck("04_theme_blocks")["slides"][2]
    # Slides can't draw numbers on balls: each ball is a picture with its number centred on it.
    assert ball_labels(slide) == ["1", "2"]
    assert all(e["anchor"] for e in slide["elements"] if e.get("number"))


def test_nested_and_two_digit_ball_numbers():
    d = deck("19_labels_on_graphics")
    assert ball_labels(d["slides"][0]) == ["1", "2", "1", "2", "3"]
    assert ball_labels(d["slides"][1]) == ["9", "10", "11"]


def test_words_on_small_graphics_become_holes():
    slide = deck("19_labels_on_graphics")["slides"][7]
    body = texts(slide)[1]["paragraphs"]
    # Circled numbers, keycaps and framed words are pictures over gaps in the native line.
    assert [r["text"] for r in body[0]["runs"] if not r.get("hole")] == ["A ", "circled number and a ", "two-digit one."]
    assert sum(bool(r.get("hole")) for p in body[:3] for r in p["runs"]) == 6
    struck = [r for r in body[3]["runs"] if r.get("strike")]
    assert [r["text"].strip() for r in struck] == ["struck out"]  # (\sout{struck out}, : not its comma)
    assert any(r.get("underline") and r["text"] == "underlined" for r in body[3]["runs"])
    badge = texts(deck("19_labels_on_graphics")["slides"][11])[2]["paragraphs"][0]
    assert sum(bool(r.get("hole")) for r in badge["runs"]) == 2


def test_proof_mark_on_a_panel_is_a_picture():
    slide = deck("19_labels_on_graphics")["slides"][9]
    assert [e["id"] for e in slide["elements"] if e["kind"] == "image"] == ["p9k0"]


def test_bar_accent_joins_its_letter():
    slide = deck("13_inline_math")["slides"][1]
    runs = [r["text"] for r in texts(slide)[1]["paragraphs"][0]["runs"]]
    assert "X̄" in runs and not any(r.endswith("Var(¯") for r in runs)


def test_code_block_keeps_indentation():
    slide = deck("05_overlays_notes")["slides"][2]
    code = [e for e in texts(slide) if e["code"]]
    assert len(code) == 1
    lines = [paragraph_text(p) for p in code[0]["paragraphs"]]
    assert lines[1].startswith("    for") and lines[2].startswith("        yield")


def test_overlay_steps_collapse_to_frames():
    d = load(DECKS / "05_overlays_notes.pdf")
    assert len(d["slides"]) == 3
    everything = load(DECKS / "05_overlays_notes.pdf", overlays="all")
    assert len(everything["slides"]) == 6


def test_transparent_covered_text_is_faded():
    d = load(DECKS / "17_transparent_overlays.pdf", overlays="all")
    colors = [{paragraph_text(p): p["runs"][0]["color"] for e in texts(s) for p in e["paragraphs"]} for s in d["slides"]]
    faded, shown = colors[0]["Second point, faded until step two"], colors[1]["Second point, faded until step two"]
    assert shown == "#000000" and faded != "#000000" and int(faded[1:3], 16) > 0xc0


def test_short_paragraphs_are_not_merged():
    slide = load(DECKS / "05_overlays_notes.pdf")["slides"][1]
    paras = [paragraph_text(p) for e in texts(slide) for p in e["paragraphs"]]
    assert "Different text on overlay two." in paras
    assert "Uncovered from overlay two onwards." in paras


@pytest.mark.parametrize("variant,mode", [("notes-pages", "note pages"), ("notes-second", "second screen")])
def test_speaker_notes(tmp_path, variant, mode):
    pdf = DECKS / "notes" / f"{variant}.pdf"
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not built")
    prepared = prepare(pdf, tmp_path)
    assert prepared.mode == mode
    assert prepared.notes == {0: "Speaker note for the pause frame.", 2: "A note on the code frame."}
    assert len(extract(prepared.pdf, prepared.labels)["pages"]) == 3


def test_raster_images_and_full_bleed_background():
    d = deck("07_images")
    assert [kinds(s).count("image") for s in d["slides"]] == [1, 1, 1, 0]


def test_serif_deck():
    d = deck("08_serif")
    assert d["stats"]["native_share"] == 1.0
    families = {r["family"] for s in d["slides"] for e in texts(s) for p in e["paragraphs"] for r in p["runs"]}
    assert "serif" in families


def test_google_font_decks_keep_their_font_and_uncounted_frames():
    from beamer2slides.fonts import google_font

    d = deck("09_metropolis_fira")
    assert len(d["slides"]) == 4, "title page and section page share a frame number but are not overlays"
    assert d["stats"]["native_share"] == 1.0
    fonts = {google_font(r["font"]) for s in d["slides"] for e in texts(s) for p in e["paragraphs"] for r in p["runs"]}
    assert ("Fira Sans", 300, False) in fonts and ("Fira Sans", 700, False) in fonts


def test_metric_compatible_fonts():
    from beamer2slides.fonts import google_font

    assert google_font("NimbusSanL-ReguItal") == ("Arial", 400, True)
    assert google_font("NimbusRomNo9L-Medi") == ("Times New Roman", 700, False)
    assert google_font("texgyreheros-bolditalic") == ("Arial", 700, True)
    assert google_font("SourceSansPro-It") == ("Source Sans Pro", 400, True)
    assert google_font("CMSS10") is None
    d = deck("10_helvet")
    fonts = {google_font(r["font"]) for s in d["slides"] for e in texts(s) for p in e["paragraphs"] for r in p["runs"]}
    assert {("Arial", 400, False), ("Arial", 700, False), ("Arial", 400, True)} <= fonts


def test_toc_split_into_boxes_keeps_its_numbers():
    d = load(THEMES / "Warsaw" / "talk.pdf")
    paras = [p for e in texts(d["slides"][1]) if e["role"] != "title" for p in e["paragraphs"]]
    assert [paragraph_text(p) for p in paras] == ["Introduction", "Method", "Conclusion"]
    assert all(p["bullet"] is None for p in paras), "Slides would number each one-item list 1."
    # Warsaw's numbers sit on balls: each goes on its ball picture, centred there in Slides.
    balls = [e for e in d["slides"][1]["elements"] if e.get("number")]
    assert [b["number"]["text"] for b in balls] == ["1", "2", "3"]
    assert all(b["kind"] == "image" and b["anchor"] for b in balls)


def test_blocks_pair_title_bar_and_body_with_shadow():
    slide = deck("04_theme_blocks")["slides"][1]  # Madrid: standard, alert and example block
    shapes = [e for e in slide["elements"] if e["kind"] == "shape"]
    bodies = [e for e in shapes if e.get("title_bar")]
    bars = [e for e in shapes if e.get("block") is not None and not e.get("title_bar")]
    assert len(bodies) == len(bars) == 3
    assert {b["block"] for b in bodies} == {b["block"] for b in bars} == {0, 1, 2}
    for body in bodies:
        assert body["shadow"]["size"] == 4.0 and len(body["shadow"]["pieces"]) == 2  # right of and below
        assert len(body["strips"]) == 1, "the gradient strip between title bar and body"
    assert not any(b.get("shadow") for b in bars)


@pytest.mark.parametrize("theme,shadow", [("Berlin", False), ("Copenhagen", False), ("Warsaw", True)])
def test_theme_blocks(theme, shadow):
    d = load(THEMES / theme / "talk.pdf")
    slide = next(s for s in d["slides"] if any("Key idea" in paragraph_text(p) for e in texts(s) for p in e["paragraphs"]))
    bodies = [e for e in slide["elements"] if e["kind"] == "shape" and e.get("title_bar")]
    assert len(bodies) == 1
    assert bool(bodies[0].get("shadow")) == shadow


def test_blocks_side_by_side_math_and_lists():
    d = deck("18_blocks_resize")
    side = [e for e in d["slides"][0]["elements"] if e["kind"] == "shape" and e.get("title_bar")]
    assert len(side) == 2 and all(e["shadow"]["size"] == 4.0 for e in side)
    maths = [e for e in d["slides"][1]["elements"] if e["kind"] == "image" and e["role"] == "math"]
    assert len(maths) == 1, "the display equation, its limits and its fraction are one picture"
    assert not [e for e in texts(d["slides"][1]) if paragraph_text(e["paragraphs"][0]).strip() in ("0", "1", "3")]
    assert len([e for e in d["slides"][1]["elements"] if e["kind"] == "shape" and e.get("title_bar")]) == 2
    assert ball_labels(d["slides"][2]) == ["1", "2"], "the last ball grazes the shadow corner"


def test_title_page_box_overlapping_parts_is_one_block():
    slide = deck("04_theme_blocks")["slides"][0]
    bodies = [e for e in slide["elements"] if e["kind"] == "shape" and e.get("title_bar")]
    assert len(bodies) == 1 and bodies[0]["shadow"]["size"] == 4.0


# Minimum native text share per theme for the realistic talk (tests/themes/content.tex).
THEME_FLOORS = {
    "default": 0.95, "Madrid": 0.9, "Warsaw": 0.75, "Berkeley": 0.75, "Bergen": 0.95,
    "Goettingen": 0.75, "Szeged": 0.75, "metropolis": 0.9, "CambridgeUS": 0.85,
}


@pytest.mark.parametrize("theme,floor", sorted(THEME_FLOORS.items()))
def test_theme_sweep_floors(theme, floor):
    d = load(THEMES / theme / "talk.pdf")
    assert d["stats"]["native_share"] >= floor
    assert all("unsure" not in {l["reason"] for l in s["left_in_background"]} for s in d["slides"])
    tables = [e for s in d["slides"] for e in s["elements"] if e["kind"] == "table"]
    assert len(tables) == 1 and len(tables[0]["cells"]) == 4


# marks edge cases

def marked(p: dict, mark: str) -> list[str]:
    return [r["text"].strip() for r in p["runs"] if r.get(mark)]


def holes(p: dict) -> list[float]:
    return [r["hole"] for r in p["runs"] if r.get("hole")]


def test_soul_marks_and_wide_frame():
    slide = deck("19_labels_on_graphics")["slides"][8]
    soul, frames = texts(slide)[1]["paragraphs"][:2]
    # soul draws a box or rule per word piece, overlapping: one highlight, strike and underline each
    assert marked(soul, "highlight") == ["highlighted words"]  # (\hl{highlighted words}, : not its comma)
    assert marked(soul, "strike") == ["soul strike"] and marked(soul, "underline") == ["soul underline"]
    # \framebox[2.5cm] is wider than its words: still one hole with them
    assert len(holes(frames)) == 3 and "wide" not in paragraph_text(frames)
    assert not [e for e in slide["elements"] if e["kind"] == "image" and not e.get("anchor") and e["role"] == "math"]


def test_hole_ends_at_its_graphic():
    body = texts(deck("19_labels_on_graphics")["slides"][7])[1]["paragraphs"]
    # the gap after a circled number counts from the circle, not from the digit inside it
    assert abs(holes(body[0])[0] - 17.6) < 0.2


def test_marks_across_line_breaks():
    paras = texts(deck("20_marks_edge_cases")["slides"][0])[1]["paragraphs"]
    assert marked(paras[0], "highlight") == ["the highlighted part starts near the end of the first line and carries on"]
    assert marked(paras[1], "underline") == ["underline begins close to the right margin and runs over the break"]
    assert marked(paras[2], "highlight") == ["bold", "inside", "it"] and marked(paras[2], "strike") == ["no"]


def test_cancel_strokes_are_holes():
    paras = texts(deck("20_marks_edge_cases")["slides"][1])[1]["paragraphs"]
    assert [len(holes(p)) for p in paras] == [2, 2, 0]
    assert not any(ch in paragraph_text(p) for p in paras for ch in "✭❤❙")  # picture-mode line glyphs
    assert marked(paras[2], "strike") == ["old", "wrong"]


def test_boxes_next_to_punctuation_and_links():
    paras = texts(deck("20_marks_edge_cases")["slides"][2])[1]["paragraphs"]
    assert [len(holes(p)) for p in paras] == [1, 1, 2, 0]
    assert paragraph_text(paras[2]).replace("\xa0", "").startswith("Two words ()")
    link = [r for r in paras[3]["runs"] if r.get("highlight")]
    assert [r["text"] for r in link] == ["\xa0in a box\xa0"] and link[0]["link"] == "https://example.com"


def test_braces_join_their_formula():
    slide = deck("20_marks_edge_cases")["slides"][3]
    paras = [p for e in texts(slide) if e["role"] == "body" for p in e["paragraphs"]]
    assert [len(holes(p)) for p in paras] == [1, 1, 1]
    assert [round(p["lines"][0]["baseline"]) for p in paras[:2]] == [99, 140], "the words' baselines, not the braces'"
    alts = [e["alt"] for e in slide["elements"] if e["kind"] == "image"]
    assert "total" in alts[0] and "both" in alts[1]


def test_framed_paragraphs_are_shapes_with_wrapped_text():
    slide = deck("20_marks_edge_cases")["slides"][4]
    # \fcolorbox: a panel with its frame as the outline, its words a text box on it
    panel = [e for e in slide["elements"] if e["kind"] == "shape"]
    assert [(p["fill"], p["outline"]["color"]) for p in panel] == [("#e6e6ff", "#0000ff")]
    words = [e for e in texts(slide) if Rect.of(panel[0]["bbox"]).contains_rect(Rect.of(e["bbox"]))]
    assert len(words) == 1 and [p["align"] for p in words[0]["paragraphs"]] == ["left", "left"]
    # \fbox: a one-cell table framed by its rules, the paragraph wrapped in its cell
    table = [e for e in slide["elements"] if e["kind"] == "table"]
    assert len(table) == 1 and table[0]["row_lines"] == [2] and len(table[0]["borders"]) == 2


# overlays on text


def test_curve_bounds_skip_control_points():
    from beamer2slides.pdf.api import curve_extremes as _curve_extremes
    # A flat S-curve whose control points reach 40 pt up and down: the curve itself stays within ±12.
    pts = _curve_extremes((0, 0), (10, 40), (20, -40), (30, 0))
    ys = [y for _, y in pts]
    assert (30, 0) in pts and 10 < max(ys) < 12 and -12 < min(ys) < -10


def test_cuts_words():
    from types import SimpleNamespace
    from beamer2slides.classify import PageClassifier, Rect
    word = SimpleNamespace(rect=Rect(100, 10, 140, 20))
    assert PageClassifier.cuts_words(Rect(90, 5, 120, 25), [word]), "an ellipse reaching into the word"
    assert not PageClassifier.cuts_words(Rect(98, 8, 142, 22), [word]), "a box set around the word"


def test_overlays_follow_their_words():
    d = deck("22_overlays_on_text")
    overlays = {s["page"]: [e for e in s["elements"] if e.get("overlay")] for s in d["slides"]}
    # Arrows, braces, the emphasis ellipse and the callout: transparent pictures of their own drawings
    # and labels, anchored to the text they point at, with marks where they meet its words.
    assert [len(overlays[p]) for p in (0, 1, 2, 5)] == [2, 2, 1, 1]
    for p in (0, 1, 2, 5):
        for o in overlays[p]:
            assert o["anchor"] and o["marks"] and o["drawings"], (p, o["id"])
    assert [len(o["spans"]) for o in overlays[1]] == [2, 2], "each brace keeps its label"
    callout = d["slides"][5]
    assert len(callout["elements"][0]["spans"]) == 5 and len(texts(callout)[1]["paragraphs"]) == 3, \
        "the callout text goes with the callout, the list stays native"
    assert not overlays[4], "labels on a photo stay part of the photo"
    assert all(not s["left_in_background"] for s in d["slides"])


def test_translucent_highlight_and_rotated_label():
    d = deck("22_overlays_on_text")
    shapes = [e for e in d["slides"][3]["elements"] if e["kind"] == "shape"]
    assert len(shapes) == 1 and shapes[0]["role"] == "highlight" and 0.3 < shapes[0]["opacity"] < 0.4
    assert shapes[0]["anchor"] == texts(d["slides"][3])[1]["id"]
    assert len(texts(d["slides"][3])[1]["paragraphs"]) == 4, "a highlight behind items doesn't split the list"
    rotated = [e for e in texts(d["slides"][7]) if e.get("rotation")]
    assert len(rotated) == 1 and rotated[0]["rotation"] == -90
    assert paragraph_text(rotated[0]["paragraphs"][0]) == "Accuracy (%)"
    assert any(e["kind"] == "table" for e in d["slides"][7]["elements"])


def test_annotation_arrow_moves_with_its_word():
    from beamer2slides import emit
    d = deck("19_labels_on_graphics")
    arrow = next(e for e in d["slides"][11]["elements"] if e.get("overlay"))
    assert [m["x"] for m in arrow["marks"]] == pytest.approx([32.46, 77.26], abs=0.5), "both ends of important"
    x0, x1 = emit.overlay_boxes(d["slides"][11], emit.SLIDE_W / 453.54, emit.FontMapper())[arrow["id"]]
    assert abs(x0 - arrow["bbox"][0]) < 5 and abs((x1 - x0) - (arrow["bbox"][2] - arrow["bbox"][0])) < 5


# bullet shapes

def bullets(slide: dict) -> list[dict]:
    return [p["bullet"] for e in texts(slide) for p in e["paragraphs"] if p["bullet"]]


def test_outline_squares_keep_shape_and_colour():
    slide = deck("19_labels_on_graphics")["slides"][5]
    assert [(b["kind"], b["shape"], b["color"]) for b in bullets(slide)] == [("shape", "square", "#3333b3")] * 2


def test_itemize_templates_keep_shape_and_colour():
    d = deck("21_bullet_shapes")
    assert {(b["kind"], b["text"], b["color"]) for b in bullets(d["slides"][0])} == {("glyph", "▶", "#3333b3")}
    assert {(b["kind"], b["text"], b["color"]) for b in bullets(d["slides"][1])} == {("glyph", "•", "#ff0000")}
    squares = bullets(d["slides"][2])
    assert len(squares) == 4 and {(b["shape"], b["color"]) for b in squares} == {("square", "#008000")}
    assert [b["kind"] for b in bullets(d["slides"][3])] == ["image"] * 4


def test_custom_item_labels():
    slide = deck("21_bullet_shapes")["slides"][4]
    assert not slide["left_in_background"], "star and diamond items are not figure labels"
    icons = [e for e in slide["elements"] if e["kind"] == "image" and e["role"] == "icon"]
    assert len(icons) == 3 and all(e.get("anchor") for e in icons), "dingbats and the picture move with their item"
    paras = {paragraph_text(p): p for e in texts(slide) for p in e["paragraphs"]}
    assert paras["A pointing hand"]["tab_x0"] is None and paras["A pointing hand"]["text_x0"] > 30
    star = paras["A blue star"]  # a subitem template glyph with a Slides preset is a real bullet
    assert star["bullet"]["text"] == "⋆" and star["bullet"]["color"] == "#0000ff" and star["level"] == 1
    assert paras["⋄\tA diamond"]["tab_x0"]


def test_roman_numbers_on_circles_and_label_tabs():
    d = deck("21_bullet_shapes")
    assert ball_labels(d["slides"][5]) == ["i", "ii", "iii", "iv"]
    paras = [paragraph_text(p) for e in texts(d["slides"][8]) for p in e["paragraphs"]]
    assert "Later\tShown from the second step" in paras, "a description item after an unlabelled one"
    q = next(p for e in texts(deck("19_labels_on_graphics")["slides"][4]) for p in e["paragraphs"]
             if paragraph_text(p).startswith("Q:"))
    assert q["tab_x0"] and paragraph_text(q) == "Q:\tA question label"


def words_line(words: list[tuple[str, float, float]], baseline: float, size: float = 10.909):
    """A classify Line of word spans ((text, x0, x1), CMSS10) on one baseline."""
    from beamer2slides.classify import Line, Rect, Span
    from beamer2slides.fonts import font_info
    return Line([Span(f"s{x0:.0f}-{baseline:.0f}", t, "CMSS10", size, "#000000",
                      Rect(x0, baseline - 0.78 * size, x1, baseline + 0.22 * size), baseline, True, font_info("CMSS10"))
                 for t, x0, x1 in words])


def test_paragraph_under_a_list_is_no_description_item():
    """sync_smoke wording: "Reviewers" ends where "keeps" of the paragraph under the list ends, and
    "polish" starts with "both" (word spaces, not a label gap): no tabs, so the paragraph stays apart."""
    from beamer2slides.classify import PageClassifier
    item = words_line([("Reviewers", 32.73, 76.96), ("polish", 80.61, 107.0), ("the", 110.63, 125.04),
                       ("slides", 128.67, 152.69), ("in", 156.34, 164.56), ("Google", 168.2, 199.57), ("Slides", 203.2, 229.1)], 123.38)
    between = words_line([("Nobody", 32.73, 68.26), ("wants", 71.89, 98.02), ("to", 101.65, 111.04), ("redo", 114.69, 134.33),
                          ("their", 137.97, 158.69), ("edits", 162.33, 183.51), ("by", 187.16, 197.51), ("hand", 201.14, 223.27)], 139.92)
    para = words_line([("A", 10.91, 18.17), ("merge", 21.81, 49.33), ("keeps", 52.97, 77.49), ("both", 81.12, 102.08),
                       ("sides", 105.71, 127.13), ("of", 130.78, 139.56), ("the", 143.19, 157.6), ("work.", 161.24, 185.61)], 167.37)
    lines = PageClassifier.join_line_labels([item, between, para])
    PageClassifier.label_tabs(lines)
    assert [l.tab for l in lines] == [None, None, None]
    # A description list (01_basic): labels ending together, \labelsep before the text.
    term = words_line([("Term", 61.28, 85.02), ("Its", 90.47, 101.61), ("definition", 105.25, 147.41)], 100)
    longer = words_line([("Longer", 29.19, 60.19), ("term", 63.83, 84.99), ("Another", 90.45, 126.93),
                         ("definition", 130.57, 172.73)], 115)
    lines = PageClassifier.join_line_labels([term, longer])
    assert [l.tab.text for l in lines] == ["Its", "Another"]


def test_outline_entries_ending_together_stay_apart():
    slide = deck("21_bullet_shapes")["slides"][10]
    paras = [paragraph_text(p) for e in texts(slide) if e["role"] == "body" for p in e["paragraphs"]]
    assert paras[-3:] == ["Outline", "Balls", "Circles"]


def test_bibliography_icons_are_pictures_not_bullets():
    slide = load(THEMES / "default" / "talk.pdf")["slides"][7]
    assert not bullets(slide)
    icons = [e for e in slide["elements"] if e["kind"] == "image" and e["role"] == "icon"]
    assert len(icons) == 2 and all(e.get("anchor") for e in icons)


def test_bullet_glyph_levels_and_sizes():
    from beamer2slides.emit import BULLET_SHAPES, bullet_level, bullet_preset, bullet_size

    square = {"kind": "shape", "shape": "square", "bbox": [0, 0, 4.85, 4.85]}
    triangle = {"kind": "glyph", "text": "▶", "bbox": [0, 0, 8, 11], "label": {"size": 10.91}}
    assert bullet_preset(square) == "BULLET_DISC_CIRCLE_SQUARE" and [bullet_level(square, k) for k in range(3)] == [2, 5, 8]
    assert bullet_preset(triangle) == "BULLET_ARROW3D_CIRCLE_SQUARE" and bullet_level(triangle, 2) == 0
    assert bullet_level({"kind": "number", "text": "a."}, 1) == 1
    assert bullet_size(square, 17.0, 1.5) == round(4.85 * 1.5 / BULLET_SHAPES["square"][2], 1)
    assert bullet_size({**square, "bbox": [0, 0, 20, 20]}, 17.0, 1.5) == 17.0, "never larger than the text"


def test_bullet_requests_keep_bullet_style():
    from beamer2slides.emit import FontMapper, text_box_requests

    run = {"text": "First part", "font": "CMSS10", "family": "sans", "size": 10.91, "bold": False, "italic": False,
           "smallcaps": False, "color": "#000000", "link": None, "script": None}
    para = {"align": "left", "level": 1, "size": 10.91, "text_x0": 35.15, "tab_x0": None, "wrap_limit": None,
            "bullet": {"kind": "shape", "shape": "square", "color": "#3333b3", "bbox": [25.46, 92.78, 30.3, 97.63]},
            "lines": [{"baseline": 97.63, "x0": 35.15, "x1": 77.65}], "runs": [run]}
    el = {"id": "t", "kind": "text", "role": "body", "paragraphs": [para, {**para, "runs": [{**run, "text": "Second"}]}]}
    reqs = text_box_requests(el, "s", "b", 1.5, FontMapper())
    assert reqs[1]["insertText"]["text"] == "-\n" + "\t" * 5 + "First part\n" + "\t" * 5 + "Second"
    kinds_ = [next(iter(r)) for r in reqs]
    assert kinds_.index("createParagraphBullets") + 1 == kinds_.index("deleteText"), "the dummy goes right after"
    assert reqs[kinds_.index("deleteText")]["deleteText"]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 2}
    before = reqs[2]["updateTextStyle"]["style"]
    assert before["foregroundColor"]["opaqueColor"]["rgbColor"]["blue"] > 0.6
    styled = [r["updateTextStyle"]["textRange"] for r in reqs[kinds_.index("deleteText"):] if "updateTextStyle" in r]
    assert all((t["startIndex"], t["endIndex"]) not in ((0, 10), (11, 17)) for t in styled), "no request covers a whole item"
