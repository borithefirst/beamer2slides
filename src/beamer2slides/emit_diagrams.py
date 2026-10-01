"""Diagrams: nodes, connectors and labels as shapes, and the groups a block's or a frame's pieces make.

A diagram is planned from its parsed record (`DiagramElement`: `diagram_requests_of`), its nodes'
templates and labels from a `NodeLook` (`node_look`). The names without `_of` take the dicts sync
and the tests hold: `diagram_requests`, `element_template_keys`, `label_inside`,
`node_template_key`, `bend_template_key`, `connection`.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Literal

from . import curves, ir_types
from .emit_metrics import PAD_X, FontMapper, u16
from .emit_model import (
    JsonMap, NodeLook, NodeSite, Template, TemplateKey, box_of, node_look, node_look_of, node_site_of, set_card,
    set_runs, template_of,
)
from .emit_pptx import arc_kind, template_key
from .emit_text import in_sentence_of, text_box_requests_of
from .google_types import LineConnection, LineProperties, Outline, ShapeProperties, SlidesRequest, slides_text_style
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
        return [(0.5, min(0.5, a) * ss / h), (0, 0.5), (0.5, 1), (1, 0.5)]
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
    """The template shapes an element dict is copied from (a panel's, a diagram's nodes' and
    elbows'), as DeckPlan and sync read every element of a deck."""
    if el["kind"] == "diagram":
        nodes = [node_look_of(_object(n)) for n in _items(el["nodes"])]
        bends = [_bend(bend) for ln in _items(el["lines"]) if (bend := _object(ln).get("bend"))]
        return [node_template_key_of(n) for n in nodes if node_templated_of(n)] + [bend_template_key_of(b) for b in bends] \
            + [k for ln in _items(el["lines"]) if (k := arc_template_key(_object(ln))) is not None]
    key = template_key(el, scale)
    return [key] if key else []


def diagram_template_keys(el: DiagramElement) -> list[TemplateKey]:
    """`element_template_keys` of a parsed diagram."""
    nodes = [node_look(n) for n in el.nodes]
    return [node_template_key_of(n) for n in nodes if node_templated_of(n)] + \
        [bend_template_key_of(ln.elbow.bend) for ln in el.lines if ln.elbow is not None] + \
        [k for ln in el.lines if (k := arc_template_key_of(ln)) is not None]


def arc_template_key(line: JsonMap) -> TemplateKey | None:
    """`arc_template_key_of` a line dict (sync's base diagrams); None for a line that is no arc."""
    sweep = line.get("sweep")
    if sweep is None:
        return None
    if isinstance(sweep, bool) or not isinstance(sweep, (int, float)):
        raise TypeError(f"sweep: {sweep!r} is not a number")
    return _arc_key(sweep, _arrow(line.get("arrow_from")), _arrow(line.get("arrow_to")))


def arc_template_key_of(ln: DiagramLine) -> TemplateKey | None:
    """An arc's template (emit_pptx's `arc` preset): its heads and its sweep as drawn
    (`curves.drawn_sweep`). A copy is turned and mirrored to its place (`arc_requests`), so
    neither where the arc starts nor which way it turns makes another template."""
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
    node's box, shape and adjustment."""
    best: tuple[float, LineConnection] | None = None
    for (bbox, shape, adjust), oid in zip(nodes, oids):
        x0, y0, x1, y1 = bbox
        for index, (fx, fy) in enumerate(connection_sites(shape, bbox, adjust)):
            d = math.hypot(point[0] - (x0 + fx * (x1 - x0)), point[1] - (y0 + fy * (y1 - y0)))
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
                               None if tpl is None else (lambda key: template_of(tpl(key))))


def _transform(oid: str, sx: float, sy: float, x: float, y: float) -> SlidesRequest:
    """An ABSOLUTE transform of a copied template: from its origin along +size (PDF pt x scale)."""
    return {"updatePageElementTransform": {"objectId": oid, "applyMode": "ABSOLUTE", "transform": {
        "scaleX": sx, "scaleY": sy, "unit": "EMU", "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}}


def _copied(template: Template, oid: str, sx: float, sy: float, x: float, y: float) -> list[SlidesRequest]:
    return [{"duplicateObject": {"objectId": template.id, "objectIds": {template.id: oid}}},
            _transform(oid, sx, sy, x, y),
            {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}}]


CHORD_STEP = 15.0  # degrees of an arc one straight piece stands for, where no template is at hand


def arc_requests(tpl: Template, oid: str, ln: DiagramLine, sweep: float, scale: float) -> list[SlidesRequest]:
    """An arc copied from its template (the preset `arc` from 0° clockwise through the drawn
    sweep, in a square `tpl.w` x `tpl.h`) and put on its circle (`curves.arc_circle`) by one
    ABSOLUTE transform: scaled to the circle, mirrored when it turns anticlockwise, turned to
    start where it starts. Its ends land on `from` and `to` exactly; its outline takes the
    line's colour and weight (the template's heads stay)."""
    drawn = curves.drawn_sweep(sweep)
    circle = curves.arc_circle(ln.from_, ln.to, drawn)
    turn = 1.0 if drawn > 0 else -1.0
    angle = math.radians(circle.start)
    cos, sin = math.cos(angle), math.sin(angle)
    sx, sy = 2 * circle.radius * scale / tpl.w, 2 * circle.radius * scale / tpl.h
    # rotation(start) . mirror(turn) . scale: the template's point at angle a goes to start + turn * a
    a, b, c, d = cos * sx, -sin * turn * sy, sin * sx, cos * turn * sy
    tx = circle.centre[0] * scale - (a * tpl.w + b * tpl.h) / 2
    ty = circle.centre[1] * scale - (c * tpl.w + d * tpl.h) / 2
    outline: Outline = {"outlineFill": {"solidFill": {"color": rgb_color(ln.stroke)}},
                        "weight": pt(round(max(0.5, ln.width * scale), 2))}
    if ln.dash is not None:  # (a solid arc writes no dash, as a solid line)
        outline["dashStyle"] = ln.dash
    return [{"duplicateObject": {"objectId": tpl.id, "objectIds": {tpl.id: oid}}},
            {"updatePageElementTransform": {"objectId": oid, "applyMode": "ABSOLUTE", "transform": {
                "scaleX": round(a, 6), "shearX": round(b, 6), "shearY": round(c, 6), "scaleY": round(d, 6),
                "unit": "EMU", "translateX": round(tx * EMU_PER_PT), "translateY": round(ty * EMU_PER_PT)}}},
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
    n = max(2, math.ceil(abs(sweep) / CHORD_STEP))
    circle = curves.arc_circle(ln.from_, ln.to, sweep)
    points = [ln.from_, *(curves.arc_point(circle, k * sweep / n) for k in range(1, n)), ln.to]
    return [(oid if k == 0 else f"{oid}c{k}", points[k], points[k + 1],
             replace(ln, sweep=None, arrow_from=ln.arrow_from if k == 0 else None,
                     arrow_to=ln.arrow_to if k == n - 1 else None))
            for k in range(n)]


def diagram_requests_of(el: DiagramElement, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                        template: Callable[[TemplateKey], Template] | None) -> list[SlidesRequest]:
    """Nodes become shapes, edges become lines with arrow heads; the parts are grouped so the
    diagram moves as one piece but stays editable. A label that fits goes inside its node (a
    template shape without text padding, see label_inside); one that doesn't gets a text box
    grouped with its node. Line ends on a node's connection site are connected to it, so edges
    follow nodes moved in Slides. `template(key)` gives this slide's template shape for a key."""
    reqs: list[SlidesRequest] = []
    children: list[str] = []
    node_oids = [f"{object_id}_n{j}" for j in range(len(el.nodes))]
    # (object id, from, to, line): elbows without a template fall back to two lines
    segments: list[tuple[str, Point, Point, DiagramLine]] = []
    for j, ln in enumerate(el.lines):
        if ln.elbow is not None and template is None:
            segments += [(f"{object_id}_l{j}", ln.from_, ln.elbow.via, replace(ln, arrow_to=None)),
                         (f"{object_id}_l{j}b", ln.elbow.via, ln.to, replace(ln, arrow_from=None))]
        elif ln.sweep is not None and template is None:
            segments += arc_chords(f"{object_id}_l{j}", ln, ln.sweep)
        else:
            segments.append((f"{object_id}_l{j}", ln.from_, ln.to, ln))
    for oid, (x1, y1), (x2, y2), ln in segments:
        dx, dy = (x2 - x1) * scale, (y2 - y1) * scale
        if ln.sweep is not None and template is not None:
            # A curve's arc: a turned copy of its template, which brought its heads along.
            reqs += arc_requests(template(_arc_key(ln.sweep, ln.arrow_from, ln.arrow_to)), oid, ln, ln.sweep, scale)
            children.append(oid)
            continue
        if ln.elbow is not None and template is not None:
            tpl = template(bend_template_key_of(ln.elbow.bend))
            # Like a straight line: from the transform origin along +size, flipped by negative scales.
            reqs += _copied(tpl, oid, dx / tpl.w, dy / tpl.h, x1 * scale, y1 * scale)
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
                tpl = template(node_template_key_of(look))
                reqs += _copied(tpl, oid, (x1 - x0) / tpl.w, (y1 - y0) / tpl.h, x0, y0)
            else:
                reqs.append({"createShape": {"objectId": oid, "shapeType": node.shape, "elementProperties": {
                    "pageObjectId": slide_id, "size": {"width": emu(x1 - x0), "height": emu(y1 - y0)},
                    "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                                  "translateX": round(x0 * EMU_PER_PT), "translateY": round(y0 * EMU_PER_PT)}}}})
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
                made: list[SlidesRequest] = [
                    {"createShape": {"objectId": target, "shapeType": "TEXT_BOX", "elementProperties": {
                        "pageObjectId": slide_id, "size": {"width": emu(w), "height": emu(y1 - y0)},
                        "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                                      "translateX": round((cx - w / 2) * EMU_PER_PT), "translateY": round(y0 * EMU_PER_PT)}}}},
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
                for run in runs:
                    piece = run.text.strip() if len(runs) == 1 else run.text
                    if offset == 0:
                        piece = piece.lstrip()
                    if not piece:
                        continue
                    font, sfields = fonts.style_of(run, scale)
                    style = slides_text_style(font, "a diagram label's font (FontMapper.style_of)")
                    style["foregroundColor"] = text_color(run.color)
                    reqs.append({"updateTextStyle": {  # (UTF-16 units: u16)
                        "objectId": target, "style": style, "fields": ",".join(sfields + ["foregroundColor"]),
                        "textRange": {"type": "FIXED_RANGE", "startIndex": start + offset,
                                      "endIndex": min(start + u16(line_text), start + offset + u16(piece))}}})
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
    # Edges follow the nodes they start or end on.
    sites = [(n.bbox, n.shape, n.adjust) for n in el.nodes]
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


def block_groups(elements: Sequence[JsonMap], object_ids: Sequence[str], title_oid: str | None) -> list[list[str]]:
    """Object ids per block: its panel shapes (title bar and body, see classify.blocks), plus
    the text and pictures lying on them."""
    blocks: dict[int, tuple[Box, list[str]]] = {}  # block -> its extent, its oids
    for el, oid in zip(elements, object_ids):
        block = _block(el)
        if block is not None:
            x0, y0, x1, y1 = box_of(el["bbox"], "bbox")
            (bx0, by0, bx1, by1), members = blocks.get(block, ((x0, y0, x1, y1), []))
            blocks[block] = (min(bx0, x0), min(by0, y0), max(bx1, x1), max(by1, y1)), members
            members.append(oid)
    out: list[list[str]] = []
    for (x0, y0, x1, y1), members in blocks.values():
        if len(members) < 2:
            continue  # a lone panel is not recognisably a block
        for el, oid in zip(elements, object_ids):
            # Tables can't be grouped in Slides: a table in a block stays on its own.
            if el["kind"] in ("text", "image") and oid != title_oid and not el.get("anchor"):
                ex0, ey0, ex1, ey1 = box_of(el["bbox"], "bbox")
                cx, cy = (ex0 + ex1) / 2, (ey0 + ey1) / 2
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    members.append(oid)
        out.append(members)
    return out


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
