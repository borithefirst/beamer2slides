"""Tables in an adopted deck: what deck_ir(foreign=True) reads of one, and what adopt writes for it.

Offline, no TeX and no Google: hand-built `presentations.get` tables shaped like the adopt corpus's -
a merged header, cell fills (one with no `propertyState`, which Slides draws), borders in the API's
`horizontalBorderRows` / `verticalBorderRows` with colour, weight, dash and invisible ones.
"""

import pytest

from beamer2slides import adopt
from beamer2slides.deck_ir import cell_pad, deck_ir, guess_lines

from .test_adopt import EMU, at, pt, presentation

SCALE = 720 / 453.54          # Slides pt per PDF pt on a 16:9 deck


@pytest.fixture(autouse=True)
def lengths_as_written(monkeypatch):
    """These tests read the writers' lengths; their rewriting into bp is test_adopt's
    `test_lengths_are_written_in_pdf_points_and_the_deck_words_are_left_alone`."""
    monkeypatch.setattr(adopt, "to_bp", lambda text: text)

@pytest.fixture(autouse=True)
def no_machine_fonts(monkeypatch, tmp_path):
    monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))


def rgb(hexc: str) -> dict:
    return {"rgbColor": {"red": int(hexc[0:2], 16) / 255, "green": int(hexc[2:4], 16) / 255,
                         "blue": int(hexc[4:6], 16) / 255}}


def cell(r: int, c: int, words: str, *, size: float = 12, bold: bool = False, align: str = "START",
         fill: str | None = None, state: str | None = None, rowspan: int = 1, colspan: int = 1,
         valign: str | None = None) -> dict:
    loc = {k: v for k, v in (("rowIndex", r), ("columnIndex", c)) if v}   # Slides leaves out a 0
    props: dict = {}
    if fill:
        props["tableCellBackgroundFill"] = {"solidFill": {"color": rgb(fill)}}
        if state:
            props["tableCellBackgroundFill"]["propertyState"] = state
    if valign:
        props["contentAlignment"] = valign
    out = {"location": loc, "rowSpan": rowspan, "columnSpan": colspan, "tableCellProperties": props}
    if words:
        out["text"] = {"textElements": [
            {"paragraphMarker": {"style": {"alignment": align}}},
            {"textRun": {"content": words + "\n",
                         "style": {"fontFamily": "Arial", "fontSize": pt(size), "bold": bold,
                                   "foregroundColor": {"opaqueColor": rgb("222222")}}}}]}
    return out


def border(r: int, c: int, colour: str | None, weight: float = 1.0, dash: str = "SOLID",
           alpha: float = 1.0) -> dict:
    props: dict = {"weight": pt(weight), "dashStyle": dash}
    if colour:
        props["tableBorderFill"] = {"solidFill": {"color": rgb(colour), "alpha": alpha}}
    loc = {k: v for k, v in (("rowIndex", r), ("columnIndex", c)) if v}
    return {"location": loc, "tableBorderProperties": props}


def table(oid: str = "g1a2b3c_0_7", widths=(100, 60, 60), heights=(30, 30, 30)) -> dict:
    """Three rows by three columns at (50, 80): a header merged across columns 1-2, a first column
    merged down rows 1-2, a filled cell with no propertyState and one NOT_RENDERED, and borders -
    black under the header, a dashed red column rule, one invisible (alpha 0) and one with no fill."""
    rows = [
        {"rowHeight": pt(heights[0]), "tableCells": [
            cell(0, 0, "Name", bold=True, fill="CCE5FF"),
            cell(0, 1, "Quarter", align="CENTER", fill="FFEEAA", state="NOT_RENDERED", colspan=2)]},
        {"rowHeight": pt(heights[1]), "tableCells": [
            cell(1, 0, "Revenue", rowspan=2, valign="MIDDLE"),
            cell(1, 1, "10", align="END"), cell(1, 2, "12", align="END", valign="BOTTOM")]},
        {"rowHeight": pt(heights[2]), "tableCells": [
            cell(2, 1, "20", align="END"), cell(2, 2, "", fill="00FF00")]},
    ]
    horizontal = [{"tableBorderCells": [border(i, c, "000000" if i == 1 else None) for c in range(3)]}
                  for i in range(4)]
    horizontal[2]["tableBorderCells"][0] = border(2, 0, "000000")        # inside the merged column
    horizontal[3]["tableBorderCells"][1] = border(3, 1, "0000FF", alpha=0.0)
    # one entry per table row, each with a cell per column boundary (the API's grid)
    vertical = [{"tableBorderCells": [border(r, i, "FF0000" if i == 1 else None, 2.0,
                                             "DASH" if i == 1 else "SOLID") for i in range(4)]}
                for r in range(3)]
    vertical[0]["tableBorderCells"][2] = border(0, 2, "FF0000", 2.0, "DASH")   # inside the header
    return {"objectId": oid, "size": {"width": pt(236.22), "height": pt(236.22)},   # 3,000,000 EMU
            "transform": at(50, 80),
            "table": {"rows": 3, "columns": 3,
                      "tableColumns": [{"columnWidth": pt(w)} for w in widths],
                      "tableRows": rows, "horizontalBorderRows": horizontal,
                      "verticalBorderRows": vertical}}


def deck(*tables: dict) -> dict:
    pres = presentation()
    pres["slides"][0]["pageElements"].extend(tables)
    return pres


def the_table(pres: dict, foreign: bool = True) -> dict:
    return next(e for e in deck_ir(pres, foreign=foreign)["slides"][0]["elements"] if e["kind"] == "table")


# ---------------------------------------------------------------- what the IR reads

def test_the_box_is_the_grid_not_the_size_slides_reports():
    """Slides says 3,000,000 EMU square for every table; the grid is 220 x 90 pt."""
    el = the_table(deck(table()))
    x0, y0, x1, y1 = el["bbox"]
    assert (x0, y0) == pytest.approx((50 / SCALE, 80 / SCALE), abs=0.01)
    assert (x1 - x0, y1 - y0) == pytest.approx((220 / SCALE, 90 / SCALE), abs=0.02)
    assert el["col_widths"] == pytest.approx([w / SCALE for w in (100, 60, 60)], abs=0.01)
    assert el["row_heights"] == pytest.approx([30 / SCALE] * 3, abs=0.01)


def test_rows_stay_what_pull_and_sync_compare():
    """`rows` is the plain text grid it always was, and pull's IR gets nothing else."""
    pull = the_table(deck(table()), foreign=False)
    assert pull["rows"] == [["Name", "Quarter"], ["Revenue", "10", "12"], ["20", ""]]
    assert not any(k.startswith(("table_", "col_", "row_", "cell_")) for k in pull)
    assert the_table(deck(table()))["rows"] == pull["rows"]


def test_only_the_head_of_a_merged_range_is_a_cell_and_a_missing_index_is_zero():
    cells = {(c["row"], c["col"]): c for c in the_table(deck(table()))["table_cells"]}
    assert sorted(cells) == [(0, 0), (0, 1), (1, 0), (1, 1), (1, 2), (2, 1), (2, 2)]
    assert (cells[0, 1]["rowspan"], cells[0, 1]["colspan"]) == (1, 2)
    assert (cells[1, 0]["rowspan"], cells[1, 0]["colspan"]) == (2, 1)


def test_a_fill_with_no_property_state_is_drawn_and_a_not_rendered_one_is_not():
    cells = {(c["row"], c["col"]): c for c in the_table(deck(table()))["table_cells"]}
    assert cells[0, 0]["fill"].lower() == "#cce5ff" and cells[0, 0]["fill_alpha"] == 1
    assert cells[0, 1]["fill"] is None
    assert cells[2, 2]["fill"].lower() == "#00ff00" and cells[2, 2]["paragraphs"] == []


def test_cell_text_reads_like_a_text_box():
    cells = {(c["row"], c["col"]): c for c in the_table(deck(table()))["table_cells"]}
    head = cells[0, 0]["paragraphs"][0]
    assert head["runs"][0]["text"] == "Name" and head["runs"][0]["bold"] is True
    assert head["runs"][0]["size"] == pytest.approx(12 / SCALE, abs=0.01)
    assert cells[0, 1]["paragraphs"][0]["align"] == "center"
    assert cells[1, 1]["paragraphs"][0]["align"] == "right"
    assert (cells[1, 0]["valign"], cells[1, 2]["valign"], cells[1, 1]["valign"]) == ("middle", "bottom", "top")
    assert not any("slides_font" in r for c in cells.values() for p in c["paragraphs"] for r in p["runs"])


def test_borders_keep_colour_weight_and_dash_and_invisible_ones_are_dropped():
    borders = the_table(deck(table()))["table_borders"]
    horizontal = [(b["row"], b["col"]) for b in borders if b["dir"] == "h"]
    assert sorted(horizontal) == [(1, 0), (1, 1), (1, 2), (2, 0)], "no fill and alpha 0 are not drawn"
    red = [b for b in borders if b["dir"] == "v"]
    assert {(b["col"], b["row"]) for b in red} == {(1, 0), (1, 1), (1, 2), (2, 0)}
    assert all(b["dash"] == "DASH" and b["color"].lower() == "#ff0000" for b in red)
    assert red[0]["weight"] == pytest.approx(2 / SCALE, abs=0.001)


def test_insets_follow_where_the_table_came_from_and_its_rows():
    # made in Slides, rows with room: Slides' own 7.2 pt
    assert cell_pad(table()) == pytest.approx((7.2, 7.2))
    # brought by a .pptx (its ids): at most the file's 3.6 pt
    assert cell_pad(table("i32")) == pytest.approx((5.8, 3.6))
    # rows that hold 12 pt text in 16 pt: the inset cannot be more than (16 - 14.4) / 2, floored at 1.5
    assert cell_pad(table("i32", heights=(16, 16, 16)))[1] == pytest.approx(1.5)
    # a Slides table whose row is smaller than one line of its text was dragged: the heights say
    # nothing and the inset is Slides' own
    assert cell_pad(table(heights=(9, 30, 30))) == pytest.approx((7.2, 7.2))


def test_a_row_a_little_short_of_its_text_at_the_cap_keeps_the_cap():
    """creandum-board: 22.0 pt rows of 7 pt text leave 6.8 pt a side, and Slides draws them 22.8 tall
    with its own 7.2; less than a point short is a row Slides grew, not a smaller inset."""
    assert cell_pad(table("i32", heights=(20, 20, 20)))[1] == pytest.approx(3.6)   # 2.8 would be the rows' room
    assert cell_pad(table(heights=(28, 28, 28)))[1] == pytest.approx(7.2)          # 6.8
    assert cell_pad(table(heights=(24, 24, 24)))[1] == pytest.approx(4.8)          # well short: the rows say it


# ---------------------------------------------------------------- what the thumbnail says of a table

PX = 2.0          # thumbnail pixels per IR pt


def grid_element(heights=(10, 10, 10), widths=(40, 40), color="#000000") -> dict:
    """A bare table element at (10, 10) with a border under every row and a left-aligned cell each."""
    borders = [{"dir": "h", "row": r, "col": c, "color": color, "alpha": 1.0, "weight": 1.0, "dash": "SOLID"}
               for r in range(len(heights) + 1) for c in range(len(widths))]
    cells = [{"row": r, "col": c, "rowspan": 1, "colspan": 1,
              "paragraphs": [{"align": "left", "runs": [{"text": "Word", "size": 10.0}]}]}
             for r in range(len(heights)) for c in range(len(widths))]
    return {"kind": "table", "bbox": [10, 10, 10 + sum(widths), 10 + sum(heights)], "row_heights": list(heights),
            "col_widths": list(widths), "table_borders": borders, "table_cells": cells, "cell_pad": [5.8, 3.6]}


def blank(h=100, w=120, colour=(255, 255, 255)):
    import numpy as np
    img = np.zeros((int(h * PX), int(w * PX), 3), dtype=np.int16)
    img[:] = colour
    return img


def rule(img, y, x0=10, x1=90, colour=(0, 0, 0)):
    img[int(y * PX):int(y * PX) + 2, int(x0 * PX):int(x1 * PX)] = colour


def test_rows_are_as_tall_as_the_thumbnail_draws_them_and_held_there():
    """Stored rows of 10 pt that Slides grew to 14 (an empty cell's line, a .pptx's insets): the
    borders on the thumbnail say so, and `\\adoptfix` keeps TeX from growing them again."""
    from beamer2slides.deck_ir import thumbnail_rows
    el = grid_element()
    img = blank()
    for y in (10, 24, 38, 52):
        rule(img, y)
    thumbnail_rows([el], img, PX)
    assert el["rows_fixed"] == [0, 1, 2]
    assert el["row_heights"] == pytest.approx([14, 14, 14], abs=0.6)


def test_measuring_stops_at_a_boundary_it_cannot_see():
    from beamer2slides.deck_ir import thumbnail_rows
    el = grid_element()
    img = blank()
    for y in (10, 24):                    # the rule under row 1 is not drawn: rows 1 and 2 could be anything
        rule(img, y)
    thumbnail_rows([el], img, PX)
    assert el["rows_fixed"] == [0]
    assert el["row_heights"][1:] == [10, 10]


def test_a_step_between_two_fills_is_no_border():
    """hebrew-lesson: a brown header over pale rows, white borders nobody sees. The step down to the
    paler fill turns towards white but never comes back: no row is measured."""
    from beamer2slides.deck_ir import thumbnail_rows
    el = grid_element(color="#ffffff")
    img = blank(colour=(250, 240, 235))
    img[int(10 * PX):int(24 * PX), int(10 * PX):int(90 * PX)] = (120, 70, 40)
    thumbnail_rows([el], img, PX)
    assert "rows_fixed" not in el and el["row_heights"] == [10, 10, 10]


def test_the_side_inset_is_where_the_cells_words_begin():
    """comps-analysis' .pptx cells start their words 3 pt in where the guess said 5.8."""
    from beamer2slides.deck_ir import thumbnail_cell_pad, thumbnail_rows
    el = grid_element(heights=(14, 14, 14))
    img = blank()
    for y in (10, 24, 38, 52):
        rule(img, y)
    for r in range(3):
        for c in range(2):
            x = 10 + 40 * c + 3 + 0.6                   # the inset plus the first glyph's bearing
            img[int((14 + 14 * r) * PX):int((20 + 14 * r) * PX), int(x * PX):int((x + 20) * PX)] = (0, 0, 0)
    thumbnail_rows([el], img, PX)
    thumbnail_cell_pad([el], img, PX)
    assert el["cell_pad"][0] == pytest.approx(3.0, abs=0.5) and el["cell_pad"][1] == 3.6


def test_measured_rows_are_written_fixed(tmp_path):
    ir = deck_ir(deck(table()), foreign=True)
    next(e for e in ir["slides"][0]["elements"] if e["kind"] == "table")["rows_fixed"] = [0, 1]
    t = table_source(adopt.bootstrap(ir, tmp_path / "tree" / "main.tex"))
    assert "\\adoptfix{0}" in t and "\\adoptfix{1}" in t and "\\adoptfix{2}" not in t


def test_guess_lines_tells_one_line_from_several():
    assert guess_lines("+1 months\n", 200, 7) == 1
    assert guess_lines("Develop\x0bPMF & integration\n", 200, 12) == 2
    assert guess_lines("a b c d e f g h", 20, 10) > 2


# ---------------------------------------------------------------- the source it writes

def source(tmp_path, *tables) -> str:
    return adopt.bootstrap(deck_ir(deck(*tables), foreign=True), tmp_path / "tree" / "main.tex")


def table_source(text: str) -> str:
    start = text.index("\\adoptrow{0}")
    return text[start:text.index("\\end{tikzpicture}", start)]


def test_a_table_is_written_at_its_place_with_its_macros(tmp_path):
    text = source(tmp_path, table())
    assert "\\newcommand\\adoptcell" in text and "\\usepackage{tikz}" in text
    block = text[text.rindex("\\begin{textblock*}", 0, text.index("\\adoptrow{0}")):]
    assert block.startswith(f"\\begin{{textblock*}}{{{220 / SCALE:.1f}pt}}({50 / SCALE:.1f}pt,{80 / SCALE:.1f}pt)")
    assert "\\adopttops{3}" in text


def test_every_row_is_a_minimum_and_every_cell_with_words_a_box(tmp_path):
    t = table_source(source(tmp_path, table()))
    assert t.count("\\adoptrow{") == 3
    assert t.count("\\adoptcell{") == 6, "the empty filled cell has no box"
    assert t.count("\\node[") == 6


def test_a_merged_cell_spans_its_rows_and_columns(tmp_path):
    t = table_source(source(tmp_path, table()))
    pad = 7.2 / SCALE
    header = next(l for l in t.splitlines() if "\\adoptcell{" in l and "}{0}{0}{" in l
                  and f"{120 / SCALE - 2 * pad:.2f}pt" in l)
    assert header, "the header's text is as wide as its two columns less the insets"
    assert any("\\adoptcell{" in l and "}{1}{2}{" in l for l in t.splitlines()), "rows 1-2 merged"
    # single-row cells are set first, so a merged cell only adds what they left it short of
    cells = [l for l in t.splitlines() if "\\adoptcell{" in l]
    assert "}{1}{2}{" in cells[-1]


def test_fills_are_drawn_and_not_rendered_ones_are_not(tmp_path):
    t = table_source(source(tmp_path, table()))
    fills = [l for l in t.splitlines() if "\\fill[" in l]
    assert len(fills) == 2


def test_no_border_is_drawn_inside_a_merged_cell(tmp_path):
    t = table_source(source(tmp_path, table()))
    draws = [l for l in t.splitlines() if "\\draw[" in l]
    # the rule under the header is one stroke of three segments; the one inside "Revenue" is gone
    h = [l for l in draws if "line cap=rect" in l]
    assert len(h) == 1 and f"({220 / SCALE:.1f}pt," in h[0]
    # the dashed rule beside column 0 runs down all three rows; the one inside the header is gone
    v = [l for l in draws if "dashed" in l]
    assert len(v) == 1 and "\\adopty{0}" in v[0] and "\\adopty{3}" in v[0]


def test_cells_sit_where_their_vertical_alignment_says(tmp_path):
    nodes = [l for l in table_source(source(tmp_path, table())).splitlines() if "\\node[" in l]
    anchors = [l.split("anchor=")[1].split("]")[0].split(",")[0] for l in nodes]
    assert anchors.count("west") == 1 and anchors.count("south west") == 1
    assert anchors.count("north west") == 4


def test_segments_join_runs_of_one_style():
    el = {"row_heights": [1, 1], "col_widths": [1, 1, 1], "table_cells": [],
          "table_borders": [{"dir": "h", "row": 1, "col": c, "color": "#000000", "alpha": 1, "weight": 1,
                             "dash": "SOLID"} for c in (0, 1)]
          + [{"dir": "h", "row": 1, "col": 2, "color": "#ff0000", "alpha": 1, "weight": 1, "dash": "SOLID"},
             {"dir": "h", "row": 9, "col": 0, "color": "#000000", "alpha": 1, "weight": 1, "dash": "SOLID"}]}
    segs = adopt.table_segments(el)
    assert [(k[2], spans) for k, spans in segs] == [("#000000", [(0, 2)]), ("#ff0000", [(2, 3)])]


def test_a_right_to_left_cell_is_read_and_written_right_to_left(tmp_path):
    """hebrew-lesson's cells: set left to right, a Hebrew line ended on the wrong side of its full stop."""
    t = table()
    head = t["table"]["tableRows"][1]["tableCells"][1]
    head["text"]["textElements"][0]["paragraphMarker"]["style"]["direction"] = "RIGHT_TO_LEFT"
    head["text"]["textElements"][1]["textRun"]["content"] = "שלום עולם.\n"
    cells = {(c["row"], c["col"]): c for c in the_table(deck(t))["table_cells"]}
    assert cells[1, 1]["paragraphs"][0]["direction"] == "rtl"
    assert "direction" not in cells[1, 2]["paragraphs"][0]
    text = source(tmp_path, t)
    assert "\\babelprovide" in text and "hebrew" in text
    assert "\\begin{otherlanguage}{hebrew}" in table_source(text)


def test_cell_lines_are_as_far_apart_as_slides_sets_them(tmp_path):
    """The size switch alone spaced a cell's lines by the class's leading (hebrew-lesson: 14.0 pt where
    the thumbnail shows 14.45); `adopt.cell_lead` sets Slides' pitch, and leaves the one-line height
    (the strut) to the size switch, since the row height says it already."""
    import re
    t = table_source(source(tmp_path, table()))
    skips = {float(v) for v in re.findall(r"\\baselineskip=([\d.]+)pt", t)}
    assert len(skips) == 1 and skips.pop() == pytest.approx(sum(adopt.line_box(12 / SCALE, 1.0)), abs=0.1)
    assert "\\strutbox" not in t


def test_a_cell_is_set_again_without_insets_when_its_rows_are_too_short(tmp_path):
    """creandum-board keeps "+1 months" on one line in a 40 pt column; the macro gets the full
    column width and the shift that keeps a centred paragraph centred."""
    t = table_source(source(tmp_path, table()))
    header = next(l for l in t.splitlines() if "\\adoptcell{" in l and "}{0}{0}{" in l
                  and f"{{{120 / SCALE:.2f}pt}}" in l)
    assert f"{{{-7.2 / SCALE:.2f}pt}}" in header
    right = [l for l in t.splitlines() if "\\adoptcell{" in l and f"{{{-2 * 7.2 / SCALE:.2f}pt}}" in l]
    assert len(right) == 3, "the numbers are right-aligned"


def test_a_one_word_cell_too_wide_for_its_insets_stays_on_its_line(tmp_path):
    """creandum-board's P&L: "(1,234)" in a narrow column is one word Slides never breaks - wider than
    the room inside the insets, it takes the cell's whole width, aligned as it says."""
    t = table_source(source(tmp_path, table()))
    ten = next(l for l in t.splitlines() if "\\hbox to\\linewidth" in l and "{10}" in l)
    assert "\\ifdim\\wd0>\\linewidth" in ten and "\\hss\\box0\\fi" in ten, "right-aligned when it fits"
    assert f"\\hbox to\\dimexpr\\linewidth+{2 * 7.2 / SCALE:.2f}pt{{\\hss\\box0}}" in ten


def test_a_middle_aligned_cell_drops_by_its_own_line_box_and_no_other_does(tmp_path):
    """Slides centres a cell's line box, whose ascent is 0.968 of 1.2 em, where TeX's strut is 0.7 of
    1.0: creandum-board's middle cells stood 0.125 em high. Top and bottom ones are placed right."""
    text = source(tmp_path, table())
    assert "\\newcommand\\adoptdrop" in text
    nodes = [l for l in table_source(text).splitlines() if "\\node[" in l]
    dropped = [l for l in nodes if "yshift=-\\adoptdrop" in l]
    assert len(dropped) == 1 and "anchor=west" in dropped[0]
