"""Diagrams: nodes, connectors and labels as shapes, and the groups a block's or a frame's pieces make.

A diagram is planned from its parsed record (`DiagramElement`: `diagram_requests_of`), its nodes'
templates and labels from a `NodeLook` (`node_look`). The names without `_of` take the dicts sync
and the tests hold: `diagram_requests`, `element_template_keys`, `label_inside`,
`node_template_key`, `bend_template_key`, `connection`.
"""

import math
from collections.abc import Callable, Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass, replace
from typing import Literal

from . import curves, ir_types
from .emit_metrics import PAD_X, FontMapper, letter_face_style, letter_faces, u16
from .emit_model import (
    JsonMap, NodeLook, NodeSite, Template, TemplateKey, box_of, node_look, node_look_of, node_site_of, set_card,
    set_runs, template_of,
)
from .emit_pptx import arc_kind, template_key
from .emit_text import LINE_MARGIN, Page, baseline_offset, in_sentence_of, run_sizes_of, text_box_requests_of
from .emit_widths import slides_width_of
from .google_types import (
    AffineTransform, LineConnection, LineProperties, Outline, ShapeProperties, SlidesRequest, slides_text_style,
)
from .gslides import EMU_PER_PT, emu, pt, rgb_color, text_color
from .ir import Arrow, Bend, TemplateKind
from .ir_types import Box, DiagramElement, DiagramLine, Point
from .json_types import Json
from .typing_compat import assert_never


# Share of a preset shape's width its text may use: Slides lays text out in the shape's .pptx
# text rectangle (an ellipse's is its inscribed square, a diamond's half its width).
TEXT_RECT_WIDTH: dict[str, float] = {"RECTANGLE": 1.0, "ROUND_RECTANGLE": 0.9, "ELLIPSE": 0.707, "DIAMOND": 0.5,
                                     # (a can's text rectangle is its body, l to r; a cloud's
                                     # w 2977/21600 to w 17087/21600; punched tape's l to r)
                                     "CAN": 1.0, "CLOUD": 0.653, "FLOW_CHART_PUNCHED_TAPE": 1.0}
# Connection sites of preset shapes in their .pptx order (fractions of the box): a line connected
# to a site follows the shape when it is moved in Slides. (CAN, HEXAGON and OCTAGON have sites
# where their adjustment puts them: `connection_sites`.)
CONNECTION_SITES: dict[str, list[tuple[float, float]]] = {
    "RECTANGLE": [(0.5, 0), (0, 0.5), (0.5, 1), (1, 0.5)],
    "ROUND_RECTANGLE": [(0.5, 0), (0, 0.5), (0.5, 1), (1, 0.5)],
    "DIAMOND": [(0.5, 0), (0, 0.5), (0.5, 1), (1, 0.5)],
    "ELLIPSE": [(0.5, 0), (0.1464, 0.1464), (0, 0.5), (0.1464, 0.8536), (0.5, 1), (0.8536, 0.8536), (1, 0.5), (0.8536, 0.1464)],
    "TRIANGLE": [(0.5, 0), (0.25, 0.5), (0, 1), (0.5, 1), (1, 1), (0.75, 0.5)],
    "CLOUD": [(21582 / 21600, 0.5), (0.5, 21577 / 21600), (67 / 21600, 0.5), (0.5, 1235 / 21600)],
    "FLOW_CHART_PUNCHED_TAPE": [(0.5, 0.1), (0, 0.5), (0.5, 0.9), (1, 0.5)],
}
# The presets whose proportions take an adjustment (ir.Node `adjust`), and its OOXML default.
ADJUSTED: dict[str, float] = {"CAN": 0.25, "HEXAGON": 0.25, "OCTAGON": 0.29289}
LABEL_ROOM = 1.08  # the substitute font may run this much wider


def label_inside(node: JsonMap) -> bool:
    """`label_inside_of` a node dict (sync's base diagrams, the tests)."""
    return label_inside_of(node_look_of(node))


def label_inside_of(node: NodeLook) -> bool:
    """A node's label goes into the node shape itself (it then moves and resizes with it) when
    it fits the shape's text rectangle without wrapping."""
    x0, _, x1, _ = node.bbox
    share = text_rect_width(node.shape, node.bbox, node.adjust)
    return bool(node.text) and share is not None and node.label_w * LABEL_ROOM <= (x1 - x0) * share - 0.5


def adjust_of(shape: TemplateKind | None, adjust: float | None) -> float | None:
    """The adjustment a node's preset is drawn at: the node's, else the preset's default; None
    for a preset that takes none."""
    if shape is None or shape not in ADJUSTED:
        return None
    return ADJUSTED[shape] if adjust is None else adjust


def text_rect_width(shape: TemplateKind | None, bbox: Box, adjust: float | None) -> float | None:
    """The share of a node's width its preset's text rectangle spans (None: a preset whose label
    is never put inside). An octagon's is inset half its cut on each side, a hexagon's as OOXML's
    guides say (its corners on the slanted sides)."""
    x0, y0, x1, y1 = bbox
    w, ss = max(x1 - x0, 0.01), max(min(x1 - x0, y1 - y0), 0.01)
    a = adjust_of(shape, adjust)
    if shape == "OCTAGON" and a is not None:
        return max(0.0, 1 - a * ss / w)
    if shape == "HEXAGON" and a is not None:
        most = 0.5 * w / ss  # maxAdj
        a = min(a, most)
        q8 = 2 + 4 * a / most if a <= most / 2 else 1 + 6 * a / most
        return 1 - q8 / 12
    return None if shape is None else TEXT_RECT_WIDTH.get(shape)


def connection_sites(shape: TemplateKind | None, bbox: Box, adjust: float | None) -> list[tuple[float, float]]:
    """A node preset's connection sites in its .pptx order (fractions of its box)."""
    x0, y0, x1, y1 = bbox
    w, h = max(x1 - x0, 0.01), max(y1 - y0, 0.01)
    ss = min(w, h)
    a = adjust_of(shape, adjust)
    if shape == "CAN" and a is not None:
        # (Google's can has five sites, the lid's front first: measured live, a line connected
        # to site i read back - OOXML's four had the bottom at 2, where Google has the left side)
        return [(0.5, min(0.5, a) * ss / h), (0.5, 0), (0, 0.5), (0.5, 1), (1, 0.5)]
    if shape == "HEXAGON" and a is not None:
        fx = min(a * ss / w, 0.5)
        return [(1, 0.5), (1 - fx, 1), (fx, 1), (0, 0.5), (fx, 0), (1 - fx, 0)]
    if shape == "OCTAGON" and a is not None:
        fx, fy = min(a, 0.5) * ss / w, min(a, 0.5) * ss / h
        return [(1, fy), (1, 1 - fy), (1 - fx, 1), (fx, 1), (0, 1 - fy), (0, fy), (fx, 0), (1 - fx, 0)]
    return [] if shape is None else CONNECTION_SITES.get(shape, [])


def node_template_key(node: JsonMap) -> TemplateKey:
    """`node_template_key_of` a node dict (sync's base diagrams)."""
    return node_template_key_of(node_look_of(node))


def node_template_key_of(node: NodeLook) -> TemplateKey:
    """A node's template: its preset, and for rounded corners of a known radius the preset's
    adjustment (as `template_key` gives a panel's) - createShape only makes the default. A can,
    a hexagon or an octagon of a measured adjustment carries it too."""
    x0, y0, x1, y1 = node.bbox
    adj = None
    if node.shape == "ROUND_RECTANGLE" and node.radius:
        adj = min(0.5, round(node.radius / max(min(x1 - x0, y1 - y0), 0.01), 2))
    elif node.shape in ADJUSTED and node.adjust is not None:
        adj = round(node.adjust, 2)
    # (a free label is never templated: node_templated_of)
    return "" if node.shape is None else node.shape, adj, None


def node_templated_of(node: NodeLook) -> bool:
    """A node copied from a template shape: its label fits inside, or its corners have a radius."""
    return bool(node.shape) and (label_inside_of(node) or node_template_key_of(node)[1] is not None)


def bend_template_key(line: JsonMap) -> TemplateKey:
    """`bend_template_key_of` a line dict's `bend` (sync's base diagrams)."""
    return bend_template_key_of(_bend(line["bend"]))


def bend_template_key_of(bend: Bend) -> TemplateKey:
    """An elbow connector (bentConnector3) turning at its start (|-, adj 0) or its end (-|).
    classify writes only |- now (at adj 1 Slides drew the turn halfway); an old base's -|
    lines keep their key, so their live objects are never copied for a |- one."""
    match bend:
        case "vh":
            return "BENT_CONNECTOR", 0.0, None
        case "hv":
            return "BENT_CONNECTOR", 1.0, None
        case _:
            assert_never(bend)


def _bend(value: Json) -> Bend:
    for b in ir_types.BENDS:
        if value == b:
            return b
    raise TypeError(f"bend: {value!r} is not one of {ir_types.BENDS}")


def element_template_keys(el: JsonMap, scale: float) -> list[TemplateKey]:
    """The template shapes an element dict may be copied from (a panel's, a diagram's nodes',
    elbows' and arcs'), whatever its page: an arc's every form (`arc_template_keys`). sync asks
    so for the elements it writes (DeckPlan's `keys` are the page's own: `element_template_keys_on`)."""
    return _template_keys(el, scale, arc_template_keys)


def element_template_keys_on(el: JsonMap, scale: float, page: Page) -> list[TemplateKey]:
    """The template shapes an element dict is copied from on its slide's `page` (slide pt): an
    arc's are those of the form `arc_plan` gives it there. DeckPlan's, for every element."""
    def arc_keys(line: JsonMap) -> list[TemplateKey]:
        sweep = _sweep(line)
        if sweep is None:
            return []
        start, end = _point(line["from"]), _point(line["to"])
        arrow_from, arrow_to = _arrow(line.get("arrow_from")), _arrow(line.get("arrow_to"))
        return arc_plan_keys(sweep, arrow_from, arrow_to, arc_plan(start, end, sweep, scale, page))
    return _template_keys(el, scale, arc_keys)


def _template_keys(el: JsonMap, scale: float, arc_keys: Callable[[JsonMap], list[TemplateKey]]) -> list[TemplateKey]:
    if el["kind"] == "diagram":
        nodes = [node_look_of(_object(n)) for n in _items(el["nodes"])]
        bends = [_bend(bend) for ln in _items(el["lines"]) if (bend := _object(ln).get("bend"))]
        return [node_template_key_of(n) for n in nodes if node_templated_of(n)] + [bend_template_key_of(b) for b in bends] \
            + [k for ln in _items(el["lines"]) for k in arc_keys(_object(ln))]
    key = template_key(el, scale)
    return [key] if key else []


def diagram_template_keys(el: DiagramElement) -> list[TemplateKey]:
    """`element_template_keys` of a parsed diagram."""
    nodes = [node_look(n) for n in el.nodes]
    return [node_template_key_of(n) for n in nodes if node_templated_of(n)] + \
        [bend_template_key_of(ln.elbow.bend) for ln in el.lines if ln.elbow is not None] + \
        [k for ln in el.lines if ln.sweep is not None
         for k in arc_keys_any(ln.from_, ln.to, ln.sweep, ln.arrow_from, ln.arrow_to)]


def arc_template_keys(line: JsonMap) -> list[TemplateKey]:
    """Every template a line dict's arc may be copied from, whatever its page (`arc_keys_any`)."""
    sweep = _sweep(line)
    if sweep is None:
        return []
    return arc_keys_any(_point(line["from"]), _point(line["to"]), sweep,
                        _arrow(line.get("arrow_from")), _arrow(line.get("arrow_to")))


def arc_keys_any(start: Point, end: Point, sweep: float, arrow_from: Arrow | None,
                 arrow_to: Arrow | None) -> list[TemplateKey]:
    """Every template an arc may be copied from, whatever its page: turned, upright and its
    elliptical pieces' (`arc_plan`)."""
    keys = [_arc_key(sweep, arrow_from, arrow_to)]
    at = upright_at(start, end, sweep)
    if at != 0:
        keys += arc_plan_keys(sweep, arrow_from, arrow_to, ArcPlan(form="upright", at=at, pieces=()))
    pieces = ellipse_pieces(start, end, sweep)
    if pieces is not None:
        keys += arc_plan_keys(sweep, arrow_from, arrow_to, ArcPlan(form="ellipses", at=0.0, pieces=pieces))
    return list(dict.fromkeys(keys))


def _sweep(line: JsonMap) -> float | None:
    sweep = line.get("sweep")
    if sweep is None:
        return None
    if isinstance(sweep, bool) or not isinstance(sweep, (int, float)):
        raise TypeError(f"sweep: {sweep!r} is not a number")
    return float(sweep)


def _point(value: Json) -> Point:
    items = _items(value)
    if len(items) != 2:
        raise TypeError(f"{value!r} is not a point")
    x, y = items
    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        raise TypeError(f"{value!r} is not a point")
    return float(x), float(y)


def arc_template_key(line: JsonMap) -> TemplateKey | None:
    """`arc_template_key_of` a line dict (sync's base diagrams); None for a line that is no arc."""
    sweep = _sweep(line)
    if sweep is None:
        return None
    return _arc_key(sweep, _arrow(line.get("arrow_from")), _arrow(line.get("arrow_to")))


def arc_template_key_of(ln: DiagramLine) -> TemplateKey | None:
    """A turned arc's template (emit_pptx's `arc` preset): its heads and its sweep as drawn
    (`curves.drawn_sweep`). A copy is turned and mirrored to its place (`arc_transform`), so
    neither where the arc starts nor which way it turns makes another template. (The one sync
    finds under the line's id; an arc's other forms: `arc_plan`, `arc_keys_any`.)"""
    return None if ln.sweep is None else _arc_key(ln.sweep, ln.arrow_from, ln.arrow_to)


def _arc_key(sweep: float, arrow_from: Arrow | None, arrow_to: Arrow | None) -> TemplateKey:
    return arc_kind(arrow_from, arrow_to), abs(curves.drawn_sweep(sweep)), None


def _arrow(value: Json) -> Arrow | None:
    if value is None:
        return None
    for a in ir_types.ARROWS:
        if value == a:
            return a
    raise TypeError(f"arrow: {value!r} is not one of {ir_types.ARROWS}")


def _items(value: Json) -> Sequence[Json]:
    # (a tuple where a list goes: dicts built in Python, never JSON)
    if isinstance(value, (list, tuple)):
        return value
    raise TypeError(f"{value!r} is not a list")


def _object(value: Json) -> JsonMap:
    if isinstance(value, dict):
        return value
    raise TypeError(f"{value!r} is not an object")


def connection(point: Sequence[float], nodes: Sequence[JsonMap], oids: Sequence[str]) -> LineConnection | None:
    """`connection_of` node dicts (the tests')."""
    x, y = point
    return connection_of((x, y), [node_site_of(n) for n in nodes], oids)


def connection_of(point: Point, nodes: Sequence[NodeSite], oids: Sequence[str]) -> LineConnection | None:
    """The node connection site a line end sits on (PDF pt, within 1.5 pt), if any. `nodes`: each
    node's box, shape, adjustment and turn (a turned node's sites turn with it)."""
    best: tuple[float, LineConnection] | None = None
    for (bbox, shape, adjust, rotation), oid in zip(nodes, oids):
        for index, (fx, fy) in enumerate(connection_sites(shape, bbox, adjust)):
            sx, sy = site_point(bbox, fx, fy, rotation)
            d = math.hypot(point[0] - sx, point[1] - sy)
            if d <= 1.5 and (best is None or d < best[0]):
                best = (d, {"connectedObjectId": oid, "connectionSiteIndex": index})
    return best[1] if best else None


def arrow_style(arrow: Arrow | None) -> Arrow | Literal["NONE"]:
    return "NONE" if arrow is None else arrow


def diagram_requests(el: JsonMap, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                     template: Callable[[TemplateKey], JsonMap] | None) -> list[SlidesRequest]:
    """`diagram_requests_of` a diagram dict (the tests'), `template(key)` giving a dict ({"id",
    "w", "h"})."""
    typed = ir_types.parse_element(el, "diagram")
    if not isinstance(typed, DiagramElement):
        raise TypeError(f"element {el.get('id')!r} is not a diagram")
    tpl = template
    return diagram_requests_of(typed, slide_id, object_id, scale, fonts,
                               None if tpl is None else (lambda key: template_of(tpl(key))), None)


def _transform(oid: str, sx: float, sy: float, x: float, y: float) -> SlidesRequest:
    """An ABSOLUTE transform of a copied template: from its origin along +size (PDF pt x scale)."""
    return {"updatePageElementTransform": {"objectId": oid, "applyMode": "ABSOLUTE", "transform": {
        "scaleX": sx, "scaleY": sy, "unit": "EMU", "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}}


def _copied(template: Template, oid: str, sx: float, sy: float, x: float, y: float) -> list[SlidesRequest]:
    return [{"duplicateObject": {"objectId": template.id, "objectIds": {template.id: oid}}},
            _transform(oid, sx, sy, x, y),
            {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}}]


def turned_transform(w: float, h: float, sx: float, sy: float, box: Box, rotation: float) -> AffineTransform:
    """The transform putting an object of its own size `w` x `h` (pt), scaled by `sx`, `sy`, on
    `box` (slide pt, the box before the turn) turned `rotation` degrees clockwise about the box's
    centre: rotation . scale, translated so the object's centre lands on the box's."""
    x0, y0, x1, y1 = box
    angle = math.radians(rotation)
    cos, sin = math.cos(angle), math.sin(angle)
    a, b, c, d = cos * sx, -sin * sy, sin * sx, cos * sy
    tx = (x0 + x1) / 2 - (a * w + b * h) / 2
    ty = (y0 + y1) / 2 - (c * w + d * h) / 2
    return {"scaleX": round(a, 6), "shearX": round(b, 6), "shearY": round(c, 6), "scaleY": round(d, 6),
            "unit": "EMU", "translateX": round(tx * EMU_PER_PT), "translateY": round(ty * EMU_PER_PT)}


def box_transform(box: Box, rotation: float | None) -> AffineTransform:
    """A created shape's transform (its size `box`'s): at the box's corner, or turned about the
    box's centre (ir.Node `rotation`)."""
    x0, y0, x1, y1 = box
    if rotation is None:
        return {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                "translateX": round(x0 * EMU_PER_PT), "translateY": round(y0 * EMU_PER_PT)}
    return turned_transform(x1 - x0, y1 - y0, 1.0, 1.0, box, rotation)


def copied_on(template: Template, oid: str, box: Box, rotation: float | None) -> list[SlidesRequest]:
    """A template copied onto a node's box (`_copied`), turned about its centre when the node is
    (ir.Node `rotation`)."""
    x0, y0, x1, y1 = box
    sx, sy = (x1 - x0) / template.w, (y1 - y0) / template.h
    if rotation is None:
        return _copied(template, oid, sx, sy, x0, y0)
    return [{"duplicateObject": {"objectId": template.id, "objectIds": {template.id: oid}}},
            {"updatePageElementTransform": {"objectId": oid, "applyMode": "ABSOLUTE",
                                            "transform": turned_transform(template.w, template.h, sx, sy, box, rotation)}},
            {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}}]


def site_point(box: Box, fx: float, fy: float, rotation: float | None) -> Point:
    """Where a connection site (fractions of the box) is on the page: on the box, or turned with
    it about its centre."""
    x0, y0, x1, y1 = box
    if rotation is None:
        return x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)
    angle = math.radians(rotation)
    cos, sin = math.cos(angle), math.sin(angle)
    dx, dy = (fx - 0.5) * (x1 - x0), (fy - 0.5) * (y1 - y0)
    return (x0 + x1) / 2 + cos * dx - sin * dy, (y0 + y1) / 2 + sin * dx + cos * dy


CHORD_STEP = 15.0  # degrees of an arc one straight piece stands for, where no template is at hand
KINK = 2.0         # degrees two straight pieces of an arc turn at their join, where no curve fits its page
ELLIPSE_TOLERANCE = curves.ARC_TOLERANCE / 2  # pt: how far an elliptical piece may run from its arc
ELLIPSE_SPAN = 35.0  # degrees: the least half span (parametric) a piece takes of its ellipse, else more pieces
MAX_ELLIPSES = 6     # elliptical pieces one arc may become
ELLIPSE_SAMPLES = 64  # points of a piece its distance from its arc is measured on


# An arc's ARC preset copy is its whole circle's box, however little of the circle it draws: a
# gentle bend's reaches far past the page (29_tikz_diagrams pages 3 and 4). `arc_plan` writes it,
# in this order, as the first form whose box lies on the page:
# - "turned": the template running from 0 degrees, turned to the arc's start (`arc_transform`);
# - "upright": a template starting where the arc starts (to `ARC_STEP`), so the box is the
#   circle's own upright square (turned by under half a degree);
# - "ellipses": the arc in a few pieces, each the top of a flat ellipse (`Ellipse`): the preset
#   about its template's top, scaled unevenly. Each piece leaves its ends in the circle's
#   directions, so the heads point as the arc's and the pieces join without a kink; each stays
#   within `ELLIPSE_TOLERANCE` of the arc;
# - "chords": straight pieces turning `KINK` degrees at most at a join (`fine_step`).
# The ellipses take Slides at its word that a preset is drawn at the size it shows (its
# rounded corners are), so the `arc` preset's angles are OOXML's: seen from the ellipse's centre
# (`ellipse_axes`). (Not yet seen live: 29_tikz_diagrams pages 3 and 4 are the check.)
ArcForm = Literal["turned", "upright", "ellipses", "chords"]


@dataclass(frozen=True, kw_only=True)
class Ellipse:
    """One piece of an arc drawn as the top of an ellipse: from `start` to `end` (PDF pt), standing
    for `sweep` degrees of the arc's circle (signed as the arc's). Its template is the preset `arc`
    over `visual` degrees about its top (`ellipse_key`), the ellipse's half axes `a` along the
    piece's chord and `b` across it (PDF pt); it runs at most `deviation` pt from the arc."""
    start: Point
    end: Point
    sweep: float
    visual: float
    a: float
    b: float
    deviation: float


@dataclass(frozen=True, kw_only=True)
class ArcPlan:
    """How an arc is written (`arc_plan`): its form, the template's start angle of an upright
    copy (`at`, degrees; 0 for the others) and an elliptical arc's pieces."""
    form: ArcForm
    at: float
    pieces: tuple[Ellipse, ...]


def ellipse_axes(chord: float, sweep: float, visual: float) -> tuple[float, float, float]:
    """(a, b, phi): the ellipse whose top runs through both ends of a piece of circle (turning
    `sweep` degrees, unsigned, over `chord` pt) and leaves them in the circle's directions, on
    which the piece spans `visual` degrees seen from its centre - the arc preset's angles (OOXML's
    `arc`: its ends are where rays at adj1 and adj2 meet the ellipse) - and `phi` radians on
    either side of the top as its parameter runs. With `visual` = `sweep` it is the circle."""
    half = math.radians(sweep) / 2
    psi = math.radians(90 - visual / 2)  # how far an end sees above the centre
    # an end at parameter phi: x = a sin phi = chord / 2; its slope (b / a) tan phi = tan half;
    # seen from the centre at tan psi = (b / a) cot phi
    phi = math.atan(math.sqrt(math.tan(half) / math.tan(psi)))
    a = chord / (2 * math.sin(phi))
    return a, a * math.tan(half) / math.tan(phi), phi


def ellipse_deviation(chord: float, sweep: float, a: float, b: float, phi: float) -> float:
    """How far the ellipse's top (`ellipse_axes`) runs from the piece of circle it stands for, pt."""
    half = math.radians(sweep) / 2
    r = chord / (2 * math.sin(half))
    below = r * math.cos(half)  # (the chord on y = 0, both bulging up: the circle's centre below)
    worst = 0.0
    for i in range(ELLIPSE_SAMPLES + 1):
        t = -phi + 2 * phi * i / ELLIPSE_SAMPLES
        x, y = a * math.sin(t), b * (math.cos(t) - math.cos(phi))
        worst = max(worst, abs(math.hypot(x, y + below) - r))
    return worst


def piece_visual(chord: float, sweep: float) -> float | None:
    """The widest span seen from its centre (a multiple of `curves.ARC_STEP` below 180 degrees:
    the template's) of an ellipse standing for a piece of circle within `ELLIPSE_TOLERANCE`; the
    wider, the flatter and smaller its box. None when the piece turns half a turn or more."""
    def off(steps: int) -> float:
        a, b, phi = ellipse_axes(chord, sweep, steps * curves.ARC_STEP)
        return ellipse_deviation(chord, sweep, a, b, phi)
    lo, hi = math.ceil(sweep / curves.ARC_STEP - 1e-9), round(180 / curves.ARC_STEP) - 1
    if lo > hi or off(lo) > ELLIPSE_TOLERANCE:
        return None
    while lo < hi:  # (the farther from the circle, the farther it runs from it)
        mid = (lo + hi + 1) // 2
        if off(mid) <= ELLIPSE_TOLERANCE:
            lo = mid
        else:
            hi = mid - 1
    return lo * curves.ARC_STEP


def ellipse_pieces(start: Point, end: Point, sweep: float) -> tuple[Ellipse, ...] | None:
    """An arc as the fewest equal pieces whose ellipses take `ELLIPSE_SPAN` or more of their
    span within `ELLIPSE_TOLERANCE` (`piece_visual`); None past `MAX_ELLIPSES`. The plan is the
    arc's alone, whatever its page, so its templates are known before the page is."""
    circle = curves.arc_circle(start, end, sweep)
    turn = abs(sweep)
    for k in range(int(turn // 180) + 1, MAX_ELLIPSES + 1):
        part = turn / k
        chord = 2 * circle.radius * math.sin(math.radians(part) / 2)
        visual = piece_visual(chord, part)
        if visual is None:
            continue
        a, b, phi = ellipse_axes(chord, part, visual)
        if math.degrees(phi) < ELLIPSE_SPAN:
            continue
        off = ellipse_deviation(chord, part, a, b, phi)
        points = [start, *(curves.arc_point(circle, j * sweep / k) for j in range(1, k)), end]
        return tuple(Ellipse(start=points[j], end=points[j + 1], sweep=sweep / k, visual=visual, a=a, b=b,
                             deviation=off) for j in range(k))
    return None


def ellipse_key(visual: float, arrow_from: Arrow | None, arrow_to: Arrow | None) -> TemplateKey:
    """An elliptical piece's template: the preset `arc` over `visual` degrees about its top
    (from 270 - visual / 2 clockwise), with the heads the piece carries."""
    return arc_kind(arrow_from, arrow_to), visual, 270 - visual / 2


def upright_at(start: Point, end: Point, sweep: float) -> float:
    """The start (degrees, to `curves.ARC_STEP`) of a template whose copy stands upright on the
    arc's circle: mirrored for an anticlockwise arc, it is turned by less than half a step."""
    drawn = curves.drawn_sweep(sweep)
    turn = 1.0 if drawn > 0 else -1.0
    start_angle = curves.arc_circle(start, end, drawn).start
    return (round(turn * start_angle / curves.ARC_STEP) * curves.ARC_STEP) % 360


def frame_corners(centre: Point, angle: float, half_u: float, half_v: float, scale: float) -> list[Point]:
    """The corners (slide pt) of a box of half sides `half_u`, `half_v` (PDF pt) about `centre`,
    turned `angle` radians."""
    cos, sin = math.cos(angle), math.sin(angle)
    return [((centre[0] + cos * du - sin * dv) * scale, (centre[1] + sin * du + cos * dv) * scale)
            for du in (-half_u, half_u) for dv in (-half_v, half_v)]


def within(corners: Sequence[Point], page: Page) -> bool:
    return min(x for x, _ in corners) >= -0.01 and min(y for _, y in corners) >= -0.01 and \
        max(x for x, _ in corners) <= page.width + 0.01 and max(y for _, y in corners) <= page.height + 0.01


def circle_corners(start: Point, end: Point, sweep: float, at: float, scale: float) -> list[Point]:
    """The corners of an arc's circle copy (`arc_transform`) from a template starting at `at`."""
    drawn = curves.drawn_sweep(sweep)
    circle = curves.arc_circle(start, end, drawn)
    turn = 1.0 if drawn > 0 else -1.0
    r = circle.radius
    return frame_corners(circle.centre, math.radians(circle.start - turn * at), r, r, scale)


def ellipse_centre(piece: Ellipse) -> tuple[Point, float, float]:
    """(centre, the chord's angle in radians, turn) of an elliptical piece: its ends are the
    piece's, so its centre is b cos(phi) from the chord's middle towards the circle's centre."""
    (x0, y0), (x1, y1) = piece.start, piece.end
    angle = math.atan2(y1 - y0, x1 - x0)
    turn = 1.0 if piece.sweep > 0 else -1.0
    nx, ny = -math.sin(angle) * turn, math.cos(angle) * turn  # (the template's down: towards the circle's centre)
    across = piece.b * math.sqrt(max(0.0, 1 - (math.hypot(x1 - x0, y1 - y0) / (2 * piece.a)) ** 2))
    return ((x0 + x1) / 2 + nx * across, (y0 + y1) / 2 + ny * across), angle, turn


def ellipse_corners(piece: Ellipse, scale: float) -> list[Point]:
    centre, angle, _ = ellipse_centre(piece)
    return frame_corners(centre, angle, piece.a, piece.b, scale)


def arc_plan(start: Point, end: Point, sweep: float, scale: float, page: Page | None) -> ArcPlan:
    """How an arc is written within `page` (the header above): turned, upright, ellipses or
    chords, the first whose boxes lie on it. With no page, turned."""
    turned = ArcPlan(form="turned", at=0.0, pieces=())
    if page is None or within(circle_corners(start, end, sweep, 0.0, scale), page):
        return turned
    at = upright_at(start, end, sweep)
    if at != 0 and within(circle_corners(start, end, sweep, at, scale), page):
        return ArcPlan(form="upright", at=at, pieces=())
    pieces = ellipse_pieces(start, end, sweep)
    if pieces is not None and all(within(ellipse_corners(p, scale), page) for p in pieces):
        return ArcPlan(form="ellipses", at=0.0, pieces=pieces)
    return ArcPlan(form="chords", at=0.0, pieces=())


def piece_heads(j: int, count: int, arrow_from: Arrow | None, arrow_to: Arrow | None) -> tuple[Arrow | None, Arrow | None]:
    """The heads piece `j` of `count` carries: the start's on the first, the end's on the last."""
    return arrow_from if j == 0 else None, arrow_to if j == count - 1 else None


def arc_plan_keys(sweep: float, arrow_from: Arrow | None, arrow_to: Arrow | None, plan: ArcPlan) -> list[TemplateKey]:
    """The templates an arc written as `plan` is copied from."""
    match plan.form:
        case "turned":
            return [_arc_key(sweep, arrow_from, arrow_to)]
        case "upright":
            return [(arc_kind(arrow_from, arrow_to), abs(curves.drawn_sweep(sweep)), plan.at)]
        case "ellipses":
            n = len(plan.pieces)
            return [ellipse_key(p.visual, *piece_heads(j, n, arrow_from, arrow_to)) for j, p in enumerate(plan.pieces)]
        case "chords":
            return []
        case _:
            assert_never(plan.form)


def arc_transform(tpl: Template, ln: DiagramLine, sweep: float, scale: float, at: float) -> AffineTransform:
    """The transform putting an arc's template (the preset `arc` from `at` degrees clockwise
    through the drawn sweep, in a square `tpl.w` x `tpl.h`) on its circle (`curves.arc_circle`):
    scaled to the circle, mirrored when it turns anticlockwise, turned to start where it starts."""
    drawn = curves.drawn_sweep(sweep)
    circle = curves.arc_circle(ln.from_, ln.to, drawn)
    turn = 1.0 if drawn > 0 else -1.0
    # rotation . mirror(turn) . scale: the template's point at angle a goes to rotation + turn * a
    return _placed(tpl, circle.centre, math.radians(circle.start - turn * at), turn,
                   2 * circle.radius * scale / tpl.w, 2 * circle.radius * scale / tpl.h, scale)


def ellipse_transform(tpl: Template, piece: Ellipse, scale: float) -> AffineTransform:
    """The transform putting an elliptical piece's template (`ellipse_key`, a square `tpl.w` x
    `tpl.h`) on its ellipse: scaled to 2a x 2b, turned to its chord, mirrored for an
    anticlockwise arc (its top bulges away from the circle's centre either way)."""
    centre, angle, turn = ellipse_centre(piece)
    return _placed(tpl, centre, angle, turn, 2 * piece.a * scale / tpl.w, 2 * piece.b * scale / tpl.h, scale)


def _placed(tpl: Template, centre: Point, angle: float, turn: float, sx: float, sy: float,
            scale: float) -> AffineTransform:
    """rotation(angle) . mirror(turn) . scale(sx, sy), the template's centre on `centre` (PDF pt)."""
    cos, sin = math.cos(angle), math.sin(angle)
    a, b, c, d = cos * sx, -sin * turn * sy, sin * sx, cos * turn * sy
    tx = centre[0] * scale - (a * tpl.w + b * tpl.h) / 2
    ty = centre[1] * scale - (c * tpl.w + d * tpl.h) / 2
    return {"scaleX": round(a, 6), "shearX": round(b, 6), "shearY": round(c, 6), "scaleY": round(d, 6),
            "unit": "EMU", "translateX": round(tx * EMU_PER_PT), "translateY": round(ty * EMU_PER_PT)}


def arc_requests(tpl: Template, oid: str, ln: DiagramLine, transform: AffineTransform,
                 scale: float) -> list[SlidesRequest]:
    """An arc (or a piece of one) copied from its template and put in place by one ABSOLUTE
    transform (`arc_transform`, `ellipse_transform`). Its ends land on the arc's; its outline
    takes the line's colour and weight (the template's heads stay)."""
    outline: Outline = {"outlineFill": {"solidFill": {"color": rgb_color(ln.stroke)}},
                        "weight": pt(round(max(0.5, ln.width * scale), 2))}
    if ln.dash is not None:  # (a solid arc writes no dash, as a solid line)
        outline["dashStyle"] = ln.dash
    return [{"duplicateObject": {"objectId": tpl.id, "objectIds": {tpl.id: oid}}},
            {"updatePageElementTransform": {"objectId": oid, "applyMode": "ABSOLUTE",
                                            "transform": transform}},
            {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}},
            {"updateShapeProperties": {
                "objectId": oid,
                "fields": "shapeBackgroundFill.propertyState,outline.outlineFill.solidFill.color,outline.weight"
                          + (",outline.dashStyle" if ln.dash is not None else ""),
                "shapeProperties": {"shapeBackgroundFill": {"propertyState": "NOT_RENDERED"},
                                    "outline": outline}}}]


def arc_chords(oid: str, ln: DiagramLine, sweep: float) -> list[tuple[str, Point, Point, DiagramLine]]:
    """An arc as straight pieces of at most `CHORD_STEP` degrees (no template to copy): the first
    keeps the start's head and object id, the last the end's head."""
    return arc_chords_by(oid, ln, sweep, CHORD_STEP)


def fine_step(ln: DiagramLine, sweep: float) -> float:
    """The degrees of an arc one straight piece stands for where no curve fits its page: no more
    than turns `KINK` at a join, nor than keeps the chord within `curves.ARC_TOLERANCE` of it."""
    radius = curves.arc_circle(ln.from_, ln.to, sweep).radius
    if radius <= curves.ARC_TOLERANCE:
        return KINK
    return min(KINK, math.degrees(2 * math.acos(1 - curves.ARC_TOLERANCE / radius)))


def arc_chords_by(oid: str, ln: DiagramLine, sweep: float, step: float) -> list[tuple[str, Point, Point, DiagramLine]]:
    """An arc as straight pieces of at most `step` degrees: the first keeps the start's head and
    object id, the last the end's head."""
    n = max(2, math.ceil(abs(sweep) / step))
    circle = curves.arc_circle(ln.from_, ln.to, sweep)
    points = [ln.from_, *(curves.arc_point(circle, k * sweep / n) for k in range(1, n)), ln.to]
    return [(oid if k == 0 else f"{oid}c{k}", points[k], points[k + 1],
             replace(ln, sweep=None, arrow_from=ln.arrow_from if k == 0 else None,
                     arrow_to=ln.arrow_to if k == n - 1 else None))
            for k in range(n)]


def label_width_on(w: float, cx: float, paragraphs: Sequence[Sequence[ir_types.Run]], scale: float,
                   fonts: FontMapper, page: Page) -> float:
    """The width of a label's own box (`w`, centred on `cx`, Slides pt) narrowed about its middle
    as far as it reaches past `page`, down to its widest line as Slides sets it, `LABEL_ROOM`
    wider, and `LINE_MARGIN` between the insets: it is centred, so its words stay where they were.
    A label nobody can measure keeps its box."""
    over = max(w / 2 - cx, cx + w / 2 - page.width)
    if over <= 0:
        return w
    widths = [slides_width_of(in_sentence_of(set_runs(line)), scale, fonts) for line in paragraphs]
    known = [v for v in widths if v is not None]
    if len(known) != len(widths) or not known:
        return w
    least = max(known) * LABEL_ROOM + LINE_MARGIN + 2 * PAD_X
    return min(w, max(least, w - 2 * over))


ON_NODE_INSET = 0.5  # pt: a line end this far inside a node's box is on the node, not at its rim


def on_filled_node(start: Point, end: Point, nodes: Sequence[ir_types.Node]) -> bool:
    """Whether a line lies wholly inside a filled node (a legend's swatch on its key box, an edge
    within a filled frame around a group): drawn after the nodes, or the fill hides it."""
    def inside(box: Box, p: Point) -> bool:
        x0, y0, x1, y1 = box
        return x0 + ON_NODE_INSET < p[0] < x1 - ON_NODE_INSET and y0 + ON_NODE_INSET < p[1] < y1 - ON_NODE_INSET
    return any(n.shape is not None and n.fill is not None and inside(n.bbox, start) and inside(n.bbox, end)
               for n in nodes)


def diagram_requests_of(el: DiagramElement, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                        template: Callable[[TemplateKey], Template] | None, page: Page | None) -> list[SlidesRequest]:
    """Nodes become shapes, edges become lines with arrow heads; the parts are grouped so the
    diagram moves as one piece but stays editable. A label that fits goes inside its node (a
    template shape without text padding, see label_inside); one that doesn't gets a text box
    grouped with its node. Line ends on a node's connection site are connected to it, so edges
    follow nodes moved in Slides. `template(key)` gives this slide's template shape for a key.
    With `page`, nothing is written past it that the diagram does not draw there: an arc whose
    circle's box would reach past it is written as `arc_plan` says (upright, elliptical pieces,
    else fine chords; its heads on the end pieces), and a label's box narrows about its middle.
    Only a turned arc is `<id>_l<j>`, as sync looks for an arc's template under the line's id: an
    upright one is `_l<j>u`, pieces `_l<j>e<k>`, fine chords `_l<j>s`, `_l<j>sc<k>`."""
    reqs: list[SlidesRequest] = []
    children: list[str] = []
    node_oids = [f"{object_id}_n{j}" for j in range(len(el.nodes))]
    # (object id, from, to, line): elbows without a template fall back to two lines
    segments: list[tuple[str, Point, Point, DiagramLine]] = []
    # an arc's object id -> its template's key, its elliptical piece or None, an upright template's start
    arcs: dict[str, tuple[TemplateKey, Ellipse | None, float]] = {}
    for j, ln in enumerate(el.lines):
        if ln.elbow is not None and template is None:
            segments += [(f"{object_id}_l{j}", ln.from_, ln.elbow.via, replace(ln, arrow_to=None)),
                         (f"{object_id}_l{j}b", ln.elbow.via, ln.to, replace(ln, arrow_from=None))]
        elif ln.sweep is not None and template is None:
            segments += arc_chords(f"{object_id}_l{j}", ln, ln.sweep)
        elif ln.sweep is not None:
            plan = arc_plan(ln.from_, ln.to, ln.sweep, scale, page)
            keys = arc_plan_keys(ln.sweep, ln.arrow_from, ln.arrow_to, plan)
            match plan.form:
                case "turned" | "upright":
                    oid = f"{object_id}_l{j}" + ("" if plan.form == "turned" else "u")
                    segments.append((oid, ln.from_, ln.to, ln))
                    arcs[oid] = (keys[0], None, plan.at)
                case "ellipses":
                    n = len(plan.pieces)
                    for k, (piece, key) in enumerate(zip(plan.pieces, keys)):
                        start_head, end_head = piece_heads(k, n, ln.arrow_from, ln.arrow_to)
                        oid = f"{object_id}_l{j}e{k}"
                        segments.append((oid, piece.start, piece.end,
                                         replace(ln, sweep=piece.sweep, arrow_from=start_head, arrow_to=end_head)))
                        arcs[oid] = (key, piece, 0.0)
                case "chords":
                    segments += arc_chords_by(f"{object_id}_l{j}s", ln, ln.sweep, fine_step(ln, ln.sweep))
                case _:
                    assert_never(plan.form)
        else:
            segments.append((f"{object_id}_l{j}", ln.from_, ln.to, ln))

    def draw(oid: str, start: Point, end: Point, ln: DiagramLine) -> None:
        (x1, y1), (x2, y2) = start, end
        dx, dy = (x2 - x1) * scale, (y2 - y1) * scale
        drawn = arcs.get(oid)
        if drawn is not None and ln.sweep is not None and template is not None:
            # A curve's arc: a copy of its template (which brought its heads along) put on its
            # circle, or on its piece's ellipse.
            key, piece, at = drawn
            tpl = template(key)
            transform = arc_transform(tpl, ln, ln.sweep, scale, at) if piece is None else \
                ellipse_transform(tpl, piece, scale)
            reqs.extend(arc_requests(tpl, oid, ln, transform, scale))
            children.append(oid)
            return
        if ln.elbow is not None and template is not None:
            tpl = template(bend_template_key_of(ln.elbow.bend))
            # Like a straight line: from the transform origin along +size, flipped by negative scales.
            reqs.extend(_copied(tpl, oid, dx / tpl.w, dy / tpl.h, x1 * scale, y1 * scale))
        else:
            reqs.append({"createLine": {"objectId": oid, "lineCategory": "STRAIGHT", "elementProperties": {
                "pageObjectId": slide_id,
                "size": {"width": emu(abs(dx)), "height": emu(abs(dy))},
                # The line runs from the transform origin along +size, flipped by negative scales.
                "transform": {"scaleX": -1 if dx < 0 else 1, "scaleY": -1 if dy < 0 else 1, "unit": "EMU",
                              "translateX": round(x1 * scale * EMU_PER_PT), "translateY": round(y1 * scale * EMU_PER_PT)}}}})
        line_props: LineProperties = {"lineFill": {"solidFill": {"color": rgb_color(ln.stroke)}},
                                      "weight": pt(round(max(0.5, ln.width * scale), 2)),
                                      "startArrow": arrow_style(ln.arrow_from),
                                      "endArrow": arrow_style(ln.arrow_to)}
        if ln.dash is not None:  # (a solid line writes no dash: the requests of an old deck stay the same)
            line_props["dashStyle"] = ln.dash
        reqs.append({"updateLineProperties": {"objectId": oid, "fields": "lineFill.solidFill.color,weight,startArrow,endArrow"
                                              + (",dashStyle" if ln.dash is not None else ""),
                                              "lineProperties": line_props}})
        children.append(oid)

    # A line wholly inside a filled node is drawn on it (a legend's swatch on its key box): under
    # the nodes, as edges between nodes are, the fill hid it.
    on_nodes = {seg[0] for seg in segments if on_filled_node(seg[1], seg[2], el.nodes)}
    for oid, start_at, end_at, ln in segments:
        if oid not in on_nodes:
            draw(oid, start_at, end_at, ln)
    for j, node in enumerate(el.nodes):
        oid = node_oids[j]
        x0, y0, x1, y1 = (v * scale for v in node.bbox)
        look = node_look(node)
        text = "\n".join("".join(r.text for r in runs).strip() for runs in node.paragraphs)
        card = node.text  # a card's text: a text box on the PDF baselines (classify.card_text)
        inside = not card and bool(node.shape) and label_inside_of(look) and template is not None
        members: list[str] = []
        if node.shape is not None:  # free labels (edge labels, captions) have no shape, only the text box below
            fill, stroke = node.fill, node.stroke
            outline: Outline = ({"outlineFill": {"solidFill": {"color": rgb_color(stroke)}},
                                 "weight": pt(round(max(0.5, (node.width or 0.4) * scale), 2))}
                                if stroke else {"propertyState": "NOT_RENDERED"})
            props: ShapeProperties = {
                "contentAlignment": "MIDDLE", "autofit": {"autofitType": "NONE"},
                "shapeBackgroundFill": ({"solidFill": {"color": rgb_color(fill)}} if fill
                                        else {"propertyState": "NOT_RENDERED"}),
                "outline": outline}
            fields = ["contentAlignment", "autofit.autofitType",
                      "shapeBackgroundFill.solidFill.color" if fill else "shapeBackgroundFill.propertyState"]
            fields += ["outline.outlineFill.solidFill.color", "outline.weight"] if stroke else ["outline.propertyState"]
            if stroke and node.dash is not None:  # a dashed frame (fit=...); a solid one writes none
                outline["dashStyle"] = node.dash
                fields.append("outline.dashStyle")
            if template is not None and node_templated_of(look):
                reqs += copied_on(template(node_template_key_of(look)), oid, (x0, y0, x1, y1), node.rotation)
            else:
                reqs.append({"createShape": {"objectId": oid, "shapeType": node.shape, "elementProperties": {
                    "pageObjectId": slide_id, "size": {"width": emu(x1 - x0), "height": emu(y1 - y0)},
                    "transform": box_transform((x0, y0, x1, y1), node.rotation)}}})
            reqs.append({"updateShapeProperties": {"objectId": oid, "shapeProperties": props, "fields": ",".join(fields)}})
            members.append(oid)
        if card:
            for k, box in enumerate(card):
                label = f"{object_id}_x{j}" + (f"_{k}" if k else "")
                reqs += text_box_requests_of(set_card(box), slide_id, label, scale, fonts, None, None, None, None, None)
                members.append(label)
        elif text:
            target = oid
            if not inside:
                # A label wider than the node's text rectangle would wrap inside the shape: it
                # gets its own wider text box, centred on the node and grouped with it.
                target = f"{object_id}_x{j}"
                cx, w = (x0 + x1) / 2, (x1 - x0) + 2 * PAD_X + 40
                if page is not None and node.rotation is None:
                    w = label_width_on(w, cx, node.paragraphs, scale, fonts, page)
                made: list[SlidesRequest] = [
                    {"createShape": {"objectId": target, "shapeType": "TEXT_BOX", "elementProperties": {
                        "pageObjectId": slide_id, "size": {"width": emu(w), "height": emu(y1 - y0)},
                        # (a turned node's or a sloped label's box turns with it)
                        "transform": box_transform((cx - w / 2, y0, cx + w / 2, y1), node.rotation)}}},
                    {"updateShapeProperties": {"objectId": target, "fields": "contentAlignment,autofit.autofitType",
                                               "shapeProperties": {"contentAlignment": "MIDDLE",
                                                                   "autofit": {"autofitType": "NONE"}}}},
                ]
                reqs += made
                members.append(target)
            reqs.append({"insertText": {"objectId": target, "text": text}})
            start = 0
            for runs in (in_sentence_of(set_runs(line)) for line in node.paragraphs):
                line_text = "".join(r.text for r in runs).strip()
                offset = 0
                for run, z in zip(runs, run_sizes_of(runs, scale, fonts)):
                    piece = run.text.strip() if len(runs) == 1 else run.text
                    if offset == 0:
                        piece = piece.lstrip()
                    if not piece:
                        continue
                    font, sfields = fonts.style_of(run, scale)
                    style = slides_text_style(font, "a diagram label's font (FontMapper.style_of)")
                    if "fontSize" in style:
                        style["fontSize"] = pt(z)  # (a subscript no larger than its text: run_sizes)
                    style["smallCaps"] = run.smallcaps
                    style["foregroundColor"] = text_color(run.color)
                    style["baselineOffset"] = baseline_offset(run.script)
                    reqs.append({"updateTextStyle": {  # (UTF-16 units: u16)
                        "objectId": target, "style": style,
                        "fields": ",".join(sfields + ["smallCaps", "foregroundColor", "baselineOffset"]),
                        "textRange": {"type": "FIXED_RANGE", "startIndex": start + offset,
                                      "endIndex": min(start + u16(line_text), start + offset + u16(piece))}}})
                    for c0, c1, face in letter_faces(piece, run.font, start + offset):
                        reqs.append({"updateTextStyle": {
                            "objectId": target, "style": letter_face_style(face, run.bold), "fields": "weightedFontFamily",
                            "textRange": {"type": "FIXED_RANGE", "startIndex": c0, "endIndex": c1}}})
                    offset += u16(piece)
                start += u16(line_text) + 1
            reqs.append({"updateParagraphStyle": {
                "objectId": target, "textRange": {"type": "ALL"}, "fields": "alignment,lineSpacing,spaceAbove,spaceBelow",
                "style": {"alignment": "CENTER", "lineSpacing": 100, "spaceAbove": pt(0),
                          "spaceBelow": pt(0)}}})
        if len(members) >= 2:  # a node with its label (or card texts) outside: they move together
            reqs.append({"groupObjects": {"groupObjectId": f"{object_id}_g{j}", "childrenObjectIds": list(members)}})
            members = [f"{object_id}_g{j}"]
        children += members
    for oid, start_at, end_at, ln in segments:
        if oid in on_nodes:
            draw(oid, start_at, end_at, ln)
    # Edges follow the nodes they start or end on.
    sites = [(n.bbox, n.shape, n.adjust, n.rotation) for n in el.nodes]
    for oid, start_at, end_at, ln in segments:
        if ln.sweep is not None:
            continue  # (an arc is a shape: only lines connect)
        ends: LineProperties = {}
        first = connection_of(start_at, sites, node_oids) if start_at == ln.from_ else None
        last = connection_of(end_at, sites, node_oids) if end_at == ln.to else None
        if first:
            ends["startConnection"] = first
        if last:
            ends["endConnection"] = last
        if ends:
            reqs.append({"updateLineProperties": {"objectId": oid, "fields": ",".join(ends), "lineProperties": ends}})
    if len(children) >= 2:
        reqs.append({"groupObjects": {"groupObjectId": object_id, "childrenObjectIds": list(children)}})
    return reqs


def _block(el: JsonMap) -> int | None:
    """A panel's block (classify.blocks), None for the rest."""
    value = el.get("block")
    if value is None or el["kind"] != "shape":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"block: {value!r} is not an int")
    return value


def block_groups(elements: Sequence[JsonMap], object_ids: Sequence[str], title_oid: str | None,
                 apart: AbstractSet[str]) -> list[list[str]]:
    """Object ids per block, in element order: its panel shapes (title bar and body, see
    classify.blocks), plus the text and pictures lying on them, and the shapes the PDF draws on
    them (a listing's framed panel in the block's body; not those in `apart`, the rules
    `rule_groups` holds). Element order is z-order, and children of a group cannot be restacked:
    a shape on the block left out of its group lay either under the block, or over the words on it
    (a white listing panel over its code)."""
    blocks: dict[int, tuple[Box, list[str], int]] = {}  # block -> its extent, its oids, its first element
    for i, (el, oid) in enumerate(zip(elements, object_ids)):
        block = _block(el)
        if block is not None:
            x0, y0, x1, y1 = box_of(el["bbox"], "bbox")
            (bx0, by0, bx1, by1), members, first = blocks.get(block, ((x0, y0, x1, y1), [], i))
            blocks[block] = (min(bx0, x0), min(by0, y0), max(bx1, x1), max(by1, y1)), members, first
            members.append(oid)
    out: list[list[str]] = []
    for (x0, y0, x1, y1), panels, first in blocks.values():
        if len(panels) < 2:
            continue  # a lone panel is not recognisably a block
        members: list[str] = []
        for i, (el, oid) in enumerate(zip(elements, object_ids)):
            if oid in panels:
                members.append(oid)
                continue
            # Tables can't be grouped in Slides: a table in a block stays on its own. (A shape
            # drawn before the block's first panel lies under it: a shadow, a panel it stands on.)
            on = el["kind"] in ("text", "image") or (el["kind"] == "shape" and i > first and _block(el) is None
                                                     and oid not in apart)
            if on and oid != title_oid and not el.get("anchor"):
                ex0, ey0, ex1, ey1 = box_of(el["bbox"], "bbox")
                cx, cy = (ex0 + ex1) / 2, (ey0 + ey1) / 2
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    members.append(oid)
        out.append(members)
    return out


def block_stacking(elements: Sequence[JsonMap], object_ids: Sequence[str],
                   blocks: Sequence[tuple[str, Sequence[str]]], tops: Mapping[str, str]) -> list[str]:
    """The top-level objects to send to the back, last first, so that each block's group stands
    where its first panel was created: Slides puts a group where its topmost child stood, above
    whatever was created between its panels and the words on them (a table on the block, which
    cannot join the group, hidden under its body). `blocks`: (group id, member object ids); `tops`:
    an object id -> the group it went into (a text's with its pictures, a rule group), the blocks'
    groups not yet among them.

    Each block's group goes to the back, and under it every shape created before the block's first
    panel (they come first: `merge_blocks` puts shapes before the rest, which keeps their own
    place), in element order, so a shape the PDF draws under a block (a panel it stands on) stays
    under it. With one block and no shape before it this is the one SEND_TO_BACK of its group."""
    if not blocks:
        return []
    top = dict(tops)
    for group, members in blocks:
        top.update((m, group) for m in members)
    at: dict[str, list[int]] = {}  # top-level object -> the indices of the elements it holds
    for i, oid in enumerate(object_ids):
        at.setdefault(_top_of(oid, top), []).append(i)
    groups = {group for group, _ in blocks}
    # (a block's group stands at its first element, any other at its topmost, as Slides puts it)
    keys = {t: min(held) if t in groups else max(held) for t, held in at.items()}
    shapes = {_top_of(oid, top) for oid, el in zip(object_ids, elements) if el["kind"] == "shape"}
    last = max(keys[group] for group in groups)
    return sorted((t for t in shapes | groups if keys[t] <= last), key=lambda t: keys[t], reverse=True)


def _top_of(oid: str, top: Mapping[str, str]) -> str:
    """The top-level object holding `oid` (`top`: object -> the group it is in)."""
    while oid in top:
        oid = top[oid]
    return oid


def rule_groups(elements: Sequence[JsonMap], object_ids: Sequence[str]) -> list[list[str]]:
    """Rules lying on one another (a progress bar on its track) move as one."""
    rules = [(box_of(el["bbox"], "bbox"), oid) for el, oid in zip(elements, object_ids)
             if el["kind"] == "shape" and el.get("role") == "rule"]
    out: list[tuple[Box, list[str]]] = []  # (extent, oids)
    for (x0, y0, x1, y1), oid in rules:
        for i, ((gx0, gy0, gx1, gy1), members) in enumerate(out):
            if x0 < gx1 and gx0 < x1 and y0 < gy1 and gy0 < y1:
                out[i] = (min(x0, gx0), min(y0, gy0), max(x1, gx1), max(y1, gy1)), members
                members.append(oid)
                break
        else:
            out.append(((x0, y0, x1, y1), [oid]))
    return [members for _, members in out if len(members) >= 2]
