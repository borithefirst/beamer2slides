"""Calibrate native drop shadows against beamer's block shadow.

Beamer's shadow (beamerbaseboxes, shadow=true) fades linearly from about 50% black at the
block's right and bottom edges to nothing 4 pt further out. This imports a .pptx with
rounded rectangles carrying outer shadows over a grid of (distance, blur, alpha), exports
the thumbnail and prints the darkness profile right of and below each shape.

Usage: python tools/calibrate_shadow.py
"""

import io
import itertools
from pathlib import Path

import numpy as np
import pymupdf
from googleapiclient.http import MediaIoBaseUpload
from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Emu, Pt

from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.gslides import EMU_PER_PT, execute, save_thumbnail

OUT = Path(__file__).resolve().parents[1] / "out"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

DISTANCES = [2.0, 4.0, 6.0]
BLURS = [4.0, 8.0, 12.0]
ALPHAS = [0.5, 0.8]
W, H = 90.0, 40.0


def cell(i: int) -> tuple[float, float]:
    return 20 + (i % 6) * 115, 30 + (i // 6) * 110


def main() -> None:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(720 * EMU_PER_PT), Emu(405 * EMU_PER_PT)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    combos = list(itertools.product(DISTANCES, BLURS, ALPHAS))
    for i, (dist, blur, alpha) in enumerate(combos):
        x, y = cell(i)
        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Pt(x), Pt(y), Pt(W), Pt(H))
        shape.fill.solid()
        shape.fill.fore_color.rgb = __import__("pptx.dml.color", fromlist=["RGBColor"]).RGBColor(0xE9, 0xE9, 0xF3)
        shape.line.fill.background()
        shape.element.spPr.append(etree.fromstring(
            f'<a:effectLst xmlns:a="{A}"><a:outerShdw blurRad="{round(blur * EMU_PER_PT)}" '
            f'dist="{round(dist * EMU_PER_PT)}" dir="2700000" algn="tl" rotWithShape="0">'
            f'<a:srgbClr val="000000"><a:alpha val="{round(alpha * 100000)}"/></a:srgbClr></a:outerShdw></a:effectLst>'))
    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    drive, slides = drive_service(), slides_service()
    pid = execute(drive.files().create(
        body={"name": "b2s calibrate shadow", "mimeType": "application/vnd.google-apps.presentation"},
        media_body=MediaIoBaseUpload(buf, mimetype=PPTX_MIME), fields="id"))["id"]
    pres = execute(slides.presentations().get(presentationId=pid))
    path = OUT / "calibrate_shadow.png"
    save_thumbnail(slides, pid, pres["slides"][0]["objectId"], path)
    pix = pymupdf.Pixmap(str(path))
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[..., :3].mean(axis=2)
    k = pix.width / 720
    print("dist blur alpha | darkness right of edge at +0,1,2,...,9 pt | below edge")
    for i, (dist, blur, alpha) in enumerate(combos):
        x, y = cell(i)
        right = [1 - img[int((y + H / 2) * k), int((x + W + t) * k)] / 255 for t in range(10)]
        below = [1 - img[int((y + H + t) * k), int((x + W / 2) * k)] / 255 for t in range(10)]
        left = [1 - img[int((y + H + 3) * k), int((x + t) * k)] / 255 for t in range(0, 20, 2)]
        print(f"{dist:4} {blur:4} {alpha:4} | " + " ".join(f"{v:.2f}" for v in right) + " | "
              + " ".join(f"{v:.2f}" for v in below) + " | left end " + " ".join(f"{v:.2f}" for v in left))
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
