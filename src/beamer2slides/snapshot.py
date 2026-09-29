"""The sync base (docs/sync.md): the converter's IR plus Google's read-back of every object it
created, recorded right after the deck was written, in `<out>/sync/base.json` and in Drive."""

import contextlib
import copy
import io
import json
import os
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

from . import identity
from .emit import background_key as emit_background_key, slide_layout
from .deck_pictures import WORKERS as PICTURE_WORKERS, LivePictures
from .gapi import HttpError
from .google_types import (AffineTransform, DriveFile, DriveService, FileBody, LayoutProperties, Page, PageElement,
                           Presentation, Size, SlideProperties, all_elements, background_fill, background_url,
                           children, file_id, image_url, object_id, part, parts, presentation_id)
from .gslides import EMU_PER_PT, execute
from .ir_types import IRError, element_json, parse_element, parse_rendered_element
from .json_types import (Json, JsonObject, JsonShapeError, as_int, as_object, as_objects, as_optional_str,
                         as_str)
from .net import Fetch
from .typing_compat import assert_never

VERSION = 1
TAG_PREFIX = "b2s:"
TEXT_STYLE_KEYS = ("fontFamily", "bold", "italic", "underline", "strikethrough", "smallCaps", "baselineOffset")
PARAGRAPH_KEYS = ("alignment", "lineSpacing", "direction")
GEOMETRY_TOLERANCE = 0.05  # pt


# ---------------------------------------------------------------- read-back

def _unit(v: Mapping[str, object] | None) -> float:
    """A Dimension in pt: a typed one (`google_types.Dimension`) or one read out of the JSON a shape's
    text and properties still are."""
    if not v:
        return 0.0
    magnitude = v.get("magnitude", 0.0)
    if not isinstance(magnitude, (int, float)):
        raise JsonShapeError(f"a dimension's magnitude: a number was expected, found {type(magnitude).__name__}")
    return magnitude / (EMU_PER_PT if v.get("unit", "EMU") == "EMU" else 1.0)


def colour(c: dict | None) -> str | None:
    """An OpaqueColor / OptionalColor as '#rrggbb' or 'theme:NAME'."""
    if not c:
        return None
    c = c.get("opaqueColor", c)
    if "themeColor" in c:
        return f"theme:{c['themeColor']}"
    if "rgbColor" in c:
        rgb = c["rgbColor"]
        return "#" + "".join(f"{round(rgb.get(k, 0.0) * 255):02x}" for k in ("red", "green", "blue"))
    return None


def matrix(t: AffineTransform | None) -> list[float]:
    """[scaleX, shearX, shearY, scaleY, translateX pt, translateY pt]."""
    if not t:
        return [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    div = EMU_PER_PT if t.get("unit", "EMU") == "EMU" else 1.0  # (omitted fields are 0, as in the API)
    return [t.get("scaleX", 0.0), t.get("shearX", 0.0), t.get("shearY", 0.0), t.get("scaleY", 0.0),
            t.get("translateX", 0.0) / div, t.get("translateY", 0.0) / div]


def compose(p: list[float], c: list[float]) -> list[float]:
    """p after c."""
    pa, pb, pc, pd, px, py = p
    ca, cb, cc, cd, cx, cy = c
    return [pa * ca + pb * cc, pa * cb + pb * cd, pc * ca + pd * cc, pc * cb + pd * cd,
            pa * cx + pb * cy + px, pc * cx + pd * cy + py]


def invert(m: list[float]) -> list[float]:
    a, b, c, d, x, y = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return [1.0, 0.0, 0.0, 1.0, -x, -y]
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return [ia, ib, ic, id_, -(ia * x + ib * y), -(ic * x + id_ * y)]


def box(m: list[float], w: float, h: float) -> list[float]:
    pts = [(m[0] * x + m[1] * y + m[4], m[2] * x + m[3] * y + m[5]) for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    return [round(min(p[0] for p in pts), 2), round(min(p[1] for p in pts), 2),
            round(max(p[0] for p in pts), 2), round(max(p[1] for p in pts), 2)]


def _text_style(style: dict) -> dict:
    out = {k: style[k] for k in TEXT_STYLE_KEYS if k in style}
    if "weightedFontFamily" in style:
        out["fontFamily"] = style["weightedFontFamily"].get("fontFamily")
        out["weight"] = style["weightedFontFamily"].get("weight")
    if "fontSize" in style:
        out["fontSize"] = round(_unit(style["fontSize"]), 2)
    for k in ("foregroundColor", "backgroundColor"):
        if style.get(k):
            out[k] = colour(style[k])
    link = style.get("link")
    if link:
        out["link"] = link.get("url") or link.get("pageObjectId") or link.get("relativeLink") or str(link.get("slideIndex"))
    return out


def _paragraph_style(marker: dict) -> dict:
    style = marker.get("style", {})
    out = {k: style[k] for k in PARAGRAPH_KEYS if k in style}
    for k in ("indentStart", "indentFirstLine", "spaceAbove", "spaceBelow"):
        if k in style:
            out[k] = round(_unit(style[k]), 2)
    if "bullet" in marker:
        out["bullet"] = [marker["bullet"].get("glyph"), marker["bullet"].get("nestingLevel", 0)]
    return out


def read_text(text: dict | None) -> tuple[str, list[dict], list[dict], list[list]]:
    """(content, distinct run styles, distinct paragraph styles, run spans) of a shape's or cell's
    text. A span is `[start, end, style]` in characters of the content, which is what says *which*
    words a style is on - the distinct styles alone cannot (`merge.styling_lost`)."""
    content, runs, paras, spans = [], [], [], []
    at = 0
    for te in (text or {}).get("textElements", []):
        if "textRun" in te:
            piece = te["textRun"].get("content", "")
            content.append(piece)
            s = _text_style(te["textRun"].get("style", {}))
            if piece.strip("\n"):
                spans.append([at, at + len(piece.rstrip("\n")), s])
                if s not in runs:
                    runs.append(s)
            at += len(piece)
        elif "autoText" in te:
            content.append(te["autoText"].get("content", ""))
            at += len(te["autoText"].get("content", ""))
        elif "paragraphMarker" in te:
            s = _paragraph_style(te["paragraphMarker"])
            if s not in paras:
                paras.append(s)
    return "".join(content), runs, paras, spans


def _fill(fill: dict | None) -> dict | None:
    if not fill:
        return None
    if "solidFill" in fill:
        return {"color": colour(fill["solidFill"].get("color")), "alpha": round(fill["solidFill"].get("alpha", 1.0), 3)}
    return {"state": fill.get("propertyState", "RENDERED")}


def _outline(o: dict | None) -> dict | None:
    if not o:
        return None
    return {"fill": _fill(o.get("outlineFill")), "weight": round(_unit(o.get("weight")), 2),
            "dash": o.get("dashStyle"), "state": o.get("propertyState", "RENDERED")}


def shape_style(e: PageElement) -> dict:
    shape, line, image = e.get("shape"), e.get("line"), e.get("image")
    if shape is not None:
        props = part(shape.get("shapeProperties"), "shape.shapeProperties")
        return {"type": shape.get("shapeType"), "fill": _fill(part(props.get("shapeBackgroundFill"), "shapeBackgroundFill")),
                "outline": _outline(part(props.get("outline"), "shapeProperties.outline")),
                "align": props.get("contentAlignment")}
    if line is not None:
        props = part(line.get("lineProperties"), "line.lineProperties")
        return {"line": line.get("lineType"), "fill": _fill(part(props.get("lineFill"), "lineFill")),
                "weight": round(_unit(part(props.get("weight"), "lineProperties.weight")), 2),
                "dash": props.get("dashStyle"), "arrows": [props.get("startArrow"), props.get("endArrow")]}
    if image is not None:
        props = part(image.get("imageProperties"), "image.imageProperties")
        return {"outline": _outline(part(props.get("outline"), "imageProperties.outline"))}
    return {}


def image_hash(url: str | None) -> str | None:
    """A hash of a contentUrl. Google hands out new contentUrls for the same picture now and then,
    so equal hashes mean the same picture but different ones don't mean a replaced picture: see
    `signature` / `same_picture`."""
    return identity.sha1(re.split(r"[=?]", url, maxsplit=1)[0])[:16] if url else None


SIGNATURE_SIZE = 32
SIGNATURE_DIFFERENCE = 0.3  # ink-normalised difference below which two signatures are the same picture


def signature(data: bytes) -> str | None:
    """A small fingerprint of a picture's pixels ("<w>x<h>:<32x32 grey levels hex>"), stable across
    Google's re-encodings and new contentUrls."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as img:
            rgba = img.convert("RGBA")
    except Exception:  # noqa: BLE001 (not a picture)
        return None
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    grey = Image.alpha_composite(white, rgba).convert("L").resize((SIGNATURE_SIZE, SIGNATURE_SIZE), Image.BOX)
    return f"{rgba.size[0]}x{rgba.size[1]}:{grey.tobytes().hex()}"


def signatures_match(a: str | None, b: str | None) -> bool | None:
    """Whether two signatures show the same picture (None: one is missing)."""
    if not a or not b:
        return None
    (size_a, pixels_a), (size_b, pixels_b) = a.split(":", 1), b.split(":", 1)
    ra, rb = (int(w) / max(1, int(h)) for w, _, h in (size_a.partition("x"), size_b.partition("x")))
    if abs(ra - rb) > 0.05 * max(ra, rb):
        return False
    pa, pb = bytes.fromhex(pixels_a), bytes.fromhex(pixels_b)
    diff = sum(abs(x - y) for x, y in zip(pa, pb))
    ink = max(sum(255 - x for x in pa), sum(255 - x for x in pb), 255 * 2)
    return diff / ink < SIGNATURE_DIFFERENCE


def same_picture(a: dict | None, b: dict | None) -> bool:
    """Image read-backs ({"contentHash", "signature"?}) or picture backgrounds ({"picture", "signature"?}):
    the same picture if the URL hash is the same, or else the pixel signatures match."""
    a, b = a or {}, b or {}
    ha, hb = a.get("contentHash", a.get("picture")), b.get("contentHash", b.get("picture"))
    if ha == hb:
        return True
    return bool(signatures_match(a.get("signature"), b.get("signature")))


def same_background(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a == b
    if "picture" in a and "picture" in b:
        return same_picture(a, b)
    return a == b


ASPECT_AGREES = 0.01  # a live picture has its file's shape when the aspects are this close


def local_signature(path: "Path | str", shape: tuple[float, float] | None) -> str | None:
    """The signature of a picture this run has just put into the deck, from the file it uploaded,
    so nothing has to be downloaded to record it. Google serves what it was given, re-encoded:
    measured on two converted decks (probe of 2026-09-24), every one of 22 uploaded pictures signed
    alike from its file and from its download. That holds while the live picture has the file's
    shape - `shape` is the element's own size (a stretch is the transform's) or the page's for a
    background - and a picture Google may have resampled to another is left to be read (None)."""
    from PIL import Image
    try:
        data = Path(path).read_bytes()
        with Image.open(io.BytesIO(data)) as img:
            fw, fh = img.size
    except (OSError, ValueError):
        return None
    if not shape or shape[1] <= 0 or fh <= 0:
        return None
    a, b = fw / fh, shape[0] / shape[1]
    if abs(a - b) > ASPECT_AGREES * max(a, b):
        return None
    return signature(data)


def upload_signatures(pres: Presentation, files: Mapping[str, "Path | str"]) -> dict[str, str]:
    """`local_signature` of each picture of `pres` whose file is known: `files` maps an image's
    objectId, or a slide's for its background picture, to the file that was uploaded for it."""
    raw = {object_id(e): e for s in pres.get("slides", [])
           for e in all_elements(s.get("pageElements", []), object_id(s))}
    page: tuple[float, float] | None = None
    if pres.get("pageSize"):
        width, height = page_size(pres)
        page = (width, height)
    slides = {object_id(s) for s in pres.get("slides", [])}
    out: dict[str, str] = {}
    for oid, path in files.items():
        e = raw.get(oid)
        if oid in slides:
            shape = page
        elif e is not None and "image" in e:
            size = e.get("size") or Size()
            shape = (_unit(size.get("width")), _unit(size.get("height")))
        else:
            continue
        sig = local_signature(path, shape)
        if sig:
            out[oid] = sig
    return out


def converted_files(deck: dict, out: Path, state: dict, pres: Presentation) -> dict[str, Path]:
    """What emit uploaded for each picture of the deck it just made: an image element's file, a
    slide's own background picture (`upload_signatures`' `files`)."""
    images = picture_urls(pres)[0]
    files: dict[str, Path] = {}
    for slide, s in zip(deck.get("slides", []), state.get("slides", [])):
        objects = s.get("objects") or [[o] for o in s["elements"]]
        for el, oids in zip(slide["elements"], objects):
            if el.get("kind") == "image" and el.get("file"):
                for oid in [o for o in oids if o in images][:1]:
                    files[oid] = out / el["file"]
        if s.get("objectId") and slide.get("background") and not slide.get("background_color"):
            files[s["objectId"]] = out / slide["background"]
    return files


def _download(url: str, fetch=None) -> bytes | None:
    """A picture's bytes, or None: a picture that cannot be downloaded stays unsigned, and is
    compared by its URL alone (`same_picture`). `fetch`: see `net.download`."""
    from . import net
    try:
        return net.download(url, fetch, tries=3)
    except Exception:  # noqa: BLE001 - a harness's fetcher raises its own types
        return None


def picture_urls(pres: Presentation) -> tuple[dict[str, str], dict[str, str]]:
    """(image objectId -> contentUrl, slide objectId -> background picture contentUrl) of a presentations.get."""
    images: dict[str, str] = {}
    backgrounds: dict[str, str] = {}
    for s in pres.get("slides", []):
        if url := background_url(s):
            backgrounds[object_id(s)] = url
        for e in all_elements(s.get("pageElements", []), object_id(s)):
            if url := image_url(e):
                images[object_id(e)] = url
    return images, backgrounds


def _fetcher(fetch: Fetch | None) -> Fetch:
    """The fetcher a pool's workers are handed: resolved here, on the calling thread."""
    if fetch is not None:
        return fetch
    from .google_auth import fetcher_for_threads
    return fetcher_for_threads()


def picture_signatures(pres: Presentation, workers: int, fetch: Fetch | None, skip: Collection[str],
                       drive: DriveService | None) -> dict[str, str]:
    """Every picture of a presentations.get's slides signed by its pixels, by the id that owns it
    (an image's own objectId, a slide's own for its background picture); `skip`: ids already
    signed (`upload_signatures`).

    Downloading them costs about as much as a round trip, and a read's contentUrls stay good while
    the deck is being tagged, so `snapshot_after_convert` starts this and writes the tags meanwhile
    (`sign_pictures`' `ready`). `fetch`: what downloads them (`net`); pass it when this runs on a
    worker thread, which inherits no context. `drive`: where what was not downloaded is exported
    from (`deck_pictures.LivePictures`) - on the calling thread only, so None on a worker."""
    images, backgrounds = picture_urls(pres)
    ids = [i for i in {**images, **backgrounds} if i not in set(skip)]
    if not ids:
        return {}
    got = LivePictures(pres, drive, _fetcher(fetch), workers, None, None).get(ids)
    return {i: sig for i, d in got.items() if (sig := signature(d))}


def sign_pictures(read: dict, pres: Presentation, objects: Collection[str] | None, slides: Collection[str] | None,
                  workers: int, ready: Mapping[str, str] | None, fetch: Fetch | None, drive: DriveService | None,
                  files: Mapping[str, "Path | str"] | None, pictures: LivePictures | None) -> int:
    """Adds pixel signatures to the image read-backs and picture backgrounds of `read`
    (read_presentation of `pres`); `objects` / `slides`: only these ids (None: all). Returns how
    many pictures were signed or asked for. `ready`: signatures somebody has already made
    (`picture_signatures`), so nothing is fetched here. `files`: the files this run uploaded for
    some of them, signed from their bytes where the shape allows (`upload_signatures`); the rest are
    read through `pictures` (a `deck_pictures.LivePictures` of `pres`, made from `fetch` and `drive`
    when not given): downloaded, else exported. A picture signed neither way stays unsigned. An
    image read-back signed here loses the `unchecked` mark `Sync.sign_changed` gave it."""
    images, backgrounds = picture_urls(pres)
    jobs = []
    for s in read["slides"]:
        bg = s.get("background") or {}
        if "picture" in bg and s["objectId"] in backgrounds and (slides is None or s["objectId"] in slides):
            jobs.append((s["objectId"], bg))
        for oid, rb in s["objects"].items():
            if "image" in rb and oid in images and (objects is None or oid in objects):
                jobs.append((oid, rb["image"]))
    if not jobs:
        return 0

    def put(target: dict, sig: str) -> None:
        target["signature"] = sig
        target.pop("unchecked", None)

    if ready is not None:
        for oid, target in jobs:
            if oid in ready:
                put(target, ready[oid])
        return len(jobs)
    local = upload_signatures(pres, {oid: f for oid, f in (files or {}).items()
                                     if oid in {j[0] for j in jobs}}) if files else {}
    rest = [oid for oid, _ in jobs if oid not in local]
    if rest:
        if pictures is None:
            pictures = LivePictures(pres, drive, _fetcher(fetch), workers, None, None)
        got = pictures.get(rest)
        local.update({i: sig for i, d in got.items() if (sig := signature(d))})
    for oid, target in jobs:
        if oid in local:
            put(target, local[oid])
    return len(jobs)


def readback(e: PageElement, parent: list[float], parent_group: str | None, z: int) -> dict:
    """Normalised read-back of one page element (pt, hex colours, absolute transform)."""
    m = compose(parent, matrix(e.get("transform")))
    size = e.get("size") or Size()
    w, h = _unit(size.get("width")), _unit(size.get("height"))
    out: dict[str, object] = {
        "kind": next((k for k in ("shape", "image", "line", "table", "elementGroup", "sheetsChart", "video", "wordArt")
                      if k in e), "other"),
        "transform": [round(v, 4) for v in m[:4]] + [round(v, 2) for v in m[4:]], "size": [round(w, 2), round(h, 2)],
        "box": box(m, w, h), "parent_group": parent_group, "z": z, "title": e.get("title"),
        "description": e.get("description")}
    text, runs, paras, spans = None, [], [], []
    shape, table, image = e.get("shape"), e.get("table"), e.get("image")
    if shape is not None:
        text, runs, paras, spans = read_text(part(shape.get("text"), "shape.text"))
        if "placeholder" in shape:
            out["placeholder"] = part(shape["placeholder"], "shape.placeholder").get("type")
    elif table is not None:
        rows, at = [], 0
        for row in parts(table.get("tableRows"), "table.tableRows"):
            cells = []
            for cell in parts(row.get("tableCells"), "tableRows.tableCells"):
                t, r, p, s = read_text(part(cell.get("text"), "tableCells.text"))
                cells.append(t.rstrip("\n"))
                runs += [x for x in r if x not in runs]
                paras += [x for x in p if x not in paras]
                # the cells are joined below, so the spans move with their cell into that text
                spans += [[a + at, min(b + at, at + len(cells[-1])), st] for a, b, st in s if a < len(cells[-1])]
                at += len(cells[-1]) + 1                       # the tab (or, after the last cell, the newline)
            rows.append("\t".join(cells))
        text = "\n".join(rows)
        out["table"] = [table.get("rows"), table.get("columns")]
    out["text"] = text
    out["text_styles"] = runs
    out["paragraph_styles"] = paras
    out["run_spans"] = spans
    out["text_style_hash"] = identity.sha1(json.dumps([sorted(json.dumps(s, sort_keys=True) for s in runs),
                                                       sorted(json.dumps(s, sort_keys=True) for s in paras)]))[:12]
    style = shape_style(e)
    out["shape_style"] = style
    out["shape_style_hash"] = identity.sha1(json.dumps(style, sort_keys=True))[:12]
    if image is not None:
        out["image"] = {"contentHash": image_hash(as_optional_str(image.get("contentUrl"), "image.contentUrl")),
                        "sourceUrl": image.get("sourceUrl")}
    return out


def background(page: Page) -> dict:
    fill = background_fill(page)
    if "stretchedPictureFill" in fill:
        return {"picture": image_hash(background_url(page))}
    if "solidFill" in fill:
        return {"color": colour(part(part(fill["solidFill"], "solidFill").get("color"), "solidFill.color"))}
    return {"state": fill.get("propertyState", "INHERIT")}


def read_slide(slide: Page) -> dict:
    """A slide as sync compares it: {objectId, layoutObjectId, background, notes, notes_id,
    order (top-level ids), objects {id: readback}}."""
    objects: dict[str, dict] = {}
    counter = [0]

    def walk(elements: list[PageElement], parent: list[float], group: str | None) -> None:
        for e in elements:
            oid = object_id(e)
            rb = readback(e, parent, group, counter[0])
            counter[0] += 1
            objects[oid] = rb
            if "elementGroup" in e:
                kids = children(e, oid)
                walk(kids, compose(parent, matrix(e.get("transform"))), oid)
                boxes = [objects[object_id(c)]["box"] for c in kids]
                if boxes:
                    rb["box"] = [min(k[0] for k in boxes), min(k[1] for k in boxes),
                                 max(k[2] for k in boxes), max(k[3] for k in boxes)]
                rb["children"] = [object_id(c) for c in kids]

    walk(slide.get("pageElements", []), [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], None)
    props = slide.get("slideProperties") or SlideProperties()
    notes_page = props.get("notesPage") or Page()
    notes_id = as_optional_str(part(notes_page.get("notesProperties"), "notesProperties").get("speakerNotesObjectId"),
                               "notesProperties.speakerNotesObjectId")
    notes = ""
    for e in notes_page.get("pageElements", []):
        if e.get("objectId") == notes_id:
            notes = read_text(part(part(e.get("shape"), "shape").get("text"), "shape.text"))[0]
    return {"objectId": object_id(slide), "layoutObjectId": props.get("layoutObjectId"),
            "background": background(slide), "notes": notes.rstrip("\n"), "notes_id": notes_id,
            "order": [object_id(e) for e in slide.get("pageElements", [])], "objects": objects}


def page_size(pres: Presentation) -> list[float]:
    """The presentation's page, in pt. Every box a sync writes is in these points, and a deck a
    person built is whatever size they made it (`adopt_sync`)."""
    size = pres.get("pageSize")
    if size is None:
        raise JsonShapeError("the presentation has no pageSize (a fields= mask that left it out?)")
    return [_unit(size.get("width")), _unit(size.get("height"))]


def read_presentation(pres: Presentation) -> dict:
    layouts = {object_id(l): (l.get("layoutProperties") or LayoutProperties()).get("name") for l in pres.get("layouts", [])}
    masters = pres.get("masters", [])
    return {"presentationId": presentation_id(pres), "revisionId": pres.get("revisionId"),
            "page_size": page_size(pres),
            "layouts": layouts, "master_background": background(masters[0]) if masters else None,
            "slides": [read_slide(s) for s in pres.get("slides", [])]}


# ---------------------------------------------------------------- base

def source_info(pdf: "Path | str | dict | None") -> dict:
    """What the base records about the PDF a deck was converted from: where it was, and what it
    said. A mapping is taken as those two facts already measured, which is how the upload half of
    a split conversion (`agent.deck_tools.deck_upload`) records the same base without the PDF's
    bytes having to cross into the workspace that does the upload - the name is all
    `guard.check_rebuild` reads, and the digest is measured where the file is."""
    if isinstance(pdf, dict):
        return {"pdf": str(pdf.get("pdf") or ""), "sha1": pdf.get("sha1")}
    if pdf is None:
        return {"pdf": "", "sha1": None}
    pdf = Path(pdf)
    return {"pdf": str(pdf), "sha1": identity.sha1(pdf.read_bytes()) if pdf.exists() else None}


def background_key(slide: dict, out: Path) -> str:
    kind, value = emit_background_key(slide, out)
    return f"{kind}:{value}"


PICTURE_FLAT = 6     # levels: the ground the older picture painted in counts as one colour
PICTURE_MATCH = 16   # levels: the new picture laid on that ground shows the older one (render.clear_ground)


def same_picture_file(a: Path, b: Path) -> bool:
    """`a` (the file the base recorded) and `b` (the new one) put the same thing on the page: the
    same pixels where `b` is opaque, over one flat colour where it is transparent - the ground the
    older picture had painted in (render.clear_ground). Anchored pictures became RGBA on a
    transparent ground, so without this every one of them would be rewritten on the first sync
    after that change although the slides look the same."""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(a) as ia, Image.open(b) as ib:
            if ia.size != ib.size:
                return False
            old = np.asarray(ia.convert("RGB")).astype(float)
            new = np.asarray(ib.convert("RGBA")).astype(float)
    except (OSError, ValueError):
        return False
    ground = np.array([255.0, 255.0, 255.0])
    clear = new[..., 3] == 0
    if clear.any():
        under = old[clear]
        ground = np.median(under, axis=0)
        if (np.abs(under - ground).max(axis=1) > PICTURE_FLAT).mean() > 0.002:
            return False  # (the older picture had something else under the new one's clear ground)
    alpha = new[..., 3:] / 255
    laid = new[..., :3] * alpha + ground * (1 - alpha)
    return bool((np.abs(laid - old).max(axis=2) > PICTURE_MATCH).mean() <= 0.002)


# ---------------------------------------------------------------- where a base's pictures are

def sync_work(out: Path) -> Path:
    """Where a sync of the deck kept in `out` converts the new PDF (`sync.build_ours`). Every sync
    renders into it again, under the same names, over the last one's files."""
    return out / "sync" / "ours"


def held_pictures(out: Path) -> Path:
    """Where a sync keeps the files its base's pictures were hashed from, one folder per hash, out
    of the next render's way (`hold_base_pictures`)."""
    return out / "sync" / "base-pictures"


PictureKey = tuple[str, str]
"""A base picture as its base knows it: the first 12 hex of its file's sha1 (`fields.image`) and
the file's name (`ir.file`)."""


@dataclass(frozen=True, kw_only=True)
class PictureFolders:
    """Where a deck's folder keeps the picture files its bases name. `kept`: folders whose files
    stay as long as a base naming them lives (`convert`'s out folder, adopt's conversion of the
    source); `rendered`: the last sync's conversion, written over by the next sync's render;
    `held`: what a sync held out of that render's way (`hold_base_pictures`)."""
    kept: tuple[Path, ...]
    rendered: Path | None
    held: Path | None


def picture_folders(out: Path) -> PictureFolders:
    """The one answer to where the pictures of a base kept in `out` may be. A base `convert` wrote
    names its files in `out`; one `adopt` recorded, in `out/sync-base/ours` (`adopt_sync.record`
    under `adopt`'s `sync-base`); one a sync wrote, in that sync's `sync_work`, and what it kept of
    older bases wherever those had them."""
    return PictureFolders(kept=(out, out / "sync-base" / "ours"), rendered=sync_work(out), held=held_pictures(out))


def picture_key(e: JsonObject) -> PictureKey | None:
    """(`fields.image`, `ir.file`) of a base element whose hash read a picture file; None for any
    other (not a picture, no file, or none there when it was hashed)."""
    ir, fields = e.get("ir"), e.get("fields")
    if not isinstance(ir, dict) or ir.get("kind") != "image" or not isinstance(fields, dict):
        return None
    file, image = ir.get("file"), fields.get("image")
    if not isinstance(file, str) or not file or not isinstance(image, str) or not image:
        return None
    return image, file


@dataclass(frozen=True, kw_only=True)
class BasePictures:
    """Where the file each of a base's pictures was hashed from is now, found by its bytes. A name
    alone does not say: a sync renders the new PDF into the same folder under the same names, so
    by the next sync another picture stands under the name the base recorded, and the older one of
    that name is in the out folder `convert` wrote. Read by name, a figure the source changed and
    then changed back was "the same picture written differently" (`refresh_pictures`): the base
    took the new hash, the deck kept the changed figure, and no later sync would put it right."""
    folders: Mapping[PictureKey, Path]

    def folder(self, e: JsonObject) -> Path | None:
        """The folder holding `e`'s file (a base element) with the bytes its base hashed, or None
        when no folder given has them."""
        key = picture_key(e)
        return None if key is None else self.folders.get(key)


NO_PICTURES = BasePictures(folders={})


def _base_picture_keys(base: JsonObject) -> list[PictureKey]:
    keys: list[PictureKey] = []
    for s in as_objects(base.get("slides", []), "base.slides"):
        for e in as_objects(s.get("elements", []), "base slide elements"):
            key = picture_key(e)
            if key is not None and key not in keys:
                keys.append(key)
    return keys


def find_base_pictures(base: JsonObject, where: PictureFolders) -> BasePictures:
    """Each picture of `base` in the first folder of `where` whose file of its name has the bytes the
    base hashed (`BasePictures`); one found nowhere is left out."""
    sums: dict[Path, str | None] = {}

    def holds(folder: Path, key: PictureKey) -> bool:
        path = folder / key[1]
        if path not in sums:
            sums[path] = identity.sha1(path.read_bytes()) if path.is_file() else None
        digest = sums[path]
        return digest is not None and digest.startswith(key[0])

    found: dict[PictureKey, Path] = {}
    for key in _base_picture_keys(base):
        folders = [*where.kept, *([where.held / key[0]] if where.held is not None else []),
                   *([where.rendered] if where.rendered is not None else [])]
        at = next((f for f in folders if holds(f, key)), None)
        if at is not None:
            found[key] = at
    return BasePictures(folders=found)


def hold_base_pictures(base: JsonObject, out: Path) -> BasePictures:
    """`find_base_pictures` over `picture_folders(out)`, and every file found where the next render
    writes (`sync_work`) copied first into `held_pictures(out)`, by hash; held files no picture of
    `base` needs any more go. Called before a sync renders the new PDF: a base a sync wrote names the
    files that sync rendered, and this sync's render is about to write others over them."""
    import shutil

    where = picture_folders(out)
    found = find_base_pictures(base, where)
    held = where.held
    if held is None:
        return found
    folders: dict[PictureKey, Path] = {}
    needed: set[str] = set()
    for key, folder in found.folders.items():
        if folder in where.kept:
            folders[key] = folder
            continue
        image, file = key
        target = held / image
        if folder != target:
            (target / file).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(folder / file, target / file)
        folders[key] = target
        needed.add(image)
    if held.is_dir():
        for sub in held.iterdir():
            if sub.name not in needed:
                shutil.rmtree(sub, ignore_errors=True)
    return BasePictures(folders=folders)


@dataclass(frozen=True, kw_only=True)
class Refreshed:
    """A base picture whose file changed but which puts the same thing on the page
    (`refresh_pictures`): the base took the new hash."""
    slide: str
    element: str


def refresh_pictures(base: JsonObject, ours_slides: Sequence[JsonObject], pairs: Mapping[int, int], ours_out: Path,
                     pictures: BasePictures) -> list[Refreshed]:
    """Pictures whose file changed but that look the same are not a source change: the base takes
    the new hash and the deck keeps its object, instead of every anchored picture being rewritten
    when the converter changes how it writes them. `base` is updated in place. The base's side is
    the file its hash was made from (`pictures`), never whatever now has its name: a picture whose
    file is not found is left to read as changed, which rewrites it - never the other way round."""
    refreshed: list[Refreshed] = []
    base_slides = as_objects(base["slides"], "base.slides")
    for j, i in pairs.items():
        b, o = base_slides[i], ours_slides[j]
        slide = as_str(b["key"], "base slide key")
        ours_by = {as_str(e["key"], "element key"): e for e in as_objects(o["elements"], f"slide {slide}: elements")}
        for el in as_objects(b["elements"], f"base slide {slide}: elements"):
            key = as_str(el["key"], f"base slide {slide}: element key")
            oe = ours_by.get(key)
            if oe is None or el.get("kind") != "image":
                continue
            new = as_object(oe["ir"], f"slide {slide}, element {key}: ir").get("file")
            old = pictures.folder(el)
            if old is None or not isinstance(new, str) or not new:
                continue
            if identity.source_changes(el, oe) != {"image"}:
                continue
            if identity.normalise_ir(el["ir"], _anchor_key(el)) != identity.normalise_ir(oe["ir"], _anchor_key(oe)):
                continue  # (the IR changed in a way the field hashes don't see)
            if not same_picture_file(old / as_str(as_object(el["ir"], "ir")["file"], "ir.file"), ours_out / new):
                continue
            ours_fields = as_object(oe["fields"], f"slide {slide}, element {key}: fields")
            el["fields"] = {**as_object(el["fields"], f"base slide {slide}, element {key}: fields"),
                            "image": ours_fields["image"]}
            el["ir_hash"] = oe["ir_hash"]
            refreshed.append(Refreshed(slide=slide, element=key))
    return refreshed


def slide_entries(deck: dict, out: Path, keys: list[str], element_keys: list[list[str]], fingerprints: list[list[dict]]) -> list[dict]:
    """The IR part of base slides (keys, hashes, fingerprints, IR), without objects and read-back."""
    page_key = page_keys(deck, keys)
    entries = []
    for slide, key, ekeys, fps in zip(deck["slides"], keys, element_keys, fingerprints):
        ids = {e["id"]: k for e, k in zip(slide["elements"], ekeys)}
        elements = []
        for el, ek, fp in zip(slide["elements"], ekeys, fps):
            h, fields = identity.ir_fields(el, out, ids.get(el.get("anchor")), page_key)
            elements.append({"key": ek, "id": el["id"], "kind": el["kind"], "role": el.get("role"), "ir_hash": h,
                             "fields": fields, "fingerprint": fp, "anchor": ids.get(el.get("anchor")), "ir": el})
        entries.append({"key": key, "label": slide.get("label"), "title": identity.slide_title(slide), "page": slide["page"],
                        "text": identity.slide_text(slide), "layout": slide_layout(slide)[0],
                        "background": background_key(slide, out), "notes": slide.get("notes") or "",
                        "elements": elements})
    return entries


def page_keys(deck: JsonObject, keys: Sequence[str]) -> Callable[[int], str]:
    """PDF page -> slide key, for internal links (a skipped overlay step links to its kept step)."""
    slides = as_objects(deck["slides"], "deck.slides")
    return page_key_of([(as_int(s["page"], "slide.page"), k) for s, k in zip(slides, keys)])


def page_key_of(pages: Sequence[tuple[int, str]]) -> Callable[[int], str]:
    """`page_keys` over (page, slide key) pairs."""
    kept = sorted(pages)

    def key(page: int) -> str:
        return next((k for p, k in kept if p >= page), kept[-1][1] if kept else "")
    return key


def base_page_key(base: JsonObject) -> Callable[[int], str]:
    """`page_keys` over a base's own slides, as `slide_entries` hashed their links. A slide the
    source dropped is left out: its page is of an older PDF, and it was not in the conversion that
    hashed the others."""
    pages: list[tuple[int, str]] = []
    for s in as_objects(base.get("slides", []), "base.slides"):
        if not s.get("removed"):
            key = as_str(s["key"], "base slide key")
            pages.append((as_int(s["page"], f"base slide {key}: page"), key))
    return page_key_of(pages)


# ---------------------------------------------------------------- a base in today's form

RewriteHow = Literal["form", "adopt_shape", "adopt_table"]
"""How a base element was brought to today's form: `form`, its IR written as `ir_types` writes it
(`rehash_base`); `adopt_shape` / `adopt_table`, a marked shape or table an adopt base recorded
before 688ebf4 (`adopt_sync.upgrade_shapes` / `upgrade_tables`)."""


@dataclass(frozen=True, kw_only=True)
class Rewritten:
    """A base element whose `ir` is now in today's form. `hashed`: its hash changed with it (a
    difference the hash does not see leaves it as it was)."""
    slide: str
    element: str
    how: RewriteHow
    hashed: bool


@dataclass(frozen=True, kw_only=True)
class Unparsed:
    """A base element today's IR parser refuses (`error` says where): kept as recorded."""
    slide: str
    element: str
    error: str


@dataclass(frozen=True, kw_only=True)
class PictureGone:
    """A picture whose IR would be rewritten, but whose file is in none of the folders given: its
    hash holds the file's, which cannot be worked out again, so the element is kept as recorded."""
    slide: str
    element: str
    file: str


@dataclass(frozen=True, kw_only=True)
class Unreproduced:
    """An element whose IR would be rewritten, but whose recorded hash its recorded IR does not give
    here (a link hashed against other pages, a picture file written again since): kept as recorded,
    since a new hash from other inputs would say a change nobody made."""
    slide: str
    element: str


BaseForm = Union[Rewritten, Unparsed, PictureGone, Unreproduced]


def _dumped(value: Json) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _anchor_key(e: JsonObject) -> str | None:
    anchor = e.get("anchor")
    return anchor if isinstance(anchor, str) else None


def _read_ir(ir: JsonObject, where: str) -> JsonObject:
    """A base element's IR as `ir_types` writes it: emit's plan of one, or - where that is refused -
    a classified element, which an older adopt base holds for a marked shape emit cannot draw (a
    freeform kept a shape by `marked.shape_marks`, which today's plan makes its picture). Raises
    the rendered reading's error when neither reads it."""
    try:
        return element_json(parse_rendered_element(ir, where))
    except IRError as rendered:
        try:
            return element_json(parse_element(ir, where))
        except IRError:
            raise rendered from None


def rehash_base(base: JsonObject, pictures: BasePictures) -> list[BaseForm]:
    """In place: every base element's `ir` read through the IR parser (`ir_types`). Where what it
    writes back differs from what the base holds, the base takes it and its hash is worked out
    again - so a form the converter now writes differently (a key left out, a false flag) is a
    rewrite of the base, not a change of the source.

    The new hash is only made where the recorded IR still gives the recorded hash with the same
    inputs (the anchor's key, the base's pages, the picture's file); otherwise the element is kept
    and reported. `pictures`: where each base picture's file is, with the bytes its hash read
    (`find_base_pictures`). A difference the hash does not see needs no file. Returns what was
    found, element by element; nothing when the base is in today's form."""
    page_key = base_page_key(base)
    found: list[BaseForm] = []
    for s in as_objects(base.get("slides", []), "base.slides"):
        slide = as_str(s["key"], "base slide key")
        for e in as_objects(s.get("elements", []), f"base slide {slide}: elements"):
            if e.get("ir") is None:
                continue  # (a base older than the IR in it: nothing to read)
            element = as_str(e["key"], f"base slide {slide}: element key")
            ir = as_object(e["ir"], f"base slide {slide}, element {element}: ir")
            try:
                now = _read_ir(ir, f"base slide {slide}")
            except IRError as err:
                found.append(Unparsed(slide=slide, element=element, error=str(err)))
                continue
            if _dumped(now) == _dumped(ir):
                continue
            anchor = _anchor_key(e)
            if identity.ir_fields(ir, None, anchor, page_key)[0] == identity.ir_fields(now, None, anchor, page_key)[0]:
                e["ir"] = now  # (what changed is nothing the hash reads: ids, pictures' files, spans)
                found.append(Rewritten(slide=slide, element=element, how="form", hashed=False))
                continue
            folder: Path | None = None  # (no file, or none when it was hashed: the hash read none)
            if picture_key(e) is not None:
                folder = pictures.folder(e)
                if folder is None:
                    found.append(PictureGone(slide=slide, element=element, file=as_str(ir["file"], "ir.file")))
                    continue
            if identity.ir_fields(ir, folder, anchor, page_key)[0] != e.get("ir_hash"):
                found.append(Unreproduced(slide=slide, element=element))
                continue
            h, fields = identity.ir_fields(now, folder, anchor, page_key)
            # (the marks `sync.mark_emitted` gives stay: they are about the neighbours, not this IR)
            e.update(ir=now, ir_hash=h, fields={**as_object(e.get("fields") or {}, "base element fields"), **fields})
            found.append(Rewritten(slide=slide, element=element, how="form", hashed=True))
    return found


def base_form_warnings(found: Sequence[BaseForm]) -> list[str]:
    """The report's words for what `rehash_base` and the adopt upgrades found: nothing when the base
    was in today's form."""
    rewritten = [f for f in found if isinstance(f, Rewritten)]
    kept = [f for f in found if not isinstance(f, Rewritten)]
    says: list[str] = []
    if rewritten:
        says.append(f"{len(rewritten)} element(s) of the sync base were recorded in an older form of the converter's IR "
                    f"and were read in today's, so they are no change of the source: "
                    + ", ".join(f"{f.slide}/{f.element}" for f in rewritten[:8]) + (" ..." if len(rewritten) > 8 else ""))
    for f in kept:
        match f:
            case Unparsed():
                says.append(f"slide {f.slide}: the sync base records {f.element} in a form this version cannot read "
                            f"({f.error}); it is kept as recorded, and may read as changed by the source")
            case PictureGone():
                says.append(f"slide {f.slide}: the picture file {f.file} the sync base records for {f.element} is gone; "
                            f"it is kept as recorded, and may read as changed by the source")
            case Unreproduced():
                says.append(f"slide {f.slide}: the sync base's hash of {f.element} cannot be worked out again here; "
                            f"it is kept as recorded, and may read as changed by the source")
            case _:
                assert_never(f)
    return says


def base_form_json(found: Sequence[BaseForm]) -> list[JsonObject]:
    """`found` for the report, each with what happened (`outcome`)."""
    out: list[JsonObject] = []
    for f in found:
        match f:
            case Rewritten():
                out.append({"outcome": "rewritten", "slide": f.slide, "element": f.element, "how": f.how,
                            "hashed": f.hashed})
            case Unparsed():
                out.append({"outcome": "unparsed", "slide": f.slide, "element": f.element, "error": f.error})
            case PictureGone():
                out.append({"outcome": "picture_gone", "slide": f.slide, "element": f.element, "file": f.file})
            case Unreproduced():
                out.append({"outcome": "unreproduced", "slide": f.slide, "element": f.element})
            case _:
                assert_never(f)
    return out


def attach_readback(entry: dict, slide_read: dict | None, objects: list[list[str]], groups: list[str]) -> None:
    """Objects and read-back of one base slide."""
    entry["objectId"] = slide_read["objectId"] if slide_read else None
    entry["layoutObjectId"] = slide_read["layoutObjectId"] if slide_read else None
    entry["background_readback"] = slide_read["background"] if slide_read else None
    entry["notes_readback"] = slide_read["notes"] if slide_read else ""
    entry["groups"] = groups
    entry["order"] = slide_read["order"] if slide_read else []
    found = slide_read["objects"] if slide_read else {}
    for el, oids in zip(entry["elements"], objects):
        if slide_read and any(oid in found for oid in oids):
            # What emit meant to create, less what the deck does not have: a diagram of one node
            # gets no group (Slides groups two objects or more), and a group id the base names
            # but the deck never had reads as deleted to every later sync and rebuild guard.
            oids = [oid for oid in oids if oid in found]
        el["objects"] = oids
        el["main"] = oids[0] if oids else None
        el["readback"] = {oid: found[oid] for oid in oids if oid in found}


def build_base(deck: dict, out: Path, pres: Presentation, state: dict, pdf: "Path | dict", generation: int,
               sign: bool, overlays: str, signatures: Mapping[str, str] | None) -> dict:
    """The base after `convert`: `state` is emit's (slides with element object ids); `sign`:
    download the pictures for their signatures (`signatures`: unless these were downloaded
    already); `overlays`: which overlay steps the deck was made from, so a later sync uses the
    same ones (a sync with fewer would delete the deck's slides)."""
    infos = [identity.slide_info(s) for s in deck["slides"]]
    keys = identity.slide_keys(infos)
    ekeys, fps = zip(*[identity.slide_element_keys(s["elements"], out) for s in deck["slides"]]) if deck["slides"] else ((), ())
    entries = slide_entries(deck, out, keys, list(ekeys), list(fps))
    read = read_presentation(pres)
    if sign:
        sign_pictures(read, pres, objects=None, slides=None, workers=PICTURE_WORKERS, ready=signatures, fetch=None,
                      drive=None, files=None, pictures=None)
    by_id = {s["objectId"]: s for s in read["slides"]}
    for entry, s in zip(entries, state["slides"]):
        attach_readback(entry, by_id.get(s["objectId"]), s.get("objects") or [[o] for o in s["elements"]], s.get("groups", []))
        # The cell margins of a table the .pptx brought (emit.pptx_table), which the API can
        # neither read nor set: a sync refills such a table in place (sync.table_refill).
        for i, margins in (s.get("table_margins") or {}).items():
            entry["elements"][int(i)]["table_margins"] = [list(m) for m in margins]
    return {"version": VERSION, "generation": generation, "presentationId": read["presentationId"],
            "revisionId": read["revisionId"], "source": source_info(pdf), "overlays": overlays,
            "scale": state.get("scale"),
            "page_size": deck["slides"][0]["size"] if deck["slides"] else None, "deck_page_size": read["page_size"],
            "master_background": master_key(deck, out), "master_readback": read["master_background"],
            "slides": entries}


def master_key(deck: dict, out: Path) -> str | None:
    """The background emit put on the master (the most common one, if shared)."""
    from collections import Counter
    counts = Counter(background_key(s, out) for s in deck["slides"])
    if not counts:
        return None
    key, n = counts.most_common(1)[0]
    return key if n >= 2 else None


def tag(slide_key: str, element_key: str) -> str:
    return f"{TAG_PREFIX}{slide_key}/{element_key}"


def tag_requests(base: dict) -> list[dict]:
    """Alt-text titles naming each element's main object, so copies made in Slides are recognised."""
    reqs = []
    for s in base["slides"]:
        for el in s["elements"]:
            main = el.get("main")
            rb = el.get("readback", {}).get(main)
            if main and rb and rb.get("title") != tag(s["key"], el["key"]) and rb["kind"] != "elementGroup":
                reqs.append({"updatePageElementAltText": {"objectId": main, "title": tag(s["key"], el["key"])}})
    return reqs


def write_tags(slides, pid: str, reqs: list[dict]) -> tuple[list[dict], str | None]:
    """Sends tag requests; the ones the API refuses (some placeholders) are skipped. Returns the
    ones that landed and the deck's revision afterwards, which the batch's own answer says
    (`writeControl.requiredRevisionId`), so nothing has to read the deck again to learn it."""
    if not reqs:
        return [], None

    def revision(answer: dict) -> str | None:
        return (answer.get("writeControl") or {}).get("requiredRevisionId")

    try:
        return reqs, revision(execute(slides.presentations().batchUpdate(
            presentationId=pid, body={"requests": reqs})))
    except HttpError:
        sent, at = [], None
        for r in reqs:
            try:
                at = revision(execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [r]})))
                sent.append(r)
            except HttpError:
                pass
        return sent, at


def tagged(pres: Presentation, reqs: list[dict], revision: str | None) -> Presentation:
    """A presentations.get with the alt-text titles `reqs` have just written put into it, and the
    revision the batch answered with.

    The only news a second read of the deck would bring is exactly this - measured on the 48-slide
    ambiguous deck (181 tags): the base built from here is equal, field for field, to the base
    built from reading the deck again, whose revisionId is the one the batch already gave us. So
    the read is not made (it is most of a second, at the end of a conversion where nothing else is
    left to overlap it with)."""
    titles = {r["updatePageElementAltText"]["objectId"]: r["updatePageElementAltText"].get("title")
              for r in reqs if "updatePageElementAltText" in r}
    if not titles:
        return pres
    out = copy.deepcopy(pres)
    for page in out.get("slides", []):
        # (a group's children are the answer's own objects, checked where they are read: written here)
        for e in all_elements(page.get("pageElements", []), object_id(page)):
            if object_id(e) in titles:
                e["title"] = titles[object_id(e)]
    if revision:
        out["revisionId"] = revision
    return out


# ---------------------------------------------------------------- storage

BASE_PROPERTY = "b2sBase"
CLEANED_PROPERTY = "b2sCleaned"   # the generation whose `cleanup` list has been carried out


def local_path(out: Path) -> Path:
    return out / "sync" / "base.json"


def save_local(base: dict, out: Path) -> Path:
    """Written to a temporary file and moved into place: a process killed while the base is being
    written leaves the previous one, never half of a file (a truncated base is no base at all)."""
    path = local_path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing")
    tmp.write_text(json.dumps(base, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def base_problem(data, pid: str | None) -> str | None:
    """Why `data` cannot be used as the base of presentation `pid` (None: it can)."""
    if not isinstance(data, dict):
        return "not a JSON object"
    version = data.get("version")
    if not isinstance(version, int):
        return "no schema version"
    if version > VERSION:
        return f"schema version {version} is newer than this beamer2slides (up to {VERSION})"
    if not isinstance(data.get("slides"), list):
        return "no slides"
    if pid and data.get("presentationId") != pid:
        return f"it belongs to presentation {data.get('presentationId')}"
    return None


def read_local(out: Path | None, pid: str | None) -> tuple[dict | None, str | None]:
    """(base, why not) of the local cache: a missing, truncated or foreign file is no base."""
    if out is None or not local_path(out).exists():
        return None, None
    try:
        data = json.loads(local_path(out).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"{local_path(out)} could not be read ({type(e).__name__}: {e})"
    problem = base_problem(data, pid)
    return (None, f"{local_path(out)}: {problem}") if problem else (data, None)


DECK_FACTS = "name,parents,appProperties"


def deck_info(drive: DriveService, pid: str) -> DriveFile:
    """The presentation's name, parents and appProperties: everything `load_drive` and `save_drive`
    ask Drive before they can touch the base, and nothing a sync does changes it. A caller that
    reads it once and hands it on (`sync`) pays that round trip once instead of four times."""
    return execute(drive.files().get(fileId=pid, fields=DECK_FACTS))


def base_file(info: DriveFile) -> str | None:
    """The id of the base file the deck names in its appProperties (None: none yet)."""
    return (info.get("appProperties") or {}).get(BASE_PROPERTY) or None


def save_drive(drive: DriveService, base: dict, title: str | None, info: DriveFile | None) -> str:
    """The base as a JSON file next to the presentation (drive.file scope), its id in the
    presentation's appProperties.b2sBase. Returns the file id.

    `info`: the presentation's name, parents and appProperties, where a caller has already read
    them (`deck_info` - they need nothing but the id, so a caller may fetch them while it is doing
    something else: `snapshot_after_convert`). A base file created here is written back into it, so
    a caller that stores the base several times keeps naming the same file."""
    from .gapi import media_upload

    pid = base["presentationId"]
    if info is None:
        info = deck_info(drive, pid)
    data = json.dumps(base, ensure_ascii=False).encode("utf-8")
    fid = base_file(info)
    if fid:
        try:
            execute(drive.files().update(fileId=fid, media_body=media_upload(io.BytesIO(data), "application/json"),
                                         fields="id"))
        except HttpError:
            fid = None
    if not fid:
        body: FileBody = {"name": f"{title or info.get('name', pid)} - beamer2slides sync base.json",
                          "mimeType": "application/json", "appProperties": {"b2sBaseOf": pid}}
        from .drive_folder import place
        place(body, drive, info.get("parents"))
        fid = file_id(execute(drive.files().create(body=body, fields="id", media_body=media_upload(
            io.BytesIO(data), "application/json"))), "the sync base")
        execute(drive.files().update(fileId=pid, body={"appProperties": {BASE_PROPERTY: fid}}, fields="id"))
        info["appProperties"] = {**(info.get("appProperties") or {}), BASE_PROPERTY: fid}
    return fid


def mark_cleaned(drive: DriveService | None, pid: str, generation: int, info: DriveFile | None) -> str | None:
    """Say that generation `generation`'s `cleanup` list has been carried out, without writing the
    base to Drive again. Returns why Drive would not take it (None: it did).

    The third store of a sync says one thing: the objects the second one named are gone. Storing a
    base is a Drive *media* update, which costs about 1.8 s whatever it carries (measured on one
    file, interleaved: 7 bytes and 147 kB cost the same), while one field of the deck's own
    appProperties - which every later `deck_info` reads anyway - costs a third of a second. Nothing
    about the ordering moves: the list is still in the base before the deletions and no longer read
    after them. A flag that does not land leaves the list standing, which is exactly what a sync
    whose cleanup failed leaves, and the next sync sweeps ids that are already gone
    (`delete_leftovers` takes "could not be found" for gone)."""
    if drive is None:
        return "no Drive service"
    try:
        execute(drive.files().update(fileId=pid, body={"appProperties": {CLEANED_PROPERTY: str(generation)}},
                                     fields="id"))
    except (HttpError, OSError) as e:
        return f"{type(e).__name__}: {e}"
    if info is not None:
        info["appProperties"] = {**(info.get("appProperties") or {}), CLEANED_PROPERTY: str(generation)}
    return None


def load_drive(drive: DriveService, pid: str, info: DriveFile | None) -> dict | None:
    try:
        if info is None:
            info = execute(drive.files().get(fileId=pid, fields="appProperties"))
        fid = base_file(info)
        if not fid:
            return None
        data = execute(drive.files().get_media(fileId=fid))
        return json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except (HttpError, ValueError):
        return None


def stale_base_warning(where: str, drive: DriveService | None, pid: str, info: DriveFile | None) -> str | None:
    """The deck names a base file in Drive that we cannot read (deleted, or owned by someone else)
    while we sync against the folder's copy: another checkout may have synced this deck since, so
    the copy can be older than the deck. Nothing is lost when it is - deck edits win, and the
    changes that checkout already made read as deck edits - but the user should hear about it."""
    if where != "local" or drive is None:
        return None
    try:
        if info is None:
            info = deck_info(drive, pid)
    except HttpError:
        return None
    fid = base_file(info)
    if not fid:
        return None
    try:
        execute(drive.files().get_media(fileId=fid))
    except HttpError:
        return ("the deck names a sync base in Drive that cannot be read; syncing against the copy in "
                "<out>/sync/base.json, which may be older than the deck (docs/sync.md, \"Two checkouts\")")
    return None


def load_base(pid: str, out: Path | None, drive: DriveService | None, problems: list[str] | None,
              info: DriveFile | None) -> tuple[dict | None, str]:
    """(base, where it came from): Drive is authoritative, the local copy a cache - except when the
    local one is newer, which is what a sync whose Drive upload failed leaves behind. A base that is
    truncated, from another deck or from a newer schema is not used at all; `problems` collects why
    (the caller reports them: a silently ignored base would sync against nothing and rewrite the
    whole deck)."""
    problems = problems if problems is not None else []
    if info is None and drive is not None:
        # The same round trip `load_drive` would make, asked a little wider: the deck's own
        # appProperties say which generation's cleanup is done (`mark_cleaned`).
        with contextlib.suppress(HttpError, OSError):
            info = deck_info(drive, pid)

    def swept(base: dict | None) -> dict | None:
        """A `cleanup` list the deck says has been carried out names nothing (`mark_cleaned`)."""
        done = None if info is None else (info.get("appProperties") or {}).get(CLEANED_PROPERTY)
        if base is not None and done and str(base.get("generation", 0)) == done:
            base.pop("cleanup", None)
        return base

    remote = swept(load_drive(drive, pid, info)) if drive is not None else None
    if remote is not None:
        problem = base_problem(remote, pid)
        if problem:
            problems.append(f"the base stored in Drive was ignored: {problem}")
            remote = None
    local, why = read_local(out, pid)
    local = swept(local)
    if why:
        problems.append(f"the local base was ignored: {why}")
    if remote is not None and local is not None and local.get("generation", 0) > remote.get("generation", 0):
        problems.append(f"the base in Drive is older than the local one (generation {remote.get('generation', 0)} vs "
                        f"{local.get('generation', 0)}): syncing from the local one and storing it in Drive again")
        return local, "local"
    if remote is not None:
        return remote, "drive"
    if local is not None:
        return local, "local"
    return None, "none"


def store_base(base: dict, out: Path, drive: DriveService | None, label: str, info: DriveFile | None) -> str | None:
    """Store the base where the next sync will look for it: locally first (atomically), then in
    Drive. Returns why Drive could not take it (None: it did). The caller decides what to do about
    a base that only reached the local folder - sync keeps the objects it would have deleted.

    `info`: the deck's Drive facts, where the caller has them (`deck_info`); a sync stores the base
    three times and they are the same three times."""
    from .faults import fail_at

    fail_at(f"{label}:save")
    save_local(base, out)
    if drive is None:
        return "no Drive service"
    fail_at(f"{label}:drive")
    try:
        save_drive(drive, base, None, info)
    except (HttpError, OSError) as e:
        return f"{type(e).__name__}: {e}"
    return None


def base_matches(base: dict, theirs: dict) -> bool:
    """Whether the base describes this live deck at all. A base whose slides are all gone means the
    deck was copied or rebuilt behind our back: syncing would report every element as deleted in the
    deck and keep nothing."""
    known = [b.get("objectId") for b in base.get("slides", []) if b.get("objectId")]
    if not known:
        return True
    live = {s["objectId"] for s in theirs.get("slides", [])}
    return any(sid in live for sid in known)


def snapshot_after_convert(deck: dict, out: Path, state: dict, pdf: "Path | dict",
                           overlays: str = "last", problems: list[str] | None = None) -> dict:
    """Tag the new deck's objects and record the base (convert's last step). `pdf`: the source,
    or what `source_info` already measured of it.

    `problems` collects what went wrong short of failing (`load_base`'s convention): the base
    kept only in the folder because Drive would not take it, the theme left unrecorded. The first
    one matters more than it looks - Drive is where a later sync looks first, and a caller whose
    folder does not outlive the call (a detached agent context) has no other copy, so its next
    sync refuses with `no_base` and nothing in this conversion's output would say why. Left None,
    they are printed, as the CLI always did."""
    from .google_auth import (credentials_for_threads, drive_service, fetcher_for_threads, shared_service,
                              slides_service)

    def problem(text: str) -> None:
        if problems is None:
            print(f"warning: {text}")
        else:
            problems.append(text)

    slides, drive = slides_service(), drive_service()
    pid = state["presentationId"]
    pool = ThreadPoolExecutor(2, thread_name_prefix="b2s-base")
    # Where the base file goes needs nothing but the id, so it is looked up while the deck is
    # being read and tagged, on a thread with a client of its own (`save_drive`'s `info`).
    where_to_put_it = None
    if not shared_service("drive", "v3"):
        creds = credentials_for_threads()  # here: a worker thread inherits no context
        where_to_put_it = pool.submit(lambda: deck_info(drive_service(creds), pid))
    pres = execute(slides.presentations().get(presentationId=pid))
    # Every picture was uploaded from a file here, so it is signed from that file
    # (`upload_signatures`); only one Google may have reshaped is downloaded, while the tags are
    # written: they hang off contentUrls, not off a Google client - only the fetcher, resolved
    # here (`net`). What no download brought is exported afterwards, on this thread.
    local = upload_signatures(pres, converted_files(deck, out, state, pres))
    signing = pool.submit(picture_signatures, pres, PICTURE_WORKERS, fetcher_for_threads(), local, None)
    pool.shutdown(wait=False)
    base = build_base(deck, out, pres, state, pdf, 0, False, overlays, None)
    landed, revision = write_tags(slides, pid, tag_requests(base))
    if landed:
        pres = tagged(pres, landed, revision)  # what a second read would say, measured (`tagged`)
    signatures = dict(local)
    with contextlib.suppress(Exception):
        signatures.update(signing.result())
    images, backgrounds = picture_urls(pres)
    unsigned = [i for i in {**images, **backgrounds} if i not in signatures]
    if unsigned:  # (their downloads failed already: straight to the export)
        exported = LivePictures(pres, drive, fetcher_for_threads(), PICTURE_WORKERS, None, slides).export()
        signatures.update({i: sig for i in unsigned if (d := exported.get(i)) and (sig := signature(d))})
    base = build_base(deck, out, pres, state, pdf, 0, True, overlays, signatures)
    # What convert wrote on the master and the layouts, so a sync can carry a new theme there and
    # tell a person's layout edits from its own (theme_sync). A deck without it syncs as before.
    try:
        from . import theme_sync
        theme = theme_sync.record(deck, out, pres, state)
        if theme:
            base["theme"] = theme
    except Exception as e:  # noqa: BLE001 (a missing record costs theme sync, never the conversion)
        problem(f"could not record the deck's theme for sync ({e}); a later sync leaves the "
                f"master and layouts alone")
    info: DriveFile | None = None
    if where_to_put_it is not None:
        with contextlib.suppress(Exception):  # then save_drive reads it itself
            info = where_to_put_it.result()
    failed = store_base(base, out, drive, "convert-base", info)
    if failed:
        problem(f"could not store the sync base in Drive ({failed}); it was kept only in "
                f"{local_path(out)}, and a later sync that cannot see that folder refuses with "
                f"no_base until the deck is converted again")
    return base
