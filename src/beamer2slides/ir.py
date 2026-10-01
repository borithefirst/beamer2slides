"""deck.json, written down: what each stage hands the next, and a check that a deck holds to it.

`classify` (with `marked.classify_marked` for the PDF of an adopted source) writes a deck;
`render` adds its pictures' files and each slide's background; `emit` writes the deck to Slides.
The types below say what each of those dicts holds, at two stages:

- **classified**: what `classify.classify` returns (`Deck`); `emit.plan_offline` also runs on it.
- **rendered**: after `render.render_backgrounds` (`RenderedDeck`): every image has its `file`,
  every slide its `background`, and no shape is left that emit cannot draw
  (`marked.pictured_shapes` made those images).

A key is **required** when every producer writes it and its consumers take it for granted
(`el["x"]`, or compare or copy it whole); **optional** when some producer leaves it out and its
consumers read it with `.get`. A required key may still hold None where its type says so.

`problems(deck, stage)` lists where a deck breaks the contract, each line naming the slide page,
element id, key and what is wrong; `validate(deck, stage)` raises ValueError with them. Both are
driven by the TypedDicts below (their keys, `Literal` values and nested types): the types are the
one schema, and a change to one changes the check.

Python 3.10, no typing_extensions: a TypedDict with optional keys is a `total=True` class and a
`total=False` one joined by inheritance. No key is declared twice in one hierarchy (3.10 then
counts it both required and optional): where a stage narrows a key's type, each stage's class
declares it on its own, over bases the two share.

Not described here: what `deck_ir` reads back from Slides (lowercase shape names, `rows`,
`crop`...), another dialect that pull and adopt read.
"""

import re
import types
import typing
from dataclasses import dataclass
from typing import Annotated, Callable, Literal, TypedDict, TypeGuard, Union

from .json_types import Json, JsonObject

# ------------------------------------------------------------------------------ values


@dataclass(frozen=True)
class Length:
    """An `Annotated` marker: the list has exactly `n` items."""
    n: int


@dataclass(frozen=True)
class Pattern:
    """An `Annotated` marker: the string matches `regex` whole (`says` names it in a problem)."""
    regex: str
    says: str


Box = Annotated[list[float], Length(4)]
"""[x0, y0, x1, y1] in PDF points from the page's top left."""
Point = Annotated[list[float], Length(2)]
"""[x, y] in PDF points from the page's top left."""
Color = Annotated[str, Pattern(r"#[0-9a-f]{6}", "a colour '#rrggbb', lowercase")]
"""An opaque colour as extract writes it."""
SpanId = str
"""A raw.json span id: render erases the span from the background, compare pairs by it."""

# ------------------------------------------------------------------------------ closed sets

ElementKind = Literal["text", "image", "shape", "table", "diagram"]
TextRole = Literal["title", "body", "subtitle", "footer"]
"""`title` goes into the layout's TITLE placeholder, `subtitle` under it on a title page;
`footer` is theme text promoted to per-slide text (a frame counter, `promote_theme_text`)."""
ImageRole = Literal["math", "figure", "icon", "fallback"]
"""`math` a formula (a hole's picture, a display formula), `icon` an inline graphic or a list's
ball, `figure` the rest; `fallback` only in the deck emit rebuilds with pictures of refused
regions (`emit.fallback_pictures`). `emit.PICTURE_TITLES` titles pictures by it."""
ShapeRole = Literal["panel", "rule", "highlight", "line"]
"""`line` only from a marked page (`marked.shape_element`); render makes such a shape a picture."""
Align = Literal["left", "center", "right"]
Family = Literal["sans", "serif", "mono", "math", "icon"]
"""`fonts.font_info(font).family`; emit's faces for them are `emit_metrics.FONT_FOR_FAMILY`."""
Script = Literal["sub", "super"]
Position = Literal["TOP", "BOTTOM", "LEFT", "RIGHT"]
"""The side of its cell a table border is on (Slides' tableBorderProperties position)."""
RulePosition = Literal["TOP", "BOTTOM"]
TemplateKind = Literal["ROUND_RECTANGLE", "ROUND_2_SAME_RECTANGLE", "RECTANGLE", "ELLIPSE", "DIAMOND", "TRIANGLE"]
"""The Slides shapes emit creates: `emit_pptx.TEMPLATE_KINDS`, in its order."""
ShapeKind = TemplateKind
"""A `shape` emit can draw: every rendered shape element's."""
ProducerShapeKind = Literal["custom", "line"]
"""Shapes only a marked page's producer writes (`marked.shape_element`): a freeform outline, a
line. Legal when classified, which compare reads; render makes them pictures
(`marked.pictured_shapes`): emit has no Slides shape for them (KeyError 'custom' before 688ebf4)."""
BulletShape = Literal["square", "open_square", "disc", "circle", "triangle"]
"""A vector bullet's shape (`classify_text.bullet_shape`); each is in `emit_metrics.BULLET_SHAPES`."""
Arrow = Literal["OPEN_ARROW", "STEALTH_ARROW", "FILL_ARROW", "FILL_CIRCLE", "OPEN_CIRCLE", "FILL_SQUARE",
                "OPEN_SQUARE", "FILL_DIAMOND", "OPEN_DIAMOND"]
"""A diagram line end's Slides arrow style (`classify_figures.tip_head`)."""
Dash = Literal["DOT", "DASH", "DASH_DOT", "LONG_DASH", "LONG_DASH_DOT"]
"""A dashed diagram line's or node outline's Slides dash style (`classify_figures.dash_style`);
a solid one writes none."""
Bend = Literal["vh", "hv"]
"""An elbow line's turn: `vh` (|-) is what classify writes, `hv` (-|) only an old base's."""
PictureRoute = Literal["raw", "decoded"]
"""How render wrote a bare `\\includegraphics` (`render.image_file`): the author's JPEG byte for
byte, or PDFium's pixels as a PNG."""

# ------------------------------------------------------------------------------ text


class Line(TypedDict):
    """One PDF line of a paragraph: emit places and sizes the box by them."""
    baseline: float
    x0: float
    x1: float


BeforeWord = tuple[float, str, Family, bool, bool, str, float]
"""A word before a hole or an overlay's mark on its line, [width, font, family, bold, italic,
text, x0] (`classify_paragraphs.runs`, `classify_figures.mark`): emit predicts where Slides sets
the hole from them."""


class _RunKeys(TypedDict):
    text: str
    font: str
    family: Family
    size: float
    bold: bool
    italic: bool
    smallcaps: bool
    color: Color
    link: str | None
    """A URL, or '#page=N' inside the deck."""
    script: Script | None
    """compare subscripts it; emit lowers or raises the run (`emit.run_sizes`)."""


class Run(_RunKeys, total=False):
    """A stretch of text in one style. emit reads the font through `FontMapper` (font, family,
    size, bold, italic subscripted), writes smallcaps, color and link as they are, and reads the
    decorations with `.get` (`literal_list_numbers` inserts a run with no `strike`)."""
    underline: bool
    strike: bool
    highlight: Color | None
    hole: float
    """A formula hole: the run's text is no-break spaces this wide (pt), its picture the image
    anchored to the element (`emit_holes`); `hole_x0`, `before` and `next_x0` place it."""
    hole_x0: float
    before: list[BeforeWord]
    next_x0: float | None


class Label(TypedDict):
    """Where and how a list's number or glyph is drawn (`classify_text.label_of`): what
    `classify.literal_list_numbers` writes as text when Slides cannot number it."""
    x0: float
    baseline: float
    font: str
    family: Family
    size: float
    bold: bool
    italic: bool
    color: Color


class _BulletKeys(TypedDict):
    text: str
    bbox: Box


class _GlyphBulletKeys(_BulletKeys):
    kind: Literal["glyph"]


class GlyphBullet(_GlyphBulletKeys, total=False):
    """A bullet glyph (•, ▶, ⋆) Slides has a preset for (`emit_metrics.GLYPH_SHAPES`). A marked
    page's drawn bullet has only kind, text '' and bbox, with `drawn`."""
    color: Color
    label: Label | None
    drawn: bool
    ink: Box
    """Added by render (`render.glyph_ink`): the glyph's ink box, and in `fill` the share of it
    the ink fills (a disc 0.79). emit sizes and shapes the Slides bullet by them."""
    fill: float


class _NumberBulletKeys(_BulletKeys):
    kind: Literal["number"]


class NumberBullet(_NumberBulletKeys, total=False):
    """A list number (1. a) iv.) set by a Slides numbered preset (`emit_metrics.BULLET_PRESETS`);
    `patch` when it is drawn on a small box render patches out of the background."""
    color: Color
    label: Label | None
    patch: bool


class _ImageBulletKeys(_BulletKeys):
    kind: Literal["image"]
    image: str
    """The raw image's id (a theme's ball): render subscripts it."""


class ImageBullet(_ImageBulletKeys, total=False):
    """A ball drawn as an image, its number (if any) in `text` and `label`."""
    label: Label | None
    color: Color | None
    """Added by render (`render.ink_colour`): the ball's colour, None when it cannot be read."""


class _ShapeBulletKeys(_BulletKeys):
    kind: Literal["shape"]
    shape: BulletShape
    color: Color


class ShapeBullet(_ShapeBulletKeys, total=False):
    """A bullet drawn as a path (a square, a disc), patched out of the background."""
    patch: bool


Bullet = Union[GlyphBullet, NumberBullet, ImageBullet, ShapeBullet]
"""By `kind`. (classify's `icon` bullets are anchored images before the deck is written.)"""


def bullet_label(bullet: Bullet) -> Label | None:
    """Where a bullet's number or glyph is drawn; a drawn shape has none."""
    if bullet["kind"] == "shape":
        return None
    return bullet.get("label")


class _ParagraphKeys(TypedDict):
    align: Align
    level: int
    bullet: Bullet | None
    size: float
    """The paragraph's body size (pt): emit sizes its runs and line pitch from it."""
    text_x0: float
    """Where its text starts (after a hanging label, after `\\parindent`)."""
    lines: list[Line]
    runs: list[Run]


class Paragraph(_ParagraphKeys, total=False):
    """A paragraph of a text box. Theme texts (and the footers made of them) have no `tab_x0` or
    `wrap_limit`; the rest are said only where they hold."""
    tab_x0: float | None
    """Where a hanging label's text starts (`label<TAB>text`)."""
    wrap_limit: float | None
    """How wide (from the paragraph's left edge) a box may grow before TeX would have pulled up
    the next line's first word: a narrower box wraps the same way."""
    direction: Literal["rtl"]
    justified: bool
    line_starts: list[int]
    """Where each wrapped line after the first starts in the paragraph's text (UTF-16 units)."""


class _TextKeys(TypedDict):
    id: str
    kind: Literal["text"]
    role: TextRole
    bbox: Box
    paragraphs: list[Paragraph]
    spans: list[SpanId]
    """The page text it replaces: render erases it from the background."""


class Part(TypedDict, total=False):
    """Another call of a marked element (`<key>+shape`: a text box's panel), folded into it."""
    kind: ElementKind
    bbox: Box | None
    mark: str


class TextElement(_TextKeys, total=False):
    """A text box (`PageClassifier.text_element`, `marked.text_elements`)."""
    panel: int | None
    """The slide panel it stands on (an index into `panels`)."""
    code: bool
    strokes: list[Box]
    """Fraction bars and underlines now written as text: render erases their paths."""
    rotation: float
    """Degrees counterclockwise: turned text (`rotated_texts`, a marked box's turn)."""
    mark: str
    """A marked page's element key (the deck object adopt wrote it for): compare pairs by it."""
    mark_box: Box
    """The box adopt set the words in (a marked page's `/box`)."""
    hidden_spans: list[SpanId]
    """A marked box's words the page hides (under a later picture): still its words, not erased."""
    parts: list[Part]
    fill: Color
    """A marked text box's panel fill (`marked.fold_parts`)."""


class ThemeText(TypedDict):
    """A line of theme text (a footline, a header) as a text element with no id: slide
    `theme_texts`, and deck `layout_texts` for those on every slide (`promote_theme_text`)."""
    kind: Literal["text"]
    role: Literal["layout"]
    bbox: Box
    panel: None
    code: bool
    key: tuple[str, int, int, str]
    """[text, x0, baseline, colours]: the same key on every slide puts the line on the layout."""
    chars: int
    paragraphs: list[Paragraph]
    spans: list[SpanId]


# ------------------------------------------------------------------------------ images


class Mark(TypedDict):
    """Where an overlay graphic (a tikzmark arrow) meets a line of text: emit stretches the
    overlay to where Slides sets those words (`emit_places`)."""
    x: float
    hole_x0: float
    pads: float
    font: str
    family: Family
    size: float
    bold: bool
    italic: bool
    before: list[BeforeWord]


class Number(Label):
    """A list number drawn on a ball, written over the ball's picture (`literal_list_numbers`)."""
    text: str
    center: Point
    height: float


class _ImageKeys(TypedDict):
    id: str
    kind: Literal["image"]
    role: ImageRole
    bbox: Box
    spans: list[SpanId]
    """The glyphs its crop shows (render grows a formula's box to their ink)."""


class _ImageOptional(TypedDict, total=False):
    alt: str
    """Alt text: the words the picture shows."""
    anchor: str
    """The text element it belongs to (a hole's formula, an icon bullet, an overlay): emit groups
    them and places the picture on the words."""
    drawings: list[str]
    """An overlay's own drawings: render subscripts them for an overlay's crop."""
    image: str
    """The raw image a bare `\\includegraphics` region is: render may keep its file."""
    marks: list[Mark]
    number: Number
    overlay: bool
    """A graphic drawn over text (`classify.overlay`), cropped on a transparent ground."""
    mark: str
    """An adopted source's picture: the mark's key (`marked.py`)."""
    mark_n: Union[int, str]
    """Its mark's `/n`, the element's number on the page slides.sty counts: `inverse.others_than`
    switches every other object off by it to judge the picture alone. A mark with no `/n` (only
    hand-written PDFs) gives its key here instead, as `marked.group_id` does, which is what
    `others_than` matches it by."""
    rotation: float
    """Degrees counterclockwise: a picture of a turned marked text box (`marked.text_elements`)."""
    hidden_spans: list[SpanId]
    """A turned marked box's glyphs the page hides (`marked.text_elements`): not erased."""
    parts: list[Part]
    """A marked picture's other calls (`marked.fold_parts`)."""


class ImageElement(_ImageKeys, _ImageOptional):
    """A picture, classified: its crop is not made yet."""


class _RenderedImageKeys(TypedDict):
    file: str
    """The picture's file relative to the output folder (`figures/<id>.png`): emit uploads it."""


class _RenderedImageOptional(TypedDict, total=False):
    px: tuple[int, int]
    """The file's size in pixels (render's record; no consumer reads it)."""
    picture: PictureRoute


class RenderedImage(ImageElement, _RenderedImageKeys, _RenderedImageOptional):
    """A picture after render: its file is written."""


# ------------------------------------------------------------------------------ shapes


class Outline(TypedDict):
    color: Color
    width: float


class Shadow(TypedDict):
    """A beamer block's drop shadow: emit gives the body a native one of `size`; render paints
    out `pieces` (or drops the shadow where the page around it is not flat)."""
    size: float
    pieces: list[Box]


class _ShapeKeys(TypedDict):
    id: str
    kind: Literal["shape"]
    role: ShapeRole
    bbox: Box
    flip: bool
    """emit_pptx subscripts it: the preset turned over (a title bar's rounded side up)."""
    radius: float
    """Corner radius (pt): render subscripts it."""
    spans: list[SpanId]


class _ShapeOptional(TypedDict, total=False):
    drawing: str
    """The raw drawing it is (a marked shape names its `drawings` instead)."""
    drawings: list[str]
    frame_drawings: list[str]
    anchor: str
    block: int
    """A beamer block's index: its title bar and body are two shapes of one block."""
    title_bar: Box
    strips: list[Box]
    shadow: Shadow
    opacity: float
    outline: Outline | None
    tiles: list[str]
    mark: str
    picture: list[str]
    """A marked shape's picture fill, raw image ids (render makes such a shape its picture)."""
    parts: list[Part]
    """A marked shape's other calls (`marked.fold_parts`)."""


class ShapeElement(_ShapeKeys, _ShapeOptional):
    """A panel, rule or highlight, classified (`classify_graphics`, `marked.shape_element`): a
    marked page may say shapes emit cannot draw (`ProducerShapeKind`, no fill)."""
    shape: ShapeKind | ProducerShapeKind
    fill: Color | None


class RenderedShape(_ShapeKeys, _ShapeOptional):
    """A shape after render: one emit draws (never marked, or `marked.drawn_natively`)."""
    shape: ShapeKind
    fill: Color


# ------------------------------------------------------------------------------ tables


class _ColumnKeys(TypedDict):
    x0: float
    x1: float
    align: Align


class Column(_ColumnKeys, total=False):
    """A column's words' extent and alignment; a head set otherwise than its body keeps its own
    (`head`, the body's extent in `body`); `centred` lists rows whose cell is set centred
    (siunitx's dash for a missing value) (`classify_tables.column_info`)."""
    head: Align
    body: tuple[float, float]
    centred: list[int]


class Merge(TypedDict):
    row: int
    col: int
    rows: int
    cols: int
    align: Align


class Rule(TypedDict):
    """A horizontal rule (booktabs) at a row's edge, `y` where the PDF draws it."""
    row: int
    position: RulePosition
    color: Color
    weight: float
    y: float


class _BorderKeys(TypedDict):
    row: int
    col: int
    position: Position
    color: Color
    weight: float


class Border(_BorderKeys, total=False):
    """A cell side's border (a marked table's row edges say their `y`)."""
    y: float


class CellFill(TypedDict):
    row: int
    col: int
    color: Color


class _TableKeys(TypedDict):
    id: str
    kind: Literal["table"]
    role: Literal["table"]
    bbox: Box
    cells: list[list[list[Run]]]
    """Rows of cells, each cell its runs."""
    frame: Box
    """The table's outer edges: emit places the table by them."""
    size: float
    row_baselines: list[float]
    row_heights: list[float]
    columns: list[Column]
    rules: list[Rule]
    spans: list[SpanId]


class TableElement(_TableKeys, total=False):
    """A native table (`classify_tables`, `marked.table_element`): emit_tables subscripts the
    required keys and reads the others with `.get`."""
    bounds: list[float]
    """Column edges, one more than columns."""
    merges: list[Merge]
    merge_x: list[tuple[float, float]]
    """Per merge (by index), where its words run: a flush merged cell keeps its indent."""
    borders: list[Border]
    fills: list[CellFill]
    bands: list[tuple[int, float, float]]
    """[row, top, bottom] of rows shaded by a band of their own: the Slides row's edges."""
    row_lines: list[int]
    """Lines per row, where some cell wraps."""
    wrapped: list[tuple[int, int, list[int]]]
    """[row, col, where each line after the first starts in the cell's text] per wrapped cell."""
    justified: list[tuple[int, int]]
    """[row, col] of wrapped cells set justified."""
    drawings: list[str]
    mark: str
    parts: list[Part]
    """A marked table's other calls (`marked.fold_parts`)."""


# ------------------------------------------------------------------------------ diagrams


class CardBox(TypedDict):
    """A text box on a card node's baselines (`classify_text.card_text`)."""
    paragraphs: list[Paragraph]


class _NodeKeys(TypedDict):
    bbox: Box
    shape: TemplateKind | None
    """None: a free label, no shape drawn around it."""
    fill: Color | None
    stroke: Color | None
    width: float | None
    paragraphs: list[list[Run]]
    """The label's lines, each its runs."""
    baselines: list[float]
    label_w: float
    text: list[CardBox] | None
    """A card's text (more than a centred label) as text boxes; None for a plain label."""


class Node(_NodeKeys, total=False):
    radius: float
    dash: Dash
    """A dashed outline (absent: solid, and in a deck.json older than it)."""


# (`from` is a keyword: this part is written the functional way)
_DiagramLineKeys = TypedDict("_DiagramLineKeys", {
    "from": Point, "to": Point, "arrow_from": Union[Arrow, None], "arrow_to": Union[Arrow, None],
    "stroke": Color, "width": float})


class DiagramLine(_DiagramLineKeys, total=False):
    """A line or arrow from `from` to `to`; an elbow turns at `via`."""
    via: Point
    bend: Bend
    dash: Dash
    """A dashed line (absent: solid, and in a deck.json older than it)."""


class DiagramElement(TypedDict):
    """Nodes, lines and arrows as grouped Slides shapes (`classify_figures.diagram_from`)."""
    id: str
    kind: Literal["diagram"]
    role: Literal["figure"]
    bbox: Box
    nodes: list[Node]
    lines: list[DiagramLine]
    spans: list[SpanId]


# ------------------------------------------------------------------------------ slides, decks

Element = Union[TextElement, ImageElement, ShapeElement, TableElement, DiagramElement]
"""By `kind`, in drawing order (bottom first)."""
RenderedElement = Union[TextElement, RenderedImage, RenderedShape, TableElement, DiagramElement]


class Panel(TypedDict):
    """A filled area of the page (text on one is on a panel: `TextElement.panel`)."""
    bbox: Box
    fill: Color | None
    rounded: bool


class LeftInBackground(TypedDict):
    """Page text no element took, by why (`theme`, `figure`, `math`, `unsure`...)."""
    reason: str
    spans: list[SpanId]
    bboxes: list[Box]


class SlideStats(TypedDict):
    chars: int
    chars_native: int


class _SlideKeys(TypedDict):
    page: int
    """The PDF page index (0-based): render, emit and sync subscript it."""
    frame: str
    """The frame number as the PDF's page label says it."""
    label: str | None
    """The frame's `label=` (hyperref's destination): the slide identity sync pairs by."""
    size: Point
    """The page's width and height (pt)."""
    notes: str | None
    """Speaker notes (a notes page's text)."""
    left_in_background: list[LeftInBackground]
    theme_texts: list[ThemeText]
    panels: list[Panel]
    figure_regions: list[Box]
    stats: SlideStats


class _SlideOptional(TypedDict, total=False):
    on_layout: list[SpanId]
    """Theme text now on the layout: render erases it from the background."""
    title_page: bool
    marked: int
    """How many marks a marked page has (`marked.classify_marked`)."""


class Slide(_SlideKeys, _SlideOptional):
    elements: list[Element]


class RenderedSlide(_SlideKeys, _SlideOptional):
    elements: list[RenderedElement]
    background: str
    """The background picture's file, relative to the output folder."""
    background_color: Color | None
    """The background's one colour when nothing is left on it: a plain Slides fill instead."""


class Source(TypedDict):
    """Where the deck came from (open to more keys: `OPEN`)."""
    pdf: str
    pages: int
    producer: str
    title: str


class DeckStats(TypedDict):
    chars: int
    chars_native: int
    native_share: float


class _DeckKeys(TypedDict):
    version: int
    source: Source
    body_size: float
    """The deck's body text size (pt)."""
    stats: DeckStats
    layout_texts: list[ThemeText]


class Deck(_DeckKeys):
    """deck.json as classify writes it."""
    slides: list[Slide]


class RenderedDeck(_DeckKeys):
    """deck.json after render: what emit uploads."""
    slides: list[RenderedSlide]


STAGES: dict[str, type] = {"classified": Deck, "rendered": RenderedDeck}
OPEN: frozenset[type] = frozenset({Source})
"""TypedDicts a key they do not declare is no problem in."""
LOCATED: frozenset[type] = frozenset({Slide, RenderedSlide, TextElement, ImageElement, RenderedImage, ShapeElement,
                                      RenderedShape, TableElement, DiagramElement})
"""TypedDicts a problem inside is reported under (its slide's page, its element's id)."""

# ------------------------------------------------------------------------------ the check

Check = Callable[[object, str, str, list[str]], None]
"""check(value, where, path, problems): appends a line per problem found in `value`."""

_NAMES: dict[object, str] = {Box: "Box", Point: "Point", Color: "Color", BeforeWord: "BeforeWord"}


def type_name(tp: object) -> str:
    """A type as these docs write it."""
    if tp in _NAMES:
        return _NAMES[tp]
    if tp is type(None):
        return "None"
    if isinstance(tp, type) and not typing.get_args(tp):
        return tp.__name__
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is Literal:
        return " | ".join(repr(a) for a in args)
    if origin in (Union, types.UnionType):
        return " | ".join(type_name(a) for a in args)
    if origin is Annotated:
        return type_name(args[0])
    for kind in (list, tuple, dict):
        if origin is kind:
            return f"{kind.__name__}[{', '.join(type_name(a) for a in args)}]"
    return str(tp)


def _say(out: list[str], where: str, path: str, what: str) -> None:
    out.append(f"{where}: {path}: {what}" if path else f"{where}: {what}")


def _show(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _wrong(out: list[str], where: str, path: str, value: object, expected: str) -> None:
    _say(out, where, path, f"is {_show(value)} ({type(value).__name__}), expected {expected}")


def _typeddict(tp: object) -> type | None:
    """`tp` when it is a TypedDict class, else None."""
    # (list[int] passes for a type on 3.10 and 3.11: a generic alias is none)
    if typing.get_origin(tp) is None and isinstance(tp, type) and issubclass(tp, dict) \
            and hasattr(tp, "__required_keys__"):
        return tp
    return None


def _required_keys(cls: type) -> list[str]:
    """A TypedDict's required keys (`__required_keys__`, which `type` does not declare), sorted."""
    keys: frozenset[str] = vars(cls)["__required_keys__"]
    return sorted(keys)


_CHECKS: dict[tuple[object, bool], Check] = {}


def checker(tp: object, strict: bool) -> Check:
    """The compiled check of a type (cached). `strict`: a key no TypedDict declares is a problem."""
    got = _CHECKS.get((tp, strict))
    if got is None:
        got = _CHECKS[tp, strict] = _compile(tp, strict)
    return got


def _scalar(tp: type) -> Check:
    kinds: tuple[type, ...] = (int, float) if tp is float else (tp,)
    name = tp.__name__

    def check(v: object, where: str, path: str, out: list[str]) -> None:
        if not isinstance(v, kinds) or (isinstance(v, bool) and tp is not bool):
            _wrong(out, where, path, v, name)
    return check


def _check_none(v: object, where: str, path: str, out: list[str]) -> None:
    if v is not None:
        _wrong(out, where, path, v, "None")


def _compile(tp: object, strict: bool) -> Check:
    if tp is type(None):
        return _check_none
    for scalar in (bool, int, float, str):
        if tp is scalar:
            return _scalar(scalar)
    cls = _typeddict(tp)
    if cls is not None:
        return _compile_typeddict(cls, strict)
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is Annotated:
        return _compile_annotated(checker(args[0], strict), args[1:])
    if origin is Literal:
        def check_literal(v: object, where: str, path: str, out: list[str]) -> None:
            if not any(type(v) is type(a) and v == a for a in args):
                _say(out, where, path, f"is {_show(v)}, expected one of {type_name(tp)}")
        return check_literal
    if origin is Union or origin is types.UnionType:
        return _compile_union(tp, args, strict)
    if origin is list:
        item = checker(args[0], strict)

        def check_list(v: object, where: str, path: str, out: list[str]) -> None:
            if not isinstance(v, list):
                _wrong(out, where, path, v, type_name(tp))
                return
            for i, x in enumerate(v):
                item(x, where, f"{path}[{i}]", out)
        return check_list
    if origin is tuple:  # (a JSON list of fixed length, each place its own type)
        items = [checker(a, strict) for a in args]

        def check_tuple(v: object, where: str, path: str, out: list[str]) -> None:
            if not isinstance(v, (list, tuple)) or len(v) != len(items):
                _say(out, where, path, f"is {_show(v)}, expected {len(items)} items: {type_name(tp)}")
                return
            for i, (x, c) in enumerate(zip(v, items)):
                c(x, where, f"{path}[{i}]", out)
        return check_tuple
    raise TypeError(f"ir: no check for {tp!r}")


def _compile_annotated(inner: Check, markers: tuple[object, ...]) -> Check:
    lengths = [m.n for m in markers if isinstance(m, Length)]
    patterns = [(re.compile(m.regex), m.says) for m in markers if isinstance(m, Pattern)]

    def check(v: object, where: str, path: str, out: list[str]) -> None:
        seen = len(out)
        inner(v, where, path, out)
        if len(out) > seen:
            if patterns:  # a colour given as None says what a colour is, not "expected str"
                del out[seen:]
                _wrong(out, where, path, v, patterns[0][1])
            return
        # (the inner check passed: a length marker's value is a list, a pattern's a string)
        for n in lengths:
            if isinstance(v, list) and len(v) != n:
                _say(out, where, path, f"has {len(v)} items, expected {n}")
        for rx, says in patterns:
            if isinstance(v, str) and not rx.fullmatch(v):
                _say(out, where, path, f"is {_show(v)}, expected {says}")
    return check


def _kinds(tp: object) -> tuple[object, ...]:
    """The `kind` values a TypedDict arm of a union is told apart by (none: not such an arm)."""
    cls = _typeddict(tp)
    if cls is None or "kind" not in _required_keys(cls):
        return ()
    kind = typing.get_type_hints(cls)["kind"]
    return typing.get_args(kind) if typing.get_origin(kind) is Literal else ()


def _compile_union(tp: object, args: tuple[object, ...], strict: bool) -> Check:
    nullable = type(None) in args
    arms = [a for a in args if a is not type(None)]
    if len(arms) == 1:  # X | None: X's own problems, told precisely
        only = checker(arms[0], strict)

        def check_optional(v: object, where: str, path: str, out: list[str]) -> None:
            if v is not None or not nullable:
                only(v, where, path, out)
        return check_optional
    kinds = [_kinds(a) for a in arms]
    by_kind = {k: checker(a, strict) for a, ks in zip(arms, kinds) for k in ks}
    if all(kinds) and len(by_kind) == sum(map(len, kinds)):  # told apart by `kind`
        names = ", ".join(repr(k) for k in by_kind)

        def check_by_kind(v: object, where: str, path: str, out: list[str]) -> None:
            if v is None and nullable:
                return
            if not isinstance(v, dict):
                _wrong(out, where, path, v, type_name(tp))
                return
            kind: object = v.get("kind")
            arm = by_kind.get(kind)
            if arm is None:
                _say(out, where, f"{path}.kind" if path else "kind", f"is {_show(kind)}, expected one of {names}")
            else:
                arm(v, where, path, out)
        return check_by_kind
    compiled = [checker(a, strict) for a in arms]

    def check_any(v: object, where: str, path: str, out: list[str]) -> None:
        if v is None and nullable:
            return
        for c in compiled:
            trial: list[str] = []
            c(v, where, path, trial)
            if not trial:
                return
        _wrong(out, where, path, v, type_name(tp))
    return check_any


def _compile_typeddict(cls: type, strict: bool) -> Check:
    hints = typing.get_type_hints(cls, include_extras=True)
    required = _required_keys(cls)
    fields: dict[str, Check] = {}  # (compiled at first use: TypedDicts name each other in any order)
    lenient = cls in OPEN or not strict
    located = cls in LOCATED
    is_slide = cls in (Slide, RenderedSlide)
    name = cls.__name__

    def check(v: object, where: str, path: str, out: list[str]) -> None:
        if not isinstance(v, dict):
            _wrong(out, where, path, v, name)
            return
        if located:
            if is_slide:
                page: object = v.get("page", "?")
                where = f"slide page {page}"
            else:
                element: object = v.get("id")
                where = f"{where}, element {element or '?'}"
            path = ""
        if not fields:
            fields.update((k, checker(t, strict)) for k, t in hints.items())
        for k in required:
            if k not in v:
                _say(out, where, path, f"lacks required key {k!r} ({type_name(hints[k])})")
        for k, x in v.items():
            c = fields.get(k)
            if c is not None:
                c(x, where, f"{path}.{k}" if path else k, out)
            elif not lenient:
                _say(out, where, path, f"has unknown key {k!r} (not in {name})")
    return check


def problems(deck: object, stage: str, *, unknown_keys: bool) -> list[str]:
    """Where `deck` breaks deck.json's contract at `stage` ('classified' or 'rendered'), a line
    each: "slide page 3, element p3t1: paragraphs[0].runs[2].color: is ...". A key no type here
    declares is a problem unless `unknown_keys=False`: a producer's new key is written here first."""
    if stage not in STAGES:
        raise ValueError(f"ir: stage {stage!r} is none of {', '.join(STAGES)}")
    out: list[str] = []
    checker(STAGES[stage], unknown_keys)(deck, "deck", "", out)
    return out


def validate(deck: object, stage: str, *, unknown_keys: bool) -> None:
    """Raise ValueError listing every problem (`problems`) when `deck` breaks the contract."""
    found = problems(deck, stage, unknown_keys=unknown_keys)
    if found:
        raise ValueError(f"deck.json breaks its {stage} contract ({len(found)} problems):\n  " + "\n  ".join(found))


def _is_slide(v: object) -> TypeGuard[Slide]:
    return not _slide_problems(v)


def _slide_problems(v: object) -> list[str]:
    out: list[str] = []
    checker(Slide, False)(v, "slide", "", out)
    return out


def slide_of(v: JsonObject) -> Slide:
    """A slide another producer wrote as JSON (`marked.classify_marked`), as the contract's
    classified `Slide` (keys no type declares allowed); ValueError listing what breaks it."""
    if _is_slide(v):
        return v
    found = _slide_problems(v)
    raise ValueError(f"not a classified slide ({len(found)} problems):\n  " + "\n  ".join(found))


# ------------------------------------------------------------------------------ as JSON


def _json(v: object, where: str) -> Json:
    """A deck.json value as JSON: tuples become lists, the dicts' key order kept."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_json(x, f"{where}[{i}]") for i, x in enumerate(v)]
    if isinstance(v, dict):
        out: JsonObject = {}
        for k, x in v.items():
            key: object = k
            if not isinstance(key, str):
                raise TypeError(f"ir: {where}: a key {key!r} is no string")
            out[key] = _json(x, f"{where}.{key}")
        return out
    raise TypeError(f"ir: {where}: {type(v).__name__} is no JSON value")


def run_json(run: Run) -> JsonObject:
    """A run as JSON (a copy), for a producer still building JSON (`marked.table_element`)."""
    return {k: _json(v, k) for k, v in run.items()}


def element_json(element: Element) -> JsonObject:
    """A classified element as JSON (a copy), for a reader of deck.json's form (`emit.table_fits`)."""
    return {k: _json(v, k) for k, v in element.items()}


def slide_json(slide: Slide) -> JsonObject:
    """A classified slide as JSON, as deck.json writes it (a copy: the slide is not changed)."""
    return {k: _json(v, k) for k, v in slide.items()}


def deck_json(deck: Deck) -> JsonObject:
    """A classified deck as JSON (`classify.classify`'s answer as render, emit and deck.json read
    it), a copy: tuples become lists, keys keep their order."""
    return {k: _json(v, k) for k, v in deck.items()}
