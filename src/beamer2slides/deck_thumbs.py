"""What a foreign deck's slide thumbnails say that the Slides API does not (`deck_ir(foreign=True,
thumbnails=...)`): table rows as tall as Slides draws them and cell insets, text insets from where a box's
first ink starts (top, side, baseline), PowerPoint's insets for a deck imported from a .pptx, the width a
stand-in font must be set at, and whether an unsure weight is drawn bold. Each reader measures the
LARGE thumbnail (`thumb`, `px` pixels per Slides pt) and hands back the elements with what it found:
a changed element is a new record (`dataclasses.replace`), the list a new list in the same order, so
an element met again in it is the same object. Nothing here calls Google. Measurements and history:
docs/adopt-bench.md."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from typing import Union

import numpy as np

from .arrays import Floats, Floats32, Mask, SignedRGB
from .deck_fills import PixelBox, px_box, rgb
from .deck_ir_types import (SlidesMeasures, TableBorder, TargetElement, TargetImage, TargetParagraph, TargetShape,
                            TargetTable, TargetText, TextBox, element_json)
from .emit import BASELINE_A, PAD_X
from .ir_types import Box

Glyph = tuple[float, Union[float, None], Union[float, None], Union[float, None], Union[float, None]]
"""A glyph's (advance, xMin, yMin, xMax, yMax) in em; the bounds None when it has no outline."""
Glyphs = Callable[[str], Union[Glyph, None]]
"""A face's glyph metrics by character (None: the face has no glyph for it)."""
Placed = tuple[str, float, Glyph]
"""A character of a line with its size and its glyph's metrics (`_lines`)."""


def text_box(e: TargetElement) -> TextBox | None:
    """A text element's Slides box, or None (not text, a WordArt's frame, no box)."""
    return e.box if isinstance(e, TargetText) and isinstance(e.box, TextBox) else None


def _slides(p: TargetParagraph) -> SlidesMeasures | None:
    return p.slides


def _indent_first(p: TargetParagraph) -> float:
    sl = _slides(p)
    return (sl.indent_first if sl else 0) or 0


def _indent_start(p: TargetParagraph) -> float:
    sl = _slides(p)
    return (sl.indent_start if sl else 0) or 0


def _line_spacing(p: TargetParagraph) -> float:
    sl = _slides(p)
    return (sl.line_spacing if sl else None) or 1.0


def para_size(p: TargetParagraph) -> float:
    """`adopt.para_size` of a paragraph record: its biggest run, 10 pt when it has none."""
    return max((r.size or 10.0) for r in p.runs) if p.runs else 10.0


ROW_LINE_SHARE = 0.5     # a row boundary is read when its borders cover this share of the table's width
ROW_LINE_HIT = 0.85      # ... and this share of the columns sampled along them shows the line


def thumbnail_rows(elements: Sequence[TargetElement], thumb: SignedRGB | None, px: float) -> list[TargetElement]:
    """Table rows as tall as the thumbnail draws them (`row_heights`), and held there (`rows_fixed`).

    A stored row height is a minimum: Slides grows a row until its tallest cell fits, and what fits
    depends on things the API does not say - the insets a .pptx brought, the line an empty cell still
    holds in a size nobody can read back (solidity-survey's empty cells grow their 15.75 pt rows to
    19.4, creandum-board's leave theirs alone), where a word too wide for its cell is broken. TeX's
    guess at all of that set comps-analysis' header 11.6 pt short and cs161-tls' rows up to 19 pt
    tall, moving every row under them. Where the thumbnail shows a row's top and bottom borders, the
    row is as tall as they are apart: each boundary whose visible borders span at least
    `ROW_LINE_SHARE` of the table is looked for from its stored place down, as the first pixel row
    where `ROW_LINE_HIT` of the columns along those borders turn towards the border colour, and a row
    between two found boundaries gets their distance (never less than stored) and no growth in TeX.
    A boundary not found (no border, or one the colour of what it separates) ends the measuring: the
    rows under it could have grown by any amount."""
    out = list(elements)
    if thumb is None or not px:
        return out
    H, W = thumb.shape[:2]
    for k, e in enumerate(out):
        if not isinstance(e, TargetTable):
            continue
        heights, widths = e.row_heights, e.col_widths
        if not heights or not widths:
            continue
        xs = [e.bbox[0]]
        for w in widths:
            xs.append(xs[-1] + w)
        by_row: dict[int, list[TableBorder]] = {}
        for b in e.table_borders or ():
            if b.dir == "h" and b.col < len(widths) and b.alpha >= 0.5 and b.color:
                by_row.setdefault(b.row, []).append(b)
        new: list[float] = list(heights)
        fixed: list[int] = []
        prev = e.bbox[1]
        for j in range(1, len(heights) + 1):
            segs = by_row.get(j, [])
            if sum(widths[b.col] for b in segs) < ROW_LINE_SHARE * (xs[-1] - xs[0]):
                break
            y = _find_row_line(thumb, px, segs, xs, prev + heights[j - 1], heights[j - 1], H, W)
            if y is None:
                break
            if y - prev > heights[j - 1] + 0.4:
                new[j - 1] = round(y - prev, 3)
            fixed.append(j - 1)
            prev = y
        if fixed:
            out[k] = replace(e, row_heights=tuple(new), rows_fixed=tuple(fixed))
    return out


CELL_BEARING_EM = 0.06   # a first or last glyph's side bearing, in em, where a cell's words' ink begins
CELL_PAD_MIN = 3         # cells whose ink edge reads the side inset, at least


def _edges(start: float, spans: Sequence[float]) -> list[float]:
    """Where each of `spans` starts, laid end to end from `start`, and where the last one ends."""
    out = [start]
    for v in spans:
        out.append(out[-1] + v)
    return out


def _known_rows(e: TargetTable, heights: Sequence[float]) -> int:
    """How many rows from the top `thumbnail_rows` measured, one after another."""
    fixed = set(e.rows_fixed or ())
    known = 0
    while known < len(heights) and known in fixed:
        known += 1
    return known


def thumbnail_cell_pad(elements: Sequence[TargetElement], thumb: SignedRGB | None, px: float) -> list[TargetElement]:
    """A table's side inset as its thumbnail shows it (`cell_pad[0]`).

    `cell_pad` guesses it from the vertical inset, and a .pptx brings its own: comps-analysis' words
    start 3 pt from their cells' left edges where the guess put them 5.8 in, wrapping "Implied Equity
    Value" after "Implied"; hebrew-lesson's right-aligned lines end 7.2 pt from the right edge, where
    the guess (4.8) ended them. A cell whose paragraphs are all left-aligned (right-aligned) gives the
    distance from its left (right) edge to its first (last) ink column, less a glyph's side bearing;
    the median of at least `CELL_PAD_MIN` such cells is the inset. Only rows whose top is known are
    read: those `thumbnail_rows` measured and the one under them, whose stored height it fills at
    least."""
    out = list(elements)
    if thumb is None or not px:
        return out
    H, W = thumb.shape[:2]
    for k, e in enumerate(out):
        if not isinstance(e, TargetTable):
            continue
        heights, widths, pad = e.row_heights, e.col_widths, e.cell_pad
        if not heights or not widths or not pad:
            continue
        known = _known_rows(e, heights)
        tops = _edges(e.bbox[1], heights)
        xs = _edges(e.bbox[0], widths)
        found: list[float] = []
        for c in e.table_cells or ():
            paras = [p for p in c.paragraphs if p.runs and "".join(r.text for r in p.runs).strip()]
            if not paras or c.rowspan != 1 or c.colspan != 1 or c.row > known:
                continue
            aligns = {p.align for p in paras}
            if aligns not in ({"left"}, {"right"}) or any(p.bullet or p.level for p in paras):
                continue
            side = aligns.pop()
            text = "".join(r.text for r in paras[0].runs)
            if side == "left" and text[:1].isspace() or side == "right" and text.rstrip("\n")[-1:].isspace():
                continue                    # words pushed in by spaces say nothing of the inset
            m = 1.5
            X0, X1 = int(np.ceil((xs[c.col] + m) * px)), int(np.floor((xs[c.col + 1] - m) * px))
            Y0, Y1 = int(np.ceil((tops[c.row] + m) * px)), int(np.floor((tops[c.row + 1] - m) * px))
            if X1 - X0 < 6 or Y1 - Y0 < 4 or X1 > W or Y1 > H:
                continue
            crop = thumb[Y0:Y1, X0:X1]
            ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
            ink = ((np.abs(crop - ground).max(axis=-1) > 80).sum(axis=0) >= 2)
            cols = np.nonzero(ink)[0]
            if len(cols) < 3:
                continue
            size = max((r.size or 0) for p in paras for r in p.runs)
            if side == "left":
                gap = X0 / px + int(cols[0]) / px - xs[c.col]
            else:
                gap = xs[c.col + 1] - (X0 + int(cols[-1]) + 1) / px
            found.append(gap - CELL_BEARING_EM * size)
        if len(found) >= CELL_PAD_MIN:
            out[k] = replace(e, cell_pad=(round(max(0.0, float(np.median(found))), 3), pad[1]))
    return out


CELL_LINE_MIN_EM = 0.3   # a band of inked rows this tall (em) at least is a line, not a dot or an accent


def thumbnail_cell_text(elements: Sequence[TargetElement], thumb: SignedRGB | None, px: float) -> list[TargetElement]:
    """Where a table's thumbnail shows its cells' text, as the inset of a Slides line box
    (`cell_text_y`, IR pt): a top-aligned cell's first baseline stands that far plus the line's ascent
    (`emit.ASCENT_EM`) under its row's top, a bottom-aligned cell's last baseline that far plus the
    line's descent over its row's bottom.

    The vertical inset `cell_pad` guesses sizes rows but not the text in them: the cells are set with
    LaTeX's strut, 0.84 em over the baseline where Slides' line box has 0.968, and the guess itself
    is off - hebrew-lesson's cells (inset guessed 1.8 pt) show their baselines 1.45 pt + 0.968 em
    under the row's top on every slide, comps-analysis' (guessed 0.7 and 1.6 in different tables)
    1.0 pt + 0.968 em, top- and bottom-aligned alike. So each cell whose one paragraph is aligned to
    its row's top or bottom, in rows whose top (bottom) `thumbnail_rows` found, gives that inset from
    its first (last) line's baseline - the lowest row inked a quarter as densely as the line's
    densest, as `baseline_drift` reads it, a line being a band of inked rows at least
    `CELL_LINE_MIN_EM` tall - and the median of at least `CELL_PAD_MIN` of them is the table's."""
    out = list(elements)
    if thumb is None or not px:
        return out
    from .emit import ASCENT_EM, LINE_EM
    H, W = thumb.shape[:2]
    for k, e in enumerate(out):
        if not isinstance(e, TargetTable):
            continue
        heights, widths = e.row_heights, e.col_widths
        if not heights or not widths:
            continue
        known = _known_rows(e, heights)
        tops = _edges(e.bbox[1], heights)
        xs = _edges(e.bbox[0], widths)
        found: list[float] = []
        for c in e.table_cells or ():
            va = c.valign
            paras = [p for p in c.paragraphs if p.runs and "".join(r.text for r in p.runs).strip()]
            if va not in ("top", "bottom") or len(paras) != 1 or c.rowspan != 1 or c.row >= len(heights):
                continue
            if c.row > known or (va == "bottom" and c.row >= known):
                continue                    # the row's top (bottom) is not where the thumbnail has it
            if any(p.bullet for p in paras) or any(r.highlight for p in paras for r in p.runs):
                continue
            z = max((r.size or 0) for r in paras[0].runs)
            spacing = paras[0].line_spacing or 1.0
            if z <= 0:
                continue
            m = 1.0
            X0 = int(np.ceil((xs[c.col] + m) * px))
            X1 = int(np.floor((xs[min(c.col + c.colspan, len(widths))] - m) * px))
            Y0, Y1 = int(np.ceil((tops[c.row] + m) * px)), int(np.floor((tops[c.row + 1] - m) * px))
            if X1 - X0 < 6 or Y1 - Y0 < 4 or X1 > W or Y1 > H or X0 < 0 or Y0 < 0:
                continue
            crop = thumb[Y0:Y1, X0:X1]
            ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
            ink = np.abs(crop - ground).max(axis=-1) > 80
            cols = np.nonzero(ink.any(axis=0))[0]
            if len(cols) < 6:
                continue
            ink[ink[:, cols[0]:cols[-1] + 1].mean(axis=1) > 0.7] = False      # underlines and strikes
            inked = np.nonzero(ink.any(axis=1))[0]
            if not len(inked):
                continue
            # bands of inked rows, split where a row has none: the lines
            cuts = np.nonzero(np.diff(inked) > 1)[0]
            bands = [b for b in np.split(inked, cuts + 1) if len(b) >= CELL_LINE_MIN_EM * z * px]
            if not bands:
                continue
            band = bands[0] if va == "top" else bands[-1]
            if band[0] == 0 or band[-1] == ink.shape[0] - 1:
                continue                    # the line runs on out of what is read
            density = ink[band[0]:band[-1] + 1].mean(axis=1)
            rows = np.nonzero(density >= 0.25 * density.max())[0]
            base = (Y0 + int(band[0]) + int(rows[-1]) + 1) / px
            above = ASCENT_EM * z - (0 if spacing >= 1 else (1 - spacing) * 0.75 * LINE_EM * z)
            below = (LINE_EM - ASCENT_EM) * z + (spacing - 1) * LINE_EM * z if spacing >= 1 \
                else (LINE_EM - ASCENT_EM) * z - (1 - spacing) * 0.25 * LINE_EM * z
            found.append(base - above - tops[c.row] if va == "top" else tops[c.row + 1] - base - below)
        if len(found) >= CELL_PAD_MIN:
            out[k] = replace(e, cell_text_y=round(float(np.median(found)), 3))
    return out


def _find_row_line(thumb: SignedRGB, px: float, segs: Sequence[TableBorder], xs: Sequence[float], expected: float,
                   stored: float, H: int, W: int) -> float | None:
    """Where a row boundary's border runs in the thumbnail, in IR pt (see `thumbnail_rows`)."""
    cols: list[np.ndarray[tuple[int], np.dtype[np.int64]]] = []
    colours: list[SignedRGB] = []
    for b in segs:
        a0, a1 = int(np.ceil(xs[b.col] * px)) + 3, int(np.floor(xs[b.col + 1] * px)) - 3
        colour = rgb(b.color)
        if a1 > a0 and colour is not None:
            cols.append(np.arange(max(0, a0), min(W, a1)))
            colours.append(np.repeat(colour[None, :], max(0, min(W, a1) - max(0, a0)), axis=0))
    if not cols:
        return None
    cols_ = np.concatenate(cols)
    want = np.concatenate(colours).astype(float)
    if len(cols_) < 6:
        return None
    thick = max(int(np.ceil(max(b.weight for b in segs) * px)) + 2, 3)
    y_from = max(thick + 2, int(np.floor((expected - 1.0) * px)))
    y_to = min(H - thick - 3, int(np.ceil((expected + 3 * stored + 20) * px)))

    def dist(y: int) -> Floats:
        return np.abs(thumb[y, cols_].astype(float) - want).sum(axis=1)

    for y in range(y_from, y_to):
        above = dist(y - 2 - thick // 2)
        here = dist(y)
        if ((here < 0.5 * above) & (above > 45)).sum() < ROW_LINE_HIT * len(cols_):
            continue
        # the line ends within its own thickness, where what lies under it shows again: the step
        # from one fill to a paler one (hebrew-lesson's brown header over its pink rows, white
        # borders) is no line
        for run in range(y, y + thick):
            below = dist(run + 3)
            mid = dist((y + run) // 2)
            ok = (mid < 0.5 * np.minimum(above, below)) & (np.minimum(above, below) > 45)
            if ok.sum() >= ROW_LINE_HIT * len(cols_):
                return (y + run + 1) / 2 / px
        return None
    return None


def text_rows(e: TargetText, paras: Sequence[TargetParagraph], page_h: float) -> tuple[float, float]:
    """The page rows (top, bottom, page pt) a text box's words can stand on: its box, or, when its
    paragraphs need more height than it has even unwrapped (1.19 em x line spacing each), as far past
    it as its alignment lets them overflow - down from the top, both ways from the middle, up from
    the bottom. gdg24's code listings (15 lines of 9.45 pt in a 63 pt box, middle-aligned) show only
    their indented middle lines inside the box; the lines that start at its edge stand above and below."""
    y0, y1 = e.bbox[1], e.bbox[3]
    need = sum(1.19 * max((r.size or p.size or 0) for r in p.runs) * _line_spacing(p) for p in paras)
    over = need - (y1 - y0)
    if over <= 0:
        return y0, y1
    box_ = text_box(e)
    valign = "top" if box_ is None else box_.valign
    up = over if valign == "bottom" else over / 2 if valign == "middle" else 0.0
    return max(0.0, y0 - up), min(page_h, y1 + over - up)


def thumbnail_insets(elements: Sequence[TargetElement], thumb: SignedRGB | None, px: float) -> list[TargetElement]:
    """Text boxes the slide's own thumbnail shows with no insets (`box.insets` = 0, anchor moved).

    `zero_insets` needs a box that resizes to fit its text; a fixed box a template made with its
    insets at 0 (gdg24's stat grids, devfest2020's cards) says nothing of it to the API, and adopt set
    its words 6.7 pt right and 6.5 pt low, wrapping them elsewhere. Its thumbnail does say: in a
    left-aligned box, the words' ink starts at the box edge plus the paragraph's indent plus the left
    inset, and a first glyph's side bearing is ~1 pt where the inset is PAD_X. The rows of the left
    strip that anything else crosses are not read (a picture, a shape's edge or another box's words
    there are ink too), ink in the box's first column counts only when nothing lies just outside the
    box, and only an ink edge closer than half the inset counts: a big glyph's bearing can only keep
    the default."""
    out = list(elements)
    if thumb is None or not px:
        return out
    for k, e in enumerate(out):
        box_ = text_box(e)
        if not isinstance(e, TargetText) or box_ is None or box_.insets is not None:
            continue
        got = _insets_read(e, box_, out, thumb, px)
        if got is not None:
            out[k] = got
    return out


def _insets_read(e: TargetText, box_: TextBox, elements: Sequence[TargetElement], thumb: SignedRGB,
                 px: float) -> TargetText | None:
    """`thumbnail_insets` of one box: the box with no insets, or None where it keeps them."""
    paras = [p for p in e.paragraphs if p.runs]
    if not paras or not any(r.text.strip() for p in paras for r in p.runs):
        return None
    if any(p.align != "left" or p.bullet or p.direction == "rtl" for p in paras):
        # centred or bulleted words have no side edge to read, but their rows still show where the
        # top (bottom) inset put them
        if inset_rows(e, box_, elements, paras, thumb, px) == 0:
            scale = box_.scale or 1.0
            align = paras[0].align
            dx = {"left": 1.0, "right": -1.0}.get(align, 0.0) * PAD_X / scale
            return _no_insets(e, box_, dx, BASELINE_A / scale * _anchor_sign(box_))
        return None
    scale = box_.scale or 1.0
    pad = PAD_X / scale
    x0, y0, x1, y1 = e.bbox
    y0, y1 = text_rows(e, paras, thumb.shape[0] / px)
    indent = min(min(_indent_first(p), _indent_start(p)) for p in paras) / scale
    strip = (x0 - 1, y0, x0 + indent + pad + 2, y1)
    others = crossing(e, elements, strip, False)
    X0, X1 = round(x0 * px), round(x1 * px)
    Y0, Y1 = round(y0 * px), round(y1 * px)
    crop = thumb[max(0, Y0):max(0, Y1), max(0, X0):max(0, X1)]
    if crop.shape[0] < 4 or crop.shape[1] < 8:
        return None
    # What else reaches into the strip covers rows, not the box: gdg24's stat grids stack a heading
    # box over a caption box whose tops overlap by 5 pt, and its code slides lay a highlight bar
    # across the middle of the listing - none of those boxes was read. Their rows are left out; the
    # rest still shows where this box's words start - and when that leaves the first line out, the
    # top test below cannot pass.
    free = np.ones(crop.shape[0], dtype=bool)
    for o in others:
        a = max(0, int(np.floor((o.bbox[1] - 1) * px)) - max(0, Y0))
        b = max(0, int(np.ceil((o.bbox[3] + 1) * px)) - max(0, Y0))
        free[a:b] = False
    if free.sum() < 4:
        return None
    ground = np.median(crop[free].reshape(-1, crop.shape[-1]), axis=0)
    mark = (np.abs(crop - ground).max(axis=-1) > 80) & free[:, None]
    ink = mark.sum(axis=0) >= 2
    cols = np.nonzero(ink)[0]
    if len(cols) < 3:
        return None
    if cols[0] == 0:
        # Ink in the box's first pixel column is a glyph whose edge rounds onto the box edge (gdg24's
        # "Connect", 0.13 pt in) unless it goes on outside the box, where no word of it can be.
        if X0 < 3:
            return None
        left = thumb[max(0, Y0):max(0, Y1), X0 - 3:X0]
        if ((np.abs(left - ground).max(axis=-1) > 80) & free[:len(left), None]).any():
            return None
    # (the crop starts at the slide's edge when the box starts left of it: ap-bio-stats' full-width
    # boxes stand 1.9 pt off the slide)
    gap = (max(0, X0) + int(cols[0])) / px - (x0 + indent)
    # The first glyph's own bearing, where the deck's font is at hand: a big one alone can be
    # more than half the inset (devfest2020's 65 pt "Use over" starts 4.5 pt in with no inset)
    bearing = starting_bearing(paras)
    if gap >= pad / 2 + bearing:
        return None
    dy = 0.0
    if box_.valign in ("top", "bottom"):
        # a box may have no side insets and still its top one (creandum-board's labels): the
        # first line's tops must stand where no top inset puts them too
        told = inset_rows(e, box_, elements, paras, thumb, px)
        if told is None and box_.valign == "top":
            dy = BASELINE_A / scale
            rows = np.nonzero(mark.sum(axis=1) >= 2)[0]
            z = max(r.size or 0 for r in paras[0].runs)
            if not len(rows) or (Y0 + int(rows[0])) / px - (e.anchor[1] - CAP_EM * z) > -dy / 2:
                return None
        elif told is not None:
            if told != 0:
                return None
            dy = BASELINE_A / scale * _anchor_sign(box_)
    return _no_insets(e, box_, pad, dy)


def _anchor_sign(box_: TextBox) -> float:
    """Which way a box's first baseline moves when its insets go: up in a top-aligned box, down in a
    bottom-aligned one, nowhere in a middle-aligned one."""
    return {"top": 1.0, "bottom": -1.0}.get(box_.valign, 0.0)


def _no_insets(e: TargetText, box_: TextBox, dx: float, dy: float) -> TargetText:
    pad = PAD_X / (box_.scale or 1.0)
    return replace(e, box=replace(box_, insets=0), wrap_width=round(e.wrap_width + 2 * pad, 2),
                   anchor=(round(e.anchor[0] - dx, 2), round(e.anchor[1] - dy, 2)))


# ------------------------------------------------------------------------------------ glyph metrics

_FACES: dict[tuple[str, bool, bool], Glyphs | None] = {}


def face_glyphs(font: str, bold: bool, italic: bool) -> Glyphs | None:
    """A function char -> (advance, xMin, yMin, xMax, yMax) in em (None: no glyph, or no outline) for
    a deck font found under its own name (on the machine or fetched, `adopt.font_family`), or None:
    a stand-in's outlines say nothing of where Slides' glyphs stand. Cached per face."""
    if not font:
        return None
    from .adopt import flatten
    key = (flatten(font), bold, italic)
    if key in _FACES:
        return _FACES[key]
    _FACES[key] = None
    try:
        from fontTools.pens.boundsPen import BoundsPen
        from fontTools.ttLib import TTFont
        from .adopt import font_family
        from .deck_ir import family_of
        files = font_family(font, family_of(font))
        if not files:
            return None
        have = flatten(str(files.get("match") or ""))
        if not (have.startswith(key[0]) or key[0].startswith(have)) or files.get("FontIndex"):
            return None
        style = ("BoldItalicFont" if italic else "BoldFont") if bold else ("ItalicFont" if italic else "UprightFont")
        f = TTFont(files.get(style) or files["UprightFont"], lazy=True)
        cmap, glyphs, hmtx, upem = f.getBestCmap(), f.getGlyphSet(), f["hmtx"], f["head"].unitsPerEm
    except Exception:                                   # noqa: BLE001 - no metrics is an answer
        return None
    if cmap is None:
        return None                                     # no Unicode cmap: no glyph is found by its character
    seen: dict[str, Glyph | None] = {}

    def glyph(c: str) -> Glyph | None:
        if c not in seen:
            name = cmap.get(ord(c))
            if name is None:
                seen[c] = None
            else:
                pen = BoundsPen(glyphs)
                glyphs[name].draw(pen)
                b = pen.bounds
                adv: float = hmtx[name][0] / upem
                seen[c] = (adv, b[0] / upem, b[1] / upem, b[2] / upem, b[3] / upem) if b \
                    else (adv, None, None, None, None)
        return seen[c]
    _FACES[key] = glyph
    return glyph


def _chars(p: TargetParagraph) -> Iterator[tuple[str, float, Glyphs | None]]:
    """(char, size, glyph metrics function) of a paragraph's text, in order."""
    for r in p.runs:
        g = face_glyphs(r.font, r.bold, r.italic)
        for c in r.text:
            yield c, r.size or 0.0, g


def starting_bearing(paras: Sequence[TargetParagraph]) -> float:
    """The smallest left side bearing (page pt) of the paragraphs' first glyphs, 0 when unknown."""
    out: float | None = None
    for p in paras:
        for c, z, g in _chars(p):
            if c.isspace():
                continue
            m = g(c) if g else None
            if m is None or m[1] is None:
                return 0.0
            out = m[1] * z if out is None else min(out, m[1] * z)
            break
    return max(0.0, out or 0.0)


def _lines(p: TargetParagraph, width: float) -> list[list[Placed]] | None:
    """A paragraph broken into lines greedily at spaces within `width` (page pt), each line its
    (char, size, metrics) - None when a glyph's metrics are unknown."""
    lines: list[list[Placed]] = []
    line: list[Placed] = []
    x, last_space = 0.0, None
    for c, z, g in _chars(p):
        if c in "\x0b\n":
            lines.append(line)
            line = []
            x, last_space = 0.0, None
            continue
        m = g(c) if g else None
        if m is None:
            return None
        if c == " ":
            last_space = len(line)
        line.append((c, z, m))
        x += m[0] * z
        if x > width and last_space is not None and c != " ":
            lines.append(line[:last_space])
            line = line[last_space + 1:]
            x = sum(q[2][0] * q[1] for q in line)
            last_space = None
    lines.append(line)
    return lines


INSET_TELL = 0.35           # of the inset: how near its ink edge must be to the no-inset prediction


def inset_rows(e: TargetText, box_: TextBox, elements: Sequence[TargetElement], paras: Sequence[TargetParagraph],
               thumb: SignedRGB, px: float) -> int | None:
    """What the thumbnail's rows say of a top- or bottom-aligned box's top (bottom) inset: 0 when its
    first line's ink tops (last line's ink bottoms) stand where no inset puts them, 1 where Slides'
    own inset does, None when it cannot tell (no metrics for the deck's own font, other things in the
    box, a middle-aligned box, a line a stand-in would break elsewhere). The glyphs' own heights say
    where the ink stands (a centred title has no side edge to read, and "Colors" in Space Mono does
    not reach the cap height of a generic face)."""
    from .adopt import WIDE_SPACING, line_box, snapped_line_box
    valign = box_.valign
    if valign not in ("top", "bottom") or box_.font_scale != 1:
        return None
    scale = box_.scale or 1.0
    x0, y0, x1, y1 = e.bbox
    width = max(1.0, x1 - x0 - 2 * PAD_X / scale)
    p = paras[0] if valign == "top" else paras[-1]
    if any((r.script or r.highlight or r.underline or r.strike) for r in p.runs):
        return None
    # Arabic and Hebrew are shaped: a letter's joined form is not the glyph its code point maps to, so
    # the cmap's heights say nothing of the line's tops (arabic-training's lists read as inset-free)
    if p.direction == "rtl" or any(unicodedata.bidirectional(c) in ("R", "AL") for r in p.runs for c in r.text):
        return None
    lines = _lines(p, width - (_indent_start(p) / scale))
    if not lines:
        return None
    line = [q for q in (lines[0] if valign == "top" else lines[-1]) if q[2][2] is not None]
    if not line:
        return None
    inset = BASELINE_A / scale
    # the first (last) baseline where adopt.text_box_latex sets it under Slides' own insets
    sl = _slides(p)
    z, r = para_size(p), _line_spacing(p)
    if valign == "top":
        above = snapped_line_box(z, r, scale, bool(box_.snap))[0]
        space = 0.0 if box_.grows else ((sl.space_above if sl else 0) or 0) / scale
        base = y0 + inset + space + above
        edge = min(base - _bound(q[2][4]) * q[1] for q in line)
    else:
        below = line_box(z, 1.0 if r >= WIDE_SPACING else r)[1]
        base = y1 - inset - below
        edge = max(base - _bound(q[2][2]) * q[1] for q in line)
    # the rows the box's text covers, less rows other elements reach into
    m = 0.5 * inset
    top, bottom = (edge - inset - m, edge + m) if valign == "top" else (edge - m, edge + inset + m)
    # sideways, only where the line's words stand, with or without the side insets: devfest2020's
    # lists run 2 pt past the panel they stand on, which would count as crossing them
    w = sum(q[2][0] * q[1] for q in line)
    pad = PAD_X / scale
    indent = max(_indent_start(p), _indent_first(p)) / scale
    align = p.align
    band: Box
    if align == "center":
        mid = (x0 + x1) / 2 + (indent - ((sl.indent_end if sl else 0) or 0) / scale) / 2
        band = (mid - w / 2 - pad, top, mid + w / 2 + pad, bottom)
    elif align == "right":
        band = (x1 - pad - w - pad, top, x1, bottom)
    else:
        band = (x0, top, x0 + pad + indent + w + pad, bottom)
    band = (max(x0, band[0]), top, min(x1, band[2]), bottom)
    x0, x1 = band[0], band[2]
    if crossed(e, elements, band):
        return None
    X0, X1 = round(max(0.0, x0) * px), round(min(x1, thumb.shape[1] / px) * px)
    Y0, Y1 = round(top * px), round(bottom * px)
    if Y0 < 0 or Y1 > thumb.shape[0] or X1 - X0 < 4:
        return None
    crop = thumb[Y0:Y1, X0:X1]
    ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
    rows = np.nonzero((np.abs(crop - ground).max(axis=-1) > 80).sum(axis=1) >= 2)[0]
    if not len(rows):
        return None
    seen = (Y0 + int(rows[0])) / px if valign == "top" else (Y0 + int(rows[-1]) + 1) / px
    shifted = edge - inset if valign == "top" else edge + inset
    if abs(seen - shifted) <= INSET_TELL * inset:
        return 0
    if abs(seen - edge) <= INSET_TELL * inset:
        return 1
    return None


def _bound(v: float | None) -> float:
    """A glyph bound `inset_rows` kept only where the glyph has one (its line is filtered on yMin)."""
    if v is None:
        raise ValueError("a glyph with no outline has no bounds")
    return v


def ink_widths(elements: Sequence[TargetElement], thumb: SignedRGB | None, px: float) -> list[TargetElement]:
    """How wide the thumbnail shows the first line of each text box's first paragraph (`ink_width`,
    page pt, first ink column to last): `adopt.font_widths` holds them against the stand-in a deck's
    font is set in when this machine does not have it, for the paragraphs that fit on one line.
    Only a paragraph in one font, size and style, flush with its own reading direction's start (left
    for a left-to-right paragraph, right for a right-to-left one - the pixel scan itself does not
    care which edge the words sit against), with no bullet, whose line nothing else crosses and whose
    words do not touch the box's sides."""
    out = list(elements)
    if thumb is None or not px:
        return out
    for k, e in enumerate(out):
        if not isinstance(e, TargetText) or text_box(e) is None:
            continue
        paras = [p for p in e.paragraphs if p.runs]
        if not paras:
            continue
        p = paras[0]
        runs = [r for r in p.runs if r.text.strip()]
        text = "".join(r.text for r in p.runs)
        # a right-to-left paragraph's own "flush start" is written align=right (deck_ir mirrors
        # START/END for it): that is its equivalent of a left-to-right paragraph's align=left, not
        # something to exclude - the column scan below reads pixels, blind to reading direction.
        start_align = "right" if p.direction == "rtl" else "left"
        if not runs or p.bullet or p.align != start_align \
                or any(c in text for c in "\x0b\n\t") or len(text.strip()) < 4 \
                or len({(r.font, r.bold, r.italic, r.size, r.script) for r in runs}) != 1:
            continue
        x0, y0, x1, y1 = e.bbox
        z, base = runs[0].size or 0, e.anchor[1]
        band = (x0, base - 0.85 * z, x1, base + 0.3 * z)
        if z <= 0 or crossed(e, out, band):
            continue
        X0, X1, Y0, Y1 = (round(v * px) for v in (x0, x1, band[1], band[3]))
        if X0 < 0 or Y0 < 0 or X1 > thumb.shape[1] or Y1 > thumb.shape[0] or Y1 - Y0 < 3 or X1 - X0 < 8:
            continue
        crop = thumb[Y0:Y1, X0:X1]
        ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
        cols = np.nonzero((np.abs(crop - ground).max(axis=-1) > 80).any(axis=0))[0]
        if len(cols) < 3 or cols[0] <= 1 or cols[-1] >= crop.shape[1] - 2:
            continue
        out[k] = replace(e, ink_width=round((int(cols[-1]) - int(cols[0]) + 1) / px, 2))
    return out


BOLD_STROKE_EM = 0.10       # mean stroke width (em) above which a thumbnail's letters are bold


def stroke_em(e: TargetText, elements: Sequence[TargetElement], thumb: SignedRGB | None, px: float) -> float | None:
    """The mean stroke width of a text box's letters in its thumbnail, in em of its biggest run (None:
    not readable: anything else reaches into the box, or too little ink). Twice the ink's area over
    its outline: a stroke w wide and L long has area wL and an outline of 2L. Antialiasing thickens
    small text, so it is only a tiebreak: the runs `thumbnail_weights` settles read 0.080-0.083 where
    drawn regular and 0.116-0.142 where drawn bold (whole boxes the API calls bold read 0.09-0.16,
    regular ones 0.05-0.13)."""
    runs = [r for p in e.paragraphs for r in p.runs if r.text.strip()]
    if thumb is None or not px or not runs or crossed(e, elements, e.bbox):
        return None
    z = max(r.size or 0 for r in runs)
    x0, y0, x1, y1 = (round(v * px) for v in e.bbox)
    crop = thumb[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
    if z <= 0 or crop.shape[0] < 4 or crop.shape[1] < 4:
        return None
    ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
    contrast = np.abs(crop - ground).max(axis=-1)
    ink = contrast > max(40.0, float(contrast.max()) / 2)
    area = int(ink.sum())
    if area < 20:
        return None
    inner = ink.copy()
    inner[1:, :] &= ink[:-1, :]
    inner[:-1, :] &= ink[1:, :]
    inner[:, 1:] &= ink[:, :-1]
    inner[:, :-1] &= ink[:, 1:]
    outline = area - int(inner.sum())
    return float(2 * area / max(outline, 1) / px / z)


def thumbnail_weights(elements: Sequence[TargetElement], thumb: SignedRGB | None, px: float) -> list[TargetElement]:
    """Settle the runs whose weight the API does not say (`weight_unsure`): a run that only names a
    font reads back as `bold: false` at weight 400 under a bold parent, and Slides draws some of those
    bold (jruby-ja's Tahoma titles, drawings-basics' title slides, solidity-survey) and some regular
    (drawings-basics' slides 9 and 11, identical in the API). The thumbnail's stroke width tells them
    apart (`stroke_em`); unreadable, the API's word stands."""
    out = list(elements)
    for k, e in enumerate(out):
        if not isinstance(e, TargetText) or not any(r.weight_unsure for p in e.paragraphs for r in p.runs):
            continue
        w = stroke_em(e, out, thumb, px)
        bold = w is not None and w > BOLD_STROKE_EM
        out[k] = replace(e, paragraphs=tuple(
            replace(p, runs=tuple(replace(r, weight_unsure=None, bold=True if bold else r.bold) if r.weight_unsure else r
                                  for r in p.runs))
            for p in e.paragraphs))
    return out


def crossed(e: TargetElement, elements: Sequence[TargetElement], strip: Box) -> bool:
    """Does anything but the box itself reach into `strip`, where its thumbnail is read? What lies under
    the box, from its top down past the strip, does not: a panel it stands on, or the full-slide
    picture of a template's layout (devfest2020 draws every slide's ground as one, and none of its
    boxes could be read)."""
    return bool(crossing(e, elements, strip, True))


def crossing(e: TargetElement, elements: Sequence[TargetElement], strip: Box, first: bool) -> list[TargetElement]:
    """The elements `crossed` asks about (only the first one with `first`). `e` is found in
    `elements` as itself: what comes before it is under it."""
    x0, y0, x1, y1 = e.bbox
    below = True
    found: list[TargetElement] = []
    own = f"{e.id}~fill" if e.id else None
    for o in elements:
        if o is e:
            below = False
            continue
        if own and o.id == own:
            # the box's own ground, a picture of the thumbnail behind its words (`deck_fills`): it
            # stands exactly under the box, so no panel test passes, and devfest2020's titles, which all
            # have one, were never read (their zero insets wrapped "Full screen slide" onto two lines)
            continue
        b = o.bbox
        if b[2] <= strip[0] or b[0] >= strip[2] or b[3] <= strip[1] or b[1] >= strip[3]:
            continue
        # (sideways it need only hold the strip: devfest2020's "50%" box runs 800 pt off the slide's
        # right edge, and no panel under it reaches past that)
        if (isinstance(o, TargetShape) or below and isinstance(o, TargetImage)) and b[0] < max(x0, strip[0]) - 1 \
                and b[1] < y0 - 2 and b[2] > min(x1, strip[2]) + 1 and b[3] > strip[3]:
            continue
        found.append(o)
        if first:
            break
    return found


CAP_EM = 0.72               # a Latin face's cap height, near enough to tell 6.5 pt of top inset
PPTX_INSET_Y = 3.6          # Slides pt: PowerPoint's default top and bottom insets (0.05 in); Slides' are 7.2
PPTX_SANE_DRIFT = 6.0       # Slides pt: a `top_drift` further off than this misread the line
# Cap heights (em) of the faces `top_drift` may read: any other face's own cap height moves its first
# ink by as much as the insets do (Calibri's 0.644 read as jeb-arch's boxes standing 2.8 pt high,
# Google Sans as firebase-jam's 4.5, and giving them PowerPoint's insets cost 0.03 each).
KNOWN_CAPS = {"Arial": 0.716, "Arimo": 0.716, "Helvetica": 0.717, "Liberation Sans": 0.716, "Roboto": 0.711}


def top_drift(e: TargetElement, elements: Sequence[TargetElement], thumb: SignedRGB | None, px: float) -> float | None:
    """Where the slide's thumbnail shows a top-aligned box's first capital, less where Slides' default
    insets put it, in Slides pt (None: not measurable here). Only a box nothing else reaches into
    above its first baseline, whose first word opens on a capital or digit (a lowercase start would
    read an x-height as a cap)."""
    box_ = text_box(e)
    if thumb is None or not isinstance(e, TargetText) or box_ is None or box_.insets is not None \
            or box_.valign != "top" or e.placeholder in ("TITLE", "CENTERED_TITLE", "SUBTITLE"):
        return None
    paras = [p for p in e.paragraphs if p.runs]
    first = "".join(r.text for r in paras[0].runs).lstrip() if paras else ""
    if not first:
        return None
    if paras[0].bullet:
        # a bullet's glyph sits beside the line, not over it; only a baseline says where the line is
        # (arabic-training's bulleted Arial and Times boxes read -3.7 and -4.0)
        return baseline_drift(e, box_, elements, paras, thumb, px)
    cap = KNOWN_CAPS.get(paras[0].runs[0].font or "")
    if cap is None or not (first[0].isupper() or first[0].isdigit()):
        return baseline_drift(e, box_, elements, paras, thumb, px)
    x0, y0, x1, y1 = e.bbox
    base = e.anchor[1]
    if crossed(e, elements, (x0, y0 - 2, x1, base + 2)):
        return None
    z = max(r.size or 0 for r in paras[0].runs)
    X0, X1, Y0, Y1 = (round(v * px) for v in (x0, x1, y0, base + 1))
    crop = thumb[max(0, Y0):max(0, Y1), max(0, X0):max(0, X1)]
    if z <= 0 or crop.shape[0] < 4 or crop.shape[1] < 8:
        return None
    ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
    rows = np.nonzero((np.abs(crop - ground).max(axis=-1) > 80).sum(axis=1) >= 2)[0]
    if not len(rows):
        return None
    scale = box_.scale or 1.0
    return ((Y0 + int(rows[0])) / px - (base - cap * z)) * scale


def side_gap(e: TargetElement, elements: Sequence[TargetElement], thumb: SignedRGB | None, px: float) -> float | None:
    """How far the thumbnail shows a box's words from the side they start on, in Slides pt: the box's
    left edge for left-aligned left-to-right text, its right edge for right-aligned right-to-left text,
    each less the paragraphs' indent (None: not measurable). That is the side inset plus the first
    glyph's bearing. Rows another element reaches into are not read."""
    box_ = text_box(e)
    if thumb is None or not px or not isinstance(e, TargetText) or box_ is None or box_.insets is not None:
        return None
    paras = [p for p in e.paragraphs if p.runs and any(r.text.strip() for r in p.runs)]
    if not paras or any(p.bullet for p in paras):
        return None
    directions = {p.direction == "rtl" for p in paras}
    if len(directions) != 1:
        return None
    rtl = directions.pop()
    # centred lines stand further in than the start-aligned ones, so they may be read along
    start = "right" if rtl else "left"
    if any(p.align not in (start, "center") for p in paras) or not any(p.align == start for p in paras):
        return None
    scale = box_.scale or 1.0
    indent = min(min(_indent_first(p), _indent_start(p)) for p in paras) / scale
    x0, y0, x1, y1 = e.bbox
    reach = (SIDE_READ + indent * scale) / scale
    strip = (x1 - reach, y0, x1 + 1, y1) if rtl else (x0 - 1, y0, x0 + reach, y1)
    X0, X1 = round(strip[0] * px), round(strip[2] * px)
    Y0, Y1 = round(y0 * px), round(y1 * px)
    if X0 < 0 or Y0 < 0 or X1 > thumb.shape[1] or Y1 > thumb.shape[0]:
        return None
    crop = thumb[Y0:Y1, X0:X1]
    if crop.shape[0] < 4 or crop.shape[1] < 8:
        return None
    free = np.ones(crop.shape[0], dtype=bool)
    for o in crossing(e, elements, strip, False):
        a = max(0, int(np.floor((o.bbox[1] - 1) * px)) - Y0)
        b = max(0, int(np.ceil((o.bbox[3] + 1) * px)) - Y0)
        free[a:b] = False
    if free.sum() < 4:
        return None
    ground = np.median(crop[free].reshape(-1, crop.shape[-1]), axis=0)
    ink = (((np.abs(crop - ground).max(axis=-1) > 80) & free[:, None]).sum(axis=0)) >= 2
    cols = np.nonzero(ink)[0]
    if len(cols) < 3:
        return None
    edge = (X0 + int(cols[-1]) + 1) / px if rtl else (X0 + int(cols[0])) / px
    return float(((x1 - indent - edge) if rtl else (edge - x0 - indent)) * scale)


SNAP_PAGE = 960.0           # pages up to this wide (Slides pt) set single-spaced lines on whole pixels (box `snap`, adopt.snapped_line_box)
SIDE_READ = 14.0            # Slides pt from a box's side that `side_gap` reads
SIDE_BEARING_EM = 0.04      # a first glyph's side bearing, near enough (Times' Hebrew 0.02-0.05, Arial ~0.07)
# `side_inset` below this is PowerPoint's 3.6 pt, above it Slides' own ~6.7: on the corpus the boxes of
# comps-analysis read 3.1-4.2 and those of every deck with Slides' sides 5.5-8 (medians 6.2-7.6)
SIDE_SPLIT = 5.15


def side_inset(e: TargetElement, gap: float) -> float:
    """The side inset a box's `side_gap` shows: the gap less its first glyph's bearing."""
    if not isinstance(e, TargetText):
        raise TypeError("only a text box has a side inset")
    z = max((r.size or 0) for p in e.paragraphs for r in p.runs)
    box_ = text_box(e)
    return gap - SIDE_BEARING_EM * z * ((None if box_ is None else box_.scale) or 1.0)


BASELINE_BAND = (0.95, 0.3)     # em above and below the predicted first baseline `baseline_drift` reads
BASELINE_MIN_COLUMNS = 0.6      # em of inked columns a first line needs before its baseline is read
BASELINE_DENSITY = 0.25         # the baseline: the lowest row inked this much of the line's densest one


def baseline_drift(e: TargetText, box_: TextBox, elements: Sequence[TargetElement], paras: Sequence[TargetParagraph],
                   thumb: SignedRGB, px: float) -> float | None:
    """`top_drift` for a first line in a script with no capitals: where the thumbnail shows its
    baseline, less where Slides' default insets put it, in Slides pt. Hebrew and Arabic letters stand
    on the baseline, and Book Antiqua's Hebrew is drawn by a fallback whose cap height nobody knows, so
    the baseline is the lowest row still inked a quarter as densely as the line's densest (descenders
    and commas are thin below it; a column median read bold Hebrew's top bars - ד ר ו are a bar on a
    stem - and put a title's baseline 5 pt high). hebrew-lesson's text stood 3.6 pt low on every slide it was not
    middle-aligned on, and not one of its boxes could be measured by its capitals. An underline or a
    strike is a row inked across the whole line: such rows are cleared first. Only these scripts:
    ideographs sit on an em box below the baseline, and a Latin line's column bottoms read other
    things than its caps did (cs161-net's disagreed by 6 pt)."""
    from .scripts import script_of
    runs = paras[0].runs
    first = next((c for r in runs for c in r.text if c.isalpha()), "")
    if not first or script_of(first) not in ("hebrew", "arabic") or any(r.highlight for r in runs):
        return None
    z = max(r.size or 0 for r in runs)
    if z <= 0:
        return None
    x0, y0, x1, y1 = e.bbox
    base = e.anchor[1]
    top, bottom = base - BASELINE_BAND[0] * z, base + BASELINE_BAND[1] * z
    if crossed(e, elements, (x0, min(y0, top) - 2, x1, bottom + 2)):
        return None
    X0, X1, Y0, Y1 = (round(v * px) for v in (x0, x1, top, bottom))
    crop = thumb[max(0, Y0):max(0, Y1), max(0, X0):max(0, X1)]
    if crop.shape[0] < 4 or crop.shape[1] < 8:
        return None
    ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
    ink = np.abs(crop - ground).max(axis=-1) > 80
    cols = np.nonzero(ink.any(axis=0))[0]
    if len(cols) < BASELINE_MIN_COLUMNS * z * px:
        return None
    rules = ink[:, cols[0]:cols[-1] + 1].mean(axis=1) > 0.7     # underlines and strikes
    ink[rules] = False
    cols = np.nonzero(ink.any(axis=0))[0]
    if len(cols) < BASELINE_MIN_COLUMNS * z * px:
        return None
    density = ink[:, cols[0]:cols[-1] + 1].mean(axis=1)
    rows = np.nonzero(density >= BASELINE_DENSITY * density.max())[0]
    if rows[-1] >= ink.shape[0] - 1:                    # ink runs on out of the band: not one line
        return None
    seen = (max(0, Y0) + int(rows[-1]) + 1) / px
    return float((seen - base) * (box_.scale or 1.0))


def pptx_insets(slides: Sequence[Sequence[TargetElement]], drifts: Sequence[tuple[TargetElement, float]],
                sides: Sequence[float]) -> list[list[TargetElement]]:
    """Boxes that stand PowerPoint's inset higher than Slides' insets would put them came from a .pptx
    whose boxes kept PowerPoint's defaults (7.2 pt at the sides, 3.6 top and bottom), which the API does
    not report: comps-analysis's text stood ~3.6 pt low on every slide. On the corpus a measured box
    (`top_drift`) is off by 0 +- 1 pt or by -3.6 +- 1, nothing between, so a box measured at -3.6 gets
    the insets; and when most of a deck's measured boxes (at least 3) stand nearer -3.6 than 0, so do
    its unmeasured ones -
    gdg24 mixes both kinds and keeps Slides' insets where it could not be measured. Such boxes get
    `box.inset_y`, and their anchors move by the difference.

    The sides are the deck's own question: `sides` (`side_inset` of every box whose words' start edge
    the thumbnails show) says whether its boxes have PowerPoint's 3.6 pt there or Slides' own. With
    none to go by, a deck imported whole gets 3.6.

    `drifts` names its boxes as the objects `slides` holds; each slide comes back as a new list."""
    import statistics
    want = PPTX_INSET_Y - 7.2
    hits = [e for e, d in drifts if abs(d - want) <= 1.2]
    # whether the deck came whole is a vote of its sane readings (a drift past 6 pt is a misread, not
    # an inset) for the nearer of the two answers: arabic-training's Calibri Arabic reads -2.3 (its
    # fallback's densest row stands a little under the baseline) beside Arial and Times at -3.7 and
    # -4.0, and poster-48x36 reads -3.1 to -5.1; both gain (boxes +0.033, +0.011)
    sane = [d for _, d in drifts if abs(d) <= PPTX_SANE_DRIFT]
    votes = [d for d in sane if d < want / 2]
    whole = len(sane) >= 3 and len(votes) >= 0.6 * len(sane)
    # hebrew-lesson is imported whole too, and its right-to-left words start 3.2 pt further in than
    # 3.6 pt of inset put them (every box read 5.4-7.4 pt, like Slides' own): its sides are Slides'.
    # Words cannot start outside their box, so a side read below 0 is something else's ink
    # (arabic-training's -3.1, -1.4 and -0.95 took its median from 5.5 to 2.3: boxes -0.024)
    read = [x for x in sides if x > 0]
    narrow = not read or statistics.median(read) < SIDE_SPLIT
    measured = {id(e) for e, _ in drifts}
    chosen = {id(e) for e in hits}
    out: list[list[TargetElement]] = []
    for s in slides:
        kept: list[TargetElement] = []
        for e in s:
            box_ = text_box(e)
            if not isinstance(e, TargetText) or box_ is None or box_.insets is not None \
                    or id(e) not in chosen and (id(e) in measured or not whole):
                kept.append(e)
                continue
            # a deck imported whole keeps the .pptx's side insets too, 3.6 pt like the top ones:
            # comps-analysis's text starts 3.1-3.8 pt left of Slides' 6.7 and wrapped every
            # other line early (boxes 0.43 -> 0.66); ap-bio-stats' two lone boxes measured at
            # -3.6 keep Slides' sides (their titles stand where 6.7 pt puts them)
            box_ = replace(box_, inset_y=PPTX_INSET_Y, inset_x=PPTX_INSET_Y if whole and narrow else box_.inset_x)
            anchor = e.anchor
            if box_.valign in ("top", "bottom"):
                dy = -want / (box_.scale or 1.0) * (1 if box_.valign == "bottom" else -1)
                anchor = (anchor[0], round(anchor[1] + dy, 2))
            kept.append(replace(e, box=box_, anchor=anchor))
        out.append(kept)
    return out


PLACE_MIN_PX = 24        # a frame smaller than this in either dimension is not worth searching
PLACE_GRID = 32          # canonical side (px) the frame's crop and the file are both resampled to
PLACE_MIN_VISIBLE = 0.35 # a candidate box must keep at least this share of its grid cells visible
PLACE_STRETCHED_OK = 0.55  # a stretch that already correlates this well is left alone
PLACE_MIN_SCORE = 0.5      # the found box must correlate at least this well (a real chart thumbnail,
                           # softened by JPEG/antialiasing and a label painted over part of it, reads
                           # nowhere near a synthetic pattern's ~0.98 even at its true box: applied-ml's
                           # own chart peaks at 0.606)
PLACE_MIN_GAIN = 0.2       # ... and clearly better than the stretch it replaces (applied-ml: 0.22 -> 0.61)
PLACE_MIN_SPAN = 0.25      # never shrink a side to less than this share of the frame
PLACE_COARSE = (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45)
PLACE_FINE = (-0.02, -0.01, 0.0, 0.01, 0.02)
PLACE_TIGHT = tuple(round(i * 0.01, 2) for i in range(-6, 7))  # +-0.06 at 0.01, around a coarse seed

Insets = dict[str, float]
"""A candidate box inside a picture's frame: its insets `l`, `t`, `r`, `b` as shares of the frame."""
Placement = tuple[float, float, float, float, float]
"""The insets `l`, `t`, `r`, `b` a search settled on, and their score."""


def _place_resize(arr: Floats32 | Floats | Mask, gh: int, gw: int) -> Floats:
    """`arr` resampled to `(gh, gw)`: a box average (an integral image, so every grid cell is exact)
    when shrinking - a chart's gridlines and a checkerboard test pattern alike alias badly under
    nearest-neighbour sampling, which once turned a clean single peak at the true box into noise a
    coordinate descent could wander off into - and nearest neighbour only for the rare case of
    upsampling a crop smaller than the grid itself."""
    h, w = arr.shape[:2]
    if h == 0 or w == 0:
        return np.zeros((gh, gw), dtype=np.float64)
    if h < gh or w < gw:
        ys = np.clip(np.arange(gh) * h // gh, 0, h - 1)
        xs = np.clip(np.arange(gw) * w // gw, 0, w - 1)
        return arr[ys][:, xs].astype(np.float64)
    integral = np.zeros((h + 1, w + 1), dtype=np.float64)
    integral[1:, 1:] = arr.astype(np.float64).cumsum(0).cumsum(1)
    ys = np.linspace(0, h, gh + 1).round().astype(int)
    xs = np.linspace(0, w, gw + 1).round().astype(int)
    total = (integral[ys[1:]][:, xs[1:]] - integral[ys[:-1]][:, xs[1:]]
             - integral[ys[1:]][:, xs[:-1]] + integral[ys[:-1]][:, xs[:-1]])
    counts = np.outer(np.diff(ys), np.diff(xs)).astype(np.float64)
    counts[counts <= 0] = 1.0
    return total / counts


def _place_ncc(a: Floats, b: Floats, visible: Mask) -> float | None:
    """Normalised cross-correlation of `a` against `b` over the pixels `visible` marks, or None where
    too little of the grid is visible or either side is flat (no ink to match at all)."""
    n = int(visible.sum())
    if n < max(16, PLACE_MIN_VISIBLE * visible.size):
        return None
    av, bv = a[visible].astype(np.float64), b[visible].astype(np.float64)
    av -= av.mean()
    bv -= bv.mean()
    da, db = float((av * av).sum()), float((bv * bv).sum())
    if da < 1e-6 or db < 1e-6:
        return None
    return float((av * bv).sum() / (da * db) ** 0.5)


def _place_score(frame_gray: Floats32, visible: Mask, src_grid: Floats, l: float, t: float, r: float,  # noqa: E741
                 b: float) -> float | None:
    """How well the file (already resampled once to `src_grid`, the whole picture, never a crop of it)
    matches the thumbnail's own pixels inside the box `(l, t, r, b)` cuts from the frame - insets as a
    share of the frame's own width (`l`, `r`) and height (`t`, `b`). Both sides are resampled to the
    same small grid before comparing, so the candidate box's own pixel size never matters, only where
    it falls inside the frame."""
    fh, fw = frame_gray.shape
    x0, x1 = round(l * fw), fw - round(r * fw)
    y0, y1 = round(t * fh), fh - round(b * fh)
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    grid = _place_resize(frame_gray[y0:y1, x0:x1], PLACE_GRID, PLACE_GRID)
    vgrid = _place_resize(visible[y0:y1, x0:x1], PLACE_GRID, PLACE_GRID) >= 0.5
    return _place_ncc(grid, src_grid, vgrid)


def _score_at(frame_gray: Floats32, visible: Mask, src_grid: Floats, at: Insets) -> float | None:
    """`_place_score` of the insets `at`."""
    return _place_score(frame_gray, visible, src_grid, at["l"], at["t"], at["r"], at["b"])


def _place_search(frame_gray: Floats32, visible: Mask, src_grid: Floats, base: float) -> Placement:
    """Coordinate descent over the four insets, coarse then fine, each held against the others: cheap
    enough to run only when the plain stretch (`base`) already reads poorly, and general enough for
    padding on one side, two opposite sides, or all four - a diagonal or rotated placement is not
    modelled and is left to the rotation/flip guard in the caller.

    Seeded first from the two symmetric hypotheses (equal padding on both sides of one axis, the
    other axis untouched) - centred content, the overwhelmingly common case for a .pptx's negative
    `srcRect`. A one-sided move alone can cross a false trough before it reaches the true box (a
    chart's own gridlines and ticks correlate with themselves at more than one offset), where the
    two matched sides together are read against a single, unambiguous width or height."""
    l = t = r = b = 0.0  # noqa: E741
    best = base
    for m in PLACE_COARSE:
        s = _place_score(frame_gray, visible, src_grid, m, 0.0, m, 0.0)
        if s is not None and s > best:
            best, l, r = s, m, m  # noqa: E741
    # PLACE_COARSE's 0.05 steps can straddle a real, narrow correlation peak (a chart's own axis and
    # gridlines are thin) without ever landing on it - applied-ml's true ~18% inset scores 0.94 at
    # 0.18 but only 0.43 at the nearest coarse step, 0.20 - so refine the symmetric seed at 0.01
    # resolution around whichever coarse step won before the four sides are ever allowed to move apart.
    for m in [round(l + step, 3) for step in PLACE_TIGHT if 0.0 <= l + step <= 0.45]:
        s = _place_score(frame_gray, visible, src_grid, m, 0.0, m, 0.0)
        if s is not None and s > best:
            best, l, r = s, m, m  # noqa: E741
    for m in PLACE_COARSE:
        s = _place_score(frame_gray, visible, src_grid, l, m, r, m)
        if s is not None and s > best:
            best, t, b = s, m, m
    for m in [round(t + step, 3) for step in PLACE_TIGHT if 0.0 <= t + step <= 0.45]:
        s = _place_score(frame_gray, visible, src_grid, l, m, r, m)
        if s is not None and s > best:
            best, t, b = s, m, m
    for steps in (PLACE_COARSE, PLACE_FINE, PLACE_FINE):
        improved = False
        for axis in ("l", "t", "r", "b"):
            cur = {"l": l, "t": t, "r": r, "b": b}
            for step in steps:
                v = step if steps is PLACE_COARSE else max(0.0, min(0.45, cur[axis] + step))
                trial = dict(cur, **{axis: v})
                if trial["l"] + trial["r"] > 0.7 or trial["t"] + trial["b"] > 0.7:
                    continue
                s = _score_at(frame_gray, visible, src_grid, trial)
                if s is not None and s > best:
                    best, cur[axis] = s, v
                    improved = True
            l, t, r, b = cur["l"], cur["t"], cur["r"], cur["b"]  # noqa: E741
        if not improved and steps is not PLACE_COARSE:
            break
    return _place_trim(frame_gray, visible, src_grid, l, t, r, b, best)


PLACE_TRIM_TOL = 0.05    # widening a side back towards 0 is taken even at a small cost in score - a
                         # side of the frame no picture in the corpus actually needed can still read a
                         # little better empty than full of the wrong pixels (a chart's own gridlines
                         # correlate with themselves at more than one width), so the hill climb above
                         # keeps drifting a side past the picture's true edge for a marginal gain


def _place_trim(frame_gray: Floats32, visible: Mask, src_grid: Floats, l: float, t: float, r: float,  # noqa: E741
                b: float, best: float) -> Placement:
    """Each inset walked back towards 0 (finer, then coarser, steps) as long as the match stays within
    `PLACE_TRIM_TOL` of the best score the coordinate descent found: undoes the drift above, which can
    cross a false trough on its way past the picture's real edge and settle a side deeper than the
    picture needs (applied-ml's chart: descent alone left the right inset at 0.29, over the visible
    plot box's real ~0.2; trimmed back to where the score is still within tolerance)."""
    cur = {"l": l, "t": t, "r": r, "b": b}
    floor = best - PLACE_TRIM_TOL       # fixed reference: trimming one side never borrows tolerance
                                         # a later side has already spent, or four small trims could
                                         # add up to a box no better than the plain stretch
    for axis in ("l", "r", "t", "b"):
        candidates = sorted({round(v, 3) for v in (0.0, *PLACE_FINE, *PLACE_COARSE) if 0 <= v < cur[axis]})
        for v in candidates:
            trial = dict(cur, **{axis: v})
            s = _score_at(frame_gray, visible, src_grid, trial)
            if s is not None and s >= floor:
                cur[axis] = v
                break
    final = _score_at(frame_gray, visible, src_grid, cur)
    return cur["l"], cur["t"], cur["r"], cur["b"], final if final is not None else best


def _place_visible(above: Sequence[TargetElement], frame_px: PixelBox, px: float, w: int, h: int) -> Mask:
    """The frame's own pixels (`frame_px`, image coordinates), less whatever an element above it
    (`thumbnail_picture`'s `above`) actually draws there - a rotated or off-colour text box included,
    however it is filled, since none of its ink is the picture's to match. An element with nothing to
    draw (a bare text placeholder, an unfilled unoutlined shape) is not in the way at all."""
    a0, b0, a1, b1 = frame_px
    vis = np.ones((b1 - b0, a1 - a0), dtype=bool)
    for e in above:
        if isinstance(e, TargetText) and not e.paragraphs:
            continue
        if isinstance(e, TargetShape) and not e.fill and not e.fill_gradient and not e.outline:
            continue
        c0, d0, c1, d1 = px_box(e.bbox, px, w, h, 0)
        if c1 <= a0 or c0 >= a1 or d1 <= b0 or d0 >= b1:
            continue
        vis[max(0, d0 - b0):max(0, d1 - b0), max(0, c0 - a0):max(0, c1 - a0)] = False
    return vis


def thumbnail_picture_places(elements: Sequence[TargetElement], thumb: SignedRGB | None,
                             px: float) -> list[TargetElement]:
    """A picture the API draws smaller than its own frame - a .pptx's negative `srcRect` (padding),
    which `imageProperties.cropProperties` never carries, so `picture_props` never sees it - found from
    the slide's own thumbnail: the file is never cropped (the whole image is shown, just not stretched
    over the whole frame), so some inset box of the frame matches the thumbnail's pixels there far
    better than the frame itself does. That box becomes the element's own `bbox` (and `box`, kept
    equal to it as `picture_props` leaves them for an unrotated picture): the frame's own margin is
    left blank around it, exactly where Slides already draws nothing.

    Conservative throughout: a plain stretch that already reads well (`PLACE_STRETCHED_OK`) is left
    exactly as it was; the found box must read clearly better (`PLACE_MIN_SCORE`, `PLACE_MIN_GAIN`)
    and keep a sane share of the frame (`PLACE_MIN_SPAN`) to be taken at all; pixels an element drawn
    above the picture actually covers (`_place_visible`) are left out of the comparison, so a caption
    or a label crossing the frame cannot be mistaken for the picture's own ink. A rotated, flipped or
    already-cropped picture is left alone - only an axis-aligned box is searched for."""
    out = list(elements)
    if thumb is None or not px:
        return out
    from PIL import Image
    H, W = thumb.shape[:2]
    for k, el in enumerate(out):
        if not isinstance(el, TargetImage) or not el.file or el.video or el.chart \
                or el.rotation or el.flip or el.crop:
            continue
        a0, b0, a1, b1 = px_box(el.bbox, px, W, H, 0)
        fw, fh = a1 - a0, b1 - b0
        if fw < PLACE_MIN_PX or fh < PLACE_MIN_PX:
            continue
        try:
            with Image.open(el.file) as im:
                if "A" in im.getbands():
                    # A transparent PNG (an icon or a badge drawn to fill its own square canvas,
                    # e.g. a circular flag with its corners cut by alpha) reads its raw RGB under
                    # the transparent pixels as if it were content: those corners are usually
                    # black or garbage, never the slide's white, so a plain greyscale conversion
                    # makes an already-correct 1:1 frame score as a bad stretch and sends the
                    # search hunting for a smaller box that was never really there (devfest2020
                    # slide 39's flag badges: alpha_transp_frac ~0.22, a circle inscribed in its
                    # square, shrunk into ovals before this fix). Composite onto white first, the
                    # colour Slides itself shows through a transparent PNG's alpha
                    # (CLAUDE.md "Layout pages reject ... transparent PNG ... shows white").
                    rgba = np.asarray(im.convert("RGBA"), dtype=np.float32)
                    alpha = rgba[:, :, 3:4] * (1.0 / 255.0)
                    flat = rgba[:, :, :3] * alpha + 255.0 * (1.0 - alpha)
                    src: Floats32 = (flat * np.array([0.299, 0.587, 0.114], dtype=np.float32)).sum(axis=2)
                else:
                    src = np.asarray(im.convert("L"), dtype=np.float32)
        except (OSError, ValueError):
            continue
        if src.size == 0:
            continue
        frame_gray: Floats32 = (thumb[b0:b1, a0:a1].astype(np.float32)
                                * np.array([0.299, 0.587, 0.114], dtype=np.float32)).sum(axis=2)
        visible = _place_visible(out[k + 1:], (a0, b0, a1, b1), px, W, H)
        src_grid = _place_resize(src, PLACE_GRID, PLACE_GRID)
        base = _place_score(frame_gray, visible, src_grid, 0.0, 0.0, 0.0, 0.0)
        if base is None or base >= PLACE_STRETCHED_OK:
            continue
        l, t, r, b, score = _place_search(frame_gray, visible, src_grid, base)  # noqa: E741
        if score < PLACE_MIN_SCORE or score - base < PLACE_MIN_GAIN:
            continue
        if 1 - l - r < PLACE_MIN_SPAN or 1 - t - b < PLACE_MIN_SPAN:
            continue
        x0, y0, x1, y1 = el.bbox
        w, h = x1 - x0, y1 - y0
        nb = (round(x0 + l * w, 2), round(y0 + t * h, 2), round(x1 - r * w, 2), round(y1 - b * h, 2))
        out[k] = replace(el, bbox=nb, box=nb, picture_place="thumbnail")
    return out


# `thumbnail_picture_masks`: the ring either side of the inscribed ellipse's edge that is judged
# neither way (an outline, anti-aliasing), the pixels each side must hold, and the shares that decide
MASK_BAND = 0.12
MASK_MIN_PX = 150
MASK_TOL = 14
MASK_INNER_MATCH = 0.8
MASK_CORNER_PAGE = 0.6


def thumbnail_picture_masks(elements: Sequence[TargetElement], thumb: SignedRGB | None,
                            px: float) -> list[TargetElement]:
    """A picture Google shows only inside the ellipse its frame inscribes, found from the thumbnail:
    a .pptx's `prstGeom prst="ellipse"` on a `p:pic` survives the import as a mask the API never
    mentions (yc-seed-white slide 10's three round portraits, each with a 3 pt outline that Slides
    draws round too). Inside the ellipse the thumbnail is the picture; in the frame's corners outside
    it the thumbnail is the page around the frame and not the picture. Both have to hold on most of
    their pixels (`MASK_INNER_MATCH`, `MASK_CORNER_PAGE`), and the corners must differ from the page
    at all, so a picture whose own corners are the page colour stays a rectangle, which draws the
    same. Pixels an element above covers are left out (`_place_visible`); a turned picture is left
    alone."""
    out = list(elements)
    if thumb is None or not px:
        return out
    from .compare import displayed_picture
    H, W = thumb.shape[:2]
    for k, el in enumerate(out):
        if not isinstance(el, TargetImage) or not el.file or el.video or el.chart or el.rotation:
            continue
        a0, b0, a1, b1 = px_box(el.bbox, px, W, H, 0)
        fw, fh = a1 - a0, b1 - b0
        if fw < PLACE_MIN_PX or fh < PLACE_MIN_PX:
            continue
        img = displayed_picture(element_json(el))
        if img is None:
            continue
        pic = np.asarray(img.convert("RGB").resize((fw, fh)), dtype=np.int16)
        sub = thumb[b0:b1, a0:a1, :3].astype(np.int16)
        g = 4
        ring = np.concatenate([thumb[max(0, b0 - g):b0, a0:a1, :3].reshape(-1, 3),
                               thumb[b1:b1 + g, a0:a1, :3].reshape(-1, 3),
                               thumb[b0:b1, max(0, a0 - g):a0, :3].reshape(-1, 3),
                               thumb[b0:b1, a1:a1 + g, :3].reshape(-1, 3)]).astype(np.int16)
        if len(ring) < MASK_MIN_PX:
            continue
        page = np.median(ring, axis=0)
        vis = _place_visible(out[k + 1:], (a0, b0, a1, b1), px, W, H)
        yy, xx = np.mgrid[0:fh, 0:fw]
        r = ((xx + 0.5 - fw / 2) / (fw / 2)) ** 2 + ((yy + 0.5 - fh / 2) / (fh / 2)) ** 2
        inner = (r < (1 - MASK_BAND) ** 2) & vis
        corner = (r > (1 + MASK_BAND) ** 2) & vis
        if inner.sum() < MASK_MIN_PX or corner.sum() < MASK_MIN_PX:
            continue
        is_pic = np.abs(sub - pic).max(axis=2) <= MASK_TOL
        is_page = np.abs(sub - page).max(axis=2) <= MASK_TOL
        pic_is_page = np.abs(pic - page).max(axis=2) <= MASK_TOL
        if is_pic[inner].mean() < MASK_INNER_MATCH:
            continue
        # the corners where the picture itself would show something other than the page
        telling = corner & ~pic_is_page
        if telling.sum() < MASK_MIN_PX:
            continue
        if (is_page & ~is_pic)[telling].mean() >= MASK_CORNER_PAGE:
            out[k] = replace(el, mask="ellipse")
    return out
