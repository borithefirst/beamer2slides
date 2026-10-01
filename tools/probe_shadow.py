"""Probe: native drop shadows on shapes. The Slides API can't set a shadow, but a .pptx
imported through Drive can carry one. Does it survive the import, duplicateObject (of the
shape and of its slide), a fill change and a transform change?

Creates a deck from a python-pptx file with a template slide holding a shadowed rounded
rectangle, duplicates the slide and the shape, restyles the copy, prints the shadow
properties the API reports, and saves a thumbnail to out/probe_shadow.png.

Usage: python tools/probe_shadow.py
"""

import io
import json
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Emu, Pt

from beamer2slides.gapi import media_upload
from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import PageElement, SlidesRequest, file_id, object_id, part
from beamer2slides.gslides import EMU_PER_PT, execute, save_thumbnail
from beamer2slides.json_types import Json, JsonObject

OUT = Path(__file__).resolve().parents[1] / "out"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def shape_of(pe: PageElement) -> JsonObject:
    """A page element's shape part ({} for an element that is no shape)."""
    return part(pe.get("shape"), f"{pe.get('objectId')}.shape")


def shadow_of(pe: PageElement) -> Json:
    """The shadow the API reports on a shape (None: it reports none)."""
    return part(shape_of(pe).get("shapeProperties"), f"{pe.get('objectId')}.shapeProperties").get("shadow")


def shadow_xml(blur: float, dist: float, alpha: int) -> etree._Element:
    return etree.fromstring(
        f'<a:effectLst xmlns:a="{A}"><a:outerShdw blurRad="{round(blur * EMU_PER_PT)}" '
        f'dist="{round(dist * EMU_PER_PT)}" dir="2700000" algn="tl" rotWithShape="0">'
        f'<a:srgbClr val="000000"><a:alpha val="{alpha}"/></a:srgbClr></a:outerShdw></a:effectLst>')


def main() -> None:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(720 * EMU_PER_PT), Emu(405 * EMU_PER_PT)
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # Title Only
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Pt(40), Pt(120), Pt(300), Pt(120))
    shape.fill.solid()
    shape.line.fill.background()
    shape.adjustments[0] = 0.05
    shape.element.spPr.append(shadow_xml(8, 6, 40000))
    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    drive, slides = drive_service(None), slides_service(None)
    f = execute(drive.files().create(
        body={"name": "b2s probe shadow", "mimeType": "application/vnd.google-apps.presentation"},
        media_body=media_upload(buf, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        fields="id"))
    pid = file_id(f, "the probe deck")
    pres = execute(slides.presentations().get(presentationId=pid))
    tpl = pres.get("slides", [])[0]
    elements = tpl.get("pageElements", [])
    for pe in elements:
        shape = shape_of(pe)
        print(object_id(pe), shape.get("shapeType"), shape.get("placeholder"), json.dumps(shadow_of(pe)))
    box = next(pe for pe in elements if "placeholder" not in shape_of(pe))
    title = next(pe for pe in elements if "placeholder" in shape_of(pe))
    reqs: list[SlidesRequest] = [
        {"duplicateObject": {"objectId": object_id(tpl), "objectIds": {
            object_id(tpl): "probe_slide2", object_id(box): "probe_tpl2", object_id(title): "probe_title2"}}},
        {"duplicateObject": {"objectId": "probe_tpl2", "objectIds": {"probe_tpl2": "probe_copy"}}},
        {"updateShapeProperties": {"objectId": "probe_copy", "fields": "shapeBackgroundFill.solidFill.color",
                                   "shapeProperties": {"shapeBackgroundFill": {"solidFill": {"color": {"rgbColor": {"red": 0.9, "green": 0.94, "blue": 0.9}}}}}}},
        {"updatePageElementTransform": {"objectId": "probe_copy", "applyMode": "ABSOLUTE", "transform": {
            "scaleX": 1.5, "scaleY": 0.6, "unit": "EMU", "translateX": 360 * EMU_PER_PT, "translateY": 60 * EMU_PER_PT}}},
        {"insertText": {"objectId": "probe_title2", "text": "Duplicated slide"}},
        {"updateSlideProperties": {"objectId": object_id(tpl), "fields": "isSkipped", "slideProperties": {"isSkipped": True}}},
    ]
    try:
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    except Exception as e:
        print("batch failed:", e)
        reqs = reqs[:-1]
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
    pres = execute(slides.presentations().get(presentationId=pid))
    for s in pres.get("slides", []):
        print("slide", object_id(s), s.get("slideProperties", {}).get("isSkipped"),
              s.get("slideProperties", {}).get("layoutObjectId"))
        for pe in s.get("pageElements", []):
            print("  ", object_id(pe), shape_of(pe).get("shapeType"), json.dumps(shadow_of(pe)))
    save_thumbnail(slides, pid, "probe_slide2", OUT / "probe_shadow.png", None)
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
