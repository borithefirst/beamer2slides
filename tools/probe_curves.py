"""Probe: can a TikZ curve (bend left, out/in, a loop, a brace) reach Slides as an editable line?

The Slides API creates only straight, bent and curved *connectors* (createLine's lineCategory),
whose curve it cannot shape. A .pptx can carry any path (`a:custGeom`, cubic Béziers) and the
connector presets with their adjustments; emit already brings its templates in through the
.pptx. This probe uploads a .pptx with:

- `sp_curve`   a shape whose geometry is one open cubic (a `bend left` edge), arrow head at its end;
- `cxn_curve`  the same path as a connector (`p:cxnSp`);
- `sp_dashed`  the open cubic, dashed, arrow heads at both ends;
- `curved3`    the preset curvedConnector3 at adj 30% (Slides' own curved connector);
- `arc`        the preset arc (a quarter circle) with an arrow head;
- `loop`       a closed-looking open path (a self loop above a node): two cubics;
- `brace`      a brace's six-piece path (c l c c l c), filled none;

then reads each back (shape or line, its type, outline/arrows/dash as the API reports them),
copies `sp_curve` with duplicateObject and stretches the copy (emit's template path), restyles
it (colour, weight, dash) through the API, groups it with an API-made rectangle, and saves the
thumbnail. The presentation goes to the trash at the end (keep it with --keep).

Usage: python tools/probe_curves.py [--keep]
"""

import io
import json
import sys
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.util import Emu

from beamer2slides import gapi
from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import PageElement, SlidesRequest, SlidesService, file_id, object_id, part
from beamer2slides.gslides import EMU_PER_PT, emu, execute, save_thumbnail

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_curves"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

Pt2 = tuple[float, float]
# One path command in the shape's own box (pt): ("M", p) | ("L", p) | ("C", p1, p2, p3)
Command = tuple[str, list[Pt2]]

BEND: list[Command] = [("M", [(0, 30)]), ("C", [(30, 0), (90, 0), (120, 30)])]
LOOP: list[Command] = [("M", [(10, 40)]), ("C", [(-10, 0), (50, 0), (30, 40)])]
BRACE: list[Command] = [("M", [(0, 12)]), ("C", [(0, 6), (3, 6), (6, 6)]), ("L", [(54, 6)]),
                        ("C", [(57, 6), (60, 6), (60, 0)]), ("C", [(60, 6), (63, 6), (66, 6)]), ("L", [(114, 6)]),
                        ("C", [(117, 6), (120, 6), (120, 12)])]


def _e(v: float) -> str:
    return str(round(v * EMU_PER_PT))


def _ln(dash: bool, head: bool, tail: bool) -> str:
    return (f'<a:ln w="{_e(1.5)}"><a:solidFill><a:srgbClr val="1F3A93"/></a:solidFill>'
            + ('<a:prstDash val="dash"/>' if dash else "")
            + ('<a:headEnd type="triangle"/>' if head else "")
            + ('<a:tailEnd type="triangle"/>' if tail else "") + "</a:ln>")


def _xfrm(x: float, y: float, w: float, h: float) -> str:
    return f'<a:xfrm><a:off x="{_e(x)}" y="{_e(y)}"/><a:ext cx="{_e(w)}" cy="{_e(h)}"/></a:xfrm>'


def _cust(path: list[Command], w: float, h: float) -> str:
    def pt(p: Pt2) -> str:
        return f'<a:pt x="{_e(p[0])}" y="{_e(p[1])}"/>'
    body = ""
    for op, pts in path:
        tag = {"M": "moveTo", "L": "lnTo", "C": "cubicBezTo"}[op]
        body += f"<a:{tag}>{''.join(pt(p) for p in pts)}</a:{tag}>"
    return (f'<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="0" t="0" r="r" b="b"/>'
            f'<a:pathLst><a:path w="{_e(w)}" h="{_e(h)}" fill="none">{body}</a:path></a:pathLst></a:custGeom>')


def _prst(name: str, adj: dict[str, int]) -> str:
    gds = "".join(f'<a:gd name="{k}" fmla="val {v}"/>' for k, v in adj.items())
    return f'<a:prstGeom prst="{name}"><a:avLst>{gds}</a:avLst></a:prstGeom>'


def _sp(sid: int, name: str, connector: bool, spPr: str) -> etree._Element:
    if connector:
        xml = (f'<p:cxnSp xmlns:p="{P}" xmlns:a="{A}"><p:nvCxnSpPr><p:cNvPr id="{sid}" name="{name}"/>'
               f'<p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr><p:spPr>{spPr}</p:spPr></p:cxnSp>')
    else:
        xml = (f'<p:sp xmlns:p="{P}" xmlns:a="{A}"><p:nvSpPr><p:cNvPr id="{sid}" name="{name}"/>'
               f'<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>{spPr}</p:spPr>'
               f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr/></a:p></p:txBody></p:sp>')
    return etree.fromstring(xml)


# name: (connector?, spPr)
ITEMS: dict[str, tuple[bool, str]] = {
    "sp_curve": (False, _xfrm(30, 40, 120, 30) + _cust(BEND, 120, 30) + "<a:noFill/>" + _ln(False, False, True)),
    "cxn_curve": (True, _xfrm(200, 40, 120, 30) + _cust(BEND, 120, 30) + "<a:noFill/>" + _ln(False, False, True)),
    "sp_dashed": (False, _xfrm(370, 40, 120, 30) + _cust(BEND, 120, 30) + "<a:noFill/>" + _ln(True, True, True)),
    "curved3": (True, _xfrm(30, 150, 120, 60) + _prst("curvedConnector3", {"adj1": 30000}) + _ln(False, False, True)),
    "arc": (False, _xfrm(200, 150, 80, 80) + _prst("arc", {"adj1": 10800000, "adj2": 0}) + "<a:noFill/>"
            + _ln(False, False, True)),
    "loop": (False, _xfrm(370, 150, 40, 40) + _cust(LOOP, 40, 40) + "<a:noFill/>" + _ln(False, False, True)),
    "brace": (False, _xfrm(450, 150, 120, 12) + _cust(BRACE, 120, 12) + "<a:noFill/>" + _ln(False, False, False)),
}


def build() -> io.BytesIO:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(720 * EMU_PER_PT), Emu(405 * EMU_PER_PT)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    tree = slide.shapes._spTree
    for i, (name, (connector, spPr)) in enumerate(ITEMS.items()):
        tree.append(_sp(100 + i, name, connector, spPr))
    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def describe(pe: PageElement) -> dict[str, object]:
    out: dict[str, object] = {"id": object_id(pe), "size": pe.get("size"), "transform": pe.get("transform")}
    if "shape" in pe:
        shape = part(pe.get("shape"), "shape")
        props = part(shape.get("shapeProperties"), "shapeProperties") if "shapeProperties" in shape else {}
        out.update(kind="shape", shapeType=shape.get("shapeType"), outline=props.get("outline"),
                   fill=props.get("shapeBackgroundFill"))
    elif "line" in pe:
        line = part(pe.get("line"), "line")
        out.update(kind="line", lineType=line.get("lineType"), lineCategory=line.get("lineCategory"),
                   lineProperties=line.get("lineProperties"))
    else:
        out.update(kind=sorted(k for k in pe if k not in ("objectId", "size", "transform")))
    return out


def main() -> None:
    keep = "--keep" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    drive, slides = drive_service(None), slides_service(None)
    media = gapi.media_upload(build(), PPTX_MIME)
    pid = file_id(execute(drive.files().create(
        body={"name": "b2s probe curves", "mimeType": "application/vnd.google-apps.presentation"},
        media_body=media, fields="id")), "the uploaded probe deck")
    try:
        run(slides, pid)
    finally:
        if keep:
            print(f"kept https://docs.google.com/presentation/d/{pid}/edit")
        else:
            execute(drive.files().update(fileId=pid, body={"trashed": True}))
            print("presentation moved to the trash")


def run(slides: SlidesService, pid: str) -> None:
    pres = execute(slides.presentations().get(presentationId=pid))
    page = pres.get("slides", [])[0]
    elements = page.get("pageElements", [])
    (OUT / "imported.json").write_text(json.dumps(elements, indent=1), encoding="utf-8")
    # (the importer drops the names; elements come back in creation order)
    by_name = dict(zip(ITEMS, elements))
    print(f"{len(elements)} elements imported for {len(ITEMS)} written")
    for name, pe in by_name.items():
        print(f"  {name:10s} {json.dumps(describe(pe))}")
    curve = by_name.get("sp_curve")
    if curve is None:
        return
    oid, sid = object_id(curve), object_id(page)
    copy, box = "probe_curve_copy", "probe_box"
    reqs: list[SlidesRequest] = [
        {"duplicateObject": {"objectId": oid, "objectIds": {oid: copy}}},
        {"updatePageElementTransform": {"objectId": copy, "applyMode": "ABSOLUTE", "transform": {
            "scaleX": 2.0, "scaleY": -1.5, "unit": "EMU",
            "translateX": round(30 * EMU_PER_PT), "translateY": round(330 * EMU_PER_PT)}}},
        {"createShape": {"objectId": box, "shapeType": "RECTANGLE", "elementProperties": {
            "pageObjectId": sid, "size": {"width": emu(60), "height": emu(30)},
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                          "translateX": round(300 * EMU_PER_PT), "translateY": round(300 * EMU_PER_PT)}}}},
    ]
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    # Restyling and grouping, each alone so a refusal names itself.
    tries: dict[str, SlidesRequest] = {
        "outline as a shape": {"updateShapeProperties": {"objectId": copy, "fields": "outline.outlineFill.solidFill.color,outline.weight,outline.dashStyle",
                                                        "shapeProperties": {"outline": {
                                                            "outlineFill": {"solidFill": {"color": {"rgbColor": {"red": 0.8}}}},
                                                            "weight": {"magnitude": 3, "unit": "PT"}, "dashStyle": "DOT"}}}},
        "line properties": {"updateLineProperties": {"objectId": copy, "fields": "dashStyle,endArrow",
                                                     "lineProperties": {"dashStyle": "DOT", "endArrow": "FILL_CIRCLE"}}},
        "group with a rectangle": {"groupObjects": {"groupObjectId": "probe_group", "childrenObjectIds": [copy, box]}},
    }
    for what, req in tries.items():
        try:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [req]}))
            print(f"  {what}: accepted")
        except gapi.HttpError as e:
            print(f"  {what}: refused ({str(e)[:200]})")
    got = execute(slides.presentations().get(presentationId=pid))
    after = got.get("slides", [])[0].get("pageElements", [])
    (OUT / "after.json").write_text(json.dumps(after, indent=1), encoding="utf-8")
    for pe in after:
        print(f"  after: {json.dumps(describe(pe))[:300]}")
    save_thumbnail(slides, pid, sid, OUT / "slide.png", None)
    print(OUT / "slide.png")


if __name__ == "__main__":
    main()
