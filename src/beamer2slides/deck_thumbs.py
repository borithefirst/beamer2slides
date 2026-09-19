"""What a foreign deck's slide thumbnails say that the Slides API does not (`deck_ir(foreign=True,
thumbnails=...)`): table rows as tall as Slides draws them and cell insets, text insets from where a box's
first ink starts (top, side, baseline), PowerPoint's insets for a deck imported from a .pptx, the width a
stand-in font must be set at, and whether an unsure weight is drawn bold. Each reader measures the
LARGE thumbnail (`thumb`, `px` pixels per Slides pt) and writes what it found into the elements or
their `box`; nothing here calls Google. Measurements and history: docs/adopt-bench.md."""

from __future__ import annotations

import unicodedata

from .emit import BASELINE_A, PAD_X, PPTX_TITLE_DY


ROW_LINE_SHARE = 0.5     # a row boundary is read when its borders cover this share of the table's width
ROW_LINE_HIT = 0.85      # ... and this share of the columns sampled along them shows the line


def thumbnail_rows(elements: list[dict], thumb, px: float) -> None:
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
    if thumb is None or not px:
        return
    import numpy as np
    H, W = thumb.shape[:2]
    for e in elements:
        heights, widths = e.get("row_heights"), e.get("col_widths")
        if e.get("kind") != "table" or not heights or not widths:
            continue
        xs = [e["bbox"][0]]
        for w in widths:
            xs.append(xs[-1] + w)
        by_row: dict[int, list] = {}
        for b in e.get("table_borders", []):
            if b["dir"] == "h" and b["col"] < len(widths) and (1.0 if b.get("alpha") is None else b["alpha"]) >= 0.5 and b.get("color"):
                by_row.setdefault(b["row"], []).append(b)
        new, fixed = list(heights), []
        prev = e["bbox"][1]
        for j in range(1, len(heights) + 1):
            segs = by_row.get(j, [])
            if sum(widths[b["col"]] for b in segs) < ROW_LINE_SHARE * (xs[-1] - xs[0]):
                break
            y = _find_row_line(thumb, px, segs, xs, prev + heights[j - 1], heights[j - 1], H, W)
            if y is None:
                break
            if y - prev > heights[j - 1] + 0.4:
                new[j - 1] = round(y - prev, 3)
            fixed.append(j - 1)
            prev = y
        if fixed:
            e["row_heights"] = new
            e["rows_fixed"] = fixed


CELL_BEARING_EM = 0.06   # a first or last glyph's side bearing, in em, where a cell's words' ink begins
CELL_PAD_MIN = 3         # cells whose ink edge reads the side inset, at least


def thumbnail_cell_pad(elements: list[dict], thumb, px: float) -> None:
    """A table's side inset as its thumbnail shows it (`cell_pad[0]`).

    `cell_pad` guesses it from the vertical inset, and a .pptx brings its own: comps-analysis' words
    start 3 pt from their cells' left edges where the guess put them 5.8 in, wrapping "Implied Equity
    Value" after "Implied"; hebrew-lesson's right-aligned lines end 7.2 pt from the right edge, where
    the guess (4.8) ended them. A cell whose paragraphs are all left-aligned (right-aligned) gives the
    distance from its left (right) edge to its first (last) ink column, less a glyph's side bearing;
    the median of at least `CELL_PAD_MIN` such cells is the inset. Only rows whose top is known are
    read: those `thumbnail_rows` measured and the one under them, whose stored height it fills at
    least."""
    if thumb is None or not px:
        return
    import numpy as np
    H, W = thumb.shape[:2]
    for e in elements:
        heights, widths = e.get("row_heights"), e.get("col_widths")
        if e.get("kind") != "table" or not heights or not widths or not e.get("cell_pad"):
            continue
        fixed = set(e.get("rows_fixed", []))
        known = 0
        while known < len(heights) and known in fixed:
            known += 1
        tops = [e["bbox"][1]]
        for h in heights:
            tops.append(tops[-1] + h)
        xs = [e["bbox"][0]]
        for w in widths:
            xs.append(xs[-1] + w)
        found = []
        for c in e.get("table_cells", []):
            paras = [p for p in c.get("paragraphs", []) if p.get("runs") and "".join(r["text"] for r in p["runs"]).strip()]
            if not paras or c["rowspan"] != 1 or c["colspan"] != 1 or c["row"] > known:
                continue
            aligns = {p.get("align", "left") for p in paras}
            if aligns not in ({"left"}, {"right"}) or any(p.get("bullet") or (p.get("level") or 0) for p in paras):
                continue
            side = aligns.pop()
            text = "".join(r["text"] for r in paras[0]["runs"])
            if side == "left" and text[:1].isspace() or side == "right" and text.rstrip("\n")[-1:].isspace():
                continue                    # words pushed in by spaces say nothing of the inset
            m = 1.5
            X0, X1 = int(np.ceil((xs[c["col"]] + m) * px)), int(np.floor((xs[c["col"] + 1] - m) * px))
            Y0, Y1 = int(np.ceil((tops[c["row"]] + m) * px)), int(np.floor((tops[c["row"] + 1] - m) * px))
            if X1 - X0 < 6 or Y1 - Y0 < 4 or X1 > W or Y1 > H:
                continue
            crop = thumb[Y0:Y1, X0:X1]
            ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
            ink = ((np.abs(crop - ground).max(axis=-1) > 80).sum(axis=0) >= 2)
            cols = np.nonzero(ink)[0]
            if len(cols) < 3:
                continue
            size = max((r.get("size") or 0) for p in paras for r in p["runs"])
            if side == "left":
                gap = X0 / px + cols[0] / px - xs[c["col"]]
            else:
                gap = xs[c["col"] + 1] - (X0 + cols[-1] + 1) / px
            found.append(gap - CELL_BEARING_EM * size)
        if len(found) >= CELL_PAD_MIN:
            e["cell_pad"] = [round(max(0.0, float(np.median(found))), 3), e["cell_pad"][1]]


CELL_LINE_MIN_EM = 0.3   # a band of inked rows this tall (em) at least is a line, not a dot or an accent


def thumbnail_cell_text(elements: list[dict], thumb, px: float) -> None:
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
    if thumb is None or not px:
        return
    import numpy as np
    from .emit import ASCENT_EM, LINE_EM
    H, W = thumb.shape[:2]
    for e in elements:
        heights, widths = e.get("row_heights"), e.get("col_widths")
        if e.get("kind") != "table" or not heights or not widths:
            continue
        fixed = set(e.get("rows_fixed", []))
        known = 0
        while known < len(heights) and known in fixed:
            known += 1
        tops = [e["bbox"][1]]
        for h in heights:
            tops.append(tops[-1] + h)
        xs = [e["bbox"][0]]
        for w in widths:
            xs.append(xs[-1] + w)
        found = []
        for c in e.get("table_cells", []):
            va = c.get("valign")
            paras = [p for p in c.get("paragraphs", []) if p.get("runs") and "".join(r["text"] for r in p["runs"]).strip()]
            if va not in ("top", "bottom") or len(paras) != 1 or c["rowspan"] != 1 or c["row"] >= len(heights):
                continue
            if c["row"] > known or (va == "bottom" and c["row"] >= known):
                continue                    # the row's top (bottom) is not where the thumbnail has it
            if any(p.get("bullet") for p in paras) or any(r.get("highlight") for p in paras for r in p["runs"]):
                continue
            z = max((r.get("size") or 0) for r in paras[0]["runs"])
            spacing = paras[0].get("line_spacing") or 1.0
            if z <= 0:
                continue
            m = 1.0
            X0 = int(np.ceil((xs[c["col"]] + m) * px))
            X1 = int(np.floor((xs[min(c["col"] + c["colspan"], len(widths))] - m) * px))
            Y0, Y1 = int(np.ceil((tops[c["row"]] + m) * px)), int(np.floor((tops[c["row"] + 1] - m) * px))
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
            base = (Y0 + band[0] + rows[-1] + 1) / px
            above = ASCENT_EM * z - (0 if spacing >= 1 else (1 - spacing) * 0.75 * LINE_EM * z)
            below = (LINE_EM - ASCENT_EM) * z + (spacing - 1) * LINE_EM * z if spacing >= 1 \
                else (LINE_EM - ASCENT_EM) * z - (1 - spacing) * 0.25 * LINE_EM * z
            found.append(base - above - tops[c["row"]] if va == "top" else tops[c["row"] + 1] - base - below)
        if len(found) >= CELL_PAD_MIN:
            e["cell_text_y"] = round(float(np.median(found)), 3)


def _find_row_line(thumb, px: float, segs: list[dict], xs: list[float], expected: float, stored: float,
                   H: int, W: int) -> float | None:
    """Where a row boundary's border runs in the thumbnail, in IR pt (see `thumbnail_rows`)."""
    import numpy as np
    cols, colours = [], []
    for b in segs:
        a0, a1 = int(np.ceil(xs[b["col"]] * px)) + 3, int(np.floor(xs[b["col"] + 1] * px)) - 3
        if a1 > a0:
            cols.append(np.arange(max(0, a0), min(W, a1)))
            colours.append(np.repeat(deck_fills_rgb(b["color"])[None, :], max(0, min(W, a1) - max(0, a0)), axis=0))
    if not cols:
        return None
    cols_ = np.concatenate(cols)
    want = np.concatenate(colours).astype(float)
    if len(cols_) < 6:
        return None
    thick = max(int(np.ceil(max(b["weight"] for b in segs) * px)) + 2, 3)
    y_from = max(thick + 2, int(np.floor((expected - 1.0) * px)))
    y_to = min(H - thick - 3, int(np.ceil((expected + 3 * stored + 20) * px)))

    def dist(y):
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


def deck_fills_rgb(hexstr: str):
    from .deck_fills import rgb
    return rgb(hexstr)


def thumbnail_insets(elements: list[dict], thumb, px: float) -> None:
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
    if thumb is None or not px:
        return
    import numpy as np
    for e in elements:
        box_ = e.get("box") if isinstance(e.get("box"), dict) else None
        if e.get("kind") != "text" or box_ is None or "insets" in box_:
            continue
        paras = [p for p in e.get("paragraphs", []) if p.get("runs")]
        if not paras or not any(r["text"].strip() for p in paras for r in p["runs"]):
            continue
        if any(p.get("align", "left") != "left" or p.get("bullet") or p.get("direction") == "rtl" for p in paras):
            # centred or bulleted words have no side edge to read, but their rows still show where the
            # top (bottom) inset put them
            if inset_rows(e, elements, paras, thumb, px) == 0:
                scale = box_.get("scale") or 1.0
                align = paras[0].get("align", "left")
                dx = {"left": 1.0, "right": -1.0}.get(align, 0.0) * PAD_X / scale
                _no_insets(e, dx, BASELINE_A / scale * _anchor_sign(box_))
            continue
        scale = box_.get("scale") or 1.0
        pad = PAD_X / scale
        x0, y0, x1, y1 = e["bbox"]
        indent = min(min(p["slides"].get("indent_first") or 0, p["slides"].get("indent_start") or 0)
                     for p in paras) / scale
        strip = (x0 - 1, y0, x0 + indent + pad + 2, y1)
        others = crossing(e, elements, strip)
        X0, X1 = int(round(x0 * px)), int(round(x1 * px))
        Y0, Y1 = int(round(y0 * px)), int(round(y1 * px))
        crop = thumb[max(0, Y0):max(0, Y1), max(0, X0):max(0, X1)]
        if crop.shape[0] < 4 or crop.shape[1] < 8:
            continue
        # What else reaches into the strip covers rows, not the box: gdg24's stat grids stack a heading
        # box over a caption box whose tops overlap by 5 pt, and its code slides lay a highlight bar
        # across the middle of the listing - none of those boxes was read. Their rows are left out; the
        # rest still shows where this box's words start - and when that leaves the first line out, the
        # top test below cannot pass.
        free = np.ones(crop.shape[0], dtype=bool)
        for o in others:
            a = max(0, int(np.floor((o["bbox"][1] - 1) * px)) - max(0, Y0))
            b = max(0, int(np.ceil((o["bbox"][3] + 1) * px)) - max(0, Y0))
            free[a:b] = False
        if free.sum() < 4:
            continue
        ground = np.median(crop[free].reshape(-1, crop.shape[-1]), axis=0)
        mark = (np.abs(crop - ground).max(axis=-1) > 80) & free[:, None]
        ink = mark.sum(axis=0) >= 2
        cols = np.nonzero(ink)[0]
        if len(cols) < 3:
            continue
        if cols[0] == 0:
            # Ink in the box's first pixel column is a glyph whose edge rounds onto the box edge (gdg24's
            # "Connect", 0.13 pt in) unless it goes on outside the box, where no word of it can be.
            if X0 < 3:
                continue
            left = thumb[max(0, Y0):max(0, Y1), X0 - 3:X0]
            if ((np.abs(left - ground).max(axis=-1) > 80) & free[:len(left), None]).any():
                continue
        # (the crop starts at the slide's edge when the box starts left of it: ap-bio-stats' full-width
        # boxes stand 1.9 pt off the slide)
        gap = (max(0, X0) + cols[0]) / px - (x0 + indent)
        # The first glyph's own bearing, where the deck's font is at hand: a big one alone can be
        # more than half the inset (devfest2020's 65 pt "Use over" starts 4.5 pt in with no inset)
        bearing = starting_bearing(paras)
        if gap >= pad / 2 + bearing:
            continue
        dy = 0.0
        if box_.get("valign", "top") in ("top", "bottom") and e.get("anchor"):
            # a box may have no side insets and still its top one (creandum-board's labels): the
            # first line's tops must stand where no top inset puts them too
            rows = inset_rows(e, elements, paras, thumb, px)
            if rows is None and box_.get("valign", "top") == "top":
                dy = (BASELINE_A - (PPTX_TITLE_DY if e.get("placeholder") in
                                    ("TITLE", "CENTERED_TITLE", "SUBTITLE") else 0.0)) / scale
                rows = np.nonzero(mark.sum(axis=1) >= 2)[0]
                z = max(r.get("size") or 0 for r in paras[0]["runs"])
                if not len(rows) or (Y0 + rows[0]) / px - (e["anchor"][1] - CAP_EM * z) > -dy / 2:
                    continue
            elif rows is not None:
                if rows != 0:
                    continue
                dy = BASELINE_A / scale * _anchor_sign(box_)
        _no_insets(e, pad, dy)


def _anchor_sign(box_: dict) -> float:
    """Which way a box's first baseline moves when its insets go: up in a top-aligned box, down in a
    bottom-aligned one, nowhere in a middle-aligned one."""
    return {"top": 1.0, "bottom": -1.0}.get(box_.get("valign", "top"), 0.0)


def _no_insets(e: dict, dx: float, dy: float) -> None:
    pad = PAD_X / (e["box"].get("scale") or 1.0)
    e["box"]["insets"] = 0
    e["wrap_width"] = round(e.get("wrap_width", 0) + 2 * pad, 2)
    if e.get("anchor"):
        e["anchor"] = [round(e["anchor"][0] - dx, 2), round(e["anchor"][1] - dy, 2)]


# ------------------------------------------------------------------------------------ glyph metrics

_FACES: dict = {}


def face_glyphs(font: str | None, bold: bool = False, italic: bool = False):
    """A function char -> (advance, xMin, yMin, xMax, yMax) in em (None: no glyph, or no outline) for
    a deck font found under its own name (on the machine or fetched, `adopt.font_family`), or None:
    a stand-in's outlines say nothing of where Slides' glyphs stand. Cached per face."""
    if not font:
        return None
    from .adopt import flatten
    key = (flatten(font), bool(bold), bool(italic))
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
        have = flatten(files.get("match") or "")
        if not (have.startswith(key[0]) or key[0].startswith(have)) or files.get("FontIndex"):
            return None
        style = ("BoldItalicFont" if italic else "BoldFont") if bold else ("ItalicFont" if italic else "UprightFont")
        f = TTFont(files.get(style) or files["UprightFont"], lazy=True)
        cmap, glyphs, hmtx, upem = f.getBestCmap(), f.getGlyphSet(), f["hmtx"], f["head"].unitsPerEm
    except Exception:                                   # noqa: BLE001 - no metrics is an answer
        return None
    seen: dict = {}

    def glyph(c: str):
        if c not in seen:
            name = cmap.get(ord(c))
            if name is None:
                seen[c] = None
            else:
                pen = BoundsPen(glyphs)
                glyphs[name].draw(pen)
                b = pen.bounds
                adv = hmtx[name][0] / upem
                seen[c] = (adv, *(v / upem for v in b)) if b else (adv, None, None, None, None)
        return seen[c]
    _FACES[key] = glyph
    return glyph


def _chars(p: dict):
    """(char, size, glyph metrics function) of a paragraph's text, in order."""
    for r in p["runs"]:
        g = face_glyphs(r.get("font"), bool(r.get("bold")), bool(r.get("italic")))
        for c in r["text"]:
            yield c, r.get("size") or 0.0, g


def starting_bearing(paras: list[dict]) -> float:
    """The smallest left side bearing (page pt) of the paragraphs' first glyphs, 0 when unknown."""
    out = None
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


def _lines(p: dict, width: float) -> list[list[tuple]] | None:
    """A paragraph broken into lines greedily at spaces within `width` (page pt), each line its
    (char, size, metrics) - None when a glyph's metrics are unknown."""
    lines, line, x, last_space = [], [], 0.0, None
    for c, z, g in _chars(p):
        if c in "\x0b\n":
            lines.append(line)
            line, x, last_space = [], 0.0, None
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


def inset_rows(e: dict, elements: list[dict], paras: list[dict], thumb, px: float) -> int | None:
    """What the thumbnail's rows say of a top- or bottom-aligned box's top (bottom) inset: 0 when its
    first line's ink tops (last line's ink bottoms) stand where no inset puts them, 1 where Slides'
    own inset does, None when it cannot tell (no metrics for the deck's own font, other things in the
    box, a middle-aligned box, a line a stand-in would break elsewhere). The glyphs' own heights say
    where the ink stands (a centred title has no side edge to read, and "Colors" in Space Mono does
    not reach the cap height of a generic face)."""
    import numpy as np
    from .adopt import WIDE_SPACING, line_box, para_size, snapped_line_box
    box_ = e["box"]
    valign = box_.get("valign", "top")
    if valign not in ("top", "bottom") or not e.get("anchor") or box_.get("font_scale", 1) not in (1, None):
        return None
    scale = box_.get("scale") or 1.0
    x0, y0, x1, y1 = e["bbox"]
    width = max(1.0, x1 - x0 - 2 * PAD_X / scale)
    p = paras[0] if valign == "top" else paras[-1]
    if any((r.get("script") or r.get("highlight") or r.get("underline") or r.get("strike")) for r in p["runs"]):
        return None
    # Arabic and Hebrew are shaped: a letter's joined form is not the glyph its code point maps to, so
    # the cmap's heights say nothing of the line's tops (arabic-training's lists read as inset-free)
    if p.get("direction") == "rtl" or any(unicodedata.bidirectional(c) in ("R", "AL")
                                           for r in p["runs"] for c in r["text"]):
        return None
    lines = _lines(p, width - ((p["slides"].get("indent_start") or 0) / scale))
    if not lines:
        return None
    line = [q for q in (lines[0] if valign == "top" else lines[-1]) if q[2][2] is not None]
    if not line:
        return None
    inset = BASELINE_A / scale
    # the first (last) baseline where adopt.text_box_latex sets it under Slides' own insets
    z, r = para_size(p), (p.get("slides") or {}).get("line_spacing") or 1.0
    if valign == "top":
        above = snapped_line_box(z, r, scale, bool(box_.get("snap")))[0]
        space = 0.0 if box_.get("grows") else ((p.get("slides") or {}).get("space_above") or 0) / scale
        base = y0 + inset + space + above
        edge = min(base - q[2][4] * q[1] for q in line)
    else:
        below = line_box(z, 1.0 if r >= WIDE_SPACING else r)[1]
        base = y1 - inset - below
        edge = max(base - q[2][2] * q[1] for q in line)
    # the rows the box's text covers, less rows other elements reach into
    m = 0.5 * inset
    top, bottom = (edge - inset - m, edge + m) if valign == "top" else (edge - m, edge + inset + m)
    # sideways, only where the line's words stand, with or without the side insets: devfest2020's
    # lists run 2 pt past the panel they stand on, which would count as crossing them
    w = sum(q[2][0] * q[1] for q in line)
    pad = PAD_X / scale
    sl = p.get("slides") or {}
    indent = max(sl.get("indent_start") or 0, sl.get("indent_first") or 0) / scale
    align = p.get("align", "left")
    if align == "center":
        mid = (x0 + x1) / 2 + (indent - (sl.get("indent_end") or 0) / scale) / 2
        band = (mid - w / 2 - pad, top, mid + w / 2 + pad, bottom)
    elif align == "right":
        band = (x1 - pad - w - pad, top, x1, bottom)
    else:
        band = (x0, top, x0 + pad + indent + w + pad, bottom)
    band = (max(x0, band[0]), top, min(x1, band[2]), bottom)
    x0, x1 = band[0], band[2]
    if crossed(e, elements, band):
        return None
    X0, X1 = int(round(max(0.0, x0) * px)), int(round(min(x1, thumb.shape[1] / px) * px))
    Y0, Y1 = int(round(top * px)), int(round(bottom * px))
    if Y0 < 0 or Y1 > thumb.shape[0] or X1 - X0 < 4:
        return None
    crop = thumb[Y0:Y1, X0:X1]
    ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
    rows = np.nonzero((np.abs(crop - ground).max(axis=-1) > 80).sum(axis=1) >= 2)[0]
    if not len(rows):
        return None
    seen = (Y0 + rows[0]) / px if valign == "top" else (Y0 + rows[-1] + 1) / px
    shifted = edge - inset if valign == "top" else edge + inset
    if abs(seen - shifted) <= INSET_TELL * inset:
        return 0
    if abs(seen - edge) <= INSET_TELL * inset:
        return 1
    return None


def ink_widths(elements: list[dict], thumb, px: float) -> None:
    """How wide the thumbnail shows the first line of each text box's first paragraph (`ink_width`,
    page pt, first ink column to last): `adopt.font_widths` holds them against the stand-in a deck's
    font is set in when this machine does not have it, for the paragraphs that fit on one line.
    Only a paragraph in one font, size and style, left-aligned, with no bullet, whose line nothing
    else crosses and whose words do not touch the box's sides."""
    if thumb is None or not px:
        return
    import numpy as np
    for e in elements:
        box_ = e.get("box") if isinstance(e.get("box"), dict) else None
        paras = [p for p in e.get("paragraphs", []) if p.get("runs")]
        if e.get("kind") != "text" or box_ is None or not paras or not e.get("anchor"):
            continue
        p = paras[0]
        runs = [r for r in p["runs"] if r["text"].strip()]
        text = "".join(r["text"] for r in p["runs"])
        if not runs or p.get("bullet") or p.get("align", "left") != "left" or p.get("direction") == "rtl" \
                or any(c in text for c in "\x0b\n\t") or len(text.strip()) < 4 \
                or len({(r.get("font"), bool(r.get("bold")), bool(r.get("italic")), r.get("size"),
                         r.get("script")) for r in runs}) != 1:
            continue
        x0, y0, x1, y1 = e["bbox"]
        z, base = runs[0].get("size") or 0, e["anchor"][1]
        band = (x0, base - 0.85 * z, x1, base + 0.3 * z)
        if z <= 0 or crossed(e, elements, band):
            continue
        X0, X1, Y0, Y1 = (int(round(v * px)) for v in (x0, x1, band[1], band[3]))
        if X0 < 0 or Y0 < 0 or X1 > thumb.shape[1] or Y1 > thumb.shape[0] or Y1 - Y0 < 3 or X1 - X0 < 8:
            continue
        crop = thumb[Y0:Y1, X0:X1]
        ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
        cols = np.nonzero((np.abs(crop - ground).max(axis=-1) > 80).any(axis=0))[0]
        if len(cols) < 3 or cols[0] <= 1 or cols[-1] >= crop.shape[1] - 2:
            continue
        e["ink_width"] = round((cols[-1] - cols[0] + 1) / px, 2)


BOLD_STROKE_EM = 0.10       # mean stroke width (em) above which a thumbnail's letters are bold


def stroke_em(e: dict, elements: list[dict], thumb, px: float) -> float | None:
    """The mean stroke width of a text box's letters in its thumbnail, in em of its biggest run (None:
    not readable: anything else reaches into the box, or too little ink). Twice the ink's area over
    its outline: a stroke w wide and L long has area wL and an outline of 2L. Antialiasing thickens
    small text, so it is only a tiebreak: the runs `thumbnail_weights` settles read 0.080-0.083 where
    drawn regular and 0.116-0.142 where drawn bold (whole boxes the API calls bold read 0.09-0.16,
    regular ones 0.05-0.13)."""
    import numpy as np
    runs = [r for p in e.get("paragraphs", []) for r in p.get("runs", []) if r["text"].strip()]
    if thumb is None or not px or not runs or crossed(e, elements, tuple(e["bbox"])):
        return None
    z = max(r.get("size") or 0 for r in runs)
    x0, y0, x1, y1 = (int(round(v * px)) for v in e["bbox"])
    crop = thumb[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
    if z <= 0 or crop.shape[0] < 4 or crop.shape[1] < 4:
        return None
    ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
    contrast = np.abs(crop - ground).max(axis=-1)
    ink = contrast > max(40.0, contrast.max() / 2)
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


def thumbnail_weights(elements: list[dict], thumb, px: float) -> None:
    """Settle the runs whose weight the API does not say (`weight_unsure`): a run that only names a
    font reads back as `bold: false` at weight 400 under a bold parent, and Slides draws some of those
    bold (jruby-ja's Tahoma titles, drawings-basics' title slides, solidity-survey) and some regular
    (drawings-basics' slides 9 and 11, identical in the API). The thumbnail's stroke width tells them
    apart (`stroke_em`); unreadable, the API's word stands."""
    for e in elements:
        runs = [r for p in e.get("paragraphs", []) for r in p.get("runs", []) if r.get("weight_unsure")]
        if not runs:
            continue
        w = stroke_em(e, elements, thumb, px)
        for r in runs:
            del r["weight_unsure"]
            if w is not None and w > BOLD_STROKE_EM:
                r["bold"] = True


def crossed(e: dict, elements: list[dict], strip: tuple) -> bool:
    """Does anything but the box itself reach into `strip`, where its thumbnail is read? What lies under
    the box, from its top down past the strip, does not: a panel it stands on, or the full-slide
    picture of a template's layout (devfest2020 draws every slide's ground as one, and none of its
    boxes could be read)."""
    return bool(crossing(e, elements, strip, first=True))


def crossing(e: dict, elements: list[dict], strip: tuple, first: bool = False) -> list[dict]:
    """The elements `crossed` asks about (only the first one with `first`)."""
    x0, y0, x1, y1 = e["bbox"]
    below = True
    found = []
    for o in elements:
        if o is e:
            below = False
            continue
        b = o.get("bbox")
        if not b or len(b) != 4 or b[2] <= strip[0] or b[0] >= strip[2] or b[3] <= strip[1] or b[1] >= strip[3]:
            continue
        # (sideways it need only hold the strip: devfest2020's "50%" box runs 800 pt off the slide's
        # right edge, and no panel under it reaches past that)
        if (o.get("kind") == "shape" or below and o.get("kind") == "image") and b[0] < max(x0, strip[0]) - 1 \
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


def top_drift(e: dict, elements: list[dict], thumb, px: float) -> float | None:
    """Where the slide's thumbnail shows a top-aligned box's first capital, less where Slides' default
    insets put it, in Slides pt (None: not measurable here). Only a box nothing else reaches into
    above its first baseline, whose first word opens on a capital or digit (a lowercase start would
    read an x-height as a cap)."""
    import numpy as np
    box_ = e.get("box") if isinstance(e.get("box"), dict) else None
    if thumb is None or e.get("kind") != "text" or box_ is None or "insets" in box_ or not e.get("anchor") \
            or box_.get("valign", "top") != "top" or e.get("placeholder") in ("TITLE", "CENTERED_TITLE", "SUBTITLE"):
        return None
    paras = [p for p in e.get("paragraphs", []) if p.get("runs")]
    first = "".join(r["text"] for r in paras[0]["runs"]).lstrip() if paras else ""
    if not first:
        return None
    if paras[0].get("bullet"):
        # a bullet's glyph sits beside the line, not over it; only a baseline says where the line is
        # (arabic-training's bulleted Arial and Times boxes read -3.7 and -4.0)
        return baseline_drift(e, elements, paras, thumb, px)
    cap = KNOWN_CAPS.get(paras[0]["runs"][0].get("font") or "")
    if cap is None or not (first[0].isupper() or first[0].isdigit()):
        return baseline_drift(e, elements, paras, thumb, px)
    x0, y0, x1, y1 = e["bbox"]
    base = e["anchor"][1]
    if crossed(e, elements, (x0, y0 - 2, x1, base + 2)):
        return None
    z = max(r.get("size") or 0 for r in paras[0]["runs"])
    X0, X1, Y0, Y1 = (int(round(v * px)) for v in (x0, x1, y0, base + 1))
    crop = thumb[max(0, Y0):max(0, Y1), max(0, X0):max(0, X1)]
    if z <= 0 or crop.shape[0] < 4 or crop.shape[1] < 8:
        return None
    ground = np.median(crop.reshape(-1, crop.shape[-1]), axis=0)
    rows = np.nonzero((np.abs(crop - ground).max(axis=-1) > 80).sum(axis=1) >= 2)[0]
    if not len(rows):
        return None
    scale = box_.get("scale") or 1.0
    return ((Y0 + rows[0]) / px - (base - cap * z)) * scale


def side_gap(e: dict, elements: list[dict], thumb, px: float) -> float | None:
    """How far the thumbnail shows a box's words from the side they start on, in Slides pt: the box's
    left edge for left-aligned left-to-right text, its right edge for right-aligned right-to-left text,
    each less the paragraphs' indent (None: not measurable). That is the side inset plus the first
    glyph's bearing. Rows another element reaches into are not read."""
    import numpy as np
    box_ = e.get("box") if isinstance(e.get("box"), dict) else None
    if thumb is None or not px or e.get("kind") != "text" or box_ is None or "insets" in box_:
        return None
    paras = [p for p in e.get("paragraphs", []) if p.get("runs") and any(r["text"].strip() for r in p["runs"])]
    if not paras or any(p.get("bullet") for p in paras):
        return None
    rtl = {p.get("direction") == "rtl" for p in paras}
    if len(rtl) != 1:
        return None
    rtl = rtl.pop()
    # centred lines stand further in than the start-aligned ones, so they may be read along
    start = "right" if rtl else "left"
    if any(p.get("align", "left") not in (start, "center") for p in paras) \
            or not any(p.get("align", "left") == start for p in paras):
        return None
    scale = box_.get("scale") or 1.0
    indent = min(min(p["slides"].get("indent_first") or 0, p["slides"].get("indent_start") or 0)
                 for p in paras) / scale
    x0, y0, x1, y1 = e["bbox"]
    reach = (SIDE_READ + indent * scale) / scale
    strip = (x1 - reach, y0, x1 + 1, y1) if rtl else (x0 - 1, y0, x0 + reach, y1)
    X0, X1 = int(round(strip[0] * px)), int(round(strip[2] * px))
    Y0, Y1 = int(round(y0 * px)), int(round(y1 * px))
    if X0 < 0 or Y0 < 0 or X1 > thumb.shape[1] or Y1 > thumb.shape[0]:
        return None
    crop = thumb[Y0:Y1, X0:X1]
    if crop.shape[0] < 4 or crop.shape[1] < 8:
        return None
    free = np.ones(crop.shape[0], dtype=bool)
    for o in crossing(e, elements, strip):
        a = max(0, int(np.floor((o["bbox"][1] - 1) * px)) - Y0)
        b = max(0, int(np.ceil((o["bbox"][3] + 1) * px)) - Y0)
        free[a:b] = False
    if free.sum() < 4:
        return None
    ground = np.median(crop[free].reshape(-1, crop.shape[-1]), axis=0)
    ink = (((np.abs(crop - ground).max(axis=-1) > 80) & free[:, None]).sum(axis=0)) >= 2
    cols = np.nonzero(ink)[0]
    if len(cols) < 3:
        return None
    edge = (X0 + cols[-1] + 1) / px if rtl else (X0 + cols[0]) / px
    return float(((x1 - indent - edge) if rtl else (edge - x0 - indent)) * scale)


SNAP_PAGE = 960.0           # pages up to this wide (Slides pt) set single-spaced lines on whole pixels (box `snap`, adopt.snapped_line_box)
SIDE_READ = 14.0            # Slides pt from a box's side that `side_gap` reads
SIDE_BEARING_EM = 0.04      # a first glyph's side bearing, near enough (Times' Hebrew 0.02-0.05, Arial ~0.07)
# `side_inset` below this is PowerPoint's 3.6 pt, above it Slides' own ~6.7: on the corpus the boxes of
# comps-analysis read 3.1-4.2 and those of every deck with Slides' sides 5.5-8 (medians 6.2-7.6)
SIDE_SPLIT = 5.15


def side_inset(e: dict, gap: float) -> float:
    """The side inset a box's `side_gap` shows: the gap less its first glyph's bearing."""
    z = max((r.get("size") or 0) for p in e.get("paragraphs", []) for r in p.get("runs", []))
    return gap - SIDE_BEARING_EM * z * ((e.get("box") or {}).get("scale") or 1.0)


BASELINE_BAND = (0.95, 0.3)     # em above and below the predicted first baseline `baseline_drift` reads
BASELINE_MIN_COLUMNS = 0.6      # em of inked columns a first line needs before its baseline is read
BASELINE_DENSITY = 0.25         # the baseline: the lowest row inked this much of the line's densest one


def baseline_drift(e: dict, elements: list[dict], paras: list[dict], thumb, px: float) -> float | None:
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
    import numpy as np
    from .scripts import script_of
    runs = paras[0]["runs"]
    first = next((c for r in runs for c in r["text"] if c.isalpha()), "")
    if not first or script_of(first) not in ("hebrew", "arabic") or any(r.get("highlight") for r in runs):
        return None
    z = max(r.get("size") or 0 for r in runs)
    if z <= 0:
        return None
    x0, y0, x1, y1 = e["bbox"]
    base = e["anchor"][1]
    top, bottom = base - BASELINE_BAND[0] * z, base + BASELINE_BAND[1] * z
    if crossed(e, elements, (x0, min(y0, top) - 2, x1, bottom + 2)):
        return None
    X0, X1, Y0, Y1 = (int(round(v * px)) for v in (x0, x1, top, bottom))
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
    return float((seen - base) * (e["box"].get("scale") or 1.0))


def pptx_insets(slides: list[dict], drifts: list[tuple[dict, float]], sides: list[float] = ()) -> None:
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
    none to go by, a deck imported whole gets 3.6."""
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
    sides = [x for x in sides if x > 0]
    narrow = not sides or statistics.median(sides) < SIDE_SPLIT
    measured = {id(e) for e, _ in drifts}
    chosen = {id(e) for e in hits}
    for s in slides:
        for e in s["elements"]:
            box_ = e.get("box") if isinstance(e.get("box"), dict) else None
            if e.get("kind") != "text" or box_ is None or "insets" in box_:
                continue
            if id(e) not in chosen and (id(e) in measured or not whole):
                continue
            box_["inset_y"] = PPTX_INSET_Y
            if whole and narrow:
                # a deck imported whole keeps the .pptx's side insets too, 3.6 pt like the top ones:
                # comps-analysis's text starts 3.1-3.8 pt left of Slides' 6.7 and wrapped every
                # other line early (boxes 0.43 -> 0.66); ap-bio-stats' two lone boxes measured at
                # -3.6 keep Slides' sides (their titles stand where 6.7 pt puts them)
                box_["inset_x"] = PPTX_INSET_Y
            if e.get("anchor") and box_.get("valign", "top") in ("top", "bottom"):
                dy = -want / (box_.get("scale") or 1.0) * (1 if box_.get("valign", "top") == "bottom" else -1)
                e["anchor"] = [e["anchor"][0], round(e["anchor"][1] + dy, 2)]
