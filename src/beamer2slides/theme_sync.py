"""The deck's theme in a sync: the master background, the theme decoration pictures on the layouts
and the look of the layouts' title and body placeholders (docs/sync.md "Layouts and the master").

`convert` puts the most common slide background on the master (or only its ground colour, when
the rest of it is decoration), the decoration shared by the slides onto every layout as a
full-page picture (`emit.plan_theme`), and the deck's title and body style into the layouts'
placeholders (`emit.style_layout_placeholders`), so a slide added in Slides looks like the others.
None of that is on a slide, so a sync that only merged slides left a retheme half done: the old
title bar, a layout picture, drawn over slides whose titles were in the new colours.

It is merged three ways like everything else. The base records what convert (or the last sync)
wrote there - per layout page, the decoration picture and each placeholder's style, and their
read-back - (`record`), the new PDF says what a fresh conversion would write (`ours_side`), and
the live deck says whether a person changed it since. What only the source changed is written;
what the person changed and the source did not stays; where both did, the deck's version stays
and a conflict says so (`plan`). What was not written stays in the base as it was, so the same
conflict comes back at the next sync rather than being forgotten.

A layout serves every slide on it, and the API cannot move a slide to another layout. Each layout
page takes the decoration a fresh conversion gives most of the slides on it; a slide that should
show another one is a warning. A layout nobody's slide uses keeps the decoration of its kind.

The base's `theme` is a `ThemeRecord`, parsed where it is read (`theme_record`) and written back
to the same JSON (`theme_json`); the new side is a `ThemeSide`, the plan a `ThemeMerge`."""

from __future__ import annotations

import io
import json
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Union

from . import identity, snapshot
from .emit_model import dict_of, maps_of, objects_of
from .google_types import (LayoutProperties, Page, PageElement, Presentation, SlideProperties, as_json, background_url,
                           image_url, object_id, part, parts)
from .json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects, as_optional_str, as_str
from .merge import conflict_entry
from .sync_model import JsonMap, ObjectId, ReadBack
from .typing_compat import assert_never

if TYPE_CHECKING:
    from .deck_pictures import LivePictures
    from .emit_metrics import FontMapper
    from .emit_theme import BgKey, PlaceholderKind

DECORATION = "Theme decoration"   # the alt text emit gives the decoration picture (emit._add_decoration)
BOX_TOL = 0.5                     # pt: a layout object this far from where it was has been moved
THUMB = (32, 18)                  # colour thumbnail of a decoration picture, compared with a tolerance
THUMB_MAX, THUMB_MEAN = 12, 1.0   # levels: two thumbnails further apart than this are two pictures
PLACEHOLDERS: tuple[PlaceholderKind, ...] = ("TITLE", "CENTERED_TITLE", "BODY")
STYLE_FIELD: dict[PlaceholderKind, str] = {"TITLE": "title style", "CENTERED_TITLE": "title page title style",
                                           "BODY": "body style"}
TEXTS_FIELD = "header and footer"


# ---------------------------------------------------------------- the base's record

@dataclass(frozen=True, kw_only=True)
class PictureId:
    """What says which picture a decoration is (`picture_id`)."""
    sha1: str
    thumb: str
    signature: str | None


@dataclass(frozen=True, kw_only=True)
class LayoutRead:
    """The part of a layout object's read-back a person's edit shows in (`slim`)."""
    box: tuple[float, ...] | None
    style: str
    content_hash: str | None


@dataclass(frozen=True, kw_only=True)
class Decoration:
    """A layout's decoration picture: its object, which picture convert put there, and its read-back
    (None only where a sync created one the deck read after it did not show)."""
    oid: ObjectId
    picture: PictureId | None
    readback: LayoutRead | None


@dataclass(frozen=True, kw_only=True)
class PlaceholderEntry:
    """A master or layout placeholder: its kind, the style written into it (emit.layout_style_spec's
    entry, JSON: it goes back into requests) and its read-back."""
    kind: PlaceholderKind
    spec: JsonObject
    readback: LayoutRead


@dataclass(frozen=True, kw_only=True)
class PageEntry:
    """One layout or master page as the base records it. `group`: "master", or the decoration group
    the layout serves (`group_of`)."""
    name: str
    group: str
    decoration: Decoration | None
    placeholders: dict[ObjectId, PlaceholderEntry]


@dataclass(frozen=True, kw_only=True)
class MasterEntry:
    """The master page and its background as `snapshot.background_of` reads it (with the picture's
    `signature` when convert put a picture there): JSON, compared by `snapshot.same_background`."""
    object_id: ObjectId
    readback: JsonObject


@dataclass(frozen=True, kw_only=True)
class LayoutText:
    """One header or footer box on a layout (`live_texts`)."""
    page: ObjectId
    text: str
    read: LayoutRead


@dataclass(frozen=True, kw_only=True)
class TextsEntry:
    """The layout texts as convert wrote them (`texts_entry`): their digest, what they say, and the
    deck's boxes after the write."""
    digest: str
    says: tuple[str, ...]
    objects: dict[ObjectId, LayoutText]


@dataclass(frozen=True, kw_only=True)
class ThemeRecord:
    """The base's `theme`: what convert or the last sync wrote on the master and the layouts.
    `fill` / `shared`: emit's background keys as "color:#fff" / "png:<sha1>". `texts`: None in a
    base older than the header and footer's sync (`plan_texts`)."""
    fill: str | None
    shared: str | None
    master: MasterEntry
    pages: dict[ObjectId, PageEntry]
    texts: TextsEntry | None


def _number(v: Json, where: str) -> float:
    # (an int stays an int: the base is written back byte for byte)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected")


def _numbers(v: Json, where: str) -> tuple[float, ...]:
    return tuple(_number(x, f"{where}[{i}]") for i, x in enumerate(as_array(v, where)))


def _json_numbers(xs: Sequence[float]) -> list[Json]:
    return [x for x in xs]


def _json_strs(xs: Sequence[str]) -> list[Json]:
    return [x for x in xs]


def _placeholder_kind(v: Json) -> PlaceholderKind | None:
    for kind in PLACEHOLDERS:
        if v == kind:
            return kind
    return None


def picture_id_of(v: Json, where: str) -> PictureId:
    m = as_object(v, where)
    return PictureId(sha1=as_str(m["sha1"], f"{where}.sha1"), thumb=as_str(m["thumb"], f"{where}.thumb"),
                     signature=as_optional_str(m.get("signature"), f"{where}.signature"))


def picture_id_json(p: PictureId) -> JsonObject:
    return {"sha1": p.sha1, "thumb": p.thumb, "signature": p.signature}


def layout_read(v: Json, where: str) -> LayoutRead:
    m = as_object(v, where)
    box = m.get("box")
    return LayoutRead(box=None if box is None else _numbers(box, f"{where}.box"),
                      style=as_str(m["style"], f"{where}.style"),
                      content_hash=as_optional_str(m.get("contentHash"), f"{where}.contentHash"))


def layout_read_json(r: LayoutRead) -> JsonObject:
    return {"box": None if r.box is None else _json_numbers(r.box), "style": r.style, "contentHash": r.content_hash}


def _decoration(v: Json, where: str) -> Decoration | None:
    if v is None:
        return None
    m = as_object(v, where)
    picture, readback = m.get("picture"), m.get("readback")
    return Decoration(oid=ObjectId(as_str(m["oid"], f"{where}.oid")),
                      picture=None if picture is None else picture_id_of(picture, f"{where}.picture"),
                      readback=None if readback is None else layout_read(readback, f"{where}.readback"))


def _decoration_json(d: Decoration | None) -> Json:
    if d is None:
        return None
    return {"oid": d.oid, "picture": None if d.picture is None else picture_id_json(d.picture),
            "readback": None if d.readback is None else layout_read_json(d.readback)}


def _placeholder_entry(v: Json, where: str) -> PlaceholderEntry:
    m = as_object(v, where)
    kind = _placeholder_kind(m["kind"])
    if kind is None:
        raise JsonShapeError(f"{where}.kind: one of {PLACEHOLDERS} was expected")
    return PlaceholderEntry(kind=kind, spec=as_object(m["spec"], f"{where}.spec"),
                            readback=layout_read(m["readback"], f"{where}.readback"))


def _page_entry(v: Json, where: str) -> PageEntry:
    m = as_object(v, where)
    return PageEntry(name=as_str(m["name"], f"{where}.name"), group=as_str(m["group"], f"{where}.group"),
                     decoration=_decoration(m.get("decoration"), f"{where}.decoration"),
                     placeholders={ObjectId(oid): _placeholder_entry(p, f"{where}.placeholders[{oid}]")
                                   for oid, p in as_object(m["placeholders"], f"{where}.placeholders").items()})


def _page_entry_json(p: PageEntry) -> JsonObject:
    return {"name": p.name, "group": p.group, "decoration": _decoration_json(p.decoration),
            "placeholders": {oid: {"kind": ph.kind, "spec": ph.spec, "readback": layout_read_json(ph.readback)}
                             for oid, ph in p.placeholders.items()}}


def _layout_text(v: Json, where: str) -> LayoutText:
    m = as_object(v, where)
    return LayoutText(page=ObjectId(as_str(m["page"], f"{where}.page")), text=as_str(m["text"], f"{where}.text"),
                      read=layout_read(m, where))


def _layout_text_json(t: LayoutText) -> JsonObject:
    return {"page": t.page, "text": t.text, **layout_read_json(t.read)}


def texts_record(v: Json, where: str) -> TextsEntry:
    m = as_object(v, where)
    return TextsEntry(digest=as_str(m["digest"], f"{where}.digest"),
                      says=tuple(as_str(s, f"{where}.says") for s in as_array(m["says"], f"{where}.says")),
                      objects={ObjectId(oid): _layout_text(t, f"{where}.objects[{oid}]")
                               for oid, t in as_object(m["objects"], f"{where}.objects").items()})


def texts_json(t: TextsEntry) -> JsonObject:
    return {"digest": t.digest, "says": _json_strs(t.says),
            "objects": {oid: _layout_text_json(o) for oid, o in t.objects.items()}}


def theme_record(v: Json, where: str) -> ThemeRecord:
    """A base's `theme` (`record`, `new_record` wrote it)."""
    m = as_object(v, where)
    master = as_object(m["master"], f"{where}.master")
    texts = m.get("texts")
    return ThemeRecord(
        fill=as_optional_str(m.get("fill"), f"{where}.fill"),
        shared=as_optional_str(m.get("shared"), f"{where}.shared"),
        master=MasterEntry(object_id=ObjectId(as_str(master["objectId"], f"{where}.master.objectId")),
                           readback=as_object(master["readback"], f"{where}.master.readback")),
        pages={ObjectId(pid): _page_entry(p, f"{where}.pages[{pid}]")
               for pid, p in as_object(m["pages"], f"{where}.pages").items()},
        texts=None if texts is None else texts_record(texts, f"{where}.texts"))


def theme_json(r: ThemeRecord) -> JsonObject:
    """The base's `theme` as written; `texts` only where it is recorded."""
    out: JsonObject = {"fill": r.fill, "shared": r.shared,
                       "master": {"objectId": r.master.object_id, "readback": r.master.readback},
                       "pages": {pid: _page_entry_json(p) for pid, p in r.pages.items()}}
    if r.texts is not None:
        out["texts"] = texts_json(r.texts)
    return out


# ---------------------------------------------------------------- the new side and the plan

@dataclass(frozen=True, kw_only=True)
class ThemeOurs:
    """What theme sync reads of the new conversion (`sync.build_ours`' answer, `theme_ours`): its
    deck.json, folder and plan's scale and fonts, `pairs` (ours index -> base index) and each of its
    slides' key."""
    deck: JsonObject
    out: Path
    scale: float
    fonts: FontMapper
    pairs: dict[int, int]
    keys: tuple[str, ...]


def theme_ours(ours: Mapping[str, object]) -> ThemeOurs:
    """`ThemeOurs` of `sync.build_ours`' answer."""
    from .emit import DeckPlan

    plan, out = ours["plan"], ours["out"]
    if not isinstance(plan, DeckPlan):
        raise TypeError("the new conversion carries no DeckPlan")
    if not isinstance(out, (str, Path)):
        raise TypeError("the new conversion's folder is no path")
    pairs = dict_of(ours.get("pairs") or {}, "pairs")
    # (build_ours' pairs have int keys, which the JSON view calls str)
    return ThemeOurs(deck=dict_of(ours["deck"], "deck"), out=Path(out), scale=plan.scale, fonts=plan.fonts,
                     pairs={int(k): as_int(v, "pairs") for k, v in pairs.items()},
                     keys=tuple(as_str(s["key"], "slides.key") for s in maps_of(ours["slides"], "slides")))


@dataclass(frozen=True, kw_only=True)
class SidePicture:
    """A decoration picture a fresh conversion renders: which picture, and its file."""
    picture: PictureId
    path: str


@dataclass(frozen=True, kw_only=True)
class ThemeSide:
    """What a fresh conversion of the new PDF writes on the master and the layouts (`ours_side`):
    the master's fill (and its file when a picture), the shared background, the placeholder styles
    by kind (emit.layout_style_spec, JSON), the decoration picture per group, each page's group, and
    the header and footer words (classify's `layout_texts`)."""
    fill: str
    fill_file: str | None
    shared: str | None
    spec: JsonObject
    pictures: dict[str, SidePicture | None]
    groups: dict[int, str]
    texts: list[JsonObject]


@dataclass(frozen=True, kw_only=True)
class WroteMaster:
    fill: str


@dataclass(frozen=True, kw_only=True)
class WrotePicture:
    """A decoration picture written on `page` (None: removed)."""
    page: ObjectId
    picture: PictureId | None


@dataclass(frozen=True, kw_only=True)
class WroteSpec:
    page: ObjectId
    spec: JsonObject


@dataclass(frozen=True, kw_only=True)
class WroteTexts:
    digest: str
    says: tuple[str, ...]


Wrote = Union[WroteMaster, WrotePicture, WroteSpec, WroteTexts]
"""What went out, for `new_record`: keyed "master", the object's id, or `TEXTS_FIELD`."""

Stage = Literal["background"]


@dataclass(frozen=True, kw_only=True)
class ThemeMerge:
    """What to write on the master and the layouts, and what to say about it (`plan`).

    `requests` go before any slide's, `cleanup` (object ids) is deleted last, `stage` (picture file
    -> None | "background") goes to the staging deck. `page_group`: layout page id -> the group it
    serves now. `written`: what went out, for `new_record`. `pending`: placeholder id -> the style
    written, `TEXTS_FIELD` -> the texts' digest (an interrupted run's). `pinned`: slide objects
    given their inherited style explicitly (`inherited_pins`). The containers fill while planning."""
    requests: list[JsonObject]
    cleanup: list[str]
    stage: dict[str, Stage | None]
    applied: list[JsonObject]
    conflicts: list[JsonObject]
    warnings: list[str]
    page_group: dict[str, str]
    written: dict[str, Wrote]
    pending: JsonObject
    pinned: list[str]


# ---------------------------------------------------------------- small pieces

def key_text(key: BgKey | None) -> str | None:
    """emit's background key ("color", "#fff") as the base spells it ("color:#fff")."""
    return f"{key[0]}:{key[1]}" if key else None


def group_of(layout_name: str | None) -> str:
    """The decoration group a b2s layout name belongs to (emit.plan_theme): "TITLE" for the title
    layout, "*" for every other, with the variant suffix of a copy ("TITLE_ONLY_V1" -> "*_V1")."""
    from .emit import VARIANT
    name = layout_name or ""
    kind, n = name, ""
    if VARIANT in name and name.rsplit(VARIANT, 1)[1].isdigit():
        kind, n = name.rsplit(VARIANT, 1)
    return ("TITLE" if kind == "TITLE" else "*") + (f"{VARIANT}{n}" if n else "")


def main_group(group: str) -> str:
    from .emit import VARIANT
    return group.split(VARIANT, 1)[0]


def picture_id(path: Path) -> PictureId:
    """What says which picture a decoration is: its bytes' digest, a colour thumbnail compared with
    a tolerance (a render of the same theme need not be byte for byte the same), and the pixel
    signature a live copy of it is compared with (Google re-encodes what it is given)."""
    from PIL import Image

    data = Path(path).read_bytes()
    with Image.open(io.BytesIO(data)) as img:
        rgba = img.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    small = Image.alpha_composite(white, rgba).convert("RGB").resize(THUMB, Image.Resampling.BOX)
    return PictureId(sha1=identity.sha1(data), thumb=small.tobytes().hex(), signature=snapshot.signature(data))


def same_picture_id(a: PictureId | None, b: PictureId | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if a.sha1 == b.sha1:
        return True
    pa, pb = bytes.fromhex(a.thumb), bytes.fromhex(b.thumb)
    if not pa or len(pa) != len(pb):
        return False
    d = [abs(x - y) for x, y in zip(pa, pb)]
    return max(d) <= THUMB_MAX and sum(d) / len(d) <= THUMB_MEAN


def _norm(v: Json) -> Json:
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def same_spec(a: Json, b: Json) -> bool:
    """Two placeholder styles (emit.layout_style_spec entries) write the same thing."""
    return json.dumps(_norm(a), sort_keys=True) == json.dumps(_norm(b), sort_keys=True)


def spec_for(spec: JsonMap, kind: PlaceholderKind) -> JsonObject | None:
    """The style emit.layout_style_spec writes into a kind of placeholder (None: none)."""
    v = spec.get(kind)
    return None if v is None else as_object(v, f"spec.{kind}")


def text_elements(e: PageElement) -> list[JsonObject]:
    """A shape's `text.textElements` (none: no shape, or no text)."""
    shape = part(e.get("shape"), "shape")
    return parts(part(shape.get("text"), "shape.text").get("textElements"), "shape.text.textElements")


def placeholder(e: PageElement) -> JsonObject:
    """A shape's `placeholder` (`{}`: it is none)."""
    return part(part(e.get("shape"), "shape").get("placeholder"), "shape.placeholder")


def style_hash(e: PageElement) -> str:
    """The styles of every run and paragraph of a page element's text. A layout placeholder holds
    nothing but a newline per list level, which the slides' `text_style_hash` leaves out (a run of
    newlines is no text), and that is exactly where a person's restyling of a layout lands."""
    styles: list[Json] = []
    for te in text_elements(e):
        if "textRun" in te:
            run = part(te["textRun"], "textRun")
            styles.append(["run", snapshot._text_style(part(run.get("style"), "textRun.style"))])
        elif "paragraphMarker" in te:
            styles.append(["paragraph", snapshot._paragraph_style(part(te["paragraphMarker"], "paragraphMarker"))])
    return identity.sha1(json.dumps(styles, sort_keys=True))[:12]


def slim(rb: ReadBack, e: PageElement) -> LayoutRead:
    """The part of a layout object's read-back a person's edit shows in (`e`: the element as read)."""
    return LayoutRead(box=rb.box, style=style_hash(e), content_hash=rb.image.content_hash if rb.image else None)


def moved(a: Sequence[float] | None, b: Sequence[float] | None) -> bool:
    return a is None or b is None or max(abs(x - y) for x, y in zip(a, b)) > BOX_TOL


def _rounded(box: Sequence[float] | None) -> list[Json]:
    return [round(v, 1) for v in box or ()]


def texts_digest(texts: Sequence[JsonObject]) -> str:
    """What classify's `layout_texts` (the header and footer words every slide shares, which
    `emit.write_layout_texts` puts on every layout) say and look like."""
    said: list[Json] = [t for t in texts]
    return identity.sha1(json.dumps(identity.normalise_ir(said), sort_keys=True))[:12]


def texts_says(texts: Sequence[JsonObject]) -> list[str]:
    return [identity.plain_text(t) for t in texts]


def live_texts(pres: Presentation) -> dict[ObjectId, LayoutText]:
    """The layout texts on the deck's layouts, by object id."""
    from .emit import LAYOUT_TEXT_PREFIX
    out: dict[ObjectId, LayoutText] = {}
    for page in pres.get("layouts", []):
        objects = snapshot.read_slide_of(page).objects
        for e in page.get("pageElements", []):
            oid = ObjectId(object_id(e))
            if oid.startswith(LAYOUT_TEXT_PREFIX) and oid in objects:
                rb = objects[oid]
                out[oid] = LayoutText(page=ObjectId(object_id(page)), text=rb.text or "", read=slim(rb, e))
    return out


def texts_entry(texts: Sequence[JsonObject], pres: Presentation) -> TextsEntry:
    """The base's record of the layout texts: what they said and what the deck held after writing them."""
    return TextsEntry(digest=texts_digest(texts), says=tuple(texts_says(texts)), objects=live_texts(pres))


def texts_edited(was: Mapping[ObjectId, LayoutText], now: Mapping[ObjectId, LayoutText]) -> list[str]:
    """How the deck's layout texts differ from what convert (or the last sync) left there."""
    out: list[str] = []
    for oid in sorted(set(was) | set(now)):
        a, b = was.get(oid), now.get(oid)
        if a is None:
            out.append(f"{oid} added")
        elif b is None:
            out.append(f"{oid} deleted")
        elif a.text.rstrip("\n") != b.text.rstrip("\n"):
            out.append(f"{oid} retyped: {b.text.strip()!r}")
        elif moved(a.read.box, b.read.box):
            out.append(f"{oid} moved to {_rounded(b.read.box)}")
        elif a.read.style != b.read.style:
            out.append(f"{oid} restyled")
    return out


def page_name(page: Page) -> str:
    props = page.get("layoutProperties") or LayoutProperties()
    return props.get("displayName") or props.get("name") or object_id(page)


def _get(v: Json, key: str) -> Json:
    return v.get(key) if isinstance(v, dict) else None


def spec_says(spec: JsonMap | None) -> Json:
    """A placeholder style as a report shows it."""
    if not spec:
        return None
    style = spec.get("style") or {}
    colour = _get(_get(_get(style, "foregroundColor"), "opaqueColor"), "rgbColor")
    out: JsonObject = {
        "font": _get(_get(style, "weightedFontFamily"), "fontFamily") or _get(style, "fontFamily"),
        "size": _get(_get(style, "fontSize"), "magnitude"),
        "color": "#" + "".join(f"{round(_number(colour.get(c, 0), 'rgbColor') * 255):02x}" for c in ("red", "green", "blue"))
        if isinstance(colour, dict) and colour else None,
        "align": spec.get("align")}
    box = spec.get("box")
    if box:
        out["box"] = _rounded(_numbers(box, "spec.box"))
    return out


def picture_says(pid: PictureId | None, path: str | None) -> str:
    if pid is None:
        return "no theme picture"
    return f"theme picture {pid.sha1[:10]}" + (f" ({Path(path).name})" if path else "")


# ---------------------------------------------------------------- what convert wrote

def layout_groups(pres: Presentation, slide_groups: Mapping[str, list[str]]) -> dict[str, str]:
    """layout page id -> the decoration group it carries: the one most slides on it have, else
    (a layout no slide uses) the one emit gives its kind."""
    out: dict[str, str] = {}
    for layout in pres.get("layouts", []):
        groups = slide_groups.get(object_id(layout))
        if groups:
            out[object_id(layout)] = Counter(groups).most_common(1)[0][0]
        else:
            name = (layout.get("layoutProperties") or LayoutProperties()).get("name")
            out[object_id(layout)] = "TITLE" if name == "TITLE" else "*"
    return out


def page_entry(page: Page, group: str, picture: PictureId | None, spec: JsonMap) -> PageEntry:
    """One layout or master page as the base records it."""
    objects = snapshot.read_slide_of(page).objects
    decoration: Decoration | None = None
    placeholders: dict[ObjectId, PlaceholderEntry] = {}
    for e in page.get("pageElements", []):
        oid = ObjectId(object_id(e))
        if "image" in e and (e.get("description") or "") == DECORATION and decoration is None:
            decoration = Decoration(oid=oid, picture=picture, readback=slim(objects[oid], e))
        kind = _placeholder_kind(placeholder(e).get("type"))
        written = None if kind is None else spec_for(spec, kind)
        if kind is not None and written is not None:
            placeholders[oid] = PlaceholderEntry(kind=kind, spec=written, readback=slim(objects[oid], e))
    return PageEntry(name=page_name(page), group=group, decoration=decoration, placeholders=placeholders)


def slide_layouts(pres: Presentation) -> dict[str, str | None]:
    """slide id -> the id of the layout it is on."""
    return {object_id(s): (s.get("slideProperties") or SlideProperties()).get("layoutObjectId")
            for s in pres.get("slides", [])}


@dataclass(frozen=True, kw_only=True)
class EmittedTheme:
    """emit.json's `theme` (emit.build_deck): the master's colour, each group's decoration file
    (relative to the out folder) and each page's layout name (keyed by the page as text)."""
    master: str | None
    decorations: dict[str, str | None]
    layouts: dict[str, str]


def emitted_theme(v: object) -> EmittedTheme | None:
    if not v:
        return None
    m = dict_of(v, "theme")
    return EmittedTheme(master=as_optional_str(m.get("master"), "theme.master"),
                        decorations={g: as_optional_str(p, "theme.decorations")
                                     for g, p in as_object(m.get("decorations") or {}, "theme.decorations").items()},
                        layouts={k: as_str(n, "theme.layouts")
                                 for k, n in as_object(m.get("layouts") or {}, "theme.layouts").items()})


def record(deck: JsonMap, out: Path, pres: Presentation, state: Mapping[str, object]) -> ThemeRecord | None:
    """The base's `theme` after `convert`: what emit wrote on the master and the layouts of `pres`
    (the deck read after it was made). `deck` / `state`: emit's plan.deck and emit.json state."""
    from .emit import PPTX_TITLE_DY, FontMapper, layout_style_spec, master_plan_of, slide_layout

    masters = pres.get("masters")
    if not deck.get("slides") or not masters:
        return None
    st = emitted_theme(state.get("theme"))
    mp = master_plan_of(deck, out, None)
    fill: BgKey = ("color", st.master) if st and st.master else mp.fill
    given = state.get("scale")
    scale: float = given if isinstance(given, (int, float)) and not isinstance(given, bool) and given else 1.0
    spec = layout_style_spec(deck, scale, FontMapper(), PPTX_TITLE_DY, mp.ground)
    decorations: Mapping[str, str | None] = st.decorations if st else {}
    pictures = {g: (picture_id(out / p) if p else None) for g, p in decorations.items()}
    layout_of = slide_layouts(pres)
    slide_groups: dict[str, list[str]] = {}
    for s, emitted in zip(objects_of(deck["slides"], "slides"), maps_of(state.get("slides", []), "slides")):
        name = (st.layouts.get(str(s["page"])) if st else None) or slide_layout(s)[0]
        sid = as_optional_str(emitted.get("objectId"), "slides.objectId")
        lid = layout_of.get(sid) if sid is not None else None
        if lid:
            slide_groups.setdefault(lid, []).append(group_of(name))
    groups = layout_groups(pres, slide_groups)
    master = masters[0]
    readback = snapshot.background_of(master)
    if "picture" in readback and fill[0] == "png" and fill in mp.bg_file:
        readback["signature"] = picture_id(mp.bg_file[fill]).signature
    pages = {ObjectId(object_id(master)): page_entry(master, "master", None, spec)}
    for layout in pres.get("layouts", []):
        g = groups[object_id(layout)]
        pages[ObjectId(object_id(layout))] = page_entry(layout, g, pictures.get(g, pictures.get(main_group(g))), spec)
    return ThemeRecord(fill=key_text(fill), shared=key_text(mp.shared),
                       master=MasterEntry(object_id=ObjectId(object_id(master)), readback=readback), pages=pages,
                       texts=texts_entry(as_objects(deck.get("layout_texts", []), "layout_texts"), pres))


# ---------------------------------------------------------------- what the new PDF says

def ours_side(ours: ThemeOurs) -> ThemeSide:
    """What a fresh conversion of the new PDF would write on the master and the layouts."""
    from .emit import PPTX_TITLE_DY, layout_style_spec, master_plan_of, slide_layout

    deck, out = ours.deck, ours.out
    mp = master_plan_of(deck, out, "plan")
    theme = mp.theme
    spec = layout_style_spec(deck, ours.scale, ours.fonts, PPTX_TITLE_DY, mp.ground)
    decorations: Mapping[str, Path | None] = theme.decorations if theme else {}
    pictures = {g: (SidePicture(picture=picture_id(p), path=str(p)) if p else None) for g, p in decorations.items()}
    groups: dict[int, str] = {}
    for s in objects_of(deck["slides"], "slides"):
        page = as_int(s["page"], "page")
        groups[page] = group_of(theme.layouts[page] if theme else slide_layout(s)[0])
    return ThemeSide(fill=f"{mp.fill[0]}:{mp.fill[1]}",
                     fill_file=str(mp.bg_file[mp.fill]) if mp.fill in mp.bg_file else None,
                     shared=key_text(mp.shared), spec=spec, pictures=pictures, groups=groups,
                     texts=as_objects(deck.get("layout_texts", []), "layout_texts"))


def ours_picture(side: ThemeSide, group: str) -> SidePicture | None:
    pictures = side.pictures
    return pictures[group] if group in pictures else pictures.get(main_group(group))


# ---------------------------------------------------------------- the merge

def live_signature(url: str | None, oid: str | None, pictures: LivePictures | None) -> str | None:
    """The signature of a live picture of the master or a layout (`oid`: its image's id, or the
    page's for its background): read through `pictures` (`deck_pictures.LivePictures`, which falls
    back to a Drive export) when given, else downloaded from `url`."""
    if pictures is not None and oid:
        data = pictures.get([oid]).get(oid)
    else:
        data = snapshot._download(url) if url else None
    return snapshot.signature(data) if data else None


def pending_theme(base: JsonMap) -> JsonObject:
    """What an interrupted sync wrote on the layouts (`ThemeMerge.pending`, kept in the base)."""
    pending = base.get("pending")
    theme = as_object(pending, "pending").get("theme") if pending else None
    return as_object(theme, "pending.theme") if theme else {}


def plan(base: JsonMap, side: ThemeSide, ours: ThemeOurs, pres: Presentation, tok: str,
         picture_url: Callable[[str], str], new_id: Callable[[str], str], pictures: LivePictures | None) -> ThemeMerge:
    """What to write on the master and the layouts, and what to say about it (`ThemeMerge`).

    `base`: the sync base, its `theme` recorded; `ours`: build_ours' answer (its slides are paired
    with the base's); `pres`: the live deck; `picture_url(path)`: the staging URL (or its marker) of
    a local picture; `new_id(page)`: an object id for a decoration picture this sync creates;
    `pictures`: how a live picture whose URL changed is read (`live_signature`)."""
    rec = theme_record(base["theme"], "the sync base's theme")
    base_slides = as_objects(base["slides"], "the sync base's slides")
    out = ThemeMerge(requests=[], cleanup=[], stage={}, applied=[], conflicts=[], warnings=[], page_group={},
                     written={}, pending={}, pinned=[])
    pending = pending_theme(base)
    styling: dict[str, JsonMap] = {}   # placeholder id -> the spec this run writes
    pages = {object_id(p): p for p in pres.get("masters", [])[:1] + pres.get("layouts", [])}

    # ---- which decoration each layout serves now
    live_layout = slide_layouts(pres)
    ours_pages = [as_int(s["page"], "page") for s in objects_of(ours.deck["slides"], "slides")]
    on_layout: dict[str, list[tuple[str, str]]] = {}   # layout -> [(slide key, ours group)]
    for j, i in ours.pairs.items():
        sid = as_optional_str(base_slides[i].get("objectId"), "slides.objectId")
        lid = live_layout.get(sid) if sid is not None else None
        if lid:
            on_layout.setdefault(lid, []).append((ours.keys[j], side.groups[ours_pages[j]]))
    for pid, entry in rec.pages.items():
        if entry.group == "master" or pid not in pages:
            continue
        here = on_layout.get(pid)
        g = Counter(x for _, x in here).most_common(1)[0][0] if here else entry.group
        out.page_group[pid] = g
        for skey, sg in here or []:
            a, b = ours_picture(side, sg), ours_picture(side, g)
            if sg != g and not same_picture_id(a.picture if a else None, b.picture if b else None):
                out.warnings.append(
                    f"slide {skey}: the new version shows another theme decoration on this slide than on the "
                    f"others of its layout ({entry.name}), which a fresh conversion puts on a copy of that layout; "
                    f"a sync cannot move a live slide to another layout, so it shows its layout's decoration")

    def conflict(where: str, element: str | None, field: str, b: Json, o: Json, t: Json) -> None:
        entry, _ = conflict_entry(None, where, element, field, b, o, t, "deck kept", False)
        out.conflicts.append(entry)

    # ---- the master's background
    live_master = pages.get(rec.master.object_id)
    if live_master is not None and side.fill != rec.fill:
        now = snapshot.background_of(live_master)
        was = rec.master.readback
        edited = not snapshot.same_background(was, now)
        signed = was.get("signature")
        if edited and "picture" in now and "picture" in was and signed:
            url = background_url(live_master)
            edited = not snapshot.signatures_match(as_str(signed, "master.readback.signature"),
                                                   live_signature(url, object_id(live_master), pictures))
        converged = side.fill.startswith("color:") and now.get("color") == side.fill[6:]
        if converged:
            out.written["master"] = WroteMaster(fill=side.fill)
        elif edited:
            conflict("master", None, "master background", rec.fill, side.fill, now)
        else:
            mid = object_id(live_master)
            if side.fill.startswith("color:"):
                from .sync import api_colour
                out.requests.append({"updatePageProperties": {
                    "objectId": mid, "fields": "pageBackgroundFill.solidFill.color",
                    "pageProperties": {"pageBackgroundFill": {"solidFill": {"color": api_colour(side.fill[6:])}}}}})
            else:
                if side.fill_file is None:
                    raise ValueError(f"the new master fill {side.fill} has no file")
                out.stage[side.fill_file] = "background"
                out.requests.append({"updatePageProperties": {
                    "objectId": mid, "fields": "pageBackgroundFill.stretchedPictureFill.contentUrl",
                    "pageProperties": {"pageBackgroundFill": {"stretchedPictureFill": {
                        "contentUrl": picture_url(side.fill_file)}}}}})
            out.written["master"] = WroteMaster(fill=side.fill)
            out.applied.append({"slide": "master", "element": None, "fields": ["master background"], "page": mid})

    # ---- each page: its decoration picture and its placeholders
    for pid, entry in rec.pages.items():
        page = pages.get(pid)
        if page is None:
            continue   # (a layout the person deleted: nothing of the theme to keep on it)
        where = "master" if entry.group == "master" else f"layout {entry.name}"
        live = {object_id(e): e for e in page.get("pageElements", [])}
        objects = snapshot.read_slide_of(page).objects
        if entry.group != "master":
            deco = entry.decoration
            was_picture = deco.picture if deco else None
            now_picture = ours_picture(side, out.page_group.get(pid, entry.group))
            if not same_picture_id(was_picture, now_picture.picture if now_picture else None):
                if deco is None and now_picture is not None:
                    # emit left this layout without a picture; the new theme has one for it
                    oid = new_id(pid)
                    out.stage[now_picture.path] = None
                    from .gslides import emu
                    w, h = snapshot.page_size(pres)
                    out.requests.extend([
                        {"createImage": {"objectId": oid, "url": picture_url(now_picture.path), "elementProperties": {
                            "pageObjectId": pid, "size": {"width": emu(w), "height": emu(h)},
                            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "EMU"}}}},
                        {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "SEND_TO_BACK"}},
                        {"updatePageElementAltText": {"objectId": oid, "description": DECORATION}}])
                    out.written[oid] = WrotePicture(page=pid, picture=now_picture.picture)
                    out.applied.append({"slide": where, "element": oid, "fields": ["theme decoration"], "page": pid})
                elif deco is not None:
                    oid = deco.oid
                    rb = objects.get(oid)
                    was_box = deco.readback.box if deco.readback else None
                    theirs: str | None = None
                    if oid not in live or rb is None:
                        theirs = "deleted"
                    elif moved(was_box, rb.box):
                        theirs = f"moved to {_rounded(rb.box)}"
                    elif (rb.image.content_hash if rb.image else None) != \
                            (deco.readback.content_hash if deco.readback else None):
                        # Google hands out new URLs for the same picture: only its pixels tell
                        sig = live_signature(image_url(live[oid]), oid, pictures)
                        if now_picture is not None and snapshot.signatures_match(sig, now_picture.picture.signature):
                            # (an interrupted sync wrote it)
                            out.written[oid] = WrotePicture(page=pid, picture=now_picture.picture)
                        elif not snapshot.signatures_match(sig, was_picture.signature if was_picture else None):
                            theirs = "another picture"
                    if oid in out.written:
                        pass
                    elif theirs is not None:
                        conflict(where, oid, "theme decoration", picture_says(was_picture, None),
                                 picture_says(now_picture.picture if now_picture else None,
                                              now_picture.path if now_picture else None), theirs)
                    elif now_picture is None:
                        out.cleanup.append(oid)
                        out.written[oid] = WrotePicture(page=pid, picture=None)
                        out.applied.append({"slide": where, "element": oid, "fields": ["theme decoration"],
                                            "how": "removed", "page": pid})
                    else:
                        out.stage[now_picture.path] = None
                        out.requests.extend([
                            {"replaceImage": {"imageObjectId": oid, "url": picture_url(now_picture.path),
                                              "imageReplaceMethod": "CENTER_INSIDE"}},
                            {"updatePageElementAltText": {"objectId": oid, "description": DECORATION}}])
                        out.written[oid] = WrotePicture(page=pid, picture=now_picture.picture)
                        out.applied.append({"slide": where, "element": oid, "fields": ["theme decoration"], "page": pid})
        for oid, ph in entry.placeholders.items():
            if oid not in live:
                continue
            now_spec = spec_for(side.spec, ph.kind)
            if now_spec is None or same_spec(ph.spec, now_spec):
                continue
            rb_ph = objects[oid]
            restyled = ph.readback.style != style_hash(live[oid])
            if moved(ph.readback.box, rb_ph.box) or restyled:
                if same_spec(pending.get(oid), now_spec):
                    out.written[oid] = WroteSpec(page=pid, spec=now_spec)   # (an interrupted sync wrote it)
                    continue
                edit: JsonObject = {}
                if moved(ph.readback.box, rb_ph.box):
                    edit["box"] = _rounded(rb_ph.box)
                if restyled:
                    edit["style"] = "restyled in the deck"
                conflict(where, oid, STYLE_FIELD[ph.kind], spec_says(ph.spec), spec_says(now_spec), edit)
                continue
            from .emit import layout_placeholder_requests
            out.requests.extend(layout_placeholder_requests(now_spec, as_json(live[oid], oid)))
            styling[oid] = now_spec
            out.written[oid] = WroteSpec(page=pid, spec=now_spec)
            out.pending[oid] = now_spec
            out.applied.append({"slide": where, "element": oid, "fields": [STYLE_FIELD[ph.kind]]})
    plan_texts(rec, side, ours, pres, pending, out)
    # After the layouts' requests, in the same batch (an interrupted run that restyled did both):
    # Slides drops a run property equal to what the run inherits, so a pin written while the
    # layout still says the same value is gone before the layout changes.
    pins, pinned = inherited_pins(pres, styling, {as_optional_str(s.get("objectId"), "slides.objectId")
                                                  for s in base_slides})
    out.pinned.extend(pinned)
    out.requests.extend(pins)
    return out


def plan_texts(rec: ThemeRecord, side: ThemeSide, ours: ThemeOurs, pres: Presentation, pending: JsonMap,
               out: ThemeMerge) -> None:
    """The header and footer words every slide shares (`\\author`, `\\title`, `\\date` in a
    footline), which convert writes once per layout (`emit.write_layout_texts`), merged three ways
    into `out` (plan's answer). Nothing merged them before: a new `\\date`, or a colour theme that
    turned the footline's words white, reached the slides' own elements and the layouts' bars but
    left the words on every slide as they were - the old theme's red on the new theme's navy
    (edit hunt h5a, 2026-09-25). They are rewritten whole, like convert does, when the source
    changed them and the deck did not; a deck whose layout texts a person edited keeps them, with a
    conflict. A base from before they were recorded cannot tell a person's edit from convert's
    words: they are left alone, with a warning when the source's words differ from the deck's."""
    new = side.texts
    now = live_texts(pres)
    says = texts_says(new)
    texts = rec.texts
    if texts is None:
        shown = sorted({t.text.strip() for t in now.values()})
        if shown != sorted({s.strip() for s in says}):
            out.warnings.append(
                "the new version's header and footer words (" + ", ".join(repr(s) for s in says if s.strip()) +
                ") differ from the ones on the deck's layouts, but the deck's sync base is older than their "
                "sync and does not record what convert wrote there: they were left as they are (edit them in "
                "Slides with View > Theme builder)")
        return
    digest = texts_digest(new)
    if digest == texts.digest:
        return
    edits = texts_edited(texts.objects, now)
    if edits and pending.get(TEXTS_FIELD) == digest:
        out.written[TEXTS_FIELD] = WroteTexts(digest=digest, says=tuple(says))   # (an interrupted sync wrote them)
        return
    if edits:
        entry, _ = conflict_entry(None, "layouts", None, TEXTS_FIELD, _json_strs(texts.says), _json_strs(says),
                                  _json_strs(edits), "deck kept", False)
        out.conflicts.append(entry)
        return
    from .emit import LAYOUT_TEXT_PREFIX, text_box_requests
    reqs: list[JsonObject] = [{"deleteObject": {"objectId": oid}} for oid in now]
    for li, layout in enumerate(pres.get("layouts", [])):
        for ti, el in enumerate(new):
            reqs += text_box_requests(el, object_id(layout), f"{LAYOUT_TEXT_PREFIX}{li}_{ti}", ours.scale, ours.fonts)
    out.requests.extend(reqs)
    out.written[TEXTS_FIELD] = WroteTexts(digest=digest, says=tuple(says))
    out.pending[TEXTS_FIELD] = digest
    out.applied.append({"slide": "layouts", "element": None, "fields": [TEXTS_FIELD]})


def _level_runs(e: PageElement) -> tuple[list[JsonObject], list[JsonObject]]:
    """A master or layout placeholder's style per list level: (run styles, paragraph styles), the
    "\\n" each level holds."""
    runs: list[JsonObject] = []
    paras: list[JsonObject] = []
    for te in text_elements(e):
        if "textRun" in te:
            runs.append(part(part(te["textRun"], "textRun").get("style"), "textRun.style"))
        elif "paragraphMarker" in te:
            paras.append(part(part(te["paragraphMarker"], "paragraphMarker").get("style"), "paragraphMarker.style"))
    return runs, paras


def _inherited(chain: list[PageElement], level: int, field: str, paragraph: bool) -> Json:
    for e in chain:
        runs, paras = _level_runs(e)
        styles = paras if paragraph else runs
        if styles:
            s = styles[min(level, len(styles) - 1)]
            if field in s:
                return s[field]
    return None


def placeholder_chain(e: PageElement, placeholders: dict[str, PageElement]) -> list[PageElement]:
    """The master and layout placeholders `e` inherits its text style from, nearest first. Each is
    taken once: parents that name each other end the chain rather than the sync."""
    chain: list[PageElement] = []
    seen: set[str] = set()
    parent = placeholder(e).get("parentObjectId")
    while isinstance(parent, str) and parent in placeholders and parent not in seen:
        seen.add(parent)
        chain.append(placeholders[parent])
        parent = placeholder(placeholders[parent]).get("parentObjectId")
    return chain


def inherited_pins(pres: Presentation, restyled: Mapping[str, JsonMap],
                   slide_ids: Collection[str | None]) -> tuple[list[JsonObject], list[str]]:
    """Requests that write onto the converter's slides, explicitly, the style their placeholder
    text now takes from a master or layout placeholder this sync restyles (`restyled`: object id
    -> the spec written), and the ids of the objects they touch.

    Google's .pptx import drops a run property equal to what the placeholder inherits: beamer
    sets the title page's title and the frame titles at one size, so the title page's title
    comes out with no size of its own and would take a retheme's new frame-title size - which a
    fresh conversion, whose .pptx says the old size, does not. The values are those rendered
    before the sync and go out after the layout requests (Slides drops a property equal to the
    inherited one, so written earlier they would vanish): the slide looks the same, and what its
    element says is left to the element's own merge. A frame title the merge refills with the new
    style gets it back from the layout, as a fresh conversion's does."""
    if not restyled:
        return [], []
    placeholders = {object_id(e): e for p in pres.get("masters", []) + pres.get("layouts", [])
                    for e in p.get("pageElements", []) if placeholder(e)}
    reqs: list[JsonObject] = []
    touched: list[str] = []
    for slide in pres.get("slides", []):
        if slide.get("objectId") not in slide_ids:
            continue
        for e in slide.get("pageElements", []):
            chain = placeholder_chain(e, placeholders)
            written = [restyled[object_id(c)] for c in chain if object_id(c) in restyled]
            if not written:
                continue
            fields = list(dict.fromkeys(f for spec in written for f in as_str(spec["fields"], "fields").split(",")))
            elements = text_elements(e)
            if not elements:
                continue
            oid = object_id(e)
            end_all = as_int(elements[-1].get("endIndex", 0), "endIndex") - 1   # (the last newline is Slides' own)
            level, before = 0, len(reqs)
            for te in elements:
                a, b = as_int(te.get("startIndex", 0), "startIndex"), min(as_int(te.get("endIndex", 0), "endIndex"), end_all)
                if "paragraphMarker" in te:
                    marker = part(te["paragraphMarker"], "paragraphMarker")
                    level = as_int(part(marker.get("bullet"), "bullet").get("nestingLevel") or 0, "bullet.nestingLevel")
                    style = part(marker.get("style"), "paragraphMarker.style")
                    if "alignment" not in style and any(spec.get("align") for spec in written) and b > a:
                        value = _inherited(chain, level, "alignment", True)
                        if value:
                            reqs.append({"updateParagraphStyle": {
                                "objectId": oid, "fields": "alignment", "style": {"alignment": value},
                                "textRange": {"type": "FIXED_RANGE", "startIndex": a, "endIndex": b}}})
                elif "textRun" in te and b > a:
                    style = part(part(te["textRun"], "textRun").get("style"), "textRun.style")
                    pin: JsonObject = {f: v for f in fields if f not in style
                                       for v in [_inherited(chain, level, f, False)] if v is not None}
                    if pin:
                        reqs.append({"updateTextStyle": {
                            "objectId": oid, "fields": ",".join(pin), "style": pin,
                            "textRange": {"type": "FIXED_RANGE", "startIndex": a, "endIndex": b}}})
            if len(reqs) > before:
                touched.append(oid)
    return reqs, touched


def new_record(rec: ThemeRecord, side: ThemeSide, done: Mapping[str, Wrote], raw: Presentation | None) -> ThemeRecord:
    """The base's `theme` after a sync: what was written (`done`, plan's `written`) takes the
    source's value and the read-back after the write (`raw`: the deck read after it); everything
    else stays as the base had it - a conflict comes back next time, a deck edit stays one."""
    read = raw if raw is not None else Presentation()
    pages = {ObjectId(object_id(p)): p for p in read.get("masters", [])[:1] + read.get("layouts", [])}
    objects = {pid: {oid: slim(rb, e) for oid, rb in snapshot.read_slide_of(p).objects.items()
                     for e in p.get("pageElements", []) if object_id(e) == oid} for pid, p in pages.items()}
    fill, master, texts = rec.fill, rec.master, rec.texts
    entries = dict(rec.pages)
    for key, what in done.items():
        oid = ObjectId(key)
        match what:
            case WroteMaster():
                fill = what.fill
                page = pages.get(rec.master.object_id)
                if page is not None:
                    readback = snapshot.background_of(page)
                    if "picture" in readback and side.fill_file:
                        readback["signature"] = picture_id(Path(side.fill_file)).signature
                    master = replace(master, readback=readback)
            case WroteTexts():
                if rec.texts is not None and raw:
                    texts = TextsEntry(digest=what.digest, says=what.says, objects=live_texts(read))
            case WrotePicture():
                entry = entries.get(what.page)
                if entry is None:
                    continue
                rb = objects.get(what.page, {}).get(oid)
                if what.picture is None:
                    entries[what.page] = replace(entry, decoration=None)
                else:
                    kept = entry.decoration.readback if entry.decoration else None
                    entries[what.page] = replace(entry, decoration=Decoration(oid=oid, picture=what.picture,
                                                                              readback=rb or kept))
            case WroteSpec():
                entry = entries.get(what.page)
                if entry is None or oid not in entry.placeholders:
                    continue
                rb = objects.get(what.page, {}).get(oid)
                ph = entry.placeholders[oid]
                placeholders = dict(entry.placeholders)
                placeholders[oid] = replace(ph, spec=what.spec, readback=rb or ph.readback)
                entries[what.page] = replace(entry, placeholders=placeholders)
            case _:
                assert_never(what)
    return ThemeRecord(fill=fill, shared=side.shared, master=master, pages=entries, texts=texts)


def old_base_warning(base: JsonMap, side: ThemeSide) -> str | None:
    """A base from before theme sync records nothing of the layouts: they are left alone."""
    if side.shared == base.get("master_background"):
        return None
    return ("the new version's theme (the background most slides share) differs from the one this deck was "
            "converted with, but the deck's sync base is older than theme sync and does not record what convert "
            "wrote on the master and the layouts: they were left as they are, and a slide whose background "
            "changed got the new one as a picture of its own")
