"""The .pptx that carries a deck's pictures, backgrounds, layouts, template shapes and tables into
Slides; shape requests; a batch of requests.
"""

import io
from pathlib import Path

from .emit_metrics import SLIDE_W, rgb, xml_text
from .gapi import HttpError, message_of
from .gslides import EMU_PER_PT, emu, execute, pt


# Native drop shadows, calibrated against beamer's block shadow (tools/calibrate_shadow.py):
# distance and blur per point of the shadow's width in the PDF, at 45°.
SHADOW_DISTANCE = 0.75
SHADOW_BLUR = 1.0
SHADOW_ALPHA = 0.5
TEMPLATE_PRESETS = {"ROUND_RECTANGLE": "roundRect", "ROUND_2_SAME_RECTANGLE": "round2SameRect", "RECTANGLE": "rect"}
TEMPLATE_LAYOUTS = {"TITLE": 0, "TITLE_ONLY": 5, "BLANK": 6}  # python-pptx default template layout indexes
# what `_add_template_shapes` can put on a source slide
TEMPLATE_KINDS = ("ROUND_RECTANGLE", "ROUND_2_SAME_RECTANGLE", "RECTANGLE", "ELLIPSE", "DIAMOND", "TRIANGLE")


def template_key(el: dict, scale: float) -> tuple | None:
    """Shapes the API can't make exactly: rounded corners of a given radius (the API only
    creates the default rounding) and drop shadows (read-only in the API). They are
    duplicated from template shapes that come with the imported .pptx. A kind no template is made
    of has none: createShape refuses it, and the refused element becomes a picture
    (`fallback_pictures`) instead of the whole .pptx failing."""
    if el["kind"] != "shape" or el["shape"] not in TEMPLATE_KINDS or (el["shape"] == "RECTANGLE" and not el.get("shadow")):
        return None
    x0, y0, x1, y1 = el["bbox"]
    adj = 0.0
    if el["shape"] != "RECTANGLE":
        adj = min(0.5, round(el.get("radius", 0.0) / max(min(x1 - x0, y1 - y0), 0.01), 2))
    shadow = round(2 * el["shadow"]["size"] * scale) / 2 if el.get("shadow") else None
    return el["shape"], adj, shadow


NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _set_background(part, c_sld, fill: dict) -> None:
    """A page background in the .pptx: {"color": "#rrggbb"} or {"picture": Path} (stretched)."""
    from lxml import etree

    if "color" in fill:
        inner = f'<a:solidFill><a:srgbClr val="{fill["color"].lstrip("#").upper()}"/></a:solidFill>'
    else:
        _, rid = part.get_or_add_image_part(str(fill["picture"]))  # identical files are stored once
        inner = (f'<a:blipFill dpi="0" rotWithShape="1"><a:blip r:embed="{rid}"/><a:srcRect/>'
                 f'<a:stretch><a:fillRect/></a:stretch></a:blipFill>')
    old = c_sld.find(f"{{{NS_P}}}bg")
    if old is not None:
        c_sld.remove(old)
    c_sld.insert(0, etree.fromstring(
        f'<p:bg xmlns:p="{NS_P}" xmlns:a="{NS_A}" xmlns:r="{NS_R}"><p:bgPr>{inner}<a:effectLst/></p:bgPr></p:bg>'))


def _add_template_shapes(slide, keys: list[tuple]) -> None:
    from lxml import etree
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.util import Pt

    kinds = dict(zip(TEMPLATE_KINDS, (MSO_SHAPE.ROUNDED_RECTANGLE, MSO_SHAPE.ROUND_2_SAME_RECTANGLE, MSO_SHAPE.RECTANGLE,
                                      MSO_SHAPE.OVAL, MSO_SHAPE.DIAMOND, MSO_SHAPE.ISOSCELES_TRIANGLE)))
    a = NS_A
    for i, (kind, adj, shadow) in enumerate(keys):
        if kind == "BENT_CONNECTOR":
            line = slide.shapes.add_connector(MSO_CONNECTOR.ELBOW, Pt(10), Pt(10), Pt(110), Pt(110))
            geometry = line._element.spPr.find(f"{{{a}}}prstGeom")
            geometry.set("prst", "bentConnector3")
            for old in geometry.findall(f"{{{a}}}avLst"):
                geometry.remove(old)
            geometry.append(etree.fromstring(
                f'<a:avLst xmlns:a="{a}"><a:gd name="adj1" fmla="val {round(adj * 100000)}"/></a:avLst>'))
            line._element.spPr.append(etree.fromstring(f'<a:effectLst xmlns:a="{a}"/>'))
            continue
        shape = slide.shapes.add_shape(kinds[kind], Pt(10 + i % 10 * 20), Pt(10 + i // 10 * 20), Pt(100), Pt(100))
        if adj is not None and kind in ("ROUND_RECTANGLE", "ROUND_2_SAME_RECTANGLE"):
            shape.adjustments[0] = adj
            if kind == "ROUND_2_SAME_RECTANGLE":
                shape.adjustments[1] = 0.0
        shape.fill.solid()
        shape.line.fill.background()
        # No text padding (the API can't set it): a diagram label fits a node as tight as TikZ's.
        body_pr = shape.text_frame._txBody.find(f"{{{a}}}bodyPr")
        for side in ("lIns", "tIns", "rIns", "bIns"):
            body_pr.set(side, "0")
        body_pr.set("anchor", "ctr")
        # python-pptx shapes refer to the theme's effect style, which has a shadow: always
        # give an explicit (possibly empty) effect list.
        effects = (f'<a:outerShdw blurRad="{round(SHADOW_BLUR * shadow * EMU_PER_PT)}" '
                   f'dist="{round(SHADOW_DISTANCE * shadow * EMU_PER_PT)}" dir="2700000" algn="tl" rotWithShape="0">'
                   f'<a:srgbClr val="000000"><a:alpha val="{round(SHADOW_ALPHA * 100000)}"/></a:srgbClr>'
                   f'</a:outerShdw>') if shadow else ""
        shape.element.spPr.append(etree.fromstring(f'<a:effectLst xmlns:a="{a}">{effects}</a:effectLst>'))


NO_TABLE_STYLE = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"  # PowerPoint's "No Style, No Grid"


def _add_table(slide, table: dict) -> None:
    """An empty table (pptx_table) on a source slide. Its cell margins are what the API can't
    set: a table made by createTable has 7.2 pt above and below every line, one from a .pptx the
    file's (tools/probe_pptx_table_margins.py); duplicating the slide, inserting rows and columns
    and filling or styling cells through the API all keep them."""
    from lxml import etree
    from pptx.util import Emu

    def e(v: float) -> Emu:
        return Emu(round(v * EMU_PER_PT))

    rows, cols = len(table["heights"]), len(table["widths"])
    frame = slide.shapes.add_table(rows, cols, e(table["x"]), e(table["y"]), e(sum(table["widths"])),
                                   e(sum(table["heights"])))
    pr = frame._element.graphic.graphicData.tbl.tblPr
    for flag in ("firstRow", "bandRow"):  # (python-pptx's default look: a header row and bands)
        pr.attrib.pop(flag, None)
    style = pr.find(f"{{{NS_A}}}tableStyleId")
    if style is None:
        style = etree.SubElement(pr, f"{{{NS_A}}}tableStyleId")
    style.text = NO_TABLE_STYLE
    for c, w in enumerate(table["widths"]):
        frame.table.columns[c].width = e(w)
    middle = {tuple(rc) for rc in table.get("middle", [])}
    for r, h in enumerate(table["heights"]):
        frame.table.rows[r].height = e(h)
        left, top, right, bottom = table["margins"][r]
        for c in range(cols):
            cell = frame.table.cell(r, c)
            cell.margin_left, cell.margin_top, cell.margin_right, cell.margin_bottom = \
                e(left), e(0.0 if (r, c) in middle else top), e(right), e(bottom)


VARIANT = "_V"      # layout name suffix: a copy of the layout with another theme decoration (plan_theme)
THEME_VARIANTS = 3


def _clone_layout(prs, layout, name: str):
    """A copy of a layout (placeholders only, no pictures) added to the master, shown as `name`."""
    from copy import deepcopy

    from lxml import etree
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.opc.packuri import PackURI
    from pptx.parts.slide import SlideLayoutPart

    master = prs.slide_master
    taken = {str(p.partname) for p in prs.part.package.iter_parts()}
    k = next(k for k in range(1, 1000) if f"/ppt/slideLayouts/slideLayout{k}.xml" not in taken)
    element = deepcopy(layout.element)
    element.cSld.set("name", name)
    element.set("type", "cust")  # (its own layout name in Slides, never taken for the original's)
    part = SlideLayoutPart(PackURI(f"/ppt/slideLayouts/slideLayout{k}.xml"), layout.part.content_type,
                           layout.part.package, element)
    part.relate_to(master.part, RT.SLIDE_MASTER)
    ids = master.element.get_or_add_sldLayoutIdLst()
    entry = etree.SubElement(ids, f"{{{NS_P}}}sldLayoutId")
    entry.set("id", str(max(int(e.get("id")) for e in ids if e.get("id")) + 1))
    entry.set(f"{{{NS_R}}}id", master.part.relate_to(part, RT.SLIDE_LAYOUT))
    return part.slide_layout


def _add_decoration(layout, picture: Path, width: int, height: int) -> None:
    """The theme decoration as a full-page picture at the bottom of a layout: above the slide
    background, below everything on the slide."""
    from lxml import etree

    _, rid = layout.part.get_or_add_image_part(str(picture))
    tree = layout.shapes._spTree
    shape_id = max([int(e.get("id")) for e in tree.iter(f"{{{NS_P}}}cNvPr")] + [1]) + 1
    tree.insert(2, etree.fromstring(
        f'<p:pic xmlns:p="{NS_P}" xmlns:a="{NS_A}" xmlns:r="{NS_R}"><p:nvPicPr>'
        f'<p:cNvPr id="{shape_id}" name="Theme" descr="Theme decoration"/><p:cNvPicPr><a:picLocks noGrp="1"/></p:cNvPicPr>'
        f'<p:nvPr userDrawn="1"/></p:nvPicPr><p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch>'
        f'</p:blipFill><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width}" cy="{height}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'))


def build_pptx(page_w: float, page_h: float, keys: list[tuple], pages: list[dict], master_fill: dict,
               decorations: dict | None = None) -> io.BytesIO:
    """The deck's starting point, imported through Drive. It carries everything the Slides API
    could only insert from a public URL, so no picture ever leaves the user's Drive:

    - the PDF's page size (presentations.create ignores pageSize);
    - the master background (`master_fill`), inherited by the layouts and most slides;
    - the theme decoration (`decorations`, see plan_theme) at the bottom of the layouts; a page
      layout named with the VARIANT suffix is a copy of its layout with that variant's decoration;
    - one source slide per deck slide (`pages`: {"layout", "fill" (None: inherit),
      "pictures": [{"file", "bbox" (slide pt), "alt", "title"}], "tables": [pptx_table(...)],
      "templates" (bool)}), holding its pictures, its tables (empty, with the cell margins the API
      cannot set) and, if it needs any, the template shapes (shadows, exact corner radii).

    emit copies each source slide under our own object IDs and then deletes it."""
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()
    height = SLIDE_W * page_h / page_w
    ratio = height / (prs.slide_height / EMU_PER_PT)
    prs.slide_width, prs.slide_height = Emu(round(SLIDE_W * EMU_PER_PT)), Emu(round(height * EMU_PER_PT))
    if abs(ratio - 1) > 1e-3:  # the default template's placeholders are laid out for 4:3
        for page in [prs.slide_master, *prs.slide_layouts]:
            for shape in page.placeholders:
                # A layout placeholder without its own position inherits the master's, rescaled
                # already (setting its top and height would scale it twice and write x and width 0).
                if shape._element.spPr.find(f"{{{NS_A}}}xfrm") is not None and shape.height is not None:
                    shape.top, shape.height = Emu(round(shape.top * ratio)), Emu(round(shape.height * ratio))
    master = prs.slide_master
    _set_background(master.part, master.element.find(f"{{{NS_P}}}cSld"), master_fill)
    decorations = decorations or {}
    layouts = {name: prs.slide_layouts[i] for name, i in TEMPLATE_LAYOUTS.items()}
    originals = list(prs.slide_layouts)
    for name in dict.fromkeys(p["layout"] for p in pages if VARIANT in p["layout"]):
        kind, n = name.rsplit(VARIANT, 1)
        picture = decorations.get(f"{'TITLE' if kind == 'TITLE' else '*'}{VARIANT}{n}")
        layouts[name] = _clone_layout(prs, layouts[kind], f"{layouts[kind].name} ({f'theme {int(n) + 1}' if picture else 'no theme'})")
        if picture:
            _add_decoration(layouts[name], picture, prs.slide_width, prs.slide_height)
    for i, layout in enumerate(originals):
        picture = decorations.get("TITLE" if i == TEMPLATE_LAYOUTS["TITLE"] else "*")
        if picture:
            _add_decoration(layout, picture, prs.slide_width, prs.slide_height)
    for page in pages:
        slide = prs.slides.add_slide(layouts[page["layout"]])
        if page["fill"]:
            _set_background(slide.part, slide.element.find(f"{{{NS_P}}}cSld"), page["fill"])
        for pic in page["pictures"]:
            x0, y0, x1, y1 = pic["bbox"]
            shape = slide.shapes.add_picture(str(pic["file"]), Emu(round(x0 * EMU_PER_PT)), Emu(round(y0 * EMU_PER_PT)),
                                             Emu(round((x1 - x0) * EMU_PER_PT)), Emu(round((y1 - y0) * EMU_PER_PT)))
            if pic.get("alt"):  # (text from the PDF: raw Type 3 T1 codes are C0 controls, xml_text)
                shape._element.nvPicPr.cNvPr.set("descr", xml_text(pic["alt"]))
                shape._element.nvPicPr.cNvPr.set("title", xml_text(pic["title"]))
        for table in page.get("tables", []):
            _add_table(slide, table)
        if page["templates"]:
            _add_template_shapes(slide, keys)
    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def shape_requests(el: dict, slide_id: str, object_id: str, scale: float, template: dict | None = None) -> list[dict]:
    """A filled shape, outlined only with the frame it carries (`outline`). With a template ({"id", "w", "h"}: a template shape
    on this slide and its size in pt) the shape is a duplicate of it, else a new shape."""
    x0, y0, x1, y1 = (v * scale for v in el["bbox"])
    # ROUND_2_SAME_RECTANGLE rounds the top corners; for bottom corners flip both axes
    # (a 180° rotation), which moves the origin to the opposite corner.
    flip = -1 if el["flip"] else 1
    tx, ty = (x1, y1) if el["flip"] else (x0, y0)
    if template:
        reqs = [
            {"duplicateObject": {"objectId": template["id"], "objectIds": {template["id"]: object_id}}},
            {"updatePageElementTransform": {"objectId": object_id, "applyMode": "ABSOLUTE", "transform": {
                "scaleX": flip * (x1 - x0) / template["w"], "scaleY": flip * (y1 - y0) / template["h"], "unit": "EMU",
                "translateX": round(tx * EMU_PER_PT), "translateY": round(ty * EMU_PER_PT)}}},
            {"updatePageElementsZOrder": {"pageElementObjectIds": [object_id], "operation": "BRING_TO_FRONT"}},
        ]
    else:
        reqs = [{"createShape": {
            "objectId": object_id, "shapeType": el["shape"],
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {"width": emu(x1 - x0), "height": emu(y1 - y0)},
                "transform": {"scaleX": flip, "scaleY": flip, "unit": "EMU",
                              "translateX": round(tx * EMU_PER_PT), "translateY": round(ty * EMU_PER_PT)},
            },
        }}]
    # A framed panel (classify.frame_of, framed_panels: \fcolorbox, tcolorbox, a listing's
    # frame=single) carries its frame as the outline, on the frame's centre line; the rules
    # themselves left the background with the panel.
    frame = el.get("outline")
    outline = {"outlineFill": {"solidFill": {"color": rgb(frame["color"])["opaqueColor"]}},
               "weight": pt(round(max(0.25, frame["width"] * scale), 2)), "propertyState": "RENDERED"} \
        if frame else {"propertyState": "NOT_RENDERED"}
    outline_fields = "outline.outlineFill.solidFill.color,outline.weight,outline.propertyState" if frame \
        else "outline.propertyState"
    return reqs + [
        {"updateShapeProperties": {
            "objectId": object_id,
            "shapeProperties": {"shapeBackgroundFill": {"solidFill": {"color": rgb(el["fill"])["opaqueColor"],
                                                                      "alpha": el.get("opacity", 1.0)}},
                                "outline": outline},
            "fields": "shapeBackgroundFill.solidFill.color,shapeBackgroundFill.solidFill.alpha," + outline_fields,
        }},
    ]


def api_error(e: HttpError) -> str:
    return message_of(e)


def batch(slides, pid: str, reqs: list[dict]) -> None:
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
