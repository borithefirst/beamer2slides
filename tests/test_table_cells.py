"""What a table's cells hold, on synthetic pages: a justified p{} cell keeps each of its PDF lines
whole in Slides, a |p|p| table's stretched justified spaces split no column, an inline fraction
in a cell is a fraction (not a rule of the table's), and Latin Modern Sans' square bullet stays
square."""

from beamer2slides import emit as E
from beamer2slides.emit_tables import held_lines_of
from beamer2slides.emit_widths import wrap_joins_of, wrapped_width_of
from beamer2slides.ir import Run, TableElement

from .test_tables_hunt import BLACK, CHAR, SIZE, W, Page, alignments, as_json, cell_texts, lines_at, serif, some, tables

# ---------------------------------------------------------------- a justified cell's lines


def justified_table(col_x1: float) -> TableElement:
    """Term | Meaning, the middle cell of row 1 justified and wrapped after 'near'; the Meaning
    column's words from x = 100 to `col_x1`."""
    cell = "a ridge of rock and coral near the surface"
    return {"id": "t", "kind": "table", "role": "table", "bbox": [50, 60, 300, 110], "spans": [],
            "frame": [50, 60, 300, 110], "size": SIZE,
            "row_baselines": [70, 85, 110], "row_heights": [15, 25, 15], "row_lines": [1, 2, 1],
            "wrapped": [(1, 1, [cell.index("the")])], "justified": [(1, 1)],
            "columns": [{"x0": 52, "x1": 90, "align": "left"}, {"x0": 100, "x1": col_x1, "align": "left"}],
            "bounds": [50, 95, 300],
            "cells": [[[serif("Term")], [serif("Meaning")]],
                      [[serif("Reef")], [serif(cell)]],
                      [[serif("Atoll")], [serif("a ring")]]],
            "merges": [], "rules": [], "borders": [], "fills": []}


def test_a_justified_cell_keeps_its_first_line_whole_where_the_substitute_runs_wider():
    """TeX shrinks a justified line's spaces as readily as it stretches them, so a full first
    line can take less room in the PDF than its words do in Slides. Its room no wider than the
    PDF's column, the cell lost that line's last word to the next ('tipically used / to store',
    real_zds-2022-drivers slide 26). The room now holds each PDF line whole, short of the next
    line's first word."""
    fonts, scale = E.FontMapper(), E.SLIDE_W / W
    probe = E.table_layout(as_json(justified_table(240)), scale, fonts, imported=True, page_w=W)
    starts = [some(justified_table(240).get("wrapped"))[0][2][0]]
    whole = some(wrapped_width_of(probe.cells[1][1], starts, scale, fonts))
    joins = some(wrap_joins_of(probe.cells[1][1], starts, scale, fonts))
    # (the PDF's column 1.5 pt narrower than the first line's words in Slides)
    el = justified_table(100 + whole / scale - 1.5)
    lay = E.table_layout(as_json(el), scale, fonts, imported=True, page_w=W)
    runs = lay.cells[1][1]
    held = some(held_lines_of(runs, starts, (whole / scale - 1.5) * scale, scale, fonts))
    assert whole < held < joins
    _, start, end = alignments(el)[(1, 1)]
    assert alignments(el)[(1, 1)][0] == "JUSTIFIED"
    room = lay.widths[1] - 2 * E.TABLE_CELL_PAD - start - end
    assert whole + 1.0 < room < joins
    assert lines_at(runs, room, scale, fonts) == 2


# ---------------------------------------------------------------- stretched spaces in a |p|p| table

def spread_to(page: Page, text: str, x0: float, x1: float, baseline: float) -> None:
    """A justified line: its words' spaces stretched so that it ends at x1."""
    words = text.split()
    gap = (x1 - x0 - sum(len(w) * CHAR * SIZE for w in words)) / (len(words) - 1)
    for w in words:
        x0 = page.text(w, x0, baseline)["bbox"][2] + gap


ROWS = [("1", ("tie up the same fields here", "of records"), ("join each of fields", "at once")),
        ("2", ("keep it when field is found", "in a set"), ("drop what is left", "over here")),
        ("3", ("common word: none, said by", "many"), ("other word: alike", "in kind"))]


def walled_table(walls: bool) -> list[TableElement]:
    """No | Heuristic | Action between vertical rules, the two p{} columns justified: each cell's
    first line stretched out to its column's edge, its spaces 0.8 to 1.5 em wide."""
    page = Page()
    xs, top = [50.0, 66.0, 224.0, 330.0], 50.0
    page.words("No", 54, top + 12)
    page.words("Heuristic", 70, top + 12)
    page.words("Action", 228, top + 12)
    page.hline(xs[0], xs[-1], top, 0.4)
    page.hline(xs[0], xs[-1], top + 16, 0.4)
    b = top + 30
    for no, (h1, h2), (a1, a2) in ROWS:
        page.words(no, 55, b)
        spread_to(page, h1, 70, 220, b)
        page.words(h2, 70, b + 12)
        spread_to(page, a1, 228, 326, b)
        page.words(a2, 228, b + 12)
        b += 30
    bottom = b - 30 + 18
    page.hline(xs[0], xs[-1], bottom, 0.4)
    if walls:
        for x in xs:
            page.vline(x, top, bottom, 0.4)
    return tables(page.elements())


def test_stretched_justified_spaces_between_walls_split_no_column():
    """A table of justified p{} columns between vertical rules (real_defense-defense slide 35):
    its first lines' spaces were stretched past the chunk gap, and every such line came apart into
    columns of its own - the table grew to the slide's edge, its Action cells centred unlike each
    other. Between two walls and off the table's column starts, a stretched space runs on."""
    (t,) = walled_table(walls=True)
    assert cell_texts(t) == [["No", "Heuristic", "Action"]] + [[no, f"{h1} {h2}", f"{a1} {a2}"]
                                                              for no, (h1, h2), (a1, a2) in ROWS]
    assert len(t["columns"]) == 3
    assert {(r, 2) for r in (1, 2, 3)} <= {(r, c) for r, c in some(t.get("justified"))}


# ---------------------------------------------------------------- an inline fraction in a cell

def fraction_table(page: Page) -> tuple[float, float, float]:
    """Quantity | Value under booktabs rules, row 1's value '-1.765' and an inline \\frac{mV}{ms}:
    a short bar on the row's math axis, the small letters above and below it. Returns the bar's
    x0, x1 and y."""
    page.hline(50, 260, 58, 0.8)
    page.words("Quantity", 55, 70)
    page.words("Value", 150, 70)
    page.hline(50, 260, 74, 0.5)
    b = 90.0
    page.words("Slope", 55, b)
    x = page.text("-1.765", 150, b)["bbox"][2] + 2.0
    small = 7.0
    part = 2 * CHAR * small
    bar = (x, x + part + 1.0, b - 2.5)
    page.span("mV", x + 0.5, bar[2] - 2.0, small, BLACK)
    page.hline(bar[0], bar[1], bar[2], 0.4)
    page.span("ms", x + 0.5, bar[2] + 6.0, small, BLACK)
    page.words("Gain", 55, 110)
    page.words("12", 150, 110)
    page.hline(50, 260, 116, 0.8)
    return bar


def test_an_inline_fraction_in_a_cell_is_scripts_about_a_slash_not_a_rule():
    """\\frac{mV}{\\mu s} in a cell (real_zds-2022-drivers slide 85): its bar was taken for one
    of the table's rules, its numerator and denominator read as scripts with nothing between
    them. A cell now writes it as text lines do: numerator raised, FRACTION SLASH, denominator
    lowered."""
    page = Page()
    _, _, y = fraction_table(page)
    (t,) = tables(page.elements())
    assert cell_texts(t)[0] == ["Quantity", "Value"] and cell_texts(t)[2] == ["Gain", "12"]
    runs: list[Run] = t["cells"][1][1]
    parts = [(r["text"].strip(), r["script"]) for r in runs if r["text"].strip()]
    assert parts[-3:] == [("mV", "super"), ("⁄", None), ("ms", "sub")]
    assert all(abs(some(b.get("y")) - y) > 1.0 for b in t.get("borders", []) if b.get("y") is not None)
    assert all(abs(r["y"] - y) > 1.0 for r in t.get("rules", []))


# ---------------------------------------------------------------- square bullets

def bullet_table(font: str) -> TableElement:
    """Topic | Notes, each Notes cell an item: a '•' in `font` before its words (\\tabitem)."""
    page = Page()
    page.hline(50, 300, 58, 0.8)
    page.words("Topic", 55, 70)
    page.words("Notes", 140, 70)
    page.hline(50, 300, 74, 0.5)
    for b, (topic, note) in zip((88.0, 102.0, 116.0), (("Drivers", "probe the device"), ("Buses", "match by name"),
                                                       ("Pins", "claim before use"))):
        page.words(topic, 55, b)
        page.span("•", 140, b, SIZE, BLACK)["font"] = font
        page.words(note, 148, b)
    page.hline(50, 300, 122, 0.8)
    (t,) = tables(page.elements())
    return t


def test_latin_modern_sans_bullets_in_cells_stay_square():
    """Latin Modern Sans draws its '•' as a square (render.glyph_ink: fill 1.0 in every PDF of
    the corpus and the test decks; CMSY, Fira, Biolinum and the other faces draw a disc), and a
    \\tabitem's came out a round dot in Slides (real_zds-2022-drivers slide 51). A cell writes
    it as '▪'; a CMSY bullet stays '•'."""
    square = bullet_table("LMSans9-Regular")
    disc = bullet_table("CMSY10")
    for r in (1, 2, 3):
        assert cell_texts(square)[r][1].startswith("▪")
        assert cell_texts(disc)[r][1].startswith("•")
