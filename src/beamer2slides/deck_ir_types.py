"""`deck_ir`'s reading of a Slides deck - a pull loop's or adopt's target (target.json) - as typed
values: `TargetDeck`, parsed from and written back to exactly the JSON `deck_ir` writes
(`parse_target`, `target_json`).

A target is deck.json-shaped but is not deck.json: `deck_ir` says what the Slides API and the
slide's thumbnail say (a Slides box's `anchor`, `box` and `wrap_width`, a paragraph's `slides`
measures, tables as rows of strings, keys like `objectId` and `shape_type`), so `ir_types` does not
model it. Read here are the target.json files every version of `deck_ir` has written, which a corpus
keeps cached, not only today's: a key a version added (a slide's `background_gradient`, a text
box's `outline_color`) is absent in what the ones before it wrote.

**The JSON form is kept key for key** (`tests/test_deck_ir_types.py` measures it on the targets
under out/):
  * a key always written is a plain field, `X | None` when it may be null ("nullable");
  * a key sometimes left out, never null, is `X | None`, None for absent ("optional");
  * a key sometimes left out and sometimes null is `X | None | Absent`, `ABSENT` for a key the
    JSON did not have: a slide's `background_file` (null when the picture could not be had),
    `background_gradient` and `layout`, an element's `key`, a line's `category` and `line_type`,
    a text box's `outline_color`, a shape's `weight`;
  * numbers are kept as they came (an int where a float goes stays an int), flags too (a false
    one written stays written);
  * fixed-length arrays are tuples in memory, lists in JSON.
Key order is `deck_ir`'s own for what it builds in one piece and canonical after that: JSON
objects are unordered, and nothing reads an order out of one. A key no version wrote is refused
(`IRError`), so a key `deck_ir` starts writing is a key this module learns.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar, Union

from .ir import Align, Family, Script
from .ir_types import (ALIGNS, ELEMENT_KINDS, FAMILIES, SCRIPTS, At, Box, Fields, IRError, Parse, Point, boolean, box, integer,
                       number, one_of, pair, point, rtl, string, tuple_of)
from .json_types import Json, JsonObject
from .typing_compat import assert_never

T = TypeVar("T")

VAlign = Literal["top", "middle", "bottom"]
VALIGNS: tuple[VAlign, ...] = ("top", "middle", "bottom")
Matrix = tuple[float, float, float, float]
"""A frame's linear part with the size taken out: (a, b, d, e)."""


@dataclass(frozen=True, kw_only=True)
class Absent:
    """A key the JSON did not have, where null is a value of its own (`ABSENT`)."""


ABSENT = Absent()

# ------------------------------------------------------------------------------ text


@dataclass(frozen=True, kw_only=True)
class TargetRun:
    text: str
    font: str
    family: Family
    size: float
    bold: bool
    italic: bool
    smallcaps: bool
    color: str
    link: str | None
    script: Script | None
    underline: bool
    strike: bool
    highlight: str | None
    hole: float | None
    """Optional: a formula hole's width (the run's text is then a space)."""
    weight: int | None
    """Optional: a weight between regular and bold, or beyond (a foreign deck's)."""
    weight_unsure: bool | None
    """Optional: `thumbnail_weights` decides, and takes it away."""


@dataclass(frozen=True, kw_only=True)
class TargetLine:
    baseline: float | None
    """Only a text box's first line has one (null: no baseline read)."""
    x0: float
    x1: float


@dataclass(frozen=True, kw_only=True)
class TargetBullet:
    kind: str
    text: str
    bbox: Box | None | Absent
    """Null in a text box (Slides draws its bullets itself), absent in a table cell."""
    color: str | None
    size: float | None
    font_family: Family | None
    font: str | None
    bold: bool | None


@dataclass(frozen=True, kw_only=True)
class SlidesMeasures:
    """A text paragraph's `slides`: what Slides says of it, in Slides pt."""
    font: str
    size: float
    indent_start: float
    indent_first: float
    line_spacing: float
    space_above: float
    space_below: float | None
    indent_end: float | None
    spacing_mode: str | None | Absent
    justified: bool | None


@dataclass(frozen=True, kw_only=True)
class TargetParagraph:
    align: Align
    level: int
    bullet: TargetBullet | None
    direction: Literal["rtl"] | None
    size: float
    text_x0: float
    tab_x0: float | None
    lines: tuple[TargetLine, ...]
    runs: tuple[TargetRun, ...]
    slides: SlidesMeasures | None
    """Optional: a WordArt's paragraphs have none."""


@dataclass(frozen=True, kw_only=True)
class TextBox:
    """A text element's `box`: how Slides lays its lines out (`adopt.text_box_latex`)."""
    valign: VAlign
    scale: float
    font_scale: float
    grows: bool | None
    insets: int | None
    span: float | None
    snap: bool | None
    inset_x: float | None
    inset_y: float | None


@dataclass(frozen=True, kw_only=True)
class Frame:
    """An element's own frame when it is turned, mirrored or sheared (`deck_ir.frame`)."""
    size: tuple[float, float]
    origin: Point
    matrix: Matrix
    rotation: float
    flip: bool
    box: Box
    shear: bool | None


@dataclass(frozen=True, kw_only=True)
class FillGradient:
    """A fill the thumbnail shows as a ramp (`deck_fills`)."""
    axis: str
    colors: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class PageGradient:
    """A slide background the thumbnail shows as a gradient (`deck_fills.page_gradient`)."""
    type: str
    colors: tuple[str, ...]
    angle: float | None
    center: Point | None
    radius: float | None


@dataclass(frozen=True, kw_only=True)
class _Element:
    id: str
    object: str | None
    """Optional: a picture `deck_fills` splits off a shape's fill has no object of its own."""
    group: str | None
    key: str | None | Absent
    role: str
    bbox: Box
    inherited: str | None
    """Optional: the layout or master a foreign slide draws it from."""


@dataclass(frozen=True, kw_only=True)
class TargetText(_Element):
    anchor: Point
    wrap_width: float
    placeholder: str | None
    shape_type: str | None
    paragraphs: tuple[TargetParagraph, ...]
    box: TextBox | Box | None
    """Optional: a Slides box's layout, or a WordArt's unturned frame."""
    wordart: bool | None
    rotation: float | None
    outline_color: str | None | Absent
    outline_alpha: float | None
    """A text box's outline seen through (`deck_ir.outline_props`)."""
    weight: float | None
    dash: str | None
    fill: str | None
    fill_alpha: float | None
    fill_gradient: FillGradient | None
    fill_source: str | None
    fill_unread: bool | None
    not_rendered: bool | None
    """`_not_rendered`."""
    frame: Frame | None
    ink_width: float | None


# ------------------------------------------------------------------------------ pictures


@dataclass(frozen=True, kw_only=True)
class Crop:
    """Fractions cut off the picture file, per side."""
    l: float  # noqa: E741 - the JSON's own name
    t: float
    r: float
    b: float


@dataclass(frozen=True, kw_only=True)
class RecolorStop:
    color: str
    alpha: float
    position: float


@dataclass(frozen=True, kw_only=True)
class Recolor:
    name: str | None
    stops: tuple[RecolorStop, ...]


@dataclass(frozen=True, kw_only=True)
class PictureOutline:
    color: str
    weight: float
    dash: str


@dataclass(frozen=True, kw_only=True)
class Chart:
    spreadsheet_id: str | None
    chart_id: int | None


@dataclass(frozen=True, kw_only=True)
class Video:
    source: str | None
    id: str | None
    url: str | None
    start: float | None
    end: float | None


@dataclass(frozen=True, kw_only=True)
class _PictureProps:
    """What a picture's file and properties say; `thumbnail_of` keeps them for a picture the slide's
    thumbnail replaced."""
    file: str | None
    sha1: str | None
    format: str | None
    error: str | None
    crop: Crop | None
    crop_angle: float | None
    opacity: float | None
    brightness: float | None
    contrast: float | None
    recolor: Recolor | None
    rotation: float | None
    flip: bool | None
    box: Box | None
    outline: PictureOutline | None


@dataclass(frozen=True, kw_only=True)
class ThumbnailOf(_PictureProps):
    bbox: Box | None


@dataclass(frozen=True, kw_only=True)
class TargetImage(_Element, _PictureProps):
    alt: str | None
    source_url: str | None
    chart: Chart | None
    video: Video | None
    number: str | None
    """Optional (`{"text": ...}`): the number on a ball `fold_groups` read into it."""
    fill_source: str | None
    thumbnail_crop: bool | None
    thumbnail_of: ThumbnailOf | None
    picture_place: str | None
    picture_source: str | None
    poster: str | None
    mask: str | None
    clip: str | None


# ------------------------------------------------------------------------------ shapes


@dataclass(frozen=True, kw_only=True)
class Trace:
    """A freeform's outline as the thumbnail shows it (`deck_freeforms`)."""
    rings: tuple[tuple[Point, ...], ...]
    fill: str
    alpha: float | None
    stroke: str | None
    weight: float | None
    source: str


@dataclass(frozen=True, kw_only=True)
class TargetShape(_Element):
    shape: str
    shape_type: str | None
    fill: str | None
    outline: str | None
    weight: float | None | Absent
    fill_alpha: float | None
    outline_alpha: float | None
    dash: str | None
    fill_gradient: FillGradient | None
    fill_source: str | None
    fill_unread: bool | None
    frame: Frame | None
    corner_radius: float | None
    pie: tuple[float, float] | None
    trace: Trace | None
    start: Point | None
    """`from`: a line's first end."""
    end: Point | None
    """`to`."""
    arrow: bool | None
    arrow_start: bool | None
    start_arrow: str | None
    end_arrow: str | None
    line_type: str | None | Absent
    category: str | None | Absent
    no_ramp: bool | None
    """`_no_ramp`."""
    not_rendered: bool | None
    """`_not_rendered`."""
    unsaid: bool | None
    """`_unsaid`."""


# ------------------------------------------------------------------------------ tables, diagrams


@dataclass(frozen=True, kw_only=True)
class CellParagraph:
    align: Align
    level: int
    bullet: TargetBullet | None
    size: float
    line_spacing: float
    direction: Literal["rtl"] | None
    runs: tuple[TargetRun, ...]


@dataclass(frozen=True, kw_only=True)
class TableCell:
    row: int
    col: int
    rowspan: int
    colspan: int
    fill: str | None
    fill_alpha: float | None
    valign: VAlign
    paragraphs: tuple[CellParagraph, ...]
    fill_source: str | None
    fill_unread: bool | None
    empty_size: float | None


@dataclass(frozen=True, kw_only=True)
class TableBorder:
    dir: Literal["h", "v"]
    row: int
    col: int
    color: str | None
    alpha: float
    weight: float
    dash: str


@dataclass(frozen=True, kw_only=True)
class TargetTable(_Element):
    rows: tuple[tuple[str, ...], ...]
    """Each cell's words."""
    cell_pad: tuple[float, float] | None
    col_widths: tuple[float, ...] | None
    row_heights: tuple[float, ...] | None
    rows_fixed: tuple[int, ...] | None
    cell_text_y: float | None
    table_cells: tuple[TableCell, ...] | None
    table_borders: tuple[TableBorder, ...] | None


@dataclass(frozen=True, kw_only=True)
class DiagramNode:
    bbox: Box
    fill: str | None
    shape: str | None
    paragraphs: tuple[tuple[TargetRun, ...], ...]


@dataclass(frozen=True, kw_only=True)
class TargetDiagram(_Element):
    nodes: tuple[DiagramNode, ...]


TargetElement = Union[TargetText, TargetImage, TargetShape, TargetTable, TargetDiagram]

# ------------------------------------------------------------------------------ slides, deck


@dataclass(frozen=True, kw_only=True)
class TargetSlide:
    page: int
    frame: str
    size: tuple[float, float]
    object_id: str
    key: str | None
    notes: str | None
    background_color: str | None
    background_picture: str | None
    background_gradient: PageGradient | None | Absent
    background_file: str | None | Absent
    background_source: str | None
    layout: str | None | Absent
    thumbnail: str | None
    elements: tuple[TargetElement, ...]


@dataclass(frozen=True, kw_only=True)
class DeckSource:
    presentation_id: str | None | Absent
    """Absent (with `revisionId`) in a fixture that names no deck of ours (`showcase.write_fixture`)."""
    title: str | None
    revision_id: str | None | Absent


@dataclass(frozen=True, kw_only=True)
class Layout:
    name: str
    master: str | None
    background_color: str | None
    background_picture: str | None


@dataclass(frozen=True, kw_only=True)
class TargetDeck:
    version: int
    source: DeckSource
    page_size: tuple[float, float]
    scale: float
    slides: tuple[TargetSlide, ...]
    layouts: tuple[tuple[str, Layout], ...] | None
    """Optional (a foreign read's): each layout and master by object id, in the deck's order."""
    first_slide: int | None
    """Optional: where a slide range's first slide stands in the whole deck (`adopt_bench.load_target`
    cuts one out)."""


# ------------------------------------------------------------------------------ reading


def _tri(f: Fields, key: str, parse: Parse[T]) -> T | None | Absent:
    """A key sometimes left out and sometimes null."""
    return f.nullable(key, parse) if f.has(key) else ABSENT


def _matrix(v: object, at: At) -> Matrix:
    return box(v, at)


def _dir(v: object, at: At) -> Literal["h", "v"]:
    if v == "h":
        return "h"
    if v == "v":
        return "v"
    at.fail(f"is {v!r}, expected 'h' or 'v'")


def _run(v: object, at: At) -> TargetRun:
    f = Fields(v, at, "TargetRun")
    out = TargetRun(text=f.req("text", string), font=f.req("font", string), family=f.req("family", one_of(FAMILIES)),
                    size=f.req("size", number), bold=f.req("bold", boolean), italic=f.req("italic", boolean),
                    smallcaps=f.req("smallcaps", boolean), color=f.req("color", string),
                    link=f.nullable("link", string), script=f.nullable("script", one_of(SCRIPTS)),
                    underline=f.req("underline", boolean), strike=f.req("strike", boolean),
                    highlight=f.nullable("highlight", string), hole=f.optional("hole", number),
                    weight=f.optional("weight", integer), weight_unsure=f.optional("weight_unsure", boolean))
    f.close()
    return out


def _line(v: object, at: At) -> TargetLine:
    f = Fields(v, at, "TargetLine")
    out = TargetLine(baseline=f.nullable("baseline", number), x0=f.req("x0", number), x1=f.req("x1", number))
    f.close()
    return out


def _bullet(v: object, at: At) -> TargetBullet:
    f = Fields(v, at, "TargetBullet")
    out = TargetBullet(kind=f.req("kind", string), text=f.req("text", string), bbox=_tri(f, "bbox", box),
                       color=f.optional("color", string), size=f.optional("size", number),
                       font_family=f.optional("font_family", one_of(FAMILIES)), font=f.optional("font", string),
                       bold=f.optional("bold", boolean))
    f.close()
    return out


def _measures(v: object, at: At) -> SlidesMeasures:
    f = Fields(v, at, "SlidesMeasures")
    out = SlidesMeasures(font=f.req("font", string), size=f.req("size", number),
                         indent_start=f.req("indent_start", number), indent_first=f.req("indent_first", number),
                         line_spacing=f.req("line_spacing", number), space_above=f.req("space_above", number),
                         space_below=f.optional("space_below", number), indent_end=f.optional("indent_end", number),
                         spacing_mode=_tri(f, "spacing_mode", string), justified=f.optional("justified", boolean))
    f.close()
    return out


def _paragraph(v: object, at: At) -> TargetParagraph:
    f = Fields(v, at, "TargetParagraph")
    out = TargetParagraph(align=f.req("align", one_of(ALIGNS)), level=f.req("level", integer),
                          bullet=f.nullable("bullet", _bullet), direction=f.optional("direction", rtl),
                          size=f.req("size", number), text_x0=f.req("text_x0", number),
                          tab_x0=f.nullable("tab_x0", number), lines=f.req("lines", tuple_of(_line)),
                          runs=f.req("runs", tuple_of(_run)), slides=f.optional("slides", _measures))
    f.close()
    return out


def _text_box(v: object, at: At) -> TextBox | Box:
    if isinstance(v, list):
        return box(v, at)
    f = Fields(v, at, "TextBox")
    out = TextBox(valign=f.req("valign", one_of(VALIGNS)), scale=f.req("scale", number),
                  font_scale=f.req("font_scale", number), grows=f.optional("grows", boolean),
                  insets=f.optional("insets", integer), span=f.optional("span", number),
                  snap=f.optional("snap", boolean), inset_x=f.optional("inset_x", number),
                  inset_y=f.optional("inset_y", number))
    f.close()
    return out


def _frame(v: object, at: At) -> Frame:
    f = Fields(v, at, "Frame")
    out = Frame(size=f.req("size", pair), origin=f.req("origin", point), matrix=f.req("matrix", _matrix),
                rotation=f.req("rotation", number), flip=f.req("flip", boolean), box=f.req("box", box),
                shear=f.optional("shear", boolean))
    f.close()
    return out


def _fill_gradient(v: object, at: At) -> FillGradient:
    f = Fields(v, at, "FillGradient")
    out = FillGradient(axis=f.req("axis", string), colors=f.req("colors", tuple_of(string)))
    f.close()
    return out


def _page_gradient(v: object, at: At) -> PageGradient:
    f = Fields(v, at, "PageGradient")
    out = PageGradient(type=f.req("type", string), colors=f.req("colors", tuple_of(string)),
                       angle=f.optional("angle", number), center=f.optional("center", point),
                       radius=f.optional("radius", number))
    f.close()
    return out


def _crop(v: object, at: At) -> Crop:
    f = Fields(v, at, "Crop")
    out = Crop(l=f.req("l", number), t=f.req("t", number), r=f.req("r", number), b=f.req("b", number))
    f.close()
    return out


def _stop(v: object, at: At) -> RecolorStop:
    f = Fields(v, at, "RecolorStop")
    out = RecolorStop(color=f.req("color", string), alpha=f.req("alpha", number), position=f.req("position", number))
    f.close()
    return out


def _recolor(v: object, at: At) -> Recolor:
    f = Fields(v, at, "Recolor")
    out = Recolor(name=f.nullable("name", string), stops=f.req("stops", tuple_of(_stop)))
    f.close()
    return out


def _picture_outline(v: object, at: At) -> PictureOutline:
    f = Fields(v, at, "PictureOutline")
    out = PictureOutline(color=f.req("color", string), weight=f.req("weight", number), dash=f.req("dash", string))
    f.close()
    return out


def _chart(v: object, at: At) -> Chart:
    f = Fields(v, at, "Chart")
    out = Chart(spreadsheet_id=f.nullable("spreadsheetId", string), chart_id=f.nullable("chartId", integer))
    f.close()
    return out


def _video(v: object, at: At) -> Video:
    f = Fields(v, at, "Video")
    out = Video(source=f.nullable("source", string), id=f.nullable("id", string), url=f.nullable("url", string),
                start=f.nullable("start", number), end=f.nullable("end", number))
    f.close()
    return out


def _number_text(v: object, at: At) -> str:
    f = Fields(v, at, "Number")
    out = f.req("text", string)
    f.close()
    return out


def _thumbnail_of(v: object, at: At) -> ThumbnailOf:
    f = Fields(v, at, "ThumbnailOf")
    out = ThumbnailOf(file=f.optional("file", string), sha1=f.optional("sha1", string),
                      format=f.optional("format", string), error=f.optional("error", string),
                      crop=f.optional("crop", _crop), crop_angle=f.optional("crop_angle", number),
                      opacity=f.optional("opacity", number), brightness=f.optional("brightness", number),
                      contrast=f.optional("contrast", number), recolor=f.optional("recolor", _recolor),
                      rotation=f.optional("rotation", number), flip=f.optional("flip", boolean),
                      box=f.optional("box", box), outline=f.optional("outline", _picture_outline),
                      bbox=f.optional("bbox", box))
    f.close()
    return out


def _trace(v: object, at: At) -> Trace:
    f = Fields(v, at, "Trace")
    out = Trace(rings=f.req("rings", tuple_of(tuple_of(point))), fill=f.req("fill", string),
                alpha=f.nullable("alpha", number), stroke=f.nullable("stroke", string),
                weight=f.nullable("weight", number), source=f.req("source", string))
    f.close()
    return out


def _cell_paragraph(v: object, at: At) -> CellParagraph:
    f = Fields(v, at, "CellParagraph")
    out = CellParagraph(align=f.req("align", one_of(ALIGNS)), level=f.req("level", integer),
                        bullet=f.nullable("bullet", _bullet), size=f.req("size", number),
                        line_spacing=f.req("line_spacing", number), direction=f.optional("direction", rtl),
                        runs=f.req("runs", tuple_of(_run)))
    f.close()
    return out


def _cell(v: object, at: At) -> TableCell:
    f = Fields(v, at, "TableCell")
    out = TableCell(row=f.req("row", integer), col=f.req("col", integer), rowspan=f.req("rowspan", integer),
                    colspan=f.req("colspan", integer), fill=f.nullable("fill", string),
                    fill_alpha=f.nullable("fill_alpha", number), valign=f.req("valign", one_of(VALIGNS)),
                    paragraphs=f.req("paragraphs", tuple_of(_cell_paragraph)),
                    fill_source=f.optional("fill_source", string), fill_unread=f.optional("fill_unread", boolean),
                    empty_size=f.optional("empty_size", number))
    f.close()
    return out


def _border(v: object, at: At) -> TableBorder:
    f = Fields(v, at, "TableBorder")
    out = TableBorder(dir=f.req("dir", _dir), row=f.req("row", integer), col=f.req("col", integer),
                      color=f.nullable("color", string), alpha=f.req("alpha", number),
                      weight=f.req("weight", number), dash=f.req("dash", string))
    f.close()
    return out


def _node(v: object, at: At) -> DiagramNode:
    f = Fields(v, at, "DiagramNode")
    out = DiagramNode(bbox=f.req("bbox", box), fill=f.nullable("fill", string), shape=f.nullable("shape", string),
                      paragraphs=f.req("paragraphs", tuple_of(tuple_of(_run))))
    f.close()
    return out


def _element(v: object, at: At) -> TargetElement:
    f = Fields(v, at, "TargetElement")
    kind = f.req("kind", one_of(ELEMENT_KINDS))
    id_, obj, group = f.req("id", string), f.optional("object", string), f.nullable("group", string)
    key, role, bbox = _tri(f, "key", string), f.req("role", string), f.req("bbox", box)
    inherited = f.optional("inherited", string)
    out: TargetElement
    if kind == "text":
        out = TargetText(
            id=id_, object=obj, group=group, key=key, role=role, bbox=bbox, inherited=inherited,
            anchor=f.req("anchor", point), wrap_width=f.req("wrap_width", number),
            placeholder=f.nullable("placeholder", string), shape_type=f.nullable("shape_type", string),
            paragraphs=f.req("paragraphs", tuple_of(_paragraph)), box=f.optional("box", _text_box),
            wordart=f.optional("wordart", boolean), rotation=f.optional("rotation", number),
            outline_color=_tri(f, "outline_color", string), outline_alpha=f.optional("outline_alpha", number),
            weight=f.optional("weight", number),
            dash=f.optional("dash", string), fill=f.optional("fill", string),
            fill_alpha=f.optional("fill_alpha", number), fill_gradient=f.optional("fill_gradient", _fill_gradient),
            fill_source=f.optional("fill_source", string), fill_unread=f.optional("fill_unread", boolean),
            not_rendered=f.optional("_not_rendered", boolean), frame=f.optional("frame", _frame),
            ink_width=f.optional("ink_width", number))
    elif kind == "image":
        out = TargetImage(
            id=id_, object=obj, group=group, key=key, role=role, bbox=bbox, inherited=inherited,
            alt=f.nullable("alt", string), file=f.optional("file", string), sha1=f.optional("sha1", string),
            format=f.optional("format", string), error=f.optional("error", string), crop=f.optional("crop", _crop),
            crop_angle=f.optional("crop_angle", number), opacity=f.optional("opacity", number),
            brightness=f.optional("brightness", number), contrast=f.optional("contrast", number),
            recolor=f.optional("recolor", _recolor), rotation=f.optional("rotation", number),
            flip=f.optional("flip", boolean), box=f.optional("box", box),
            outline=f.optional("outline", _picture_outline), source_url=f.optional("source_url", string),
            chart=f.optional("chart", _chart), video=f.optional("video", _video),
            number=f.optional("number", _number_text), fill_source=f.optional("fill_source", string),
            thumbnail_crop=f.optional("thumbnail_crop", boolean),
            thumbnail_of=f.optional("thumbnail_of", _thumbnail_of),
            picture_place=f.optional("picture_place", string), picture_source=f.optional("picture_source", string),
            poster=f.optional("poster", string), mask=f.optional("mask", string), clip=f.optional("clip", string))
    elif kind == "shape":
        out = TargetShape(
            id=id_, object=obj, group=group, key=key, role=role, bbox=bbox, inherited=inherited,
            shape=f.req("shape", string), shape_type=f.optional("shape_type", string),
            fill=f.nullable("fill", string), outline=f.nullable("outline", string), weight=_tri(f, "weight", number),
            fill_alpha=f.optional("fill_alpha", number), outline_alpha=f.optional("outline_alpha", number),
            dash=f.optional("dash", string), fill_gradient=f.optional("fill_gradient", _fill_gradient),
            fill_source=f.optional("fill_source", string), fill_unread=f.optional("fill_unread", boolean),
            frame=f.optional("frame", _frame), corner_radius=f.optional("corner_radius", number),
            pie=f.optional("pie", pair), trace=f.optional("trace", _trace), start=f.optional("from", point),
            end=f.optional("to", point), arrow=f.optional("arrow", boolean),
            arrow_start=f.optional("arrow_start", boolean), start_arrow=f.optional("start_arrow", string),
            end_arrow=f.optional("end_arrow", string), line_type=_tri(f, "line_type", string),
            category=_tri(f, "category", string), no_ramp=f.optional("_no_ramp", boolean),
            not_rendered=f.optional("_not_rendered", boolean), unsaid=f.optional("_unsaid", boolean))
    elif kind == "table":
        out = TargetTable(
            id=id_, object=obj, group=group, key=key, role=role, bbox=bbox, inherited=inherited,
            rows=f.req("rows", tuple_of(tuple_of(string))), cell_pad=f.optional("cell_pad", pair),
            col_widths=f.optional("col_widths", tuple_of(number)),
            row_heights=f.optional("row_heights", tuple_of(number)),
            rows_fixed=f.optional("rows_fixed", tuple_of(integer)), cell_text_y=f.optional("cell_text_y", number),
            table_cells=f.optional("table_cells", tuple_of(_cell)),
            table_borders=f.optional("table_borders", tuple_of(_border)))
    elif kind == "diagram":
        out = TargetDiagram(id=id_, object=obj, group=group, key=key, role=role, bbox=bbox, inherited=inherited,
                            nodes=f.req("nodes", tuple_of(_node)))
    else:
        assert_never(kind)
    f.close()
    return out


def parse_element(v: object, where: str) -> TargetElement:
    """One element as `element_json` writes it, typed (`deck_ir`'s thumbnail passes still work on
    that JSON); raises `IRError` naming `where`."""
    return _element(v, At(where=where, path=""))


def parse_page_gradient(v: object, where: str) -> PageGradient:
    """A slide's `background_gradient` as `deck_fills.page_gradient` reads it off the thumbnail."""
    return _page_gradient(v, At(where=where, path=""))


def _slide(v: object, at: At) -> TargetSlide:
    f = Fields(v, at, "TargetSlide")
    out = TargetSlide(page=f.req("page", integer), frame=f.req("frame", string), size=f.req("size", pair),
                      object_id=f.req("objectId", string), key=f.nullable("key", string),
                      notes=f.nullable("notes", string), background_color=f.nullable("background_color", string),
                      background_picture=f.nullable("background_picture", string),
                      background_gradient=_tri(f, "background_gradient", _page_gradient),
                      background_file=_tri(f, "background_file", string),
                      background_source=f.optional("background_source", string), layout=_tri(f, "layout", string),
                      thumbnail=f.optional("thumbnail", string),
                      elements=f.req("elements", tuple_of(_element)))
    f.close()
    return out


def _source(v: object, at: At) -> DeckSource:
    f = Fields(v, at, "DeckSource")
    out = DeckSource(presentation_id=_tri(f, "presentationId", string), title=f.nullable("title", string),
                     revision_id=_tri(f, "revisionId", string))
    f.close()
    return out


def _layout(v: object, at: At) -> Layout:
    f = Fields(v, at, "Layout")
    out = Layout(name=f.req("name", string), master=f.nullable("master", string),
                 background_color=f.nullable("background_color", string),
                 background_picture=f.nullable("background_picture", string))
    f.close()
    return out


def _layouts(v: object, at: At) -> tuple[tuple[str, Layout], ...]:
    if not isinstance(v, dict):
        at.fail(f"is {type(v).__name__}, expected an object of layouts")
    out: list[tuple[str, Layout]] = []
    for k, x in v.items():
        name = string(k, at)
        out.append((name, _layout(x, at.key(name))))
    return tuple(out)


def parse_target(v: object) -> TargetDeck:
    """A target (`deck_ir`'s output, as target.json holds it), typed; raises `IRError` naming the
    slide, the element and the path inside it."""
    at = At(where="target", path="")
    f = Fields(v, at, "TargetDeck")
    out = TargetDeck(version=f.req("version", integer), source=f.req("source", _source),
                     page_size=f.req("page_size", pair), scale=f.req("scale", number),
                     slides=f.req("slides", tuple_of(_slide)), layouts=f.optional("layouts", _layouts),
                     first_slide=f.optional("first_slide", integer))
    f.close()
    return out


# ------------------------------------------------------------------------------ writing


def _opt(out: JsonObject, key: str, value: Json) -> None:
    """An optional key: written when there is something to say."""
    if value is not None:
        out[key] = value


def _tri_put(out: JsonObject, key: str, value: Json | Absent) -> None:
    if not isinstance(value, Absent):
        out[key] = value


def _box(b: Box) -> Json:
    return [b[0], b[1], b[2], b[3]]


def _point(p: Point) -> Json:
    return [p[0], p[1]]


def _floats(xs: Sequence[float]) -> Json:
    out: list[Json] = [x for x in xs]
    return out


def _ints(xs: Sequence[int]) -> Json:
    out: list[Json] = [x for x in xs]
    return out


def _strings(xs: Sequence[str]) -> Json:
    out: list[Json] = [x for x in xs]
    return out


def run_json(r: TargetRun) -> JsonObject:
    out: JsonObject = {"text": r.text, "font": r.font, "family": r.family, "size": r.size, "bold": r.bold,
                       "italic": r.italic, "smallcaps": r.smallcaps, "color": r.color, "link": r.link,
                       "script": r.script, "underline": r.underline, "strike": r.strike, "highlight": r.highlight}
    _opt(out, "weight", r.weight)
    _opt(out, "hole", r.hole)
    _opt(out, "weight_unsure", r.weight_unsure)
    return out


def _runs(runs: tuple[TargetRun, ...]) -> Json:
    return [run_json(r) for r in runs]


def _bullet_json(b: TargetBullet) -> JsonObject:
    out: JsonObject = {"kind": b.kind, "text": b.text}
    _opt(out, "color", b.color)
    _opt(out, "size", b.size)
    _opt(out, "font_family", b.font_family)
    _opt(out, "font", b.font)
    _opt(out, "bold", b.bold)
    bbox = b.bbox
    if not isinstance(bbox, Absent):
        out["bbox"] = None if bbox is None else _box(bbox)
    return out


def _measures_json(m: SlidesMeasures) -> JsonObject:
    out: JsonObject = {"indent_start": m.indent_start, "indent_first": m.indent_first,
                       "line_spacing": m.line_spacing, "space_above": m.space_above}
    _opt(out, "space_below", m.space_below)
    _opt(out, "indent_end", m.indent_end)
    _tri_put(out, "spacing_mode", m.spacing_mode)
    _opt(out, "justified", m.justified)
    out.update(font=m.font, size=m.size)
    return out


def paragraph_json(p: TargetParagraph) -> JsonObject:
    out: JsonObject = {"align": p.align, "level": p.level,
                       "bullet": None if p.bullet is None else _bullet_json(p.bullet)}
    _opt(out, "direction", p.direction)
    out.update(size=p.size, text_x0=p.text_x0, tab_x0=p.tab_x0)
    out["lines"] = [{"baseline": ln.baseline, "x0": ln.x0, "x1": ln.x1} for ln in p.lines]
    out["runs"] = _runs(p.runs)
    if p.slides is not None:
        out["slides"] = _measures_json(p.slides)
    return out


def _text_box_json(b: TextBox | Box) -> Json:
    if not isinstance(b, TextBox):
        return _box(b)
    out: JsonObject = {"valign": b.valign, "scale": b.scale, "font_scale": b.font_scale}
    _opt(out, "grows", b.grows)
    _opt(out, "insets", b.insets)
    _opt(out, "span", b.span)
    _opt(out, "snap", b.snap)
    _opt(out, "inset_x", b.inset_x)
    _opt(out, "inset_y", b.inset_y)
    return out


def _frame_json(fr: Frame) -> JsonObject:
    out: JsonObject = {"size": _point(fr.size), "origin": _point(fr.origin),
                       "matrix": [fr.matrix[0], fr.matrix[1], fr.matrix[2], fr.matrix[3]],
                       "rotation": fr.rotation, "flip": fr.flip, "box": _box(fr.box)}
    _opt(out, "shear", fr.shear)
    return out


def _fill_gradient_json(g: FillGradient) -> JsonObject:
    return {"axis": g.axis, "colors": _strings(g.colors)}


def _page_gradient_json(g: PageGradient) -> JsonObject:
    out: JsonObject = {"type": g.type, "colors": _strings(g.colors)}
    _opt(out, "angle", g.angle)
    _opt(out, "center", None if g.center is None else _point(g.center))
    _opt(out, "radius", g.radius)
    return out


def _crop_json(c: Crop) -> JsonObject:
    return {"l": c.l, "t": c.t, "r": c.r, "b": c.b}


def _recolor_json(r: Recolor) -> JsonObject:
    return {"name": r.name,
            "stops": [{"color": s.color, "alpha": s.alpha, "position": s.position} for s in r.stops]}


def _picture_props(out: JsonObject, p: _PictureProps) -> None:
    _opt(out, "box", None if p.box is None else _box(p.box))
    _opt(out, "flip", p.flip)
    _opt(out, "rotation", p.rotation)
    _opt(out, "crop", None if p.crop is None else _crop_json(p.crop))
    _opt(out, "crop_angle", p.crop_angle)
    _opt(out, "opacity", p.opacity)
    _opt(out, "brightness", p.brightness)
    _opt(out, "contrast", p.contrast)
    _opt(out, "recolor", None if p.recolor is None else _recolor_json(p.recolor))
    if p.outline is not None:
        out["outline"] = {"color": p.outline.color, "weight": p.outline.weight, "dash": p.outline.dash}
    _opt(out, "file", p.file)
    _opt(out, "sha1", p.sha1)
    _opt(out, "format", p.format)
    _opt(out, "error", p.error)


def _thumbnail_of_json(t: ThumbnailOf) -> JsonObject:
    out: JsonObject = {}
    _opt(out, "bbox", None if t.bbox is None else _box(t.bbox))
    _picture_props(out, t)
    return out


def _trace_json(t: Trace) -> JsonObject:
    return {"rings": [[_point(p) for p in ring] for ring in t.rings], "fill": t.fill, "alpha": t.alpha,
            "stroke": t.stroke, "weight": t.weight, "source": t.source}


def _cell_json(c: TableCell) -> JsonObject:
    out: JsonObject = {"row": c.row, "col": c.col, "rowspan": c.rowspan, "colspan": c.colspan, "fill": c.fill,
                       "fill_alpha": c.fill_alpha, "valign": c.valign}
    paragraphs: list[Json] = []
    for p in c.paragraphs:
        pj: JsonObject = {"align": p.align, "level": p.level,
                          "bullet": None if p.bullet is None else _bullet_json(p.bullet), "size": p.size,
                          "line_spacing": p.line_spacing}
        _opt(pj, "direction", p.direction)
        pj["runs"] = _runs(p.runs)
        paragraphs.append(pj)
    out["paragraphs"] = paragraphs
    _opt(out, "fill_source", c.fill_source)
    _opt(out, "fill_unread", c.fill_unread)
    _opt(out, "empty_size", c.empty_size)
    return out


def _border_json(b: TableBorder) -> JsonObject:
    return {"dir": b.dir, "row": b.row, "col": b.col, "color": b.color, "alpha": b.alpha, "weight": b.weight,
            "dash": b.dash}


def _node_json(n: DiagramNode) -> JsonObject:
    return {"bbox": _box(n.bbox), "fill": n.fill, "shape": n.shape, "paragraphs": [_runs(p) for p in n.paragraphs]}


def element_json(el: TargetElement) -> JsonObject:
    """An element as `deck_ir` writes it."""
    out: JsonObject = {"kind": "", "role": el.role, "bbox": _box(el.bbox)}
    match el:
        case TargetText():
            out["kind"] = "text"
            out.update(anchor=_point(el.anchor), wrap_width=el.wrap_width, placeholder=el.placeholder)
            out["paragraphs"] = [paragraph_json(p) for p in el.paragraphs]
            _opt(out, "box", None if el.box is None else _text_box_json(el.box))
            _opt(out, "wordart", el.wordart)
            _opt(out, "rotation", el.rotation)
            _opt(out, "fill", el.fill)
            _opt(out, "fill_unread", el.fill_unread)
            _opt(out, "_not_rendered", el.not_rendered)
            _opt(out, "fill_alpha", el.fill_alpha)
            _opt(out, "weight", el.weight)
            _opt(out, "dash", el.dash)
            _opt(out, "frame", None if el.frame is None else _frame_json(el.frame))
            out["shape_type"] = el.shape_type
            _tri_put(out, "outline_color", el.outline_color)
            _opt(out, "outline_alpha", el.outline_alpha)
            _opt(out, "fill_gradient", None if el.fill_gradient is None else _fill_gradient_json(el.fill_gradient))
            _opt(out, "fill_source", el.fill_source)
            _opt(out, "ink_width", el.ink_width)
        case TargetImage():
            out["kind"] = "image"
            out["alt"] = el.alt
            _picture_props(out, el)
            _opt(out, "source_url", el.source_url)
            if el.chart is not None:
                out["chart"] = {"spreadsheetId": el.chart.spreadsheet_id, "chartId": el.chart.chart_id}
            if el.video is not None:
                v = el.video
                out["video"] = {"source": v.source, "id": v.id, "url": v.url, "start": v.start, "end": v.end}
            _opt(out, "number", None if el.number is None else {"text": el.number})
            _opt(out, "fill_source", el.fill_source)
            _opt(out, "thumbnail_crop", el.thumbnail_crop)
            _opt(out, "thumbnail_of", None if el.thumbnail_of is None else _thumbnail_of_json(el.thumbnail_of))
            _opt(out, "picture_place", el.picture_place)
            _opt(out, "picture_source", el.picture_source)
            _opt(out, "poster", el.poster)
            _opt(out, "mask", el.mask)
            _opt(out, "clip", el.clip)
        case TargetShape():
            out["kind"] = "shape"
            out["shape"] = el.shape
            _opt(out, "from", None if el.start is None else _point(el.start))
            _opt(out, "to", None if el.end is None else _point(el.end))
            _opt(out, "shape_type", el.shape_type)
            out.update(fill=el.fill, outline=el.outline)
            _tri_put(out, "weight", el.weight)
            _opt(out, "arrow", el.arrow)
            _opt(out, "arrow_start", el.arrow_start)
            _opt(out, "start_arrow", el.start_arrow)
            _opt(out, "end_arrow", el.end_arrow)
            _tri_put(out, "line_type", el.line_type)
            _tri_put(out, "category", el.category)
            _opt(out, "frame", None if el.frame is None else _frame_json(el.frame))
            _opt(out, "dash", el.dash)
            _opt(out, "fill_alpha", el.fill_alpha)
            _opt(out, "outline_alpha", el.outline_alpha)
            _opt(out, "fill_unread", el.fill_unread)
            _opt(out, "_not_rendered", el.not_rendered)
            _opt(out, "fill_gradient", None if el.fill_gradient is None else _fill_gradient_json(el.fill_gradient))
            _opt(out, "fill_source", el.fill_source)
            _opt(out, "corner_radius", el.corner_radius)
            _opt(out, "pie", None if el.pie is None else _point(el.pie))
            _opt(out, "trace", None if el.trace is None else _trace_json(el.trace))
            _opt(out, "_no_ramp", el.no_ramp)
            _opt(out, "_unsaid", el.unsaid)
        case TargetTable():
            out["kind"] = "table"
            out["rows"] = [_strings(row) for row in el.rows]
            _opt(out, "cell_pad", None if el.cell_pad is None else _point(el.cell_pad))
            _opt(out, "col_widths", None if el.col_widths is None else _floats(el.col_widths))
            _opt(out, "row_heights", None if el.row_heights is None else _floats(el.row_heights))
            _opt(out, "rows_fixed", None if el.rows_fixed is None else _ints(el.rows_fixed))
            _opt(out, "cell_text_y", el.cell_text_y)
            if el.table_cells is not None:
                out["table_cells"] = [_cell_json(c) for c in el.table_cells]
            if el.table_borders is not None:
                out["table_borders"] = [_border_json(b) for b in el.table_borders]
        case TargetDiagram():
            out["kind"] = "diagram"
            out["nodes"] = [_node_json(n) for n in el.nodes]
        case _:
            assert_never(el)
    out["id"] = el.id
    _opt(out, "object", el.object)
    out["group"] = el.group
    _tri_put(out, "key", el.key)
    _opt(out, "inherited", el.inherited)
    return out


def slide_json(s: TargetSlide) -> JsonObject:
    out: JsonObject = {"page": s.page, "frame": s.frame, "size": _point(s.size), "objectId": s.object_id,
                       "key": s.key, "notes": s.notes, "background_color": s.background_color,
                       "background_picture": s.background_picture}
    g = s.background_gradient
    if not isinstance(g, Absent):
        out["background_gradient"] = None if g is None else _page_gradient_json(g)
    out["elements"] = [element_json(e) for e in s.elements]
    _tri_put(out, "background_file", s.background_file)
    _opt(out, "background_source", s.background_source)
    _tri_put(out, "layout", s.layout)
    _opt(out, "thumbnail", s.thumbnail)
    return out


def target_json(deck: TargetDeck) -> JsonObject:
    """target.json as `deck_ir` writes it."""
    src = deck.source
    source: JsonObject = {}
    _tri_put(source, "presentationId", src.presentation_id)
    source["title"] = src.title
    _tri_put(source, "revisionId", src.revision_id)
    out: JsonObject = {"version": deck.version, "source": source, "page_size": _point(deck.page_size),
                       "scale": deck.scale, "slides": [slide_json(s) for s in deck.slides]}
    if deck.layouts is not None:
        layouts: JsonObject = {}
        for name, lay in deck.layouts:
            layouts[name] = {"name": lay.name, "master": lay.master, "background_color": lay.background_color,
                             "background_picture": lay.background_picture}
        out["layouts"] = layouts
    _opt(out, "first_slide", deck.first_slide)
    return out


def is_target(v: JsonObject) -> bool:
    """Whether a deck.json-shaped value is `deck_ir`'s (every version writes `scale`, which neither
    classify's deck.json nor a rendered one has)."""
    return "scale" in v


__all__ = ["ABSENT", "Absent", "CellParagraph", "Chart", "Crop", "DeckSource", "DiagramNode", "FillGradient", "Frame",
           "IRError", "Layout", "PageGradient", "PictureOutline", "Recolor", "RecolorStop", "SlidesMeasures",
           "TableBorder", "TableCell", "TargetBullet", "TargetDeck", "TargetDiagram", "TargetElement", "TargetImage",
           "TargetLine", "TargetParagraph", "TargetRun", "TargetShape", "TargetSlide", "TargetTable", "TargetText",
           "TextBox", "ThumbnailOf", "Trace", "Video", "element_json", "is_target", "paragraph_json", "parse_element",
           "parse_page_gradient", "parse_target", "run_json",
           "slide_json", "target_json"]
