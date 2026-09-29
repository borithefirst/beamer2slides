"""What sync's planners read, as records (docs/sync.md): the base convert and every sync write, the
new conversion's slide entries, and the read-back of the live deck.

The base is JSON on disk and in Drive, and sync still builds and rewrites it as dicts. Where a
planner reads it - `merge.plan_merge`, `identity.source_changes` - it is parsed here first, and a
key of the wrong shape is an error naming where it is, not a `KeyError` three calls later.

Each record writes back exactly the JSON it was read from (`base_json`, `deck_read_json`, ...), key
for key: a key a producer writes only when it has something to say is absent here as None, and a
number keeps its type (3 stays 3, and 3.0 stays 3.0: conflict ids and hashes are digests of JSON).
Keys none of sync's planners read are kept as JSON (`source`, `theme`, an element's `ir`, a
read-back's styles) - the base is the one place they are written from.

A key no reader here knows is left out of the record and refused by nothing: the fuzz world's bases
carry notes of its own (`group_readback`, `left_object`), and a test hands in only what it needs.
`tools` that measure the round trip (see `unread`) say when a real base holds one.

A slide key, an element key and a Slides objectId are three kinds of name: `SlideKey`,
`ElementKey`, `ObjectId`."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, NewType

from .ir_types import Box
from .json_types import Json, JsonObject, JsonShapeError

ObjectId = NewType("ObjectId", str)
"""A Slides object: a page element's or a slide's objectId."""
SlideKey = NewType("SlideKey", str)
"""A slide's identity across conversions (`identity.fresh_slide_key`): a label, `title:...#n`, `page:n`."""
ElementKey = NewType("ElementKey", str)
"""An element's identity within its slide (`identity.default_keys`): `kind/role/ordinal`."""

JsonMap = Mapping[str, Json]
"""A JSON object as the entries here take it: read-only, so a dict of narrower values is one too."""

ContextField = Literal["width", "placed", "emitted"]
"""The marks `sync.mark_emitted` gives an element emit writes differently because of its neighbours
(`identity.CONTEXT_FIELDS`)."""
CONTEXT_FIELDS: tuple[ContextField, ...] = ("width", "placed", "emitted")
OwnField = Literal["text", "position", "size", "style", "image"]
FIELD_NAMES: tuple[OwnField, ...] = ("text", "position", "size", "style", "image")
"""An element's own field hashes (`identity.ir_fields`), in the order they are written."""


# ---------------------------------------------------------------- reading JSON

def _missing(where: str, key: str) -> JsonShapeError:
    return JsonShapeError(f"{where}: the key {key!r} is missing")


def _wrong(where: str, expected: str, v: Json) -> JsonShapeError:
    found = "null" if v is None else type(v).__name__
    return JsonShapeError(f"{where}: {expected} was expected, found {found}")


def _req(m: JsonMap, key: str, where: str) -> Json:
    if key not in m:
        raise _missing(where, key)
    return m[key]


def _object(v: Json, where: str) -> JsonObject:
    if isinstance(v, dict):
        return v
    raise _wrong(where, "an object", v)


def _array(v: Json, where: str) -> list[Json]:
    if isinstance(v, list):
        return v
    raise _wrong(where, "an array", v)


def _str(v: Json, where: str) -> str:
    if isinstance(v, str):
        return v
    raise _wrong(where, "a string", v)


def _opt_str(v: Json, where: str) -> str | None:
    return None if v is None else _str(v, where)


def _int(v: Json, where: str) -> int:
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    raise _wrong(where, "an integer", v)


def _num(v: Json, where: str) -> float:
    """A number as it came: an int stays an int (a digest of the JSON tells 3 from 3.0)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise _wrong(where, "a number", v)


def _bool(v: Json, where: str) -> bool:
    if isinstance(v, bool):
        return v
    raise _wrong(where, "true or false", v)


def _flag(m: JsonMap, key: str, where: str) -> bool:
    """A key written only when true."""
    if key not in m:
        return False
    if m[key] is not True:
        raise _wrong(f"{where}.{key}", "true (written only when true)", m[key])
    return True


def _nums(v: Json, where: str) -> tuple[float, ...]:
    return tuple(_num(x, f"{where}[{i}]") for i, x in enumerate(_array(v, where)))


def _box(v: Json, where: str) -> Box:
    items = _array(v, where)
    if len(items) != 4:
        raise JsonShapeError(f"{where}: a box of 4 numbers was expected, found {len(items)}")
    return (_num(items[0], where), _num(items[1], where), _num(items[2], where), _num(items[3], where))


def _pair(v: Json, where: str) -> tuple[float, float]:
    items = _array(v, where)
    if len(items) != 2:
        raise JsonShapeError(f"{where}: 2 numbers were expected, found {len(items)}")
    return (_num(items[0], where), _num(items[1], where))


def _int_pair(v: Json, where: str) -> tuple[int, int]:
    items = _array(v, where)
    if len(items) != 2:
        raise JsonShapeError(f"{where}: 2 integers were expected, found {len(items)}")
    return (_int(items[0], where), _int(items[1], where))


def _strs(v: Json, where: str) -> tuple[str, ...]:
    return tuple(_str(x, f"{where}[{i}]") for i, x in enumerate(_array(v, where)))


def _ids(v: Json, where: str) -> tuple[ObjectId, ...]:
    return tuple(ObjectId(s) for s in _strs(v, where))


def _objects(v: Json, where: str) -> tuple[JsonObject, ...]:
    return tuple(_object(x, f"{where}[{i}]") for i, x in enumerate(_array(v, where)))


def _opt(m: JsonMap, key: str) -> Json:
    """A key's value, or None when it is absent (for keys whose null and absence read alike)."""
    return m.get(key)


# ---------------------------------------------------------------- writing JSON

def _json_nums(xs: Sequence[float]) -> list[Json]:
    return [x for x in xs]


def _json_strs(xs: Sequence[str]) -> list[Json]:
    return [x for x in xs]


def _json_objects(xs: Sequence[JsonObject]) -> list[Json]:
    return [x for x in xs]


def _put(out: JsonObject, key: str, value: Json) -> None:
    """A key written only when it says something."""
    if value is not None:
        out[key] = value


# ---------------------------------------------------------------- the read-back of one object

@dataclass(frozen=True, kw_only=True)
class ImageRead:
    """A picture's read-back (`snapshot.readback`): its contentUrl's hash, and its pixels'
    `signature` once something read them. `unchecked`: a new URL nobody read yet
    (`sync.Sync.sign_changed`)."""
    content_hash: str | None
    source_url: str | None
    signature: str | None
    unchecked: bool


def image_read(v: Json, where: str) -> ImageRead:
    m = _object(v, where)
    return ImageRead(content_hash=_opt_str(_opt(m, "contentHash"), f"{where}.contentHash"),
                     source_url=_opt_str(_opt(m, "sourceUrl"), f"{where}.sourceUrl"),
                     signature=_opt_str(_opt(m, "signature"), f"{where}.signature"),
                     unchecked=_flag(m, "unchecked", where))


def image_read_json(r: ImageRead) -> JsonObject:
    out: JsonObject = {"contentHash": r.content_hash, "sourceUrl": r.source_url}
    _put(out, "signature", r.signature)
    if r.unchecked:
        out["unchecked"] = True
    return out


RunSpan = tuple[int, int, JsonObject]
"""A run of a read-back's text: [start, end, style] in characters (`snapshot.read_text`)."""


@dataclass(frozen=True, kw_only=True)
class ReadBack:
    """One live object as sync compares it (`snapshot.readback`): pt, hex colours, the transform
    composed with its groups'. The styles are JSON, being what goes back into API requests.

    Absent where the producer writes nothing: `placeholder` (not a placeholder), `table` (not a
    table: [rows, columns]), `run_spans` (a base older than them), `image`, `children` (not a
    group), `refit` (a step `refit.reshape_base` recorded)."""
    kind: str
    transform: tuple[float, ...]
    size: tuple[float, ...]
    box: Box
    parent_group: ObjectId | None
    z: int
    title: str | None
    description: str | None
    placeholder: str | None
    table: tuple[int, int] | None
    text: str | None
    text_styles: tuple[JsonObject, ...]
    paragraph_styles: tuple[JsonObject, ...]
    run_spans: tuple[RunSpan, ...] | None
    text_style_hash: str
    shape_style: JsonObject
    shape_style_hash: str
    image: ImageRead | None
    children: tuple[ObjectId, ...] | None
    refit: tuple[float, float] | None


def _run_span(v: Json, where: str) -> RunSpan:
    items = _array(v, where)
    if len(items) != 3:
        raise JsonShapeError(f"{where}: [start, end, style] was expected")
    return (_int(items[0], where), _int(items[1], where), _object(items[2], where))


def readback(v: Json, where: str) -> ReadBack:
    return readback_of(_object(v, where), where)


def readback_of(m: JsonMap, where: str) -> ReadBack:
    """`readback` of an object already known to be one (a dict entry's argument)."""
    spans = m.get("run_spans")
    parent = _opt_str(_req(m, "parent_group", where), f"{where}.parent_group")
    return ReadBack(
        kind=_str(_req(m, "kind", where), f"{where}.kind"),
        transform=_nums(_req(m, "transform", where), f"{where}.transform"),
        size=_nums(_opt(m, "size") or [], f"{where}.size") if "size" in m else (),
        box=_box(_req(m, "box", where), f"{where}.box"),
        parent_group=None if parent is None else ObjectId(parent),
        z=_int(m["z"], f"{where}.z") if "z" in m else 0,
        title=_opt_str(_opt(m, "title"), f"{where}.title"),
        description=_opt_str(_opt(m, "description"), f"{where}.description"),
        placeholder=_opt_str(_opt(m, "placeholder"), f"{where}.placeholder"),
        table=_int_pair(m["table"], f"{where}.table") if m.get("table") is not None else None,
        text=_opt_str(_opt(m, "text"), f"{where}.text"),
        text_styles=_objects(m["text_styles"], f"{where}.text_styles") if "text_styles" in m else (),
        paragraph_styles=_objects(m["paragraph_styles"], f"{where}.paragraph_styles") if "paragraph_styles" in m else (),
        run_spans=None if spans is None else tuple(_run_span(x, f"{where}.run_spans[{i}]")
                                                   for i, x in enumerate(_array(spans, f"{where}.run_spans"))),
        text_style_hash=_str(_req(m, "text_style_hash", where), f"{where}.text_style_hash"),
        shape_style=_object(m["shape_style"], f"{where}.shape_style") if "shape_style" in m else {},
        shape_style_hash=_str(_req(m, "shape_style_hash", where), f"{where}.shape_style_hash"),
        image=image_read(m["image"], f"{where}.image") if m.get("image") is not None else None,
        children=_ids(m["children"], f"{where}.children") if m.get("children") is not None else None,
        refit=_pair(m["refit"], f"{where}.refit") if m.get("refit") is not None else None)


def readback_json(r: ReadBack) -> JsonObject:
    """In `snapshot.readback`'s order, then what `read_slide` and `refit` add."""
    out: JsonObject = {"kind": r.kind, "transform": _json_nums(r.transform), "size": _json_nums(r.size),
                       "box": _json_nums(r.box), "parent_group": r.parent_group, "z": r.z, "title": r.title,
                       "description": r.description}
    _put(out, "placeholder", r.placeholder)
    if r.table is not None:
        out["table"] = [r.table[0], r.table[1]]
    out["text"] = r.text
    out["text_styles"] = _json_objects(r.text_styles)
    out["paragraph_styles"] = _json_objects(r.paragraph_styles)
    if r.run_spans is not None:
        out["run_spans"] = [[a, b, style] for a, b, style in r.run_spans]
    out["text_style_hash"] = r.text_style_hash
    out["shape_style"] = r.shape_style
    out["shape_style_hash"] = r.shape_style_hash
    if r.image is not None:
        out["image"] = image_read_json(r.image)
    if r.children is not None:
        out["children"] = _json_strs(r.children)
    if r.refit is not None:
        out["refit"] = [r.refit[0], r.refit[1]]
    return out


def readbacks(v: Json, where: str) -> dict[ObjectId, ReadBack]:
    return {ObjectId(oid): readback(rb, f"{where}[{oid}]") for oid, rb in _object(v, where).items()}


def readbacks_json(rbs: Mapping[ObjectId, ReadBack]) -> JsonObject:
    return {oid: readback_json(rb) for oid, rb in rbs.items()}


# ---------------------------------------------------------------- the live deck

@dataclass(frozen=True, kw_only=True)
class SlideRead:
    """One live slide (`snapshot.read_slide`): its objects by id, top-level `order`, and its
    background as `snapshot.background` reads it ({picture, signature?, unchecked?} | {color} |
    {state}; JSON, compared by `snapshot.same_background`)."""
    object_id: ObjectId
    layout_object_id: str | None
    background: JsonObject | None
    notes: str
    notes_id: str | None
    order: tuple[ObjectId, ...]
    objects: dict[ObjectId, ReadBack]


def slide_read(v: Json, where: str) -> SlideRead:
    return slide_read_of(_object(v, where), where)


def slide_read_of(m: JsonMap, where: str) -> SlideRead:
    oid = _str(_req(m, "objectId", where), f"{where}.objectId")
    at = f"slide {oid}"
    bg = m.get("background")
    return SlideRead(object_id=ObjectId(oid),
                     layout_object_id=_opt_str(_opt(m, "layoutObjectId"), f"{at}.layoutObjectId"),
                     background=None if bg is None else _object(bg, f"{at}.background"),
                     notes=_str(m["notes"], f"{at}.notes") if m.get("notes") is not None else "",
                     notes_id=_opt_str(_opt(m, "notes_id"), f"{at}.notes_id"),
                     order=_ids(m["order"], f"{at}.order") if "order" in m else (),
                     objects=readbacks(_req(m, "objects", at), f"{at}.objects"))


def slide_read_json(s: SlideRead) -> JsonObject:
    return {"objectId": s.object_id, "layoutObjectId": s.layout_object_id, "background": s.background,
            "notes": s.notes, "notes_id": s.notes_id, "order": _json_strs(s.order), "objects": readbacks_json(s.objects)}


@dataclass(frozen=True, kw_only=True)
class DeckRead:
    """The live deck (`snapshot.read_presentation`), "theirs" to the merge."""
    presentation_id: str | None
    revision_id: str | None
    page_size: tuple[float, ...] | None
    layouts: JsonObject | None
    master_background: JsonObject | None
    slides: tuple[SlideRead, ...]


def deck_read(v: JsonMap) -> DeckRead:
    where = "the deck's read-back"
    size, master, layouts = v.get("page_size"), v.get("master_background"), v.get("layouts")
    return DeckRead(presentation_id=_opt_str(_opt(v, "presentationId"), f"{where}.presentationId"),
                    revision_id=_opt_str(_opt(v, "revisionId"), f"{where}.revisionId"),
                    page_size=None if size is None else _nums(size, f"{where}.page_size"),
                    layouts=None if layouts is None else _object(layouts, f"{where}.layouts"),
                    master_background=None if master is None else _object(master, f"{where}.master_background"),
                    slides=tuple(slide_read(s, f"{where}.slides[{i}]")
                                 for i, s in enumerate(_array(_req(v, "slides", where), f"{where}.slides"))))


def deck_read_json(d: DeckRead) -> JsonObject:
    return {"presentationId": d.presentation_id, "revisionId": d.revision_id,
            "page_size": None if d.page_size is None else _json_nums(d.page_size), "layouts": d.layouts,
            "master_background": d.master_background, "slides": [slide_read_json(s) for s in d.slides]}


# ---------------------------------------------------------------- an element entry

@dataclass(frozen=True, kw_only=True)
class Fingerprint:
    """What an element is recognised by across conversions (`identity.fingerprint`). `look`: a
    panel's fill (absent for everything else)."""
    text: str
    bbox: Box
    image_sha1: str | None
    anchor: ElementKey | None
    look: str | None


def fingerprint(v: Json, where: str) -> Fingerprint:
    m = _object(v, where)
    anchor = _opt_str(_opt(m, "anchor"), f"{where}.anchor")
    return Fingerprint(text=_str(_req(m, "text", where), f"{where}.text"),
                       bbox=_box(_req(m, "bbox", where), f"{where}.bbox"),
                       image_sha1=_opt_str(_opt(m, "image_sha1"), f"{where}.image_sha1"),
                       anchor=None if anchor is None else ElementKey(anchor),
                       look=_opt_str(_opt(m, "look"), f"{where}.look"))


def fingerprint_json(fp: Fingerprint) -> JsonObject:
    out: JsonObject = {"text": fp.text, "bbox": _json_nums(fp.bbox), "image_sha1": fp.image_sha1, "anchor": fp.anchor}
    _put(out, "look", fp.look)
    return out


@dataclass(frozen=True, kw_only=True)
class ElementFields:
    """An element's field hashes (`identity.ir_fields`), and the marks `sync.mark_emitted` gave it,
    in the order they were given."""
    text: str
    position: str
    size: str
    style: str
    image: str
    marks: tuple[tuple[ContextField, str], ...]

    def own(self) -> tuple[tuple[OwnField, str], ...]:
        return (("text", self.text), ("position", self.position), ("size", self.size), ("style", self.style),
                ("image", self.image))

    def mark(self, field: ContextField) -> str | None:
        return next((v for f, v in self.marks if f == field), None)


def _context_field(key: str) -> ContextField | None:
    for f in CONTEXT_FIELDS:
        if f == key:
            return f
    return None


def element_fields(v: Json, where: str) -> ElementFields:
    m = _object(v, where)
    marks: list[tuple[ContextField, str]] = []
    for key, value in m.items():
        if key in FIELD_NAMES:
            continue
        f = _context_field(key)
        if f is None:
            raise JsonShapeError(f"{where}: an unknown field {key!r}")
        if value is not None:   # (a mark is there or it is not: `identity.source_changes`)
            marks.append((f, _str(value, f"{where}.{key}")))
    return ElementFields(text=_str(_req(m, "text", where), f"{where}.text"),
                         position=_str(_req(m, "position", where), f"{where}.position"),
                         size=_str(_req(m, "size", where), f"{where}.size"),
                         style=_str(_req(m, "style", where), f"{where}.style"),
                         image=_str(_req(m, "image", where), f"{where}.image"), marks=tuple(marks))


def element_fields_json(f: ElementFields) -> JsonObject:
    out: JsonObject = {k: v for k, v in f.own()}
    for k, v in f.marks:
        out[k] = v
    return out


@dataclass(frozen=True, kw_only=True)
class Tied:
    """The objects a base element is written as (`snapshot.attach_readback`): `main` is the one
    that holds it (None: none was made), `readback` what the deck had of each."""
    objects: tuple[ObjectId, ...]
    main: ObjectId | None
    readback: dict[ObjectId, ReadBack]


@dataclass(frozen=True, kw_only=True)
class ElementEntry:
    """One element of a slide entry (`snapshot.slide_entries`): its key and hashes, its fingerprint
    and the IR they were made from (JSON: an older base's IR is an older form). `tied`: where it is
    in the deck - a base's entry has it, the new conversion's has not.

    `removed`: a unit kept though the source dropped it (None: the entry never said). The adopt
    marks: `drawn_from` (the member whose box it came out of), `from_layout` (the deck's layout
    draws it), `in_table` (a cell of the deck's own table) - `adopt_sync.build_base`."""
    key: ElementKey
    id: str
    kind: str
    role: str | None
    ir_hash: str
    fields: ElementFields
    fingerprint: Fingerprint
    anchor: ElementKey | None
    ir: JsonObject | None
    tied: Tied | None
    removed: bool | None
    table_margins: tuple[tuple[float, ...], ...] | None
    drawn_from: ElementKey | None
    from_layout: bool
    in_table: bool

    @property
    def main(self) -> ObjectId | None:
        return None if self.tied is None else self.tied.main

    @property
    def objects(self) -> tuple[ObjectId, ...]:
        return () if self.tied is None else self.tied.objects

    @property
    def readback(self) -> Mapping[ObjectId, ReadBack]:
        return {} if self.tied is None else self.tied.readback


def element_entry(v: Json, where: str) -> ElementEntry:
    m = _object(v, where)
    key = _str(_req(m, "key", where), f"{where}.key")
    at = f"{where} {key}"
    anchor = _opt_str(_opt(m, "anchor"), f"{at}.anchor")
    drawn = _opt_str(_opt(m, "drawn_from"), f"{at}.drawn_from")
    ir = m.get("ir")
    tied: Tied | None = None
    if "objects" in m or "readback" in m or "main" in m:
        main = _opt_str(_opt(m, "main"), f"{at}.main")
        tied = Tied(objects=_ids(m.get("objects") or [], f"{at}.objects"), main=None if main is None else ObjectId(main),
                    readback=readbacks(m.get("readback") or {}, f"{at}.readback"))
    removed = m.get("removed")
    margins = m.get("table_margins")
    return ElementEntry(
        key=ElementKey(key), id=_str(m["id"], f"{at}.id") if "id" in m else "",
        kind=_str(_req(m, "kind", at), f"{at}.kind"), role=_opt_str(_opt(m, "role"), f"{at}.role"),
        ir_hash=_str(_req(m, "ir_hash", at), f"{at}.ir_hash"), fields=element_fields(_req(m, "fields", at), f"{at}.fields"),
        fingerprint=fingerprint(_req(m, "fingerprint", at), f"{at}.fingerprint"),
        anchor=None if anchor is None else ElementKey(anchor), ir=None if ir is None else _object(ir, f"{at}.ir"),
        tied=tied, removed=None if removed is None else _bool(removed, f"{at}.removed"),
        table_margins=None if margins is None else tuple(_nums(x, f"{at}.table_margins")
                                                         for x in _array(margins, f"{at}.table_margins")),
        drawn_from=None if drawn is None else ElementKey(drawn), from_layout=_flag(m, "from_layout", at),
        in_table=_flag(m, "in_table", at))


def element_entry_json(e: ElementEntry) -> JsonObject:
    """In `snapshot.slide_entries`' order, then what `attach_readback` and the base's writers add."""
    out: JsonObject = {"key": e.key, "id": e.id, "kind": e.kind, "role": e.role, "ir_hash": e.ir_hash,
                       "fields": element_fields_json(e.fields), "fingerprint": fingerprint_json(e.fingerprint),
                       "anchor": e.anchor}
    _put(out, "ir", e.ir)
    if e.tied is not None:
        out["objects"] = _json_strs(e.tied.objects)
        out["main"] = e.tied.main
        out["readback"] = readbacks_json(e.tied.readback)
    if e.removed is not None:
        out["removed"] = e.removed
    if e.table_margins is not None:
        out["table_margins"] = [_json_nums(x) for x in e.table_margins]
    _put(out, "drawn_from", e.drawn_from)
    if e.from_layout:
        out["from_layout"] = True
    if e.in_table:
        out["in_table"] = True
    return out


# ---------------------------------------------------------------- a slide entry

@dataclass(frozen=True, kw_only=True)
class SlideSeen:
    """What a base slide recorded of the live slide it was written as (`snapshot.attach_readback`):
    absent from the new conversion's entries."""
    object_id: ObjectId | None
    layout_object_id: str | None
    background_readback: JsonObject | None
    notes_readback: str
    groups: tuple[ObjectId, ...]
    order: tuple[ObjectId, ...]


@dataclass(frozen=True, kw_only=True)
class SlideEntry:
    """One slide of a base or of the new conversion (`snapshot.slide_entries`): its key and what
    identity pairs it by (label, title, text, page), what emit writes of it (layout, background,
    notes) and its elements. `seen`: the live slide it was written as (a base's). `removed`: a
    slide kept though the source dropped it (None: the entry never said). `left_alone`: an adopted
    deck's objects the pairing tied to nothing (`adopt_sync.build_base`).

    `key_order`: the order its JSON had. convert, sync and the fuzz world each add the deck's keys
    at another point, and a base read and written back should be the same bytes; () for an entry
    made here."""
    key: SlideKey
    label: str | None
    title: str
    page: int
    text: str
    layout: str | None
    background: str | None
    notes: str | None
    elements: tuple[ElementEntry, ...]
    seen: SlideSeen | None
    removed: bool | None
    left_alone: tuple[ObjectId, ...] | None
    key_order: tuple[str, ...]

    @property
    def object_id(self) -> ObjectId | None:
        return None if self.seen is None else self.seen.object_id


SEEN_KEYS = ("objectId", "layoutObjectId", "background_readback", "notes_readback", "groups", "order")


def slide_entry(v: Json, where: str) -> SlideEntry:
    return slide_entry_of(_object(v, where), where)


def slide_entry_of(m: JsonMap, where: str) -> SlideEntry:
    key = _str(_req(m, "key", where), f"{where}.key")
    at = f"slide {key}"
    seen: SlideSeen | None = None
    if any(k in m for k in SEEN_KEYS):
        oid, bg = _opt_str(_opt(m, "objectId"), f"{at}.objectId"), m.get("background_readback")
        seen = SlideSeen(object_id=None if oid is None else ObjectId(oid),
                         layout_object_id=_opt_str(_opt(m, "layoutObjectId"), f"{at}.layoutObjectId"),
                         background_readback=None if bg is None else _object(bg, f"{at}.background_readback"),
                         notes_readback=_str(m["notes_readback"], f"{at}.notes_readback")
                         if m.get("notes_readback") is not None else "",
                         groups=_ids(m.get("groups") or [], f"{at}.groups"),
                         order=_ids(m.get("order") or [], f"{at}.order"))
    removed, left = m.get("removed"), m.get("left_alone")
    return SlideEntry(
        key=SlideKey(key), label=_opt_str(_opt(m, "label"), f"{at}.label"),
        title=_str(m["title"], f"{at}.title") if m.get("title") is not None else "",
        page=_int(m["page"], f"{at}.page") if "page" in m else 0,
        text=_str(m["text"], f"{at}.text") if m.get("text") is not None else "",
        layout=_opt_str(_opt(m, "layout"), f"{at}.layout"), background=_opt_str(_opt(m, "background"), f"{at}.background"),
        notes=_opt_str(_opt(m, "notes"), f"{at}.notes"),
        elements=tuple(element_entry(e, f"{at}: element") for e in _array(_req(m, "elements", at), f"{at}.elements")),
        seen=seen, removed=None if removed is None else _bool(removed, f"{at}.removed"),
        left_alone=None if left is None else _ids(left, f"{at}.left_alone"), key_order=tuple(m))


def slide_entry_json(s: SlideEntry) -> JsonObject:
    """In the order it was read, else `snapshot.slide_entries`' order, then `attach_readback`'s."""
    out = _slide_entry_json(s)
    if not s.key_order:
        return out
    first = {k: out[k] for k in s.key_order if k in out}
    return {**first, **{k: v for k, v in out.items() if k not in first}}


def _slide_entry_json(s: SlideEntry) -> JsonObject:
    out: JsonObject = {"key": s.key, "label": s.label, "title": s.title, "page": s.page, "text": s.text}
    _put(out, "layout", s.layout)
    _put(out, "background", s.background)
    _put(out, "notes", s.notes)
    out["elements"] = [element_entry_json(e) for e in s.elements]
    if s.seen is not None:
        out["objectId"] = s.seen.object_id
        out["layoutObjectId"] = s.seen.layout_object_id
        out["background_readback"] = s.seen.background_readback
        out["notes_readback"] = s.seen.notes_readback
        out["groups"] = _json_strs(s.seen.groups)
        out["order"] = _json_strs(s.seen.order)
    if s.removed is not None:
        out["removed"] = s.removed
    if s.left_alone is not None:
        out["left_alone"] = _json_strs(s.left_alone)
    return out


# ---------------------------------------------------------------- the base

@dataclass(frozen=True, kw_only=True)
class Base:
    """A sync base (`snapshot.build_base`, `sync.Sync.new_base`, `adopt_sync.build_base`). `scale`:
    deck pt per PDF pt; `page_size` the PDF's page, `deck_page_size` the deck's. `origin`: "adopt"
    for a deck a person built (`merge.ADOPTED`). What sync alone reads stays JSON: `source`,
    `master_readback`, `theme`, `pending`, `adopt`."""
    version: int | None
    generation: int | None
    presentation_id: str | None
    revision_id: str | None
    source: JsonObject | None
    overlays: str | None
    scale: float | None
    page_size: tuple[float, ...] | None
    deck_page_size: tuple[float, ...] | None
    master_background: str | None
    master_readback: JsonObject | None
    slides: tuple[SlideEntry, ...]
    theme: JsonObject | None
    pending: JsonObject | None
    cleanup: tuple[str, ...] | None
    origin: str | None
    adopt: JsonObject | None


BASE_KEYS = ("version", "generation", "presentationId", "revisionId", "source", "overlays", "scale", "page_size",
             "deck_page_size", "master_background", "master_readback", "slides", "theme", "pending", "cleanup",
             "origin", "adopt")


def base(v: JsonMap) -> Base:
    where = "the sync base"
    size, deck_size, cleanup = v.get("page_size"), v.get("deck_page_size"), v.get("cleanup")
    source, master, theme, pending, adopt = (v.get(k) for k in ("source", "master_readback", "theme", "pending", "adopt"))
    scale = v.get("scale")
    return Base(
        version=_int(v["version"], f"{where}.version") if v.get("version") is not None else None,
        generation=_int(v["generation"], f"{where}.generation") if v.get("generation") is not None else None,
        presentation_id=_opt_str(_opt(v, "presentationId"), f"{where}.presentationId"),
        revision_id=_opt_str(_opt(v, "revisionId"), f"{where}.revisionId"),
        source=None if source is None else _object(source, f"{where}.source"),
        overlays=_opt_str(_opt(v, "overlays"), f"{where}.overlays"),
        scale=None if scale is None else _num(scale, f"{where}.scale"),
        page_size=None if size is None else _nums(size, f"{where}.page_size"),
        deck_page_size=None if deck_size is None else _nums(deck_size, f"{where}.deck_page_size"),
        master_background=_opt_str(_opt(v, "master_background"), f"{where}.master_background"),
        master_readback=None if master is None else _object(master, f"{where}.master_readback"),
        slides=tuple(slide_entry(s, f"{where}.slides[{i}]")
                     for i, s in enumerate(_array(_req(v, "slides", where), f"{where}.slides"))),
        theme=None if theme is None else _object(theme, f"{where}.theme"),
        pending=None if pending is None else _object(pending, f"{where}.pending"),
        cleanup=None if cleanup is None else _strs(cleanup, f"{where}.cleanup"),
        origin=_opt_str(_opt(v, "origin"), f"{where}.origin"),
        adopt=None if adopt is None else _object(adopt, f"{where}.adopt"))


def base_json(b: Base) -> JsonObject:
    """In `snapshot.build_base`'s order; the keys a sync or adopt adds after it, where they are."""
    out: JsonObject = {}
    _put(out, "version", b.version)
    _put(out, "generation", b.generation)
    _put(out, "presentationId", b.presentation_id)
    out["revisionId"] = b.revision_id
    _put(out, "source", b.source)
    _put(out, "overlays", b.overlays)
    out["scale"] = b.scale
    out["page_size"] = None if b.page_size is None else _json_nums(b.page_size)
    out["deck_page_size"] = None if b.deck_page_size is None else _json_nums(b.deck_page_size)
    out["master_background"] = b.master_background
    out["master_readback"] = b.master_readback
    out["slides"] = [slide_entry_json(s) for s in b.slides]
    _put(out, "theme", b.theme)
    _put(out, "pending", b.pending)
    if b.cleanup is not None:
        out["cleanup"] = _json_strs(b.cleanup)
    _put(out, "origin", b.origin)
    _put(out, "adopt", b.adopt)
    return out


def unread(v: JsonMap) -> list[str]:
    """The keys of a base no record here reads, as paths: none, for a base convert or sync wrote
    (what `tools` measuring the round trip ask)."""
    found = [k for k in v if k not in BASE_KEYS]
    slide_keys = {"key", "label", "title", "page", "text", "layout", "background", "notes", "elements", "removed",
                  "left_alone", *SEEN_KEYS}
    element_keys = {"key", "id", "kind", "role", "ir_hash", "fields", "fingerprint", "anchor", "ir", "objects", "main",
                    "readback", "removed", "table_margins", "drawn_from", "from_layout", "in_table"}
    slides = v.get("slides")
    for s in slides if isinstance(slides, list) else []:
        if not isinstance(s, dict):
            continue
        found += [f"slide.{k}" for k in s if k not in slide_keys]
        elements = s.get("elements")
        for e in elements if isinstance(elements, list) else []:
            if isinstance(e, dict):
                found += [f"element.{k}" for k in e if k not in element_keys]
    return sorted(set(found))
