"""The part of Google's Slides, Drive and Docs APIs this package calls, written down as types.

`googleapiclient` builds its clients at runtime from a discovery document, so to a type checker a
client is a `Resource` with no methods at all: `slides.presentations().get(...)` was an unknown
call returning an unknown answer, and so was every answer read from it. These Protocols say which
methods we call, with which keywords, and what each one answers - a misspelt method, a keyword the
API does not take or a missing required one is an error where it is written. They describe only
what the package uses; a call not listed here is one to add here first.

The keywords of each method are a TypedDict (`**kw: Unpack[...]`), as the discovery document lists
them: `Required` where Google requires one, optional otherwise. That is how a method with optional
parameters is written without a default value.

Answers are JSON, described by TypedDicts rather than parsed into dataclasses, on purpose:
- they are Google's schema, not ours, and open-ended: Google adds fields, and a field mask
  (`fields=`) leaves out any of them - so every key is optional (`total=False`), and a reader
  says what happens when one is absent;
- they are kept and written back as they came (a presentation.json, a sync base's read-back), so
  the dict is the value; a dataclass would have to carry every field it does not model;
- what the package decides from an answer is its own type, parsed from these where it is read
  (docs/typing.md, "Parse at the boundary").
Nested parts nobody reads through a typed client yet (a shape's text, a table's cells, a Doc's
body) are `JsonObject` (`json_types`): JSON the checker follows no further until someone models it
here, read with `json_types`' narrowings.

A TypedDict is not assignable to a parameter annotated `dict`, and a `JsonObject` is. So
`presentations.get` answers a `Presentation` and `files.get` a `DriveFile`, and their readers
(snapshot, sync, theme_sync, guard, deck_pictures) say so; a reader not typed yet takes a
presentation.json it loaded through `presentation` (checked against the TypedDict, as far as it is
modelled) and hands an answer on as JSON through `as_json` (checked all the way down).

Nothing here imports the client library: `gapi` builds the real clients and says they are these.
The functions here are boundary reads: `file_id` for every `files.create`/`copy` caller, and the
walk of a presentation (`object_id`, `children`, `all_elements`, `background_url`, `image_url`),
where an id a `fields=` mask left out, or a group's child that is no page element, is said once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import UnionType
from typing import (TYPE_CHECKING, ForwardRef, Literal, Protocol, TypedDict, TypeGuard, TypeVar, Union, get_args,
                    get_origin, get_type_hints, is_typeddict, runtime_checkable)

from .json_types import Json, JsonObject, JsonShapeError, as_objects

if TYPE_CHECKING:
    from typing_extensions import Required, Unpack

T_co = TypeVar("T_co", covariant=True)


# ------------------------------------------------------------------------------ one call


class ExecuteOptions(TypedDict, total=False):
    """`HttpRequest.execute`'s keyword: the connection to answer over (`gapi.patient_http`)."""
    http: object


class Request(Protocol[T_co]):
    """One API call, made when executed: what a method of a client returns. `gslides.execute` runs
    one with retries; a client a caller injected answers `execute()` with no keyword at all, and is
    only ever given `http` when `gapi.patient_http` found the library's own connection on it."""

    def execute(self, **options: Unpack[ExecuteOptions]) -> T_co: ...


class MediaBody(Protocol):
    """A file's content for `media_body` (`gapi.media_upload`); a fake Drive reads it back with
    `getbytes(0, size())`, as the library's own upload does."""

    def mimetype(self) -> str: ...
    def size(self) -> int: ...
    def getbytes(self, begin: int, length: int) -> bytes: ...


# ------------------------------------------------------------------------------ Slides: answers


class Dimension(TypedDict, total=False):
    magnitude: float
    unit: Literal["EMU", "PT", "UNIT_UNSPECIFIED"]


class Size(TypedDict, total=False):
    width: Dimension
    height: Dimension


class AffineTransform(TypedDict, total=False):
    scaleX: float
    scaleY: float
    shearX: float
    shearY: float
    translateX: float
    translateY: float
    unit: Literal["EMU", "PT", "UNIT_UNSPECIFIED"]


class PageElement(TypedDict, total=False):
    objectId: str
    size: Size
    transform: AffineTransform
    title: str
    description: str
    shape: JsonObject
    image: JsonObject
    table: JsonObject
    line: JsonObject
    elementGroup: JsonObject
    sheetsChart: JsonObject
    video: JsonObject
    wordArt: JsonObject
    speakerSpotlight: JsonObject


class SlideProperties(TypedDict, total=False):
    layoutObjectId: str
    masterObjectId: str
    notesPage: Page
    isSkipped: bool


class LayoutProperties(TypedDict, total=False):
    masterObjectId: str
    name: str
    displayName: str


class Page(TypedDict, total=False):
    objectId: str
    pageType: Literal["SLIDE", "MASTER", "LAYOUT", "NOTES", "NOTES_MASTER"]
    pageElements: list[PageElement]
    revisionId: str
    pageProperties: JsonObject
    slideProperties: SlideProperties
    layoutProperties: LayoutProperties
    notesProperties: JsonObject
    masterProperties: JsonObject


class Presentation(TypedDict, total=False):
    """`presentations.get` / `create`."""
    presentationId: str
    title: str
    locale: str
    revisionId: str
    pageSize: Size
    slides: list[Page]
    masters: list[Page]
    layouts: list[Page]
    notesMaster: Page


class WriteControl(TypedDict, total=False):
    requiredRevisionId: str


class BatchUpdateResponse(TypedDict, total=False):
    """`presentations.batchUpdate`: one reply per request, in order (`{}` for one that makes
    nothing), and the revision the deck is at after it."""
    presentationId: str
    replies: list[JsonObject]
    writeControl: WriteControl


class Thumbnail(TypedDict, total=False):
    """`presentations.pages.getThumbnail`: where Google put the PNG, and its size in pixels."""
    contentUrl: str
    width: int
    height: int


# ------------------------------------------------------------------------------ Slides: walking an answer

UNITS = ("EMU", "PT", "UNIT_UNSPECIFIED")
TRANSFORM_NUMBERS = ("scaleX", "scaleY", "shearX", "shearY", "translateX", "translateY")
ELEMENT_PARTS = ("shape", "image", "table", "line", "elementGroup", "sheetsChart", "video", "wordArt",
                 "speakerSpotlight")


def object_id(o: PageElement | Page) -> str:
    """The id of a page or page element. Google answers one for each, and only a `fields=` mask
    that did not ask for it leaves it out: every key of an answer is optional to the type, this one
    is not to its reader, so its absence is said here once rather than as a KeyError."""
    oid = o.get("objectId")
    if oid is None:
        raise JsonShapeError("a page or page element without its objectId (a fields= mask that left it out?)")
    return oid


def presentation_id(p: Presentation) -> str:
    """The id of a presentation read with `presentations.get` (as `object_id`)."""
    pid = p.get("presentationId")
    if pid is None:
        raise JsonShapeError("a presentation without its presentationId (a fields= mask that left it out?)")
    return pid


def _number(v: Json) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_dimension(v: Json) -> bool:
    return isinstance(v, dict) and ("magnitude" not in v or _number(v["magnitude"])) and v.get("unit", "EMU") in UNITS


def _is_size(v: Json) -> bool:
    return isinstance(v, dict) and all(_is_dimension(v[k]) for k in ("width", "height") if k in v)


def _is_transform(v: Json) -> bool:
    return isinstance(v, dict) and all(_number(v[k]) for k in TRANSFORM_NUMBERS if k in v) \
        and v.get("unit", "EMU") in UNITS


def is_page_element(o: JsonObject) -> TypeGuard[PageElement]:
    """Whether `o` holds what `PageElement` says of each key it has - the object itself, not a copy,
    so what a walker writes into it lands in the answer it came from."""
    return all(isinstance(o[k], str) for k in ("objectId", "title", "description") if k in o) \
        and ("size" not in o or _is_size(o["size"])) and ("transform" not in o or _is_transform(o["transform"])) \
        and all(isinstance(o[k], dict) for k in ELEMENT_PARTS if k in o)


def children(e: PageElement, where: str) -> list[PageElement]:
    """A group's own elements (none for any other element). `elementGroup` is JSON to the type, as
    every nested part nobody models, but its children are page elements: checked as such here,
    where a walker steps into them, so a child of another shape is an error naming the group."""
    group = e.get("elementGroup")
    kids = None if group is None else group.get("children")
    if kids is None:
        return []
    out: list[PageElement] = []
    for i, kid in enumerate(as_objects(kids, f"{where}.children")):
        if not is_page_element(kid):
            raise JsonShapeError(f"{where}.children[{i}]: not a page element")
        out.append(kid)
    return out


PAGE_TYPES = ("SLIDE", "MASTER", "LAYOUT", "NOTES", "NOTES_MASTER")


def _strings(o: JsonObject, keys: Sequence[str]) -> bool:
    return all(isinstance(o[k], str) for k in keys if k in o)


def _is_page(v: Json) -> bool:
    if not isinstance(v, dict):
        return False
    elements = v.get("pageElements", [])
    slide = v.get("slideProperties", {})
    layout = v.get("layoutProperties", {})
    return _strings(v, ("objectId", "revisionId")) and v.get("pageType", "SLIDE") in PAGE_TYPES \
        and isinstance(elements, list) and all(isinstance(e, dict) and is_page_element(e) for e in elements) \
        and all(isinstance(v[k], dict) for k in ("pageProperties", "notesProperties", "masterProperties") if k in v) \
        and isinstance(slide, dict) and _strings(slide, ("layoutObjectId", "masterObjectId")) \
        and isinstance(slide.get("isSkipped", False), bool) and ("notesPage" not in slide or _is_page(slide["notesPage"])) \
        and isinstance(layout, dict) and _strings(layout, ("masterObjectId", "name", "displayName"))


def is_presentation(o: JsonObject) -> TypeGuard[Presentation]:
    """Whether `o` - a `presentations.get` kept as JSON (a deck-files presentation.json, a reader
    not typed yet) - holds what `Presentation` says, pages and page elements included, as far as
    they are modelled. The object itself, not a copy."""
    for key in ("slides", "masters", "layouts"):
        pages = o.get(key)
        if pages is not None and not (isinstance(pages, list) and all(_is_page(p) for p in pages)):
            return False
    return ("notesMaster" not in o or _is_page(o["notesMaster"])) \
        and _strings(o, ("presentationId", "title", "locale", "revisionId")) \
        and ("pageSize" not in o or _is_size(o["pageSize"]))


def presentation(o: JsonObject, where: str) -> Presentation:
    """`o` as the `presentations.get` it was read from (`is_presentation`), or an error naming `where`."""
    if not is_presentation(o):
        raise JsonShapeError(f"{where}: not a presentation as presentations.get answers one")
    return o


def _is_json(v: object) -> bool:
    if v is None or isinstance(v, (bool, int, float, str)):
        return True
    if isinstance(v, list):
        return all(_is_json(x) for x in v)
    if isinstance(v, dict):
        return all(isinstance(k, str) and _is_json(x) for k, x in v.items())
    return False


def _is_json_object(v: object) -> TypeGuard[JsonObject]:
    return isinstance(v, dict) and _is_json(v)


def as_json(answer: Presentation | Page | PageElement | DriveFile, where: str) -> JsonObject:
    """An answer, or a part of one, handed to a reader that still reads it as JSON (deck_ir, the
    devtools) or put back into JSON (a group's children): what it is, checked all the way down (no
    copy), since a TypedDict is not assignable to a `dict` parameter nor a `Json` value."""
    value: object = answer
    if not _is_json_object(value):
        raise JsonShapeError(f"{where}: not JSON")
    return value


def json_object(value: object, where: str) -> JsonObject:
    """A value a caller built as plain data (a replay's `dict[str, object]`) read as the JSON object
    it is, checked all the way down (no copy), or an error naming `where`."""
    if not _is_json_object(value):
        raise JsonShapeError(f"{where}: not a JSON object")
    return value


def part(value: Json, where: str) -> JsonObject:
    """A nested object of an answer, which Google leaves out when it is empty: absent is `{}`."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise JsonShapeError(f"{where}: an object was expected, found {type(value).__name__}")
    return value


def parts(value: Json, where: str) -> list[JsonObject]:
    """A nested list of objects of an answer (absent: none)."""
    return [] if value is None else as_objects(value, where)


def _url(value: Json, where: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise JsonShapeError(f"{where}: a string was expected, found {type(value).__name__}")
    return value or None


def background_fill(page: Page) -> JsonObject:
    """A page's `pageBackgroundFill` (`{}`: the page says none)."""
    return part(part(page.get("pageProperties"), "pageProperties").get("pageBackgroundFill"),
                 "pageProperties.pageBackgroundFill")


def background_url(page: Page) -> str | None:
    """The contentUrl of a page's background picture (None: it has none)."""
    picture = part(background_fill(page).get("stretchedPictureFill"), "pageBackgroundFill.stretchedPictureFill")
    return _url(picture.get("contentUrl"), "stretchedPictureFill.contentUrl")


def image_url(e: PageElement) -> str | None:
    """The contentUrl of an image element (None: another element, or no picture)."""
    image = e.get("image")
    return None if image is None else _url(image.get("contentUrl"), "image.contentUrl")


def all_elements(elements: Sequence[PageElement], where: str) -> list[PageElement]:
    """`elements` and everything in their groups, depth first, a group before its children (the
    order a .pptx export draws them in)."""
    out: list[PageElement] = []
    for e in elements:
        out.append(e)
        out += all_elements(children(e, where), f"{where}/{e.get('objectId')}")
    return out


# ------------------------------------------------------------------------------ Slides: payloads
# The parts a request carries, as slides.v1.json names them (a `Slides` prefix where Docs has a
# schema of the same name). Every field is optional, as Google's open schema has it; an enum is a
# Literal of the discovery document's values. `tests/test_google_schema.py` holds each of these to
# the discovery document, field by field. A part a request copies from a read-back is parsed where
# it is read (`slides_text_style`, `shape_properties`, ... below), never handed on as JSON.

ThemeColorType = Literal[
    "THEME_COLOR_TYPE_UNSPECIFIED", "DARK1", "LIGHT1", "DARK2", "LIGHT2", "ACCENT1", "ACCENT2", "ACCENT3",
    "ACCENT4", "ACCENT5", "ACCENT6", "HYPERLINK", "FOLLOWED_HYPERLINK", "TEXT1", "BACKGROUND1", "TEXT2",
    "BACKGROUND2"]
PropertyState = Literal["RENDERED", "NOT_RENDERED", "INHERIT"]
DashStyle = Literal["DASH_STYLE_UNSPECIFIED", "SOLID", "DOT", "DASH", "DASH_DOT", "LONG_DASH", "LONG_DASH_DOT"]
ContentAlignment = Literal["CONTENT_ALIGNMENT_UNSPECIFIED", "CONTENT_ALIGNMENT_UNSUPPORTED", "TOP", "MIDDLE",
                           "BOTTOM"]
ArrowStyle = Literal["ARROW_STYLE_UNSPECIFIED", "NONE", "STEALTH_ARROW", "FILL_ARROW", "FILL_CIRCLE", "FILL_SQUARE",
                     "FILL_DIAMOND", "OPEN_ARROW", "OPEN_CIRCLE", "OPEN_SQUARE", "OPEN_DIAMOND"]
LineCategory = Literal["STRAIGHT", "BENT", "CURVED"]
RectanglePosition = Literal["RECTANGLE_POSITION_UNSPECIFIED", "TOP_LEFT", "TOP_CENTER", "TOP_RIGHT", "LEFT_CENTER",
                            "CENTER", "RIGHT_CENTER", "BOTTOM_LEFT", "BOTTOM_CENTER", "BOTTOM_RIGHT"]
ShapeType = Literal[
    "TYPE_UNSPECIFIED", "TEXT_BOX", "RECTANGLE", "ROUND_RECTANGLE", "ELLIPSE", "ARC", "BENT_ARROW", "BENT_UP_ARROW",
    "BEVEL", "BLOCK_ARC", "BRACE_PAIR", "BRACKET_PAIR", "CAN", "CHEVRON", "CHORD", "CLOUD", "CORNER", "CUBE",
    "CURVED_DOWN_ARROW", "CURVED_LEFT_ARROW", "CURVED_RIGHT_ARROW", "CURVED_UP_ARROW", "DECAGON", "DIAGONAL_STRIPE",
    "DIAMOND", "DODECAGON", "DONUT", "DOUBLE_WAVE", "DOWN_ARROW", "DOWN_ARROW_CALLOUT", "FOLDED_CORNER", "FRAME",
    "HALF_FRAME", "HEART", "HEPTAGON", "HEXAGON", "HOME_PLATE", "HORIZONTAL_SCROLL", "IRREGULAR_SEAL_1",
    "IRREGULAR_SEAL_2", "LEFT_ARROW", "LEFT_ARROW_CALLOUT", "LEFT_BRACE", "LEFT_BRACKET", "LEFT_RIGHT_ARROW",
    "LEFT_RIGHT_ARROW_CALLOUT", "LEFT_RIGHT_UP_ARROW", "LEFT_UP_ARROW", "LIGHTNING_BOLT", "MATH_DIVIDE", "MATH_EQUAL",
    "MATH_MINUS", "MATH_MULTIPLY", "MATH_NOT_EQUAL", "MATH_PLUS", "MOON", "NO_SMOKING", "NOTCHED_RIGHT_ARROW",
    "OCTAGON", "PARALLELOGRAM", "PENTAGON", "PIE", "PLAQUE", "PLUS", "QUAD_ARROW", "QUAD_ARROW_CALLOUT", "RIBBON",
    "RIBBON_2", "RIGHT_ARROW", "RIGHT_ARROW_CALLOUT", "RIGHT_BRACE", "RIGHT_BRACKET", "ROUND_1_RECTANGLE",
    "ROUND_2_DIAGONAL_RECTANGLE", "ROUND_2_SAME_RECTANGLE", "RIGHT_TRIANGLE", "SMILEY_FACE", "SNIP_1_RECTANGLE",
    "SNIP_2_DIAGONAL_RECTANGLE", "SNIP_2_SAME_RECTANGLE", "SNIP_ROUND_RECTANGLE", "STAR_10", "STAR_12", "STAR_16",
    "STAR_24", "STAR_32", "STAR_4", "STAR_5", "STAR_6", "STAR_7", "STAR_8", "STRIPED_RIGHT_ARROW", "SUN", "TRAPEZOID",
    "TRIANGLE", "UP_ARROW", "UP_ARROW_CALLOUT", "UP_DOWN_ARROW", "UTURN_ARROW", "VERTICAL_SCROLL", "WAVE",
    "WEDGE_ELLIPSE_CALLOUT", "WEDGE_RECTANGLE_CALLOUT", "WEDGE_ROUND_RECTANGLE_CALLOUT",
    "FLOW_CHART_ALTERNATE_PROCESS", "FLOW_CHART_COLLATE", "FLOW_CHART_CONNECTOR", "FLOW_CHART_DECISION",
    "FLOW_CHART_DELAY", "FLOW_CHART_DISPLAY", "FLOW_CHART_DOCUMENT", "FLOW_CHART_EXTRACT", "FLOW_CHART_INPUT_OUTPUT",
    "FLOW_CHART_INTERNAL_STORAGE", "FLOW_CHART_MAGNETIC_DISK", "FLOW_CHART_MAGNETIC_DRUM",
    "FLOW_CHART_MAGNETIC_TAPE", "FLOW_CHART_MANUAL_INPUT", "FLOW_CHART_MANUAL_OPERATION", "FLOW_CHART_MERGE",
    "FLOW_CHART_MULTIDOCUMENT", "FLOW_CHART_OFFLINE_STORAGE", "FLOW_CHART_OFFPAGE_CONNECTOR",
    "FLOW_CHART_ONLINE_STORAGE", "FLOW_CHART_OR", "FLOW_CHART_PREDEFINED_PROCESS", "FLOW_CHART_PREPARATION",
    "FLOW_CHART_PROCESS", "FLOW_CHART_PUNCHED_CARD", "FLOW_CHART_PUNCHED_TAPE", "FLOW_CHART_SORT",
    "FLOW_CHART_SUMMING_JUNCTION", "FLOW_CHART_TERMINATOR", "ARROW_EAST", "ARROW_NORTH_EAST", "ARROW_NORTH", "SPEECH",
    "STARBURST", "TEARDROP", "ELLIPSE_RIBBON", "ELLIPSE_RIBBON_2", "CLOUD_CALLOUT", "CUSTOM"]
SHAPE_TYPES: frozenset[str] = frozenset(get_args(ShapeType))


def is_shape_type(value: str) -> TypeGuard[ShapeType]:
    return value in SHAPE_TYPES


def shape_type(value: str, where: str) -> ShapeType:
    """`value` as a `createShape` shape type, or an error naming `where` (a preset Slides has no
    shape for would be refused with the whole batch)."""
    if not is_shape_type(value):
        raise JsonShapeError(f"{where}: {value!r} is no Slides shape type")
    return value


class SlidesRgbColor(TypedDict, total=False):
    red: float
    green: float
    blue: float


class OpaqueColor(TypedDict, total=False):
    rgbColor: SlidesRgbColor
    themeColor: ThemeColorType


class SlidesOptionalColor(TypedDict, total=False):
    """A colour that may be none: `{}` is transparent (a text style's background cleared)."""
    opaqueColor: OpaqueColor


class SolidFill(TypedDict, total=False):
    color: OpaqueColor
    alpha: float


class SlidesLink(TypedDict, total=False):
    url: str
    relativeLink: Literal["RELATIVE_SLIDE_LINK_UNSPECIFIED", "NEXT_SLIDE", "PREVIOUS_SLIDE", "FIRST_SLIDE",
                          "LAST_SLIDE"]
    pageObjectId: str
    slideIndex: int


class SlidesWeightedFontFamily(TypedDict, total=False):
    fontFamily: str
    weight: int


class SlidesTextStyle(TypedDict, total=False):
    backgroundColor: SlidesOptionalColor
    foregroundColor: SlidesOptionalColor
    bold: bool
    italic: bool
    fontFamily: str
    fontSize: Dimension
    link: SlidesLink
    baselineOffset: Literal["BASELINE_OFFSET_UNSPECIFIED", "NONE", "SUPERSCRIPT", "SUBSCRIPT"]
    smallCaps: bool
    strikethrough: bool
    underline: bool
    weightedFontFamily: SlidesWeightedFontFamily


class SlidesParagraphStyle(TypedDict, total=False):
    lineSpacing: float
    alignment: Literal["ALIGNMENT_UNSPECIFIED", "START", "CENTER", "END", "JUSTIFIED"]
    indentStart: Dimension
    indentEnd: Dimension
    spaceAbove: Dimension
    spaceBelow: Dimension
    indentFirstLine: Dimension
    direction: Literal["TEXT_DIRECTION_UNSPECIFIED", "LEFT_TO_RIGHT", "RIGHT_TO_LEFT"]
    spacingMode: Literal["SPACING_MODE_UNSPECIFIED", "NEVER_COLLAPSE", "COLLAPSE_LISTS"]


class SlidesBullet(TypedDict, total=False):
    """A paragraph's bullet as a read-back has it (no request writes one: `createParagraphBullets`)."""
    listId: str
    nestingLevel: int
    glyph: str
    bulletStyle: SlidesTextStyle


class OutlineFill(TypedDict, total=False):
    solidFill: SolidFill


class Outline(TypedDict, total=False):
    outlineFill: OutlineFill
    weight: Dimension
    dashStyle: DashStyle
    propertyState: PropertyState


class Shadow(TypedDict, total=False):
    """(Read-only to the API: Slides refuses a shadow in a write; a .pptx import keeps one.)"""
    type: Literal["SHADOW_TYPE_UNSPECIFIED", "OUTER"]
    transform: AffineTransform
    alignment: RectanglePosition
    blurRadius: Dimension
    color: OpaqueColor
    alpha: float
    rotateWithShape: bool
    propertyState: PropertyState


class ShapeBackgroundFill(TypedDict, total=False):
    propertyState: PropertyState
    solidFill: SolidFill


class Autofit(TypedDict, total=False):
    autofitType: Literal["AUTOFIT_TYPE_UNSPECIFIED", "NONE", "TEXT_AUTOFIT", "SHAPE_AUTOFIT"]
    fontScale: float
    lineSpacingReduction: float


class ShapeProperties(TypedDict, total=False):
    shapeBackgroundFill: ShapeBackgroundFill
    outline: Outline
    shadow: Shadow
    link: SlidesLink
    contentAlignment: ContentAlignment
    autofit: Autofit


class StretchedPictureFill(TypedDict, total=False):
    contentUrl: str
    size: Size


class PageBackgroundFill(TypedDict, total=False):
    propertyState: PropertyState
    solidFill: SolidFill
    stretchedPictureFill: StretchedPictureFill


class ThemeColorPair(TypedDict, total=False):
    type: ThemeColorType
    color: SlidesRgbColor


class ColorScheme(TypedDict, total=False):
    colors: list[ThemeColorPair]


class PageProperties(TypedDict, total=False):
    pageBackgroundFill: PageBackgroundFill
    colorScheme: ColorScheme


class LineFill(TypedDict, total=False):
    solidFill: SolidFill


class LineConnection(TypedDict, total=False):
    connectedObjectId: str
    connectionSiteIndex: int


class LineProperties(TypedDict, total=False):
    lineFill: LineFill
    weight: Dimension
    dashStyle: DashStyle
    startArrow: ArrowStyle
    endArrow: ArrowStyle
    link: SlidesLink
    startConnection: LineConnection
    endConnection: LineConnection


class SlidesCropProperties(TypedDict, total=False):
    leftOffset: float
    rightOffset: float
    topOffset: float
    bottomOffset: float
    angle: float


class ColorStop(TypedDict, total=False):
    color: OpaqueColor
    alpha: float
    position: float


class Recolor(TypedDict, total=False):
    recolorStops: list[ColorStop]
    name: Literal["NONE", "LIGHT1", "LIGHT2", "LIGHT3", "LIGHT4", "LIGHT5", "LIGHT6", "LIGHT7", "LIGHT8", "LIGHT9",
                  "LIGHT10", "DARK1", "DARK2", "DARK3", "DARK4", "DARK5", "DARK6", "DARK7", "DARK8", "DARK9", "DARK10",
                  "GRAYSCALE", "NEGATIVE", "SEPIA", "CUSTOM"]


class SlidesImageProperties(TypedDict, total=False):
    """(Slides documents crop, recolor and shadow as read-only; `tools/probe_images` writes them
    anyway to see what Slides does.)"""
    cropProperties: SlidesCropProperties
    transparency: float
    brightness: float
    contrast: float
    recolor: Recolor
    outline: Outline
    shadow: Shadow
    link: SlidesLink


class TableCellBackgroundFill(TypedDict, total=False):
    propertyState: PropertyState
    solidFill: SolidFill


class TableCellProperties(TypedDict, total=False):
    tableCellBackgroundFill: TableCellBackgroundFill
    contentAlignment: ContentAlignment


class TableBorderFill(TypedDict, total=False):
    solidFill: SolidFill


class TableBorderProperties(TypedDict, total=False):
    tableBorderFill: TableBorderFill
    weight: Dimension
    dashStyle: DashStyle


class TableRowProperties(TypedDict, total=False):
    minRowHeight: Dimension


class SlidesTableColumnProperties(TypedDict, total=False):
    columnWidth: Dimension


class SlidesTableCellLocation(TypedDict, total=False):
    rowIndex: int
    columnIndex: int


class SlidesTableRange(TypedDict, total=False):
    location: SlidesTableCellLocation
    rowSpan: int
    columnSpan: int


class SlidesRange(TypedDict, total=False):
    type: Required[Literal["ALL", "FIXED_RANGE", "FROM_START_INDEX"]]   # (every range we write says its type)
    startIndex: int
    endIndex: int


class SlidesPageElementProperties(TypedDict, total=False):
    pageObjectId: Required[str]      # (a created element always names its page)
    size: Size
    transform: AffineTransform


PlaceholderType = Literal["NONE", "BODY", "CHART", "CLIP_ART", "CENTERED_TITLE", "DIAGRAM", "DATE_AND_TIME", "FOOTER",
                          "HEADER", "MEDIA", "OBJECT", "PICTURE", "SLIDE_NUMBER", "SUBTITLE", "TABLE", "TITLE",
                          "SLIDE_IMAGE"]
PLACEHOLDER_TYPES: frozenset[str] = frozenset(get_args(PlaceholderType))


def is_placeholder_type(value: str) -> TypeGuard[PlaceholderType]:
    return value in PLACEHOLDER_TYPES


class Placeholder(TypedDict, total=False):
    type: PlaceholderType
    index: int
    parentObjectId: str


class LayoutReference(TypedDict, total=False):
    layoutId: str
    predefinedLayout: Literal["PREDEFINED_LAYOUT_UNSPECIFIED", "BLANK", "CAPTION_ONLY", "TITLE", "TITLE_AND_BODY",
                              "TITLE_AND_TWO_COLUMNS", "TITLE_ONLY", "SECTION_HEADER",
                              "SECTION_TITLE_AND_DESCRIPTION", "ONE_COLUMN_TEXT", "MAIN_POINT", "BIG_NUMBER"]


class LayoutPlaceholderIdMapping(TypedDict, total=False):
    layoutPlaceholder: Placeholder
    layoutPlaceholderObjectId: str
    objectId: str


# ------------------------------------------------------------------------------ parsing a part
# A part read back from Slides (or Docs) and written again (a text style sync copies, a fill a
# layout had) is parsed where it is read: checked against its TypedDict, key by key and value by
# value, all the way down, and handed on as that type. One walker reads every TypedDict's own
# annotations (its field table), so a field added to a TypedDict is checked with no code to add
# here. It checks what a value holds, not what it lacks: a `Required` field left out is not said.

TD = TypeVar("TD")
_FIELDS: dict[object, dict[str, object]] = {}


class _Bare:
    """`Required[X]` read as X: `Required` is imported for the checker only, and a field that is
    required is still just an X to the walker (which checks what is there, not what is missing)."""

    def __getitem__(self, item: object) -> object:
        return item


def _fields_of(shape: object) -> dict[str, object]:
    fields = _FIELDS.get(shape)
    if fields is None:
        fields = _FIELDS[shape] = dict(get_type_hints(shape, localns={"Required": _Bare()}))
    return fields


def _name_of(hint: object) -> str:
    name = getattr(hint, "__name__", None)
    return name if isinstance(name, str) else str(hint)


def _check(value: object, hint: object, where: str) -> None:
    """`value` holds what `hint` says, or a JsonShapeError naming where it does not."""
    if hint == Json or hint is object:
        return                          # (JSON the type leaves unmodelled: it came from JSON)
    if isinstance(hint, ForwardRef):    # (`Json` inside itself, as `get_type_hints` leaves it)
        if hint.__forward_arg__ == "Json" and _is_json(value):
            return
        raise JsonShapeError(f"{where}: {type(value).__name__} is no {hint.__forward_arg__}")
    if is_typeddict(hint):
        if not isinstance(value, dict):
            raise JsonShapeError(f"{where}: a {_name_of(hint)} object was expected, found {type(value).__name__}")
        fields = _fields_of(hint)
        for key, v in value.items():
            if key not in fields:
                raise JsonShapeError(f"{where}: {key!r} is no field of {_name_of(hint)}")
            _check(v, fields[key], f"{where}.{key}")
        return
    origin, args = get_origin(hint), get_args(hint)
    if origin is Literal:
        if not any(type(value) is type(a) and value == a for a in args):
            raise JsonShapeError(f"{where}: {value!r} is none of {', '.join(map(repr, args))}")
    elif origin is Union or origin is UnionType:
        problems: list[str] = []
        for arm in args:
            try:
                _check(value, arm, where)
                return
            except JsonShapeError as e:
                problems.append(str(e))
        raise JsonShapeError(" / ".join(problems))
    elif origin is list or origin is Sequence:
        if not isinstance(value, list):
            raise JsonShapeError(f"{where}: an array was expected, found {type(value).__name__}")
        for i, item in enumerate(value):
            _check(item, args[0], f"{where}[{i}]")
    elif origin is dict or origin is Mapping:
        if not isinstance(value, dict):
            raise JsonShapeError(f"{where}: an object was expected, found {type(value).__name__}")
        for key, item in value.items():
            _check(key, args[0], f"{where} (a key)")
            _check(item, args[1], f"{where}[{key!r}]")
    elif hint is type(None):
        if value is not None:
            raise JsonShapeError(f"{where}: null was expected, found {type(value).__name__}")
    elif hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise JsonShapeError(f"{where}: a number was expected, found {type(value).__name__}")
    elif hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise JsonShapeError(f"{where}: an integer was expected, found {type(value).__name__}")
    elif hint is bool or hint is str:
        if not isinstance(value, hint):
            raise JsonShapeError(f"{where}: a {_name_of(hint)} was expected, found {type(value).__name__}")
    else:
        raise TypeError(f"{where}: no runtime check for {hint!r}")


def _holds(o: JsonObject, shape: type[TD], where: str) -> TypeGuard[TD]:
    _check(o, shape, where)
    return True


def typed_part(o: JsonObject, shape: type[TD], where: str) -> TD:
    """`o` as the TypedDict `shape` of this module (checked all the way down, not copied), or a
    JsonShapeError naming where in `where` it is not."""
    if not _holds(o, shape, where):
        raise JsonShapeError(f"{where}: not a {_name_of(shape)}")   # (unreached: _holds raises first)
    return o


def slides_text_style(o: JsonObject, where: str) -> SlidesTextStyle:
    return typed_part(o, SlidesTextStyle, where)


def slides_paragraph_style(o: JsonObject, where: str) -> SlidesParagraphStyle:
    return typed_part(o, SlidesParagraphStyle, where)


def shape_properties(o: JsonObject, where: str) -> ShapeProperties:
    return typed_part(o, ShapeProperties, where)


def page_properties(o: JsonObject, where: str) -> PageProperties:
    return typed_part(o, PageProperties, where)


def solid_fill(o: JsonObject, where: str) -> SolidFill:
    return typed_part(o, SolidFill, where)


def opaque_color(o: JsonObject, where: str) -> OpaqueColor:
    return typed_part(o, OpaqueColor, where)


def layout_placeholder_id_mapping(o: JsonObject, where: str) -> LayoutPlaceholderIdMapping:
    return typed_part(o, LayoutPlaceholderIdMapping, where)


# ------------------------------------------------------------------------------ Slides: requests
# Every request kind this package sends, one TypedDict per kind, and the oneof `SlidesRequest`.
# `Required` marks what the API needs to act (the object an update names, its field mask, the
# text an insert writes) and every one of our producers sends; the rest is optional, as Google's
# schema has it. A batch built as JSON still takes a built request through `slides_json`.


class DeleteObjectRequest(TypedDict, total=False):
    objectId: Required[str]


class UpdatePageElementTransformRequest(TypedDict, total=False):
    objectId: Required[str]
    transform: Required[AffineTransform]
    applyMode: Required[Literal["RELATIVE", "ABSOLUTE"]]


class UpdatePageElementsZOrderRequest(TypedDict, total=False):
    pageElementObjectIds: Required[list[str]]
    operation: Required[Literal["BRING_TO_FRONT", "BRING_FORWARD", "SEND_BACKWARD", "SEND_TO_BACK"]]


class UpdatePageElementAltTextRequest(TypedDict, total=False):
    objectId: Required[str]
    title: str
    description: str


class UpdateSlidesPositionRequest(TypedDict, total=False):
    slideObjectIds: Required[list[str]]
    insertionIndex: Required[int]


class CreateSlideRequest(TypedDict, total=False):
    objectId: str
    insertionIndex: int
    slideLayoutReference: LayoutReference
    placeholderIdMappings: list[LayoutPlaceholderIdMapping]


class CreateImageRequest(TypedDict, total=False):
    objectId: str
    url: Required[str]
    elementProperties: SlidesPageElementProperties


class CreateShapeRequest(TypedDict, total=False):
    objectId: str
    shapeType: Required[ShapeType]
    elementProperties: SlidesPageElementProperties


class CreateLineRequest(TypedDict, total=False):
    objectId: str
    category: Literal["LINE_CATEGORY_UNSPECIFIED", "STRAIGHT", "BENT", "CURVED"]   # (deprecated: lineCategory)
    lineCategory: LineCategory
    elementProperties: SlidesPageElementProperties


class CreateTableRequest(TypedDict, total=False):
    objectId: str
    elementProperties: SlidesPageElementProperties
    rows: Required[int]
    columns: Required[int]


class DuplicateObjectRequest(TypedDict, total=False):
    objectId: Required[str]
    objectIds: Mapping[str, str]


class UpdateImagePropertiesRequest(TypedDict, total=False):
    objectId: Required[str]
    imageProperties: Required[SlidesImageProperties]
    fields: Required[str]


class UpdateLinePropertiesRequest(TypedDict, total=False):
    objectId: Required[str]
    lineProperties: Required[LineProperties]
    fields: Required[str]


class UpdateSlidePropertiesRequest(TypedDict, total=False):
    objectId: Required[str]
    slideProperties: Required[SlideProperties]
    fields: Required[str]


class UpdateTableBorderPropertiesRequest(TypedDict, total=False):
    objectId: Required[str]
    tableRange: SlidesTableRange
    borderPosition: Literal["ALL", "BOTTOM", "INNER", "INNER_HORIZONTAL", "INNER_VERTICAL", "LEFT", "OUTER", "RIGHT",
                            "TOP"]
    tableBorderProperties: Required[TableBorderProperties]
    fields: Required[str]


class UpdateTableCellPropertiesRequest(TypedDict, total=False):
    objectId: Required[str]
    tableRange: SlidesTableRange
    tableCellProperties: Required[TableCellProperties]
    fields: Required[str]


class UpdateTableRowPropertiesRequest(TypedDict, total=False):
    objectId: Required[str]
    rowIndices: list[int]
    tableRowProperties: Required[TableRowProperties]
    fields: Required[str]


class SlidesUpdateTableColumnPropertiesRequest(TypedDict, total=False):
    objectId: Required[str]
    columnIndices: list[int]
    tableColumnProperties: Required[SlidesTableColumnProperties]
    fields: Required[str]


BulletPreset = Literal[
    "BULLET_DISC_CIRCLE_SQUARE", "BULLET_DIAMONDX_ARROW3D_SQUARE", "BULLET_CHECKBOX", "BULLET_ARROW_DIAMOND_DISC",
    "BULLET_STAR_CIRCLE_SQUARE", "BULLET_ARROW3D_CIRCLE_SQUARE", "BULLET_LEFTTRIANGLE_DIAMOND_DISC",
    "BULLET_DIAMONDX_HOLLOWDIAMOND_SQUARE", "BULLET_DIAMOND_CIRCLE_SQUARE", "NUMBERED_DIGIT_ALPHA_ROMAN",
    "NUMBERED_DIGIT_ALPHA_ROMAN_PARENS", "NUMBERED_DIGIT_NESTED", "NUMBERED_UPPERALPHA_ALPHA_ROMAN",
    "NUMBERED_UPPERROMAN_UPPERALPHA_DIGIT", "NUMBERED_ZERODIGIT_ALPHA_ROMAN"]


class SlidesCreateParagraphBulletsRequest(TypedDict, total=False):
    objectId: Required[str]
    cellLocation: SlidesTableCellLocation
    textRange: SlidesRange
    bulletPreset: BulletPreset


class SlidesDeleteParagraphBulletsRequest(TypedDict, total=False):
    objectId: Required[str]
    cellLocation: SlidesTableCellLocation
    textRange: SlidesRange


class SlidesMergeTableCellsRequest(TypedDict, total=False):
    objectId: Required[str]
    tableRange: Required[SlidesTableRange]


class SlidesReplaceImageRequest(TypedDict, total=False):
    imageObjectId: Required[str]
    url: Required[str]
    imageReplaceMethod: Literal["IMAGE_REPLACE_METHOD_UNSPECIFIED", "CENTER_INSIDE", "CENTER_CROP"]


class SlidesDeleteTextRequest(TypedDict, total=False):
    objectId: Required[str]
    cellLocation: SlidesTableCellLocation
    textRange: SlidesRange


class SlidesInsertTextRequest(TypedDict, total=False):
    objectId: Required[str]
    cellLocation: SlidesTableCellLocation
    text: Required[str]
    insertionIndex: int


class SlidesUpdateTextStyleRequest(TypedDict, total=False):
    objectId: Required[str]
    cellLocation: SlidesTableCellLocation
    style: Required[SlidesTextStyle]
    textRange: SlidesRange
    fields: Required[str]


class SlidesUpdateParagraphStyleRequest(TypedDict, total=False):
    objectId: Required[str]
    cellLocation: SlidesTableCellLocation
    style: Required[SlidesParagraphStyle]
    textRange: SlidesRange
    fields: Required[str]


class UpdatePagePropertiesRequest(TypedDict, total=False):
    objectId: Required[str]
    pageProperties: Required[PageProperties]
    fields: Required[str]


class UpdateShapePropertiesRequest(TypedDict, total=False):
    objectId: Required[str]
    shapeProperties: Required[ShapeProperties]
    fields: Required[str]


class InsertTableRowsRequest(TypedDict, total=False):
    tableObjectId: Required[str | None]
    """(None: a step planned before the table is named, `sync.table_steps`)"""
    cellLocation: SlidesTableCellLocation
    insertBelow: bool
    number: int


class InsertTableColumnsRequest(TypedDict, total=False):
    tableObjectId: Required[str | None]
    cellLocation: SlidesTableCellLocation
    insertRight: bool
    number: int


class DeleteTableRowRequest(TypedDict, total=False):
    tableObjectId: Required[str | None]
    cellLocation: Required[SlidesTableCellLocation]


class DeleteTableColumnRequest(TypedDict, total=False):
    tableObjectId: Required[str | None]
    cellLocation: Required[SlidesTableCellLocation]


class GroupObjectsRequest(TypedDict, total=False):
    groupObjectId: str
    childrenObjectIds: Required[list[str]]


class UngroupObjectsRequest(TypedDict, total=False):
    objectIds: Required[list[str]]


class SlidesRequest(TypedDict, total=False):
    """One request of a `presentations.batchUpdate`: exactly one of these is set
    (`slides_request_kind` says which)."""
    createImage: CreateImageRequest
    createLine: CreateLineRequest
    createShape: CreateShapeRequest
    createSlide: CreateSlideRequest
    createTable: CreateTableRequest
    deleteObject: DeleteObjectRequest
    deleteText: SlidesDeleteTextRequest
    duplicateObject: DuplicateObjectRequest
    groupObjects: GroupObjectsRequest
    ungroupObjects: UngroupObjectsRequest
    insertTableColumns: InsertTableColumnsRequest
    insertTableRows: InsertTableRowsRequest
    insertText: SlidesInsertTextRequest
    updateImageProperties: UpdateImagePropertiesRequest
    updateLineProperties: UpdateLinePropertiesRequest
    updatePageElementAltText: UpdatePageElementAltTextRequest
    updatePageElementTransform: UpdatePageElementTransformRequest
    updatePageElementsZOrder: UpdatePageElementsZOrderRequest
    updatePageProperties: UpdatePagePropertiesRequest
    updateShapeProperties: UpdateShapePropertiesRequest
    updateSlideProperties: UpdateSlidePropertiesRequest
    updateSlidesPosition: UpdateSlidesPositionRequest
    updateTableBorderProperties: UpdateTableBorderPropertiesRequest
    updateTableCellProperties: UpdateTableCellPropertiesRequest
    updateTableRowProperties: UpdateTableRowPropertiesRequest
    updateTableColumnProperties: SlidesUpdateTableColumnPropertiesRequest
    createParagraphBullets: SlidesCreateParagraphBulletsRequest
    deleteParagraphBullets: SlidesDeleteParagraphBulletsRequest
    deleteTableColumn: DeleteTableColumnRequest
    deleteTableRow: DeleteTableRowRequest
    mergeTableCells: SlidesMergeTableCellsRequest
    replaceImage: SlidesReplaceImageRequest
    updateParagraphStyle: SlidesUpdateParagraphStyleRequest
    updateTextStyle: SlidesUpdateTextStyleRequest


SlidesRequestKind = Literal[
    "createImage", "createLine", "createShape", "createSlide", "createTable", "deleteObject", "deleteText",
    "duplicateObject", "groupObjects", "ungroupObjects", "insertTableColumns", "insertTableRows", "insertText",
    "updateImageProperties", "updateLineProperties", "updatePageElementAltText", "updatePageElementTransform",
    "updatePageElementsZOrder", "updatePageProperties", "updateShapeProperties", "updateSlideProperties",
    "updateSlidesPosition", "updateTableBorderProperties", "updateTableCellProperties", "updateTableRowProperties",
    "updateTableColumnProperties", "createParagraphBullets", "deleteParagraphBullets", "deleteTableColumn",
    "deleteTableRow", "mergeTableCells", "replaceImage", "updateParagraphStyle", "updateTextStyle"]
SLIDES_REQUEST_KINDS: tuple[SlidesRequestKind, ...] = (
    "createImage", "createLine", "createShape", "createSlide", "createTable", "deleteObject", "deleteText",
    "duplicateObject", "groupObjects", "ungroupObjects", "insertTableColumns", "insertTableRows", "insertText",
    "updateImageProperties", "updateLineProperties", "updatePageElementAltText", "updatePageElementTransform",
    "updatePageElementsZOrder", "updatePageProperties", "updateShapeProperties", "updateSlideProperties",
    "updateSlidesPosition", "updateTableBorderProperties", "updateTableCellProperties", "updateTableRowProperties",
    "updateTableColumnProperties", "createParagraphBullets", "deleteParagraphBullets", "deleteTableColumn",
    "deleteTableRow", "mergeTableCells", "replaceImage", "updateParagraphStyle", "updateTextStyle")
"""(Held equal to `SlidesRequestKind` and to `SlidesRequest`'s keys by `tests/test_google_schema.py`.)"""


def slides_request_kind(request: SlidesRequest) -> SlidesRequestKind:
    """Which kind `request` is: the one key it sets, or a ValueError when it sets none or several
    (Google refuses a request that is not exactly one kind)."""
    kinds: list[SlidesRequestKind] = []
    for kind in SLIDES_REQUEST_KINDS:
        if kind in request:
            kinds.append(kind)
    if len(kinds) != 1:
        raise ValueError(f"a Slides request of {sorted(request)} is {len(kinds)} kinds, not one")
    return kinds[0]


def slides_json(request: SlidesRequest) -> JsonObject:
    """A built request as the JSON of the batch it joins: checked all the way down, not copied."""
    value: object = request
    if not _is_json_object(value):
        raise JsonShapeError(f"a Slides request of {sorted(request)}: not JSON")
    return value


# (emit's side: what its planners hand on as typed values)
BULLET_PRESETS: frozenset[str] = frozenset(get_args(BulletPreset))


def is_bullet_preset(value: str) -> TypeGuard[BulletPreset]:
    return value in BULLET_PRESETS


def bullet_preset(value: str, where: str) -> BulletPreset:
    """`value` as a `createParagraphBullets` preset, or an error naming `where` (Slides refuses a
    preset it does not know, with the whole batch)."""
    if not is_bullet_preset(value):
        raise JsonShapeError(f"{where}: {value!r} is no Slides bullet preset")
    return value


def part_json(value: Mapping[str, object], where: str) -> JsonObject:
    """A typed part (a text style a record holds) as the JSON a file keeps it as: checked all the
    way down, not copied (`slides_json` for a whole request)."""
    found: object = value
    if not _is_json_object(found):
        raise JsonShapeError(f"{where}: not JSON")
    return found


def slides_request(o: JsonObject, where: str) -> SlidesRequest:
    """A request built as JSON (a caller not typed yet) as a `SlidesRequest`: checked all the way
    down, exactly one kind, not copied."""
    request = typed_part(o, SlidesRequest, where)
    try:
        slides_request_kind(request)
    except ValueError as e:
        raise JsonShapeError(f"{where}: {e}") from e
    return request


# ------------------------------------------------------------------------------ Slides: calls


class BatchUpdateBody(TypedDict, total=False):
    requests: Required[Sequence[Mapping[str, object]]]
    writeControl: WriteControl


class NewPresentation(TypedDict, total=False):
    title: str
    pageSize: Size          # (ignored: every presentation Slides creates is 16:9, hence the .pptx route)


class GetPresentation(TypedDict, total=False):
    presentationId: Required[str]
    fields: str


class CreatePresentation(TypedDict, total=False):
    body: Required[NewPresentation]


class UpdatePresentation(TypedDict, total=False):
    presentationId: Required[str]
    body: Required[BatchUpdateBody]


class GetThumbnail(TypedDict, total=False):
    presentationId: Required[str]
    pageObjectId: Required[str]
    thumbnailProperties_mimeType: Literal["PNG"]
    thumbnailProperties_thumbnailSize: Literal["LARGE", "MEDIUM", "SMALL"]


class GetPage(TypedDict, total=False):
    presentationId: Required[str]
    pageObjectId: Required[str]


class Pages(Protocol):
    def get(self, **kw: Unpack[GetPage]) -> Request[Page]: ...
    def getThumbnail(self, **kw: Unpack[GetThumbnail]) -> Request[Thumbnail]: ...


class Presentations(Protocol):
    def get(self, **kw: Unpack[GetPresentation]) -> Request[Presentation]: ...
    def create(self, **kw: Unpack[CreatePresentation]) -> Request[Presentation]: ...
    def batchUpdate(self, **kw: Unpack[UpdatePresentation]) -> Request[BatchUpdateResponse]: ...
    def pages(self) -> Pages: ...


@runtime_checkable
class SlidesService(Protocol):
    """The Slides API v1 client (`google_auth.slides_service`)."""

    def presentations(self) -> Presentations: ...


# ------------------------------------------------------------------------------ Drive


class DriveFile(TypedDict, total=False):
    """A Drive file's metadata, as far as `fields=` asked for it."""
    id: str
    name: str
    mimeType: str
    parents: list[str]
    appProperties: dict[str, str]
    trashed: bool
    createdTime: str
    modifiedTime: str
    size: str               # (bytes, as a decimal string: Drive's int64; none for a Google document)


class FileList(TypedDict, total=False):
    files: list[DriveFile]
    nextPageToken: str


class Empty(TypedDict, total=False):
    """What a call that answers nothing answers (`files.delete`)."""


class FileBody(TypedDict, total=False):
    """What `files.create` / `copy` / `update` write. An appProperty set to None is removed."""
    name: str
    mimeType: str
    parents: list[str]
    appProperties: Mapping[str, str | None]
    trashed: bool


class ListFiles(TypedDict, total=False):
    q: str
    spaces: str
    fields: str
    pageSize: int
    pageToken: str | None


class CreateFile(TypedDict, total=False):
    body: Required[FileBody]
    fields: str
    media_body: MediaBody


class FileId(TypedDict, total=False):
    fileId: Required[str]


class GetFile(TypedDict, total=False):
    fileId: Required[str]
    fields: str


class UpdateFile(TypedDict, total=False):
    fileId: Required[str]
    body: FileBody
    fields: str
    media_body: MediaBody


class CopyFile(TypedDict, total=False):
    fileId: Required[str]
    body: FileBody
    fields: str


class ExportFile(TypedDict, total=False):
    fileId: Required[str]
    mimeType: Required[str]


class Files(Protocol):
    def list(self, **kw: Unpack[ListFiles]) -> Request[FileList]: ...
    def get(self, **kw: Unpack[GetFile]) -> Request[DriveFile]: ...
    def create(self, **kw: Unpack[CreateFile]) -> Request[DriveFile]: ...
    def update(self, **kw: Unpack[UpdateFile]) -> Request[DriveFile]: ...
    def copy(self, **kw: Unpack[CopyFile]) -> Request[DriveFile]: ...
    def delete(self, **kw: Unpack[FileId]) -> Request[Empty]: ...
    def get_media(self, **kw: Unpack[FileId]) -> Request[bytes]: ...
    def export(self, **kw: Unpack[ExportFile]) -> Request[bytes]: ...
    def export_media(self, **kw: Unpack[ExportFile]) -> Request[bytes]: ...


def file_id(answer: DriveFile, what: str) -> str:
    """The id Drive answered for `what` (a file just created, copied or updated with `fields`
    naming `id`): every key of an answer is optional to the type, and this one is not to the
    caller, so its absence is Drive breaking its word, said here once rather than as a KeyError."""
    fid = answer.get("id")
    if fid is None:
        raise ValueError(f"Drive answered no id for {what}")
    return fid


class Permission(TypedDict, total=False):
    id: str
    type: Literal["user", "group", "domain", "anyone"]
    role: Literal["owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"]


class CreatePermission(TypedDict, total=False):
    fileId: Required[str]
    body: Required[Permission]
    fields: str


class Permissions(Protocol):
    def create(self, **kw: Unpack[CreatePermission]) -> Request[Permission]: ...


class Comment(TypedDict, total=False):
    """A comment on a file (`doc_sync.open_comments` reads them for the report)."""
    id: str
    content: str
    resolved: bool
    author: JsonObject
    quotedFileContent: JsonObject
    replies: list[JsonObject]


class CommentList(TypedDict, total=False):
    comments: list[Comment]
    nextPageToken: str


class NewComment(TypedDict, total=False):
    content: Required[str]


class ListComments(TypedDict, total=False):
    fileId: Required[str]
    fields: Required[str]           # (Drive refuses a comments call without a field mask)
    includeDeleted: bool
    pageSize: int
    pageToken: str | None


class CreateComment(TypedDict, total=False):
    fileId: Required[str]
    fields: Required[str]
    body: Required[NewComment]


class Comments(Protocol):
    def list(self, **kw: Unpack[ListComments]) -> Request[CommentList]: ...
    def create(self, **kw: Unpack[CreateComment]) -> Request[Comment]: ...


class DriveUser(TypedDict, total=False):
    displayName: str
    emailAddress: str
    me: bool
    permissionId: str
    photoLink: str
    kind: str


class Revision(TypedDict, total=False):
    """One revision of a file's content (`revisions.list` / `get`)."""
    id: str
    kind: str
    mimeType: str
    modifiedTime: str
    keepForever: bool
    published: bool
    publishAuto: bool
    publishedOutsideDomain: bool
    publishedLink: str
    lastModifyingUser: DriveUser
    originalFilename: str
    md5Checksum: str
    size: str               # (Drive's int64, as for a file)
    exportLinks: dict[str, str]


class RevisionList(TypedDict, total=False):
    kind: str
    revisions: list[Revision]
    nextPageToken: str


class RevisionBody(TypedDict, total=False):
    """What `revisions.update` writes: the settings a person can change on a revision."""
    keepForever: bool
    published: bool
    publishAuto: bool
    publishedOutsideDomain: bool


class ListRevisions(TypedDict, total=False):
    fileId: Required[str]
    fields: str
    pageSize: int
    pageToken: str | None


class GetRevision(TypedDict, total=False):
    fileId: Required[str]
    revisionId: Required[str]
    fields: str
    acknowledgeAbuse: bool


class UpdateRevision(TypedDict, total=False):
    fileId: Required[str]
    revisionId: Required[str]
    body: Required[RevisionBody]
    fields: str


class Revisions(Protocol):
    def list(self, **kw: Unpack[ListRevisions]) -> Request[RevisionList]: ...
    def get(self, **kw: Unpack[GetRevision]) -> Request[Revision]: ...
    def update(self, **kw: Unpack[UpdateRevision]) -> Request[Revision]: ...


class About(TypedDict, total=False):
    """`about.get`, as far as `fields=` (which Drive requires here) asked for it."""
    kind: str
    user: DriveUser
    appInstalled: bool
    exportFormats: dict[str, list[str]]
    importFormats: dict[str, list[str]]
    maxImportSizes: dict[str, str]
    maxUploadSize: str
    storageQuota: JsonObject


class GetAbout(TypedDict, total=False):
    fields: Required[str]           # (Drive refuses an about call without a field mask)


class AboutResource(Protocol):
    def get(self, **kw: Unpack[GetAbout]) -> Request[About]: ...


@runtime_checkable
class DriveService(Protocol):
    """The Drive API v3 client (`google_auth.drive_service`)."""

    def files(self) -> Files: ...
    def permissions(self) -> Permissions: ...
    def comments(self) -> Comments: ...
    def revisions(self) -> Revisions: ...
    def about(self) -> AboutResource: ...


# ------------------------------------------------------------------------------ Docs


class DocsWriteControl(TypedDict, total=False):
    requiredRevisionId: str
    targetRevisionId: str


# The parts of a document `doc_ir` reads (its `_NODES` graph says the same thing, for the
# report of what it does not read). Styles, colours and sizes are also what the requests
# below write, so the two halves share them.


class DocsDimension(TypedDict, total=False):
    magnitude: float
    unit: Literal["UNIT_UNSPECIFIED", "PT"]


class DocsRgbColor(TypedDict, total=False):
    red: float
    green: float
    blue: float


class DocsColor(TypedDict, total=False):
    rgbColor: DocsRgbColor


class DocsOptionalColor(TypedDict, total=False):
    color: DocsColor


class DocsWeightedFontFamily(TypedDict, total=False):
    fontFamily: str
    weight: int


class DocsLink(TypedDict, total=False):
    url: str


class DocsTextStyle(TypedDict, total=False):
    bold: bool
    italic: bool
    underline: bool
    strikethrough: bool
    smallCaps: bool
    baselineOffset: DocsBaselineOffset
    weightedFontFamily: DocsWeightedFontFamily
    fontSize: DocsDimension
    foregroundColor: DocsOptionalColor
    backgroundColor: DocsOptionalColor
    link: DocsLink


class DocsParagraphBorder(TypedDict, total=False):
    color: DocsOptionalColor
    width: DocsDimension
    padding: DocsDimension
    dashStyle: DocsDashStyle


class DocsShading(TypedDict, total=False):
    backgroundColor: DocsOptionalColor


DocsNamedStyleType = Literal["NAMED_STYLE_TYPE_UNSPECIFIED", "NORMAL_TEXT", "TITLE", "SUBTITLE", "HEADING_1",
                           "HEADING_2", "HEADING_3", "HEADING_4", "HEADING_5", "HEADING_6"]


class DocsParagraphStyle(TypedDict, total=False):
    namedStyleType: DocsNamedStyleType
    alignment: DocsAlignment
    indentStart: DocsDimension
    indentFirstLine: DocsDimension
    lineSpacing: float
    spaceAbove: DocsDimension
    spaceBelow: DocsDimension
    shading: DocsShading
    borderTop: DocsParagraphBorder
    borderBottom: DocsParagraphBorder
    borderLeft: DocsParagraphBorder
    borderRight: DocsParagraphBorder
    pageBreakBefore: bool
    keepWithNext: bool


class DocsTextRun(TypedDict, total=False):
    content: str
    textStyle: DocsTextStyle
    suggestedInsertionIds: list[str]     # (suggestions, as `suggestionsViewMode=SUGGESTIONS_INLINE` shows them)
    suggestedDeletionIds: list[str]


class DocsDateElementProperties(TypedDict, total=False):
    displayText: str
    timestamp: str
    dateFormat: DocsDateFormat
    timeFormat: Literal["TIME_FORMAT_UNSPECIFIED", "TIME_FORMAT_DISABLED", "TIME_FORMAT_HOUR_MINUTE",
                        "TIME_FORMAT_HOUR_MINUTE_TIMEZONE"]
    locale: str


class DocsDateElement(TypedDict, total=False):
    dateElementProperties: DocsDateElementProperties


class DocsPersonProperties(TypedDict, total=False):
    name: str
    email: str


class DocsPerson(TypedDict, total=False):
    personProperties: DocsPersonProperties


class DocsRichLinkProperties(TypedDict, total=False):
    title: str
    uri: str
    mimeType: str


class DocsRichLink(TypedDict, total=False):
    richLinkProperties: DocsRichLinkProperties


class DocsFootnoteReference(TypedDict, total=False):
    footnoteNumber: str
    footnoteId: str


class DocsInlineObjectElement(TypedDict, total=False):
    inlineObjectId: str


class DocsParagraphElement(TypedDict, total=False):
    startIndex: int
    endIndex: int
    textRun: DocsTextRun
    dateElement: DocsDateElement
    person: DocsPerson
    richLink: DocsRichLink
    footnoteReference: DocsFootnoteReference
    equation: JsonObject
    inlineObjectElement: DocsInlineObjectElement
    horizontalRule: JsonObject


class DocsBullet(TypedDict, total=False):
    listId: str
    nestingLevel: int


class DocsParagraph(TypedDict, total=False):
    elements: list[DocsParagraphElement]
    paragraphStyle: DocsParagraphStyle
    bullet: DocsBullet


class DocsTableCell(TypedDict, total=False):
    startIndex: int
    endIndex: int
    content: list[DocsStructuralElement]


class DocsTableRowStyle(TypedDict, total=False):
    minRowHeight: DocsDimension
    tableHeader: bool
    preventOverflow: bool


class DocsTableRow(TypedDict, total=False):
    startIndex: int
    endIndex: int
    tableCells: list[DocsTableCell]
    tableRowStyle: DocsTableRowStyle


class DocsTableColumnProperties(TypedDict, total=False):
    width: DocsDimension
    widthType: Literal["WIDTH_TYPE_UNSPECIFIED", "EVENLY_DISTRIBUTED", "FIXED_WIDTH"]


class DocsTableStyle(TypedDict, total=False):
    tableColumnProperties: list[DocsTableColumnProperties]


class DocsTable(TypedDict, total=False):
    rows: int
    columns: int
    tableRows: list[DocsTableRow]
    tableStyle: DocsTableStyle


class DocsStructuralElement(TypedDict, total=False):
    startIndex: int
    endIndex: int
    paragraph: DocsParagraph
    table: DocsTable
    tableOfContents: JsonObject
    sectionBreak: JsonObject


class DocsBody(TypedDict, total=False):
    content: list[DocsStructuralElement]


class DocsNestingLevel(TypedDict, total=False):
    glyphSymbol: str
    glyphType: Literal["GLYPH_TYPE_UNSPECIFIED", "NONE", "DECIMAL", "ZERO_DECIMAL", "UPPER_ALPHA", "ALPHA",
                       "UPPER_ROMAN", "ROMAN"]
    glyphFormat: str
    startNumber: int


class DocsListProperties(TypedDict, total=False):
    nestingLevels: list[DocsNestingLevel]


class DocsList(TypedDict, total=False):
    listProperties: DocsListProperties


class DocsSize(TypedDict, total=False):
    width: DocsDimension
    height: DocsDimension


class DocsImageProperties(TypedDict, total=False):
    contentUri: str


class DocsEmbeddedObject(TypedDict, total=False):
    title: str
    description: str
    size: DocsSize
    imageProperties: DocsImageProperties


class DocsInlineObjectProperties(TypedDict, total=False):
    embeddedObject: DocsEmbeddedObject


class DocsInlineObject(TypedDict, total=False):
    objectId: str
    inlineObjectProperties: DocsInlineObjectProperties


class DocsNamedStyle(TypedDict, total=False):
    namedStyleType: DocsNamedStyleType
    textStyle: DocsTextStyle
    paragraphStyle: DocsParagraphStyle


class DocsNamedStyles(TypedDict, total=False):
    styles: list[DocsNamedStyle]


class DocsRange(TypedDict, total=False):
    startIndex: int
    endIndex: int
    segmentId: str
    tabId: str


class DocsNamedRange(TypedDict, total=False):
    namedRangeId: str
    name: str
    ranges: list[DocsRange]


class DocsNamedRanges(TypedDict, total=False):
    """All the ranges of one name (`namedRanges` maps a name to one of these)."""
    name: str
    namedRanges: list[DocsNamedRange]


class DocsTabProperties(TypedDict, total=False):
    tabId: str
    title: str
    parentTabId: str
    index: int
    nestingLevel: int


class DocsDocumentTab(TypedDict, total=False):
    body: DocsBody
    lists: dict[str, DocsList]
    inlineObjects: dict[str, DocsInlineObject]
    namedStyles: DocsNamedStyles
    namedRanges: dict[str, DocsNamedRanges]
    documentStyle: JsonObject
    headers: dict[str, DocsHeader]
    footers: dict[str, DocsFooter]
    footnotes: dict[str, DocsFootnote]
    positionedObjects: dict[str, DocsPositionedObject]


class DocsTab(TypedDict, total=False):
    tabProperties: DocsTabProperties
    documentTab: DocsDocumentTab
    childTabs: list[DocsTab]


class DocsBackground(TypedDict, total=False):
    color: DocsOptionalColor


class DocsDocumentFormat(TypedDict, total=False):
    documentMode: Literal["DOCUMENT_MODE_UNSPECIFIED", "PAGES", "PAGELESS"]


class DocsDocumentStyle(TypedDict, total=False):
    """A document's page setup: what `updateDocumentStyle` writes (read back as `documentStyle`,
    still JSON there: `doc_ir` does not read it yet)."""
    background: DocsBackground
    defaultHeaderId: str
    defaultFooterId: str
    evenPageHeaderId: str
    evenPageFooterId: str
    firstPageHeaderId: str
    firstPageFooterId: str
    documentFormat: DocsDocumentFormat
    flipPageOrientation: bool
    marginTop: DocsDimension
    marginBottom: DocsDimension
    marginLeft: DocsDimension
    marginRight: DocsDimension
    marginHeader: DocsDimension
    marginFooter: DocsDimension
    pageNumberStart: int
    pageSize: DocsSize
    useCustomHeaderFooterMargins: bool
    useEvenPageHeaderFooter: bool
    useFirstPageHeaderFooter: bool


class DocsHeader(TypedDict, total=False):
    headerId: str
    content: list[DocsStructuralElement]


class DocsFooter(TypedDict, total=False):
    footerId: str
    content: list[DocsStructuralElement]


class DocsFootnote(TypedDict, total=False):
    footnoteId: str
    content: list[DocsStructuralElement]


class DocsPositionedObjectPositioning(TypedDict, total=False):
    layout: Literal["POSITIONED_OBJECT_LAYOUT_UNSPECIFIED", "WRAP_TEXT", "BREAK_LEFT", "BREAK_RIGHT",
                    "BREAK_LEFT_RIGHT", "IN_FRONT_OF_TEXT", "BEHIND_TEXT"]
    leftOffset: DocsDimension
    topOffset: DocsDimension


class DocsPositionedObjectProperties(TypedDict, total=False):
    embeddedObject: DocsEmbeddedObject
    positioning: DocsPositionedObjectPositioning


class DocsPositionedObject(TypedDict, total=False):
    """A picture anchored to a paragraph rather than in its text (Docs' "wrap text")."""
    objectId: str
    positionedObjectProperties: DocsPositionedObjectProperties


SuggestionsViewMode = Literal["DEFAULT_FOR_CURRENT_ACCESS", "SUGGESTIONS_INLINE", "PREVIEW_SUGGESTIONS_ACCEPTED",
                              "PREVIEW_WITHOUT_SUGGESTIONS"]


class Document(TypedDict, total=False):
    """`documents.get`. With `includeTabsContent` the content is under `tabs`, not `body`; the
    headers, footers, footnotes and positioned objects of a document without tabs are here."""
    documentId: str
    title: str
    revisionId: str
    suggestionsViewMode: SuggestionsViewMode
    body: DocsBody
    tabs: list[DocsTab]
    headers: dict[str, DocsHeader]
    footers: dict[str, DocsFooter]
    footnotes: dict[str, DocsFootnote]
    positionedObjects: dict[str, DocsPositionedObject]
    namedRanges: dict[str, DocsNamedRanges]
    inlineObjects: dict[str, DocsInlineObject]
    lists: dict[str, DocsList]
    documentStyle: JsonObject
    namedStyles: DocsNamedStyles


# The requests `doc_merge` and `doc_ir` write, as the discovery document spells them. A
# `Request` is a oneof: exactly one of its keys is set, which is how the API says it and
# how a reader (`devtools/doc_world`) asks which one it holds (`request.get(...)`).


class DocsLocation(TypedDict, total=False):
    index: Required[int]
    tabId: str
    segmentId: str


class DocsEndOfSegmentLocation(TypedDict, total=False):
    tabId: str
    segmentId: str


class DocsRangeWrite(TypedDict, total=False):
    """A `Range` a request names: always both ends, a tab where it is not the first."""
    startIndex: Required[int]
    endIndex: Required[int]
    tabId: str
    segmentId: str


class DocsTabsCriteria(TypedDict, total=False):
    tabIds: list[str]


class InsertTextRequest(TypedDict, total=False):
    text: Required[str]
    location: DocsLocation
    endOfSegmentLocation: DocsEndOfSegmentLocation


class DeleteContentRangeRequest(TypedDict, total=False):
    range: Required[DocsRangeWrite]


class UpdateTextStyleRequest(TypedDict, total=False):
    range: Required[DocsRangeWrite]
    textStyle: Required[DocsTextStyle]
    fields: Required[str]


class UpdateParagraphStyleRequest(TypedDict, total=False):
    range: Required[DocsRangeWrite]
    paragraphStyle: Required[DocsParagraphStyle]
    fields: Required[str]


DocsBulletPreset = Literal[
    "BULLET_GLYPH_PRESET_UNSPECIFIED", "BULLET_DISC_CIRCLE_SQUARE", "BULLET_DIAMONDX_ARROW3D_SQUARE", "BULLET_CHECKBOX",
    "BULLET_ARROW_DIAMOND_DISC", "BULLET_STAR_CIRCLE_SQUARE", "BULLET_ARROW3D_CIRCLE_SQUARE",
    "BULLET_LEFTTRIANGLE_DIAMOND_DISC", "BULLET_DIAMONDX_HOLLOWDIAMOND_SQUARE", "BULLET_DIAMOND_CIRCLE_SQUARE",
    "NUMBERED_DECIMAL_ALPHA_ROMAN", "NUMBERED_DECIMAL_ALPHA_ROMAN_PARENS", "NUMBERED_DECIMAL_NESTED",
    "NUMBERED_UPPERALPHA_ALPHA_ROMAN", "NUMBERED_UPPERROMAN_UPPERALPHA_DECIMAL", "NUMBERED_ZERODECIMAL_ALPHA_ROMAN"]
"""(Not Slides' presets: Docs numbers with DECIMAL where Slides says DIGIT.)"""


class CreateParagraphBulletsRequest(TypedDict, total=False):
    range: Required[DocsRangeWrite]
    bulletPreset: Required[DocsBulletPreset]


class DeleteParagraphBulletsRequest(TypedDict, total=False):
    range: Required[DocsRangeWrite]


class CreateNamedRangeRequest(TypedDict, total=False):
    name: Required[str]
    range: Required[DocsRangeWrite]


class DeleteNamedRangeRequest(TypedDict, total=False):
    namedRangeId: str
    name: str
    tabsCriteria: DocsTabsCriteria


class InsertTableRequest(TypedDict, total=False):
    rows: Required[int]
    columns: Required[int]
    location: DocsLocation
    endOfSegmentLocation: DocsEndOfSegmentLocation


class DocsTableCellLocation(TypedDict, total=False):
    tableStartLocation: Required[DocsLocation]
    rowIndex: Required[int]
    columnIndex: Required[int]


class InsertTableRowRequest(TypedDict, total=False):
    tableCellLocation: Required[DocsTableCellLocation]
    insertBelow: Required[bool]


class InsertTableColumnRequest(TypedDict, total=False):
    tableCellLocation: Required[DocsTableCellLocation]
    insertRight: Required[bool]


class DeleteTableLineRequest(TypedDict, total=False):
    """`deleteTableRow` and `deleteTableColumn`: the line through one cell."""
    tableCellLocation: Required[DocsTableCellLocation]


class DocsObjectSize(TypedDict, total=False):
    width: DocsDimension
    height: DocsDimension


class InsertInlineImageRequest(TypedDict, total=False):
    uri: Required[str]
    location: DocsLocation
    endOfSegmentLocation: DocsEndOfSegmentLocation
    objectSize: DocsObjectSize


class InsertPersonRequest(TypedDict, total=False):
    location: Required[DocsLocation]
    personProperties: Required[DocsPersonProperties]


class InsertDateRequest(TypedDict, total=False):
    location: Required[DocsLocation]
    dateElementProperties: Required[DocsDateElementProperties]


class AddDocumentTabRequest(TypedDict, total=False):
    tabProperties: Required[DocsTabProperties]


class UpdateDocumentTabPropertiesRequest(TypedDict, total=False):
    tabProperties: Required[DocsTabProperties]
    fields: Required[str]


class DeleteTabRequest(TypedDict, total=False):
    tabId: Required[str]


class CreateHeaderRequest(TypedDict, total=False):
    type: Required[Literal["HEADER_FOOTER_TYPE_UNSPECIFIED", "DEFAULT"]]
    sectionBreakLocation: DocsLocation


class CreateFooterRequest(TypedDict, total=False):
    type: Required[Literal["HEADER_FOOTER_TYPE_UNSPECIFIED", "DEFAULT"]]
    sectionBreakLocation: DocsLocation


class CreateFootnoteRequest(TypedDict, total=False):
    location: DocsLocation
    endOfSegmentLocation: DocsEndOfSegmentLocation


class InsertPageBreakRequest(TypedDict, total=False):
    location: DocsLocation
    endOfSegmentLocation: DocsEndOfSegmentLocation


class InsertSectionBreakRequest(TypedDict, total=False):
    sectionType: Required[Literal["SECTION_TYPE_UNSPECIFIED", "CONTINUOUS", "NEXT_PAGE"]]
    location: DocsLocation
    endOfSegmentLocation: DocsEndOfSegmentLocation


class InsertRichLinkRequest(TypedDict, total=False):
    richLinkProperties: Required[DocsRichLinkProperties]
    location: DocsLocation
    endOfSegmentLocation: DocsEndOfSegmentLocation


class UpdateDocumentStyleRequest(TypedDict, total=False):
    documentStyle: Required[DocsDocumentStyle]
    fields: Required[str]
    tabId: str


class DocsRequest(TypedDict, total=False):
    """One request of a `documents.batchUpdate`: exactly one of these is set."""
    insertText: InsertTextRequest
    deleteContentRange: DeleteContentRangeRequest
    updateTextStyle: UpdateTextStyleRequest
    updateParagraphStyle: UpdateParagraphStyleRequest
    createParagraphBullets: CreateParagraphBulletsRequest
    deleteParagraphBullets: DeleteParagraphBulletsRequest
    createNamedRange: CreateNamedRangeRequest
    deleteNamedRange: DeleteNamedRangeRequest
    insertTable: InsertTableRequest
    insertTableRow: InsertTableRowRequest
    insertTableColumn: InsertTableColumnRequest
    deleteTableRow: DeleteTableLineRequest
    deleteTableColumn: DeleteTableLineRequest
    insertInlineImage: InsertInlineImageRequest
    insertPerson: InsertPersonRequest
    insertDate: InsertDateRequest
    addDocumentTab: AddDocumentTabRequest
    updateDocumentTabProperties: UpdateDocumentTabPropertiesRequest
    deleteTab: DeleteTabRequest
    createHeader: CreateHeaderRequest
    createFooter: CreateFooterRequest
    createFootnote: CreateFootnoteRequest
    insertPageBreak: InsertPageBreakRequest
    insertSectionBreak: InsertSectionBreakRequest
    insertRichLink: InsertRichLinkRequest
    updateDocumentStyle: UpdateDocumentStyleRequest


# Every kind of request above, by the key that says it (held equal to `DocsRequest`'s keys by
# `tests/test_google_schema.py`).
DocsRequestKind = Literal[
    "insertText", "deleteContentRange", "updateTextStyle", "updateParagraphStyle",
    "createParagraphBullets", "deleteParagraphBullets", "createNamedRange", "deleteNamedRange",
    "insertTable", "insertTableRow", "insertTableColumn", "deleteTableRow", "deleteTableColumn",
    "insertInlineImage", "insertPerson", "insertDate", "addDocumentTab",
    "updateDocumentTabProperties", "deleteTab", "createHeader", "createFooter", "createFootnote",
    "insertPageBreak", "insertSectionBreak", "insertRichLink", "updateDocumentStyle"]
DOCS_REQUEST_KINDS: tuple[DocsRequestKind, ...] = (
    "insertText", "deleteContentRange", "updateTextStyle", "updateParagraphStyle",
    "createParagraphBullets", "deleteParagraphBullets", "createNamedRange", "deleteNamedRange",
    "insertTable", "insertTableRow", "insertTableColumn", "deleteTableRow", "deleteTableColumn",
    "insertInlineImage", "insertPerson", "insertDate", "addDocumentTab",
    "updateDocumentTabProperties", "deleteTab", "createHeader", "createFooter", "createFootnote",
    "insertPageBreak", "insertSectionBreak", "insertRichLink", "updateDocumentStyle")


def docs_request_kind(request: DocsRequest) -> DocsRequestKind:
    """Which request this is: the one key of the oneof that is set."""
    for kind in DOCS_REQUEST_KINDS:
        if kind in request:
            return kind
    raise ValueError(f"a Docs request of no kind we write: {sorted(request)}")


class DocsBatchUpdateResponse(TypedDict, total=False):
    documentId: str
    replies: list[JsonObject]
    writeControl: DocsWriteControl


# The Docs enums a document's styles carry, as docs.v1.json lists them (read back and written
# alike: `doc_ir`'s tables translate them to and from the dialect's words).
DocsAlignment = Literal["ALIGNMENT_UNSPECIFIED", "START", "CENTER", "END", "JUSTIFIED"]
DocsBaselineOffset = Literal["BASELINE_OFFSET_UNSPECIFIED", "NONE", "SUPERSCRIPT", "SUBSCRIPT"]
DocsDashStyle = Literal["DASH_STYLE_UNSPECIFIED", "SOLID", "DOT", "DASH"]
DocsDateFormat = Literal["DATE_FORMAT_UNSPECIFIED", "DATE_FORMAT_CUSTOM", "DATE_FORMAT_MONTH_DAY_ABBREVIATED",
                         "DATE_FORMAT_MONTH_DAY_FULL", "DATE_FORMAT_MONTH_DAY_YEAR_ABBREVIATED", "DATE_FORMAT_ISO8601"]

# Slides' predefined layouts, as slides.v1.json's LayoutReference.predefinedLayout lists them (the
# decks the devtools make from Google's own layouts name one).
PredefinedLayout = Literal["PREDEFINED_LAYOUT_UNSPECIFIED", "BLANK", "CAPTION_ONLY", "TITLE", "TITLE_AND_BODY",
                           "TITLE_AND_TWO_COLUMNS", "TITLE_ONLY", "SECTION_HEADER", "SECTION_TITLE_AND_DESCRIPTION",
                           "ONE_COLUMN_TEXT", "MAIN_POINT", "BIG_NUMBER"]


class DocsBatchUpdateBody(TypedDict, total=False):
    requests: Required[Sequence[DocsRequest]]
    writeControl: DocsWriteControl


class GetDocument(TypedDict, total=False):
    documentId: Required[str]
    includeTabsContent: bool
    suggestionsViewMode: SuggestionsViewMode


class UpdateDocument(TypedDict, total=False):
    documentId: Required[str]
    body: Required[DocsBatchUpdateBody]


class Documents(Protocol):
    def get(self, **kw: Unpack[GetDocument]) -> Request[Document]: ...
    def batchUpdate(self, **kw: Unpack[UpdateDocument]) -> Request[DocsBatchUpdateResponse]: ...


@runtime_checkable
class DocsService(Protocol):
    """The Docs API v1 client (`google_auth.docs_service`)."""

    def documents(self) -> Documents: ...
