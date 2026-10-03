"""The sync base (docs/sync.md): the converter's IR plus Google's read-back of every object it
created, recorded right after the deck was written, in `<out>/sync/base.json` and in Drive."""

import contextlib
import copy
import io
import json
import os
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Union

from . import identity
from .emit import background_key as emit_background_key, slide_layout
from .emit_state import EmitState, SlideState, emit_state_json
from .deck_pictures import WORKERS as PICTURE_WORKERS, LivePictures
from .gapi import HttpError
from .google_types import (AffineTransform, DriveFile, DriveService, FileBody, LayoutProperties, Page, PageElement,
                           Presentation, BatchUpdateResponse, Size, SlideProperties, SlidesRequest, SlidesService, WriteControl,
                           all_elements, background_fill, background_url,
                           children, file_id, image_url, object_id, part, parts, presentation_id)
from .gslides import EMU_PER_PT, execute
from .ir_types import IRError, element_json, parse_element, parse_rendered_element
from .json_types import (Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects, as_optional_str,
                         as_str)
from .net import Fetch
from .sync_model import (Base, DeckRead, ElementEntry, ElementKey, Fingerprint, ImageRead, ObjectId, ReadBack, RunSpan,
                         SlideEntry, SlideKey, SlideRead, SlideSeen, Tied, base_json, deck_read_json, fingerprint,
                         slide_entry_json, slide_read_json)
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


def _number(v: Json, where: str) -> float:
    """A number of an answer as it came (an int stays one: what is written is what was read)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected, found {type(v).__name__}")


def _text(v: Json, where: str) -> str:
    if isinstance(v, str):
        return v
    raise JsonShapeError(f"{where}: a string was expected, found {type(v).__name__}")


def colour(c: JsonObject | None) -> str | None:
    """An OpaqueColor / OptionalColor as '#rrggbb' or 'theme:NAME'."""
    if not c:
        return None
    if "opaqueColor" in c:
        c = part(c["opaqueColor"], "opaqueColor")
    if "themeColor" in c:
        return f"theme:{c['themeColor']}"
    if "rgbColor" in c:
        rgb = part(c["rgbColor"], "rgbColor")
        return "#" + "".join(f"{round(_number(rgb.get(k, 0.0), f'rgbColor.{k}') * 255):02x}"
                             for k in ("red", "green", "blue"))
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


def _text_style(style: JsonObject) -> JsonObject:
    out: JsonObject = {k: style[k] for k in TEXT_STYLE_KEYS if k in style}
    if "weightedFontFamily" in style:
        weighted = part(style["weightedFontFamily"], "textStyle.weightedFontFamily")
        out["fontFamily"] = weighted.get("fontFamily")
        out["weight"] = weighted.get("weight")
    if "fontSize" in style:
        out["fontSize"] = round(_unit(part(style["fontSize"], "textStyle.fontSize")), 2)
    for k in ("foregroundColor", "backgroundColor"):
        if style.get(k):
            out[k] = colour(part(style[k], f"textStyle.{k}"))
    link = part(style.get("link"), "textStyle.link")
    if link:
        out["link"] = link.get("url") or link.get("pageObjectId") or link.get("relativeLink") or str(link.get("slideIndex"))
    return out


def _paragraph_style(marker: JsonObject) -> JsonObject:
    style = part(marker.get("style"), "paragraphMarker.style")
    out: JsonObject = {k: style[k] for k in PARAGRAPH_KEYS if k in style}
    for k in ("indentStart", "indentFirstLine", "spaceAbove", "spaceBelow"):
        if k in style:
            out[k] = round(_unit(part(style[k], f"paragraphStyle.{k}")), 2)
    if "bullet" in marker:
        bullet = part(marker["bullet"], "paragraphMarker.bullet")
        out["bullet"] = [bullet.get("glyph"), bullet.get("nestingLevel", 0)]
    return out


@dataclass(frozen=True, kw_only=True)
class TextRead:
    """A shape's or cell's text as sync compares it (`read_text`): the content, its distinct run and
    paragraph styles, and where each run is. A span is (start, end, style) in characters of the
    content, which is what says *which* words a style is on - the distinct styles alone cannot
    (`merge.styling_lost`)."""
    content: str
    runs: list[JsonObject]
    paragraphs: list[JsonObject]
    spans: list[RunSpan]


def read_text_of(text: JsonObject) -> TextRead:
    content: list[str] = []
    runs: list[JsonObject] = []
    paras: list[JsonObject] = []
    spans: list[RunSpan] = []
    at = 0
    for te in parts(text.get("textElements"), "text.textElements"):
        if "textRun" in te:
            run = part(te["textRun"], "textRun")
            piece = _text(run.get("content", ""), "textRun.content")
            content.append(piece)
            s = _text_style(part(run.get("style"), "textRun.style"))
            if piece.strip("\n"):
                spans.append((at, at + len(piece.rstrip("\n")), s))
                if s not in runs:
                    runs.append(s)
            at += len(piece)
        elif "autoText" in te:
            piece = _text(part(te["autoText"], "autoText").get("content", ""), "autoText.content")
            content.append(piece)
            at += len(piece)
        elif "paragraphMarker" in te:
            s = _paragraph_style(part(te["paragraphMarker"], "paragraphMarker"))
            if s not in paras:
                paras.append(s)
    return TextRead(content="".join(content), runs=runs, paragraphs=paras, spans=spans)


def read_text(text: JsonObject | None) -> tuple[str, list[JsonObject], list[JsonObject], list[list[int | JsonObject]]]:
    """`read_text_of` as (content, run styles, paragraph styles, spans as [start, end, style])."""
    t = read_text_of(text or {})
    return t.content, t.runs, t.paragraphs, [[a, b, s] for a, b, s in t.spans]


def _fill(fill: JsonObject) -> JsonObject | None:
    if not fill:
        return None
    if "solidFill" in fill:
        solid = part(fill["solidFill"], "solidFill")
        return {"color": colour(part(solid.get("color"), "solidFill.color")),
                "alpha": round(_number(solid.get("alpha", 1.0), "solidFill.alpha"), 3)}
    return {"state": fill.get("propertyState", "RENDERED")}


def _outline(o: JsonObject) -> JsonObject | None:
    if not o:
        return None
    return {"fill": _fill(part(o.get("outlineFill"), "outline.outlineFill")),
            "weight": round(_unit(part(o.get("weight"), "outline.weight")), 2),
            "dash": o.get("dashStyle"), "state": o.get("propertyState", "RENDERED")}


def shape_style(e: PageElement) -> JsonObject:
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
    grey = Image.alpha_composite(white, rgba).convert("L").resize((SIGNATURE_SIZE, SIGNATURE_SIZE), Image.Resampling.BOX)
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


def _signature_of(read: Mapping[str, object]) -> str | None:
    sig = read.get("signature")
    return sig if isinstance(sig, str) else None


def same_picture(a: Mapping[str, object] | None, b: Mapping[str, object] | None) -> bool:
    """Image read-backs ({"contentHash", "signature"?}) or picture backgrounds ({"picture", "signature"?}):
    the same picture if the URL hash is the same, or else the pixel signatures match."""
    ra: Mapping[str, object] = a or {}
    rb: Mapping[str, object] = b or {}
    ha, hb = ra.get("contentHash", ra.get("picture")), rb.get("contentHash", rb.get("picture"))
    if ha == hb:
        return True
    return bool(signatures_match(_signature_of(ra), _signature_of(rb)))


def same_background(a: Mapping[str, object] | None, b: Mapping[str, object] | None) -> bool:
    if a is None or b is None:
        return a == b
    if "picture" in a and "picture" in b:
        return same_picture(a, b)
    return a == b


ASPECT_AGREES = 0.01  # a live picture has its file's shape when the aspects are this close


@dataclass(frozen=True, kw_only=True)
class SignedFile:
    """A picture file's size in pixels and its `signature` (`signed_file`)."""
    width: int
    height: int
    signature: str


def signed_file(path: "Path | str") -> SignedFile | None:
    """A picture file signed, before anything says which live picture shows it: None when it is
    no picture."""
    from PIL import Image
    try:
        data = Path(path).read_bytes()
        with Image.open(io.BytesIO(data)) as img:
            fw, fh = img.size
    except (OSError, ValueError):
        return None
    sig = signature(data)
    return None if sig is None else SignedFile(width=fw, height=fh, signature=sig)


def sign_files(paths: Iterable[Path]) -> dict[Path, SignedFile | None]:
    """`signed_file` of each of `paths`: convert signs what it uploaded while it reads the deck
    back (`snapshot_after_convert`; 2.5 s of decoding on an 86-slide deck)."""
    return {p: signed_file(p) for p in dict.fromkeys(paths)}


def shaped_signature(signed: SignedFile | None, shape: tuple[float, float] | None) -> str | None:
    """A signed file's signature when the live picture has its shape (`local_signature`)."""
    if signed is None or not shape or shape[1] <= 0 or signed.height <= 0:
        return None
    a, b = signed.width / signed.height, shape[0] / shape[1]
    if abs(a - b) > ASPECT_AGREES * max(a, b):
        return None
    return signed.signature


def local_signature(path: "Path | str", shape: tuple[float, float] | None) -> str | None:
    """The signature of a picture this run has just put into the deck, from the file it uploaded,
    so nothing has to be downloaded to record it. Google serves what it was given, re-encoded:
    measured on two converted decks (probe of 2026-09-24), every one of 22 uploaded pictures signed
    alike from its file and from its download. That holds while the live picture has the file's
    shape - `shape` is the element's own size (a stretch is the transform's) or the page's for a
    background - and a picture Google may have resampled to another is left to be read (None)."""
    return shaped_signature(signed_file(path), shape)


def upload_signatures(pres: Presentation, files: Mapping[str, "Path | str"],
                      signed: Mapping[Path, SignedFile | None]) -> dict[str, str]:
    """`local_signature` of each picture of `pres` whose file is known: `files` maps an image's
    objectId, or a slide's for its background picture, to the file that was uploaded for it;
    `signed` holds files already signed (`sign_files`), the others are signed here."""
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
        file = Path(path)
        sig = shaped_signature(signed[file] if file in signed else signed_file(file), shape)
        if sig:
            out[oid] = sig
    return out


def _ids(v: Json, where: str) -> list[ObjectId]:
    return [ObjectId(as_str(x, f"{where}[{i}]")) for i, x in enumerate(as_array(v, where))]


def _margins(v: Json, where: str) -> tuple[tuple[float, ...], ...]:
    return tuple(tuple(_number(x, where) for x in as_array(m, where)) for m in as_array(v, where))


@dataclass(frozen=True, kw_only=True)
class WrittenSlide:
    """What a base takes of a slide made for it: the slide's objectId (None: none is known), each
    element's objects (its main object first), the slide's other groups, and the cell margins of
    each table the .pptx brought, by the element's index. emit's (`written_of`) or adopt's pairing
    (`written_json`)."""
    object_id: ObjectId | None
    objects: tuple[tuple[ObjectId, ...], ...]
    groups: tuple[ObjectId, ...]
    table_margins: Mapping[int, tuple[tuple[float, ...], ...]]


def written_of(s: SlideState) -> WrittenSlide:
    """An emitted slide as the base takes it. An emit.json older than `objects` names each
    element's main object alone."""
    objects = s.objects if s.objects else tuple((o,) for o in s.elements)
    return WrittenSlide(object_id=ObjectId(s.object_id),
                        objects=tuple(tuple(ObjectId(o) for o in os) for os in objects),
                        groups=tuple(ObjectId(g) for g in s.groups or ()),
                        table_margins={int(i): m for i, m in (s.table_margins or {}).items()})


def written_json(s: JsonObject, where: str) -> WrittenSlide:
    """A slide of a state given as a dict (adopt's pairing, a test's): `objects`, `groups` and
    `table_margins` may be missing, `objectId` None."""
    written = s.get("objects")
    objects = [_ids(o, f"{where}: objects") for o in as_array(written, f"{where}: objects")] if written else \
        [[o] for o in _ids(s["elements"], f"{where}: elements")]
    oid = s.get("objectId")
    margins = part(s.get("table_margins"), f"{where}: table_margins")
    return WrittenSlide(object_id=ObjectId(oid) if isinstance(oid, str) else None,
                        objects=tuple(tuple(o) for o in objects),
                        groups=tuple(_ids(s.get("groups", []), f"{where}: groups")),
                        table_margins={int(i): _margins(m, f"{where}: table_margins") for i, m in margins.items()})


def converted_files(deck: JsonObject, out: Path, written: Sequence[WrittenSlide], pres: Presentation) -> dict[str, Path]:
    """What emit uploaded for each picture of the deck it just made: an image element's file, a
    slide's own background picture (`upload_signatures`' `files`)."""
    images = picture_urls(pres)[0]
    files: dict[str, Path] = {}
    for slide, s in zip(as_objects(deck.get("slides", []), "deck.slides"), written):
        for el, oids in zip(as_objects(slide["elements"], "slide.elements"), s.objects):
            file = el.get("file")
            if el.get("kind") == "image" and isinstance(file, str) and file:
                for oid in [o for o in oids if o in images][:1]:
                    files[oid] = out / file
        background = slide.get("background")
        if s.object_id and isinstance(background, str) and background and not slide.get("background_color"):
            files[s.object_id] = out / background
    return files


def uploaded_files(deck: JsonObject, out: Path) -> list[Path]:
    """Every file `converted_files` may name, known before the deck is read (`sign_files`)."""
    files: list[Path] = []
    for slide in as_objects(deck.get("slides", []), "deck.slides"):
        for el in as_objects(slide["elements"], "slide.elements"):
            file = el.get("file")
            if el.get("kind") == "image" and isinstance(file, str) and file:
                files.append(out / file)
        background = slide.get("background")
        if isinstance(background, str) and background and not slide.get("background_color"):
            files.append(out / background)
    return files


def _download(url: str, fetch: Fetch | None) -> bytes | None:
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


def sign_pictures(read: JsonObject, pres: Presentation, objects: Collection[str] | None, slides: Collection[str] | None,
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
    jobs: list[tuple[str, JsonObject]] = []
    for s in as_objects(read["slides"], "read.slides"):
        sid = as_str(s["objectId"], "read slide objectId")
        bg = part(s.get("background"), f"slide {sid}: background")
        if "picture" in bg and sid in backgrounds and (slides is None or sid in slides):
            jobs.append((sid, bg))
        for oid, rb in as_object(s["objects"], f"slide {sid}: objects").items():
            rb = as_object(rb, f"slide {sid}: {oid}")
            if "image" in rb and oid in images and (objects is None or oid in objects):
                jobs.append((oid, as_object(rb["image"], f"slide {sid}: {oid}.image")))
    if not jobs:
        return 0
    found = _signatures(pres, [oid for oid, _ in jobs], workers, ready, fetch, drive, files, pictures)
    for oid, target in jobs:
        if oid in found:
            target["signature"] = found[oid]
            target.pop("unchecked", None)
    return len(jobs)


def sign_pictures_of(read: DeckRead, pres: Presentation, objects: Collection[str] | None,
                     slides: Collection[str] | None, workers: int, ready: Mapping[str, str] | None,
                     fetch: Fetch | None, drive: DriveService | None, files: Mapping[str, "Path | str"] | None,
                     pictures: LivePictures | None) -> tuple[DeckRead, int]:
    """`sign_pictures` of a read-back record: the read with the signatures in, and how many
    pictures were signed or asked for."""
    images, backgrounds = picture_urls(pres)
    jobs: list[str] = []
    for s in read.slides:
        if s.background is not None and "picture" in s.background and s.object_id in backgrounds \
                and (slides is None or s.object_id in slides):
            jobs.append(s.object_id)
        jobs += [oid for oid, rb in s.objects.items()
                 if rb.image is not None and oid in images and (objects is None or oid in objects)]
    if not jobs:
        return read, 0
    found = _signatures(pres, jobs, workers, ready, fetch, drive, files, pictures)

    def signed_background(s: SlideRead) -> JsonObject | None:
        if s.background is None or s.object_id not in found:   # (found holds only what was asked)
            return s.background
        bg = dict(s.background)
        bg["signature"] = found[s.object_id]
        bg.pop("unchecked", None)
        return bg

    def signed(oid: ObjectId, rb: ReadBack) -> ReadBack:
        if rb.image is None or oid not in found:
            return rb
        return replace(rb, image=replace(rb.image, signature=found[oid], unchecked=False))

    return replace(read, slides=tuple(replace(s, background=signed_background(s),
                                              objects={oid: signed(oid, rb) for oid, rb in s.objects.items()})
                                      for s in read.slides)), len(jobs)


def _signatures(pres: Presentation, ids: Sequence[str], workers: int, ready: Mapping[str, str] | None,
                fetch: Fetch | None, drive: DriveService | None, files: Mapping[str, "Path | str"] | None,
                pictures: LivePictures | None) -> dict[str, str]:
    """The signatures of the pictures `ids` names that could be had (`sign_pictures`)."""
    if ready is not None:
        return {oid: ready[oid] for oid in ids if oid in ready}
    asked = set(ids)
    local = upload_signatures(pres, {oid: f for oid, f in files.items() if oid in asked}, {}) if files else {}
    rest = [oid for oid in ids if oid not in local]
    if rest:
        if pictures is None:
            pictures = LivePictures(pres, drive, _fetcher(fetch), workers, None, None)
        got = pictures.get(rest)
        local.update({i: sig for i, d in got.items() if (sig := signature(d))})
    return local


KINDS = ("shape", "image", "line", "table", "elementGroup", "sheetsChart", "video", "wordArt")


def read_object(e: PageElement, parent: list[float], parent_group: ObjectId | None, z: int) -> ReadBack:
    """Normalised read-back of one page element (pt, hex colours, absolute transform). A group's
    box and children are its slide's to say (`read_slide_of`)."""
    m = compose(parent, matrix(e.get("transform")))
    size = e.get("size") or Size()
    w, h = _unit(size.get("width")), _unit(size.get("height"))
    x0, y0, x1, y1 = box(m, w, h)
    text: str | None = None
    runs: list[JsonObject] = []
    paras: list[JsonObject] = []
    spans: list[RunSpan] = []
    placeholder: str | None = None
    grid: tuple[int, int] | None = None
    shape, table, image = e.get("shape"), e.get("table"), e.get("image")
    if shape is not None:
        read = read_text_of(part(shape.get("text"), "shape.text"))
        text, runs, paras, spans = read.content, read.runs, read.paragraphs, read.spans
        if "placeholder" in shape:
            placeholder = as_optional_str(part(shape["placeholder"], "shape.placeholder").get("type"), "placeholder.type")
    elif table is not None:
        rows: list[str] = []
        at = 0
        for row in parts(table.get("tableRows"), "table.tableRows"):
            cells: list[str] = []
            for cell in parts(row.get("tableCells"), "tableRows.tableCells"):
                read = read_text_of(part(cell.get("text"), "tableCells.text"))
                cells.append(read.content.rstrip("\n"))
                runs += [x for x in read.runs if x not in runs]
                paras += [x for x in read.paragraphs if x not in paras]
                # the cells are joined below, so the spans move with their cell into that text
                spans += [(a + at, min(b + at, at + len(cells[-1])), st) for a, b, st in read.spans if a < len(cells[-1])]
                at += len(cells[-1]) + 1                       # the tab (or, after the last cell, the newline)
            rows.append("\t".join(cells))
        text = "\n".join(rows)
        grid = (as_int(table.get("rows"), "table.rows"), as_int(table.get("columns"), "table.columns"))
    style = shape_style(e)
    return ReadBack(
        kind=next((k for k in KINDS if k in e), "other"),
        transform=tuple([round(v, 4) for v in m[:4]] + [round(v, 2) for v in m[4:]]), size=(round(w, 2), round(h, 2)),
        box=(x0, y0, x1, y1), parent_group=parent_group, z=z, title=e.get("title"), description=e.get("description"),
        placeholder=placeholder, table=grid, text=text, text_styles=tuple(runs), paragraph_styles=tuple(paras),
        run_spans=tuple(spans),
        text_style_hash=identity.sha1(json.dumps([sorted(json.dumps(s, sort_keys=True) for s in runs),
                                                  sorted(json.dumps(s, sort_keys=True) for s in paras)]))[:12],
        shape_style=style, shape_style_hash=identity.sha1(json.dumps(style, sort_keys=True))[:12],
        image=None if image is None else ImageRead(
            content_hash=image_hash(as_optional_str(image.get("contentUrl"), "image.contentUrl")),
            source_url=as_optional_str(image.get("sourceUrl"), "image.sourceUrl"), signature=None, unchecked=False),
        children=None, refit=None)


def background_of(page: Page) -> JsonObject:
    """A page's background as sync compares it: {picture: hash} | {color} | {state}."""
    fill = background_fill(page)
    if "stretchedPictureFill" in fill:
        return {"picture": image_hash(background_url(page))}
    if "solidFill" in fill:
        return {"color": colour(part(part(fill["solidFill"], "solidFill").get("color"), "solidFill.color"))}
    return {"state": fill.get("propertyState", "INHERIT")}


def background(page: Page) -> JsonObject:
    """`background_of`, for the readers that still take a dict."""
    return background_of(page)


def read_slide_of(slide: Page) -> SlideRead:
    """A slide as sync compares it: its objects' read-backs by id (a group before its children, its
    box theirs), its top-level `order`, background and speaker notes."""
    objects: dict[ObjectId, ReadBack] = {}
    counter = [0]

    def walk(elements: Sequence[PageElement], parent: list[float], group: ObjectId | None) -> None:
        for e in elements:
            oid = ObjectId(object_id(e))
            rb = read_object(e, parent, group, counter[0])
            counter[0] += 1
            objects[oid] = rb
            if "elementGroup" in e:
                kids = children(e, oid)
                walk(kids, compose(parent, matrix(e.get("transform"))), oid)
                boxes = [objects[ObjectId(object_id(c))].box for c in kids]
                grown = rb.box if not boxes else (min(k[0] for k in boxes), min(k[1] for k in boxes),
                                                  max(k[2] for k in boxes), max(k[3] for k in boxes))
                # (put back under its own key, so the group still comes before its children)
                objects[oid] = replace(rb, box=grown, children=tuple(ObjectId(object_id(c)) for c in kids))

    walk(slide.get("pageElements", []), [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], None)
    props = slide.get("slideProperties") or SlideProperties()
    notes_page = props.get("notesPage") or Page()
    notes_id = as_optional_str(part(notes_page.get("notesProperties"), "notesProperties").get("speakerNotesObjectId"),
                               "notesProperties.speakerNotesObjectId")
    notes = ""
    for e in notes_page.get("pageElements", []):
        if e.get("objectId") == notes_id:
            notes = read_text_of(part(part(e.get("shape"), "shape").get("text"), "shape.text")).content
    return SlideRead(object_id=ObjectId(object_id(slide)), layout_object_id=props.get("layoutObjectId"),
                     background=background_of(slide), notes=notes.rstrip("\n"), notes_id=notes_id,
                     order=tuple(ObjectId(object_id(e)) for e in slide.get("pageElements", [])), objects=objects)


def read_slide(slide: Page) -> JsonObject:
    """`read_slide_of` as JSON: {objectId, layoutObjectId, background, notes, notes_id, order,
    objects {id: read-back}}."""
    return slide_read_json(read_slide_of(slide))


def page_size(pres: Presentation) -> list[float]:
    """The presentation's page, in pt. Every box a sync writes is in these points, and a deck a
    person built is whatever size they made it (`adopt_sync`)."""
    size = pres.get("pageSize")
    if size is None:
        raise JsonShapeError("the presentation has no pageSize (a fields= mask that left it out?)")
    return [_unit(size.get("width")), _unit(size.get("height"))]


def read_presentation_of(pres: Presentation) -> DeckRead:
    """The live deck as sync compares it: "theirs" to the merge, and what a base records of it."""
    layouts: JsonObject = {object_id(l): (l.get("layoutProperties") or LayoutProperties()).get("name")
                           for l in pres.get("layouts", [])}
    masters = pres.get("masters", [])
    return DeckRead(presentation_id=presentation_id(pres), revision_id=pres.get("revisionId"),
                    page_size=tuple(page_size(pres)), layouts=layouts,
                    master_background=background_of(masters[0]) if masters else None,
                    slides=tuple(read_slide_of(s) for s in pres.get("slides", [])))


def read_presentation(pres: Presentation) -> JsonObject:
    """`read_presentation_of` as JSON, for the readers that still take it so (and change it)."""
    return deck_read_json(read_presentation_of(pres))


# ---------------------------------------------------------------- base

def source_info(pdf: "Path | str | JsonObject | None") -> JsonObject:
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


def background_key(slide: JsonObject, out: Path) -> str:
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
            if identity.normalise_ir(el["ir"], _anchor_key(el), None) != identity.normalise_ir(oe["ir"], _anchor_key(oe), None):
                continue  # (the IR changed in a way the field hashes don't see)
            if not same_picture_file(old / as_str(as_object(el["ir"], "ir")["file"], "ir.file"), ours_out / new):
                continue
            ours_fields = as_object(oe["fields"], f"slide {slide}, element {key}: fields")
            el["fields"] = {**as_object(el["fields"], f"base slide {slide}, element {key}: fields"),
                            "image": ours_fields["image"]}
            el["ir_hash"] = oe["ir_hash"]
            refreshed.append(Refreshed(slide=slide, element=key))
    return refreshed


def slide_entries_of(deck: JsonObject, out: Path, keys: Sequence[SlideKey], element_keys: Sequence[Sequence[ElementKey]],
                     fingerprints: Sequence[Sequence[Fingerprint]]) -> list[SlideEntry]:
    """The IR part of base slides (keys, hashes, fingerprints, IR), without objects and read-back.
    An element's `ir` is the deck's own element, not a copy."""
    page_key = page_keys(deck, keys)
    entries: list[SlideEntry] = []
    for slide, key, ekeys, fps in zip(as_objects(deck["slides"], "deck.slides"), keys, element_keys, fingerprints):
        els = as_objects(slide["elements"], f"slide {key}: elements")
        ids = {as_str(e["id"], f"slide {key}: element id"): k for e, k in zip(els, ekeys)}
        elements: list[ElementEntry] = []
        for el, ek, fp in zip(els, ekeys, fps):
            at = f"slide {key}, element {ek}"
            anchor = el.get("anchor")
            anchor_key = ids.get(anchor) if isinstance(anchor, str) else None
            h, fields = identity.ir_fields_json(el, out, anchor_key, page_key)
            elements.append(ElementEntry(
                key=ek, id=as_str(el["id"], f"{at}: id"), kind=as_str(el["kind"], f"{at}: kind"),
                role=as_optional_str(el.get("role"), f"{at}: role"), ir_hash=h, fields=fields, fingerprint=fp,
                anchor=anchor_key, ir=el, tied=None, removed=None, table_margins=None, drawn_from=None,
                from_layout=False, in_table=False))
        entries.append(SlideEntry(
            key=key, label=as_optional_str(slide.get("label"), f"slide {key}: label"), title=identity.slide_title(slide),
            page=as_int(slide["page"], f"slide {key}: page"), text=identity.slide_text(slide),
            layout=slide_layout(slide)[0], background=background_key(slide, out),
            notes=as_optional_str(slide.get("notes"), f"slide {key}: notes") or "", elements=tuple(elements), seen=None,
            removed=None, left_alone=None, key_order=()))
    return entries


def slide_entries(deck: JsonObject, out: Path, keys: Sequence[str], element_keys: Sequence[Sequence[str]],
                  fingerprints: Sequence[Sequence[JsonObject]]) -> list[JsonObject]:
    """`slide_entries_of` as JSON, the fingerprints as `identity.fingerprint` writes them."""
    return [slide_entry_json(s) for s in slide_entries_of(
        deck, out, [SlideKey(k) for k in keys], [[ElementKey(k) for k in ks] for ks in element_keys],
        [[fingerprint(fp, f"slide {key}: fingerprint") for fp in fps] for key, fps in zip(keys, fingerprints)])]


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


def attached(entry: SlideEntry, slide_read: SlideRead | None, objects: Sequence[Sequence[ObjectId]],
             groups: Sequence[ObjectId]) -> SlideEntry:
    """One base slide with the objects it was written as and their read-back (None: the deck has
    no such slide)."""
    found: Mapping[ObjectId, ReadBack] = {} if slide_read is None else slide_read.objects
    elements = list(entry.elements)
    for i, (el, written) in enumerate(zip(entry.elements, objects)):
        oids = list(written)
        if slide_read is not None and any(oid in found for oid in oids):
            # What emit meant to create, less what the deck does not have: a diagram of one node
            # gets no group (Slides groups two objects or more), and a group id the base names
            # but the deck never had reads as deleted to every later sync and rebuild guard.
            oids = [oid for oid in oids if oid in found]
        elements[i] = replace(el, tied=Tied(objects=tuple(oids), main=oids[0] if oids else None,
                                            readback={oid: found[oid] for oid in oids if oid in found}))
    seen = SlideSeen(object_id=None if slide_read is None else slide_read.object_id,
                     layout_object_id=None if slide_read is None else slide_read.layout_object_id,
                     background_readback=None if slide_read is None else slide_read.background,
                     notes_readback="" if slide_read is None else slide_read.notes, groups=tuple(groups),
                     order=() if slide_read is None else slide_read.order)
    return replace(entry, elements=tuple(elements), seen=seen)


def build_base_of(deck: JsonObject, out: Path, pres: Presentation, written: Sequence[WrittenSlide],
                  scale: float | None, pdf: "Path | str | JsonObject | None", generation: int, sign: bool, overlays: str,
                  signatures: Mapping[str, str] | None) -> Base:
    """The base after `convert`: `written` is what emit made of each slide and `scale` its deck pt
    per PDF pt (`emit_state.EmitState`); `sign`: download the pictures for their signatures
    (`signatures`: unless these were downloaded already); `overlays`: which overlay steps the deck
    was made from, so a later sync uses the same ones (a sync with fewer would delete the deck's
    slides)."""
    deck_slides = as_objects(deck["slides"], "deck.slides")
    keys = identity.slide_keys([identity.slide_info(s) for s in deck_slides])
    made = [identity.slide_element_keys_of(as_objects(s["elements"], "slide.elements"), out, None) for s in deck_slides]
    entries = slide_entries_of(deck, out, keys, [k for k, _ in made], [f for _, f in made])
    read = read_presentation_of(pres)
    if sign:
        read, _ = sign_pictures_of(read, pres, None, None, PICTURE_WORKERS, signatures, None, None, None, None)
    by_id = {s.object_id: s for s in read.slides}
    for n, s in enumerate(written[:len(entries)]):
        entry = attached(entries[n], None if s.object_id is None else by_id.get(s.object_id), s.objects, s.groups)
        # The cell margins of a table the .pptx brought (emit.pptx_table), which the API can
        # neither read nor set: a sync refills such a table in place (sync.table_refill).
        if s.table_margins:
            elements = list(entry.elements)
            for i, m in s.table_margins.items():
                elements[i] = replace(elements[i], table_margins=m)
            entry = replace(entry, elements=tuple(elements))
        entries[n] = entry
    size = deck_slides[0]["size"] if deck_slides else None
    return Base(version=VERSION, generation=generation, presentation_id=read.presentation_id,
                revision_id=read.revision_id, source=source_info(pdf), overlays=overlays, scale=scale,
                page_size=None if size is None else tuple(_number(x, "slide.size") for x in as_array(size, "slide.size")),
                deck_page_size=read.page_size, master_background=master_key(deck, out),
                master_readback=read.master_background, slides=tuple(entries), theme=None, pending=None, cleanup=None,
                origin=None, adopt=None)


def converted_base(deck: JsonObject, out: Path, pres: Presentation, written: Sequence[WrittenSlide],
                   scale: float | None, pdf: "Path | str | JsonObject | None", sign: bool, overlays: str,
                   signatures: Mapping[str, str] | None) -> JsonObject:
    """`build_base_of` of a conversion (generation 0) as base.json holds it: what
    `snapshot_after_convert` stores, and the one step of it a test replaces."""
    return base_json(build_base_of(deck, out, pres, written, scale, pdf, 0, sign, overlays, signatures))


def build_base(deck: JsonObject, out: Path, pres: Presentation, state: JsonObject, pdf: "Path | str | JsonObject | None",
               generation: int,
               sign: bool, overlays: str, signatures: Mapping[str, str] | None) -> JsonObject:
    """`build_base_of` as the JSON base.json holds, from a state given as a dict ({"slides",
    "scale"}: adopt's pairing, which names no page or URL; a test's)."""
    scale = state.get("scale")
    written = [written_json(s, f"state slide {n}") for n, s in enumerate(as_objects(state["slides"], "state.slides"))]
    return base_json(build_base_of(deck, out, pres, written, None if scale is None else _number(scale, "state.scale"),
                                   pdf, generation, sign, overlays, signatures))


def master_key(deck: JsonObject, out: Path) -> str | None:
    """The background emit put on the master (the most common one, if shared)."""
    from collections import Counter
    counts = Counter(background_key(s, out) for s in as_objects(deck["slides"], "deck.slides"))
    if not counts:
        return None
    key, n = counts.most_common(1)[0]
    return key if n >= 2 else None


def tag(slide_key: str, element_key: str) -> str:
    return f"{TAG_PREFIX}{slide_key}/{element_key}"


def tag_requests(base: JsonObject) -> list[SlidesRequest]:
    """Alt-text titles naming each element's main object, so copies made in Slides are recognised."""
    reqs: list[SlidesRequest] = []
    for s in as_objects(base["slides"], "base.slides"):
        skey = as_str(s["key"], "base slide key")
        for el in as_objects(s["elements"], f"base slide {skey}: elements"):
            main = el.get("main")
            if not isinstance(main, str) or not main:
                continue
            ekey = as_str(el["key"], f"base slide {skey}: element key")
            rb = part(as_object(el.get("readback") or {}, f"{skey}/{ekey}: readback").get(main),
                      f"{skey}/{ekey}: readback of {main}")
            if rb and rb.get("title") != tag(skey, ekey) and rb["kind"] != "elementGroup":
                reqs.append({"updatePageElementAltText": {"objectId": main, "title": tag(skey, ekey)}})
    return reqs


def write_tags(slides: SlidesService, pid: str, reqs: list[SlidesRequest]) -> tuple[list[SlidesRequest], str | None]:
    """Sends tag requests; the ones the API refuses (some placeholders) are skipped. Returns the
    ones that landed and the deck's revision afterwards, which the batch's own answer says
    (`writeControl.requiredRevisionId`), so nothing has to read the deck again to learn it."""
    if not reqs:
        return [], None

    def revision(answer: BatchUpdateResponse) -> str | None:
        return (answer.get("writeControl") or WriteControl()).get("requiredRevisionId")

    try:
        return reqs, revision(execute(slides.presentations().batchUpdate(
            presentationId=pid, body={"requests": reqs})))
    except HttpError:
        sent: list[SlidesRequest] = []
        at: str | None = None
        for r in reqs:
            try:
                at = revision(execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [r]})))
                sent.append(r)
            except HttpError:
                pass
        return sent, at


def tagged(pres: Presentation, reqs: list[SlidesRequest], revision: str | None) -> Presentation:
    """A presentations.get with the alt-text titles `reqs` have just written put into it, and the
    revision the batch answered with.

    The only news a second read of the deck would bring is exactly this - measured on the 48-slide
    ambiguous deck (181 tags): the base built from here is equal, field for field, to the base
    built from reading the deck again, whose revisionId is the one the batch already gave us. So
    the read is not made (it is most of a second, at the end of a conversion where nothing else is
    left to overlap it with)."""
    titles: dict[str, str] = {}
    for r in reqs:
        alt = r.get("updatePageElementAltText")
        title = None if alt is None else alt.get("title")
        if alt is not None and title is not None:
            titles[alt["objectId"]] = title
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


def save_local(base: Mapping[str, object], out: Path) -> Path:
    """Written to a temporary file and moved into place: a process killed while the base is being
    written leaves the previous one, never half of a file (a truncated base is no base at all)."""
    path = local_path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing")
    tmp.write_text(json.dumps(base, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def base_problem(data: Json, pid: str | None) -> str | None:
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


def read_local(out: Path | None, pid: str | None) -> tuple[JsonObject | None, str | None]:
    """(base, why not) of the local cache: a missing, truncated or foreign file is no base."""
    if out is None or not local_path(out).exists():
        return None, None
    try:
        data: Json = json.loads(local_path(out).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"{local_path(out)} could not be read ({type(e).__name__}: {e})"
    problem = base_problem(data, pid)
    if problem or not isinstance(data, dict):
        return None, f"{local_path(out)}: {problem}"
    return data, None


DECK_FACTS = "name,parents,appProperties"


def deck_info(drive: DriveService, pid: str) -> DriveFile:
    """The presentation's name, parents and appProperties: everything `load_drive` and `save_drive`
    ask Drive before they can touch the base, and nothing a sync does changes it. A caller that
    reads it once and hands it on (`sync`) pays that round trip once instead of four times."""
    return execute(drive.files().get(fileId=pid, fields=DECK_FACTS))


def base_file(info: DriveFile) -> str | None:
    """The id of the base file the deck names in its appProperties (None: none yet)."""
    return (info.get("appProperties") or {}).get(BASE_PROPERTY) or None


def save_drive(drive: DriveService, base: Mapping[str, object], title: str | None, info: DriveFile | None) -> str:
    """The base as a JSON file next to the presentation (drive.file scope), its id in the
    presentation's appProperties.b2sBase. Returns the file id.

    `info`: the presentation's name, parents and appProperties, where a caller has already read
    them (`deck_info` - they need nothing but the id, so a caller may fetch them while it is doing
    something else: `snapshot_after_convert`). A base file created here is written back into it, so
    a caller that stores the base several times keeps naming the same file."""
    from .gapi import media_upload

    pid = base["presentationId"]
    if not isinstance(pid, str):
        raise JsonShapeError(f"base.presentationId: a string was expected, found {type(pid).__name__}")
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


def load_drive(drive: DriveService, pid: str, info: DriveFile | None) -> Json:
    """The base file the deck names, as JSON (None: none, or it could not be read)."""
    try:
        if info is None:
            info = execute(drive.files().get(fileId=pid, fields="appProperties"))
        fid = base_file(info)
        if not fid:
            return None
        data = execute(drive.files().get_media(fileId=fid))
        parsed: Json = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        return parsed
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
              info: DriveFile | None) -> tuple[JsonObject | None, str]:
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

    def swept(base: JsonObject | None) -> JsonObject | None:
        """A `cleanup` list the deck says has been carried out names nothing (`mark_cleaned`)."""
        done = None if info is None else (info.get("appProperties") or {}).get(CLEANED_PROPERTY)
        if base is not None and done and str(base.get("generation", 0)) == done:
            base.pop("cleanup", None)
        return base

    def generation(base: JsonObject) -> int:
        return as_int(base.get("generation", 0), "base.generation")

    stored = load_drive(drive, pid, info) if drive is not None else None
    remote: JsonObject | None = None
    if stored is not None:
        # (judged before it is swept: a file that is no JSON object is reported, never read into)
        problem = base_problem(stored, pid)
        if problem or not isinstance(stored, dict):
            problems.append(f"the base stored in Drive was ignored: {problem}")
        else:
            remote = swept(stored)
    local, why = read_local(out, pid)
    local = swept(local)
    if why:
        problems.append(f"the local base was ignored: {why}")
    if remote is not None and local is not None and generation(local) > generation(remote):
        problems.append(f"the base in Drive is older than the local one (generation {remote.get('generation', 0)} vs "
                        f"{local.get('generation', 0)}): syncing from the local one and storing it in Drive again")
        return local, "local"
    if remote is not None:
        return remote, "drive"
    if local is not None:
        return local, "local"
    return None, "none"


def store_base(base: Mapping[str, object], out: Path, drive: DriveService | None, label: str, info: DriveFile | None) -> str | None:
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


def base_matches(base: JsonObject, theirs: JsonObject) -> bool:
    """Whether the base describes this live deck at all. A base whose slides are all gone means the
    deck was copied or rebuilt behind our back: syncing would report every element as deleted in the
    deck and keep nothing."""
    known = [sid for b in as_objects(base.get("slides", []), "base.slides")
             if (sid := as_optional_str(b.get("objectId"), "base slide objectId"))]
    if not known:
        return True
    live = {as_str(s["objectId"], "read slide objectId") for s in as_objects(theirs.get("slides", []), "read.slides")}
    return any(sid in live for sid in known)


def snapshot_after_convert(deck: JsonObject, out: Path, state: EmitState, pdf: "Path | str | JsonObject | None",
                           overlays: str, problems: list[str] | None) -> JsonObject:
    """Tag the new deck's objects and record the base (convert's last step). `deck`: the deck as
    emit wrote it (`Emitted.deck`), `state` emit.json's; `pdf`: the source, or what `source_info`
    already measured of it; `overlays`: which overlay steps the deck was made from ("last").

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

    slides, drive = slides_service(None), drive_service(None)
    pid = state.presentation_id
    written = [written_of(s) for s in state.slides]
    pool = ThreadPoolExecutor(3, thread_name_prefix="b2s-base")
    # Where the base file goes needs nothing but the id, so it is looked up while the deck is
    # being read and tagged, on a thread with a client of its own (`save_drive`'s `info`).
    where_to_put_it = None
    if not shared_service("drive", "v3"):
        creds = credentials_for_threads()  # here: a worker thread inherits no context
        where_to_put_it = pool.submit(lambda: deck_info(drive_service(creds), pid))
    # Every picture was uploaded from a file here, so it is signed from that file
    # (`upload_signatures`), decoded while the deck is read; only one Google may have reshaped is
    # downloaded, while the tags are written: they hang off contentUrls, not off a Google client -
    # only the fetcher, resolved here (`net`). What no download brought is exported afterwards,
    # on this thread.
    files = pool.submit(sign_files, uploaded_files(deck, out))
    fetch = fetcher_for_threads()

    def sign(read: Presentation) -> tuple[dict[str, str], dict[str, str]]:
        local = upload_signatures(read, converted_files(deck, out, written, read), files.result())
        try:
            return local, picture_signatures(read, PICTURE_WORKERS, fetch, local, None)
        except Exception:  # noqa: BLE001 (left unsigned: exported below)
            return local, {}

    pres = execute(slides.presentations().get(presentationId=pid))
    signing = pool.submit(sign, pres)
    pool.shutdown(wait=False)
    base = converted_base(deck, out, pres, written, state.scale, pdf, False, overlays, None)
    landed, revision = write_tags(slides, pid, tag_requests(base))
    if landed:
        pres = tagged(pres, landed, revision)  # what a second read would say, measured (`tagged`)
    local, downloaded = signing.result()
    signatures = {**local, **downloaded}
    images, backgrounds = picture_urls(pres)
    unsigned = [i for i in {**images, **backgrounds} if i not in signatures]
    if unsigned:  # (their downloads failed already: straight to the export)
        exported = LivePictures(pres, drive, fetcher_for_threads(), PICTURE_WORKERS, None, slides).export()
        signatures.update({i: sig for i in unsigned if (d := exported.get(i)) and (sig := signature(d))})
    base = converted_base(deck, out, pres, written, state.scale, pdf, True, overlays, signatures)
    # What convert wrote on the master and the layouts, so a sync can carry a new theme there and
    # tell a person's layout edits from its own (theme_sync). A deck without it syncs as before.
    try:
        from . import theme_sync
        theme = theme_sync.record(deck, out, pres, emit_state_json(state))
        if theme is not None:
            base["theme"] = theme_sync.theme_json(theme)
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
