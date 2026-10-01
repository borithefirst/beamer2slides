"""raw.json, written down: what `extract` hands `classify` for each page, and its parser.

A key is required when `extract.extract_page` always writes it, optional (in the `total=False`
half of a type) when it writes it only where it holds: a span's `marks` on an adopted source's
page, a page's `hidden_text`. `frame_label` and `notes` are added to the page after extract
(`extract.extract`; `notes` by the callers that read speaker notes), so a page built elsewhere
may have neither. `overlays` is added to the file by `extract.select_overlays`.

Coordinates are PDF points from the page's top left. Classify's readers keep their `.get` with a
default for keys a test's hand-built page leaves out; the type says what extract writes.

The records stay TypedDicts, not dataclasses: raw.json is the value itself (every writer dumps
it as it is, byte for byte), and classify, render and marked read it by key at some hundred
places. What the types buy is at the two ends: extract builds each record as a literal of its
type, so a key it forgets is an error where it is written, and JSON becomes a page only through
`parse_raw` / `parse_page`, which check every key's presence and type and refuse a key the type
does not have.

Python 3.10, no typing_extensions: optional keys come by inheritance, as in `ir.py`.
"""

from collections.abc import Callable
from typing import Literal, TypedDict, TypeVar

from .json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_optional_str, as_str

RawColor = str
"""'#rrggbb', lowercase."""

RawMark = tuple[str, dict[str, str | int]]
"""(tag, params) of one B2S mark on an adopted source's page (`marked.params` reads them): the
string and integer parameters of the marked-content sequence. In JSON a pair, as `PathItem`."""

PathItem = tuple[str, list[list[float]]]
"""One piece of a drawing's path: its operator ('re', 'l', 'c', 'qu') and its points [[x, y], ...]
('re': the two corners). In JSON a pair; typed as the tuple every reader unpacks it as
(`for op, pts in path`), and never tested as one."""

DrawingType = Literal["f", "s", "fs"]
"""Filled, stroked, or both."""
DRAWING_TYPES: tuple[DrawingType, ...] = ("f", "s", "fs")


class _RawSpanKeys(TypedDict):
    id: str
    text: str
    font: str
    size: float
    color: RawColor
    alpha: int
    """0 (unseen) to 255."""
    origin: list[float]
    """[x, y] of the first glyph's origin, on its baseline."""
    bbox: list[float]
    dir: list[float]
    """The text's direction, [1, 0] for level text."""
    smallcaps: bool


class RawSpan(_RawSpanKeys, total=False):
    """A run of glyphs on one line in one font, size and colour, split at word gaps."""
    marks: list[RawMark]


class _RawImageKeys(TypedDict):
    id: str
    bbox: list[float]
    px: list[float]
    """[width, height] in pixels (a shadow piece's in points)."""


class RawImage(_RawImageKeys, total=False):
    marks: list[RawMark]


class _RawDrawingKeys(TypedDict):
    id: str
    """p<page>d<index into the page's drawings>: render finds the object by it."""
    type: DrawingType
    items: str
    """The path's operators run together ('re', 'lll', 'cccc')."""
    bbox: list[float]
    fill: RawColor | None
    stroke: RawColor | None
    width: float | None
    fill_opacity: float
    stroke_opacity: float
    soft_mask: bool
    """A soft mask or a blend mode (multiply)."""
    corners: dict[str, float]
    """Which bbox corners ('tl', 'br'...) are drawn with a curve, and the curve's radius."""
    path: list[PathItem] | None
    """None for a drawing of more than 20 pieces."""


class RawDrawing(_RawDrawingKeys, total=False):
    marks: list[RawMark]
    dash: list[float]
    """A dashed stroke's on and off lengths, page pt (the PDF's dash array as drawn); absent for a
    solid one, and in a raw.json older than it."""


RawItem = RawSpan | RawImage | RawDrawing
"""Anything on a page a B2S mark can be around."""


class _RawLinkKeys(TypedDict):
    bbox: list[float]


class RawLink(_RawLinkKeys, total=False):
    """A link to a URL (`uri`) or to a page of the document (`page`, 0-based): one of the two."""
    uri: str
    page: int


class _RawPageKeys(TypedDict):
    index: int
    label: str
    """The page label (beamer: the frame number)."""
    size: list[float]
    """[width, height]."""
    spans: list[RawSpan]
    images: list[RawImage]
    drawings: list[RawDrawing]
    links: list[RawLink]


class RawPage(_RawPageKeys, total=False):
    hidden_text: list[str]
    """Words the page draws and does not show (beamer's transparent overlays)."""
    hidden_spans: list[RawSpan]
    """A marked page's hidden words, as spans."""
    frame_label: str | None
    """The frame's \\label (hyperref's anchor), if it has one."""
    notes: str | None
    """The page's speaker notes."""


class RawSource(TypedDict):
    pdf: str
    """The PDF read, as the caller named it (without note pages: `notes.prepare`'s copy)."""
    producer: str
    pages: int
    """The PDF's page count, before `select_overlays`."""
    title: str
    """The document title from the PDF's metadata, "" when it has none."""


class RawOverlays(TypedDict):
    """What `select_overlays` left out."""
    mode: str
    dropped: int


class _RawDocKeys(TypedDict):
    version: int
    source: RawSource
    pages: list[RawPage]


class RawDoc(_RawDocKeys, total=False):
    """raw.json."""
    overlays: RawOverlays


# ---------------------------------------------------------------------------------------- parsing

T = TypeVar("T")


def _found(value: Json) -> str:
    return "null" if value is None else type(value).__name__


def _number(v: Json, where: str) -> float:
    """A JSON number, kept as it came (an int stays an int, so the file dumps back the same)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected, found {_found(v)}")


def _optional_number(v: Json, where: str) -> float | None:
    return None if v is None else _number(v, where)


def _bool(v: Json, where: str) -> bool:
    if isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a boolean was expected, found {_found(v)}")


def _list(v: Json, where: str, parse: Callable[[Json, str], T]) -> list[T]:
    return [parse(x, f"{where}[{i}]") for i, x in enumerate(as_array(v, where))]


def _numbers(v: Json, where: str) -> list[float]:
    return _list(v, where, _number)


def _fixed(v: Json, where: str, n: int) -> list[float]:
    """[x, y] or [x0, y0, x1, y1]: every reader unpacks it."""
    out = _numbers(v, where)
    if len(out) != n:
        raise JsonShapeError(f"{where}: {n} numbers were expected, found {len(out)}")
    return out


def _point(v: Json, where: str) -> list[float]:
    return _fixed(v, where, 2)


def _box(v: Json, where: str) -> list[float]:
    return _fixed(v, where, 4)


def _color(v: Json, where: str) -> RawColor:
    text = as_str(v, where)
    if len(text) != 7 or text[0] != "#" or any(c not in "0123456789abcdef" for c in text[1:]):
        raise JsonShapeError(f"{where}: a colour '#rrggbb' was expected, found {text!r}")
    return text


def _optional_color(v: Json, where: str) -> RawColor | None:
    return None if v is None else _color(v, where)


def _keys(v: Json, where: str, required: frozenset[str], optional: frozenset[str]) -> JsonObject:
    """The object at `where`, holding every required key and no key its type does not have."""
    o = as_object(v, where)
    missing = sorted(required - set(o))
    if missing:
        raise JsonShapeError(f"{where}: the key {missing[0]!r} is missing")
    unknown = sorted(set(o) - required - optional)
    if unknown:
        raise JsonShapeError(f"{where}.{unknown[0]}: no such key in raw.json")
    return o


def parse_mark(v: Json, where: str) -> RawMark:
    pair = as_array(v, where)
    if len(pair) != 2:
        raise JsonShapeError(f"{where}: a pair [tag, params] was expected, found {len(pair)} items")
    params: dict[str, str | int] = {}
    for key, value in as_object(pair[1], f"{where}[1]").items():
        params[key] = value if isinstance(value, str) else as_int(value, f"{where}[1].{key}")
    return (as_str(pair[0], f"{where}[0]"), params)


def _path_item(v: Json, where: str) -> PathItem:
    pair = as_array(v, where)
    if len(pair) != 2:
        raise JsonShapeError(f"{where}: a pair [operator, points] was expected, found {len(pair)} items")
    return (as_str(pair[0], f"{where}[0]"), _list(pair[1], f"{where}[1]", _point))


def _drawing_type(v: Json, where: str) -> DrawingType:
    for t in DRAWING_TYPES:
        if v == t:
            return t
    raise JsonShapeError(f"{where}: 'f', 's' or 'fs' was expected, found {v!r}")


def _corners(v: Json, where: str) -> dict[str, float]:
    return {k: _number(x, f"{where}.{k}") for k, x in as_object(v, where).items()}


def parse_span(v: Json, where: str) -> RawSpan:
    o = _keys(v, where, RawSpan.__required_keys__, RawSpan.__optional_keys__)
    span: RawSpan = {
        "id": as_str(o["id"], f"{where}.id"), "text": as_str(o["text"], f"{where}.text"),
        "font": as_str(o["font"], f"{where}.font"), "size": _number(o["size"], f"{where}.size"),
        "color": _color(o["color"], f"{where}.color"), "alpha": as_int(o["alpha"], f"{where}.alpha"),
        "origin": _point(o["origin"], f"{where}.origin"), "bbox": _box(o["bbox"], f"{where}.bbox"),
        "dir": _point(o["dir"], f"{where}.dir"), "smallcaps": _bool(o["smallcaps"], f"{where}.smallcaps"),
    }
    if "marks" in o:
        span["marks"] = _list(o["marks"], f"{where}.marks", parse_mark)
    return span


def parse_image(v: Json, where: str) -> RawImage:
    o = _keys(v, where, RawImage.__required_keys__, RawImage.__optional_keys__)
    image: RawImage = {"id": as_str(o["id"], f"{where}.id"), "bbox": _box(o["bbox"], f"{where}.bbox"),
                       "px": _point(o["px"], f"{where}.px")}
    if "marks" in o:
        image["marks"] = _list(o["marks"], f"{where}.marks", parse_mark)
    return image


def parse_drawing(v: Json, where: str) -> RawDrawing:
    o = _keys(v, where, RawDrawing.__required_keys__, RawDrawing.__optional_keys__)
    path = o["path"]
    drawing: RawDrawing = {
        "id": as_str(o["id"], f"{where}.id"), "type": _drawing_type(o["type"], f"{where}.type"),
        "items": as_str(o["items"], f"{where}.items"), "bbox": _box(o["bbox"], f"{where}.bbox"),
        "fill": _optional_color(o["fill"], f"{where}.fill"), "stroke": _optional_color(o["stroke"], f"{where}.stroke"),
        "width": _optional_number(o["width"], f"{where}.width"),
        "fill_opacity": _number(o["fill_opacity"], f"{where}.fill_opacity"),
        "stroke_opacity": _number(o["stroke_opacity"], f"{where}.stroke_opacity"),
        "soft_mask": _bool(o["soft_mask"], f"{where}.soft_mask"),
        "corners": _corners(o["corners"], f"{where}.corners"),
        "path": None if path is None else _list(path, f"{where}.path", _path_item),
    }
    if "dash" in o:  # (before the marks, as extract writes them)
        drawing["dash"] = _numbers(o["dash"], f"{where}.dash")
    if "marks" in o:
        drawing["marks"] = _list(o["marks"], f"{where}.marks", parse_mark)
    return drawing


def parse_link(v: Json, where: str) -> RawLink:
    o = _keys(v, where, RawLink.__required_keys__, RawLink.__optional_keys__)
    link: RawLink = {"bbox": _box(o["bbox"], f"{where}.bbox")}
    if ("uri" in o) == ("page" in o):
        raise JsonShapeError(f"{where}: a link has a 'uri' or a 'page', one of the two")
    if "uri" in o:
        link["uri"] = as_str(o["uri"], f"{where}.uri")
    else:
        link["page"] = as_int(o["page"], f"{where}.page")
    return link


def parse_page(v: Json, where: str) -> RawPage:
    """One page of raw.json, checked key by key; a value of another shape raises
    `JsonShapeError` naming its path (`pages[3].spans[12].bbox`)."""
    o = _keys(v, where, RawPage.__required_keys__, RawPage.__optional_keys__)
    page: RawPage = {
        "index": as_int(o["index"], f"{where}.index"), "label": as_str(o["label"], f"{where}.label"),
        "size": _point(o["size"], f"{where}.size"),
        "spans": _list(o["spans"], f"{where}.spans", parse_span),
        "images": _list(o["images"], f"{where}.images", parse_image),
        "drawings": _list(o["drawings"], f"{where}.drawings", parse_drawing),
        "links": _list(o["links"], f"{where}.links", parse_link),
    }
    if "hidden_text" in o:
        page["hidden_text"] = _list(o["hidden_text"], f"{where}.hidden_text", as_str)
    if "hidden_spans" in o:
        page["hidden_spans"] = _list(o["hidden_spans"], f"{where}.hidden_spans", parse_span)
    if "frame_label" in o:
        page["frame_label"] = as_optional_str(o["frame_label"], f"{where}.frame_label")
    if "notes" in o:
        page["notes"] = as_optional_str(o["notes"], f"{where}.notes")
    return page


def parse_source(v: Json, where: str) -> RawSource:
    o = _keys(v, where, RawSource.__required_keys__, RawSource.__optional_keys__)
    return {"pdf": as_str(o["pdf"], f"{where}.pdf"), "producer": as_str(o["producer"], f"{where}.producer"),
            "pages": as_int(o["pages"], f"{where}.pages"), "title": as_str(o["title"], f"{where}.title")}


def parse_overlays(v: Json, where: str) -> RawOverlays:
    o = _keys(v, where, RawOverlays.__required_keys__, RawOverlays.__optional_keys__)
    return {"mode": as_str(o["mode"], f"{where}.mode"), "dropped": as_int(o["dropped"], f"{where}.dropped")}


def parse_raw(v: Json, where: str) -> RawDoc:
    """raw.json (`where`: the file, for the message), checked key by key as `parse_page` does: the
    only way a raw.json read from disk becomes a `RawDoc`."""
    o = _keys(v, where, RawDoc.__required_keys__, RawDoc.__optional_keys__)
    raw: RawDoc = {
        "version": as_int(o["version"], f"{where}: version"),
        "source": parse_source(o["source"], f"{where}: source"),
        "pages": _list(o["pages"], f"{where}: pages", parse_page),
    }
    if "overlays" in o:
        raw["overlays"] = parse_overlays(o["overlays"], f"{where}: overlays")
    return raw
