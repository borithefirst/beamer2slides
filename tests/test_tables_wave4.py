"""Tables and code listings, wave 4 of the visual hunt: a shaded row's Slides edges on its band,
measured columns that fit the PDF's frame keeping it, and the line numbers of a listing on the
code's own pitch."""

import pytest

from beamer2slides import emit as E
from beamer2slides.emit import FontMapper

from .test_tables_hunt import Page, shaded_grid, tables


def test_a_shaded_row_starts_and_ends_on_its_band():
    """r2_tables_v1 slide 4: \\rowcolor bands with no rules, the words 13 pt under each band's top.
    Set just above its words (Slides' 10.4 pt from a row's top to its baseline), every row began
    ~3 pt below its band: the fills ran 14 px low on Google's renderer and the words sat at the
    top of them. Now each shaded row's edges are its band's, a top inset puts the words back on
    the PDF's baseline."""
    page = Page()
    shaded_grid(page, ["#bfbfff", "#f0f0f0", "#e0e0f8"])
    (t,) = tables(page.elements())
    assert [b[0] for b in t["bands"]] == [0, 1, 2, 3]
    assert t["bands"][0][1:] == [50, 68]
    fonts, scale = FontMapper(), 720.0 / 453.54
    lay = E.table_layout(t, scale, fonts, imported=True, page_w=453.54)
    assert lay["y"] / scale == pytest.approx(50, abs=0.05)
    tops = [lay["y"] + sum(lay["heights"][:i]) for i in range(len(lay["heights"]) + 1)]
    assert [round(y / scale, 1) for y in tops] == [50, 68, 86, 104, 122]
    # the words stay on the PDF's baselines: the row's inset takes the room above them
    for i, top in enumerate(tops[:-1]):
        base = top + lay["insets"][i] + E.TABLE_TEXT_TOP + E.ASCENT_EM * lay["sizes"][i]
        assert base / scale == pytest.approx(t["row_baselines"][i], abs=0.1)


def test_a_band_spanning_two_rows_is_no_row_edge():
    """A fill holding two rows' baselines (a \\cellcolor over a \\multirow, a panel behind the
    table) says nothing about where either row starts."""
    page = Page()
    shaded_grid(page, ["#c8d4e8", "#eef2f8"])
    page.drawings.insert(0, {**page.drawings[0], "id": "p0dback", "bbox": [60, 68, 390, 104],
                             "path": [["re", [[60, 68], [390, 104]]]]})
    for d in page.drawings[1:]:
        if d["bbox"][1] in (68, 86):
            d["fill"] = "#ffffff"  # (the rows over the panel are left the page's colour)
    els = page.elements()
    for t in tables(els):
        assert all(b[0] not in (1, 2) or b[2] - b[1] <= 18.01 for b in t.get("bands", []))


def test_measured_columns_that_fit_the_frame_keep_the_pdf_width():
    """r2_tables_v1 slide 4 (a \\centering table, Lato words at most 1.2 pt wider than CM's): the
    last column got 8% of its width as room for the substitute font although its words were
    measured, the table grew 4.6 pt past its frame and table_shift moved it 2.3 pt left, ~10 px
    wider each side on Google. A measured column gets only what its words take past the PDF's
    and WRAP_MARGIN."""
    scale = 1.9844
    bounds = [24.14, 100.57, 149.93, 196.15, 253.71, 338.69]
    cols = [{"x0": 30.12, "x1": 94.53, "align": "left"}, {"x0": 106.54, "x1": 143.95, "align": "right"},
            {"x0": 155.91, "x1": 190.16, "align": "right"}, {"x0": 202.12, "x1": 247.73, "align": "right"},
            {"x0": 259.68, "x1": 332.62, "align": "left"}]
    need = [64.62, 39.28, 35.59, 41.73, 74.1]
    out = E.fit_columns(bounds, cols, scale, need)
    assert out[0] == pytest.approx(24.14) and out[-1] == pytest.approx(338.69)
    pad = E.TABLE_CELL_PAD / scale
    for c, n, a, b in zip(cols, need, out, out[1:]):
        assert b - a >= n + 2 * pad + E.WRAP_MARGIN / scale  # no cell wraps
    # an unmeasured column still keeps its 8% for the substitute font
    loose = E.fit_columns(bounds, cols, scale, [None] * 5)
    assert loose[-1] > 338.69 + 4
