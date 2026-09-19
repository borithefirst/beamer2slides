"""Shapes, connectors and turned elements of a foreign deck, drawn in TikZ for `adopt`.

Slides gives a shape as a `shapeType` (one of the ~140 OOXML presets), a size and a transform; the
geometry itself is the preset's, with its default adjustment values (the API does not expose the
adjustments a person dragged). So each preset is drawn here from the OOXML presetShapeDefinitions in
the shape's own frame - its displayed width and height, before rotation - and the frame is then put
on the page by the element's transform (`cm`), which is how a turned, sheared or mirrored shape, and
one inside turned groups (`deck_ir.flatten` composes them), lands where the deck has it.

A text box is turned the same way without touching the text writer: `turned_text` wraps whatever
`textblock*` it writes in `\\adoptturned`, which boxes each block's contents and sets that box turned
about the element's centre (see `TURN_MACRO`).

What cannot be drawn: freeform shapes (`CUSTOM`, or no shapeType at all). The API has no geometry
for them, only a box; see `CUSTOM_AS`.
"""

import math

from .inverse import colour_name

K = 0.5523                      # cubic Bezier control distance for a quarter circle, per radius

# Slides' arrow heads, in line widths (measured on the corpus thumbnails: a FILL_ARROW head is
# about 4.5 line widths wide and 4-6 long, whatever the weight).
HEAD_W, HEAD_L = 4.5, 5.0
ARROW_TIPS = {
    "FILL_ARROW": "Triangle[length=0pt {l}, width=0pt {w}]",
    "STEALTH_ARROW": "Stealth[length=0pt {l}, width=0pt {w}, inset=0pt {i}]",
    "OPEN_ARROW": "Straight Barb[length=0pt {h}, width=0pt {w}]",
    "FILL_CIRCLE": "Circle[length=0pt {c}]",
    "OPEN_CIRCLE": "Circle[open, length=0pt {c}]",
    "FILL_SQUARE": "Square[length=0pt {c}]",
    "OPEN_SQUARE": "Square[open, length=0pt {c}]",
    "FILL_DIAMOND": "Turned Square[length=0pt {c}, width=0pt {c}]",
    "OPEN_DIAMOND": "Turned Square[open, length=0pt {c}, width=0pt {c}]",
}

# dash patterns in line widths (OOXML prstDash: dash 4:3, dot 1:1, lgDash 8:3, dashDot 4:3:1:3)
DASHES = {"DOT": (1, 1), "DASH": (4, 3), "DASH_DOT": (4, 3, 1, 3), "LONG_DASH": (8, 3),
          "LONG_DASH_DOT": (8, 3, 1, 3)}

# What a freeform (`CUSTOM` or no shapeType) is drawn as: its geometry is not in the API, only its box.
# Measured on sc-memphis, gdg24, firebase-jam and drawing-workshop (217 slides, boxes vs the old
# bootstrap): its box as a rectangle +0.003, as an ellipse +0.001, left out -0.030 (sc-memphis -0.29:
# its freeforms are most of the ink).
CUSTOM_AS = "rect"

TURN_MACRO = r"""\makeatletter
\newsavebox\adopt@box
% \adoptturned{angle (deg, counter-clockwise)}{centre x}{centre y}{textblocks}: every textblock* in
% the fourth argument is boxed at its own width and set turned about the centre, where it would be if
% the element were upright - the way Slides turns a text box with everything in it.
\newcommand\adoptturned[4]{\begingroup
  \expandafter\let\expandafter\adopt@tb\csname textblock*\endcsname
  \expandafter\let\expandafter\adopt@endtb\csname endtextblock*\endcsname
  \def\adopt@angle{#1}\def\adopt@cx{#2}\def\adopt@cy{#3}%
  \expandafter\def\csname textblock*\endcsname##1(##2,##3){\def\adopt@x{##2}\def\adopt@y{##3}%
    \begin{lrbox}{\adopt@box}\begin{minipage}[t]{##1}}%
  \expandafter\def\csname endtextblock*\endcsname{\end{minipage}\end{lrbox}%
    \adopt@tb{0pt}(\adopt@cx,\adopt@cy)\begin{tikzpicture}[overlay]
      \node[inner sep=0pt,outer sep=0pt,anchor=north west,rotate=\adopt@angle]
        at ([rotate=\adopt@angle]\adopt@x-\adopt@cx,\adopt@cy-\adopt@y) {\usebox\adopt@box};
    \end{tikzpicture}\adopt@endtb}%
  #4\endgroup}
\makeatother"""


def pt(v: float) -> str:
    # (a "-0.67".replace("-0", "0") here once turned every length between -1 and 0 positive)
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-", "0", "-0") else s


def P(x: float, y: float) -> str:
    """A point of the shape's frame (y down, pt) as a TikZ coordinate (y up)."""
    return f"({pt(x)}pt,{pt(-y)}pt)"


def poly(points) -> str:
    return " -- ".join(P(x, y) for x, y in points) + " -- cycle"


def rounded_poly(points, radii) -> str:
    """A closed polygon whose corner i is rounded with radius radii[i] (0 = sharp)."""
    n = len(points)
    out = []
    for i in range(n):
        (px, py), (x, y), (nx, ny) = points[i - 1], points[i], points[(i + 1) % n]
        r = radii[i]
        if r <= 0:
            out.append(("L", (x, y)))
            continue
        d1, d2 = math.hypot(x - px, y - py), math.hypot(nx - x, ny - y)
        r = min(r, d1 / 2, d2 / 2)
        a = (x + (px - x) * r / d1, y + (py - y) * r / d1)
        b = (x + (nx - x) * r / d2, y + (ny - y) * r / d2)
        c1 = (a[0] + (x - a[0]) * K, a[1] + (y - a[1]) * K)
        c2 = (b[0] + (x - b[0]) * K, b[1] + (y - b[1]) * K)
        out.append(("C", a, c1, c2, b))
    s = ""
    for k, op in enumerate(out):
        if op[0] == "L":
            s += (" -- " if k else "") + P(*op[1])
        else:
            _, a, c1, c2, b = op
            s += (" -- " if k else "") + P(*a) + f" .. controls {P(*c1)} and {P(*c2)} .. " + P(*b)
    return s + " -- cycle"


def ellipse(cx: float, cy: float, rx: float, ry: float) -> str:
    return f"{P(cx, cy)} ellipse [x radius={pt(max(rx, 0.01))}pt, y radius={pt(max(ry, 0.01))}pt]"


def arc(cx: float, cy: float, rx: float, ry: float, start: float, sweep: float, move: bool = True) -> str:
    """An elliptical arc from OOXML angle `start` (degrees, clockwise from +x, y down) through `sweep`."""
    sx = cx + rx * math.cos(math.radians(start))
    sy = cy + ry * math.sin(math.radians(start))
    head = P(sx, sy) + " " if move else ""
    return head + (f"arc [start angle={pt(-start)}, end angle={pt(-(start + sweep))}, "
                   f"x radius={pt(max(rx, 0.01))}pt, y radius={pt(max(ry, 0.01))}pt]")


def regular(n: int, w: float, h: float, inner: float | None = None, offset: float = -90.0):
    """A regular polygon (or star with inner radius ratio `inner`) stretched to fill the box."""
    pts = []
    for k in range(n):
        a = math.radians(offset + 360 * k / n)
        pts.append((math.cos(a), math.sin(a)))
        if inner is not None:
            a2 = math.radians(offset + 360 * (k + 0.5) / n)
            pts.append((inner * math.cos(a2), inner * math.sin(a2)))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return [((x - x0) / (x1 - x0) * w, (y - y0) / (y1 - y0) * h) for x, y in pts]


# irregularSeal1/2 on OOXML's 21600 grid
SEAL1 = [(10800, 5800), (14522, 0), (14155, 5325), (18380, 4457), (16702, 7315), (21097, 8137),
         (17607, 10475), (21600, 13290), (16837, 12942), (18145, 18095), (14020, 14457), (13247, 19737),
         (10532, 14935), (8485, 21600), (7715, 15627), (4762, 17617), (5667, 13937), (135, 14587),
         (3722, 11775), (0, 8615), (4627, 7617), (370, 2295), (7312, 6320), (8352, 2295)]
SEAL2 = [(11462, 4342), (14790, 0), (14525, 5777), (18007, 3172), (16380, 6532), (21600, 6645),
         (16985, 9402), (18270, 11290), (16380, 12310), (18877, 15632), (14640, 14350), (14942, 17370),
         (12180, 15935), (11612, 18842), (9872, 17370), (8700, 19712), (7527, 18125), (4917, 21600),
         (4805, 18240), (1285, 17825), (3330, 15370), (0, 12877), (3935, 11592), (1172, 8270),
         (5372, 7817), (4502, 3625), (8550, 6382), (9722, 1887)]

STARS = {"STAR_4": (4, 0.25), "STAR_5": (5, 0.38196), "STAR_6": (6, 0.57735), "STAR_7": (7, 0.69202),
         "STAR_8": (8, 0.765), "STAR_10": (10, 0.85066), "STAR_12": (12, 0.75), "STAR_16": (16, 0.75),
         "STAR_24": (24, 0.75), "STAR_32": (32, 0.75)}
POLYGONS = {"PENTAGON": 5, "HEPTAGON": 7, "DECAGON": 10, "DODECAGON": 12}


def arrow_polygon(kind: str, w: float, h: float):
    """Block arrows: shaft half the cross size (adj1 50000), head as long as half the short side."""
    ss = min(w, h)
    if kind in ("RIGHT_ARROW", "LEFT_ARROW", "NOTCHED_RIGHT_ARROW"):
        x1 = w - ss / 2
        y1, y2 = h / 4, 3 * h / 4
        pts = [(0, y1), (x1, y1), (x1, 0), (w, h / 2), (x1, h), (x1, y2), (0, y2)]
        if kind == "NOTCHED_RIGHT_ARROW":
            pts.append((ss / 4, h / 2))
        if kind == "LEFT_ARROW":
            pts = [(w - x, y) for x, y in pts]
        return pts
    if kind in ("DOWN_ARROW", "UP_ARROW"):
        y1 = h - ss / 2
        x1, x2 = w / 4, 3 * w / 4
        pts = [(x1, 0), (x2, 0), (x2, y1), (w, y1), (w / 2, h), (0, y1), (x1, y1)]
        if kind == "UP_ARROW":
            pts = [(x, h - y) for x, y in pts]
        return pts
    if kind == "LEFT_RIGHT_ARROW":
        x1, x2 = ss / 2, w - ss / 2
        y1, y2 = h / 4, 3 * h / 4
        return [(0, h / 2), (x1, 0), (x1, y1), (x2, y1), (x2, 0), (w, h / 2), (x2, h), (x2, y2), (x1, y2), (x1, h)]
    if kind == "UP_DOWN_ARROW":
        y1, y2 = ss / 2, h - ss / 2
        x1, x2 = w / 4, 3 * w / 4
        return [(w / 2, 0), (w, y1), (x2, y1), (x2, y2), (w, y2), (w / 2, h), (0, y2), (x1, y2), (x1, y1), (0, y1)]
    return None


def callout_tail(w: float, h: float):
    """The wedge callouts' default tail tip (adj1 -20833, adj2 62500): below the box, left of centre."""
    return w / 2 - w * 0.20833, h / 2 + h * 0.625


def preset(kind: str, w: float, h: float) -> list[tuple[str, str]] | None:
    """[(TikZ path in the shape's frame, mode)] for a Slides shapeType, mode "fs" (fill and stroke),
    "f" (fill only), "s" (stroke only), or "shade+"/"shade-" (the fill lightened/darkened, as OOXML
    draws a cube's top or a folded corner). None when the preset is not known here."""
    ss = min(w, h)
    rect = [(0, 0), (w, 0), (w, h), (0, h)]
    if kind in ("RECTANGLE", "FLOW_CHART_PROCESS", "TEXT_BOX", "HORIZONTAL_SCROLL", "VERTICAL_SCROLL",
                "BEVEL", "PLAQUE"):
        return [(poly(rect), "fs")]
    rounded = {"ROUND_RECTANGLE": (1, 1, 1, 1), "ROUND_1_RECTANGLE": (0, 1, 0, 0),
               "ROUND_2_SAME_RECTANGLE": (1, 1, 0, 0), "ROUND_2_DIAGONAL_RECTANGLE": (1, 0, 1, 0)}
    if kind in rounded:
        return [(rounded_poly(rect, [c * ss * 0.16667 for c in rounded[kind]]), "fs")]
    if kind == "FLOW_CHART_ALTERNATE_PROCESS":
        return [(rounded_poly(rect, [ss / 6] * 4), "fs")]
    snips = {"SNIP_1_RECTANGLE": (0, 1, 0, 0), "SNIP_2_SAME_RECTANGLE": (1, 1, 0, 0),
             "SNIP_2_DIAGONAL_RECTANGLE": (0, 1, 0, 1)}
    if kind in snips or kind == "SNIP_ROUND_RECTANGLE":
        d = ss * 0.16667
        tl, tr, br, bl = snips.get(kind, (0, 1, 0, 0))
        pts = []
        pts += [(0, d), (d, 0)] if tl else [(0, 0)]
        pts += [(w - d, 0), (w, d)] if tr else [(w, 0)]
        pts += [(w, h - d), (w - d, h)] if br else [(w, h)]
        pts += [(d, h), (0, h - d)] if bl else [(0, h)]
        if kind == "SNIP_ROUND_RECTANGLE":
            return [(rounded_poly([(0, 0), (w - d, 0), (w, d), (w, h), (0, h)], [d, 0, 0, 0, 0]), "fs")]
        return [(poly(pts), "fs")]
    if kind in ("ELLIPSE", "FLOW_CHART_CONNECTOR", "CLOUD", "CLOUD_CALLOUT", "FLOW_CHART_SUMMING_JUNCTION",
                "FLOW_CHART_OR"):
        return [(ellipse(w / 2, h / 2, w / 2, h / 2), "fs")]
    if kind in ("TRIANGLE", "FLOW_CHART_EXTRACT"):
        return [(poly([(0, h), (w / 2, 0), (w, h)]), "fs")]
    if kind == "FLOW_CHART_MERGE":
        return [(poly([(0, 0), (w, 0), (w / 2, h)]), "fs")]
    if kind == "RIGHT_TRIANGLE":
        return [(poly([(0, 0), (0, h), (w, h)]), "fs")]
    if kind in ("DIAMOND", "FLOW_CHART_DECISION"):
        return [(poly([(w / 2, 0), (w, h / 2), (w / 2, h), (0, h / 2)]), "fs")]
    if kind == "PARALLELOGRAM":
        x = ss * 0.25
        return [(poly([(0, h), (x, 0), (w, 0), (w - x, h)]), "fs")]
    if kind == "FLOW_CHART_INPUT_OUTPUT":
        return [(poly([(0, h), (w / 5, 0), (w, 0), (4 * w / 5, h)]), "fs")]
    if kind == "TRAPEZOID":
        x = ss * 0.25
        return [(poly([(0, h), (x, 0), (w - x, 0), (w, h)]), "fs")]
    if kind == "FLOW_CHART_MANUAL_OPERATION":
        return [(poly([(0, 0), (w, 0), (0.8 * w, h), (0.2 * w, h)]), "fs")]
    if kind == "FLOW_CHART_MANUAL_INPUT":
        return [(poly([(0, 0.2 * h), (w, 0), (w, h), (0, h)]), "fs")]
    if kind == "FLOW_CHART_OFFPAGE_CONNECTOR":
        return [(poly([(0, 0), (w, 0), (w, 0.8 * h), (w / 2, h), (0, 0.8 * h)]), "fs")]
    if kind in POLYGONS:
        return [(poly(regular(POLYGONS[kind], w, h)), "fs")]
    if kind in ("HEXAGON", "FLOW_CHART_PREPARATION"):
        x = ss * 0.25 if kind == "HEXAGON" else w / 5
        return [(poly([(0, h / 2), (x, 0), (w - x, 0), (w, h / 2), (w - x, h), (x, h)]), "fs")]
    if kind == "OCTAGON":
        x = ss * 0.29289
        return [(poly([(x, 0), (w - x, 0), (w, x), (w, h - x), (w - x, h), (x, h), (0, h - x), (0, x)]), "fs")]
    if kind == "HOME_PLATE":
        x = w - ss / 2
        return [(poly([(0, 0), (x, 0), (w, h / 2), (x, h), (0, h)]), "fs")]
    if kind == "CHEVRON":
        x = ss / 2
        return [(poly([(0, 0), (w - x, 0), (w, h / 2), (w - x, h), (0, h), (x, h / 2)]), "fs")]
    pts = arrow_polygon(kind, w, h)
    if pts:
        return [(poly(pts), "fs")]
    if kind == "STRIPED_RIGHT_ARROW":
        x1 = w - ss / 2
        y1, y2 = h / 4, 3 * h / 4
        return [(poly([(0, y1), (ss / 32, y1), (ss / 32, y2), (0, y2)]), "fs"),
                (poly([(ss / 16, y1), (ss / 8, y1), (ss / 8, y2), (ss / 16, y2)]), "fs"),
                (poly([(5 * ss / 32, y1), (x1, y1), (x1, 0), (w, h / 2), (x1, h), (x1, y2), (5 * ss / 32, y2)]), "fs")]
    if kind == "PLUS":
        x = ss * 0.25
        return [(poly([(0, x), (x, x), (x, 0), (w - x, 0), (w - x, x), (w, x), (w, h - x), (w - x, h - x),
                       (w - x, h), (x, h), (x, h - x), (0, h - x)]), "fs")]
    if kind in ("MATH_PLUS", "MATH_MINUS", "MATH_EQUAL", "MATH_MULTIPLY"):
        t = ss * 0.2352 / 2                              # half the bar thickness
        dx, dy = w * 0.7349 / 2, h * 0.7349 / 2
        if kind == "MATH_MINUS":
            return [(poly([(w / 2 - dx, h / 2 - t), (w / 2 + dx, h / 2 - t), (w / 2 + dx, h / 2 + t), (w / 2 - dx, h / 2 + t)]), "fs")]
        if kind == "MATH_EQUAL":
            g = ss * 0.1176 / 2
            return [(poly([(w / 2 - dx, h / 2 - g - 2 * t), (w / 2 + dx, h / 2 - g - 2 * t), (w / 2 + dx, h / 2 - g), (w / 2 - dx, h / 2 - g)]), "fs"),
                    (poly([(w / 2 - dx, h / 2 + g), (w / 2 + dx, h / 2 + g), (w / 2 + dx, h / 2 + g + 2 * t), (w / 2 - dx, h / 2 + g + 2 * t)]), "fs")]
        if kind == "MATH_PLUS":
            cx, cy = w / 2, h / 2
            return [(poly([(cx - dx, cy - t), (cx - t, cy - t), (cx - t, cy - dy), (cx + t, cy - dy), (cx + t, cy - t),
                           (cx + dx, cy - t), (cx + dx, cy + t), (cx + t, cy + t), (cx + t, cy + dy), (cx - t, cy + dy),
                           (cx - t, cy + t), (cx - dx, cy + t)]), "fs")]
        out = []                                         # two bars along the diagonals of the inner box
        for (ax, ay), (bx, by) in (((w * 0.15, h * 0.15), (w * 0.85, h * 0.85)), ((w * 0.85, h * 0.15), (w * 0.15, h * 0.85))):
            ln = math.hypot(bx - ax, by - ay)
            nx, ny = -(by - ay) / ln * t, (bx - ax) / ln * t
            out.append((poly([(ax + nx, ay + ny), (bx + nx, by + ny), (bx - nx, by - ny), (ax - nx, ay - ny)]), "fs"))
        return out
    if kind in ("DONUT", "NO_SMOKING"):
        d = ss * (0.25 if kind == "DONUT" else 0.1875)
        ring = ellipse(w / 2, h / 2, w / 2, h / 2) + " " + ellipse(w / 2, h / 2, w / 2 - d, h / 2 - d)
        out = [(ring, "fs-eo")]
        if kind == "NO_SMOKING":
            a = math.radians(45)
            ax, ay = w / 2 + (w / 2 - d) * math.cos(a + math.pi), h / 2 + (h / 2 - d) * math.sin(a + math.pi)
            bx, by = w / 2 + (w / 2 - d) * math.cos(a), h / 2 + (h / 2 - d) * math.sin(a)
            ln = math.hypot(bx - ax, by - ay) or 1
            nx, ny = -(by - ay) / ln * d / 2, (bx - ax) / ln * d / 2
            out.append((poly([(ax + nx, ay + ny), (bx + nx, by + ny), (bx - nx, by - ny), (ax - nx, ay - ny)]), "fs"))
        return out
    if kind == "FRAME":
        d = ss * 0.125
        return [(poly(rect) + " " + poly([(d, d), (w - d, d), (w - d, h - d), (d, h - d)]), "fs-eo")]
    if kind == "CORNER":
        d = ss / 2
        return [(poly([(0, 0), (d, 0), (d, h - d), (w, h - d), (w, h), (0, h)]), "fs")]
    if kind == "HEART":
        dx1, dx2 = w * 49 / 48, w * 10 / 48
        y1 = -h / 3
        return [(f"{P(w / 2, h / 4)} .. controls {P(w / 2 + dx2, y1)} and {P(w / 2 + dx1, h / 4)} .. {P(w / 2, h)}"
                 f" .. controls {P(w / 2 - dx1, h / 4)} and {P(w / 2 - dx2, y1)} .. {P(w / 2, h / 4)} -- cycle", "fs")]
    if kind == "PIE":
        return [(f"{P(w / 2, h / 2)} -- {arc(w / 2, h / 2, w / 2, h / 2, 0, 270)} -- cycle", "fs")]
    if kind == "ARC":
        return [(f"{P(w / 2, h / 2)} -- {arc(w / 2, h / 2, w / 2, h / 2, 270, 90)} -- cycle", "f"),
                (arc(w / 2, h / 2, w / 2, h / 2, 270, 90), "s")]
    if kind in STARS:
        n, r = STARS[kind]
        return [(poly(regular(n, w, h, r)), "fs")]
    if kind in ("IRREGULAR_SEAL_1", "IRREGULAR_SEAL_2"):
        pts = SEAL1 if kind == "IRREGULAR_SEAL_1" else SEAL2
        return [(poly([(x * w / 21600, y * h / 21600) for x, y in pts]), "fs")]
    if kind in ("WEDGE_RECTANGLE_CALLOUT", "WEDGE_ROUND_RECTANGLE_CALLOUT"):
        tx, ty = callout_tail(w, h)
        pts = [(0, 0), (w, 0), (w, h), (w * 5 / 12, h), (tx, ty), (w * 2 / 12, h), (0, h)]
        radii = [ss * 0.16667 if kind == "WEDGE_ROUND_RECTANGLE_CALLOUT" else 0] * 3 + [0, 0, 0] + \
                [ss * 0.16667 if kind == "WEDGE_ROUND_RECTANGLE_CALLOUT" else 0]
        return [(rounded_poly(pts, radii), "fs")]
    if kind == "WEDGE_ELLIPSE_CALLOUT":
        tx, ty = callout_tail(w, h)
        a = math.degrees(math.atan2((ty - h / 2) / (h / 2), (tx - w / 2) / (w / 2)))
        return [(f"{P(tx, ty)} -- {arc(w / 2, h / 2, w / 2, h / 2, a + 11, 338)} -- cycle", "fs")]
    if kind == "FLOW_CHART_TERMINATOR":
        rx = w * 3475 / 21600
        return [(f"{P(rx, 0)} -- {P(w - rx, 0)} {arc(w - rx, h / 2, rx, h / 2, 270, 180, move=False)} -- {P(rx, h)} "
                 f"{arc(rx, h / 2, rx, h / 2, 90, 180, move=False)} -- cycle", "fs")]
    if kind == "FLOW_CHART_ONLINE_STORAGE":
        rx = w / 6
        return [(f"{P(rx, 0)} -- {P(w, 0)} {arc(w, h / 2, rx, h / 2, 270, -180, move=False)} -- {P(rx, h)} "
                 f"{arc(rx, h / 2, rx, h / 2, 90, 180, move=False)} -- cycle", "fs")]
    if kind == "FLOW_CHART_DELAY":
        return [(f"{P(0, 0)} -- {P(w / 2, 0)} {arc(w / 2, h / 2, w / 2, h / 2, 270, 180, move=False)} -- {P(0, h)} -- cycle", "fs")]
    if kind in ("FLOW_CHART_DOCUMENT", "FLOW_CHART_MULTIDOCUMENT"):
        def doc(x0, y0, dw, dh):
            f = lambda x, y: (x0 + x * dw / 21600, y0 + y * dh / 21600)   # noqa: E731
            return (f"{P(*f(0, 0))} -- {P(*f(21600, 0))} -- {P(*f(21600, 17322))} .. controls {P(*f(10800, 17322))}"
                    f" and {P(*f(10800, 23922))} .. {P(*f(0, 20172))} -- cycle")
        if kind == "FLOW_CHART_DOCUMENT":
            return [(doc(0, 0, w, h), "fs")]
        dw, dh = w * 18800 / 21600, h * 18000 / 21600
        return [(doc(w - dw, 0, dw, dh), "fs"), (doc((w - dw) / 2, (h - dh) / 2, dw, dh), "fs"), (doc(0, h - dh, dw, dh), "fs")]
    if kind == "FLOW_CHART_PUNCHED_TAPE":
        return [(f"{P(0, 0.1 * h)} .. controls {P(0.17 * w, 0.3 * h)} and {P(0.33 * w, 0.3 * h)} .. {P(0.5 * w, 0.1 * h)}"
                 f" .. controls {P(0.67 * w, -0.1 * h)} and {P(0.83 * w, -0.1 * h)} .. {P(w, 0.1 * h)} -- {P(w, 0.9 * h)}"
                 f" .. controls {P(0.83 * w, 0.7 * h)} and {P(0.67 * w, 0.7 * h)} .. {P(0.5 * w, 0.9 * h)}"
                 f" .. controls {P(0.33 * w, 1.1 * h)} and {P(0.17 * w, 1.1 * h)} .. {P(0, 0.9 * h)} -- cycle", "fs")]
    if kind == "FLOW_CHART_PREDEFINED_PROCESS":
        return [(poly(rect), "fs"), (f"{P(w / 8, 0)} -- {P(w / 8, h)} {P(7 * w / 8, 0)} -- {P(7 * w / 8, h)}", "s")]
    if kind in ("CAN", "FLOW_CHART_MAGNETIC_DISK"):
        y1 = ss * 0.125 if kind == "CAN" else h / 6
        body = (f"{P(0, y1)} -- {P(0, h - y1)} {arc(w / 2, h - y1, w / 2, y1, 180, -180, move=False)} -- {P(w, y1)} "
                f"{arc(w / 2, y1, w / 2, y1, 0, -180, move=False)} -- cycle")
        return [(body, "fs"), (ellipse(w / 2, y1, w / 2, y1), "shade+")]
    if kind == "CUBE":
        d = ss * 0.25
        return [(poly([(0, d), (w - d, d), (w - d, h), (0, h)]), "fs"),
                (poly([(0, d), (d, 0), (w, 0), (w - d, d)]), "shade+"),
                (poly([(w - d, d), (w, 0), (w, h - d), (w - d, h)]), "shade-")]
    if kind == "FOLDED_CORNER":
        d = ss * 0.16667
        return [(poly([(0, 0), (w, 0), (w, h - d), (w - d, h), (0, h)]), "fs"),
                (poly([(w - d, h), (w - d * 0.8, h - d * 0.8), (w, h - d)]), "shade-")]
    if kind in ("LEFT_BRACKET", "RIGHT_BRACKET", "BRACKET_PAIR"):
        r = ss * 0.08333 if kind != "BRACKET_PAIR" else ss * 0.16667
        rw = w if kind != "BRACKET_PAIR" else r
        left = f"{P(rw, 0)} {arc(rw, r, rw, r, 270, -90, move=False)} -- {P(0, h - r)} {arc(rw, h - r, rw, r, 180, -90, move=False)}"
        right = f"{P(w - rw, 0)} {arc(w - rw, r, rw, r, 270, 90, move=False)} -- {P(w, h - r)} {arc(w - rw, h - r, rw, r, 0, 90, move=False)}"
        return [({"LEFT_BRACKET": left, "RIGHT_BRACKET": right}.get(kind, left + " " + right), "s")]
    if kind in ("LEFT_BRACE", "RIGHT_BRACE", "BRACE_PAIR"):
        def brace(x0, bw, flip):
            f = (lambda x, y: P(x0 + bw - x, y)) if not flip else (lambda x, y: P(x0 + x, y))
            r = min(ss * 0.0833, h / 4)
            return (f"{f(0, 0)} .. controls {f(bw / 2, 0)} and {f(bw / 2, 0)} .. {f(bw / 2, r)} -- {f(bw / 2, h / 2 - r)}"
                    f" .. controls {f(bw / 2, h / 2)} and {f(bw / 2, h / 2)} .. {f(bw, h / 2)}"
                    f" .. controls {f(bw / 2, h / 2)} and {f(bw / 2, h / 2)} .. {f(bw / 2, h / 2 + r)} -- {f(bw / 2, h - r)}"
                    f" .. controls {f(bw / 2, h)} and {f(bw / 2, h)} .. {f(0, h)}")
        if kind == "LEFT_BRACE":
            return [(brace(0, w, False), "s")]
        if kind == "RIGHT_BRACE":
            return [(brace(0, w, True), "s")]
        bw = ss * 0.0833 * 2
        return [(brace(0, bw, False) + " " + brace(w - bw, bw, True), "s")]
    return None


def dash_option(dash: str | None, weight: float) -> list[str]:
    pattern = DASHES.get(dash or "SOLID")
    if not pattern:
        return []
    w = max(weight, 0.3)
    return ["dash pattern=" + " ".join(f"{'on' if k % 2 == 0 else 'off'} {pt(v * w)}pt" for k, v in enumerate(pattern))]


def style_options(el: dict, ctx, stroke_only: bool = False) -> tuple[list[str], list[str]]:
    """(fill options, stroke options) for an element: colours, opacities, weight and dashes."""
    fill, stroke = el.get("fill"), el.get("outline") or el.get("outline_color")
    fo, so = [], []
    gradient = el.get("fill_gradient")
    if gradient and not stroke_only:
        # read off the thumbnail (`deck_fills`): an axis shading through three colours
        a, b, c = (colour_name(x, ctx.colours) for x in gradient["colors"])
        # (the middle colour last: setting an end colour resets it to the mean of the two)
        first, last = ("left", "right") if gradient["axis"] == "x" else ("top", "bottom")
        fo += [f"{first} color={a}", f"{last} color={c}", f"middle color={b}"]
    elif fill and not stroke_only:
        fo.append(f"fill={colour_name(fill, ctx.colours)}")
        if el.get("fill_alpha") is not None and el["fill_alpha"] < 0.995:
            fo.append(f"fill opacity={el['fill_alpha']:.3f}")
    if stroke:
        weight = el.get("weight") or 0.75
        so += [f"draw={colour_name(stroke, ctx.colours)}", f"line width={weight:.2f}pt"]
        if el.get("outline_alpha") is not None and el["outline_alpha"] < 0.995:
            so.append(f"draw opacity={el['outline_alpha']:.3f}")
        so += dash_option(el.get("dash"), weight)
    return fo, so


def frame_of(el: dict) -> dict:
    """The element's frame: displayed size, the page position of its (0,0) corner and the unit
    linear part of its transform. Upright elements without one get it from their box."""
    fr = el.get("frame")
    if fr:
        return fr
    x0, y0, x1, y1 = el["bbox"]
    return {"size": [x1 - x0, y1 - y0], "origin": [x0, y0], "matrix": [1.0, 0.0, 0.0, 1.0]}


def transform_option(fr: dict, x0: float, y0: float) -> str:
    """`cm` putting the frame (TikZ coordinates, y up, origin at its corner) where the deck has it,
    relative to the tikzpicture's origin at page (x0, y0)."""
    q0, q1, q2, q3 = fr["matrix"]
    ox, oy = fr["origin"]
    tx, ty = ox - x0, -(oy - y0)
    if abs(q0 - 1) < 1e-6 and abs(q3 - 1) < 1e-6 and abs(q1) < 1e-6 and abs(q2) < 1e-6:
        return f"shift={{({pt(tx)}pt,{pt(ty)}pt)}}"
    return f"cm={{{q0:.5f},{-q2:.5f},{-q1:.5f},{q3:.5f},({pt(tx)}pt,{pt(ty)}pt)}}"


def tikz_block(body: str, x0: float, y0: float, w: float, h: float, ind: str) -> str:
    """A tikzpicture whose bounding box is the element's box on the page, hanging from the top of
    its textblock, so that its origin is at page (x0, y0) whatever is drawn (arrow heads, a callout's
    tail and strokes may reach out of the box: they overlap, the box stays)."""
    return (f"{ind}\\begin{{textblock*}}{{{max(w, 0.1):.1f}pt}}({x0:.1f}pt,{y0:.1f}pt)\n"
            f"{ind}  \\begin{{tikzpicture}}[baseline=(current bounding box.north),inner sep=0pt,outer sep=0pt]\n"
            f"{ind}    \\useasboundingbox (0pt,0pt) rectangle ({max(w, 0.01):.2f}pt,{-max(h, 0.01):.2f}pt);\n"
            f"{ind}    {body}\n"
            f"{ind}  \\end{{tikzpicture}}\n"
            f"{ind}\\end{{textblock*}}\n")


def connector_path(el: dict, w: float, h: float) -> str:
    """A connector in its own frame (0,0)-(w,h): straight, elbow (bentConnector2/3/4 with their
    default adjustments) or curved (curvedConnector2/3/4)."""
    kind = el.get("line_type") or ""
    if "BENT_CONNECTOR_2" in kind:
        return f"{P(0, 0)} -- {P(w, 0)} -- {P(w, h)}"
    if "BENT_CONNECTOR_3" in kind:
        return f"{P(0, 0)} -- {P(w / 2, 0)} -- {P(w / 2, h)} -- {P(w, h)}"
    if "BENT_CONNECTOR_4" in kind or "BENT_CONNECTOR_5" in kind:
        return f"{P(0, 0)} -- {P(w / 2, 0)} -- {P(w / 2, h / 2)} -- {P(w, h / 2)} -- {P(w, h)}"
    if "CURVED_CONNECTOR_2" in kind:
        return f"{P(0, 0)} .. controls {P(w / 2, 0)} and {P(w, h / 2)} .. {P(w, h)}"
    if "CURVED_CONNECTOR" in kind:
        return (f"{P(0, 0)} .. controls {P(w / 4, 0)} and {P(w / 2, h / 4)} .. {P(w / 2, h / 2)}"
                f" .. controls {P(w / 2, 3 * h / 4)} and {P(3 * w / 4, h)} .. {P(w, h)}")
    return f"{P(0, 0)} -- {P(w, h)}"


def tip(kind: str | None) -> str:
    spec = ARROW_TIPS.get(kind or "NONE")
    if not spec:
        return ""
    return spec.format(l=HEAD_L, w=HEAD_W, i=HEAD_L * 0.35, h=HEAD_L * 0.55, c=HEAD_W * 0.8)


def freeform_line(el: dict) -> bool:
    """A "line" with neither lineType nor lineCategory is a freeform polyline or polygon (an elbow drawn
    point by point in jeb-arch s4, a filled wedge in journey-maps s2): the API gives only its box, and
    the segment corner to corner that its transform describes is ink the deck does not have."""
    return "category" in el and not el["category"] and not el.get("line_type")


def traced_path(rings, x0: float, y0: float) -> str:
    """Traced rings (page pt) as one TikZ path relative to the page point (x0, y0)."""
    return " ".join(" -- ".join(P(x - x0, y - y0) for x, y in ring) + " -- cycle" for ring in rings)


def traced_block(el: dict, ctx, ind: str) -> str:
    """A freeform whose outline `deck_freeforms` traced from the slide's picture: one filled path
    (even-odd: the rings of a letter-like shape nest), and where the deck outlines it in another
    colour, that outline drawn just inside the traced edge (clipped to it, twice as wide), which is
    where the picture shows it."""
    tr = el["trace"]
    ctx.packages.add("\\usepackage{tikz}")
    x0, y0, x1, y1 = el["bbox"]
    path = traced_path(tr["rings"], x0, y0)
    opts = [f"fill={colour_name(tr['fill'], ctx.colours)}", "even odd rule"]
    if tr.get("alpha") is not None and tr["alpha"] < 0.995:
        opts.append(f"fill opacity={tr['alpha']:.3f}")
    body = [f"\\path[{','.join(opts)}] {path};"]
    if tr.get("stroke"):
        body.append(f"\\begin{{scope}}[even odd rule]\\clip {path};")
        body.append(f"\\path[draw={colour_name(tr['stroke'], ctx.colours)},line width={2 * (tr.get('weight') or 0.75):.2f}pt] {path};")
        body.append("\\end{scope}")
    return tikz_block(("\n" + ind + "    ").join(body), x0, y0, x1 - x0, y1 - y0, ind)


def line_block(el: dict, ctx, ind: str) -> str:
    """A line or connector, with its heads, dashes and transparency."""
    if el.get("trace"):
        return traced_block(el, ctx, ind)
    stroke = el.get("outline")
    if not stroke or freeform_line(el):
        return ""
    ctx.packages.add("\\usepackage{tikz}")
    _, so = style_options(el, ctx, stroke_only=True)
    start = tip(el.get("start_arrow") or ("FILL_ARROW" if el.get("arrow_start") else None))
    end = tip(el.get("end_arrow") or ("FILL_ARROW" if el.get("arrow") else None))
    if start or end:
        ctx.packages.add("\\usetikzlibrary{arrows.meta}")
        so.append(f"{{{start}}}-{{{end}}}".replace("{}", ""))
    x0, y0, x1, y1 = el["bbox"]
    fr = el.get("frame")
    if fr and el.get("line_type") and "STRAIGHT" not in el["line_type"]:
        w, h = fr["size"]
        body = f"\\path[{','.join(so)},{transform_option(fr, x0, y0)}] {connector_path(el, w, h)};"
    else:
        (ax, ay), (bx, by) = el["from"], el["to"]
        body = f"\\path[{','.join(so)}] {P(ax - x0, ay - y0)} -- {P(bx - x0, by - y0)};"
    return tikz_block(body, x0, y0, x1 - x0, y1 - y0, ind)


def shape_block(el: dict, ctx, ind: str) -> str:
    """A shape in its preset's geometry, filled and outlined as the deck has it, turned and mirrored
    by its transform. A freeform is drawn as `deck_freeforms` traced it from the slide's picture,
    else as `CUSTOM_AS` says (its geometry is not in the API)."""
    if el.get("trace"):
        return traced_block(el, ctx, ind)
    fo, so = style_options(el, ctx)
    if not fo and not so:
        return ""
    kind = (el.get("shape_type") or el.get("shape") or "RECTANGLE").upper()
    fr = frame_of(el)
    w, h = max(fr["size"][0], 0.01), max(fr["size"][1], 0.01)
    if kind in ("CUSTOM", "?", "FREEFORM"):
        paths = preset("ELLIPSE" if CUSTOM_AS == "ellipse" else "RECTANGLE", w, h)
    else:
        paths = preset(kind, w, h) or preset("RECTANGLE", w, h)
    ctx.packages.add("\\usepackage{tikz}")
    x0, y0, x1, y1 = el["bbox"]
    where = transform_option(fr, x0, y0)
    body = []
    for path, mode in paths:
        opts = []
        if mode.startswith("fs"):
            opts = fo + so
        elif mode == "f":
            opts = fo
        elif mode == "s":
            opts = so
        elif mode.startswith("shade"):
            if fo:                                       # the face in the fill, then lightened/darkened
                body.append(f"\\path[{','.join(fo + [where])}] {path};")
                opts = [f"fill={'white' if mode == 'shade+' else 'black'}", "fill opacity=0.2"]
            opts = opts + so
        if mode.endswith("-eo") and opts:
            opts.append("even odd rule")
        if not opts:
            continue
        body.append(f"\\path[{','.join(opts + [where])}] {path};")
    if not body:
        return ""
    return tikz_block(("\n" + ind + "    ").join(body), x0, y0, x1 - x0, y1 - y0, ind)


def turned_text(latex: str, el: dict, ctx) -> str:
    """The text writer's `textblock*`s for a turned element, set turned about its centre."""
    fr = el.get("frame") or {}
    angle = fr.get("rotation") or 0.0
    if abs(angle) < 0.05 or not latex.strip():
        return latex
    x0, y0, x1, y1 = fr["box"]
    ctx.packages.add("\\usepackage{tikz}")
    ctx.packages.add(TURN_MACRO)
    return (f"  \\adoptturned{{{-angle:.2f}}}{{{(x0 + x1) / 2:.2f}pt}}{{{(y0 + y1) / 2:.2f}pt}}{{%\n"
            + latex + "}")
