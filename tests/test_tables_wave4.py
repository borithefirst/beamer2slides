"""Tables and code listings, wave 4 of the visual hunt: a shaded row's Slides edges on its band,
measured columns that fit the PDF's frame keeping it, and the line numbers of a listing on the
code's own pitch."""

import pytest

from beamer2slides import emit as E
from beamer2slides.emit import FontMapper

from .test_tables_hunt import W, Page, shaded_grid, tables


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


def overfull_table() -> tuple[Page, list[float]]:
    """A booktabs table of 12 columns, its heads a few points apart, whose rules start at the text
    margin and run 31 pt off the page's right edge, where TeX lets an overfull box go (r2_tables_v2
    slide 6)."""
    page = Page()
    heads = ["Set", "Layers", "Hidden", "Heads", "Cutoff", "Neighb", "Batches", "LRate", "Decays", "Epochs",
             "Dropout", "EMA"]
    body = [[name] + [f"{i}.{j:02}e3" for j in range(11)] for i, name in enumerate(["mp_e", "gap", "jdft"])]
    xs = [16 + 39 * j for j in range(12)]
    for y, width in ((58, 0.8), (74, 0.5), (130, 0.8)):
        page.hline(11, xs[-1] + 40, y, width)
    for j, word in enumerate(heads):
        page.text(word, xs[j], 69)
    for i, row in enumerate(body):
        for j, word in enumerate(row):
            page.text(word, xs[j], 88 + 16 * i)
    return page, xs


def test_an_overfull_tables_rules_running_off_the_page_are_its_own():
    """The rules of a table wider than the page reach past its right edge: table_hairlines took
    them for theme hairlines, the header's close-set words merged into one drifting text box and
    the body became a rule-less plain table (r2_tables_v2 slide 6, r2_tables_v3 slide 2). Theme
    hairlines start at the page's left edge; an overfull table's start at its margin."""
    page, _ = overfull_table()
    (t,) = tables(page.elements())
    assert len(t["cells"]) == 4 and len(t["columns"]) == 12
    assert t["cells"][0][0][0]["text"] == "Set" and len(t["rules"]) == 3
    # a rule over the page's whole width is still the theme's
    page = Page()
    page.hline(0, W, 58, 0.8)
    page.hline(0, W, 130, 0.8)
    page.words("Some words", 40, 80)
    page.words("more words", 300, 80)
    page.words("A second line", 40, 100)
    assert not tables(page.elements())


def test_an_overfull_table_is_set_smaller_to_end_where_the_pdfs_does():
    """Grown by the Slides cell padding of its twelve columns, an overfull table could reach
    neither the margin nor the page edge at TABLE_MIN_SHRINK and kept its size, running further
    off the slide than the PDF's; it is set smaller until it ends where the PDF's does."""
    page, _ = overfull_table()
    (t,) = tables(page.elements())
    scale = 720.0 / W
    fonts = FontMapper()
    lay = E.table_layout(t, scale, fonts, imported=True, page_w=W)
    grown = E.table_columns(t, [[[{**r, "cell": True} for r in E.in_sentence(c)] for c in row] for row in t["cells"]],
                            scale, fonts, tight=True)[0]
    assert grown[-1] > t["frame"][2] + 5  # (the case: at its size it runs past the PDF's end)
    assert lay["bounds"][-1] <= t["frame"][2] + 0.01
    assert E.TABLE_MIN_SHRINK <= lay["shrink"] < 1


def google_steps(z: float, space_above: list[float]) -> list[float]:
    """Where Google's renderer sets the baselines of single-line paragraphs of size z, relative to
    the first: each step - the natural pitch and the paragraph's spaceAbove together - on whole
    CSS pixels (measured on the hunt's r8 renders, see `emit.pitch_between`)."""
    out = [0.0]
    for sa in space_above[1:]:
        out.append(out[-1] + round((E.LINE_EM * z + sa) / E.PX_PT) * E.PX_PT)
    return out


@pytest.mark.parametrize("font, z", [("Lato", 11.6), ("Roboto Mono", 13.4), ("Roboto Mono", 12.5), ("Lato", 8.7)])
def test_a_listings_numbers_and_code_keep_the_pdf_pitch(font, z):
    """r2_code_v3 slide 2, r2_code_v4 slides 2 and 6: a listing's line numbers (Lato 11.6 pt, one
    right-aligned box) and its code (Roboto Mono 12.5-13.4 pt, another box), 20 Slides pt apart
    in the PDF. The spaceAbove was written against the natural pitch snapped alone (14.25 pt for
    11.6 pt Lato) while Google snaps the whole step: the numbers climbed 0.5 pt a line (4 pt
    by line 14), the 13.4 pt code sank 0.5 pt a line, and the two boxes parted. Aimed unsnapped,
    every line of either box stays within half a pixel of its PDF baseline."""
    pitch = 12.6 * 720.0 / 453.54  # the PDF's 12.6 pt listing pitch in Slides pt
    blank = {4, 9}  # empty source lines: a double step
    targets, y = [], 100.0
    for k in range(14):
        targets.append(y)
        y += pitch * (2 if k in blank else 1)
    paras = [{"bullet": None, "lines": [{"baseline": t}]} for t in targets]
    ratios, space_above = E.vertical_layout(paras, [[t] for t in targets], [z] * len(targets))
    assert set(ratios) == {1.0}
    landed = [targets[0] + s for s in google_steps(z, space_above)]
    assert max(abs(a - b) for a, b in zip(landed, targets)) <= E.PX_PT / 2 + 1e-6
