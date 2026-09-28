"""Diagrams: nodes, connectors and labels as shapes, and the groups a block's or a frame's pieces make."""

import math

from .emit_metrics import PAD_X, FontMapper, rgb, u16
from .emit_pptx import template_key
from .emit_text import in_sentence, text_box_requests
from .gslides import EMU_PER_PT, emu, pt


# Share of a preset shape's width its text may use: Slides lays text out in the shape's .pptx
# text rectangle (an ellipse's is its inscribed square, a diamond's half its width).
TEXT_RECT_WIDTH = {"RECTANGLE": 1.0, "ROUND_RECTANGLE": 0.9, "ELLIPSE": 0.707, "DIAMOND": 0.5}
# Connection sites of preset shapes in their .pptx order (fractions of the box): a line connected
# to a site follows the shape when it is moved in Slides.
CONNECTION_SITES = {
    "RECTANGLE": [(0.5, 0), (0, 0.5), (0.5, 1), (1, 0.5)],
    "ROUND_RECTANGLE": [(0.5, 0), (0, 0.5), (0.5, 1), (1, 0.5)],
    "DIAMOND": [(0.5, 0), (0, 0.5), (0.5, 1), (1, 0.5)],
    "ELLIPSE": [(0.5, 0), (0.1464, 0.1464), (0, 0.5), (0.1464, 0.8536), (0.5, 1), (0.8536, 0.8536), (1, 0.5), (0.8536, 0.1464)],
    "TRIANGLE": [(0.5, 0), (0.25, 0.5), (0, 1), (0.5, 1), (1, 1), (0.75, 0.5)],
}
LABEL_ROOM = 1.08  # the substitute font may run this much wider


def label_inside(node: dict) -> bool:
    """A node's label goes into the node shape itself (it then moves and resizes with it) when
    it fits the shape's text rectangle without wrapping."""
    x0, _, x1, _ = node["bbox"]
    text = "".join(r["text"] for runs in node["paragraphs"] for r in runs).strip()
    return bool(text) and node["shape"] in TEXT_RECT_WIDTH and \
        node.get("label_w", 0.0) * LABEL_ROOM <= (x1 - x0) * TEXT_RECT_WIDTH[node["shape"]] - 0.5


def node_template_key(node: dict) -> tuple:
    """A node's template: its preset, and for rounded corners of a known radius the preset's
    adjustment (as `template_key` gives a panel's) - createShape only makes the default."""
    x0, y0, x1, y1 = node["bbox"]
    adj = None
    if node["shape"] == "ROUND_RECTANGLE" and node.get("radius"):
        adj = min(0.5, round(node["radius"] / max(min(x1 - x0, y1 - y0), 0.01), 2))
    return node["shape"], adj, None


def node_templated(node: dict) -> bool:
    """A node copied from a template shape: its label fits inside, or its corners have a radius."""
    return bool(node["shape"]) and (label_inside(node) or node_template_key(node)[1] is not None)


def bend_template_key(line: dict) -> tuple:
    """An elbow connector (bentConnector3) turning at its start (|-, adj 0) or its end (-|).
    classify writes only |- now (at adj 1 Slides drew the turn halfway); an old base's -|
    lines keep their key, so their live objects are never copied for a |- one."""
    return "BENT_CONNECTOR", 0.0 if line["bend"] == "vh" else 1.0, None


def element_template_keys(el: dict, scale: float) -> list[tuple]:
    if el["kind"] == "diagram":
        return [node_template_key(n) for n in el["nodes"] if node_templated(n)] + \
               [bend_template_key(ln) for ln in el["lines"] if ln.get("bend")]
    key = template_key(el, scale)
    return [key] if key else []


def connection(point: list[float], nodes: list[dict], oids: list[str]) -> dict | None:
    """The node connection site a line end sits on (PDF pt, within 1.5 pt), if any."""
    best = None
    for node, oid in zip(nodes, oids):
        if not node["shape"] or node["shape"] not in CONNECTION_SITES:
            continue
        x0, y0, x1, y1 = node["bbox"]
        for index, (fx, fy) in enumerate(CONNECTION_SITES[node["shape"]]):
            d = math.hypot(point[0] - (x0 + fx * (x1 - x0)), point[1] - (y0 + fy * (y1 - y0)))
            if d <= 1.5 and (best is None or d < best[0]):
                best = (d, {"connectedObjectId": oid, "connectionSiteIndex": index})
    return best[1] if best else None


def arrow_style(arrow) -> str:
    if not arrow:
        return "NONE"
    return arrow if isinstance(arrow, str) else "OPEN_ARROW"


def diagram_requests(el: dict, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                     template=None) -> list[dict]:
    """Nodes become shapes, edges become lines with arrow heads; the parts are grouped so the
    diagram moves as one piece but stays editable. A label that fits goes inside its node (a
    template shape without text padding, see label_inside); one that doesn't gets a text box
    grouped with its node. Line ends on a node's connection site are connected to it, so edges
    follow nodes moved in Slides. `template(key)` gives this slide's template shape for a key."""
    reqs: list[dict] = []
    children: list[str] = []
    node_oids = [f"{object_id}_n{j}" for j in range(len(el["nodes"]))]
    segments = []  # (object id, from, to, line): elbows without a template fall back to two lines
    for j, ln in enumerate(el["lines"]):
        if ln.get("bend") and template is None:
            segments += [(f"{object_id}_l{j}", ln["from"], ln["via"], {**ln, "arrow_to": None}),
                         (f"{object_id}_l{j}b", ln["via"], ln["to"], {**ln, "arrow_from": None})]
        else:
            segments.append((f"{object_id}_l{j}", ln["from"], ln["to"], ln))
    for oid, (x1, y1), (x2, y2), ln in segments:
        dx, dy = (x2 - x1) * scale, (y2 - y1) * scale
        if ln.get("bend") and template is not None:
            tpl = template(bend_template_key(ln))
            reqs += [
                {"duplicateObject": {"objectId": tpl["id"], "objectIds": {tpl["id"]: oid}}},
                # Like a straight line: from the transform origin along +size, flipped by negative scales.
                {"updatePageElementTransform": {"objectId": oid, "applyMode": "ABSOLUTE", "transform": {
                    "scaleX": dx / tpl["w"], "scaleY": dy / tpl["h"], "unit": "EMU",
                    "translateX": round(x1 * scale * EMU_PER_PT), "translateY": round(y1 * scale * EMU_PER_PT)}}},
                {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}},
            ]
        else:
            reqs.append({"createLine": {"objectId": oid, "lineCategory": "STRAIGHT", "elementProperties": {
                "pageObjectId": slide_id,
                "size": {"width": emu(abs(dx)), "height": emu(abs(dy))},
                # The line runs from the transform origin along +size, flipped by negative scales.
                "transform": {"scaleX": -1 if dx < 0 else 1, "scaleY": -1 if dy < 0 else 1, "unit": "EMU",
                              "translateX": round(x1 * scale * EMU_PER_PT), "translateY": round(y1 * scale * EMU_PER_PT)}}}})
        reqs.append({"updateLineProperties": {"objectId": oid, "fields": "lineFill.solidFill.color,weight,startArrow,endArrow",
                                              "lineProperties": {
                                                  "lineFill": {"solidFill": {"color": rgb(ln["stroke"])["opaqueColor"]}},
                                                  "weight": pt(round(max(0.5, ln["width"] * scale), 2)),
                                                  "startArrow": arrow_style(ln["arrow_from"]),
                                                  "endArrow": arrow_style(ln["arrow_to"])}}})
        children.append(oid)
    for j, node in enumerate(el["nodes"]):
        oid = node_oids[j]
        x0, y0, x1, y1 = (v * scale for v in node["bbox"])
        text = "\n".join("".join(r["text"] for r in runs).strip() for runs in node["paragraphs"])
        card = node.get("text")  # a card's text: a text box on the PDF baselines (classify.card_text)
        inside = not card and bool(node["shape"]) and label_inside(node) and template is not None
        copied = template is not None and node_templated(node)
        props = None if node["shape"] is None else {"contentAlignment": "MIDDLE", "autofit": {"autofitType": "NONE"},
                 "shapeBackgroundFill": ({"solidFill": {"color": rgb(node["fill"])["opaqueColor"]}} if node["fill"]
                                         else {"propertyState": "NOT_RENDERED"}),
                 "outline": ({"outlineFill": {"solidFill": {"color": rgb(node["stroke"])["opaqueColor"]}},
                              "weight": pt(round(max(0.5, (node["width"] or 0.4) * scale), 2))}
                             if node["stroke"] else {"propertyState": "NOT_RENDERED"})}
        members = []
        if props:  # free labels (edge labels, captions) have no shape, only the text box below
            fields = ["contentAlignment", "autofit.autofitType", "shapeBackgroundFill"]
            fields += ["outline.outlineFill.solidFill.color", "outline.weight"] if node["stroke"] else ["outline.propertyState"]
            if not node["fill"]:
                fields[fields.index("shapeBackgroundFill")] = "shapeBackgroundFill.propertyState"
            else:
                fields[fields.index("shapeBackgroundFill")] = "shapeBackgroundFill.solidFill.color"
            if copied:
                tpl = template(node_template_key(node))
                reqs += [
                    {"duplicateObject": {"objectId": tpl["id"], "objectIds": {tpl["id"]: oid}}},
                    {"updatePageElementTransform": {"objectId": oid, "applyMode": "ABSOLUTE", "transform": {
                        "scaleX": (x1 - x0) / tpl["w"], "scaleY": (y1 - y0) / tpl["h"], "unit": "EMU",
                        "translateX": round(x0 * EMU_PER_PT), "translateY": round(y0 * EMU_PER_PT)}}},
                    {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}},
                ]
            else:
                reqs.append({"createShape": {"objectId": oid, "shapeType": node["shape"], "elementProperties": {
                    "pageObjectId": slide_id, "size": {"width": emu(x1 - x0), "height": emu(y1 - y0)},
                    "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                                  "translateX": round(x0 * EMU_PER_PT), "translateY": round(y0 * EMU_PER_PT)}}}})
            reqs.append({"updateShapeProperties": {"objectId": oid, "shapeProperties": props, "fields": ",".join(fields)}})
            members.append(oid)
        if card:
            for k, box in enumerate(card):
                label = f"{object_id}_x{j}" + (f"_{k}" if k else "")
                reqs += text_box_requests(box, slide_id, label, scale, fonts)
                members.append(label)
        elif text:
            if not inside:
                # A label wider than the node's text rectangle would wrap inside the shape: it
                # gets its own wider text box, centred on the node and grouped with it.
                label = f"{object_id}_x{j}"
                cx, w = (x0 + x1) / 2, (x1 - x0) + 2 * PAD_X + 40
                reqs += [
                    {"createShape": {"objectId": label, "shapeType": "TEXT_BOX", "elementProperties": {
                        "pageObjectId": slide_id, "size": {"width": emu(w), "height": emu(y1 - y0)},
                        "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                                      "translateX": round((cx - w / 2) * EMU_PER_PT), "translateY": round(y0 * EMU_PER_PT)}}}},
                    {"updateShapeProperties": {"objectId": label, "fields": "contentAlignment,autofit.autofitType",
                                               "shapeProperties": {"contentAlignment": "MIDDLE",
                                                                   "autofit": {"autofitType": "NONE"}}}},
                ]
                members.append(label)
            target = oid if inside else label
            reqs.append({"insertText": {"objectId": target, "text": text}})
            start = 0
            for runs in map(in_sentence, node["paragraphs"]):
                line_text = "".join(r["text"] for r in runs).strip()
                offset = 0
                for run in runs:
                    piece = run["text"].strip() if len(runs) == 1 else run["text"]
                    if offset == 0:
                        piece = piece.lstrip()
                    if not piece:
                        continue
                    style, sfields = fonts.text_style(run, scale)
                    style["foregroundColor"] = rgb(run["color"])
                    reqs.append({"updateTextStyle": {  # (UTF-16 units: u16)
                        "objectId": target, "style": style, "fields": ",".join(sfields + ["foregroundColor"]),
                        "textRange": {"type": "FIXED_RANGE", "startIndex": start + offset,
                                      "endIndex": min(start + u16(line_text), start + offset + u16(piece))}}})
                    offset += u16(piece)
                start += u16(line_text) + 1
            reqs.append({"updateParagraphStyle": {
                "objectId": target, "textRange": {"type": "ALL"}, "fields": "alignment,lineSpacing,spaceAbove,spaceBelow",
                "style": {"alignment": "CENTER", "lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0)}}})
        if len(members) >= 2:  # a node with its label (or card texts) outside: they move together
            reqs.append({"groupObjects": {"groupObjectId": f"{object_id}_g{j}", "childrenObjectIds": members}})
            members = [f"{object_id}_g{j}"]
        children += members
    # Edges follow the nodes they start or end on.
    for oid, start, end, ln in segments:
        ends = {"startConnection": connection(start, el["nodes"], node_oids) if start == ln["from"] else None,
                "endConnection": connection(end, el["nodes"], node_oids) if end == ln["to"] else None}
        ends = {k: v for k, v in ends.items() if v}
        if ends:
            reqs.append({"updateLineProperties": {"objectId": oid, "fields": ",".join(ends), "lineProperties": ends}})
    if len(children) >= 2:
        reqs.append({"groupObjects": {"groupObjectId": object_id, "childrenObjectIds": children}})
    return reqs


def block_groups(elements: list[dict], object_ids: list[str], title_oid: str | None) -> list[list[str]]:
    """Object ids per block: its panel shapes (title bar and body, see classify.blocks), plus
    the text and pictures lying on them."""
    blocks: dict[int, list] = {}  # block -> [x0, y0, x1, y1, [oids]]
    for el, oid in zip(elements, object_ids):
        if el["kind"] == "shape" and el.get("block") is not None:
            x0, y0, x1, y1 = el["bbox"]
            b = blocks.setdefault(el["block"], [x0, y0, x1, y1, []])
            b[:4] = [min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)]
            b[4].append(oid)
    out = []
    for x0, y0, x1, y1, members in blocks.values():
        if len(members) < 2:
            continue  # a lone panel is not recognisably a block
        for el, oid in zip(elements, object_ids):
            # Tables can't be grouped in Slides: a table in a block stays on its own.
            if el["kind"] in ("text", "image") and oid != title_oid and not el.get("anchor"):
                ex0, ey0, ex1, ey1 = el["bbox"]
                cx, cy = (ex0 + ex1) / 2, (ey0 + ey1) / 2
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    members.append(oid)
        out.append(members)
    return out


def rule_groups(elements: list[dict], object_ids: list[str]) -> list[list[str]]:
    """Rules lying on one another (a progress bar on its track) move as one."""
    rules = [(el["bbox"], oid) for el, oid in zip(elements, object_ids) if el["kind"] == "shape" and el.get("role") == "rule"]
    out: list[list] = []  # [bbox, [oids]]
    for (x0, y0, x1, y1), oid in rules:
        for g in out:
            gx0, gy0, gx1, gy1 = g[0]
            if x0 < gx1 and gx0 < x1 and y0 < gy1 and gy0 < y1:
                g[0] = [min(x0, gx0), min(y0, gy0), max(x1, gx1), max(y1, gy1)]
                g[1].append(oid)
                break
        else:
            out.append([[x0, y0, x1, y1], [oid]])
    return [g[1] for g in out if len(g[1]) >= 2]
