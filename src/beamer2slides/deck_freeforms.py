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
page. When any of that fails, nothing changes: the element keeps what it had before this module.
Without thumbnails nothing is traced."""

from __future__ import annotations

from collections import Counter

import numpy as np

from . import deck_fills as F

TOL = 14                  # max channel distance of a pixel that is the paint itself
GROW = 2                  # px around the box: the antialiased rim
SIMPLIFY = 0.4            # px, Douglas-Peucker tolerance
MIN_PX = 6                # traced pixels a shape needs at all
SIDE = 2.5                # px: how close the ink must come to each side of the box
OTHER = 0.04              # share of a `{}` box's visible pixels allowed to be neither paint nor ground
CONTINUES = 0.5           # a piece whose cut edges mostly continue outside the box is a neighbour's


def freeform(el: dict) -> bool:
    """Is this element drawn from a geometry the API does not give?"""
    if el["kind"] != "shape":
        return False
    if el.get("role") == "line":
        return "category" in el and not el["category"] and not el.get("line_type")
    return (el.get("shape_type") or "").upper() in ("CUSTOM", "FREEFORM")


# ------------------------------------------------------------------------------ pixel machinery

def components(mask: np.ndarray, conn8: bool = True) -> tuple[np.ndarray, int]:
    """Connected components of a boolean mask (labels 1..n, 0 = background), by runs per row and a
    union-find over the runs that touch - numpy has no labelling of its own and scipy is not a
    dependency."""
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
    rows_runs = []
    for y in range(h):
        pad[1:-1] = mask[y]
        d = np.diff(pad)
        starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        cur = []
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


def dilate(mask: np.ndarray, r: int) -> np.ndarray:
    out = mask.copy()
    for _ in range(r):
        m = out.copy()
        m[1:] |= out[:-1]
        m[:-1] |= out[1:]
        m[:, 1:] |= out[:, :-1]
        m[:, :-1] |= out[:, 1:]
        out = m
    return out


def max_filter(v: np.ndarray, r: int) -> np.ndarray:
    out = v.copy()
    h, w = v.shape
    p = np.pad(v, r, mode="edge")
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            np.maximum(out, p[dy:dy + h, dx:dx + w], out=out)
    return out


def coverage(sub: np.ndarray, paint, ground=None) -> np.ndarray:
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

def contours(field: np.ndarray, level: float = 0.5) -> list[list[tuple[float, float]]]:
    """Closed iso-lines of `field` at `level`, in pixel-centre coordinates (x, y) of `field`."""
    f = np.pad(field.astype(np.float32), 1, constant_values=0.0)
    inside = f >= level
    case = (inside[:-1, :-1] * 8 + inside[:-1, 1:] * 4 + inside[1:, 1:] * 2 + inside[1:, :-1] * 1).astype(np.int8)
    ys, xs = np.nonzero((case > 0) & (case < 15))
    links: dict[tuple, list[tuple]] = {}
    points: dict[tuple, tuple[float, float]] = {}

    def point(key):
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
        top, bottom, left, right = ("h", i, j), ("h", i + 1, j), ("v", i, j), ("v", i, j + 1)
        centre = (f[i, j] + f[i, j + 1] + f[i + 1, j] + f[i + 1, j + 1]) / 4 >= level
        segs = {1: [(left, bottom)], 2: [(bottom, right)], 3: [(left, right)], 4: [(top, right)],
                6: [(top, bottom)], 7: [(left, top)], 8: [(left, top)], 9: [(top, bottom)],
                11: [(top, right)], 12: [(left, right)], 13: [(bottom, right)], 14: [(left, bottom)],
                5: [(left, top), (bottom, right)] if centre else [(top, right), (left, bottom)],
                10: [(top, right), (left, bottom)] if centre else [(left, top), (bottom, right)]}[c]
        for a, b in segs:
            links.setdefault(point(a), []).append(point(b))
            links.setdefault(b, []).append(a)
    rings = []
    seen: set = set()
    for start in links:
        if start in seen:
            continue
        ring = [start]
        seen.add(start)
        prev, cur = None, start
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


def simplify(ring: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
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
    out = [tuple(p) for p in closed[keep][:-1]]
    return out if len(out) >= 3 else ring


def area(ring) -> float:
    p = np.asarray(ring)
    return 0.5 * abs(float(np.dot(p[:, 0], np.roll(p[:, 1], -1)) - np.dot(p[:, 1], np.roll(p[:, 0], -1))))


# ----------------------------------------------------------------------------------- the reading

def paste(dst: np.ndarray, origin, mask: np.ndarray, at) -> None:
    """OR `mask` (whose [0, 0] is image pixel `at`) into `dst` (whose [0, 0] is `origin`)."""
    ox, oy = origin
    ax, ay = at
    h, w = dst.shape
    mh, mw = mask.shape
    x0, y0 = max(0, ax - ox), max(0, ay - oy)
    x1, y1 = min(w, ax - ox + mw), min(h, ay - oy + mh)
    if x1 > x0 and y1 > y0:
        dst[y0:y1, x0:x1] |= mask[y0 - (ay - oy):y1 - (ay - oy), x0 - (ax - ox):x1 - (ax - ox)]


def unknowns(a: np.ndarray, region, above: list[dict], px: float):
    """(pixels hidden under opaque elements above, pixels a text above may have letters on, the
    colours of those letters) over the pixel box `region`."""
    a0, b0, a1, b1 = region
    h, w = a.shape[:2]
    hidden = np.zeros((b1 - b0, a1 - a0), dtype=bool)
    wordy = np.zeros_like(hidden)
    inks = []
    for e in above:
        c0, d0, c1, d1 = F.px_box(e["bbox"], px, w, h, -1)
        if c1 <= a0 or c0 >= a1 or d1 <= b0 or d0 >= b1:
            continue
        if e.get("_traced") is not None:
            ex, ey, m = e["_traced"]
            paste(hidden, (a0, b0), dilate(m, 1), (ex, ey))
        elif F.opaque(e):
            hidden[max(0, d0 - b0):max(0, d1 - b0), max(0, c0 - a0):max(0, c1 - a0)] = True
        elif F.wordy(e):
            wordy[max(0, d0 - b0):max(0, d1 - b0), max(0, c0 - a0):max(0, c1 - a0)] = True
            for p in e.get("paragraphs", []):
                for r in p.get("runs", []):
                    if r.get("color") and F.rgb(r["color"]) is not None:
                        inks.append(F.rgb(r["color"]))
            if e["kind"] != "text":
                inks.append(None)                  # a table or a see-through picture: any colour
    return hidden, wordy & ~hidden, inks


def unsaid(a: np.ndarray, region, above: list[dict], px: float) -> np.ndarray:
    """The boxes of shapes above whose fill neither the API nor the thumbnail could say (a multicolour
    `{}` freeform: Canva's art on sc-memphis' panel): whatever shows there may be theirs."""
    a0, b0, a1, b1 = region
    h, w = a.shape[:2]
    m = np.zeros((b1 - b0, a1 - a0), dtype=bool)
    for e in above:
        if e.get("_unsaid"):
            c0, d0, c1, d1 = F.px_box(e["bbox"], px, w, h, 0)
            m[max(0, d0 - b0):max(0, d1 - b0), max(0, c0 - a0):max(0, c1 - a0)] = True
    return m


def blend(colour, alpha: float, ground):
    return np.asarray(colour, dtype=np.float32) * alpha + np.asarray(ground, dtype=np.float32) * (1 - alpha)


def ring_colour(a: np.ndarray, region, width: int = 4):
    """The colour most of a ring just around `region` is (None when there is too little of it)."""
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
    near = px_[packed == top]
    if len(near) < 0.5 * len(px_):
        return None
    return np.median(near, axis=0)


def unread_paint(sub: np.ndarray, visible: np.ndarray, ground):
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
    paint = np.median(px_[close], axis=0)
    # every pixel is the paint, the ground, or antialiasing between the two
    v = (paint - g).astype(np.float32)
    t = np.clip(((px_ - g).astype(np.float32) @ v) / float((v * v).sum()), 0, 1)
    on_line = np.abs(px_ - (g + t[:, None] * v)).max(axis=1) <= TOL
    if (~on_line).sum() > OTHER * n:
        return None
    return paint


REFUSED: Counter = Counter()             # why freeforms were left as they were (for the bench)


def trace(a: np.ndarray, el: dict, above: list[dict], under: list[dict], px: float,
          background: str | None, bottom: bool, unread: bool) -> bool:
    """Trace a freeform in the thumbnail `a` (see the module docstring): sets `el["trace"]` (and for
    a `{}` fill its `fill`), or leaves the element as it is and counts why in `REFUSED`."""
    why = _trace(a, el, above, under, px, background, bottom, unread)
    if why:
        REFUSED[why] += 1
    return not why


def _trace(a: np.ndarray, el: dict, above: list[dict], under: list[dict], px: float,
           background: str | None, bottom: bool, unread: bool) -> str | None:
    if el.get("fill_gradient") or el.get("trace"):
        return "done"
    h, w = a.shape[:2]
    line = el.get("role") == "line"
    fill = None if line else el.get("fill")
    stroke = el.get("outline")
    weight = (el.get("weight") or 0.75) if stroke else 0.0
    grow = GROW + int(np.ceil(weight * px / 2))
    x0, y0, x1, y1 = (v * px for v in el["bbox"])
    if x1 - x0 < 1.5 and y1 - y0 < 1.5:
        return "tiny"
    region = (max(0, int(np.floor(x0)) - grow), max(0, int(np.floor(y0)) - grow),
              min(w, int(np.ceil(x1)) + grow), min(h, int(np.ceil(y1)) + grow))
    a0, b0, a1, b1 = region
    if a1 - a0 < 2 or b1 - b0 < 2:
        return "tiny"
    sub = a[b0:b1, a0:a1].astype(np.int16)
    hidden, wordy, inks = unknowns(a, region, above, px)
    page = F.rgb(background) if bottom else None
    ground = page
    if unread and not fill:
        ground = page if page is not None else ring_colour(a, region)
        paint = unread_paint(sub, ~hidden & ~wordy, ground)
        if paint is None or (page is not None and np.abs(paint - page).max() <= TOL):
            return "unread-paint"
        for e in under:                      # a colour an element under it has: whose ink is it?
            if not F.overlaps(e, el):
                continue
            colours = [e.get("fill"), e.get("outline")] + [r.get("color") for p in e.get("paragraphs", [])
                                                           for r in p.get("runs", [])]
            if any(F.rgb(c) is not None and np.abs(F.rgb(c) - paint).max() <= 2 * TOL for c in colours):
                return "unread-under"
        if any(ink is not None and np.abs(ink - paint).max() <= 3 * TOL for ink in inks):
            return "unread-ink"                           # the letters of a text above (they overflow its box)
        fill, fill_alpha = F.hexcolour(paint), 1.0
        paints = [paint]
    elif unread:
        return "flat-read"                               # settled by `deck_fills` as a flat box: it is one
    else:
        paints = []
        for colour, alpha in ((fill, el.get("fill_alpha")), (stroke, el.get("outline_alpha"))):
            if not colour:
                continue
            alpha = 1.0 if alpha is None else alpha
            if alpha < 0.99:
                if page is None:
                    return "alpha-over"                   # what it is seen as depends on what is under it
                paints.append(blend(F.rgb(colour), alpha, page))
            else:
                paints.append(F.rgb(colour).astype(np.float32))
        if not paints:
            return "no-paint"
        if page is not None and all(np.abs(p - page).max() <= TOL for p in paints):
            return "page-colour"                           # the page's own colour: its edges cannot be seen
    cov = np.max([coverage(sub, p, ground) for p in paints], axis=0)
    inside = (cov >= 0.5) & ~hidden
    if any(ink is not None and np.abs(ink - p).max() <= 3 * TOL for ink in inks for p in paints):
        inside &= ~wordy                     # letters in the shape's own colour are not the shape
    # a piece with no pixel that is the paint itself is a texture's speck or an antialiased edge
    # of something else, not a part of the shape
    core = np.min([np.abs(sub - p).max(axis=2) for p in paints], axis=0) <= TOL
    labels, n = components(inside)
    if n:
        cored = np.zeros(n + 1, dtype=bool)
        cored[np.unique(labels[core & inside])] = True
        cored[0] = False
        inside = cored[labels]
    # holes a text above made with its letters, or that lie wholly under opaque elements, are the
    # shape's; any other hole is the shape's own
    holes, n = components(~inside, conn8=False)
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
        explained = hidden | lettered | (wordy & (cov > 0.1)) | unsaid(a, region, above, px)
        # what a hole of the shape's own shows is what lies under it: the page, or an element under
        # it. A hole showing anything else is something above that the deck did not tell (sc-memphis'
        # Canva art on its yellow panel), and the shape goes on under it
        grounds = [page] if page is not None else under_colours(a, region, el, under)
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
    labels, n = components(inside)
    if not n:
        return "few"
    cont = continues_outside(a, region, labels, n, paints)
    sizes = np.bincount(labels.ravel(), minlength=n + 1)
    sizes[0] = 0
    if cont[int(sizes.argmax())] > CONTINUES:
        return "continues"
    if n > 1:
        for k in range(1, n + 1):
            if cont[k] > CONTINUES and sizes[k] < 0.5 * sizes[1:].max():
                inside[labels == k] = False
    if inside.sum() < MIN_PX or not reaches_sides(inside, hidden | wordy, region, (x0, y0, x1, y1), (w, h)):
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
    rings = []
    for ring in contours(field):
        if area(ring) < 1.5:
            continue
        ring = simplify(ring, SIMPLIFY)
        rings.append([[round(float(a0 + x + 0.5) / px, 2), round(float(b0 + y + 0.5) / px, 2)] for x, y in ring])
    if not rings:
        return "no-rings"
    paint_fill, paint_stroke = (fill, None) if fill else (stroke, None)
    if fill and stroke and np.abs(F.rgb(fill) - F.rgb(stroke)).max() > TOL:
        paint_stroke = stroke
    if unread and el.get("fill") is None:
        el["fill"], el["fill_source"] = fill, "thumbnail"
    el["trace"] = {"rings": rings, "fill": paint_fill,
                   "alpha": (el.get("fill_alpha") if fill else el.get("outline_alpha")),
                   "stroke": paint_stroke, "weight": round(weight, 3) if paint_stroke else None,
                   "source": "thumbnail"}
    el["_traced"] = (a0, b0, inside)


def under_colours(a: np.ndarray, region, el: dict, under: list[dict]) -> list:
    """The colours a hole in `el` may show when elements lie under it: the colour around the region,
    the fills, outlines and letters of what is under it (a translucent fill blended over the others),
    or [] when a picture, table or shading is under it and any colour may show."""
    beneath = [e for e in under if F.overlaps(e, el)]
    if any(e.get("kind") not in ("shape", "text") or e.get("fill_gradient") for e in beneath):
        return []
    grounds = [ring_colour(a, region)]
    see_through = []
    for e in beneath:
        for c in [e.get("outline")] + [r.get("color") for p in e.get("paragraphs", []) for r in p.get("runs", [])]:
            grounds.append(F.rgb(c))
        if e.get("fill"):
            alpha = 1.0 if e.get("fill_alpha") is None else e["fill_alpha"]
            (grounds if alpha >= 0.99 else see_through).append((F.rgb(e["fill"]), alpha) if alpha < 0.99
                                                               else F.rgb(e["fill"]))
    grounds = [g for g in grounds if g is not None]
    grounds += [blend(rgb, alpha, g) for rgb, alpha in see_through if rgb is not None for g in list(grounds)]
    return grounds


def continues_outside(a: np.ndarray, region, labels: np.ndarray, n: int, paints):
    """Per component: the share of its pixels on the region's edge whose neighbour just outside the
    region is the paint too (0 for a component that touches the edge at fewer than 4 pixels)."""
    h, w = a.shape[:2]
    a0, b0, a1, b1 = region
    out = np.zeros(n + 1)
    hits = np.zeros(n + 1)
    total = np.zeros(n + 1)

    def is_paint(pix):
        return np.min([np.abs(pix - p).max(axis=-1) for p in paints], axis=0) <= TOL

    for side, lab, outside in (
            ("top", labels[0, :], a[b0 - 2, a0:a1] if b0 >= 2 else None),
            ("bottom", labels[-1, :], a[b1 + 1, a0:a1] if b1 + 1 < h else None),
            ("left", labels[:, 0], a[b0:b1, a0 - 2] if a0 >= 2 else None),
            ("right", labels[:, -1], a[b0:b1, a1 + 1] if a1 + 1 < w else None)):
        if outside is None:
            continue
        ok = is_paint(outside.astype(np.int16))
        np.add.at(total, lab, 1)
        np.add.at(hits, lab, ok.astype(float))
    out[total >= 4] = hits[total >= 4] / total[total >= 4]
    out[0] = 0
    return out


def reaches_sides(inside: np.ndarray, unknown: np.ndarray, region, box, size) -> bool:
    """Does the traced ink reach every side of the element's box (within `SIDE` px)? A side that is
    off the page, or mostly unseen, is excused."""
    a0, b0, a1, b1 = region
    x0, y0, x1, y1 = box
    w, h = size
    ys, xs = np.nonzero(inside)
    mx0, mx1 = a0 + xs.min(), a0 + xs.max() + 1
    my0, my1 = b0 + ys.min(), b0 + ys.max() + 1
    lx0, ly0 = int(np.clip(round(x0) - a0, 0, inside.shape[1] - 1)), int(np.clip(round(y0) - b0, 0, inside.shape[0] - 1))
    lx1, ly1 = int(np.clip(round(x1) - a0, 1, inside.shape[1])), int(np.clip(round(y1) - b0, 1, inside.shape[0]))

    def unseen(strip) -> bool:
        return strip.size == 0 or strip.mean() > 0.5

    checks = [
        (mx0 <= x0 + SIDE, x0 <= 0.5 or unseen(unknown[:, lx0:lx0 + 4])),
        (my0 <= y0 + SIDE, y0 <= 0.5 or unseen(unknown[ly0:ly0 + 4, :])),
        (mx1 >= x1 - SIDE, x1 >= w - 0.5 or unseen(unknown[:, max(0, lx1 - 4):lx1])),
        (my1 >= y1 - SIDE, y1 >= h - 0.5 or unseen(unknown[max(0, ly1 - 4):ly1, :])),
    ]
    return all(reached or excused for reached, excused in checks)
