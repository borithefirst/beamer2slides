"""Fills Slides draws but its API cannot say, read back from the slide's own picture.

The API describes a fill only as a `solidFill`. A gradient, picture or texture fill - what a .pptx
import brings, and what SlidesCarnival's freeform decorations, cs161's black-to-red header bar and
hebrew-lesson's paper backdrop are - comes back as `shapeBackgroundFill: {}`: drawn, with nothing
said about how. A table cell a .pptx table style colours comes back NOT_RENDERED, the style being
nowhere in the answer at all. Placeholder inheritance does not help: across the adopt corpus every
`INHERIT` fill ends on a parent that is NOT_RENDERED itself. So the only witness is Google's own
thumbnail of the slide, and `deck_ir(foreign=True, thumbnails=...)` asks it:

- a candidate (`fill_unread` on a shape or text box, or on a table cell) whose box is one flat colour
  in the thumbnail gets that colour (`fill_source: thumbnail`);
- a rectangle whose rows or columns are each flat, changing smoothly from one side to the other, gets
  a `fill_gradient` (axis and three stops, which is what TikZ's axis shading takes);
- anything else keeps no fill, and a shape with neither fill nor outline is dropped as before.

A false fill is worse than a missing one - it paints over whatever lies under the element - so the
reading is conservative. Pixels under opaque elements drawn above (pictures, filled shapes, tables)
are not looked at; everything else in the box must be the one colour, except ink inside the boxes
of texts drawn above it or of the element's own words (letters on a panel are not the panel). Ink of
anything *under* the element breaks the flatness, and the fill is refused, because painting it would
hide that ink - unless nothing but a plain background lies under the element, when off-colour pixels
can only be its own (a texture's printed border). A colour equal to the slide's background, or to all
that stands around the box (its edges would show nowhere), adds nothing and is not written either.
Elements are settled from the top down, so one settled above hides its box from those under it.

Without thumbnails (the offline tests, `pull`) candidates are simply dropped, exactly as before."""

from __future__ import annotations

import numpy as np

TOL = 14                 # max channel distance of a pixel from the region's colour (JPEG-ish noise, AA)
FLAT = 0.72              # share of the looked-at pixels that must be the colour (the rest: words above)
FLAT_WORDS = 0.8         # ... when the box holds its own words, which may be anywhere in it
STRAY = 0.012           # share of off-colour pixels allowed outside any text above (antialiased rims)
SEEN = 0.3               # share of the box that must be visible (not under opaque elements above)
GRAD_FLAT = 0.72         # a gradient: share of pixels within TOL of the three-stop ramp
GRAD_SPAN = 40           # ... whose ends differ by at least this much (max channel)
MARGIN_PX = 3            # rim left out of the box: antialiasing, outlines, borders
RING_PX = 6              # width of the ring around a box whose colour is what stands under it
EDGE_SAME = 0.5          # a box whose ring is this much its own colour has no visible edge


def load(image) -> np.ndarray | None:
    """An RGB array from a path, a PIL image or an array (None stays None)."""
    if image is None:
        return None
    if isinstance(image, np.ndarray):
        return image[..., :3].astype(np.int16)
    from PIL import Image
    im = image if isinstance(image, Image.Image) else Image.open(image)
    return np.asarray(im.convert("RGB")).astype(np.int16)


def hexcolour(c) -> str:
    return "#" + "".join(f"{int(round(v)):02x}" for v in c)


def rgb(hexstr: str | None):
    if not hexstr or len(hexstr) != 7:
        return None
    return np.array([int(hexstr[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.int16)


def px_box(bbox, px: float, w: int, h: int, margin: int = 0) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox
    a0 = min(w, max(0, int(np.ceil(x0 * px)) + margin))
    b0 = min(h, max(0, int(np.ceil(y0 * px)) + margin))
    a1 = max(0, min(w, int(np.floor(x1 * px)) - margin))
    b1 = max(0, min(h, int(np.floor(y1 * px)) - margin))
    return a0, b0, max(a0, a1), max(b0, b1)


_SEE_THROUGH: dict[str, bool] = {}


def see_through(file: str | None) -> bool:
    """A picture with transparent pixels (hebrew-lesson's ornamental frame over its paper backdrop):
    what is under it shows, so it is ink above a candidate rather than a cover. Unknown = opaque."""
    if not file:
        return False
    if file not in _SEE_THROUGH:
        try:
            from PIL import Image
            with Image.open(file) as im:
                if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
                    alpha = np.asarray(im.convert("RGBA"))[..., 3]
                    _SEE_THROUGH[file] = bool((alpha < 250).mean() > 0.01)
                else:
                    _SEE_THROUGH[file] = False
        except OSError:
            _SEE_THROUGH[file] = False
    return _SEE_THROUGH[file]


def opaque(el: dict) -> bool:
    """Drawn above a candidate, does this element hide what is under it?"""
    if el.get("fill_unread") and not el.get("fill"):
        return False                      # not settled yet (or never): may be see-through
    if el.get("fill_gradient") and el.get("role") != "line":
        return True
    if el["kind"] == "image":
        return not see_through(el.get("file"))
    if el["kind"] == "table":
        return True
    if el["kind"] in ("shape", "text") and el.get("fill") and (el.get("fill_alpha") or 1.0) >= 0.99:
        return el.get("role") != "line"
    return False


def wordy(el: dict) -> bool:
    """Ink above a candidate that may be anywhere in its box: words, a table, a see-through picture."""
    return (el["kind"] == "text" and bool(el.get("paragraphs")) or el["kind"] == "table"
            or el["kind"] == "image" and see_through(el.get("file")))


def read_region(a: np.ndarray, region: np.ndarray, allow: np.ndarray, gradient_ok: bool, flat: float = FLAT):
    """What fills the pixels `region` of `a` (the box less what hides it): ("solid", colour),
    ("gradient", axis, [three colours]) or None. `allow`: where ink of words drawn above may be."""
    n = int(region.sum())
    if n == 0:
        return None
    px = a[region]
    med = np.median(px, axis=0)
    close = np.abs(a - med).max(axis=2) <= TOL
    within = close & region
    stray = region & ~close & ~allow
    if within.sum() >= flat * n and stray.sum() <= STRAY * n:
        return ("solid", med)
    if not gradient_ok:
        return None
    ys, xs = np.nonzero(region)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub, reg, alw = a[y0:y1, x0:x1], region[y0:y1, x0:x1], allow[y0:y1, x0:x1]
    n2 = int(reg.sum())
    for axis in ("x", "y"):
        # three stops, each the median of the pixels looked at in a band of columns (axis x) or rows
        # (axis y) at the start, the middle and the end - then the ramp TikZ draws through them must
        # explain the box, as the flat colour must above
        lines = sub.transpose(1, 0, 2) if axis == "x" else sub
        lreg = reg.T if axis == "x" else reg
        k = lines.shape[0]
        if k < 24:
            continue
        band = max(2, k // 25)
        stops = []
        for lo in (0, k // 2 - band // 2, k - band):
            m = lreg[lo:lo + band]
            if not m.any():
                break
            stops.append(np.median(lines[lo:lo + band][m], axis=0))
        if len(stops) < 3 or np.abs(stops[2] - stops[0]).max() < GRAD_SPAN:
            continue
        t = (np.arange(k) + 0.5) / k
        at = np.where(t[:, None] < 0.5, stops[0] + (stops[1] - stops[0]) * (t[:, None] / 0.5),
                      stops[1] + (stops[2] - stops[1]) * ((t[:, None] - 0.5) / 0.5))
        model = at[:, None, :] if axis == "y" else at[None, :, :]
        close = np.abs(sub - model).max(axis=2) <= TOL
        if (close & reg).sum() >= max(GRAD_FLAT, flat) * n2 and (reg & ~close & ~alw).sum() <= STRAY * n2:
            return ("gradient", axis, stops)
    return None


def settle(elements: list[dict], image, px: float, background: str | None, picture: bool = False) -> list[dict]:
    """Give the slide's unread fills what its thumbnail shows (see the module docstring), and drop
    the shapes left with nothing to draw. `px`: thumbnail pixels per IR pt; `background`: the slide's
    background colour, `picture`: it has a background picture instead."""
    a = load(image)
    out = []
    # top down: an element settled above another hides it from the looking, so what a squiggle
    # tile under an unread wave shows is the wave's colour, never taken for the tile's own
    for k in range(len(elements) - 1, -1, -1):
        el = elements[k]
        cells = [c for c in el.get("table_cells", []) if c.pop("fill_unread", False)]
        if not el.get("fill_unread") and not cells:
            out.append(el)
            continue
        unread = el.pop("fill_unread", False)
        if a is not None:
            above = elements[k + 1:]
            if unread:
                bottom = not picture and not any(overlaps(e, el) for e in elements[:k])
                sample_element(a, el, above, px, background, bottom)
            around = outside(a, el["bbox"], px) if cells else None
            for c in cells:
                sample_cell(a, el, c, above, px, background, around)
        if el["kind"] == "shape" and not el.get("fill") and not el.get("fill_gradient") and not el.get("outline"):
            continue                       # nothing to draw after all, as before this module
        out.append(el)
    return out[::-1]


def masks(a: np.ndarray, box, above: list[dict], px: float, own_words: bool):
    """(the pixels of the box `box`, the part of it looked at, where ink not the fill's may be in it:
    all of it when `own_words`)."""
    h, w = a.shape[:2]
    a0, b0, a1, b1 = box
    sub = a[b0:b1, a0:a1]
    region = np.ones(sub.shape[:2], dtype=bool)
    allow = np.full(sub.shape[:2], own_words, dtype=bool)
    for e in above:
        c0, d0, c1, d1 = px_box(e["bbox"], px, w, h)
        if c1 <= a0 or c0 >= a1 or d1 <= b0 or d0 >= b1:
            continue
        if opaque(e):
            # its own rim is antialiased against what is under it: leave a little more out
            c0, d0, c1, d1 = px_box(e["bbox"], px, w, h, -MARGIN_PX)
            region[max(0, d0 - b0):max(0, d1 - b0), max(0, c0 - a0):max(0, c1 - a0)] = False
        elif wordy(e):
            c0, d0, c1, d1 = px_box(e["bbox"], px, w, h, -2 * MARGIN_PX)
            allow[max(0, d0 - b0):max(0, d1 - b0), max(0, c0 - a0):max(0, c1 - a0)] = True
    return sub, region, allow


def edges_show(a: np.ndarray, bbox, px: float, colour) -> bool:
    """Does a box of `colour` stand out from what is just around it? When most of the ring around
    it is that colour too, the element's edges are nowhere to be seen: either it is not filled at
    all and shows what it stands on (sc-memphis' squiggle tiles over the pale wave), or its fill
    adds nothing a reader could see - both are no reason to draw a panel."""
    h, w = a.shape[:2]
    inner = px_box(bbox, px, w, h)
    o0, p0, o1, p1 = px_box(bbox, px, w, h, -RING_PX)
    m = np.ones((p1 - p0, o1 - o0), dtype=bool)
    m[inner[1] - p0:inner[3] - p0, inner[0] - o0:inner[2] - o0] = False
    if m.sum() < 0.25 * RING_PX * 2 * ((inner[2] - inner[0]) + (inner[3] - inner[1])):
        return True                        # the box is (nearly) the page: little around it to compare with
    ring_px = a[p0:p1, o0:o1][m]
    return (np.abs(ring_px - np.asarray(colour)).max(axis=1) <= TOL).mean() < EDGE_SAME


def outside(a: np.ndarray, bbox, px: float):
    """The colour the page mostly is outside a box: what a table's unfilled cells show. A ring just
    around a table is no good for that - its own outer borders and a row grown past the stored
    height run through it (comps-analysis' tables on a #444444 page)."""
    h, w = a.shape[:2]
    a0, b0, a1, b1 = px_box(bbox, px, w, h)
    m = np.ones((h, w), dtype=bool)
    m[b0:b1, a0:a1] = False
    if m.sum() < 0.1 * m.size:
        return None
    q = (a[m][::7] // 4).astype(np.int32)
    packed = (q[:, 0] << 12) | (q[:, 1] << 6) | q[:, 2]
    top = int(np.bincount(packed).argmax())
    return np.array([(top >> 12) * 4 + 2, ((top >> 6) & 63) * 4 + 2, (top & 63) * 4 + 2], dtype=np.int16)


def worth(colour, background: str | None, around=None) -> bool:
    """Is a read colour a fill at all? Not when it is what shows around the element anyway - the
    slide's background, or the flat colour the element stands on (a table's cells over a picture
    backdrop read as that picture): a transparent element reads the same, and drawing it would add
    a panel the deck does not have."""
    for c in (rgb(background), around):
        if c is not None and np.abs(np.asarray(colour) - c).max() <= TOL:
            return False
    return True


def overlaps(e: dict, el: dict) -> bool:
    x0, y0, x1, y1 = e["bbox"]
    u0, v0, u1, v1 = el["bbox"]
    return x0 < u1 and u0 < x1 and y0 < v1 and v0 < y1


def sample_element(a, el: dict, above: list[dict], px: float, background: str | None,
                   bottom: bool = False) -> None:
    """`bottom`: nothing is drawn under the element but the page's colour, so no ink it could hide -
    off-colour pixels no element above explains are then its own fill's (hebrew-lesson's paper
    texture carries an ornamental border in the picture itself) and only flatness counts."""
    h, w = a.shape[:2]
    if el.get("frame") and (abs((el["frame"].get("rotation") or 0.0)) > 0.05 or el["frame"].get("shear")):
        return                             # a turned shape's box is not its face
    box = px_box(el["bbox"], px, w, h, MARGIN_PX)
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return
    sub, region, allow = masks(a, box, above, px, wordy(el) or bottom)
    if region.sum() < SEEN * region.size:
        return
    kind = (el.get("shape_type") or el.get("shape") or "").upper()
    if bottom and not wordy(el) and (page := rgb(background)) is not None:
        # the stray check is waived, but not for the page itself showing in the box: a squiggle tile
        # half over sc-memphis' wave is 70% pink and 30% page, and is no pink panel (hebrew-lesson's
        # leather backdrop darkens to its edges, which is its own texture, not the page)
        bare = region & (np.abs(sub - page).max(axis=2) <= TOL)
        if bare.sum() > STRAY * region.sum():
            return
    got = read_region(sub, region, allow, kind in ("RECTANGLE", "TEXT_BOX", "CUSTOM", ""),
                      FLAT_WORDS if wordy(el) else FLAT)
    if got is None:
        return
    if got[0] == "solid":
        if worth(got[1], background) and edges_show(a, el["bbox"], px, got[1]):
            el["fill"] = hexcolour(got[1])
            el["fill_source"] = "thumbnail"
            el.pop("fill_alpha", None)
    else:
        el["fill_gradient"] = {"axis": got[1], "colors": [hexcolour(c) for c in got[2]]}
        el["fill_source"] = "thumbnail"


def sample_cell(a, table: dict, cell: dict, above: list[dict], px: float, background: str | None,
                around=None) -> None:
    h, w = a.shape[:2]
    x0, y0 = table["bbox"][:2]
    cw, rh = table.get("col_widths") or [], table.get("row_heights") or []
    r, c = cell["row"], cell["col"]
    if r + cell.get("rowspan", 1) > len(rh) or c + cell.get("colspan", 1) > len(cw):
        return
    # Rows grow to their text beyond the stored heights; the stored top is right, and only the
    # stored height is looked at (a grown row keeps more of the same colour below it).
    bx = [x0 + sum(cw[:c]), y0 + sum(rh[:r]), x0 + sum(cw[:c + cell.get("colspan", 1)]),
          y0 + sum(rh[:r + cell.get("rowspan", 1)])]
    box = px_box(bx, px, w, h, MARGIN_PX)
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return
    sub, region, allow = masks(a, box, above, px, True)
    if region.sum() < SEEN * region.size:
        return
    got = read_region(sub, region, allow, False, FLAT_WORDS)
    if got and worth(got[1], background, around):
        cell["fill"] = hexcolour(got[1])
        cell["fill_alpha"] = None
        cell["fill_source"] = "thumbnail"
