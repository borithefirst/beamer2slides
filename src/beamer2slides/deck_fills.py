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
- anything else - a photo cut to a freeform, a texture - is written, when the caller gives a folder,
  as the thumbnail's own picture of the box (`thumbnail_picture`: letters of texts above painted out,
  the page colour round it transparent); without a folder a shape with neither fill nor outline is
  dropped as before.

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


def bake_shared_cluster(elements: list[dict], members: list[int], a: np.ndarray, px: float,
                        background: str | None, picture: bool, pictures) -> list[dict]:
    """Replace a `deck_freeforms.shared_boxes` cluster - freeform siblings that all declare the same
    box - with one picture of the thumbnail's own pixels in that box (`thumbnail_picture`), in place
    of tracing each alone into an opaque rectangle the size of the whole group. The picture goes
    where the cluster's *source group* begins, under every one of its own siblings (labels, an inner
    picture) still drawn natively above it, and their letters are painted out of it so nothing is
    printed twice. Left as it was when the crop refuses (too small, or nothing but the page)."""
    group_id = elements[members[0]].get("group")
    box = elements[members[0]]["bbox"]
    member_set = set(members)
    start = min((i for i, e in enumerate(elements) if e.get("group") == group_id), default=members[0])
    bottom = not picture and not any(overlaps(e, {"bbox": box})
                                     for i, e in enumerate(elements) if i < start)
    above = [e for i, e in enumerate(elements) if i >= start and i not in member_set]
    pic = thumbnail_picture(a, {"bbox": box}, above, px, background if bottom else None, pictures)
    if pic is None:
        return elements
    pic["id"] = f"{group_id}~arrows" if group_id else pic.get("id")
    if group_id:
        pic["group"] = group_id
    rest = [e for i, e in enumerate(elements) if i not in member_set]
    insert_at = sum(1 for i in range(start) if i not in member_set)
    return rest[:insert_at] + [pic] + rest[insert_at:]


def shape_fallback_picture(a: np.ndarray, el: dict, above: list[dict], px: float,
                           background: str | None, pictures):
    """A shape whose Slides shapeType `adopt_shapes.preset` has no geometry for (a curved or bent
    block arrow, or any other preset the corpora have not yet needed) drawn as the thumbnail's own
    pixels in its box, rather than `shape_block`'s silent RECTANGLE fallback (a rotated
    CURVED_UP_ARROW came out a solid blue diamond: the rotated rectangle). The box is already the
    shape's rotated bounding box (`bbox`), so the crop needs no un-rotating - it is pasted back
    axis-aligned, exactly where the thumbnail showed it. Words drawn above it are painted out of the
    crop like any other thumbnail fill (`thumbnail_picture`); refused (too small, or the shape
    turned out to show nothing of its own) leaves the element to `shape_block`'s own fallback."""
    kind = el.get("shape_type")
    if not kind or el.get("role") == "line":
        return None
    from . import adopt_shapes
    if adopt_shapes.known_preset(kind.upper()):
        return None
    pic = thumbnail_picture(a, el, above, px, background, pictures)
    if pic is None:
        return None
    pic["id"] = f"{el['id']}~shape" if el.get("id") else pic.get("id")
    if el.get("group"):
        pic["group"] = el["group"]
    return pic


def settle(elements: list[dict], image, px: float, background: str | None, picture: bool = False,
           pictures=None) -> list[dict]:
    """Give the slide's unread fills what its thumbnail shows (see the module docstring), and drop
    the shapes left with nothing to draw. `px`: thumbnail pixels per IR pt; `background`: the slide's
    background colour, `picture`: it has a background picture instead. `pictures`: a folder where a
    fill that is no colour at all (a photo cut to a freeform) is written as the thumbnail's picture
    of it (`thumbnail_picture`); without one such shapes are dropped."""
    from . import deck_freeforms
    a = load(image)
    if a is not None and pictures is not None:
        for members in deck_freeforms.shared_boxes(elements):
            elements = bake_shared_cluster(elements, members, a, px, background, picture, pictures)
    out = []
    # top down: an element settled above another hides it from the looking, so what a squiggle
    # tile under an unread wave shows is the wave's colour, never taken for the tile's own
    for k in range(len(elements) - 1, -1, -1):
        el = elements[k]
        if a is not None and pictures is not None and el["kind"] == "shape":
            bottom = not picture and not any(overlaps(e, el) for e in elements[:k])
            pic = shape_fallback_picture(a, el, elements[k + 1:], px, background if bottom else None, pictures)
            if pic is not None:
                out.append(pic)
                continue
        if a is not None and el["kind"] == "shape" and (el.get("shape_type") or "").upper() == "PIE" \
                and not el.get("fill_unread"):
            angles = pie_angles(a, el, elements[k + 1:], px)
            if angles is not None:
                el["pie"] = list(angles)
        if a is not None and el["kind"] == "shape" and not el.get("fill_unread") \
                and (el.get("shape_type") or "").upper() in ROUNDED_CORNERS:
            r = corner_radius(a, el, elements[k + 1:], px)
            if r is not None:
                el["corner_radius"] = r
        if a is not None and pictures is not None and el.get("video") and not el.get("file"):
            # a Drive video's poster frame, which no API gives, is on the slide's thumbnail
            pic = thumbnail_picture(a, el, elements[k + 1:], px, None, pictures)
            if pic is not None:
                el.update(file=pic["file"], sha1=pic["sha1"], format="png", poster="thumbnail")
        cells = [c for c in el.get("table_cells", []) if c.pop("fill_unread", False)]
        # a freeform's geometry is not in the API either: traced from the same picture
        # (`deck_freeforms`), after its fill is known
        free = a is not None and deck_freeforms.freeform(el)
        if not el.get("fill_unread") and not cells:
            if free:
                bottom = not picture and not any(overlaps(e, el) for e in elements[:k])
                deck_freeforms.trace(a, el, elements[k + 1:], elements[:k], px, background, bottom, False)
            out.append(el)
            continue
        unread = el.pop("fill_unread", False)
        if a is not None:
            above = elements[k + 1:]
            if unread:
                bottom = not picture and not any(overlaps(e, el) for e in elements[:k])
                sample_element(a, el, above, px, background, bottom)
                if free:
                    deck_freeforms.trace(a, el, above, elements[:k], px, background, bottom, True)
            around = outside(a, el["bbox"], px) if cells else None
            for c in cells:
                sample_cell(a, el, c, above, px, background, around)
        if el["kind"] == "text" and el.pop("_no_ramp", False) and el.get("paragraphs"):
            # china-pptx's tall panel: a rectangular/diagonal .pptx shading `read_region`'s single-axis
            # ramp cannot fit at all (neither "x" nor "y" reaches half its pixels). Only this - no flat
            # colour and no ramp explain the box at all - is a candidate: a box that read as flat but
            # matched the background (`worth`), or mostly hidden or bare page, is left exactly as before,
            # or every ordinary transparent text box would get a needless picture behind it (comps-
            # analysis: NOT_RENDERED with a bogus white `solidFill` is *also* how a placeholder with no
            # fill at all comes back). The text stays native and keeps no fill of its own; its background
            # is a picture of the thumbnail's own pixels behind it, with its own words (not just an
            # element drawn above it) painted out - the same trick `thumbnail_picture` uses for a
            # shape's letters, pointed at itself.
            if a is not None and pictures is not None:
                bottom = not picture and not any(overlaps(e, el) for e in elements[:k])
                pic = thumbnail_picture(a, el, elements[k + 1:] + [el], px,
                                        background if bottom else None, pictures)
                if pic is not None:
                    pic.pop("object", None)
                    pic.pop("key", None)
                    pic["id"] = f"{el['id']}~fill" if el.get("id") else pic.get("id")
                    out.append(el)
                    out.append(pic)         # the panel behind its own words (appended after: out is
                    continue                # built top-down and reversed at the end, so this is under it
        if el["kind"] == "shape" and unread and not el.get("fill") and not el.get("fill_gradient"):
            el["_unsaid"] = True           # drawn in the picture as nothing we can say: see deck_freeforms
            if a is not None and pictures is not None and not el.get("trace"):
                bottom = not picture and not any(overlaps(e, el) for e in elements[:k])
                pic = thumbnail_picture(a, el, elements[k + 1:], px, background if bottom else None, pictures)
                if pic is not None:
                    if el.get("outline"):
                        out.append({**el, "fill": None})   # its outline, drawn above the picture
                    out.append(pic)
                    continue
        if el["kind"] == "shape" and not el.get("fill") and not el.get("fill_gradient") and not el.get("outline"):
            continue                       # nothing to draw after all, as before this module
        out.append(el)
    for el in elements:
        el.pop("_traced", None)            # the traced pixels, kept only while settling
        el.pop("_unsaid", None)
        el.pop("_no_ramp", None)
    return out[::-1]


PIE_STEP = 0.25          # degrees between the rays a pie's angles are read on
PIE_RADII = (0.3, 0.45, 0.6, 0.75, 0.9)   # where along each ray, in the radius
PIE_STRAY = 2.0          # degrees of other colour allowed inside the arc read (letters, leader lines)


def pie_angles(a: np.ndarray, el: dict, above: list[dict], px: float):
    """The (start, sweep) a PIE shape is drawn with, in OOXML degrees (clockwise from +x, y down), read
    from the thumbnail: the API gives a pie as its preset and box only, not the angles a person
    dragged, and the preset's default is a 270 degree slice (intro-lecture's grading chart: five
    slices in one box, each drawn as the same three quarters). Along rays from the centre, the
    angles where the pie's own colour shows are its arc; where a colour of a shape above shows it may
    lie underneath, and anything else says it is not there. The smallest arc holding every angle it
    shows is what it draws - more is hidden anyway. None when unreadable: a turned or mirrored frame,
    no own colour, a box too small, or other colours inside the arc (another shape under it)."""
    fr = el.get("frame") or {}
    if (fr.get("rotation") or 0) % 360 or fr.get("flip") or el.get("fill_gradient"):
        return None
    col = rgb(el.get("fill"))
    if col is None or (el.get("fill_alpha") or 1.0) < 0.99:
        return None
    h, w = a.shape[:2]
    x0, y0, x1, y1 = el["bbox"]
    cx, cy, rx, ry = (x0 + x1) / 2 * px, (y0 + y1) / 2 * px, (x1 - x0) / 2 * px, (y1 - y0) / 2 * px
    if rx < 8 or ry < 8:
        return None
    covers = [rgb(e.get("fill")) for e in above if e["kind"] == "shape" and e.get("fill") and overlaps(e, el)]
    covers = [c for c in covers if c is not None and np.abs(c - col).max() > TOL]
    theta = np.radians(np.arange(0.0, 360.0, PIE_STEP))
    mine = np.zeros(len(theta), int)
    other = np.zeros(len(theta), int)
    for f in PIE_RADII:
        xs = np.clip(np.round(cx + f * rx * np.cos(theta)).astype(int), 0, w - 1)
        ys = np.clip(np.round(cy + f * ry * np.sin(theta)).astype(int), 0, h - 1)
        p = a[ys, xs]
        me = np.abs(p - col).max(axis=1) <= TOL
        hidden = np.zeros(len(theta), bool)
        for c in covers:
            hidden |= np.abs(p - c).max(axis=1) <= TOL
        mine += me
        other += ~me & ~hidden
    n = len(PIE_RADII)
    shows = mine * 2 > n
    absent = other * 2 > n
    idx = np.nonzero(shows)[0]
    if len(idx) < 4:
        return None
    if len(idx) == len(theta):
        return (0.0, 360.0)
    # the arc is the circle less its largest gap between angles the pie shows
    gaps = np.diff(np.concatenate([idx, [idx[0] + len(theta)]]))
    g = int(np.argmax(gaps))
    first = idx[(g + 1) % len(idx)]
    span = len(theta) - int(gaps[g]) + 1
    inside = (np.arange(span) + first) % len(theta)
    if absent[inside].sum() * PIE_STEP > PIE_STRAY:
        return None
    return (round(float(first * PIE_STEP), 2) % 360.0, round(span * PIE_STEP, 2))


# The corners each rounded preset rounds (top left, top right, bottom right, bottom left)
ROUNDED_CORNERS = {"ROUND_RECTANGLE": (1, 1, 1, 1), "FLOW_CHART_ALTERNATE_PROCESS": (1, 1, 1, 1),
                   "ROUND_1_RECTANGLE": (0, 1, 0, 0), "ROUND_2_SAME_RECTANGLE": (1, 1, 0, 0),
                   "ROUND_2_DIAGONAL_RECTANGLE": (1, 0, 1, 0)}
CORNER_MIN_PX = 12       # a rounded box's shorter side must be this many thumbnail pixels
# the corners read must give radii this close to each other, or this share of the radius: a big
# corner's shallow arc reads looser (drawing-workshop 5's 75 px corners read 70, 74 and 80)
CORNER_AGREE_PX = 2.5
CORNER_AGREE_SHARE = 0.15


def corner_radius(a: np.ndarray, el: dict, above: list[dict], px: float) -> float | None:
    """The corner radius (IR pt) a rounded rectangle is drawn with, read from the thumbnail: the API
    gives the preset, not the corner a person dragged, and the preset's default of a sixth of the
    shorter side rounded journey-maps' square title bars. Along each rounded corner's diagonal the
    shape's colour begins r(sqrt 2 - 1) in from the box's corner; where it crosses half way from the
    ground outside is read to a fraction of a pixel. None when unreadable: a turned or mirrored
    frame, no own opaque colour, a small box, a corner a shape above may hide or on ground of the
    shape's own colour, or corners that disagree."""
    corners = ROUNDED_CORNERS.get((el.get("shape_type") or "").upper())
    fr = el.get("frame") or {}
    if corners is None or (fr.get("rotation") or 0) % 360 or fr.get("flip") or el.get("fill_gradient"):
        return None
    col = rgb(el.get("fill"))
    if col is None or (el.get("fill_alpha") or 1.0) < 0.99:
        return None
    line = rgb(el.get("outline"))
    h, w = a.shape[:2]
    x0, y0, x1, y1 = (v * px for v in el["bbox"])
    if min(x1 - x0, y1 - y0) < CORNER_MIN_PX:
        return None
    reach = int(min(x1 - x0, y1 - y0) / 2)
    # an outline's outer edge is its arc's radius plus half its width round the same centre: on the
    # diagonal that many pixels / sqrt 2 nearer the box's corner (gdg24's 1.42 pt outlines read 1.7 pt small)
    rim = (el.get("weight") or 0.75) / 2 * px / 2 ** 0.5 if line is not None else 0.0
    radii = []
    for on, (cx, cy, dx, dy) in zip(corners, ((x0, y0, 1, 1), (x1, y0, -1, 1), (x1, y1, -1, -1), (x0, y1, 1, -1))):
        xa, xb = sorted((cx - 2 * dx, cx + reach * dx))
        ya, yb = sorted((cy - 2 * dy, cy + reach * dy))
        if not on or any(overlaps(e, {"bbox": [xa / px, ya / px, xb / px, yb / px]})
                         for e in above if e["kind"] != "text"):
            continue                       # (a text's letters stand inside its box, off the corner)
        steps = np.arange(-2, reach)
        xs = np.floor(cx + dx * (steps + 0.5)).astype(int)
        ys = np.floor(cy + dy * (steps + 0.5)).astype(int)
        if 0 <= xs[0] < w and 0 <= ys[0] < h:
            # along the diagonal: pixel s's centre is s + 0.5 in from the corner each way, and the arc
            # crosses the diagonal r (1 - 1/sqrt 2) in
            e = crossing(a[np.clip(ys, 0, h - 1), np.clip(xs, 0, w - 1)], steps, col, line)
            if e is not None:
                radii.append(max(0.0, e + rim) / (1 - 2 ** -0.5))
            continue
        # a corner on the page's edge (journey-maps' full-width bars) has no ground outside it along
        # the diagonal: along the box's first row (column) in from its edge on the page instead,
        # where the arc is u in from the corner at half a pixel in, r = u + 0.5 + sqrt u
        steps = np.arange(-1, reach)
        if 0 <= ys[0] < h:                 # the page's edge is to the side: along the top/bottom row
            rx = np.clip(np.floor(cx + dx * (steps + 0.5)).astype(int), 0, w - 1)
            ry = np.full(len(steps), int(np.floor(cy + dy * 0.5)))
            ry[0] = int(np.floor(cy - dy * 1.5))           # the ground: outside the box's edge
        elif 0 <= xs[0] < w:
            ry = np.clip(np.floor(cy + dy * (steps + 0.5)).astype(int), 0, h - 1)
            rx = np.full(len(steps), int(np.floor(cx + dx * 0.5)))
            rx[0] = int(np.floor(cx - dx * 1.5))
        else:
            continue
        if not (0 <= rx[0] < w and 0 <= ry[0] < h):
            continue
        u = crossing(a[np.clip(ry, 0, h - 1), np.clip(rx, 0, w - 1)], np.r_[-1, steps[1:]], col, line, edge=True)
        if u is not None:
            u = max(0.0, u)
            radii.append(u + 0.5 + u ** 0.5 if u > 0 else 0.0)
    if not radii or max(radii) - min(radii) > max(CORNER_AGREE_PX, CORNER_AGREE_SHARE * float(np.median(radii))):
        return None
    return round(float(np.median(radii)) / px, 2)


def crossing(p: np.ndarray, steps: np.ndarray, col, line, edge: bool = False) -> float | None:
    """Where along a run of pixels (the first the ground outside a shape, the others at `steps` + 0.5
    in from its corner) the shape's colour or outline covers half a pixel, to a fraction of one; None
    when the ground is the shape's colour or no three pixels in a row are covered. `edge`: the run
    starts inside the box, so its first covered pixel may be the first one."""
    p = p.astype(float)
    ground = p[0]
    span = np.abs(ground - col).max()
    rim = np.abs(ground - line).max() if line is not None else 0.0
    if line is not None and rim <= 2 * TOL:
        # on its outline's colour: gdg24 54's pink box stands on the yellow one's black outline, and
        # that corner read as covered from its first pixel: square, the only corner read (the pink was
        # too near the page's grey for the others, below)
        return None
    if span <= 2 * TOL and not rim:
        return None                        # on its own colour: no edge to see
    # (a fill as pale as its ground - the same pink box on gdg24's grey - is edged by its outline)
    mine = 1 - np.abs(p - col).max(axis=1) / span if span > 2 * TOL else np.zeros(len(p))
    if line is not None:
        mine = np.maximum(mine, 1 - np.abs(p - line).max(axis=1) / rim)
    mine[0] = 0.0
    inside = np.nonzero(mine >= 0.5)[0]
    if len(inside) == 0 or mine[inside[0]:inside[0] + 3].min() < 0.5:
        return None
    i = inside[0]
    if edge and i == 1:
        return 0.0                         # covered from the page's edge on: a square corner
    t = (0.5 - mine[i - 1]) / max(1e-6, mine[i] - mine[i - 1])     # how far past pixel i - 1
    return float(steps[i - 1] + 0.5 + t)


PICTURE_MIN_PX = 12      # a thumbnail picture's box must be at least this many pixels each way

def thumbnail_picture(a: np.ndarray, el: dict, above: list[dict], px: float, page: str | None, folder):
    """A shape whose fill the thumbnail shows as neither one colour nor a ramp - a photo cut to a
    freeform (sc-memphis' section slides: the girl, the wave, the ring), a texture - as the one
    picture of it there is: the thumbnail's pixels in its box. The API gives no URL for a picture
    fill, so this is what keeps the photo on the slide at all. Letters of texts drawn above it are
    painted out (filled in from the pixels around them), or they would be printed twice, once in
    the picture and once by their own text box a little apart; opaque elements above cover their
    part anyway. `page`: the page colour when nothing but the page lies under the shape - pixels of
    that colour are then made transparent, so the picture is the shape's own outline."""
    import hashlib
    from pathlib import Path
    from PIL import Image
    h, w = a.shape[:2]
    a0, b0, a1, b1 = px_box(el["bbox"], px, w, h)
    if a1 - a0 < PICTURE_MIN_PX or b1 - b0 < PICTURE_MIN_PX:
        return None
    sub = a[b0:b1, a0:a1].astype(np.float32)
    letters = np.zeros(sub.shape[:2], dtype=bool)
    for e in above:
        if e["kind"] != "text" or not e.get("paragraphs"):
            continue
        c0, d0, c1, d1 = px_box(e["bbox"], px, w, h, -2 * MARGIN_PX)
        if c1 <= a0 or c0 >= a1 or d1 <= b0 or d0 >= b1:
            continue
        box = (slice(max(0, d0 - b0), max(0, d1 - b0)), slice(max(0, c0 - a0), max(0, c1 - a0)))
        part = sub[box]
        if part.size == 0:
            continue
        ground = np.median(part.reshape(-1, 3), axis=0)
        off = np.abs(part - ground).max(axis=2)
        for p in e["paragraphs"]:
            for r in p.get("runs", []):
                c = rgb(r.get("color"))
                if c is not None and r.get("text", "").strip() and np.abs(c - ground).max() > TOL:
                    # a letter's pixel is nearer its colour than the ground's (antialiased rims are
                    # taken by the dilation below)
                    letters[box] |= (np.abs(part - c).max(axis=2) < off) & (off > TOL)
    if letters.any():
        from .deck_freeforms import dilate
        letters = dilate(letters, 2)
        sub = inpaint(sub, letters)
    alpha = np.full(sub.shape[:2], 255, dtype=np.uint8)
    ground = rgb(page)
    if ground is not None:
        alpha[np.abs(sub - ground).max(axis=2) <= TOL] = 0
        if (alpha > 0).mean() < 0.02:
            return None                    # nothing but the page: it shows nothing of its own
    im = Image.fromarray(np.dstack([np.clip(sub + 0.5, 0, 255).astype(np.uint8), alpha]), "RGBA")
    if ground is None:
        im = im.convert("RGB")
    import io
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=False)
    data = buf.getvalue()
    sha = hashlib.sha1(data).hexdigest()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"thumb-{sha[:16]}.png"
    if not path.exists():
        path.write_bytes(data)
    keep = {k: el[k] for k in ("id", "object", "group", "key", "inherited") if k in el}
    return {"kind": "image", "role": "figure", "alt": "picture fill", **keep,
            "bbox": [a0 / px, b0 / px, a1 / px, b1 / px], "file": str(path), "sha1": sha, "format": "png",
            "fill_source": "thumbnail"}


#: a download that is not a decodable image at all - a Google sign-in page for a picture that
#: needed one, a site's own 404/Next.js error page instead of the asset, an expired signed URL:
#: `deck_ir.image_format` calling it "unknown" is never something LaTeX could show
BROKEN_FORMATS = {"unknown"}


def recover_pictures(elements: list[dict], a, px: float, folder) -> None:
    """A picture element whose download gave nothing usable - `deck_ir.stash_picture`'s `error`
    (the fetch failed or was refused outright), or bytes that came back but are not a decodable
    picture at all (`BROKEN_FORMATS`) - is drawn instead from Google's own thumbnail: the pixels of
    its own box (`thumbnail_picture`), which is always closer to the deck than the hole
    `pictures_missing` would otherwise report. Whatever crop, recolour, brightness or contrast the
    API described applied to the file that never arrived, so none of it applies to the thumbnail's
    own pixels, which already show the picture as the slide draws it; the outline and place are
    untouched. Left alone (and still `pictures_missing`) without a thumbnail to crop, for a video's
    poster frame or a linked chart (each has its own fallback already), or a rotated or flipped
    picture: `thumbnail_picture`'s crop is axis-aligned, so only an upright one is safe this way."""
    if a is None:
        return
    for el in elements:
        if el.get("kind") != "image" or el.get("video") or el.get("chart") or el.get("wordArt"):
            continue
        if el.get("rotation") or el.get("flip") or not el.get("bbox"):
            continue
        if not (el.get("error") or el.get("format") in BROKEN_FORMATS):
            continue
        pic = thumbnail_picture(a, el, [], px, None, folder)
        if pic is None:
            continue
        for k in ("error", "crop", "crop_angle", "opacity", "brightness", "contrast", "recolor"):
            el.pop(k, None)
        el["file"], el["sha1"], el["format"] = pic["file"], pic["sha1"], pic["format"]
        el["picture_source"] = "thumbnail"


def inpaint(sub: np.ndarray, hole: np.ndarray) -> np.ndarray:
    """`sub` with the pixels `hole` filled in from their neighbours, ring by ring inwards."""
    out = sub.copy()
    todo = hole.copy()
    for _ in range(64):
        if not todo.any():
            break
        known = ~todo
        acc = np.zeros_like(out)
        cnt = np.zeros(out.shape[:2], dtype=np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            k = np.roll(np.roll(known, dy, 0), dx, 1)
            v = np.roll(np.roll(out, dy, 0), dx, 1)
            if dy == -1: k[-1, :] = False
            if dy == 1: k[0, :] = False
            if dx == -1: k[:, -1] = False
            if dx == 1: k[:, 0] = False
            acc += v * k[..., None]
            cnt += k
        fill = todo & (cnt > 0)
        out[fill] = acc[fill] / cnt[fill][:, None]
        todo &= ~fill
    if todo.any() and (~hole).any():
        out[todo] = np.median(sub[~hole], axis=0)
    return out


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
        if e.get("_traced") is not None:
            # a traced freeform hides its own ink, not its box
            from .deck_freeforms import dilate, paste
            ex, ey, m = e["_traced"]
            cover = np.zeros_like(region)
            paste(cover, (a0, b0), dilate(m, MARGIN_PX), (ex, ey))
            region &= ~cover
        elif opaque(e):
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
        el["_no_ramp"] = True     # neither flat nor a ramp explains it: a candidate for `thumbnail_picture`
        return
    if got[0] == "solid":
        if worth(got[1], background) and edges_show(a, el["bbox"], px, got[1]):
            el["fill"] = hexcolour(got[1])
            el["fill_source"] = "thumbnail"
            el.pop("fill_alpha", None)
    else:
        el["fill_gradient"] = {"axis": got[1], "colors": [hexcolour(c) for c in got[2]]}
        el["fill_source"] = "thumbnail"


INHERITED_MIN_PX = 300       # an inherited element's own visible part must reach this many thumbnail
                              # pixels before a slide's thumbnail is trusted to vote on it at all
INHERITED_VISIBLE = FLAT     # share of that visible part matching its own appearance to vote "shown"
INHERITED_ABSENT_MAX = 0.35  # ... at or below this, "not shown" (between the two: too ambiguous
                              # to vote either way - a partial cover or a near-miss colour, not the
                              # confident either/or a whole deck's worth of slides should decide on)


def element_appearance_share(sub: np.ndarray, region: np.ndarray, el: dict) -> float | None:
    """Share of an inherited (master/layout) element's own visible pixels (`sub`, `region`: `masks`'
    box-local view of it, whatever another element draws over it already excluded) that match its
    own known appearance - a flat `fill`, or its own downloaded picture cropped and stretched to its
    box, the way Slides draws a picture element (instagram's master draws six pieces of one 1154x2048
    screenshot, each by its own crop: the whole sheet squeezed into each box matched nowhere, and every
    piece voted absent and was dropped from all its slides). None when its appearance is not something this can characterise
    at all (no `file` yet, no plain opaque `fill`, a gradient, a fill the thumbnail has not settled) -
    such an element casts no vote in `deck_ir.vote_inherited` and is never judged missing on
    suspicion alone (ua-space: a master picture never drawn on any of 16 slides votes confidently
    absent on every one that can judge it; a layout element only ever partly covered stays
    unjudged rather than guessed at)."""
    n = int(region.sum())
    if n == 0:
        return None
    if el["kind"] == "image" and el.get("file") and not el.get("video"):
        try:
            from PIL import Image
            from .compare import cropped_picture
            h, w = sub.shape[:2]
            img = cropped_picture(Image.open(el["file"]).convert("RGB"), el.get("crop"))
            cand = np.asarray(img.resize((w, h))).astype(np.int16)
        except Exception:  # noqa: BLE001 - an unreadable download casts no vote
            return None
        close = np.abs(sub - cand).max(axis=2) <= TOL
        return float((close & region).sum() / n)
    if el["kind"] in ("shape", "text") and el.get("fill") and not el.get("fill_gradient") \
            and not el.get("fill_unread") and (el.get("fill_alpha") or 1.0) >= 0.99:
        col = rgb(el["fill"])
        if col is None:
            return None
        close = np.abs(sub - col).max(axis=2) <= TOL
        return float((close & region).sum() / n)
    return None


PAGE_GRAD_MIN_SHARE = 0.03   # share of the page that must be visible past every element's box - low
                              # enough that a page mostly covered by its own title and a full-bleed
                              # picture still gets a fit from the two thin strips beside it
                              # (china-pptx slide 47: two vertical slivers are 5.3% of the page and
                              # alone fit the same radial model - centre, radius and stops all within
                              # a few percent - that a sibling slide sharing the same layout fits from
                              # 22% of it; `_fit_share`'s own 85%-explained bar and margin over the
                              # flat colour, not this share, are what keep a small region honest)
PAGE_GRAD_MIN_PIXELS = 3000  # ... and at least this many pixels, so a sliver never fits a "ramp"
PAGE_GRAD_FIT = 0.85         # share of the visible background pixels a model must explain
PAGE_GRAD_MARGIN = 0.15      # a model must beat the flat colour's own fit by at least this much
PAGE_GRAD_SUBSAMPLE = 20000  # pixels drawn from the region for the regression (it is not free)
PAGE_GRAD_TOL = 20           # max channel distance for a *regression's* own residual, not a flat
                              # match: a live thumbnail's JPEG-ish banding sits a few units wider off
                              # an honest affine/radial ramp than off one constant colour (korea-pptx
                              # slide 28's own green-to-cream ramp: 79% of it within `TOL`, 94% within
                              # this - `TOL` stays the flat-colour and picture-verification bar)
PAGE_GRAD_TRIM = 0.12        # worst-explained share of the sample a plain fit short of `PAGE_GRAD_FIT`
                              # gets refit without, once: a large CJK title's antialiased rim leaks a
                              # sliver of near-black/near-white through the mask (its box grown only
                              # `MARGIN_PX`, not that big a glyph's true ink), dragging an honest ramp's
                              # least squares down (jruby-ja: a white-to-grey radial fits 0.83 raw,
                              # 0.96 with the worst 12% dropped and rescored against *every* sampled
                              # point). An artwork backdrop's error sits over most of its pixels, not a
                              # worst-percentile tail, so a photo or swirl is not rescued by this.


def page_visible_mask(elements: list[dict], px: float, w: int, h: int) -> np.ndarray:
    """The page's own pixels: everywhere no element's box (grown by `MARGIN_PX` for its antialiased
    rim) reaches. Conservative like the rest of the module - an element that turns out see-through or
    unfilled still keeps its box out of the reading, so a plain page is never blamed for what stands
    on it."""
    mask = np.ones((h, w), dtype=bool)
    for e in elements:
        bbox = e.get("bbox")
        if not bbox:
            continue
        a0, b0, a1, b1 = px_box(bbox, px, w, h, -MARGIN_PX)
        mask[max(0, b0):max(0, b1), max(0, a0):max(0, a1)] = False
    return mask


def region_flat_share(a: np.ndarray, region: np.ndarray, colour: str | None) -> float:
    """The share of `region`'s pixels within `TOL` of `colour` - 0 when there is nothing to compare
    or nothing visible."""
    col = rgb(colour)
    n = int(region.sum())
    if col is None or n == 0:
        return 0.0
    close = np.abs(a - col).max(axis=2) <= TOL
    return float((close & region).sum() / n)


def region_matches(a: np.ndarray, region: np.ndarray, colour: str | None) -> bool:
    """Does a flat `colour` explain what a region of the page's own pixels shows?"""
    return region_flat_share(a, region, colour) >= FLAT


def picture_region_share(a: np.ndarray, region: np.ndarray, path: str, w: int, h: int) -> float:
    """Does a candidate picture - stretched to the page, as `stretchedPictureFill` draws it - explain
    a region of the page's own pixels? The same question `region_flat_share` asks of a flat colour,
    for the layout's or master's own picture `parent_background` offers instead of one (china-pptx:
    a slide-level solid sits over the layout's radial picture, which several *other* layouts do not
    share - a picture is only worth taking over a flat colour that clearly is not one when the
    picture is confirmed on these same pixels, never on its say-so alone)."""
    n = int(region.sum())
    if n == 0:
        return 0.0
    try:
        from PIL import Image
        cand = np.asarray(Image.open(path).convert("RGB").resize((w, h))).astype(np.int16)
    except Exception:  # noqa: BLE001 - a truncated or unreadable download is simply no match
        return 0.0
    close = np.abs(a - cand).max(axis=2) <= TOL
    return float((close & region).sum() / n)


def _page_regression(region: np.ndarray, a: np.ndarray, n: int):
    """`n` pixels drawn from `region` (all of it when smaller): (x, y, colour) in pixel coordinates,
    float64, for the least-squares fits below."""
    ys, xs = np.nonzero(region)
    if len(xs) > n:
        idx = np.linspace(0, len(xs) - 1, n).astype(int)
        ys, xs = ys[idx], xs[idx]
    pix = a[ys, xs].astype(np.float64)
    return xs.astype(np.float64), ys.astype(np.float64), pix


RADIAL_STEP_MAX = 16     # pixel spacing of the grid a radial centre is triangulated on
RADIAL_STEPS_MIN = 24    # ... at least this many steps each way, so a small region still has some
RADIAL_KEEP_PCTL = 60    # only the steeper half or so of gradients: near the centre they are noise


def _radial_centre(a: np.ndarray, region: np.ndarray):
    """Where a radial fill's centre is, from the *direction* of the thumbnail's own colour gradient
    rather than its shape: a radial fill is some function of the distance to its centre alone, so at
    every point its steepest change points straight along the line to (or from) that centre, whatever
    the function - linear in the radius (what a real radial shading draws), its square, or anything
    else monotonic. Each such point gives one line the centre must lie on; least squares finds the
    point closest to all of them. None when there is too little of the region to grid, or its colour
    barely changes at all (no gradient anywhere to point)."""
    h, w = a.shape[:2]
    step = max(4, min(RADIAL_STEP_MAX, min(w, h) // RADIAL_STEPS_MIN))
    ys = np.arange(step, h - step, step)
    xs = np.arange(step, w - step, step)
    if len(ys) < 3 or len(xs) < 3:
        return None
    Y, X = np.meshgrid(ys, xs, indexing="ij")
    valid = region[Y, X] & region[Y, X + step] & region[Y, X - step] & region[Y + step, X] & region[Y - step, X]
    if valid.sum() < 8:
        return None
    seen = a[region].astype(np.float64)
    ch = int(np.argmax(seen.max(axis=0) - seen.min(axis=0)))            # the channel that varies the most
    gx = (a[Y, X + step, ch].astype(np.float64) - a[Y, X - step, ch].astype(np.float64)) / (2 * step)
    gy = (a[Y + step, X, ch].astype(np.float64) - a[Y - step, X, ch].astype(np.float64)) / (2 * step)
    mag = np.hypot(gx, gy)
    if not valid.any() or mag[valid].max() < 1e-6:
        return None
    keep = valid & (mag >= np.percentile(mag[valid], RADIAL_KEEP_PCTL))
    if keep.sum() < 8:
        return None
    px_, py_, gx, gy = X[keep].astype(np.float64), Y[keep].astype(np.float64), gx[keep], gy[keep]
    # each point (px, py) with gradient (gx, gy) puts the centre on the line through it in that
    # direction: (centre - p) parallel to g, i.e. cx*gy - cy*gx = px*gy - py*gx
    A = np.stack([gy, -gx], axis=1)
    bvec = px_ * gy - py_ * gx
    sol, *_ = np.linalg.lstsq(A, bvec, rcond=None)
    return float(sol[0]), float(sol[1])


def _fit_share(X: np.ndarray, pix: np.ndarray, tol: float):
    """Least squares `pix` against `X`, retried once on the worst `PAGE_GRAD_TRIM` share of points
    dropped when the plain fit's own share is short of `PAGE_GRAD_FIT` (`PAGE_GRAD_TRIM`). Either way
    the returned `share`/`span` are scored against *every* point `X`/`pix` holds, refit coefficients
    included - a fit only rescued this way when the ramp explains the outliers too, not merely the
    points kept for it. Returns `(coef, pred, share, span)`."""
    coef, *_ = np.linalg.lstsq(X, pix, rcond=None)
    pred = X @ coef
    err = np.abs(pred - pix).max(axis=1)
    share = float((err <= tol).mean())
    if share < PAGE_GRAD_FIT:
        cut = np.quantile(err, 1 - PAGE_GRAD_TRIM)
        keep = err <= cut
        if 0 < int(keep.sum()) < len(err):
            coef2, *_ = np.linalg.lstsq(X[keep], pix[keep], rcond=None)
            pred2 = X @ coef2
            share2 = float((np.abs(pred2 - pix).max(axis=1) <= tol).mean())
            if share2 > share:
                coef, pred, share = coef2, pred2, share2
    span = float(np.abs(pred.max(axis=0) - pred.min(axis=0)).max())
    return coef, pred, share, span


PAGE_TEXTURE_PATCH = 24      # side (px) of a small square sampled to test for fine grain a ramp's
                              # own smoothness never has
PAGE_TEXTURE_STD = 9.0       # a patch's own std, its local mean taken out, past this many channel
                              # levels is a printed texture's grain, not a clean ramp's JPEG-ish
                              # noise (en-flowchart's fine gold stripes: patches std ~13-18 against a
                              # real gradient's ~2-4)
PAGE_TEXTURE_PATCHES = 14    # patches drawn from the region before giving up on finding `TEXTURE_PATCH`
                              # of them wholly inside it
PAGE_TEXTURE_MIN_HITS = 6    # ... at least this many must land to say anything either way


def page_textured(a: np.ndarray, region: np.ndarray) -> bool:
    """Is the page's own visible background (`region`) a fine printed texture - stripes, a weave,
    fine noise - rather than the smooth ramp `page_gradient` fits affine or radial models to? A
    handful of small squares spread across `region` (wholly inside it, so an element's edge is never
    mistaken for the page's own grain) have their local mean taken out; a real gradient leaves each
    square within a few channel levels of flat (a straight or radial ramp barely curves over
    `PATCH` pixels), while a woven or striped backdrop still swings by ten or more. Too few whole
    squares to sample says nothing either way (a small or heavily obstructed region)."""
    h, w = a.shape[:2]
    ys, xs = np.nonzero(region)
    if len(ys) == 0:
        return False
    rng = np.random.default_rng(0)
    order = rng.permutation(len(ys))
    worst = 0.0
    hits = 0
    for i in order[: max(200, PAGE_TEXTURE_PATCHES * 12)]:
        y0, x0 = int(ys[i]), int(xs[i])
        y1, x1 = y0 + PAGE_TEXTURE_PATCH, x0 + PAGE_TEXTURE_PATCH
        if y1 > h or x1 > w or not region[y0:y1, x0:x1].all():
            continue
        block = a[y0:y1, x0:x1].astype(np.float64).reshape(-1, 3)
        worst = max(worst, float(block.std(axis=0).max()))
        hits += 1
        if hits >= PAGE_TEXTURE_PATCHES:
            break
    return hits >= PAGE_TEXTURE_MIN_HITS and worst >= PAGE_TEXTURE_STD


def page_texture_picture(a: np.ndarray, region: np.ndarray, folder) -> dict | None:
    """The page's own background where nothing describes it - no flat colour, no picture the API can
    point to, and (`page_textured`) too fine a grain for `page_gradient`'s ramp - as the one picture
    of it there is: the thumbnail's own pixels, with whatever elements stand on the page painted back
    in from what surrounds them (`inpaint`, `thumbnail_picture`'s own trick) so the picture covers the
    whole page, not just what stood clear of every element. None when there is too little of the page
    to go on. Same file whenever the same pixels come up twice (`sha1`-named)."""
    import hashlib
    from pathlib import Path
    from PIL import Image
    n = int(region.sum())
    if n < PAGE_GRAD_MIN_PIXELS or n < PAGE_GRAD_MIN_SHARE * region.size:
        return None
    filled = inpaint(a.astype(np.float32), ~region)
    im = Image.fromarray(np.clip(filled + 0.5, 0, 255).astype(np.uint8), "RGB")
    import io
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=False)
    data = buf.getvalue()
    sha = hashlib.sha1(data).hexdigest()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"thumb-{sha[:16]}.png"
    if not path.exists():
        path.write_bytes(data)
    return {"file": str(path), "sha1": sha, "format": "png"}


def page_gradient(a: np.ndarray, region: np.ndarray, flat_colour: str | None, px: float):
    """A linear (any angle) or radial gradient the page's own pixels (`region`, `page_visible_mask`)
    fit better than the flat colour Slides' API reported for the page background
    (`deck_ir.page_background`): `pageBackgroundFill` has no gradient type at all, so a .pptx
    `<a:gradFill>` or a radial fill always comes back as one `solidFill`. None when there is too
    little of the page to judge, the flat colour already explains it, or nothing fits clearly better
    - an artwork backdrop (swirls, a photo) is meant to fail here, not be drawn as a ramp.

    A linear ramp's colour is affine in page position for *any* axis - not just horizontal or
    vertical - so each channel is fit against (1, x, y) directly; the strongest of the three
    channels' (x, y) gradients gives the axis, and the colour at the page's two extreme corners
    along it gives the stops. A radial one is fit in two passes: `_radial_centre` triangulates the
    centre from the direction of the thumbnail's own colour gradient (radial from every point,
    whatever the profile), then colour against radius alone from that centre - a radial shading's
    own model - gives the stops and the fit to judge.

    Returns `{"type": "linear", "angle": degrees, "colors": [c0, c1]}` (0 degrees: left to right,
    turning counter-clockwise - `adopt.gradient_tikz`'s `rotate`) or `{"type": "radial", "center":
    [x, y], "radius": r, "colors": [inner, outer]}` (page pt from the top left), else None."""
    if a is None or not flat_colour:
        return None
    h, w = a.shape[:2]
    n = int(region.sum())
    if n < PAGE_GRAD_MIN_PIXELS or n < PAGE_GRAD_MIN_SHARE * region.size:
        return None
    flat_share = region_flat_share(a, region, flat_colour)
    if flat_share >= FLAT:
        return None                        # the reported colour already explains what shows
    if page_textured(a, region):
        return None                        # a fine grain, not a ramp (en-flowchart's gold stripes):
                                            # deck_ir falls back to `page_texture_picture` instead
    xs, ys, pix = _page_regression(region, a, PAGE_GRAD_SUBSAMPLE)
    if len(xs) < 200:
        return None
    best = None
    # linear
    X = np.stack([np.ones_like(xs), xs, ys], axis=1)
    coef, pred, share, span = _fit_share(X, pix, PAGE_GRAD_TOL)
    if share >= PAGE_GRAD_FIT and share >= flat_share + PAGE_GRAD_MARGIN and span >= GRAD_SPAN:
        b, c = coef[1], coef[2]
        k = int(np.argmax(np.hypot(b, c)))
        dx, dy = float(b[k]), float(c[k])
        norm = (dx ** 2 + dy ** 2) ** 0.5
        if norm > 1e-6:
            dx, dy = dx / norm, dy / norm
            angle = float(np.degrees(np.arctan2(-dy, dx)))
            corners = np.array([[0, 0], [w, 0], [0, h], [w, h]], dtype=np.float64)
            t = corners @ np.array([dx, dy])
            lo, hi = corners[int(np.argmin(t))], corners[int(np.argmax(t))]
            c0 = coef[0] + lo[0] * coef[1] + lo[1] * coef[2]
            c1 = coef[0] + hi[0] * coef[1] + hi[1] * coef[2]
            best = (share, {"type": "linear", "angle": round(angle, 1),
                            "colors": [hexcolour(np.clip(c0, 0, 255)), hexcolour(np.clip(c1, 0, 255))]})
    # radial
    centre = _radial_centre(a, region)
    if centre is not None:
        cx, cy = centre
        if -0.5 * w <= cx <= 1.5 * w and -0.5 * h <= cy <= 1.5 * h:
            r = np.hypot(xs - cx, ys - cy)
            Xr = np.stack([np.ones_like(r), r], axis=1)
            coefr, predr, sharer, spanr = _fit_share(Xr, pix, PAGE_GRAD_TOL)
            if sharer >= PAGE_GRAD_FIT and sharer >= flat_share + PAGE_GRAD_MARGIN and spanr >= GRAD_SPAN \
                    and (best is None or sharer > best[0]):
                r_out = float(r.max())
                inner, outer = coefr[0], coefr[0] + coefr[1] * r_out
                best = (sharer, {"type": "radial", "center": [round(cx / px, 2), round(cy / px, 2)],
                                "radius": round(r_out / px, 2),
                                "colors": [hexcolour(np.clip(inner, 0, 255)), hexcolour(np.clip(outer, 0, 255))]})
    return best[1] if best else None


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
