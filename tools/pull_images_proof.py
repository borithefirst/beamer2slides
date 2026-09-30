"""The picture edits of the live pull proof, made on a converted deck through the API.

  python tools/pull_images_proof.py --folder out/agent-pull-images/talk

On the deck converted from tests/decks/sync/out/v1.tex it
  - inserts a 3000x2000 photo on the last slide, crops it (updateImageProperties) and turns it
    (a rotating transform),
  - replaces the TikZ plot of the `convergence` frame with a chart picture (replaceImage), and
  - adds a half-transparent PNG on the `steps` frame.
The Slides API refuses `transparency`, `brightness`, `contrast` and `recolor`
(tools/probe_images.py), so the half-transparent picture carries its alpha in its pixels.

The pictures come from a staging .pptx (as sync does): `createImage` needs a URL Google can
fetch, and the staging file is deleted again right after. Re-running replaces what it added.
"""

import argparse
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.probe_images import photo  # noqa: E402

from beamer2slides.deck_ir import TAG_RE, walk_elements  # noqa: E402
from beamer2slides.emit import PPTX_MIME, build_pptx  # noqa: E402
from beamer2slides.google_auth import credentials, drive_service, slides_service  # noqa: E402
from beamer2slides.google_types import (  # noqa: E402
    Dimension,
    DriveService,
    PageElement,
    SlidesService,
    file_id,
    object_id,
    part,
)
from beamer2slides.gslides import EMU_PER_PT, execute  # noqa: E402
from beamer2slides.json_types import as_object, as_str  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ADDED = ("b2s_proof_photo", "b2s_proof_alpha")
CROP = {"leftOffset": 0.15, "topOffset": 0.1, "rightOffset": 0.1, "bottomOffset": 0.25}
ROTATION = 12.0
HALF_SIZE = (600, 400)


def magnitude(d: Dimension, where: str) -> float:
    m = d.get("magnitude")
    if m is None:
        raise ValueError(f"Slides answered no magnitude for {where}")
    return m


def extent(pe: PageElement) -> tuple[float, float]:
    """A page element's unscaled width and height (EMU)."""
    size = pe.get("size")
    if size is None:
        raise ValueError(f"{object_id(pe)}: Slides answered no size")
    return magnitude(size.get("width", {}), "size.width"), magnitude(size.get("height", {}), "size.height")


def chart(path: Path, size: tuple[int, int]) -> Path:
    """A plot-like picture: the replacement for a TikZ figure."""
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    w, h = size
    d.line([(60, h - 60), (w - 30, h - 60)], fill=(30, 30, 30), width=4)
    d.line([(60, h - 60), (60, 30)], fill=(30, 30, 30), width=4)
    for k, v in enumerate((0.92, 0.61, 0.38, 0.22, 0.12, 0.05)):
        x = 90 + k * (w - 160) / 6
        d.rectangle([x, h - 60 - v * (h - 120), x + (w - 160) / 9, h - 60], fill=(60, 110, 200))
    d.text((w - 260, 40), "conflicts per sync", fill=(20, 20, 20))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def half_transparent(path: Path, size: tuple[int, int]) -> Path:
    """A picture whose 50% transparency is in its pixels (the API cannot set the property)."""
    base = photo(*size).convert("RGBA")
    base.putalpha(128)
    path.parent.mkdir(parents=True, exist_ok=True)
    base.save(path)
    return path


def stage(drive: DriveService, slides: SlidesService, files: list[Path],
          page: tuple[float, float]) -> tuple[str, dict[str, str]]:
    # Resumable, unlike gapi.media_upload: the 3000x2000 photo takes the .pptx past the 5 MB a
    # simple upload carries.
    from googleapiclient.http import MediaIoBaseUpload

    pages: list[Mapping[str, object]] = [{"layout": "BLANK", "fill": None, "templates": False, "pictures": [
        {"file": f, "bbox": [0, 0, 100, 100 * Image.open(f).size[1] / Image.open(f).size[0]],
         "alt": f"b2s-stage:{k}", "title": "stage"} for k, f in enumerate(files)]}]
    pptx = build_pptx(page[0], page[1], [], pages, {"color": "#ffffff"}, None)
    fid = file_id(execute(drive.files().create(body={"name": "beamer2slides pull proof staging (temporary)",
                                                     "mimeType": "application/vnd.google-apps.presentation"},
                                               media_body=MediaIoBaseUpload(pptx, mimetype=PPTX_MIME, resumable=True),
                                               fields="id")), "the staging deck")
    staged = execute(slides.presentations().get(presentationId=fid))
    urls: dict[str, str] = {}
    for pe in staged.get("slides", [])[0].get("pageElements", []):
        d = pe.get("description") or ""
        if "image" in pe and d.startswith("b2s-stage:"):
            urls[files[int(d[10:])].name] = as_str(part(pe.get("image"), "image").get("contentUrl"), "image.contentUrl")
    return fid, urls


def element_properties(page_id: str, box: list[float]) -> dict[str, object]:
    x0, y0, x1, y1 = box
    return {"pageObjectId": page_id, "size": {"width": {"magnitude": (x1 - x0) * EMU_PER_PT, "unit": "EMU"},
                                              "height": {"magnitude": (y1 - y0) * EMU_PER_PT, "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x0 * EMU_PER_PT, "translateY": y0 * EMU_PER_PT,
                          "unit": "EMU"}}


def rotate_request(pe: PageElement, degrees: float) -> dict[str, object]:
    """A rotation about the picture's centre, as the Slides UI makes it."""
    tr = pe.get("transform")
    if tr is None:
        raise ValueError(f"{object_id(pe)}: Slides answered no transform")
    w, h = extent(pe)
    sx, sy = tr.get("scaleX", 1), tr.get("scaleY", 1)
    cx, cy = tr.get("translateX", 0) + sx * w / 2, tr.get("translateY", 0) + sy * h / 2
    th = math.radians(degrees)
    a, b, d, e = sx * math.cos(th), -sy * math.sin(th), sx * math.sin(th), sy * math.cos(th)
    return {"updatePageElementTransform": {"objectId": object_id(pe), "applyMode": "ABSOLUTE", "transform": {
        "scaleX": a, "shearX": b, "shearY": d, "scaleY": e, "unit": "EMU",
        "translateX": cx - (a * w / 2 + b * h / 2), "translateY": cy - (d * w / 2 + e * h / 2)}}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", type=Path, required=True, help="the convert output folder of the deck")
    args = ap.parse_args()
    folder = args.folder.resolve()
    emitted = folder / "emit.json"
    pid = as_str(as_object(json.loads(emitted.read_text(encoding="utf-8")), str(emitted))["presentationId"],
                 "emit.json presentationId")
    work = folder / "proof-images"
    work.mkdir(parents=True, exist_ok=True)
    creds = credentials()
    slides, drive = slides_service(creds), drive_service(creds)
    pres = execute(slides.presentations().get(presentationId=pid))

    keys: dict[str, str] = {}
    objects: dict[tuple[str, str], PageElement] = {}
    for s in pres.get("slides", []):
        tags: list[str] = []
        for pe in walk_elements(s.get("pageElements", [])):
            m = TAG_RE.match(pe.get("title") or "")
            if m:
                tags.append(m.group("slide"))
                objects[(m.group("slide"), m.group("element"))] = pe
        if tags:
            keys[max(set(tags), key=tags.count)] = object_id(s)
    figure = next(pe for (skey, ekey), pe in objects.items() if skey == "convergence" and ekey.startswith("image/"))
    figure_tr = figure.get("transform")
    if figure_tr is None:
        raise ValueError(f"{object_id(figure)}: Slides answered no transform")
    figure_w, figure_h = extent(figure)
    fw = figure_w * figure_tr.get("scaleX", 1) / EMU_PER_PT
    fh = figure_h * figure_tr.get("scaleY", 1) / EMU_PER_PT

    photo(3000, 2000).save(work / "photo.png")
    chart(work / "chart.png", (round(fw * 6), round(fh * 6)))  # the figure's aspect: no letterboxing
    half_transparent(work / "half.png", HALF_SIZE)
    files = [work / "photo.png", work / "chart.png", work / "half.png"]

    page_size = pres.get("pageSize", {})
    page = (magnitude(page_size.get("width", {}), "pageSize.width") / EMU_PER_PT,
            magnitude(page_size.get("height", {}), "pageSize.height") / EMU_PER_PT)
    fid, urls = stage(drive, slides, files, page)
    try:
        last = object_id(pres.get("slides", [])[-1])
        steps = keys["steps"]
        old = {object_id(pe) for s in pres.get("slides", []) for pe in s.get("pageElements", [])} & set(ADDED)
        reqs: list[Mapping[str, object]] = [{"deleteObject": {"objectId": o}} for o in sorted(old)]
        reqs += [
            {"createImage": {"objectId": ADDED[0], "url": urls["photo.png"],
                             "elementProperties": element_properties(last, [360, 120, 560, 253])}},
            {"createImage": {"objectId": ADDED[1], "url": urls["half.png"],
                             "elementProperties": element_properties(steps, [420, 150, 600, 270])}},
            {"replaceImage": {"imageObjectId": object_id(figure), "url": urls["chart.png"],
                              "imageReplaceMethod": "CENTER_INSIDE"}},
            {"updateImageProperties": {"objectId": ADDED[0], "imageProperties": {"cropProperties": CROP},
                                       "fields": "cropProperties"}},
        ]
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
        last_page = execute(slides.presentations().pages().get(presentationId=pid, pageObjectId=last))
        pe = next(pe for pe in last_page.get("pageElements", []) if object_id(pe) == ADDED[0])
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [rotate_request(pe, ROTATION)]}))
    finally:
        execute(drive.files().delete(fileId=fid))
    print(f"photo inserted, cropped {CROP} and turned {ROTATION} degrees; the convergence figure replaced by a chart; "
          f"a half-transparent picture added on the steps slide")
    print(f"https://docs.google.com/presentation/d/{pid}/edit")


if __name__ == "__main__":
    main()
