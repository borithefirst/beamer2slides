"""Tables, wave 5 of the visual hunt: an overfull table - running off the page in the PDF too -
never ends past the slide's right edge; one that cannot fit even set smaller stays a picture."""


from beamer2slides import emit as E
from beamer2slides.emit import FontMapper
from beamer2slides.emit_model import JsonMap
from beamer2slides.emit_tables import TableLayout
from beamer2slides.ir import TableElement

from .json_reads import jarr, jint, jnum, jobj, jobjs
from .test_tables_hunt import W, Page, as_json, some, tables
from .test_tables_wave4 import overfull_table


def layout(t: TableElement) -> tuple[TableLayout, float, FontMapper]:
    scale, fonts = 720.0 / W, FontMapper()
    return E.table_layout(as_json(t), scale, fonts, imported=True, page_w=W), scale, fonts


def test_an_overfull_table_ends_on_the_page_with_every_column():
    """r2_tables_v2 slide 6, r2_tables_v4 slide 2: set smaller to end where the PDF's does (past
    the page's edge), the table's last columns were cut off the slide ('EM', 'ye', 'Dropout').
    Its columns keep the PDF's places, so no size brought it back: they now close up towards their
    words and the text is set as large as still lets the table end on the page."""
    page, _ = overfull_table()
    (t,) = tables(page.elements())
    assert t["frame"][2] > W + 20  # (the case: the PDF's rules run off the page)
    lay, scale, fonts = layout(t)
    assert lay.fits and lay.bounds[-1] <= W + 0.01
    assert E.TABLE_MIN_SHRINK <= lay.shrink <= 1
    # every cell still fits its column on one line
    for (r, c), w in lay.cell_width.items():
        if w is not None:
            assert lay.widths[c] >= w + 2 * E.TABLE_CELL_PAD + E.WRAP_MARGIN - 0.01, (r, c)
    # the text is no smaller than it has to be: a little larger and the columns no longer close up
    if lay.shrink < 1:
        table = as_json(t)
        cells: list[list[list[JsonMap]]] = [
            [[{**r, "cell": True, "size": jnum(r["size"]) * (lay.shrink + 0.01)} for r in E.in_sentence(jobjs(runs))]
             for runs in jarr(row)] for row in jarr(table, "cells")]
        got = E.table_columns(table, cells, scale, fonts, tight=True)
        assert E.squeezed_columns(table, cells, got, scale, W) is None


def test_a_squeezed_columns_words_move_with_it():
    """Closed up, a left-aligned column keeps its words at its left edge: indented from the PDF's
    place they would sit at the right of their narrower cell, against the next column."""
    page, xs = overfull_table()
    (t,) = tables(page.elements())
    lay, scale, fonts = layout(t)
    reqs = E.table_requests(as_json(t), "s", "tab", scale, fonts, imported=True, page_w=W)
    indents: dict[tuple[int, int], float] = {}
    for r in reqs:
        q = r.get("updateParagraphStyle")
        if not q:
            continue
        style = jobj(q)
        if "cellLocation" in style:
            indents[(jint(style, "cellLocation", "rowIndex"), jint(style, "cellLocation", "columnIndex"))] = \
                jnum(style, "style", "indentStart", "magnitude")
    pdf_offset = [max(0.0, (c["x0"] - b) * scale - E.PAD_X) for c, b in zip(t["columns"], some(t.get("bounds")))]
    for c, col in enumerate(t["columns"]):
        if col["align"] == "left":
            assert indents[(1, c)] <= pdf_offset[c] + 0.5, c


def far_too_wide() -> Page:
    """A booktabs table of twelve columns of long words set 60 pt apart: twice the page's width."""
    page = Page()
    xs = [16 + 72 * j for j in range(12)]
    for y, width in ((58, 0.8), (74, 0.5), (130, 0.8)):
        page.hline(11, xs[-1] + 72, y, width)
    for j in range(12):
        page.text(f"Parameter{j:02}", xs[j], 69)
    for i in range(3):
        for j in range(12):
            page.text(f"value{i}.{j:02}e3", xs[j], 88 + 16 * i)
    return page


def test_a_table_that_cannot_fit_the_page_at_the_least_size_stays_a_picture():
    """Where even TABLE_MIN_SHRINK and columns closed up to their words cannot end the table on
    the page, it is no native table (it would run off the slide) but a picture of the PDF's."""
    page = far_too_wide()
    els = page.elements()
    assert not tables(els)
    assert any(e["kind"] == "image" and e["bbox"][2] > W for e in els)
