"""Probe: pictures and backgrounds carried by the imported .pptx instead of public URLs.

The Slides API only inserts images from publicly fetchable URLs, which protected Google
Workspace domains forbid. A .pptx imported through Drive can carry the pictures itself. Does
the import keep: a master background picture (inherited by layouts and slides), a slide's own
background picture or colour, pictures with their alt text, empty title placeholders? And does
duplicateObject of such a slide keep all of that under the object IDs we choose?

Usage: python tools/probe_pptx_pictures.py [presentationId-to-reuse]
"""

import io
import json
import sys
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.oxml.xmlchemy import BaseOxmlElement
from pptx.parts.slide import BaseSlidePart
from pptx.util import Emu, Pt

from beamer2slides.gapi import media_upload
from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import Page, background_fill, file_id, object_id
from beamer2slides.gslides import EMU_PER_PT, execute, save_thumbnail
from beamer2slides.json_types import Json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_pptx_pictures"
SRC = ROOT / "out" / "gdg-talk"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def picture_background(part: BaseSlidePart, c_sld: BaseOxmlElement | None, png: Path) -> None:
    if c_sld is None:
        raise ValueError("a page without its cSld")
    _, rid = part.get_or_add_image_part(str(png))
    bg = etree.fromstring(
        f'<p:bg xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:bgPr><a:blipFill dpi="0" rotWithShape="1">'
        f'<a:blip r:embed="{rid}"/><a:srcRect/><a:stretch><a:fillRect/></a:stretch></a:blipFill>'
        f'<a:effectLst/></p:bgPr></p:bg>')
    old = c_sld.find(f"{{{P}}}bg")
    if old is not None:
        c_sld.remove(old)
    c_sld.insert(0, bg)


def colour_background(c_sld: BaseOxmlElement | None, hex_colour: str) -> None:
    if c_sld is None:
        raise ValueError("a page without its cSld")
    bg = etree.fromstring(
        f'<p:bg xmlns:p="{P}" xmlns:a="{A}"><p:bgPr><a:solidFill><a:srgbClr val="{hex_colour}"/></a:solidFill>'
        f'<a:effectLst/></p:bgPr></p:bg>')
    c_sld.insert(0, bg)


def build() -> io.BytesIO:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(720 * EMU_PER_PT), Emu(405 * EMU_PER_PT)
    master = prs.slide_master
    master_part = master.part
    if not isinstance(master_part, BaseSlidePart):
        raise TypeError(f"python-pptx's slide master is a {type(master_part).__name__}, not a slide part")
    picture_background(master_part, master.element.find(f"{{{P}}}cSld"), SRC / "backgrounds" / "bg-005.png")

    s1 = prs.slides.add_slide(prs.slide_layouts[5])  # Title Only, inherits the master background
    pic = s1.shapes.add_picture(str(SRC / "figures" / "p4h1.png"), Pt(100), Pt(150), Pt(200), Pt(100))
    c_nv_pr = pic._element.find(f"{{{P}}}nvPicPr/{{{P}}}cNvPr")
    if c_nv_pr is None:
        raise ValueError("python-pptx made a picture without its cNvPr")
    c_nv_pr.set("descr", "b2s_f3")
    c_nv_pr.set("title", "Figure")

    s2 = prs.slides.add_slide(prs.slide_layouts[6])  # Blank, own background picture
    picture_background(s2.part, s2.element.find(f"{{{P}}}cSld"), SRC / "backgrounds" / "bg-009.png")

    s3 = prs.slides.add_slide(prs.slide_layouts[0])  # Title, own colour
    colour_background(s3.element.find(f"{{{P}}}cSld"), "FDE293")
    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def fill(page: Page) -> dict[str, Json]:
    """A page's background fill, its picture's URL said only as there or not."""
    out: dict[str, Json] = {}
    for k, v in background_fill(page).items():
        if k == "stretchedPictureFill" and isinstance(v, dict):
            v = {"contentUrl": bool(v.get("contentUrl")), "size": v.get("size")}
        out[k] = v
    return out


def main() -> None:
    drive, slides = drive_service(None), slides_service(None)
    media = media_upload(build(), PPTX_MIME)
    if len(sys.argv) > 1:
        pid = sys.argv[1]
        execute(drive.files().update(fileId=pid, media_body=media, fields="id"))
    else:
        pid = file_id(execute(drive.files().create(
            body={"name": "b2s probe pptx pictures", "mimeType": "application/vnd.google-apps.presentation"},
            media_body=media, fields="id")), "the uploaded probe deck")
    pres = execute(slides.presentations().get(presentationId=pid))

    for m in pres.get("masters", []):
        print("master", object_id(m), json.dumps(fill(m)))
    for l in pres.get("layouts", [])[:3]:
        print("layout", object_id(l), json.dumps(fill(l)))
    for s in pres.get("slides", []):
        print("slide", object_id(s), json.dumps(fill(s)))
        for pe in s.get("pageElements", []):
            kind = next(k for k in ("shape", "image", "table", "line", "elementGroup") if k in pe)
            print("   ", object_id(pe), kind, pe.get("title"), pe.get("description"),
                  pe.get("shape", {}).get("placeholder"), json.dumps(pe.get("transform")), json.dumps(pe.get("size")))

    first, second = pres.get("slides", [])[:2]
    ids = {object_id(first): "b2s_s900",
           **{object_id(pe): f"b2s_s900_e{j}" for j, pe in enumerate(first.get("pageElements", []))}}
    ids2 = {object_id(second): "b2s_s901"}
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [
        {"duplicateObject": {"objectId": object_id(first), "objectIds": ids}},
        {"duplicateObject": {"objectId": object_id(second), "objectIds": ids2}},
        {"updatePageElementsZOrder": {"pageElementObjectIds": ["b2s_s900_e1"], "operation": "BRING_TO_FRONT"}},
        {"insertText": {"objectId": "b2s_s900_e0", "text": "Duplicated"}},
    ]}))
    pres = execute(slides.presentations().get(presentationId=pid))
    for s in pres.get("slides", []):
        if object_id(s).startswith("b2s_"):
            print("dup", object_id(s), json.dumps(fill(s)),
                  [(object_id(pe), pe.get("description")) for pe in s.get("pageElements", [])])
    save_thumbnail(slides, pid, "b2s_s900", OUT / "dup-900.png", None)
    save_thumbnail(slides, pid, "b2s_s901", OUT / "dup-901.png", None)
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
