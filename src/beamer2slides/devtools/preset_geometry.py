"""Rasterising a preset shape's *default*-adjustment geometry (`adopt_shapes.preset`) and comparing
it with what a foreign deck's own thumbnail shows in the same box.

The Slides API gives an adjustable preset (`PARALLELOGRAM`, `CHEVRON`, `STAR_5`, a rounded
rectangle...) as a `shapeType`, a size and a transform - never the adjustment values a person
dragged (CLAUDE.md "What becomes native" has no bearing here: this is `adopt`, reading a deck
nobody converted, not `classify` reading a PDF). `adopt_shapes.preset` draws every such preset
from its own OOXML default adjustment, so a shape whose real adjustment differs a lot from that
default is drawn wrong - the corpus finding this module exists to measure is `ja-schedule`'s
groups of three `PARALLELOGRAM`s: Google renders them as thin ~22 px ribbons (a big slant
adjustment), our default (`x = ss * 0.25`, half the shape's short side) draws each one wide enough
that the three merge into one solid block.

`path_polygons` turns one of `adopt_shapes.preset`'s TikZ path strings back into polygon rings, in
the shape's own w by h frame (pt, y down, origin top left - the same convention as an element's
`bbox`), sampling curves and arcs rather than hand-duplicating their geometry a second time.
`default_rings` collects every ring a preset's *fill* draws (stroke-only paths - brackets, braces -
have no fill silhouette to compare and are left out). `rasterize` fills those rings, even-odd, onto
a pixel grid at the thumbnail's own scale. `match_score` is the actual comparison: how much of what
the thumbnail shows as the element's fill colour, inside its box and not hidden by something drawn
above it (`deck_fills.masks` - the same occlusion logic `deck_fills.sample_element` uses, so a
shape mostly covered by a text box or a picture is left out exactly as a fill reading would be),
lines up with our default polygon (intersection over union) - the number `devtools/preset_survey.py`
tallies per preset family, and later, if a family disagrees often enough, what a fitted adjustment
would be judged by improving.
"""

from __future__ import annotations

import math
import re

import numpy as np

from ..arrays import Mask, SignedRGB

POINT_RE = r"\(([-\d.]+)pt,([-\d.]+)pt\)"
ARC_RE = r"arc \[start angle=([-\d.]+), end angle=([-\d.]+), x radius=([-\d.]+)pt, y radius=([-\d.]+)pt\]"
ELLIPSE_RE = r"ellipse \[x radius=([-\d.]+)pt, y radius=([-\d.]+)pt\]"
CONTROLS_RE = r"\.\. controls \([-\d.]+pt,[-\d.]+pt\) and \([-\d.]+pt,[-\d.]+pt\) \.\."
POINT = re.compile(POINT_RE)
ARC = re.compile(ARC_RE)
ELLIPSE = re.compile(ELLIPSE_RE)
CONTROLS = re.compile(CONTROLS_RE)
CONTROL_POINTS = re.compile(r"\(([-\d.]+)pt,([-\d.]+)pt\) and \(([-\d.]+)pt,([-\d.]+)pt\)")
TOKEN = re.compile(f"{CONTROLS_RE}|{ARC_RE}|{ELLIPSE_RE}|{POINT_RE}|cycle")

ARC_DEGREE_STEP = 6      # an arc or ellipse is flattened to a point every this many degrees
BEZIER_STEPS = 12        # segments a cubic Bezier corner is flattened to

# Presets `adopt_shapes.preset` draws with a tunable default (an OOXML `adj` this file's default
# geometry stands in for) rather than a fixed silhouette - RECTANGLE, ELLIPSE, TEXT_BOX and the
# many flowchart symbols that are one or the other under another name have no adjustment to miss,
# and are left out; so are the (regular, seal, heart, most flowchart...) presets whose shape is
# fixed whatever a person dragged. `LEFT_BRACKET`/`RIGHT_BRACKET`/`BRACKET_PAIR`/`LEFT_BRACE`/
# `RIGHT_BRACE`/`BRACE_PAIR` do have a real adjustment (their thickness) but draw no fill at all
# (mode "s"): `default_rings` returns nothing for them, and the fill-colour comparison this module
# makes cannot see them either - they are listed so a survey counts them as "unmeasurable", not
# silently as agreeing.
ADJUSTABLE_PRESETS = {
    "PARALLELOGRAM", "TRAPEZOID", "HEXAGON", "FLOW_CHART_PREPARATION", "OCTAGON", "HOME_PLATE",
    "CHEVRON",
    "RIGHT_ARROW", "LEFT_ARROW", "UP_ARROW", "DOWN_ARROW", "LEFT_RIGHT_ARROW", "UP_DOWN_ARROW",
    "NOTCHED_RIGHT_ARROW", "STRIPED_RIGHT_ARROW",
    "PLUS", "MATH_PLUS", "MATH_MINUS", "MATH_EQUAL", "MATH_MULTIPLY",
    "DONUT", "NO_SMOKING", "FRAME", "CORNER", "PIE", "ARC",
    "WEDGE_RECTANGLE_CALLOUT", "WEDGE_ROUND_RECTANGLE_CALLOUT", "WEDGE_ELLIPSE_CALLOUT",
    "CAN", "FLOW_CHART_MAGNETIC_DISK", "CUBE", "FOLDED_CORNER",
    "STAR_4", "STAR_5", "STAR_6", "STAR_7", "STAR_8", "STAR_10", "STAR_12", "STAR_16", "STAR_24", "STAR_32",
    "ROUND_RECTANGLE", "ROUND_1_RECTANGLE", "ROUND_2_SAME_RECTANGLE", "ROUND_2_DIAGONAL_RECTANGLE",
    "FLOW_CHART_ALTERNATE_PROCESS",
    "SNIP_1_RECTANGLE", "SNIP_2_SAME_RECTANGLE", "SNIP_2_DIAGONAL_RECTANGLE", "SNIP_ROUND_RECTANGLE",
    "LEFT_BRACKET", "RIGHT_BRACKET", "BRACKET_PAIR", "LEFT_BRACE", "RIGHT_BRACE", "BRACE_PAIR",
}
# Already fitted from the thumbnail elsewhere (`deck_fills.corner_radius`, `deck_fills.pie_angles`):
# `adopt_shapes.shape_block` does not use the *default* geometry for these once a radius or a pie
# angle was read, so they are counted, but expected to disagree far less often than the rest.
FITTED_ELSEWHERE = {"ROUND_RECTANGLE", "ROUND_1_RECTANGLE", "ROUND_2_SAME_RECTANGLE",
                    "ROUND_2_DIAGONAL_RECTANGLE", "FLOW_CHART_ALTERNATE_PROCESS", "PIE"}


def _bezier(p0, c1, c2, p1, steps: int = BEZIER_STEPS):
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        x = mt ** 3 * p0[0] + 3 * mt ** 2 * t * c1[0] + 3 * mt * t ** 2 * c2[0] + t ** 3 * p1[0]
        y = mt ** 3 * p0[1] + 3 * mt ** 2 * t * c1[1] + 3 * mt * t ** 2 * c2[1] + t ** 3 * p1[1]
        pts.append((x, y))
    return pts


def path_polygons(path: str) -> list[list[tuple[float, float]]]:
    """The closed rings a TikZ path string built by `adopt_shapes.poly`/`rounded_poly`/`ellipse`/
    `arc` draws, back in the shape's own frame (pt, y down, origin top left - undoing `P`'s y
    flip and `arc`'s matching angle flip): one list of points per ring, curves and arcs flattened
    to short segments. Best-effort - a construct `adopt_shapes.preset` does not in fact use (there
    is none, at the time of writing) is simply skipped rather than raising."""
    rings: list[list[tuple[float, float]]] = []
    ring: list[tuple[float, float]] = []
    cur: tuple[float, float] | None = None
    pos = 0
    while pos < len(path):
        m = TOKEN.search(path, pos)
        if not m:
            break
        text = m.group(0)
        pos = m.end()
        if text == "cycle":
            if ring:
                rings.append(ring)
            ring, cur = [], None
            continue
        cm = CONTROLS.fullmatch(text)
        if cm:
            pts = CONTROL_POINTS.search(text)
            if not pts or cur is None:
                continue
            c1 = (float(pts.group(1)), -float(pts.group(2)))
            c2 = (float(pts.group(3)), -float(pts.group(4)))
            nm = POINT.search(path, pos)
            if not nm:
                break
            pos = nm.end()
            p1 = (float(nm.group(1)), -float(nm.group(2)))
            ring.extend(_bezier(cur, c1, c2, p1))
            cur = p1
            continue
        am = ARC.fullmatch(text)
        if am:
            if cur is None:
                continue
            a0, a1 = -float(am.group(1)), -float(am.group(2))
            rx, ry = float(am.group(3)), float(am.group(4))
            cx = cur[0] - rx * math.cos(math.radians(a0))
            cy = cur[1] - ry * math.sin(math.radians(a0))
            steps = max(2, round(abs(a1 - a0) / ARC_DEGREE_STEP))
            for t in np.linspace(a0, a1, steps + 1)[1:]:
                ring.append((cx + rx * math.cos(math.radians(t)), cy + ry * math.sin(math.radians(t))))
            cur = ring[-1]
            continue
        em = ELLIPSE.fullmatch(text)
        if em:
            if cur is None:
                continue
            rx, ry = float(em.group(1)), float(em.group(2))
            cx, cy = cur
            steps = max(8, round(360 / ARC_DEGREE_STEP))
            pts = [(cx + rx * math.cos(t), cy + ry * math.sin(t))
                   for t in np.linspace(0, 2 * math.pi, steps, endpoint=False)]
            if ring:
                rings.append(ring)
            rings.append(pts)
            ring, cur = [], None
            continue
        pm = POINT.fullmatch(text)
        if pm:
            x, y = float(pm.group(1)), -float(pm.group(2))
            cur = (x, y)
            if not path[pos:pos + 12].lstrip().startswith("ellipse ["):
                ring.append(cur)             # the point before "ellipse [...]" is its centre, not a vertex
            continue
    if ring:
        rings.append(ring)
    return rings


def default_rings(kind: str, w: float, h: float,
                  corner: float | None = None) -> list[list[tuple[float, float]]] | None:
    """Every ring of `adopt_shapes.preset`'s fill paths ("fs", "f" or an "-eo" fill mode) for
    `kind` in its own w by h frame - the silhouette the *default* adjustment draws - or None when
    the preset is unknown, or draws no fill at all (stroke-only: brackets, braces)."""
    from .. import adopt_shapes
    paths = adopt_shapes.preset(kind, w, h, corner)
    if not paths:
        return None
    rings: list[list[tuple[float, float]]] = []
    for path, mode in paths:
        if mode == "s":
            continue
        rings += path_polygons(path)
    return rings or None


def mirror_rings(rings: list[list[tuple[float, float]]], w: float) -> list[list[tuple[float, float]]]:
    """`rings` mirrored left to right in a w-wide frame (a flipped element's frame)."""
    return [[(w - x, y) for x, y in ring] for ring in rings]


def rasterize(rings: list[list[tuple[float, float]]], w_px: int, h_px: int, scale: float) -> Mask:
    """A boolean mask, `h_px` by `w_px`, of `rings` (the shape's own frame, pt) filled even-odd at
    `scale` pixels per pt: right for a plain fill (one ring) and for the ring pairs a hole preset
    (donut, frame, no-smoking) draws, each further ring toggling what is already covered - exactly
    as Slides' own `even odd rule` fill would render it."""
    from PIL import Image, ImageDraw
    w_px, h_px = max(1, w_px), max(1, h_px)
    acc = np.zeros((h_px, w_px), dtype=bool)
    for ring in rings:
        if len(ring) < 3:
            continue
        pts = [(x * scale, y * scale) for x, y in ring]
        layer = Image.new("L", (w_px, h_px), 0)
        ImageDraw.Draw(layer).polygon(pts, fill=255)
        acc ^= np.asarray(layer, dtype=bool)
    return acc


TOL = 14          # deck_fills.TOL: max channel distance counted as "the fill colour"
MIN_TRUE_PX = 30  # a shape with fewer visible fill-coloured pixels than this says too little to judge


def match_score(a: SignedRGB, el: dict, above: list[dict], px: float, own_words: bool = False) -> dict | None:
    """How well `adopt_shapes`' default geometry for `el` (a foreign deck's shape element, as
    `deck_ir.read_presentation` + `deck_fills.settle` leave it: `fill` already read off the
    thumbnail where the API said nothing) agrees with what the thumbnail `a` actually shows in its
    box - `{"iou", "p_in", "p_out", "n_true", "n_box", "kind"}`, or None when there is nothing to
    judge: an unknown or non-adjustable preset, no fill, a turned or sheared frame (its box is not
    its face - `deck_fills.sample_element` skips these for the same reason), or too little of the
    box visible (`deck_fills.SEEN`, mirroring "skip shapes under text or pictures") or too little
    fill-coloured ink to call it either way (`MIN_TRUE_PX`)."""
    from .. import deck_fills
    kind = (el.get("shape_type") or "").upper()
    if kind not in ADJUSTABLE_PRESETS:
        return None
    fill = deck_fills.rgb(el.get("fill"))
    if fill is None or (el.get("fill_alpha") or 1.0) < 0.99:
        return None
    fr = el.get("frame") or {}
    if abs(fr.get("rotation") or 0.0) % 360 > 0.05 or fr.get("shear"):
        return None
    h_img, w_img = a.shape[:2]
    box = deck_fills.px_box(el["bbox"], px, w_img, h_img)
    bw, bh = box[2] - box[0], box[3] - box[1]
    if bw < 6 or bh < 6:
        return None
    sub, region, allow = deck_fills.masks(a, box, above, px, own_words)
    if region.sum() < deck_fills.SEEN * region.size:
        return None                        # mostly under a text box or a picture: nothing to judge
    x0, y0, x1, y1 = el["bbox"]
    w_pt, h_pt = x1 - x0, y1 - y0
    rings = default_rings(kind, w_pt, h_pt, el.get("corner_radius"))
    if rings is None:
        return None                        # a known preset with no fill silhouette (brackets, braces)
    if fr.get("flip"):
        rings = mirror_rings(rings, w_pt)
    ours = rasterize(rings, bw, bh, px)
    close = np.abs(sub.astype(np.int16) - fill).max(axis=2) <= TOL
    theirs = close & region & ~allow
    n_true = int(theirs.sum())
    if n_true < MIN_TRUE_PX:
        return None
    seen = region & ~allow
    ours_seen = ours & seen
    inter = int((theirs & ours_seen).sum())
    union = int((theirs | ours_seen).sum())
    n_seen = int(seen.sum()) or 1
    return {
        "kind": kind,
        "iou": inter / union if union else 0.0,
        "p_in": inter / max(1, int(ours_seen.sum())),
        "p_out": (n_true - inter) / max(1, n_seen - int(ours_seen.sum())),
        "n_true": n_true,
        "n_box": int(region.size),
    }
