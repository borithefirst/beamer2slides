"""Freeform shapes, traced from the slide's own picture.

The Slides API gives a freeform (`shapeType` CUSTOM, or none at all) and a freeform line (a line
with neither `lineType` nor `lineCategory`) as a box and a transform: its geometry - SlidesCarnival's
squiggles, waves, rings and checkmarks, DevFest's and gdg24's blobs - is nowhere in the answer, and
`adopt` could only draw the box. Google's thumbnail of the slide is the only witness offline, so
`deck_ir(foreign=True, thumbnails=...)` asks it, from `deck_fills.settle` (top down, so what is
settled above an element is known when it is looked at):

- the element's paint - its fill, its outline, or for a fill the API reads as `{}` the one colour
  that is not the ground its box stands on - is looked for in its box (grown by the antialiasing
  rim and half an outline);
- how much of that paint each pixel holds (projected between the ground and the paint when the
  ground is known, else against the strongest contrast around it) is traced at one half by marching
  squares, and the rings are simplified (Douglas-Peucker, `SIMPLIFY` px) into polygons in page pt;
- what cannot be seen is decided, not guessed: pixels under an opaque element above take the
  shape's side of the edge they continue (they are hidden in the output as in the deck), holes made
  by the letters of a text above in the text's colour are filled, and so are holes that lie in the
  box of a shape above nobody could read (`unsaid`) or that show no colour of what is under the
  shape (then something above it the API did not tell is drawn there); a hole showing the ground is
  the shape's own;
- ink of the paint whose cut edges run on outside the box is a neighbour's, and a freeform that
  fills its box is left the rectangle its preset already draws.

It is written as `trace` (`rings` in page pt, even-odd), which `adopt_shapes.traced_block` draws as
one filled TikZ path - an editable vector outline rather than a picture of it, since the shapes are
flat colour. Conservative, because a wrong shape paints over what lies under it: a fill the API
cannot say must be one colour over one ground (a picture or texture fill is left as before), a
colour some element under it has too is ambiguous, and the traced ink must reach all four sides of
the element's box (a shape's box is the box of its geometry) except where they are hidden or off the
page, or where the ink ends against something drawn above it (`reaches_sides`; a line's ink is
looked for in its whole frame, `frame_bounds`) - or be the element's fill wrapped in its outline of
another colour (`outlined`: a curved arrow in a frame larger than itself). Lines of one group in one
paint (`kin`: SmartArt connectors sharing a bar) share their ink: it neither hides nor runs on from
one to the other. When any of that fails,
nothing changes: the element keeps what it had before this module. Without thumbnails nothing is
traced. A traced shape is a new record (`Tracing`), its ink beside it for the elements under it."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from typing import Union

import numpy as np

from .arrays import Floats, Floats32, Int32, Mask, SignedRGB
from . import deck_fills as F
from .deck_fills import Layer, PixelBox, Traced, Tracing
from .deck_ir_types import Absent, Frame, TargetElement, TargetShape, TargetText, Trace
from .ir_types import Box, Point

TOL = 14                  # max channel distance of a pixel that is the paint itself
GROW = 2                  # px around the box: the antialiased rim
SIMPLIFY = 0.4            # px, Douglas-Peucker tolerance
MIN_PX = 6                # traced pixels a shape needs at all
SIDE = 2.5                # px: how close the ink must come to each side of the box
OTHER = 0.04              # share of a `{}` box's visible pixels allowed to be neither paint nor ground
CONTINUES = 0.5           # a piece whose cut edges mostly continue outside the box is a neighbour's
CORED_STROKE = 2.0        # px: a stroke this wide shows its paint all along, so ink far from it is not its

Colour = Union[SignedRGB, Floats32, Floats]
"""A colour as the pixel code compares it: three channels, of whatever dtype it was read in."""
Ring = list[tuple[float, float]]
"""A closed polygon in pixel coordinates."""


def freeform(el: TargetElement) -> bool:
    """Is this element drawn from a geometry the API does not give?"""
    if not isinstance(el, TargetShape):
        return False
    if el.role == "line":
        line_type = None if isinstance(el.line_type, Absent) else el.line_type
        return not isinstance(el.category, Absent) and not el.category and not line_type
    return (el.shape_type or "").upper() in ("CUSTOM", "FREEFORM")


def kin(e: TargetElement, el: TargetShape) -> bool:
    """Is `e` a line of the line `el`'s own group, drawn in its paint? A SmartArt org chart's elbow
    connectors (en-smartart 3: one line per child, down from the parent, along a bar they all share,
    down to the child) run into each other's boxes by design: what one of them shows of the other is
    their common ink, neither hidden by it nor a neighbour's run on outside the box."""
    return (isinstance(e, TargetShape) and e is not el and el.role == "line" and e.role == "line"
            and bool(el.group) and e.group == el.group and (e.outline or "").lower() == (el.outline or "").lower()
            and e.outline_alpha == el.outline_alpha)


SAME_BOX = 0.5            # pt: siblings this close in every edge are called "the same declared box"


def shared_boxes(elements: Sequence[TargetElement]) -> list[list[int]]:
    """Indices of two or more sibling freeform shapes (same source group, not a line) that declare
    the exact same box - proof the API gave back the group's own canvas, not this piece's true
    extent (a PowerPoint SmartArt diagram's freeform pieces keep the whole diagram frame's off/ext;
    the one geometry that tells them apart is a path the API never returns). Tracing each alone then
    reads every other member as an opaque shape covering the *entire* shared box (`deck_fills.opaque`),
    so each is left "the box, solid" - as many stacked, identical, opaque rectangles as there are
    pieces, hiding the labels and picture a diagram draws inside that same box. Two distinct shapes
    sharing a box by coincidence is not a thing this converter draws; every group with two or more
    freeforms at one box is this."""
    by_group: dict[str, list[int]] = {}
    for i, el in enumerate(elements):
        if el.group and el.role != "line" and freeform(el):
            by_group.setdefault(el.group, []).append(i)
    clusters: list[list[int]] = []
    for idxs in by_group.values():
        if len(idxs) < 2:
            continue
        used: set[int] = set()
        for a_i, i in enumerate(idxs):
            if i in used:
                continue
            box = elements[i].bbox
            same = [i]
            for j in idxs[a_i + 1:]:
                if j in used:
                    continue
                if all(abs(x - y) <= SAME_BOX for x, y in zip(box, elements[j].bbox)):
                    same.append(j)
            if len(same) >= 2:
                used.update(same)
                clusters.append(same)
    return clusters


# ------------------------------------------------------------------------------ pixel machinery

def components(mask: Mask, conn8: bool) -> tuple[Int32, int]:
    """Connected components of a boolean mask (labels 1..n, 0 = background), by runs per row and a
    union-find over the runs that touch - numpy has no labelling of its own and scipy is not a
    dependency. `conn8`: diagonal neighbours touch too."""
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent: list[int] = [0]
    prev: list[tuple[int, int, int]] = []

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pad = np.zeros(w + 2, dtype=np.int8)
    rows_runs: list[list[tuple[int, int, int]]] = []
    for y in range(h):
        pad[1:-1] = mask[y]
        d = np.diff(pad)
        starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        cur: list[tuple[int, int, int]] = []
        k = 0
        for s, e in zip(starts.tolist(), ends.tolist()):
            lab = len(parent)
            parent.append(lab)
            lo, hi = (s - 1, e + 1) if conn8 else (s, e)
            while k < len(prev) and prev[k][1] <= lo:
                k += 1
            j = k
            while j < len(prev) and prev[j][0] < hi:
                a, b = find(prev[j][2]), find(lab)
                if a != b:
                    parent[max(a, b)] = min(a, b)
                j += 1
            cur.append((s, e, lab))
        rows_runs.append(cur)
        prev = cur
    remap: dict[int, int] = {}
    for y, cur in enumerate(rows_runs):
        for s, e, lab in cur:
            r = find(lab)
            if r not in remap:
                remap[r] = len(remap) + 1
            labels[y, s:e] = remap[r]
    return labels, len(remap)


def dilate(mask: Mask, r: int) -> Mask:
    out = mask.copy()
    for _ in range(r):
        m = out.copy()
        m[1:] |= out[:-1]
        m[:-1] |= out[1:]
        m[:, 1:] |= out[:, :-1]
        m[:, :-1] |= out[:, 1:]
        out = m
    return out


def enclosed(wall: Mask) -> Mask:
    """What a closed ring (`wall`, its ink) keeps in: the ring itself, plus every pixel that a walk
    from the region's own edge cannot reach without crossing it. A gap in the ring lets the outside
    in, so it is no longer enclosed - as with a hand-drawn outline that never quite closes."""
    free = ~wall
    labels, n = components(free, False)
    if not n:
        return np.ones_like(wall)
    border = np.unique(np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]))
    border = border[border != 0]
    outside = np.isin(labels, border) if border.size else np.zeros_like(free)
    return ~outside


def max_filter(v: Floats32, r: int) -> Floats32:
    out = v.copy()
    h, w = v.shape
    p = np.pad(v, r, mode="edge")
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            np.maximum(out, p[dy:dy + h, dx:dx + w], out=out)
    return out


def coverage(sub: SignedRGB, paint: Colour, ground: Colour | None) -> Floats32:
    """How much of `paint` each pixel holds, 0..1. With the ground known, the pixel's projection on
    the line from ground to paint (antialiasing mixes the two); without, against the strongest
    contrast to the paint within two pixels (what lies beyond the edge, whatever it is)."""
    p = np.asarray(paint, dtype=np.float32)
    s = sub.astype(np.float32)
    d = np.abs(s - p).max(axis=2)
    if ground is not None:
        g = np.asarray(ground, dtype=np.float32)
        v = p - g
        n = float((v * v).sum())
        if n > (2 * TOL) ** 2:
            t = ((s - g) * v).sum(axis=2) / n
            t = np.where(d <= TOL, 1.0, t)
            return np.clip(t, 0.0, 1.0)
    ref = np.maximum(max_filter(d, 2), 2.0 * TOL)
    a = 1.0 - d / ref
    a[d <= TOL] = 1.0
    return np.clip(a, 0.0, 1.0)


# --------------------------------------------------------------------------- marching squares

Crossing = tuple[str, int, int]
"""Where an iso-line crosses a cell edge: ("h", i, j) between (i, j) and (i, j + 1), ("v", i, j)
between (i, j) and (i + 1, j)."""


def contours(field: Floats32, level: float) -> list[Ring]:
    """Closed iso-lines of `field` at `level`, in pixel-centre coordinates (x, y) of `field`."""
    f = np.pad(field.astype(np.float32), 1, constant_values=0.0)
    inside = f >= level
    case = (inside[:-1, :-1] * 8 + inside[:-1, 1:] * 4 + inside[1:, 1:] * 2 + inside[1:, :-1] * 1).astype(np.int8)
    ys, xs = np.nonzero((case > 0) & (case < 15))
    links: dict[Crossing, list[Crossing]] = {}
    points: dict[Crossing, tuple[float, float]] = {}

    def point(key: Crossing) -> Crossing:
        if key not in points:
            kind, i, j = key
            if kind == "h":                       # between (i, j) and (i, j + 1)
                a, b = f[i, j], f[i, j + 1]
                t = (level - a) / (b - a) if b != a else 0.5
                points[key] = (j + t - 1.0, i - 1.0)
            else:                                 # between (i, j) and (i + 1, j)
                a, b = f[i, j], f[i + 1, j]
                t = (level - a) / (b - a) if b != a else 0.5
                points[key] = (j - 1.0, i + t - 1.0)
        return key

    for i, j in zip(ys.tolist(), xs.tolist()):
        c = int(case[i, j])
        top: Crossing = ("h", i, j)
        bottom: Crossing = ("h", i + 1, j)
        left: Crossing = ("v", i, j)
        right: Crossing = ("v", i, j + 1)
        centre = (f[i, j] + f[i, j + 1] + f[i + 1, j] + f[i + 1, j + 1]) / 4 >= level
        segs = {1: [(left, bottom)], 2: [(bottom, right)], 3: [(left, right)], 4: [(top, right)],
                6: [(top, bottom)], 7: [(left, top)], 8: [(left, top)], 9: [(top, bottom)],
                11: [(top, right)], 12: [(left, right)], 13: [(bottom, right)], 14: [(left, bottom)],
                5: [(left, top), (bottom, right)] if centre else [(top, right), (left, bottom)],
                10: [(top, right), (left, bottom)] if centre else [(left, top), (bottom, right)]}[c]
        for a, b in segs:
            links.setdefault(point(a), []).append(point(b))
            links.setdefault(b, []).append(a)
    rings: list[Ring] = []
    seen: set[Crossing] = set()
    for start in links:
        if start in seen:
            continue
        ring = [start]
        seen.add(start)
        prev: Crossing | None = None
        cur = start
        while True:
            nxt = [k for k in links[cur] if k != prev and k not in seen]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            seen.add(cur)
            ring.append(cur)
        if len(ring) >= 3:
            rings.append([points[k] for k in ring])
    return rings


def simplify(ring: Ring, tol: float) -> Ring:
    """Douglas-Peucker on a closed ring (split at its first point and the point farthest from it)."""
    pts = np.asarray(ring, dtype=np.float64)
    n = len(pts)
    if n <= 4:
        return ring
    far = int(np.argmax(((pts - pts[0]) ** 2).sum(axis=1)))
    keep = np.zeros(n + 1, dtype=bool)
    closed = np.vstack([pts, pts[:1]])
    keep[[0, far, n]] = True
    stack = [(0, far), (far, n)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        seg = closed[b] - closed[a]
        ln = float(np.hypot(*seg))
        mid = closed[a + 1:b] - closed[a]
        d = np.abs(mid[:, 0] * seg[1] - mid[:, 1] * seg[0]) / ln if ln > 1e-9 else np.hypot(mid[:, 0], mid[:, 1])
        k = int(np.argmax(d))
        if d[k] > tol:
            keep[a + 1 + k] = True
            stack += [(a, a + 1 + k), (a + 1 + k, b)]
    out = [(float(p[0]), float(p[1])) for p in closed[keep][:-1]]
    return out if len(out) >= 3 else ring


def area(ring: Sequence[Sequence[float]]) -> float:
    p = np.asarray(ring)
    return 0.5 * abs(float(np.dot(p[:, 0], np.roll(p[:, 1], -1)) - np.dot(p[:, 1], np.roll(p[:, 0], -1))))


# ----------------------------------------------------------------------------------- the reading

def paste(dst: Mask, origin: tuple[int, int], mask: Mask, at: tuple[int, int]) -> None:
    """OR `mask` (whose [0, 0] is image pixel `at`) into `dst` (whose [0, 0] is `origin`)."""
    ox, oy = origin
    ax, ay = at
    h, w = dst.shape
    mh, mw = mask.shape
    x0, y0 = max(0, ax - ox), max(0, ay - oy)
    x1, y1 = min(w, ax - ox + mw), min(h, ay - oy + mh)
    if x1 > x0 and y1 > y0:
        dst[y0:y1, x0:x1] |= mask[y0 - (ay - oy):y1 - (ay - oy), x0 - (ax - ox):x1 - (ax - ox)]


def unknowns(a: SignedRGB, region: PixelBox, above: Sequence[Layer],
             px: float) -> tuple[Mask, Mask, list[SignedRGB | None]]:
    """(pixels hidden under opaque elements above, pixels a text above may have letters on, the
    colours of those letters - None for any colour) over the pixel box `region`."""
    a0, b0, a1, b1 = region
    h, w = a.shape[:2]
    hidden = np.zeros((b1 - b0, a1 - a0), dtype=bool)
    wordy = np.zeros_like(hidden)
    inks: list[SignedRGB | None] = []
    for layer in above:
        e = layer.el
        c0, d0, c1, d1 = F.px_box(e.bbox, px, w, h, -1)
        if c1 <= a0 or c0 >= a1 or d1 <= b0 or d0 >= b1:
            continue
        if layer.traced is not None:
            paste(hidden, (a0, b0), dilate(layer.traced.ink, 1), (layer.traced.x, layer.traced.y))
        elif F.opaque(e):
            hidden[max(0, d0 - b0):max(0, d1 - b0), max(0, c0 - a0):max(0, c1 - a0)] = True
        elif F.wordy(e):
            wordy[max(0, d0 - b0):max(0, d1 - b0), max(0, c0 - a0):max(0, c1 - a0)] = True
            for colour in F.run_colours(e):
                ink = F.rgb(colour)
                if colour and ink is not None:
                    inks.append(ink)
            if not isinstance(e, TargetText):
                inks.append(None)                  # a table or a see-through picture: any colour
    return hidden, wordy & ~hidden, inks


def unsaid(a: SignedRGB, region: PixelBox, above: Sequence[TargetElement], px: float, drawn: bool) -> Mask:
    """The boxes of shapes above whose fill neither the API nor the thumbnail could say (a multicolour
    `{}` freeform: Canva's art on sc-memphis' panel): whatever shows there may be theirs. `drawn`:
    only what such a shape draws on the page, which is what its picture from the thumbnail covers
    (`deck_fills.thumbnail_picture`) - an ELLIPSE's is the ellipse its box inscribes, its corners
    showing what lies under it (en-smartart 4: a photo cut to a disc)."""
    a0, b0, a1, b1 = region
    h, w = a.shape[:2]
    m = np.zeros((b1 - b0, a1 - a0), dtype=bool)
    for e in above:
        if isinstance(e, TargetShape) and e.unsaid:
            c0, d0, c1, d1 = F.px_box(e.bbox, px, w, h, 0)
            r0, r1 = max(0, d0 - b0), min(b1 - b0, max(0, d1 - b0))
            q0, q1 = max(0, c0 - a0), min(a1 - a0, max(0, c1 - a0))
            if r1 <= r0 or q1 <= q0:
                continue
            part = (slice(r0, r1), slice(q0, q1))
            if drawn and (e.shape_type or "").upper() == "ELLIPSE" and (e.frame is None or not e.frame.rotation):
                x0, y0, x1, y1 = (v * px for v in e.bbox)
                ys, xs = np.mgrid[part] + 0.5
                ys, xs = ys + b0, xs + a0
                rx, ry = max((x1 - x0) / 2, 1e-6), max((y1 - y0) / 2, 1e-6)
                m[part] |= ((xs - (x0 + x1) / 2) / rx) ** 2 + ((ys - (y0 + y1) / 2) / ry) ** 2 <= 1
            else:
                m[part] = True
    return m


def blend(colour: Colour, alpha: float, ground: Colour) -> Floats32:
    mixed = np.asarray(colour, dtype=np.float32) * alpha + np.asarray(ground, dtype=np.float32) * (1 - alpha)
    return mixed.astype(np.float32, copy=False)     # (float32 already: a Python float does not widen it)


def ring_colour(a: SignedRGB, region: PixelBox, width: int) -> Floats | None:
    """The colour most of a ring `width` px wide just around `region` is (None when there is too
    little of it). The majority vote is taken twice - a coarse 8-wide bucket first to guess near which
    colour the votes cluster, then every pixel within `TOL` of that guess (as `unread_paint` already
    does) - because a ring thin enough to sit hard against a neighbour (a check mark's ring against
    its own drop shadow's crescent, drawing-workshop 52) is mostly one true flat colour that JPEG noise
    and antialiasing still split across several adjacent buckets, none reaching the coarse bucket's
    own 50% on its own even though the colour they all approximate does."""
    h, w = a.shape[:2]
    a0, b0, a1, b1 = region
    o0, p0, o1, p1 = max(0, a0 - width), max(0, b0 - width), min(w, a1 + width), min(h, b1 + width)
    m = np.ones((p1 - p0, o1 - o0), dtype=bool)
    m[b0 - p0:b1 - p0, a0 - o0:a1 - o0] = False
    if m.sum() < 2 * width * ((a1 - a0) + (b1 - b0)) * 0.4:
        return None
    px_ = a[p0:p1, o0:o1][m]
    q = (px_ // 8).astype(np.int32)
    packed = (q[:, 0] << 10) | (q[:, 1] << 5) | q[:, 2]
    top = int(np.bincount(packed).argmax())
    guess = np.median(px_[packed == top], axis=0)
    close = np.abs(px_.astype(np.int16) - guess).max(axis=1) <= TOL
    if close.sum() < 0.5 * len(px_):
        return None
    colour: Floats = np.median(px_[close], axis=0)
    return colour


def unread_paint(sub: SignedRGB, visible: Mask, ground: Colour | None) -> Floats | None:
    """The one colour a `{}` fill shows over `ground` in its box, or None (a picture, a texture,
    or more than one colour: nothing a flat path can say)."""
    n = int(visible.sum())
    if n < MIN_PX or ground is None:
        return None
    px_ = sub[visible]
    g = np.asarray(ground, dtype=np.int16)
    off = np.abs(px_ - g).max(axis=1) > TOL
    if off.sum() < MIN_PX:
        return None
    q = (px_[off] // 8).astype(np.int32)
    packed = (q[:, 0] << 10) | (q[:, 1] << 5) | q[:, 2]
    top = int(np.bincount(packed).argmax())
    guess = np.median(px_[off][packed == top], axis=0)
    close = np.abs(px_ - guess).max(axis=1) <= TOL
    if close.sum() < max(MIN_PX, 0.01 * n):
        return None
    paint: Floats = np.median(px_[close], axis=0)
    # every pixel is the paint, the ground, or antialiasing between the two
    v = (paint - g).astype(np.float32)
    t = np.clip(((px_ - g).astype(np.float32) @ v) / float((v * v).sum()), 0, 1)
    on_line = np.abs(px_ - (g + t[:, None] * v)).max(axis=1) <= TOL
    if (~on_line).sum() > OTHER * n:
        return None
    return paint


REFUSED: Counter[str] = Counter()        # why freeforms were left as they were (for the bench)


def trace(a: SignedRGB, el: TargetShape, above: Sequence[Layer], under: Sequence[TargetElement], px: float,
          background: str | None, bottom: bool, unread: bool) -> Tracing | None:
    """Trace a freeform in the thumbnail `a` (see the module docstring): the shape with its `trace`
    (and for a `{}` fill its `fill`) and the ink it was traced from, or None, counting why in
    `REFUSED`, when it stays as it is."""
    got = _trace(a, el, above, under, px, background, bottom, unread, True)
    if got == "sides" and el.role == "line":
        # a stroke cut back to its paint loses its end where it crosses another line's ink, which is
        # no pixel of its paint (journey-maps 4: three curves from one origin): then it is asked whole
        got = _trace(a, el, above, under, px, background, bottom, unread, False)
    if isinstance(got, str):
        REFUSED[got] += 1
        return None
    return got


def _trace(a: SignedRGB, el: TargetShape, above: Sequence[Layer], under: Sequence[TargetElement], px: float,
           background: str | None, bottom: bool, unread: bool, trim: bool) -> str | Tracing:
    """`trace`'s reading: the traced shape, or why not. `trim`: a wide stroke is cut back to its paint."""
    if el.fill_gradient or el.trace:
        return "done"
    h, w = a.shape[:2]
    line = el.role == "line"
    fill = None if line else el.fill
    stroke = el.outline
    weight = (F.weight_of(el) or 0.75) if stroke else 0.0
    grow = GROW + int(np.ceil(weight * px / 2))
    x0, y0, x1, y1 = (v * px for v in el.bbox)
    if x1 - x0 < 1.5 and y1 - y0 < 1.5:
        return "tiny"
    # a line's box is that of its two ends; its path may lie anywhere in its frame, whose other two
    # corners a turned frame puts beyond that box (en-smartart 4: SmartArt's connectors run along
    # the middle of a turned frame as tall as their stroke)
    f0, g0, f1, g1 = (v * px for v in frame_bounds(el.frame, el.bbox)) if line else (x0, y0, x1, y1)
    region = (max(0, int(np.floor(f0)) - grow), max(0, int(np.floor(g0)) - grow),
              min(w, int(np.ceil(f1)) + grow), min(h, int(np.ceil(g1)) + grow))
    a0, b0, a1, b1 = region
    if a1 - a0 < 2 or b1 - b0 < 2:
        return "tiny"
    sub = a[b0:b1, a0:a1].astype(np.int16)
    hidden, wordy, inks = unknowns(a, region, [layer for layer in above if not kin(layer.el, el)], px)
    page = F.rgb(background) if bottom else None
    ground: Colour | None = page
    ring_field: tuple[Floats32 | None, Floats32] | None = None
    paints: list[Colour]
    if unread and not fill:
        ground = page if page is not None else ring_colour(a, region, 4)
        paint = unread_paint(sub, ~hidden & ~wordy, ground)
        if paint is None or (page is not None and np.abs(paint - page).max() <= TOL):
            return "unread-paint"
        for e in under:                      # a colour an element under it has: whose ink is it?
            if not F.overlaps(e.bbox, el.bbox):
                continue
            colours = [F.fill_of(e), F.outline_of(e), *F.run_colours(e)]
            for c in colours:
                known = F.rgb(c)
                if known is not None and np.abs(known - paint).max() <= 2 * TOL:
                    return "unread-under"
        if any(ink is not None and np.abs(ink - paint).max() <= 3 * TOL for ink in inks):
            return "unread-ink"                           # the letters of a text above (they overflow its box)
        fill = F.hexcolour(paint)
        paints = [paint]
    elif unread:
        return "flat-read"                               # settled by `deck_fills` as a flat box: it is one
    else:
        paints = []
        for colour, alpha in ((fill, el.fill_alpha), (stroke, el.outline_alpha)):
            known = F.rgb(colour)
            if not colour or known is None:
                # (a colour is always "#rrggbb" here: `deck_ir` writes no other)
                continue
            alpha = 1.0 if alpha is None else alpha
            if alpha < 0.99:
                if page is None:
                    stroke_rgb = F.rgb(stroke)
                    if colour == fill and stroke_rgb is not None and (el.outline_alpha or 1.0) >= 0.99:
                        # a see-through fill over other shapes, inside an opaque outline (en-smartart's
                        # funnel, white at 0.4 over its balls): the ring is what is traced, and what it
                        # encloses is the shape
                        ring_field = (None, stroke_rgb.astype(np.float32))
                        continue
                    return "alpha-over"                   # what it is seen as depends on what is under it
                paints.append(blend(known, alpha, page))
            else:
                paints.append(known.astype(np.float32))
        if not paints:
            return "no-paint"
        if page is not None and all(np.abs(p - page).max() <= TOL for p in paints):
            return "page-colour"                           # the page's own colour: its edges cannot be seen
        # a fill the ground already shows draws no edge of its own (a white cloud on a near-white
        # page): the outline is the only ink marching squares can key on, so what is asked for is
        # not the fill's colour but what that ring encloses
        fill_known, stroke_known = F.rgb(fill), F.rgb(stroke)
        if fill_known is not None and stroke_known is not None and page is not None:
            fill_paint, stroke_paint = fill_known.astype(np.float32), stroke_known.astype(np.float32)
            if np.abs(fill_paint - page).max() <= TOL and np.abs(stroke_paint - page).max() > TOL:
                ring_field = (fill_paint, stroke_paint)
    if ring_field is not None:
        ring_fill, ring_stroke = ring_field
        ring_cov = coverage(sub, ring_stroke, page)
        wall = dilate(ring_cov >= 0.5, 1)
        inside = enclosed(wall) & ~hidden
        cov = np.where(wall, np.clip(ring_cov, 0.51, 1.0), np.where(inside, 1.0, 0.0))
        paints = [ring_stroke] if ring_fill is None else [ring_fill, ring_stroke]
    else:
        cov = np.max([coverage(sub, p, ground) for p in paints], axis=0)
        inside = (cov >= 0.5) & ~hidden
    if any(ink is not None and np.abs(ink - p).max() <= 3 * TOL for ink in inks for p in paints):
        inside &= ~wordy                     # letters in the shape's own colour are not the shape
    # a piece with no pixel that is the paint itself is a texture's speck or an antialiased edge
    # of something else, not a part of the shape
    core = np.min([np.abs(sub - p).max(axis=2) for p in paints], axis=0) <= TOL
    labels, n = components(inside, True)
    if n:
        cored = np.zeros(n + 1, dtype=bool)
        cored[np.unique(labels[core & inside])] = True
        cored[0] = False
        inside = cored[labels]
    # an antialiased rim is a pixel or two wide: what reads as the paint beyond that is the edge of
    # something else against a stronger contrast still (jeb-arch 4: a box outline beside a connector's
    # end became a hook of it; emoji-essay 2: letters on a dark band closed a curve's box into a
    # slab). Only a stroke, and ink vouched for by its outline (below), is cut back to it: a filled
    # shape's own hatching is hairs with no core either (drawing-workshop 63), and a hairline is
    # all rim, its paint showing only here and there (journey-maps 2: a 1 px outline came out dashed)
    near_core = dilate(core, 2)
    if line and trim and weight * px >= CORED_STROKE:
        inside &= near_core
    # holes a text above made with its letters, or that lie wholly under opaque elements, are the
    # shape's; any other hole is the shape's own
    holes, n = components(~inside, False)
    if n:
        edge = np.zeros_like(inside)
        edge[0, :] = edge[-1, :] = True
        edge[:, 0] = edge[:, -1] = True
        touching = set(np.unique(holes[edge]).tolist())
        lettered = np.zeros_like(inside)
        for ink in inks:
            if ink is None:
                lettered |= wordy
            else:
                lettered |= wordy & (np.abs(sub - ink).max(axis=2) <= 3 * TOL)
        explained = hidden | lettered | (wordy & (cov > 0.1)) | unsaid(a, region, [la.el for la in above], px, False)
        # what a hole of the shape's own shows is what lies under it: the page, or an element under
        # it. A hole showing anything else is something above that the deck did not tell (sc-memphis'
        # Canva art on its yellow panel), and the shape goes on under it. The page's own colour is a
        # ground even when something else also lies under the element somewhere in its box (drawing-
        # workshop 52's check mark: a black drop-shadow copy of the same ring sits almost on top of it, so `page` is
        # None and only that shadow's own colour was asked for before - but the ring's true interior is
        # the slide's own background, not the shadow's, wherever the shadow itself does not reach it)
        grounds: list[Colour]
        if page is not None:
            grounds = [page]
        else:
            bg = F.rgb(background)
            grounds = ([bg] if bg is not None else []) + under_colours(a, region, el, under)
        shows_ground = np.zeros_like(inside)
        for g in grounds:
            shows_ground |= np.abs(sub - g).max(axis=2) <= TOL
        for k in range(1, n + 1):
            if k in touching:
                continue
            m = holes == k
            if explained[m].mean() >= 0.9 or (grounds and shows_ground[m].mean() < 0.5):
                inside |= m
    inside |= hidden & dilate(inside, 3)
    if inside.sum() < MIN_PX:
        return "few"
    # pieces cut off by the box that go on outside it are a neighbour's of the same colour
    # - and when the whole of it goes on outside, it is not this shape at all (a squiggle tile
    # over a wave of the colour it was taken for: the box cuts the wave)
    labels, n = components(inside, True)
    if not n:
        return "few"
    kin_boxes = [F.px_box(e.bbox, px, w, h, -grow) for e in [*(la.el for la in above), *under] if kin(e, el)]
    cont = continues_outside(a, region, labels, n, paints, kin_boxes)
    sizes = np.bincount(labels.ravel(), minlength=n + 1)
    sizes[0] = 0
    if cont[int(sizes.argmax())] > CONTINUES:
        return "continues"
    if n > 1:
        # the ink kin share runs on from the shape's own; a piece apart from it that runs on into a
        # kin's box, or that a kin above already traced, is that kin's (drawing-workshop 14: each
        # letter's outline is a line of one group, and the next letter's edge lies in this one's box)
        taken = np.zeros_like(inside)
        if kin_boxes:
            cont = continues_outside(a, region, labels, n, paints, [])
            for layer in above:
                if kin(layer.el, el) and layer.traced is not None:
                    paste(taken, (a0, b0), dilate(layer.traced.ink, 1), (layer.traced.x, layer.traced.y))
        for k in range(1, n + 1):
            piece = labels == k
            if sizes[k] < 0.5 * sizes[1:].max() and (cont[k] > CONTINUES or taken[piece].mean() > 0.9):
                inside[piece] = False
    if inside.sum() < MIN_PX:
        return "sides"
    covered = hidden | unsaid(a, region, [la.el for la in above], px, True)
    if not reaches_sides(inside, hidden | wordy, region, (x0, y0, x1, y1), (w, h), covered):
        # cut back to its paint as a stroke is (above; china-pptx 60: a dark branch against the sky
        # ran on from an arrow's tip as a hair of it), what is left must be wrapped in its outline
        inside = inside & (near_core | hidden | wordy)
        if not outlined(inside, sub, fill, stroke, el, hidden | wordy):
            return "sides"
    # a freeform that is its box is a rectangle, which the preset says better (and edits better)
    bx0, by0 = int(np.clip(round(x0) - a0, 0, inside.shape[1])), int(np.clip(round(y0) - b0, 0, inside.shape[0]))
    bx1, by1 = int(np.clip(round(x1) - a0, 0, inside.shape[1])), int(np.clip(round(y1) - b0, 0, inside.shape[0]))
    box_px = np.zeros_like(inside)
    box_px[by0:by1, bx0:bx1] = True
    seen = ~(hidden | wordy)
    if box_px.any() and (inside | ~seen)[box_px].mean() >= 0.97 and (inside & ~box_px).sum() <= 0.03 * box_px.sum():
        return "rectangle"
    field = np.where(inside, np.clip(cov, 0.51, 1.0), np.where(hidden | wordy, 0.0, np.minimum(cov, 0.49)))
    rings: list[tuple[Point, ...]] = []
    for ring in contours(field, 0.5):
        if area(ring) < 1.5:
            continue
        ring = simplify(ring, SIMPLIFY)
        rings.append(tuple((round(float(a0 + x + 0.5) / px, 2), round(float(b0 + y + 0.5) / px, 2)) for x, y in ring))
    if not rings:
        return "no-rings"
    paint_fill = fill if fill else stroke
    if paint_fill is None:
        return "no-paint"                    # (a paint was found above, so one of the two is set)
    paint_stroke: str | None = None
    fill_rgb, stroke_rgb = F.rgb(fill), F.rgb(stroke)
    if fill_rgb is not None and stroke_rgb is not None and np.abs(fill_rgb - stroke_rgb).max() > TOL:
        paint_stroke = stroke
    shape = el
    if unread and el.fill is None:
        shape = replace(shape, fill=fill, fill_source="thumbnail")
    traced = Trace(rings=tuple(rings), fill=paint_fill, alpha=el.fill_alpha if fill else el.outline_alpha,
                   stroke=paint_stroke, weight=round(weight, 3) if paint_stroke else None, source="thumbnail")
    return Tracing(shape=replace(shape, trace=traced), traced=Traced(x=a0, y=b0, ink=inside))


def frame_bounds(fr: Frame | None, bbox: Box) -> Box:
    """The page box of an element's whole frame (`deck_ir.frame`: its four corners, turned and
    sheared as the transform has them), grown to hold `bbox`; `bbox` alone without a frame."""
    if fr is None:
        return bbox
    (ox, oy), (q0, q1, q2, q3), (fw, fh) = fr.origin, fr.matrix, fr.size
    xs = [ox + q0 * u * fw + q1 * v * fh for u in (0, 1) for v in (0, 1)] + [bbox[0], bbox[2]]
    ys = [oy + q2 * u * fw + q3 * v * fh for u in (0, 1) for v in (0, 1)] + [bbox[1], bbox[3]]
    return (min(xs), min(ys), max(xs), max(ys))


def under_colours(a: SignedRGB, region: PixelBox, el: TargetShape, under: Sequence[TargetElement]) -> list[Colour]:
    """The colours a hole in `el` may show when elements lie under it: the colour around the region,
    the fills, outlines and letters of what is under it (a translucent fill blended over the others),
    or [] when a picture, table or shading is under it and any colour may show."""
    beneath = [e for e in under if F.overlaps(e.bbox, el.bbox)]
    if any(not isinstance(e, (TargetShape, TargetText)) or e.fill_gradient for e in beneath):
        return []
    found: list[Colour | None] = [ring_colour(a, region, 4)]
    see_through: list[tuple[SignedRGB | None, float]] = []
    for e in beneath:
        for c in [F.outline_of(e), *F.run_colours(e)]:
            found.append(F.rgb(c))
        fill = F.fill_of(e)
        if fill:
            alpha = F.fill_alpha_of(e)
            alpha = 1.0 if alpha is None else alpha
            if alpha >= 0.99:
                found.append(F.rgb(fill))
            else:
                see_through.append((F.rgb(fill), alpha))
    grounds: list[Colour] = [g for g in found if g is not None]
    grounds += [blend(colour, alpha, g) for colour, alpha in see_through if colour is not None for g in list(grounds)]
    return grounds


def continues_outside(a: SignedRGB, region: PixelBox, labels: Int32, n: int, paints: Sequence[Colour],
                      kin_boxes: Sequence[PixelBox]) -> Floats:
    """Per component: the share of its pixels on the region's edge whose neighbour just outside the
    region is the paint too (0 for a component that touches the edge at fewer than 4 pixels). An
    outside neighbour in one of `kin_boxes` (pixel boxes of `kin` lines) is not asked: ink running on
    there is the ink the two share."""
    h, w = a.shape[:2]
    a0, b0, a1, b1 = region
    out = np.zeros(n + 1)
    hits = np.zeros(n + 1)
    total = np.zeros(n + 1)

    def is_paint(pix: SignedRGB) -> Mask:
        return np.min([np.abs(pix - p).max(axis=-1) for p in paints], axis=0) <= TOL

    xs, ys = np.arange(a0, a1), np.arange(b0, b1)
    for lab, outside, px_, py_ in (
            (labels[0, :], a[b0 - 2, a0:a1] if b0 >= 2 else None, xs, np.full(len(xs), b0 - 2)),
            (labels[-1, :], a[b1 + 1, a0:a1] if b1 + 1 < h else None, xs, np.full(len(xs), b1 + 1)),
            (labels[:, 0], a[b0:b1, a0 - 2] if a0 >= 2 else None, np.full(len(ys), a0 - 2), ys),
            (labels[:, -1], a[b0:b1, a1 + 1] if a1 + 1 < w else None, np.full(len(ys), a1 + 1), ys)):
        if outside is None:
            continue
        shared = np.zeros(len(lab), dtype=bool)
        for c0, d0, c1, d1 in kin_boxes:
            shared |= (px_ >= c0) & (px_ < c1) & (py_ >= d0) & (py_ < d1)
        ok = is_paint(outside.astype(np.int16))
        np.add.at(total, lab[~shared], 1)
        np.add.at(hits, lab[~shared], ok[~shared].astype(float))
    out[total >= 4] = hits[total >= 4] / total[total >= 4]
    out[0] = 0
    return out


RIM = 0.9                 # share of a traced edge an outline must run along to vouch for the ink


def outlined(inside: Mask, sub: SignedRGB, fill: str | None, stroke: str | None, el: TargetShape,
             unknown: Mask) -> bool:
    """Is the ink an opaque fill wrapped in an opaque outline of another colour - both paints in it,
    and the outline's along (`RIM` of) every edge the ink shows? Two colours in that order are the
    element's own signature: a neighbour of the fill's colour, or a colour taken for it, does not wear
    the outline too. Such ink need not reach every side of the box: a curved arrow drawn in a frame
    larger than itself (china-pptx 60: four red arrows outlined in green, each touching two sides of
    its turned frame) is the element whole, not a piece of something else."""
    if el.role == "line" or not fill or not stroke:
        return False
    if any((alpha if alpha is not None else 1.0) < 0.99 for alpha in (el.fill_alpha, el.outline_alpha)):
        return False
    f, s = F.rgb(fill), F.rgb(stroke)
    if f is None or s is None or np.abs(f - s).max() <= 3 * TOL:
        return False
    fcore = inside & (np.abs(sub - f).max(axis=2) <= TOL)
    score = inside & (np.abs(sub - s).max(axis=2) <= TOL)
    if fcore.sum() < MIN_PX or score.sum() < MIN_PX:
        return False
    rim = inside & dilate(~inside & ~unknown, 1)
    return bool(rim.any()) and bool((rim & dilate(score, 2)).sum() >= RIM * rim.sum())


UNDER = 0.5               # share of the ink's end toward a side that must meet a cover for it to run on under it


def reaches_sides(inside: Mask, unknown: Mask, region: PixelBox, box: tuple[float, float, float, float],
                  size: tuple[int, int], covered: Mask | None) -> bool:
    """Does the traced ink reach every side of the element's box (`box`, in thumbnail pixels; within
    `SIDE` px)? A side that is off the page, or mostly unseen, is excused - and so is one toward which
    the ink ends against something drawn above it (`covered`: opaque elements, and the pictures of
    shapes nobody could read), since it runs on under it there. A line's end is a point of its box's
    side, not the side: en-smartart 4's three connectors leave from under Homer, a photo cut to a disc,
    whose rim cuts each one's end off well short of a side that mostly lies beside the disc."""
    a0, b0, a1, b1 = region
    x0, y0, x1, y1 = box
    w, h = size
    ys, xs = np.nonzero(inside)
    mx0, mx1 = a0 + xs.min(), a0 + xs.max() + 1
    my0, my1 = b0 + ys.min(), b0 + ys.max() + 1
    lx0, ly0 = int(np.clip(round(x0) - a0, 0, inside.shape[1] - 1)), int(np.clip(round(y0) - b0, 0, inside.shape[0] - 1))
    lx1, ly1 = int(np.clip(round(x1) - a0, 1, inside.shape[1])), int(np.clip(round(y1) - b0, 1, inside.shape[0]))
    near = dilate(covered, 2) if covered is not None and covered.any() else None

    def unseen(strip: Mask) -> bool:
        return strip.size == 0 or bool(strip.mean() > 0.5)

    def under(end: Mask) -> bool:
        return near is not None and bool(near[ys[end], xs[end]].mean() >= UNDER)

    checks = [
        (mx0 <= x0 + SIDE, x0 <= 0.5 or unseen(unknown[:, lx0:lx0 + 4]) or under(xs <= xs.min() + 1)),
        (my0 <= y0 + SIDE, y0 <= 0.5 or unseen(unknown[ly0:ly0 + 4, :]) or under(ys <= ys.min() + 1)),
        (mx1 >= x1 - SIDE, x1 >= w - 0.5 or unseen(unknown[:, max(0, lx1 - 4):lx1]) or under(xs >= xs.max() - 1)),
        (my1 >= y1 - SIDE, y1 >= h - 0.5 or unseen(unknown[max(0, ly1 - 4):ly1, :]) or under(ys >= ys.max() - 1)),
    ]
    return all(reached or excused for reached, excused in checks)
