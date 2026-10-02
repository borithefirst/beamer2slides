"""Turned nodes and sloped labels (ir.Node `rotation`) on synthetic pages: a rectangle and an
ellipse turned (TikZ's `rotate=`), an edge label `sloped` along its line - classify's box before
the turn, the transform emit writes, and a line connected to a turned node's site."""

import math
from collections.abc import Sequence

from beamer2slides import ir_types
from beamer2slides.emit_diagrams import connection, diagram_requests
from beamer2slides.emit_metrics import FontMapper
from beamer2slides.emit_model import JsonMap, TemplateKey
from beamer2slides.google_types import slides_json
from beamer2slides.gslides import EMU_PER_PT
from beamer2slides.ir import DiagramElement, Node, element_json
from beamer2slides.json_types import JsonObject
from beamer2slides.pdf.api import char_box
from beamer2slides.raw_types import PathItem, RawSpan

from .json_reads import jnum, jobj, jobjs, jstr
from .test_charts_diagrams import SANS, Page, body_text, lines, rect
from .test_diagrams_overlays import diagram_of, elements

Point = tuple[float, float]
SIZE = 10.91
ASCENT, DESCENT = 0.78, -0.22  # CM sans' font box, em
ADVANCE = 0.5                   # em per letter of the synthetic labels


def turned(p: Point, centre: Point, rotation: float) -> Point:
    """`p` turned `rotation` degrees clockwise on the page (y down) about `centre`."""
    c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    dx, dy = p[0] - centre[0], p[1] - centre[1]
    return centre[0] + c * dx - s * dy, centre[1] + s * dx + c * dy


def sloped(page: Page, text: str, origin: Point, rotation: float) -> RawSpan:
    """A span set `rotation` degrees clockwise from `origin`, boxed as extract boxes one: the upright
    box of its turned font box, along PDFium's loose advance (the upright box of the turned glyph
    box, read along the direction)."""
    c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    advance = len(text) * ADVANCE * SIZE
    loose = advance + 2 * (ASCENT - DESCENT) * SIZE * abs(c * s)
    x0, y0, x1, y1 = char_box(origin[0], origin[1], c, s, loose, SIZE, ASCENT, DESCENT)
    span: RawSpan = {"id": f"p{page.index}s{len(page.spans)}", "text": text, "font": SANS, "size": SIZE,
                     "color": "#000000", "alpha": 255, "origin": [origin[0], origin[1]], "bbox": [x0, y0, x1, y1],
                     "dir": [c, s], "smallcaps": False}
    page.spans.append(span)
    return span


def label_at(page: Page, text: str, centre: Point, rotation: float) -> None:
    """A sloped label centred on `centre`."""
    half = len(text) * ADVANCE * SIZE / 2
    origin = turned((centre[0] - half, centre[1] + (ASCENT + DESCENT) / 2 * SIZE), centre, rotation)
    sloped(page, text, origin, rotation)


def turned_rectangle(centre: Point, w: float, h: float, rotation: float) -> list[PathItem]:
    corners = [turned((centre[0] + x, centre[1] + y), centre, rotation)
               for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]
    return lines(*corners, closed=True)


def turned_ellipse(centre: Point, rx: float, ry: float, rotation: float) -> list[PathItem]:
    """TikZ's ellipse: four quarters from the ends of its axes, turned."""
    k = 0.5523
    out: list[PathItem] = []
    for q in range(4):
        a0, a1 = math.radians(90 * q), math.radians(90 * (q + 1))
        p0 = (rx * math.cos(a0), ry * math.sin(a0))
        p3 = (rx * math.cos(a1), ry * math.sin(a1))
        p1 = (p0[0] - k * rx * math.sin(a0), p0[1] + k * ry * math.cos(a0))
        p2 = (p3[0] + k * rx * math.sin(a1), p3[1] - k * ry * math.cos(a1))
        out.append(("c", [list(turned((centre[0] + x, centre[1] + y), centre, rotation)) for x, y in (p0, p1, p2, p3)]))
    return out


def nodes_of(d: DiagramElement) -> list[Node]:
    return list(d["nodes"])


def texts(n: Node) -> str:
    return "".join(r["text"] for par in n["paragraphs"] for r in par).strip()


def node_of(d: DiagramElement, text: str) -> Node:
    return next(n for n in nodes_of(d) if texts(n) == text)


def centre_of(n: Node) -> Point:
    x0, y0, x1, y1 = n["bbox"]
    return (x0 + x1) / 2, (y0 + y1) / 2


def rotation_of(n: Node) -> float:
    rotation = n.get("rotation")
    assert rotation is not None, f"{texts(n)!r} is not turned"
    return rotation


# ---------------------------------------------------------------- classify

def tilted_page() -> Page:
    """An upright box, a rectangle turned 20 degrees clockwise with its word turned with it, and
    the edge between them with a sloped label above it."""
    page = Page()
    body_text(page)
    page.draw(rect(60, 100, 120, 130), type="fs", fill="#dddddd", stroke="#000000", width=0.4)
    page.text("Plain", 75, 118)
    page.draw(turned_rectangle((260, 115), 60, 24, 20), type="fs", fill="#ffd0d0", stroke="#000000", width=0.4)
    label_at(page, "Tilted", (260, 115), 20)
    site = turned((230, 115), (260, 115), 20)  # the turned box's left side, midway
    page.draw(lines((120, 115), site), type="s", stroke="#000000", width=0.4)
    angle = math.degrees(math.atan2(site[1] - 115, site[0] - 120))
    label_at(page, "edge", turned(((120 + site[0]) / 2, (115 + site[1]) / 2 - 8), ((120 + site[0]) / 2, (115 + site[1]) / 2), angle), angle)
    return page


def test_a_turned_rectangle_is_a_node_turned_about_its_true_box() -> None:
    d = diagram_of(tilted_page())
    node = node_of(d, "Tilted")
    assert node["shape"] == "RECTANGLE"
    assert abs(rotation_of(node) - 20) < 0.1, "degrees clockwise on the page"
    x0, y0, x1, y1 = node["bbox"]
    assert abs(x1 - x0 - 60) < 0.2 and abs(y1 - y0 - 24) < 0.2, "the box before the turn, not its upright hull"
    cx, cy = centre_of(node)
    assert abs(cx - 260) < 0.2 and abs(cy - 115) < 0.2
    # its word read upright in its frame: on a baseline inside the box, as wide as its advance
    assert y0 < node["baselines"][0] < y1
    assert abs(node["label_w"] - 6 * ADVANCE * SIZE) < 0.5
    assert "rotation" not in node_of(d, "Plain"), "an upright node writes no rotation"


def test_a_sloped_label_is_a_free_label_turned_along_its_line() -> None:
    d = diagram_of(tilted_page())
    label = node_of(d, "edge")
    assert label["shape"] is None
    site = turned((230, 115), (260, 115), 20)
    angle = math.degrees(math.atan2(site[1] - 115, site[0] - 120))
    assert abs(rotation_of(label) - angle) < 0.1
    x0, y0, x1, y1 = label["bbox"]
    assert abs(x1 - x0 - 4 * ADVANCE * SIZE) < 0.5, "its true advance, not PDFium's loose one"
    middle = ((120 + site[0]) / 2, (115 + site[1]) / 2)
    want = turned((middle[0], middle[1] - 8), middle, angle)
    # (the font box's centre: half the ascent less the descent above the baseline the label sits on)
    cx, cy = centre_of(label)
    assert math.dist((cx, cy), want) < 0.5


def test_a_turned_ellipse_keeps_its_axes() -> None:
    page = Page()
    body_text(page)
    page.draw(turned_ellipse((150, 110), 40, 15, -30), type="fs", fill="#d0e0ff", stroke="#000000", width=0.4)
    label_at(page, "Orbit", (150, 110), -30)
    page.draw(rect(260, 95, 320, 125), type="fs", fill="#dddddd", stroke="#000000", width=0.4)
    page.text("Box", 280, 113)
    page.draw(lines((190, 110), (260, 110)), type="s", stroke="#000000", width=0.4)
    d = diagram_of(page)
    node = node_of(d, "Orbit")
    assert node["shape"] == "ELLIPSE" and abs(rotation_of(node) + 30) < 0.1
    x0, y0, x1, y1 = node["bbox"]
    assert abs(x1 - x0 - 80) < 0.3 and abs(y1 - y0 - 30) < 0.3
    assert math.dist(centre_of(node), (150, 110)) < 0.2


def turned_rounded_rectangle(centre: Point, w: float, h: float, radius: float, rotation: float) -> list[PathItem]:
    """TikZ's `rounded corners` box (`lclclclc`), turned."""
    k = 0.5523 * radius
    x, y = w / 2, h / 2
    pieces: list[tuple[str, list[Point]]] = [
        ("l", [(-x + radius, -y), (x - radius, -y)]), ("c", [(x - radius, -y), (x - radius + k, -y), (x, -y + radius - k), (x, -y + radius)]),
        ("l", [(x, -y + radius), (x, y - radius)]), ("c", [(x, y - radius), (x, y - radius + k), (x - radius + k, y), (x - radius, y)]),
        ("l", [(x - radius, y), (-x + radius, y)]), ("c", [(-x + radius, y), (-x + radius - k, y), (-x, y - radius + k), (-x, y - radius)]),
        ("l", [(-x, y - radius), (-x, -y + radius)]), ("c", [(-x, -y + radius), (-x, -y + radius - k), (-x + radius - k, -y), (-x + radius, -y)])]
    return [(op, [list(turned((centre[0] + px, centre[1] + py), centre, rotation)) for px, py in pts]) for op, pts in pieces]


def test_a_turned_rounded_rectangle_keeps_its_corners() -> None:
    page = Page()
    body_text(page)
    page.draw(turned_rounded_rectangle((200, 110), 70, 28, 5, -15), type="fs", fill="#ffd0d0", stroke="#000000", width=0.4)
    label_at(page, "Round", (200, 110), -15)
    page.draw(rect(60, 95, 120, 125), type="fs", fill="#dddddd", stroke="#000000", width=0.4)
    page.text("Box", 80, 113)
    page.draw(lines((120, 110), (160, 110)), type="s", stroke="#000000", width=0.4)
    node = node_of(diagram_of(page), "Round")
    assert node["shape"] == "ROUND_RECTANGLE" and abs(rotation_of(node) + 15) < 0.1
    x0, y0, x1, y1 = node["bbox"]
    assert abs(x1 - x0 - 70) < 0.3 and abs(y1 - y0 - 28) < 0.3
    assert abs(node.get("radius", 0) - 5) < 0.1


def test_a_level_word_in_a_turned_shape_stays_a_picture() -> None:
    # (`shape border rotate`: the outline turned, its words not - Slides turns a shape's text with it)
    page = Page()
    body_text(page)
    page.draw(turned_rectangle((200, 110), 70, 28, 20), type="fs", fill="#ffd0d0", stroke="#000000", width=0.4)
    page.text("Level", 186, 113)
    page.draw(rect(60, 95, 120, 125), type="fs", fill="#dddddd", stroke="#000000", width=0.4)
    page.text("Box", 80, 113)
    page.draw(lines((120, 110), (166, 110)), type="s", stroke="#000000", width=0.4)
    assert not any(e["kind"] == "diagram" for e in elements(page))


# ---------------------------------------------------------------- the IR

def test_an_old_node_without_rotation_reads_upright_and_writes_none() -> None:
    old: JsonObject = {"bbox": [0, 0, 40, 20], "shape": "RECTANGLE", "fill": None, "stroke": "#000000", "width": 0.4,
                       "paragraphs": [], "baselines": [], "label_w": 0, "text": None}
    node = ir_types.node(old, ir_types.At(where="test", path="node"))
    assert node.rotation is None
    assert "rotation" not in ir_types.node_json(node)
    turned_node: JsonObject = {**old, "rotation": -30.0}
    assert ir_types.node_json(ir_types.node(turned_node, ir_types.At(where="test", path="node")))["rotation"] == -30.0


# ---------------------------------------------------------------- emit

def placed(transform: JsonObject, local: Point) -> Point:
    """Where an object's own point `local` (pt) goes on the page (pt)."""
    x, y = local
    return ((jnum(transform, "scaleX") * x + jnum(transform, "shearX") * y) + jnum(transform, "translateX") / EMU_PER_PT,
            (jnum(transform, "shearY") * x + jnum(transform, "scaleY") * y) + jnum(transform, "translateY") / EMU_PER_PT)


def requests(d: DiagramElement, templated: bool) -> list[JsonObject]:
    def template(key: TemplateKey) -> JsonMap:
        return {"id": "tpl", "w": 236.22, "h": 236.22}
    return [slides_json(r) for r in diagram_requests(element_json(d), "s", "d", 1.0, FontMapper(),
                                                     template if templated else None)]


def transform_of(reqs: Sequence[JsonObject], oid: str) -> tuple[JsonObject, Point]:
    """The transform an object was given, and its own size (pt)."""
    for r in reqs:
        create = r.get("createShape")
        if isinstance(create, dict) and create.get("objectId") == oid:
            props = jobj(create, "elementProperties")
            size = jobj(props, "size")
            return jobj(props, "transform"), (jnum(jobj(size, "width"), "magnitude") / EMU_PER_PT,
                                              jnum(jobj(size, "height"), "magnitude") / EMU_PER_PT)
        update = r.get("updatePageElementTransform")
        if isinstance(update, dict) and update.get("objectId") == oid:
            return jobj(update, "transform"), (236.22, 236.22)
    raise AssertionError(f"no transform for {oid}")


def test_emit_turns_a_node_about_its_centre_created_or_copied() -> None:
    d = diagram_of(tilted_page())
    j = nodes_of(d).index(node_of(d, "Tilted"))
    node = nodes_of(d)[j]
    for templated in (False, True):
        reqs = requests(d, templated)
        transform, (w, h) = transform_of(reqs, f"d_n{j}")
        angle = math.radians(rotation_of(node))
        sx, sy = jnum(transform, "scaleX"), jnum(transform, "shearY")
        assert abs(math.degrees(math.atan2(sy, sx)) - rotation_of(node)) < 0.01, "turned clockwise"
        x0, y0, x1, y1 = node["bbox"]
        assert math.dist(placed(transform, (w / 2, h / 2)), centre_of(node)) < 0.05, "about the box's centre"
        # its corners where the drawing's are: the unturned box's corner turned about the centre
        corner = turned((x0, y0), centre_of(node), rotation_of(node))
        assert math.dist(placed(transform, (0, 0)), corner) < 0.05
        right = placed(transform, (w, h / 2))
        assert abs(math.dist(right, centre_of(node)) - (x1 - x0) / 2) < 0.05
        assert abs(math.atan2(right[1] - centre_of(node)[1], right[0] - centre_of(node)[0]) - angle) < 1e-3


def test_emit_turns_a_sloped_label_box_and_leaves_upright_ones_alone() -> None:
    d = diagram_of(tilted_page())
    reqs = requests(d, False)
    j = nodes_of(d).index(node_of(d, "edge"))
    transform, (w, h) = transform_of(reqs, f"d_x{j}")
    assert math.dist(placed(transform, (w / 2, h / 2)), centre_of(nodes_of(d)[j])) < 0.05
    assert jnum(transform, "shearY") < 0, "rising to the right: turned anticlockwise"
    plain = nodes_of(d).index(node_of(d, "Plain"))
    upright, _ = transform_of(reqs, f"d_n{plain}")
    assert "shearX" not in upright and jnum(upright, "scaleX") == 1, "an upright node's transform as before"


def test_a_line_connects_to_a_turned_site_only_where_it_is_drawn() -> None:
    d = diagram_of(tilted_page())
    j = nodes_of(d).index(node_of(d, "Tilted"))
    as_maps: list[JsonMap] = list(jobjs(element_json(d), "nodes"))
    oids = [f"n{k}" for k in range(len(as_maps))]
    site = turned((230, 115), (260, 115), 20)
    assert connection([site[0], site[1]], as_maps, oids) == {"connectedObjectId": f"n{j}", "connectionSiteIndex": 1}
    assert connection([230, 115], as_maps, oids) is None, "the unturned site is not where the side is drawn"
    reqs = requests(d, True)
    ends = [jobj(r, "updateLineProperties") for r in reqs if "updateLineProperties" in r
            and "endConnection" in jobj(jobj(r, "updateLineProperties"), "lineProperties")]
    assert [jstr(jobj(jobj(e, "lineProperties"), "endConnection"), "connectedObjectId") for e in ends] == [f"d_n{j}"]
