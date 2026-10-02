"""Tables: column widths from measured Slides advances, row heights, whether a table fits the page,
and the requests that fill it.

Each is planned from a `SetTable` (emit_model: a parsed `TableElement` through `set_table`, a dict
through `table_of`), and what it derives - the columns, the layout, the empty table the .pptx
carries - is a record (`ColumnFit`, `TableLayout`, `PptxTable`). The names without `_of` take the
dicts sync, classify and the tests hold.
"""

import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, TypedDict

from . import bidi
from .emit_metrics import ASCENT_EM, BASELINE_A, LINE_EM, PAD_X, SLIDE_W, FontMapper, u16
from .emit_model import (
    CellGrid, JsonMap, ObjectMap, PptxTable, SetRun, SetTable, columns_of, set_table, table_of,
)
from .emit_text import baseline_offset, extra_above, in_sentence_of, run_sizes_of
from .emit_widths import (
    WRAP_MARGIN, set_runs_of, slides_width_of, wrap_joins_of, wrap_window_of, wrapped_width_of,
)
from .google_types import (
    SlidesParagraphStyle, SlidesRequest, SlidesTableCellLocation, slides_text_style,
)
from .gslides import EMU_PER_PT, emu, pt, rgb_color, text_color
from .ir import Align
from .ir_types import Column, Merge, TableElement
from .json_types import Json
from .typing_compat import assert_never


TABLE_MIN_COLUMN_PT = 32.0  # the API refuses narrower columns
# A table made by createTable has 7.2 pt of cell padding above and below that the API cannot
# change; a table the .pptx brings keeps the file's `a:tcPr` margins, down to 0
# (tools/probe_pptx_table_margins.py). emit's own tables come with the .pptx (`build_pptx`).
TABLE_ROW_PAD = 14.4        # an API-made table's padding above and below
TABLE_ROW_EM = 1.195        # a row of one line is at least its insets + 1.195·z·lineSpacing
TABLE_MIN_SPACING = 0.5
TABLE_TEXT_TOP = BASELINE_A - TABLE_ROW_PAD / 2  # cell top inset -> first baseline, less ASCENT_EM·z

CellWidths = Mapping[tuple[int, int], float | None]
"""(row, col) -> the Slides width of a cell's text where it is known."""


@dataclass(frozen=True, kw_only=True)
class ColumnFit:
    """A table's columns as `table_columns_of` fits them."""
    bounds: tuple[float, ...]
    """Column boundaries, PDF pt."""
    cell_width: CellWidths
    """(row, col) -> the text's Slides width where known (a wrapped cell's least width at the PDF's line count)."""
    held: tuple[float | None, ...]
    """Per column, the text width (PDF pt) its left-aligned cells are held under (their indentEnd), or None."""


@dataclass(frozen=True, kw_only=True)
class Squeezed:
    """A table's columns closed up towards their words (`squeezed_columns_of`)."""
    fit: ColumnFit
    moved: tuple[float, ...]
    """Per column, PDF pt its words moved on their alignment edge."""


@dataclass(frozen=True, kw_only=True)
class TableLayout:
    """Where a table goes in Slides (`table_layout_of`), Slides pt unless said otherwise."""
    x: float
    y: float
    widths: tuple[float, ...]
    heights: tuple[float, ...]
    ratios: tuple[float, ...]
    """Per row, its lineSpacing."""
    insets: tuple[float, ...]
    """Per row, its top cell inset."""
    bounds: tuple[float, ...]
    """Column boundaries, PDF pt."""
    cell_width: CellWidths
    z: float
    first_run: SetRun | None
    cells: CellGrid
    """The runs as they are written: `in_sentence`, `cell`, shrunk."""
    shrink: float
    """The share of its size the text is set at."""
    dx: float
    """PDF pt the columns moved (table_shift)."""
    held: tuple[float | None, ...]
    sizes: tuple[float, ...]
    """Per row, the Slides size its lines are laid out at (row_sizes)."""
    moved: tuple[float, ...]
    """Per column, PDF pt its words moved as its columns closed up (squeezed_columns_of)."""
    fits: bool
    """False: it cannot end on the page at TABLE_MIN_SHRINK, and classify keeps it a picture."""


def table_line_spacing(pitch: float, z: float, pad: float) -> float:
    """lineSpacing ratio at which a row of text size z fits into the given row pitch."""
    return min(1.0, max(TABLE_MIN_SPACING, (pitch - pad) / (TABLE_ROW_EM * z)))


TABLE_CELL_PAD = 7.2  # cell padding left and right


def _left(align: Align, room: float) -> float:
    """The share of a column's room on its text's left: text grows away from its alignment edge."""
    match align:
        case "right":
            return room
        case "center":
            return room / 2
        case "left":
            return 0.0
        case _:
            assert_never(align)


def _right(align: Align, room: float) -> float:
    match align:
        case "left":
            return room
        case "center":
            return room / 2
        case "right":
            return 0.0
        case _:
            assert_never(align)


def fit_columns(bounds: list[float], cols: list[dict[str, Json]], scale: float,
                need: list[float | None] | None, tight: bool,
                cap: list[float | None] | None) -> list[float]:
    """`fit_columns_of` column dicts (the tests'). `need`, `cap`: None is None for every column."""
    return fit_columns_of(bounds, columns_of(cols), scale, need or [None] * len(cols), tight, cap or [None] * len(cols))


def fit_columns_of(bounds: Sequence[float], cols: Sequence[Column], scale: float, need: Sequence[float | None],
                   tight: bool, cap: Sequence[float | None]) -> list[float]:
    """Column boundaries (PDF pt) moved just enough that every column's text fits inside the
    Slides cell padding, with room for the substitute font. Tables typeset with @{} have text
    touching the frame, which would otherwise wrap in Slides.

    need[i] is the width (PDF pt) column i's widest one-column cell takes in Slides
    (slides_width), when it is known. A cell that wraps in Slides doubles its row and pushes the
    table down over whatever stands under it - a caption - so where the PDF left less room
    between two columns than their text needs, the table grows sideways instead: the columns
    after move right.

    `tight`, for a table that would otherwise run off the page (table_layout): a measured column
    is as wide as its text in Slides and WRAP_MARGIN, lined up on its alignment edge - narrower
    than the PDF's when the text is set smaller.

    cap[i], for a left-aligned column holding a wrapped cell (table_columns): the text width
    (PDF pt) at which a line of that cell would take the next line's first word. The column's
    text room stays under it where its own lines allow (capped_columns), so the cell breaks where
    TeX did: joined into fewer lines than the PDF's, it left its row - which keeps the PDF's
    pitch - half empty. The cap is the cells' indentEnd (table_requests), not the boundary: moved
    left to it, the rule after the column left the PDF's place, the next column's words an
    indent away from it (r2_tables_v1 slide 2)."""
    pad = TABLE_CELL_PAD / scale
    if tight:
        def extent(c: Column, n: float | None) -> Column:
            if n is None:
                return c
            x0 = c.x0 if c.align == "left" else c.x1 - n if c.align == "right" else (c.x0 + c.x1 - n) / 2
            return replace(c, x0=x0, x1=x0 + n)
        cols = [extent(c, n) for c, n in zip(cols, need)]
    # Text grows away from its alignment edge: room on the right of left-aligned columns, on
    # the left of right-aligned ones, half on each side of centred ones. A column whose words
    # were measured (need) gets what they take past the PDF's and WRAP_MARGIN; only an
    # unmeasured one keeps 8% for the substitute font. With the 8% on measured columns too, a
    # table whose words fit its PDF frame grew 2-4 pt past it on each side (r2_tables_v1
    # slide 4, r2_tables_v2 slide 10).
    width = [c.x1 - c.x0 for c in cols]
    room = [(1 + WRAP_MARGIN) / scale if tight and n is not None else
            max(0.0, n - w) + (1 + WRAP_MARGIN) / scale if n is not None else 0.08 * w + 1 / scale
            for w, n in zip(width, need)]
    capped = capped_columns(cols, scale, need, cap)
    room = [min(r, k - w) if ok and k is not None else r for r, w, k, ok in zip(room, width, cap, capped)]
    right = [_right(c.align, r) for c, r in zip(cols, room)]
    left = [_left(c.align, r) for c, r in zip(cols, room)]
    out = list(bounds)
    out[0] = min(out[0], cols[0].x0 - pad - left[0])
    out[-1] = max(out[-1], cols[-1].x1 + pad + right[-1])
    for i in range(1, len(cols)):
        lo, hi = cols[i - 1].x1 + pad + right[i - 1], cols[i].x0 - pad - left[i]
        out[i] = min(max(out[i], lo), hi) if lo <= hi else (lo + hi) / 2
    # Crowded columns: a column narrower than its text, its room and both paddings pushes every
    # boundary after it along (table_requests then caps the cell's alignment indent).
    for i in range(len(cols)):
        least = width[i] + room[i] + 2 * pad
        if out[i + 1] - out[i] < least:
            shift = least - (out[i + 1] - out[i])
            out[i + 1:] = [x + shift for x in out[i + 1:]]
    return out


def capped_columns(cols: Sequence[Column], scale: float, need: Sequence[float | None],
                   cap: Sequence[float | None]) -> list[bool]:
    """Which columns' text room is held under their `cap` (fit_columns): a left-aligned column
    whose cap still leaves its widest cell (`need`, else its PDF width) and WRAP_MARGIN."""
    return [k is not None and c.align == "left" and
            k >= (n if n is not None else c.x1 - c.x0) + (1 + WRAP_MARGIN) / scale
            for c, n, k in zip(cols, need, cap)]


def table_rows(t: SetTable, z: float, scale: float, imported: bool, sizes: Sequence[float]
               ) -> tuple[float, list[float], list[float], list[float]]:
    """Table top, row heights, per-row lineSpacing and per-row top cell inset (Slides pt).

    `sizes`: per row, the Slides size of its largest words (row_sizes), which set where its
    top-anchored baseline falls and how tall its lines are; `z` for every row when empty.

    A row is at least its top and bottom insets + 1.195·z·lineSpacing tall (+ LINE_EM·z·lineSpacing
    per further line of a wrapped cell, `row_lines`). Row boundaries sit on the PDF's rules where
    there are any (booktabs puts extra space around them) and else just above the next row's text.

    An API-made table (`imported` False) has 7.2 pt insets above and below, which TeX's rows are
    too tight for (tools/probe_table_rows.py): the line spacing is tightened until rows keep the
    original pitch, and each row's lineSpacing then moves its baseline to the PDF's. A table the
    .pptx brings (`build_pptx`) has no inset below and a top inset of its own per row: the text
    keeps its natural line spacing and the inset moves the baseline down to the PDF's (booktabs'
    space under a rule), so a row keeps the PDF's pitch down to 1.195 em."""
    baselines = [b * scale for b in t.row_baselines]
    n = len(baselines)
    pitches = [h * scale for h in t.row_heights]
    lines = list(t.row_lines) or [1] * n
    pad_top = pad_bottom = 0.0 if imported else TABLE_ROW_PAD / 2
    default = 1.0 if imported else table_line_spacing(min(pitches), z, TABLE_ROW_PAD)
    zs = list(sizes) or [z] * n

    def offset(r: float, i: int) -> float:  # row top -> baseline, with the fixed top inset
        zi = z if i >= n else zs[i]
        return pad_top + TABLE_TEXT_TOP + ASCENT_EM * zi + extra_above(r, zi)

    def body(i: int) -> float:  # the height of row i's text at lineSpacing 100
        return (TABLE_ROW_EM + (lines[i] - 1) * LINE_EM) * zs[i]

    ruled: dict[int, float] = {}
    for rule in t.rules:
        ruled.setdefault(rule.row + (1 if rule.position == "BOTTOM" else 0), rule.y * scale)
    for b in t.borders:
        if b.y is not None and b.position in ("TOP", "BOTTOM"):
            ruled.setdefault(b.row + (1 if b.position == "BOTTOM" else 0), b.y * scale)
    # A row shaded by a band of its own (classify `bands`) starts and ends where its band does:
    # just above its words, a \rowcolor row began ~3 pt below its fill, its words at the top of
    # the Slides cell's shading (r2_tables_v1 slide 4). A rule still wins.
    for row, y0, y1 in t.bands:
        ruled.setdefault(row, y0 * scale)
        ruled.setdefault(row + 1, y1 * scale)

    def target(i: int) -> float:  # where row i should start
        if i in ruled:
            return ruled[i]
        if i < n:
            return baselines[i] - offset(default, i)
        return baselines[-1] - offset(default, n - 1) + pitches[-1]

    def clamp(r: float) -> float:
        return min(1.0, max(TABLE_MIN_SPACING, r))

    # (\multirow heads are centred in their rows, contentAlignment MIDDLE, and have no inset of
    # their own: pptx_table's `middle`. The other cells of their first row keep the row's inset:
    # 'WP1 status' beside a \multirow sat at the top of its cell, r2_tables_v3 slide 7.)
    # Row by row from the actual top: a row's baseline offset and its minimum height both grow
    # with lineSpacing, so when the room above the text (from a rule) asks for more height than
    # the row has, the error is split between this baseline and the rows below.
    top = target(0)
    y = top
    heights: list[float] = []
    ratios: list[float] = []
    insets: list[float] = []
    for i in range(n):
        room = baselines[i] - y  # offset(r) = offset(1) - (1 - r)·0.9·z
        h_target = target(i + 1) - y
        inset = pad_top
        if imported:
            # The inset takes the room above the text, as far as the row's height allows.
            inset = max(0.0, min(room - offset(1.0, i), h_target - pad_bottom - body(i)))
        r_room = clamp(1.0 if room >= offset(1.0, i) + inset else
                       1 - (offset(1.0, i) + inset - room) / (0.75 * LINE_EM * zs[i]))
        r_fit = clamp((h_target - inset - pad_bottom) / body(i))
        r = r_room if r_room <= r_fit else (r_room + r_fit) / 2
        h = max(h_target, inset + pad_bottom + body(i) * r)
        ratios.append(r)
        heights.append(h)
        insets.append(inset)
        y += h
    return top, heights, ratios, insets


def table_columns(el: ObjectMap, cells: list[list[list[JsonMap]]], scale: float, fonts: FontMapper,
                  tight: bool) -> tuple[list[float], dict[tuple[int, int], float | None], list[float | None]]:
    """`table_columns_of` a table dict whose cells hold run dicts (the tests')."""
    grid = tuple(tuple(set_runs_of(runs) for runs in row) for row in cells)
    got = table_columns_of(table_of(el), grid, scale, fonts, tight)
    return list(got.bounds), dict(got.cell_width), list(got.held)


def table_columns_of(t: SetTable, cells: CellGrid, scale: float, fonts: FontMapper, tight: bool) -> ColumnFit:
    """Column boundaries (PDF pt) of a table whose cells hold `cells`, each cell's Slides width
    where it is known (slides_width; a wrapped cell's least width at the PDF's line count,
    wrap_window), and per column the text width (PDF pt) its cells are held under (their
    indentEnd, table_requests) or None. `tight`: see fit_columns."""
    cols = t.columns
    fx0, _, fx1, _ = t.frame
    bounds = list(t.bounds) or [fx0] + [(a.x1 + b.x0) / 2 for a, b in zip(cols, cols[1:])] + [fx1]
    spanned = {(m.row, m.col) for m in t.merges if m.cols > 1}
    # A cell set in a paragraph column (p{3cm}) wraps in Slides as in the PDF (classify
    # `wrapped`): it takes the least room its words need in as many lines as the PDF's.
    wrapped = {(r, c): starts for r, c, starts in t.wrapped}
    window = {rc: wrap_window_of(cells[rc[0]][rc[1]], len(starts) + 1, scale, fonts)
              for rc, starts in wrapped.items() if rc[0] < len(cells) and rc[1] < len(cells[rc[0]])}

    def width_of(rc: tuple[int, int], runs: Sequence[SetRun]) -> float | None:
        if rc in window:
            least = window[rc]
            return None if least is None else least[0]
        return slides_width_of(runs, scale, fonts)

    cell_width: dict[tuple[int, int], float | None] = {
        (r, c): width_of((r, c), runs) for r, row in enumerate(cells) for c, runs in enumerate(row) if runs}
    need: list[float | None] = []
    cap: list[float | None] = []
    for c, col in enumerate(cols):
        ws = [w for (r, cc), w in cell_width.items() if cc == c and (r, c) not in spanned]
        known = [w for w in ws if w is not None]
        need.append(None if not ws or len(known) < len(ws) else max(known) / scale)
        # A wrapped cell breaks where TeX did while no line has room for the next word (wrap_joins)
        # when its lines fit the PDF's column (a word TeX hyphenated taken whole: wrapped_width);
        # else, and when the cells' TeX breaks leave no common room, it keeps its line count
        # (wrap_window). A font the probe did not measure is the PDF's own (a Google font it
        # uses): TeX broke each line because the next word overflowed its p{} width, at least
        # the widest line.
        mine = [((r, cc), starts) for (r, cc), starts in wrapped.items() if cc == c and (r, cc) not in spanned]
        w = col.x1 - col.x0
        if not mine:
            cap.append(None)
            continue
        windows = [window.get(rc) for rc, _ in mine]
        measured = [x for x in windows if x is not None]
        if len(measured) < len(windows):
            cap.append(w + max((1 + WRAP_MARGIN) / scale, 0.02 * w))
            continue
        counted = min(x[1] for x in measured) / scale - 0.5 / scale
        exact: list[float] = []
        for (rc, starts), least in zip(mine, measured):
            runs = cells[rc[0]][rc[1]]
            whole, joins = wrapped_width_of(runs, starts, scale, fonts), wrap_joins_of(runs, starts, scale, fonts)
            fits = whole is not None and joins is not None and whole / scale <= 1.08 * w
            exact.append(joins if fits and joins is not None else least[1])
        k = min(exact) / scale - 0.5 / scale
        k = k if capped_columns([col], scale, [need[-1]], [k])[0] else counted
        widest = need[-1]
        if widest is not None and all(rc in t.justified for rc, _ in mine):
            # Justified cells (classify `justified`) are set out to the PDF's edge (table_requests):
            # the column needs no room past what its words take, which only widened the table.
            k = min(k, max(w, widest) + (1 + WRAP_MARGIN) / scale)
        cap.append(k)
    bounds = fit_columns_of(bounds, cols, scale, need, tight, cap)
    held = [k if ok else None for k, ok in zip(cap, capped_columns(cols, scale, need, cap))]
    # A cell spanning columns wraps as readily as one that does not: the columns it spans grow.
    for m in t.merges:
        span = cell_width.get((m.row, m.col))
        end = m.col + m.cols
        if m.cols > 1 and span is not None:
            short = (span + 2 * TABLE_CELL_PAD + 1 + WRAP_MARGIN) / scale - (bounds[end] - bounds[m.col])
            if short > 0:
                bounds[end:] = [x + short for x in bounds[end:]]
    return ColumnFit(bounds=tuple(bounds), cell_width=cell_width, held=tuple(held))


# A table fit_columns widens past the page (eleven \scriptsize columns, each given room for the
# substitute and both cell paddings; a full-width tabularx) lost its last column off the slide.
# It may reach into the right margin by TABLE_MARGIN of the room left of it; past that, it first
# keeps only the room its measured text needs, then its text is set smaller - down to
# TABLE_MIN_SHRINK of its size - until it ends there (or, if the margin is out of reach, at the
# page edge). A table still on the page that would shrink by less than 2% keeps its size.
# A table the PDF centres on the page (\centering) grows on both sides alike, each side into
# TABLE_MARGIN of its margin (table_shift): grown to the right only, it reached the slide's edge
# with its left margin whole (r2_tables_v2 slide 10).
TABLE_MARGIN = 0.5
TABLE_MIN_SHRINK = 0.75
TABLE_KEEP_SIZE = 0.98
TABLE_CENTRED_TOL = 2.0  # pt the two margins of a centred table may differ by


def table_centred(t: SetTable, page_w: float) -> tuple[float, float] | None:
    """(left, right) of a table the PDF centres on the page, else None."""
    pdf = t.bounds or (t.frame[0], t.frame[2])
    left, right = pdf[0], pdf[-1]
    return (left, right) if left > 0 and abs(left - (page_w - right)) <= TABLE_CENTRED_TOL else None


def table_shift(t: SetTable, bounds: Sequence[float], page_w: float) -> float:
    """How far (PDF pt) a centred table's Slides columns move to stay centred where they grew
    wider than the PDF's (never off the page); 0 for any other table. Mostly left: a column
    grows away from its alignment edge, to the right. Right when its first column's cell padding
    took more of the left margin than its words took of the right one."""
    centred = table_centred(t, page_w)
    if centred is None:
        return 0.0
    dx = ((centred[0] + centred[1]) - (bounds[0] + bounds[-1])) / 2
    return max(dx, -bounds[0]) if dx < 0 else min(dx, max(0.0, page_w - bounds[-1]))


def _shrunk(cells: CellGrid, s: float) -> CellGrid:
    return tuple(tuple(tuple(replace(r, size=r.size * s) for r in runs) for runs in row) for row in cells)


def table_layout(el: ObjectMap, scale: float, fonts: FontMapper, imported: bool, page_w: float) -> TableLayout:
    """`table_layout_of` a table dict (the tests'). `page_w`: the PDF page's width (on a deck
    SLIDE_W wide, what every Slides page size is: SLIDE_W / scale)."""
    return table_layout_of(table_of(el), scale, fonts, imported, page_w)


def table_layout_of(t: SetTable, scale: float, fonts: FontMapper, imported: bool, page_w: float) -> TableLayout:
    """Where a table goes in Slides (TableLayout). `imported`: the table comes with the .pptx
    (table_rows). `page_w`: the PDF page's width."""
    # (`cell`: set at the table's size, not shaped to the PDF's width: FontMapper.shape_ratio)
    cells: CellGrid = tuple(tuple(tuple(replace(r, cell=True) for r in in_sentence_of(runs)) for runs in row)
                            for row in t.cells)
    base = cells
    roomy = table_columns_of(t, cells, scale, fonts, False)
    chosen = roomy
    shrink = 1.0
    # Into the right margin at most half as far as the table stands from the left edge (TABLE_MARGIN),
    # unless the PDF's own table reaches further. A centred table grows into both margins
    # (table_shift): as far again, half of it on each side.
    centred = table_centred(t, page_w)
    reach = page_w - TABLE_MARGIN * max(0.0, roomy.bounds[0]) if centred is None else \
        centred[1] + 2 * TABLE_MARGIN * centred[0] - (centred[0] - roomy.bounds[0])
    limit = min(page_w, max(reach, t.frame[2], (t.bounds or (0.0,))[-1]))
    fits, moved = True, (0.0,) * len(t.columns)
    if roomy.bounds[-1] > limit + 0.01:
        tight = table_columns_of(t, cells, scale, fonts, True)
        least = table_columns_of(t, _shrunk(base, TABLE_MIN_SHRINK), scale, fonts, True)
        # The margin if the smallest size reaches it, else the page edge - never past the page,
        # whatever the PDF does: an overfull table (running off the page in the PDF too) set
        # smaller to end where the PDF's does lost its last columns off the slide (r2_tables_v2
        # slide 6, r2_tables_v4 slide 2).
        goal = next((g for g in (limit, page_w) if least.bounds[-1] <= g + 0.01), None)
        if goal is None:
            # Its columns keep the PDF's places (fit_columns), so no size alone brings it back:
            # they close up towards their words (squeezed_columns), the text kept as large as
            # still lets the table end on the page, at its margin (`reach`) where that size
            # allows it. Where even TABLE_MIN_SHRINK cannot, `fits` is False and classify keeps
            # it a picture (table_fits).
            packed = squeezed_layout(t, cells, scale, fonts, tuple(sorted({min(reach, page_w), limit, page_w})))
            if packed is None:
                # (as before: its size and room, smaller words would not bring its last column back)
                fits, chosen = False, roomy if roomy.bounds[-1] <= page_w + 0.01 else tight
            else:
                shrink, squeezed = packed
                chosen, moved = squeezed.fit, squeezed.moved
                cells = _shrunk(base, shrink)
        else:
            best: tuple[float, ColumnFit] | None = None
            if tight.bounds[-1] > limit + 0.01:
                lo, hi, best = TABLE_MIN_SHRINK, 1.0, (TABLE_MIN_SHRINK, least)
                for _ in range(12):
                    mid = (lo + hi) / 2
                    got = table_columns_of(t, _shrunk(base, mid), scale, fonts, True)
                    if got.bounds[-1] <= goal + 0.01:
                        lo, best = mid, (mid, got)
                    else:
                        hi = mid
            if tight.bounds[-1] <= limit + 0.01:
                chosen = tight
            elif best is not None and (best[0] < TABLE_KEEP_SIZE or roomy.bounds[-1] > page_w + 0.01):
                shrink, chosen = best
                cells = _shrunk(base, shrink)
            else:
                # A table on the page that a smaller size would pull back only a little from the
                # margin keeps its size and its room (every size step is a residual for pull).
                chosen = roomy if roomy.bounds[-1] <= page_w + 0.01 else tight
    dx = table_shift(t, chosen.bounds, page_w)
    bounds = tuple(b + dx for b in chosen.bounds)
    widths = tuple(max(TABLE_MIN_COLUMN_PT, (b - a) * scale) for a, b in zip(bounds, bounds[1:]))
    first_run = next((r for row in cells for cell in row for r in cell), None)
    z = fonts.size_of(first_run, scale)[1] if first_run is not None else t.size * scale * shrink
    sizes = row_sizes(t, cells, z, scale, fonts)
    y, heights, ratios, insets = table_rows(t, z, scale, imported, sizes)
    return TableLayout(x=bounds[0] * scale, y=y, widths=widths, heights=tuple(heights), ratios=tuple(ratios),
                       insets=tuple(insets), bounds=bounds, cell_width=chosen.cell_width, z=z, first_run=first_run,
                       cells=cells, shrink=round(shrink, 3), dx=dx, held=chosen.held, sizes=sizes, moved=moved,
                       fits=fits)


def squeezed_columns(el: ObjectMap, cells: list[list[list[JsonMap]]],
                     got: tuple[list[float], dict[tuple[int, int], float | None], list[float | None]],
                     scale: float, goal: float
                     ) -> tuple[list[float], dict[tuple[int, int], float | None], list[float | None], list[float]] | None:
    """`squeezed_columns_of` a table dict and table_columns' answer (the tests'). (`cells` is not read.)"""
    bounds, cell_width, held = got
    out = squeezed_columns_of(table_of(el), ColumnFit(bounds=tuple(bounds), cell_width=cell_width, held=tuple(held)),
                              scale, goal)
    return None if out is None else (list(out.fit.bounds), dict(out.fit.cell_width), list(out.fit.held),
                                     list(out.moved))


def squeezed_columns_of(t: SetTable, got: ColumnFit, scale: float, goal: float) -> Squeezed | None:
    """`got` (table_columns' tight answer) with its columns narrowed towards their words until the
    table ends at `goal` (PDF pt), each by the same share of the room it has past its least - its
    widest one-column cell in Slides, WRAP_MARGIN and both cell paddings (fit_columns' own), or
    for a column nothing measured its PDF width and 8%, and never under TABLE_MIN_COLUMN_PT. Also
    per column how far its words move (PDF pt), on their alignment edge. None if the least widths,
    or a cell spanning columns, cannot end there."""
    bounds, cell_width = got.bounds, got.cell_width
    cols = t.columns
    pad = TABLE_CELL_PAD / scale
    spanned = {(m.row, m.col) for m in t.merges if m.cols > 1}
    least: list[float] = []
    for c, col in enumerate(cols):
        ws = [w for (r, cc), w in cell_width.items() if cc == c and (r, c) not in spanned]
        known = [w for w in ws if w is not None]
        text = max(known) / scale + (1 + WRAP_MARGIN) / scale if ws and len(known) == len(ws) else \
            1.08 * (col.x1 - col.x0) + 1 / scale
        least.append(max(text + 2 * pad, TABLE_MIN_COLUMN_PT / scale))
    widths = [b - a for a, b in zip(bounds, bounds[1:])]
    cut = bounds[-1] - goal
    if cut > 0:
        slack = [max(0.0, w - k) for w, k in zip(widths, least)]
        if sum(slack) < cut - 0.01:
            return None
        widths = [w - s * cut / sum(slack) for w, s in zip(widths, slack)]
    out = [bounds[0]]
    for w in widths:
        out.append(out[-1] + w)
    for m in t.merges:
        span = cell_width.get((m.row, m.col))
        if m.cols > 1 and span is not None and \
                out[m.col + m.cols] - out[m.col] < (span + 2 * TABLE_CELL_PAD + 1 + WRAP_MARGIN) / scale - 0.01:
            return None
    moved = tuple((b1 - a1) if col.align == "left" else (b2 - a2) if col.align == "right" else (b1 + b2 - a1 - a2) / 2
                  for col, a1, a2, b1, b2 in zip(cols, bounds, bounds[1:], out, out[1:]))
    return Squeezed(fit=replace(got, bounds=tuple(out)), moved=moved)


def squeezed_layout(t: SetTable, cells: CellGrid, scale: float, fonts: FontMapper,
                    goals: tuple[float, ...]) -> tuple[float, Squeezed] | None:
    """(shrink, squeezed_columns' answer) for a table ending at one of `goals` (PDF pt, nearest
    first), its text as large as any of them allows (TABLE_MIN_SHRINK at least), the nearest of
    those that allow that; None if it reaches none of them."""
    def packed(s: float, goal: float) -> Squeezed | None:
        return squeezed_columns_of(t, table_columns_of(t, _shrunk(cells, s), scale, fonts, True), scale, goal)

    best: tuple[float, float] | None = None
    for goal in goals:
        if packed(TABLE_MIN_SHRINK, goal) is None:
            continue
        lo, hi = TABLE_MIN_SHRINK, 1.0
        if packed(1.0, goal) is not None:
            lo = 1.0
        else:
            for _ in range(12):
                mid = (lo + hi) / 2
                if packed(mid, goal) is not None:
                    lo = mid
                else:
                    hi = mid
        if best is None or lo > best[0] + 0.005:
            best = lo, goal
    if best is None:
        return None
    got = packed(best[0], best[1])  # (never None: each size kept was tried)
    return None if got is None else (best[0], got)


def table_fits(el: ObjectMap, page_w: float) -> bool:
    """Whether a table element (classify's dict) can be set on its page in Slides, at
    TABLE_MIN_SHRINK of its size at the least (table_layout's `fits`), on a deck SLIDE_W wide."""
    return table_layout_of(table_of(el), SLIDE_W / page_w, _fit_fonts(), True, page_w).fits


@functools.lru_cache(maxsize=1)
def _fit_fonts() -> "FontMapper":
    return FontMapper()


def row_sizes(t: SetTable, cells: CellGrid, z: float, scale: float, fonts: FontMapper) -> tuple[float, ...]:
    """Per row, the Slides size of its largest words, where its top-anchored lines take their
    ascent and height from (table_rows): a \\footnotesize note row set at the table's size sat a
    point above the PDF's baseline (r2_tables_v2 slide 1). Scripts, a \\multirow centred in its
    rows and the cells a merge covers do not count; a row of no words is `z`. An empty cell holds a
    space at its row's size (table_requests), which then never makes the row taller."""
    merges = t.merges
    middle = {(m.row, m.col) for m in merges if m.rows > 1}
    hidden = {(m.row + i, m.col + j) for m in merges
              for i in range(m.rows) for j in range(m.cols)} - {(m.row, m.col) for m in merges}
    out: list[float] = []
    for r, row in enumerate(cells):
        sizes: list[float] = []
        for c, runs in enumerate(row):
            if (r, c) in hidden or (r, c) in middle:
                continue
            sizes += [fonts.size_of(run, scale)[1] for run in runs if run.text.strip() and run.script is None]
        out.append(max(sizes) if sizes else z)
    return tuple(out)


class PptxTableDict(TypedDict):
    """A `PptxTable` as sync's refill reads it (`sync.table_refill`), in lists as the base holds them."""
    x: float
    y: float
    widths: list[float]
    heights: list[float]
    margins: list[list[float]]
    middle: list[list[int]]


def pptx_table(el: ObjectMap, scale: float, fonts: FontMapper, page_w: float) -> PptxTableDict:
    """`pptx_table_of` a table dict, as a dict (sync's refill, the tests). `page_w`: see
    `table_layout`."""
    table = pptx_table_of(table_of(el), scale, fonts, page_w)
    return {"x": table.x, "y": table.y, "widths": list(table.widths), "heights": list(table.heights),
            "margins": [list(m) for m in table.margins], "middle": [list(rc) for rc in table.middle]}


def pptx_table_of(t: SetTable, scale: float, fonts: FontMapper, page_w: float) -> PptxTable:
    """The empty table the .pptx carries for a table element (build_pptx): its box, grid and
    per-row cell margins (left, top, right, bottom; Slides pt), and the cells that span rows
    (`middle`: a \\multirow, centred in its rows with no top margin of its own). The API fills it
    in (table_requests)."""
    lay = table_layout_of(t, scale, fonts, True, page_w)
    return PptxTable(x=lay.x, y=lay.y, widths=lay.widths, heights=lay.heights,
                     margins=tuple((TABLE_CELL_PAD, round(ins, 2), TABLE_CELL_PAD, 0.0) for ins in lay.insets),
                     middle=tuple((m.row, m.col) for m in t.merges if m.rows > 1))


def merged_pads(m: Merge, x: tuple[float, float], bounds: Sequence[float], dx: float, scale: float,
                width: float | None) -> tuple[float, float]:
    """(indentStart, indentEnd) of a flush cell spanning columns: its words where the PDF has them
    (`merge_x`, moved with the table by `dx`), as a column's are - with none, a full-width
    \\multicolumn note sat a padding left of the cells above (r2_tables_v2 slide 1) - but never
    so far that they no longer fit on one line (`width`, their Slides width)."""
    c0, c1 = m.col, m.col + m.cols
    left = max(0.0, (x[0] + dx - bounds[c0]) * scale - PAD_X) if m.align == "left" else 0.0
    right = max(0.0, (bounds[c1] - x[1] - dx) * scale - PAD_X) if m.align == "right" else 0.0
    if width is not None:
        spare = max(0.0, (bounds[c1] - bounds[c0]) * scale - 2 * TABLE_CELL_PAD - WRAP_MARGIN - width)
        left, right = min(left, spare), min(right, spare)
    return left, right


def _alignment(align: Align, rtl: bool) -> Literal["START", "CENTER", "END"]:
    """A cell's paragraph alignment: a cell that reads right to left starts at its right edge."""
    match align:
        case "left":
            return "END" if rtl else "START"
        case "center":
            return "CENTER"
        case "right":
            return "START" if rtl else "END"
        case _:
            assert_never(align)


def _moved(c: Column, dx: float, m: float) -> Column:
    """A column (and its body's extent, when its head is set apart) moved `dx` and then `m` PDF pt."""
    head = c.head
    return replace(c, x0=c.x0 + dx + m, x1=c.x1 + dx + m,
                   head=None if head is None else replace(head, body=(head.body[0] + dx + m, head.body[1] + dx + m)))


def table_requests(el: ObjectMap, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                   imported: bool, page_w: float) -> list[SlidesRequest]:
    """`table_requests_of` a table dict (sync's recreated tables, the tests). `page_w`: see
    `table_layout`."""
    return table_requests_of(table_of(el), slide_id, object_id, scale, fonts, imported, page_w)


def table_element_requests(el: TableElement, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                           imported: bool) -> list[SlidesRequest]:
    """The requests of a parsed table, on a deck SLIDE_W wide."""
    return table_requests_of(set_table(el), slide_id, object_id, scale, fonts, imported, SLIDE_W / scale)


def table_requests_of(t: SetTable, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                      imported: bool, page_w: float) -> list[SlidesRequest]:
    """A table filled in through the API. `imported`: the table (`object_id`) came with the .pptx,
    empty and with the cell margins `pptx_table` gave it; else it is made here by createTable."""
    lay = table_layout_of(t, scale, fonts, imported, page_w)
    dx = lay.dx  # (a centred table's columns moved with it: table_shift)
    # (and a squeezed column's words with their edge: squeezed_columns)
    cols = [_moved(c, dx, m) for c, m in zip(t.columns, lay.moved)]
    x, y, widths, heights, row_ratio = lay.x, lay.y, lay.widths, lay.heights, lay.ratios
    bounds, cell_width, first_run = lay.bounds, lay.cell_width, lay.first_run
    n_rows, n_cols = len(t.cells), len(cols)

    reqs: list[SlidesRequest] = [
        # (it lies where the .pptx put it, below what the slide's elements before it made)
        {"updatePageElementsZOrder": {"pageElementObjectIds": [object_id], "operation": "BRING_TO_FRONT"}}
    ] if imported else [
        {"createTable": {"objectId": object_id, "rows": n_rows, "columns": n_cols, "elementProperties": {
            "pageObjectId": slide_id,
            "size": {"width": emu(sum(widths)), "height": emu(sum(heights))},
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                          "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}}},
    ]
    reqs.append(
        # No grid: only the rules of the original are drawn.
        {"updateTableBorderProperties": {
            "objectId": object_id, "borderPosition": "ALL",
            "tableRange": {"location": {"rowIndex": 0, "columnIndex": 0}, "rowSpan": n_rows, "columnSpan": n_cols},
            "tableBorderProperties": {"tableBorderFill": {"solidFill": {"color": {"rgbColor": {}}, "alpha": 0}}},
            "fields": "tableBorderFill.solidFill.alpha"}})
    for i, w in enumerate(widths):
        reqs.append({"updateTableColumnProperties": {"objectId": object_id, "columnIndices": [i],
                                                     "tableColumnProperties": {"columnWidth": emu(w)},
                                                     "fields": "columnWidth"}})
    for i, h in enumerate(heights):
        reqs.append({"updateTableRowProperties": {"objectId": object_id, "rowIndices": [i],
                                                  "tableRowProperties": {"minRowHeight": emu(h)},
                                                  "fields": "minRowHeight"}})
    for rule in t.rules:
        reqs.append({"updateTableBorderProperties": {
            "objectId": object_id, "borderPosition": rule.position,
            "tableRange": {"location": {"rowIndex": rule.row, "columnIndex": 0}, "rowSpan": 1, "columnSpan": n_cols},
            "tableBorderProperties": {
                "tableBorderFill": {"solidFill": {"color": rgb_color(rule.color), "alpha": 1}},
                "weight": pt(round(max(0.5, rule.weight * scale), 2))},
            "fields": "tableBorderFill.solidFill.color,tableBorderFill.solidFill.alpha,weight"}})
    for b in t.borders:
        reqs.append({"updateTableBorderProperties": {
            "objectId": object_id, "borderPosition": b.position,
            "tableRange": {"location": {"rowIndex": b.row, "columnIndex": b.col}, "rowSpan": 1, "columnSpan": 1},
            "tableBorderProperties": {
                "tableBorderFill": {"solidFill": {"color": rgb_color(b.color), "alpha": 1}},
                "weight": pt(round(max(0.5, b.weight * scale), 2))},
            "fields": "tableBorderFill.solidFill.color,tableBorderFill.solidFill.alpha,weight"}})
    for f in t.fills:
        reqs.append({"updateTableCellProperties": {
            "objectId": object_id, "tableRange": {"location": {"rowIndex": f.row, "columnIndex": f.col},
                                                  "rowSpan": 1, "columnSpan": 1},
            "tableCellProperties": {"tableCellBackgroundFill": {"solidFill": {"color": rgb_color(f.color)}}},
            "fields": "tableCellBackgroundFill.solidFill.color"}})
    merged = {(m.row, m.col): m for m in t.merges}
    merge_x = {(m.row, m.col): x for m, x in zip(t.merges, t.merge_x)}
    justified = set(t.justified)  # (classify: a tabularx X column's cells)
    for m in merged.values():
        reqs.append({"mergeTableCells": {"objectId": object_id, "tableRange": {
            "location": {"rowIndex": m.row, "columnIndex": m.col}, "rowSpan": m.rows, "columnSpan": m.cols}}})
        if m.rows > 1:  # \multirow centres its text vertically
            reqs.append({"updateTableCellProperties": {
                "objectId": object_id, "tableRange": {"location": {"rowIndex": m.row, "columnIndex": m.col},
                                                      "rowSpan": m.rows, "columnSpan": m.cols},
                "tableCellProperties": {"contentAlignment": "MIDDLE"}, "fields": "contentAlignment"}})

    hidden = {(m.row + i, m.col + j) for m in merged.values()
              for i in range(m.rows) for j in range(m.cols)} - set(merged)
    # The indent puts the text where the PDF has it, but never so far that a cell's text no
    # longer fits on one line (a wrapped cell doubles its row): at most what the column's widest
    # cell leaves, the same for every cell of the column so that they stay aligned. Capped cell
    # by cell, a tight right-aligned column of signed numbers came out left-aligned. A width
    # nothing measured (a font the probe did not measure: Arial, Calibri) is the PDF's.
    spare: list[float | None] = []
    for c, col in enumerate(cols):
        # (a head set its own way leaves the indent to its body: classify `head`, `body`)
        ex0, ex1 = col.head.body if col.head is not None else (col.x0, col.x1)
        centred = col.centred or ()
        ws = [_known(cell_width.get((r, c)), (ex1 - ex0) * scale * lay.shrink)
              for r, row in enumerate(t.cells) if c < len(row) and row[c] and (r, c) not in merged
              and (r, c) not in hidden and not (r == 0 and col.head is not None) and r not in centred]
        spare.append(max(0.0, widths[c] - 2 * TABLE_CELL_PAD - WRAP_MARGIN - max(ws)) if ws else None)
    for r, row in enumerate(lay.cells):
        for c, runs in enumerate(row):
            text = "".join(run.text for run in runs).strip()
            loc: SlidesTableCellLocation = {"rowIndex": r, "columnIndex": c}
            if not text:
                if (r, c) in hidden or first_run is None:
                    continue
                # An empty cell still has a line of the default font, which would set the row's
                # minimum height: give it a space in the table's font, its row's size and line spacing.
                font, fields = fonts.style_of(first_run, scale)
                style = slides_text_style(font, "a table's font (FontMapper.style_of)")
                if "fontSize" in style:
                    style["fontSize"] = pt(round(lay.sizes[r], 2))
                space: list[SlidesRequest] = [
                    {"insertText": {"objectId": object_id, "cellLocation": loc, "text": " "}},
                    {"updateTextStyle": {"objectId": object_id, "cellLocation": loc, "textRange": {"type": "ALL"},
                                         "style": style, "fields": ",".join(fields)}},
                    {"updateParagraphStyle": {"objectId": object_id, "cellLocation": loc, "textRange": {"type": "ALL"},
                                              "style": {"lineSpacing": round(100 * row_ratio[r], 1),
                                                        "spaceAbove": pt(0), "spaceBelow": pt(0)},
                                              "fields": "lineSpacing,spaceAbove,spaceBelow"}},
                ]
                reqs += space
                continue
            reqs.append({"insertText": {"objectId": object_id, "cellLocation": loc, "text": text}})
            start = 0
            for run, z in zip(runs, run_sizes_of(runs, scale, fonts)):
                piece = run.text.strip() if len(runs) == 1 else run.text
                if start == 0:
                    piece = piece.lstrip()
                if not piece:
                    continue
                font, fields = fonts.style_of(run, scale)
                style = slides_text_style(font, "a cell run's font (FontMapper.style_of)")
                if "fontSize" in style:
                    style["fontSize"] = pt(z)  # (a subscript no larger than its text: run_sizes)
                style["smallCaps"] = run.smallcaps
                style["foregroundColor"] = text_color(run.color)
                style["baselineOffset"] = baseline_offset(run.script)
                reqs.append({"updateTextStyle": {
                    "objectId": object_id, "cellLocation": loc,  # (UTF-16 units: u16)
                    "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": min(u16(text), start + u16(piece))},
                    "style": style, "fields": ",".join(fields + ["smallCaps", "foregroundColor", "baselineOffset"])}})
                start += u16(piece)
            col = cols[c]
            centred = col.centred or ()
            # A head set otherwise than its column's body (classify `head`): the head row its own
            # way, the body to the body's own edges. A cell siunitx centres on the column (a dash
            # among numbers, classify `centred`) is set like a centred head.
            head = (r == 0 and col.head is not None) or r in centred
            align: Align = "center" if r in centred else col.head.align if head and col.head is not None else col.align
            x0, x1 = col.head.body if not head and col.head is not None else (col.x0, col.x1)
            # Line the text up with the original inside the (contiguous) Slides columns.
            left_pad = max(0.0, (x0 - bounds[c]) * scale - PAD_X) if align == "left" else 0.0
            right_pad = max(0.0, (bounds[c + 1] - x1) * scale - PAD_X) if align == "right" else 0.0
            here = merged.get((r, c))
            if here is not None and (here.cols > 1 or here.align == "center"):
                # (a \multirow or \makecell head centred over a flush column stays centred)
                align, left_pad, right_pad = here.align, 0.0, 0.0
                words = merge_x.get((r, c))
                if here.cols > 1 and align != "center" and words is not None:
                    left_pad, right_pad = merged_pads(here, words, bounds, dx + lay.moved[c], scale,
                                                      cell_width.get((r, c)))
            room_c = spare[c]
            if room_c is not None and here is None and not head:  # (the column's: see `spare` above)
                left_pad, right_pad = min(left_pad, room_c), min(right_pad, room_c)
            held = lay.held[c]
            if held is not None and align == "left" and here is None and not head:
                # A wrapped cell's column held under its cap (table_columns): the text room ends
                # there, the rule after the column stays on the PDF's boundary.
                text_x0 = bounds[c] * scale + TABLE_CELL_PAD + left_pad
                right_pad = max(right_pad, (bounds[c + 1] * scale - TABLE_CELL_PAD) - (text_x0 + held * scale))
            justify = (r, c) in justified and align == "left" and here is None and not head
            if justify:
                # A justified cell's lines end where the PDF's do: its text room is the PDF's
                # column, never less than any of the column's justified cells needs in as many
                # lines (wrap_window) - one edge for all of them.
                text_x0 = bounds[c] * scale + TABLE_CELL_PAD + left_pad
                room = bounds[c + 1] * scale - TABLE_CELL_PAD - right_pad - text_x0
                want = max([(x1 - x0) * scale * lay.shrink] + [w + WRAP_MARGIN for rc in justified if rc[1] == c
                                                               for w in [cell_width.get(rc)] if w is not None])
                right_pad += max(0.0, room - want)
            # A cell that reads right to left starts at its right edge, so its alignment and
            # its two indents are mirrored (the text element's rule, one cell wide).
            rtl = bidi.reads_rtl(text)
            indent_start, indent_end = (right_pad, left_pad) if rtl else (left_pad, right_pad)
            paragraph: SlidesParagraphStyle = {
                "alignment": "JUSTIFIED" if justify and not rtl else _alignment(align, rtl),
                "lineSpacing": round(100 * row_ratio[r], 1), "spaceAbove": pt(0),
                "spaceBelow": pt(0), "indentStart": pt(round(indent_start, 2)),
                "indentFirstLine": pt(round(indent_start, 2)), "indentEnd": pt(round(indent_end, 2))}
            if rtl:
                paragraph["direction"] = "RIGHT_TO_LEFT"
            reqs.append({"updateParagraphStyle": {
                "objectId": object_id, "cellLocation": loc, "textRange": {"type": "ALL"},
                "style": paragraph,
                "fields": "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine,indentEnd" +
                          (",direction" if rtl else "")}})
    return reqs


def _known(width: float | None, otherwise: float) -> float:
    return width if width is not None else otherwise
