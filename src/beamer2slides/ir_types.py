"""deck.json as typed values: a frozen dataclass per element kind and stage, parsed from and written
back to exactly the JSON the producers write today (docs/typing.md, "The IR").

`ir.py` says the same contract as TypedDicts and checks a dict against it; this module is the form
the code will hold instead of dicts. Nothing reads it yet: producers and consumers move to it one
by one, parsing at their entry (`parse_deck`, `parse_element`) and writing at their exit
(`deck_json`, `element_json`).

**Stages are types.** A rendered deck is a classified one plus what render adds, and where render
adds a field the rendered record is a subclass that has it: `RenderedImage` (its file), the glyph
and drawn bullets' ink, an image bullet's colour, a marked shape emit can draw
(`RenderedMarkedShape`), and the paragraphs and text elements holding such bullets. A slide and a
deck are two classes each (`Slide`/`RenderedSlide`, `Deck`/`RenderedDeck`): a rendered slide may
hold a `FallbackImage`, which no classified slide can.

**Variants are types** where producers write different keys for one kind: a hole run
(`HoleRun`: a formula's room, never struck), a marked page's drawn bullet (`DrawnBullet`), a marked
page's shape (`MarkedShape`: `drawings` and an `outline` always said, where a beamer shape has one
`drawing` and says an outline only when it has one), and the picture of a refused element
(`FallbackImage`: no spans).

**The JSON form is today's, key for key**, because `identity.ir_fields` hashes an element's
dumped JSON (key set included) and sync bases store those hashes:
  * a field typed `X | None` whose key producers leave out writes no key for None ("None:
    absent"); one whose key is always written with null writes null ("nullable");
  * a paragraph of theme text (`ThemeText`, and a footer: a `TextElement` of role `footer`)
    writes no `tab_x0` / `wrap_limit`, which are None there; every other paragraph writes both;
  * a flag (`overlay`, `justified`, `title_page`, `composite`, a number bullet's `patch`) is
    written only when true. This is the one canonicalisation: a `false` flag reads as absent and
    is written as absent. No deck.json or base holds one (docs in tests/test_ir_types.py);
  * numbers are kept as they came: an int where a float goes stays an int (the hash dumps `3`
    and `3.0` differently);
  * fixed-length arrays (`Box`, `Point`, `BeforeWord`...) and lists are tuples in memory, lists in
    JSON: tuples are immutable (the records are frozen), a fixed length is a type the checker
    reads, and `json.dumps` writes a tuple as the list it came from, so a hash is the same.

Evidence for each required / None-absent / nullable choice: tests/test_ir_types.py's docstring.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, NewType, NoReturn, TypeVar, Union, overload

from .ir import (Align, Arrow, Bend, BulletShape, ElementKind, Family, PictureRoute, Position, ProducerShapeKind,
                 RulePosition, Script, ShapeKind, ShapeRole, TemplateKind, TextRole)
from .json_types import Json, JsonObject
from .typing_compat import assert_never

# ------------------------------------------------------------------------------ values

Color = NewType("Color", str)
"""'#rrggbb', lowercase: a string the parser has checked is one."""
Box = tuple[float, float, float, float]
"""(x0, y0, x1, y1) in PDF points from the page's top left."""
Point = tuple[float, float]
BeforeWord = tuple[float, str, Family, bool, bool, str, float]
"""A word before a hole or an overlay's mark: (width, font, family, bold, italic, text, x0)."""
SpanId = str
DrawingId = str
ImageId = str
Stage = Literal["classified", "rendered"]
ImageElementRole = Literal["math", "figure", "icon"]
"""An `ImageElement`'s role: `fallback` is a `FallbackImage`."""

ALIGNS: tuple[Align, ...] = ("left", "center", "right")
FAMILIES: tuple[Family, ...] = ("sans", "serif", "mono", "math", "icon")
SCRIPTS: tuple[Script, ...] = ("sub", "super")
TEXT_ROLES: tuple[TextRole, ...] = ("title", "body", "subtitle", "footer")
IMAGE_ROLES: tuple[ImageElementRole, ...] = ("math", "figure", "icon")
SHAPE_ROLES: tuple[ShapeRole, ...] = ("panel", "rule", "highlight", "line")
TEMPLATE_KINDS: tuple[TemplateKind, ...] = ("ROUND_RECTANGLE", "ROUND_2_SAME_RECTANGLE", "RECTANGLE", "ELLIPSE",
                                            "DIAMOND", "TRIANGLE")
PRODUCER_SHAPE_KINDS: tuple[ProducerShapeKind, ...] = ("custom", "line")
BULLET_SHAPES: tuple[BulletShape, ...] = ("square", "open_square", "disc", "circle", "triangle")
POSITIONS: tuple[Position, ...] = ("TOP", "BOTTOM", "LEFT", "RIGHT")
RULE_POSITIONS: tuple[RulePosition, ...] = ("TOP", "BOTTOM")
ARROWS: tuple[Arrow, ...] = ("OPEN_ARROW", "STEALTH_ARROW", "FILL_ARROW")
BENDS: tuple[Bend, ...] = ("vh", "hv")
PICTURE_ROUTES: tuple[PictureRoute, ...] = ("raw", "decoded")
ELEMENT_KINDS: tuple[ElementKind, ...] = ("text", "image", "shape", "table", "diagram")
BulletKind = Literal["glyph", "number", "image", "shape"]
BULLET_KINDS: tuple[BulletKind, ...] = ("glyph", "number", "image", "shape")
"""Each Literal of ir.py spelled out (the checker reads a tuple's values; tests/test_ir_types.py
holds each equal to its Literal)."""

# ------------------------------------------------------------------------------ text


@dataclass(frozen=True, kw_only=True)
class Line:
    """One PDF line of a paragraph."""
    baseline: float
    x0: float
    x1: float


@dataclass(frozen=True, kw_only=True)
class _RunStyle:
    """What every run says: its words and their style."""
    text: str
    font: str
    family: Family
    size: float
    bold: bool
    italic: bool
    smallcaps: bool
    color: Color
    link: str | None
    """A URL, or '#page=N' inside the deck (nullable)."""
    script: Script | None
    underline: bool
    highlight: Color | None
    """(nullable)"""


@dataclass(frozen=True, kw_only=True)
class Run(_RunStyle):
    """Words in one style."""
    strike: bool | None
    """None: absent (a fraction slash, a literal list number: nothing said, not struck)."""


@dataclass(frozen=True, kw_only=True)
class HoleRun(_RunStyle):
    """A formula hole: no-break spaces `hole` pt wide, its picture the image anchored to the element."""
    hole: float
    hole_x0: float
    before: tuple[BeforeWord, ...]
    next_x0: float | None
    """Where the word after it starts (nullable: none on its line, or a space between)."""


AnyRun = Union[Run, HoleRun]


@dataclass(frozen=True, kw_only=True)
class Label:
    """Where and how a list's number or glyph is drawn."""
    x0: float
    baseline: float
    font: str
    family: Family
    size: float
    bold: bool
    italic: bool
    color: Color


@dataclass(frozen=True, kw_only=True)
class GlyphInk:
    """A glyph bullet's ink box and the share of it the ink fills (render.glyph_ink): JSON `ink`, `fill`."""
    box: Box
    fill: float


@dataclass(frozen=True, kw_only=True)
class GlyphBullet:
    """A bullet glyph (•, ▶) Slides has a preset for. JSON kind `glyph`."""
    text: str
    bbox: Box
    color: Color
    label: Label


@dataclass(frozen=True, kw_only=True)
class RenderedGlyphBullet(GlyphBullet):
    ink: GlyphInk | None
    """None: render could not measure it (absent)."""


@dataclass(frozen=True, kw_only=True)
class DrawnBullet:
    """A marked page's bullet drawn as art (`marked.py`): JSON kind `glyph` with `drawn: true`."""
    text: str
    bbox: Box


@dataclass(frozen=True, kw_only=True)
class RenderedDrawnBullet(DrawnBullet):
    ink: GlyphInk | None


@dataclass(frozen=True, kw_only=True)
class NumberBullet:
    """A list number set by a Slides numbered preset. JSON kind `number`."""
    text: str
    bbox: Box
    color: Color
    label: Label
    patch: bool
    """Drawn on a small box render patches out of the background (a flag)."""


@dataclass(frozen=True, kw_only=True)
class ImageBullet:
    """A ball drawn as an image, its number (if any) in `text` and `label`. JSON kind `image`."""
    text: str
    bbox: Box
    image: ImageId
    label: Label | None
    """(nullable)"""


@dataclass(frozen=True, kw_only=True)
class RenderedImageBullet(ImageBullet):
    color: Color | None
    """The ball's colour (render.ink_colour); None when it cannot be read (nullable)."""


@dataclass(frozen=True, kw_only=True)
class ShapeBullet:
    """A bullet drawn as a path (a square, a disc), patched out of the background. JSON kind `shape`."""
    text: str
    bbox: Box
    shape: BulletShape
    color: Color
    patch: bool
    """Always written (always true today)."""


Bullet = Union[GlyphBullet, DrawnBullet, NumberBullet, ImageBullet, ShapeBullet]
RenderedBullet = Union[RenderedGlyphBullet, RenderedDrawnBullet, NumberBullet, RenderedImageBullet, ShapeBullet]


@dataclass(frozen=True, kw_only=True)
class Paragraph:
    """A paragraph of a text box, a theme line or a diagram card."""
    align: Align
    level: int
    bullet: Bullet | None
    size: float
    text_x0: float
    lines: tuple[Line, ...]
    runs: tuple[AnyRun, ...]
    tab_x0: float | None
    """Where a hanging label's text starts. Never set in theme text, which writes no key."""
    wrap_limit: float | None
    """How wide a box may grow before TeX would have pulled up the next line's first word."""
    direction: Literal["rtl"] | None
    justified: bool
    line_starts: tuple[int, ...] | None


@dataclass(frozen=True, kw_only=True)
class RenderedParagraph(Paragraph):
    bullet: RenderedBullet | None


@dataclass(frozen=True, kw_only=True)
class Part:
    """Another call of a marked element (a text box's panel), folded into it."""
    kind: ElementKind
    bbox: Box
    mark: str


@dataclass(frozen=True, kw_only=True)
class TextElement:
    """A text box. JSON kind `text`; a `footer` (a frame counter promoted from theme text) has
    theme paragraphs."""
    id: str
    role: TextRole
    bbox: Box
    paragraphs: tuple[Paragraph, ...]
    spans: tuple[SpanId, ...]
    panel: int | None
    """(nullable)"""
    code: bool
    strokes: tuple[Box, ...]
    rotation: float | None
    mark: str | None
    mark_box: Box | None
    hidden_spans: tuple[SpanId, ...] | None
    parts: tuple[Part, ...] | None
    fill: Color | None
    composite: bool
    """Folded from several of the converter's boxes against one of the deck's (a flag, adopt_sync)."""


@dataclass(frozen=True, kw_only=True)
class RenderedText(TextElement):
    paragraphs: tuple[RenderedParagraph, ...]


@dataclass(frozen=True, kw_only=True)
class ThemeText:
    """A line of theme text: JSON kind `text`, role `layout`, panel null, theme paragraphs."""
    bbox: Box
    code: bool
    key: tuple[str, int, int, str]
    chars: int
    paragraphs: tuple[Paragraph, ...]
    spans: tuple[SpanId, ...]

# ------------------------------------------------------------------------------ images


@dataclass(frozen=True, kw_only=True)
class Mark:
    """Where an overlay graphic meets a line of text."""
    x: float
    hole_x0: float
    pads: float
    font: str
    family: Family
    size: float
    bold: bool
    italic: bool
    before: tuple[BeforeWord, ...]


@dataclass(frozen=True, kw_only=True)
class Number(Label):
    """A list number drawn on a ball, written over the ball's picture."""
    text: str
    center: Point
    height: float


@dataclass(frozen=True, kw_only=True)
class ImageElement:
    """A picture, classified. JSON kind `image`."""
    id: str
    role: ImageElementRole
    bbox: Box
    spans: tuple[SpanId, ...]
    alt: str | None
    anchor: str | None
    drawings: tuple[DrawingId, ...] | None
    image: ImageId | None
    marks: tuple[Mark, ...] | None
    number: Number | None
    overlay: bool
    mark: str | None
    mark_n: int | str | None


@dataclass(frozen=True, kw_only=True)
class RenderedImage(ImageElement):
    file: str
    px: tuple[int, int]
    picture: PictureRoute | None


@dataclass(frozen=True, kw_only=True)
class FallbackImage:
    """The picture of an element Slides refused or emit could not plan (`emit.fallback_element`):
    JSON kind `image`, role `fallback`, and no spans."""
    id: str
    bbox: Box
    file: str

# ------------------------------------------------------------------------------ shapes


@dataclass(frozen=True, kw_only=True)
class Outline:
    color: Color
    width: float


@dataclass(frozen=True, kw_only=True)
class Shadow:
    size: float
    pieces: tuple[Box, ...]


@dataclass(frozen=True, kw_only=True)
class ShapeElement:
    """A panel, rule or highlight of a beamer page (`classify_graphics`): the same at both stages.
    JSON kind `shape`."""
    id: str
    role: ShapeRole
    bbox: Box
    shape: ShapeKind
    fill: Color
    flip: bool
    radius: float
    spans: tuple[SpanId, ...]
    drawing: DrawingId
    outline: Outline | None
    opacity: float | None
    block: int | None
    title_bar: Box | None
    strips: tuple[Box, ...] | None
    shadow: Shadow | None
    anchor: str | None
    tiles: tuple[str, ...] | None
    frame_drawings: tuple[DrawingId, ...] | None


@dataclass(frozen=True, kw_only=True)
class MarkedShape:
    """A marked page's shape (`marked.shape_element`): it may be one emit cannot draw (a freeform,
    a line, no fill). JSON kind `shape`, with `mark`."""
    id: str
    role: ShapeRole
    bbox: Box
    shape: ShapeKind | ProducerShapeKind
    fill: Color | None
    """(nullable)"""
    outline: Outline | None
    """(nullable)"""
    flip: bool
    radius: float
    drawings: tuple[DrawingId, ...]
    spans: tuple[SpanId, ...]
    mark: str
    picture: tuple[ImageId, ...] | None
    opacity: float | None


@dataclass(frozen=True, kw_only=True)
class RenderedMarkedShape(MarkedShape):
    """A marked shape after render: one emit draws (`marked.drawn_natively`); the rest became pictures."""
    shape: ShapeKind
    fill: Color
    picture: None

# ------------------------------------------------------------------------------ tables


@dataclass(frozen=True, kw_only=True)
class ColumnHead:
    """A head set otherwise than its column's body: JSON `head` (its alignment), `body` (the body's extent)."""
    align: Align
    body: tuple[float, float]


@dataclass(frozen=True, kw_only=True)
class Column:
    x0: float
    x1: float
    align: Align
    head: ColumnHead | None
    centred: tuple[int, ...] | None


@dataclass(frozen=True, kw_only=True)
class Merge:
    row: int
    col: int
    rows: int
    cols: int
    align: Align


@dataclass(frozen=True, kw_only=True)
class Rule:
    row: int
    position: RulePosition
    color: Color
    weight: float
    y: float


@dataclass(frozen=True, kw_only=True)
class Border:
    row: int
    col: int
    position: Position
    color: Color
    weight: float
    y: float | None


@dataclass(frozen=True, kw_only=True)
class CellFill:
    row: int
    col: int
    color: Color


@dataclass(frozen=True, kw_only=True)
class TableElement:
    """A native table. JSON kind `table`, role `table`."""
    id: str
    bbox: Box
    cells: tuple[tuple[tuple[Run, ...], ...], ...]
    frame: Box
    size: float
    row_baselines: tuple[float, ...]
    row_heights: tuple[float, ...]
    columns: tuple[Column, ...]
    rules: tuple[Rule, ...]
    spans: tuple[SpanId, ...]
    bounds: tuple[float, ...]
    merges: tuple[Merge, ...]
    borders: tuple[Border, ...]
    fills: tuple[CellFill, ...] | None
    bands: tuple[tuple[int, float, float], ...] | None
    row_lines: tuple[int, ...] | None
    wrapped: tuple[tuple[int, int, tuple[int, ...]], ...] | None
    justified: tuple[tuple[int, int], ...] | None
    merge_x: tuple[tuple[float, float], ...] | None
    drawings: tuple[DrawingId, ...] | None
    mark: str | None

# ------------------------------------------------------------------------------ diagrams


@dataclass(frozen=True, kw_only=True)
class CardBox:
    paragraphs: tuple[Paragraph, ...]


@dataclass(frozen=True, kw_only=True)
class Node:
    bbox: Box
    shape: TemplateKind | None
    """(nullable) None: a free label."""
    fill: Color | None
    stroke: Color | None
    width: float | None
    paragraphs: tuple[tuple[Run, ...], ...]
    baselines: tuple[float, ...]
    label_w: float
    text: tuple[CardBox, ...] | None
    """(nullable) None: a plain label."""
    radius: float | None


@dataclass(frozen=True, kw_only=True)
class Elbow:
    """An elbow line's turn: JSON `via`, `bend`."""
    via: Point
    bend: Bend


@dataclass(frozen=True, kw_only=True)
class DiagramLine:
    from_: Point
    """JSON `from`."""
    to: Point
    arrow_from: Arrow | None
    arrow_to: Arrow | None
    stroke: Color
    width: float
    elbow: Elbow | None


@dataclass(frozen=True, kw_only=True)
class DiagramElement:
    """JSON kind `diagram`, role `figure`."""
    id: str
    bbox: Box
    nodes: tuple[Node, ...]
    lines: tuple[DiagramLine, ...]
    spans: tuple[SpanId, ...]

# ------------------------------------------------------------------------------ slides, decks


Element = Union[TextElement, ImageElement, ShapeElement, MarkedShape, TableElement, DiagramElement]
RenderedElement = Union[RenderedText, RenderedImage, FallbackImage, ShapeElement, RenderedMarkedShape, TableElement,
                        DiagramElement]


@dataclass(frozen=True, kw_only=True)
class Panel:
    bbox: Box
    fill: Color | None
    rounded: bool


@dataclass(frozen=True, kw_only=True)
class LeftInBackground:
    reason: str
    spans: tuple[SpanId, ...]
    bboxes: tuple[Box, ...]


@dataclass(frozen=True, kw_only=True)
class SlideStats:
    chars: int
    chars_native: int


@dataclass(frozen=True, kw_only=True)
class _SlideCommon:
    page: int
    frame: str
    label: str | None
    size: Point
    notes: str | None
    left_in_background: tuple[LeftInBackground, ...]
    theme_texts: tuple[ThemeText, ...]
    panels: tuple[Panel, ...]
    figure_regions: tuple[Box, ...]
    stats: SlideStats
    on_layout: tuple[SpanId, ...]
    title_page: bool
    marked: int | None


@dataclass(frozen=True, kw_only=True)
class Slide(_SlideCommon):
    elements: tuple[Element, ...]


@dataclass(frozen=True, kw_only=True)
class RenderedSlide(_SlideCommon):
    elements: tuple[RenderedElement, ...]
    background: str
    background_color: Color | None


@dataclass(frozen=True, kw_only=True)
class Source:
    pdf: str
    pages: int
    producer: str
    title: str


@dataclass(frozen=True, kw_only=True)
class DeckStats:
    chars: int
    chars_native: int
    native_share: float


@dataclass(frozen=True, kw_only=True)
class _DeckCommon:
    version: int
    source: Source
    body_size: float
    stats: DeckStats
    layout_texts: tuple[ThemeText, ...]


@dataclass(frozen=True, kw_only=True)
class Deck(_DeckCommon):
    """deck.json as classify writes it."""
    slides: tuple[Slide, ...]


@dataclass(frozen=True, kw_only=True)
class RenderedDeck(_DeckCommon):
    """deck.json after render: what emit uploads."""
    slides: tuple[RenderedSlide, ...]

# ------------------------------------------------------------------------------ reading


class IRError(ValueError):
    """A deck.json value that is not what its type says, naming where as `ir.problems` does."""


@dataclass(frozen=True, kw_only=True)
class At:
    """Where a value is: its slide and element (`where`) and the path inside them."""
    where: str
    path: str

    def key(self, k: str) -> "At":
        return At(where=self.where, path=f"{self.path}.{k}" if self.path else k)

    def item(self, i: int) -> "At":
        return At(where=self.where, path=f"{self.path}[{i}]")

    def fail(self, what: str) -> NoReturn:
        raise IRError(f"{self.where}: {self.path}: {what}" if self.path else f"{self.where}: {what}")


T = TypeVar("T")
L = TypeVar("L", bound=str)
Parse = Callable[[object, At], T]


def _show(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _wrong(v: object, at: At, expected: str) -> NoReturn:
    at.fail(f"is {_show(v)} ({type(v).__name__}), expected {expected}")


class Fields:
    """A JSON object being read as `name`: each key read is ticked off, and `close` refuses a key
    nobody read (as `ir.problems` does by default)."""

    def __init__(self, value: object, at: At, name: str) -> None:
        if not isinstance(value, dict):
            _wrong(value, at, name)
        self.at = at
        self.name = name
        self.values: dict[str, object] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                _wrong(k, at, "a string key")
            self.values[k] = v
        self.read: set[str] = set()

    def has(self, key: str) -> bool:
        return key in self.values

    def req(self, key: str, parse: Parse[T]) -> T:
        """A key every producer writes."""
        if key not in self.values:
            self.at.fail(f"lacks required key {key!r}")
        self.read.add(key)
        return parse(self.values[key], self.at.key(key))

    def nullable(self, key: str, parse: Parse[T]) -> T | None:
        """A key every producer writes, null when there is nothing to say."""
        if key not in self.values:
            self.at.fail(f"lacks required key {key!r}")
        self.read.add(key)
        v = self.values[key]
        return None if v is None else parse(v, self.at.key(key))

    def optional(self, key: str, parse: Parse[T]) -> T | None:
        """A key written only when it says something: absent is None, null is refused."""
        if key not in self.values:
            return None
        self.read.add(key)
        return parse(self.values[key], self.at.key(key))

    def flag(self, key: str) -> bool:
        """A key written only when true."""
        return self.optional(key, boolean) or False

    def const(self, key: str, value: str | None) -> None:
        """A key whose value the type says (a kind, a role, a theme line's null panel)."""
        if key not in self.values:
            self.at.fail(f"lacks required key {key!r}")
        self.read.add(key)
        if self.values[key] != value or (value is None) != (self.values[key] is None):
            self.at.key(key).fail(f"is {_show(self.values[key])}, expected {value!r}")

    def absent(self, key: str) -> None:
        """A key this type never has (a theme paragraph's `tab_x0`): refused when it is there."""
        if key in self.values:
            self.at.fail(f"has key {key!r}, which a {self.name} never has")

    def close(self) -> None:
        extra = sorted(set(self.values) - self.read)
        if extra:
            self.at.fail(f"has unknown key {extra[0]!r} (not in {self.name})")


def number(v: object, at: At) -> float:
    """A JSON number, kept as it came (an int stays an int: the hash tells 3 from 3.0)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _wrong(v, at, "float")
    return v


def integer(v: object, at: At) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        _wrong(v, at, "int")
    return v


def string(v: object, at: At) -> str:
    if not isinstance(v, str):
        _wrong(v, at, "str")
    return v


def boolean(v: object, at: At) -> bool:
    if not isinstance(v, bool):
        _wrong(v, at, "bool")
    return v


def color(v: object, at: At) -> Color:
    if not isinstance(v, str) or len(v) != 7 or v[0] != "#" or any(c not in "0123456789abcdef" for c in v[1:]):
        _wrong(v, at, "a colour '#rrggbb', lowercase")
    return Color(v)


def int_or_str(v: object, at: At) -> int | str:
    if isinstance(v, str):
        return v
    return integer(v, at)


def one_of(values: tuple[L, ...]) -> Parse[L]:
    """A parser for a closed set of strings (a Literal spelled out)."""
    def parse(v: object, at: At) -> L:
        for x in values:
            if v == x:
                return x
        at.fail(f"is {_show(v)}, expected one of {' | '.join(repr(x) for x in values)}")
    return parse


def rtl(v: object, at: At) -> Literal["rtl"]:
    if v != "rtl":
        at.fail(f"is {_show(v)}, expected 'rtl'")
    return "rtl"


def _items(v: object, at: At, what: str) -> list[object]:
    if not isinstance(v, list):
        _wrong(v, at, what)
    out: list[object] = list(v)
    return out


def tuple_of(parse: Parse[T]) -> Parse[tuple[T, ...]]:
    def parse_list(v: object, at: At) -> tuple[T, ...]:
        return tuple(parse(x, at.item(i)) for i, x in enumerate(_items(v, at, "a list")))
    return parse_list


def _fixed(v: object, at: At, n: int, what: str) -> list[object]:
    items = _items(v, at, what)
    if len(items) != n:
        at.fail(f"has {len(items)} items, expected {n}")
    return items


def box(v: object, at: At) -> Box:
    a, b, c, d = _fixed(v, at, 4, "Box")
    return (number(a, at.item(0)), number(b, at.item(1)), number(c, at.item(2)), number(d, at.item(3)))


def point(v: object, at: At) -> Point:
    a, b = _fixed(v, at, 2, "Point")
    return (number(a, at.item(0)), number(b, at.item(1)))


def pair(v: object, at: At) -> tuple[float, float]:
    a, b = _fixed(v, at, 2, "a pair of numbers")
    return (number(a, at.item(0)), number(b, at.item(1)))


def int_pair(v: object, at: At) -> tuple[int, int]:
    a, b = _fixed(v, at, 2, "a pair of ints")
    return (integer(a, at.item(0)), integer(b, at.item(1)))


def before_word(v: object, at: At) -> BeforeWord:
    w, font, family, bold, italic, text, x0 = _fixed(v, at, 7, "BeforeWord")
    return (number(w, at.item(0)), string(font, at.item(1)), one_of(FAMILIES)(family, at.item(2)),
            boolean(bold, at.item(3)), boolean(italic, at.item(4)), string(text, at.item(5)), number(x0, at.item(6)))


def theme_key(v: object, at: At) -> tuple[str, int, int, str]:
    text, x0, baseline, colours = _fixed(v, at, 4, "a theme line's key")
    return (string(text, at.item(0)), integer(x0, at.item(1)), integer(baseline, at.item(2)), string(colours, at.item(3)))


def band(v: object, at: At) -> tuple[int, float, float]:
    row, top, bottom = _fixed(v, at, 3, "a band [row, top, bottom]")
    return (integer(row, at.item(0)), number(top, at.item(1)), number(bottom, at.item(2)))


def wrapped_cell(v: object, at: At) -> tuple[int, int, tuple[int, ...]]:
    row, col, starts = _fixed(v, at, 3, "a wrapped cell [row, col, starts]")
    return (integer(row, at.item(0)), integer(col, at.item(1)), tuple_of(integer)(starts, at.item(2)))

# ------------------------------------------------------------------------------ reading: text


def line(v: object, at: At) -> Line:
    f = Fields(v, at, "Line")
    out = Line(baseline=f.req("baseline", number), x0=f.req("x0", number), x1=f.req("x1", number))
    f.close()
    return out


def run(v: object, at: At) -> AnyRun:
    """A run, or a hole run (one with `hole`)."""
    f = Fields(v, at, "Run")
    text, font, family = f.req("text", string), f.req("font", string), f.req("family", one_of(FAMILIES))
    size, bold, italic = f.req("size", number), f.req("bold", boolean), f.req("italic", boolean)
    smallcaps, col = f.req("smallcaps", boolean), f.req("color", color)
    link, script = f.nullable("link", string), f.nullable("script", one_of(SCRIPTS))
    underline, highlight = f.req("underline", boolean), f.nullable("highlight", color)
    out: AnyRun
    if f.has("hole"):
        out = HoleRun(text=text, font=font, family=family, size=size, bold=bold, italic=italic, smallcaps=smallcaps,
                      color=col, link=link, script=script, underline=underline, highlight=highlight,
                      hole=f.req("hole", number), hole_x0=f.req("hole_x0", number),
                      before=f.req("before", tuple_of(before_word)), next_x0=f.nullable("next_x0", number))
    else:
        out = Run(text=text, font=font, family=family, size=size, bold=bold, italic=italic, smallcaps=smallcaps,
                  color=col, link=link, script=script, underline=underline, highlight=highlight,
                  strike=f.optional("strike", boolean))
    f.close()
    return out


def plain_run(v: object, at: At) -> Run:
    """A run of a table cell or a diagram label: never a hole."""
    r = run(v, at)
    if isinstance(r, HoleRun):
        at.fail("is a hole run, which a table cell or a diagram label never holds")
    return r


def label(v: object, at: At) -> Label:
    f = Fields(v, at, "Label")
    out = Label(x0=f.req("x0", number), baseline=f.req("baseline", number), font=f.req("font", string),
                family=f.req("family", one_of(FAMILIES)), size=f.req("size", number), bold=f.req("bold", boolean),
                italic=f.req("italic", boolean), color=f.req("color", color))
    f.close()
    return out


def _glyph_ink(f: Fields) -> GlyphInk | None:
    ink, fill = f.optional("ink", box), f.optional("fill", number)
    if (ink is None) != (fill is None):
        f.at.fail("has one of 'ink' and 'fill' without the other")
    return None if ink is None or fill is None else GlyphInk(box=ink, fill=fill)


def _bullet(v: object, at: At, stage: Stage) -> Bullet | RenderedBullet:
    f = Fields(v, at, "Bullet")
    kind = f.req("kind", one_of(BULLET_KINDS))
    text, bbox = f.req("text", string), f.req("bbox", box)
    out: Bullet | RenderedBullet
    match kind:
        case "glyph":
            if f.has("drawn"):
                f.req("drawn", _true)
                out = DrawnBullet(text=text, bbox=bbox) if stage == "classified" else \
                    RenderedDrawnBullet(text=text, bbox=bbox, ink=_glyph_ink(f))
            else:
                col, lab = f.req("color", color), f.req("label", label)
                out = GlyphBullet(text=text, bbox=bbox, color=col, label=lab) if stage == "classified" else \
                    RenderedGlyphBullet(text=text, bbox=bbox, color=col, label=lab, ink=_glyph_ink(f))
        case "number":
            out = NumberBullet(text=text, bbox=bbox, color=f.req("color", color), label=f.req("label", label),
                               patch=f.flag("patch"))
        case "image":
            image, lab2 = f.req("image", string), f.nullable("label", label)
            out = ImageBullet(text=text, bbox=bbox, image=image, label=lab2) if stage == "classified" else \
                RenderedImageBullet(text=text, bbox=bbox, image=image, label=lab2, color=f.nullable("color", color))
        case "shape":
            out = ShapeBullet(text=text, bbox=bbox, shape=f.req("shape", one_of(BULLET_SHAPES)),
                              color=f.req("color", color), patch=f.req("patch", boolean))
        case _:
            assert_never(kind)
    f.close()
    return out


def _true(v: object, at: At) -> bool:
    if v is not True:
        _wrong(v, at, "true")
    return True


def bullet(v: object, at: At) -> Bullet:
    b = _bullet(v, at, "classified")
    # (the classified parse builds only classified bullets; this narrows what `_bullet` says)
    if isinstance(b, (RenderedGlyphBullet, RenderedDrawnBullet, RenderedImageBullet)):
        at.fail("is a rendered bullet in a classified deck")
    return b


def rendered_bullet(v: object, at: At) -> RenderedBullet:
    b = _bullet(v, at, "rendered")
    match b:
        case RenderedGlyphBullet() | RenderedDrawnBullet() | NumberBullet() | RenderedImageBullet() | ShapeBullet():
            return b
        case GlyphBullet() | DrawnBullet() | ImageBullet():
            at.fail("is a classified bullet in a rendered deck")
        case _:
            assert_never(b)


def _paragraph_fields(f: Fields, theme: bool) -> tuple[Align, int, float, float, tuple[Line, ...], tuple[AnyRun, ...],
                                                       float | None, float | None, Literal["rtl"] | None, bool,
                                                       tuple[int, ...] | None]:
    if theme:
        f.absent("tab_x0")
        f.absent("wrap_limit")
        tab_x0, wrap_limit = None, None
    else:
        tab_x0, wrap_limit = f.nullable("tab_x0", number), f.nullable("wrap_limit", number)
    return (f.req("align", one_of(ALIGNS)), f.req("level", integer), f.req("size", number), f.req("text_x0", number),
            f.req("lines", tuple_of(line)), f.req("runs", tuple_of(run)), tab_x0, wrap_limit,
            f.optional("direction", rtl), f.flag("justified"), f.optional("line_starts", tuple_of(integer)))


def paragraph(v: object, at: At, theme: bool) -> Paragraph:
    """A paragraph; `theme`: of theme text, which writes no `tab_x0` or `wrap_limit`."""
    f = Fields(v, at, "theme Paragraph" if theme else "Paragraph")
    b = f.nullable("bullet", bullet)
    align, level, size, text_x0, lines, runs, tab_x0, wrap_limit, direction, justified, starts = \
        _paragraph_fields(f, theme)
    f.close()
    return Paragraph(align=align, level=level, bullet=b, size=size, text_x0=text_x0, lines=lines, runs=runs,
                     tab_x0=tab_x0, wrap_limit=wrap_limit, direction=direction, justified=justified,
                     line_starts=starts)


def rendered_paragraph(v: object, at: At, theme: bool) -> RenderedParagraph:
    f = Fields(v, at, "theme Paragraph" if theme else "Paragraph")
    b = f.nullable("bullet", rendered_bullet)
    align, level, size, text_x0, lines, runs, tab_x0, wrap_limit, direction, justified, starts = \
        _paragraph_fields(f, theme)
    f.close()
    return RenderedParagraph(align=align, level=level, bullet=b, size=size, text_x0=text_x0, lines=lines, runs=runs,
                             tab_x0=tab_x0, wrap_limit=wrap_limit, direction=direction, justified=justified,
                             line_starts=starts)


def _paragraphs(theme: bool) -> Parse[tuple[Paragraph, ...]]:
    return tuple_of(lambda v, at: paragraph(v, at, theme))


def _rendered_paragraphs(theme: bool) -> Parse[tuple[RenderedParagraph, ...]]:
    return tuple_of(lambda v, at: rendered_paragraph(v, at, theme))


def part(v: object, at: At) -> Part:
    f = Fields(v, at, "Part")
    out = Part(kind=f.req("kind", one_of(ELEMENT_KINDS)), bbox=f.req("bbox", box), mark=f.req("mark", string))
    f.close()
    return out


def theme_text(v: object, at: At) -> ThemeText:
    f = Fields(v, at, "ThemeText")
    f.const("kind", "text")
    f.const("role", "layout")
    f.const("panel", None)
    out = ThemeText(bbox=f.req("bbox", box), code=f.req("code", boolean), key=f.req("key", theme_key),
                    chars=f.req("chars", integer), paragraphs=f.req("paragraphs", _paragraphs(True)),
                    spans=f.req("spans", tuple_of(string)))
    f.close()
    return out


def _text(f: Fields, stage: Stage) -> TextElement | RenderedText:
    f.const("kind", "text")
    ident, role, bbox = f.req("id", string), f.req("role", one_of(TEXT_ROLES)), f.req("bbox", box)
    theme = role == "footer"
    spans, panel, code = f.req("spans", tuple_of(string)), f.nullable("panel", integer), f.req("code", boolean)
    strokes, rotation, mark = f.req("strokes", tuple_of(box)), f.optional("rotation", number), f.optional("mark", string)
    mark_box, hidden = f.optional("mark_box", box), f.optional("hidden_spans", tuple_of(string))
    parts, fill, composite = f.optional("parts", tuple_of(part)), f.optional("fill", color), f.flag("composite")
    if stage == "classified":
        return TextElement(id=ident, role=role, bbox=bbox, paragraphs=f.req("paragraphs", _paragraphs(theme)),
                           spans=spans, panel=panel, code=code, strokes=strokes, rotation=rotation, mark=mark,
                           mark_box=mark_box, hidden_spans=hidden, parts=parts, fill=fill, composite=composite)
    return RenderedText(id=ident, role=role, bbox=bbox, paragraphs=f.req("paragraphs", _rendered_paragraphs(theme)),
                        spans=spans, panel=panel, code=code, strokes=strokes, rotation=rotation, mark=mark,
                        mark_box=mark_box, hidden_spans=hidden, parts=parts, fill=fill, composite=composite)

# ------------------------------------------------------------------------------ reading: the other kinds


def mark(v: object, at: At) -> Mark:
    f = Fields(v, at, "Mark")
    out = Mark(x=f.req("x", number), hole_x0=f.req("hole_x0", number), pads=f.req("pads", number),
               font=f.req("font", string), family=f.req("family", one_of(FAMILIES)), size=f.req("size", number),
               bold=f.req("bold", boolean), italic=f.req("italic", boolean), before=f.req("before", tuple_of(before_word)))
    f.close()
    return out


def number_on_ball(v: object, at: At) -> Number:
    f = Fields(v, at, "Number")
    out = Number(x0=f.req("x0", number), baseline=f.req("baseline", number), font=f.req("font", string),
                 family=f.req("family", one_of(FAMILIES)), size=f.req("size", number), bold=f.req("bold", boolean),
                 italic=f.req("italic", boolean), color=f.req("color", color), text=f.req("text", string),
                 center=f.req("center", point), height=f.req("height", number))
    f.close()
    return out


def _image(f: Fields, stage: Stage) -> ImageElement | RenderedImage | FallbackImage:
    f.const("kind", "image")
    if f.values.get("role") == "fallback":
        f.const("role", "fallback")
        if stage == "classified":
            f.at.fail("is a fallback picture, which only a rendered deck holds")
        return FallbackImage(id=f.req("id", string), bbox=f.req("bbox", box), file=f.req("file", string))
    ident, role, bbox = f.req("id", string), f.req("role", one_of(IMAGE_ROLES)), f.req("bbox", box)
    spans, alt, anchor = f.req("spans", tuple_of(string)), f.optional("alt", string), f.optional("anchor", string)
    drawings, image = f.optional("drawings", tuple_of(string)), f.optional("image", string)
    marks, num = f.optional("marks", tuple_of(mark)), f.optional("number", number_on_ball)
    overlay, mk, mark_n = f.flag("overlay"), f.optional("mark", string), f.optional("mark_n", int_or_str)
    if stage == "classified":
        return ImageElement(id=ident, role=role, bbox=bbox, spans=spans, alt=alt, anchor=anchor, drawings=drawings,
                            image=image, marks=marks, number=num, overlay=overlay, mark=mk, mark_n=mark_n)
    return RenderedImage(id=ident, role=role, bbox=bbox, spans=spans, alt=alt, anchor=anchor, drawings=drawings,
                         image=image, marks=marks, number=num, overlay=overlay, mark=mk, mark_n=mark_n,
                         file=f.req("file", string), px=f.req("px", int_pair),
                         picture=f.optional("picture", one_of(PICTURE_ROUTES)))


def outline(v: object, at: At) -> Outline:
    f = Fields(v, at, "Outline")
    out = Outline(color=f.req("color", color), width=f.req("width", number))
    f.close()
    return out


def shadow(v: object, at: At) -> Shadow:
    f = Fields(v, at, "Shadow")
    out = Shadow(size=f.req("size", number), pieces=f.req("pieces", tuple_of(box)))
    f.close()
    return out


def _shape(f: Fields, stage: Stage) -> ShapeElement | MarkedShape | RenderedMarkedShape:
    f.const("kind", "shape")
    ident, role, bbox = f.req("id", string), f.req("role", one_of(SHAPE_ROLES)), f.req("bbox", box)
    flip, radius, spans = f.req("flip", boolean), f.req("radius", number), f.req("spans", tuple_of(string))
    opacity = f.optional("opacity", number)
    if not f.has("mark"):
        return ShapeElement(id=ident, role=role, bbox=bbox, shape=f.req("shape", one_of(TEMPLATE_KINDS)),
                            fill=f.req("fill", color), flip=flip, radius=radius, spans=spans,
                            drawing=f.req("drawing", string), outline=f.optional("outline", outline), opacity=opacity,
                            block=f.optional("block", integer), title_bar=f.optional("title_bar", box),
                            strips=f.optional("strips", tuple_of(box)), shadow=f.optional("shadow", shadow),
                            anchor=f.optional("anchor", string), tiles=f.optional("tiles", tuple_of(string)),
                            frame_drawings=f.optional("frame_drawings", tuple_of(string)))
    drawings, mk, line_outline = f.req("drawings", tuple_of(string)), f.req("mark", string), f.nullable("outline", outline)
    if stage == "rendered":
        if f.has("picture"):
            f.at.fail("is a marked shape with a picture fill, which render makes a picture")
        return RenderedMarkedShape(id=ident, role=role, bbox=bbox, shape=f.req("shape", one_of(TEMPLATE_KINDS)),
                                   fill=f.req("fill", color), outline=line_outline, flip=flip, radius=radius,
                                   drawings=drawings, spans=spans, mark=mk, picture=None, opacity=opacity)
    return MarkedShape(id=ident, role=role, bbox=bbox,
                       shape=f.req("shape", one_of(TEMPLATE_KINDS + PRODUCER_SHAPE_KINDS)),
                       fill=f.nullable("fill", color), outline=line_outline, flip=flip, radius=radius,
                       drawings=drawings, spans=spans, mark=mk, picture=f.optional("picture", tuple_of(string)),
                       opacity=opacity)


def column(v: object, at: At) -> Column:
    f = Fields(v, at, "Column")
    head_align, body = f.optional("head", one_of(ALIGNS)), f.optional("body", pair)
    if (head_align is None) != (body is None):
        at.fail("has one of 'head' and 'body' without the other")
    out = Column(x0=f.req("x0", number), x1=f.req("x1", number), align=f.req("align", one_of(ALIGNS)),
                 head=None if head_align is None or body is None else ColumnHead(align=head_align, body=body),
                 centred=f.optional("centred", tuple_of(integer)))
    f.close()
    return out


def merge(v: object, at: At) -> Merge:
    f = Fields(v, at, "Merge")
    out = Merge(row=f.req("row", integer), col=f.req("col", integer), rows=f.req("rows", integer),
                cols=f.req("cols", integer), align=f.req("align", one_of(ALIGNS)))
    f.close()
    return out


def rule(v: object, at: At) -> Rule:
    f = Fields(v, at, "Rule")
    out = Rule(row=f.req("row", integer), position=f.req("position", one_of(RULE_POSITIONS)),
               color=f.req("color", color), weight=f.req("weight", number), y=f.req("y", number))
    f.close()
    return out


def border(v: object, at: At) -> Border:
    f = Fields(v, at, "Border")
    out = Border(row=f.req("row", integer), col=f.req("col", integer), position=f.req("position", one_of(POSITIONS)),
                 color=f.req("color", color), weight=f.req("weight", number), y=f.optional("y", number))
    f.close()
    return out


def cell_fill(v: object, at: At) -> CellFill:
    f = Fields(v, at, "CellFill")
    out = CellFill(row=f.req("row", integer), col=f.req("col", integer), color=f.req("color", color))
    f.close()
    return out


def _table(f: Fields) -> TableElement:
    f.const("kind", "table")
    f.const("role", "table")
    return TableElement(
        id=f.req("id", string), bbox=f.req("bbox", box), cells=f.req("cells", tuple_of(tuple_of(tuple_of(plain_run)))),
        frame=f.req("frame", box), size=f.req("size", number), row_baselines=f.req("row_baselines", tuple_of(number)),
        row_heights=f.req("row_heights", tuple_of(number)), columns=f.req("columns", tuple_of(column)),
        rules=f.req("rules", tuple_of(rule)), spans=f.req("spans", tuple_of(string)),
        bounds=f.req("bounds", tuple_of(number)), merges=f.req("merges", tuple_of(merge)),
        borders=f.req("borders", tuple_of(border)), fills=f.optional("fills", tuple_of(cell_fill)),
        bands=f.optional("bands", tuple_of(band)), row_lines=f.optional("row_lines", tuple_of(integer)),
        wrapped=f.optional("wrapped", tuple_of(wrapped_cell)), justified=f.optional("justified", tuple_of(int_pair)),
        merge_x=f.optional("merge_x", tuple_of(pair)), drawings=f.optional("drawings", tuple_of(string)),
        mark=f.optional("mark", string))


def card_box(v: object, at: At) -> CardBox:
    f = Fields(v, at, "CardBox")
    out = CardBox(paragraphs=f.req("paragraphs", _paragraphs(False)))
    f.close()
    return out


def node(v: object, at: At) -> Node:
    f = Fields(v, at, "Node")
    out = Node(bbox=f.req("bbox", box), shape=f.nullable("shape", one_of(TEMPLATE_KINDS)),
               fill=f.nullable("fill", color), stroke=f.nullable("stroke", color), width=f.nullable("width", number),
               paragraphs=f.req("paragraphs", tuple_of(tuple_of(plain_run))),
               baselines=f.req("baselines", tuple_of(number)), label_w=f.req("label_w", number),
               text=f.nullable("text", tuple_of(card_box)), radius=f.optional("radius", number))
    f.close()
    return out


def diagram_line(v: object, at: At) -> DiagramLine:
    f = Fields(v, at, "DiagramLine")
    via, bend = f.optional("via", point), f.optional("bend", one_of(BENDS))
    if (via is None) != (bend is None):
        at.fail("has one of 'via' and 'bend' without the other")
    out = DiagramLine(from_=f.req("from", point), to=f.req("to", point),
                      arrow_from=f.nullable("arrow_from", one_of(ARROWS)), arrow_to=f.nullable("arrow_to", one_of(ARROWS)),
                      stroke=f.req("stroke", color), width=f.req("width", number),
                      elbow=None if via is None or bend is None else Elbow(via=via, bend=bend))
    f.close()
    return out


def _diagram(f: Fields) -> DiagramElement:
    f.const("kind", "diagram")
    f.const("role", "figure")
    return DiagramElement(id=f.req("id", string), bbox=f.req("bbox", box), nodes=f.req("nodes", tuple_of(node)),
                          lines=f.req("lines", tuple_of(diagram_line)), spans=f.req("spans", tuple_of(string)))


def _element_fields(v: object, where: str) -> tuple[Fields, str]:
    if not isinstance(v, dict):
        _wrong(v, At(where=where, path=""), "an element")
    ident = v.get("id")
    at = At(where=f"{where}, element {ident if isinstance(ident, str) and ident else '?'}", path="")
    kind = v.get("kind")
    return Fields(v, at, f"{kind} element"), kind if isinstance(kind, str) else ""


def parse_element(v: object, where: str) -> Element:
    """A classified element; `where` names its slide in errors ("slide page 3")."""
    f, kind = _element_fields(v, where)
    out: Element
    if kind == "text":
        t = _text(f, "classified")
        if isinstance(t, RenderedText):
            f.at.fail("is a rendered text element")
        out = t
    elif kind == "image":
        im = _image(f, "classified")
        if isinstance(im, (RenderedImage, FallbackImage)):
            f.at.fail("is a rendered picture")
        out = im
    elif kind == "shape":
        sh = _shape(f, "classified")
        if isinstance(sh, RenderedMarkedShape):
            f.at.fail("is a rendered shape")
        out = sh
    elif kind == "table":
        out = _table(f)
    elif kind == "diagram":
        out = _diagram(f)
    else:
        f.at.key("kind").fail(f"is {_show(kind)}, expected one of {', '.join(repr(k) for k in ELEMENT_KINDS)}")
    f.close()
    return out


def parse_rendered_element(v: object, where: str) -> RenderedElement:
    """A rendered element (what a sync base's `ir` holds: emit's plan of one)."""
    f, kind = _element_fields(v, where)
    out: RenderedElement
    if kind == "text":
        t = _text(f, "rendered")
        if not isinstance(t, RenderedText):
            f.at.fail("is a classified text element")
        out = t
    elif kind == "image":
        im = _image(f, "rendered")
        if not isinstance(im, (RenderedImage, FallbackImage)):
            f.at.fail("is a picture render has not made")
        out = im
    elif kind == "shape":
        sh = _shape(f, "rendered")
        if not isinstance(sh, (ShapeElement, RenderedMarkedShape)):
            f.at.fail("is a shape emit cannot draw")
        out = sh
    elif kind == "table":
        out = _table(f)
    elif kind == "diagram":
        out = _diagram(f)
    else:
        f.at.key("kind").fail(f"is {_show(kind)}, expected one of {', '.join(repr(k) for k in ELEMENT_KINDS)}")
    f.close()
    return out

# ------------------------------------------------------------------------------ reading: slides, decks


def panel(v: object, at: At) -> Panel:
    f = Fields(v, at, "Panel")
    out = Panel(bbox=f.req("bbox", box), fill=f.nullable("fill", color), rounded=f.req("rounded", boolean))
    f.close()
    return out


def left_in_background(v: object, at: At) -> LeftInBackground:
    f = Fields(v, at, "LeftInBackground")
    out = LeftInBackground(reason=f.req("reason", string), spans=f.req("spans", tuple_of(string)),
                           bboxes=f.req("bboxes", tuple_of(box)))
    f.close()
    return out


def slide_stats(v: object, at: At) -> SlideStats:
    f = Fields(v, at, "SlideStats")
    out = SlideStats(chars=f.req("chars", integer), chars_native=f.req("chars_native", integer))
    f.close()
    return out


def _slide_fields(v: object) -> tuple[Fields, str]:
    if not isinstance(v, dict):
        _wrong(v, At(where="deck", path="slides[]"), "a slide")
    page = v.get("page")
    where = f"slide page {page if isinstance(page, int) else '?'}"
    return Fields(v, At(where=where, path=""), "Slide"), where


def _elements(v: object, at: At, where: str, parse: Callable[[object, str], T]) -> tuple[T, ...]:
    return tuple(parse(x, where) for x in _items(v, at, "a list of elements"))


def parse_slide(v: object) -> Slide:
    f, where = _slide_fields(v)
    elements = _elements(f.req("elements", lambda x, at: x), f.at.key("elements"), where, parse_element)
    out = Slide(elements=elements, page=f.req("page", integer), frame=f.req("frame", string),
                label=f.nullable("label", string), size=f.req("size", point), notes=f.nullable("notes", string),
                left_in_background=f.req("left_in_background", tuple_of(left_in_background)),
                theme_texts=f.req("theme_texts", tuple_of(theme_text)), panels=f.req("panels", tuple_of(panel)),
                figure_regions=f.req("figure_regions", tuple_of(box)), stats=f.req("stats", slide_stats),
                on_layout=f.req("on_layout", tuple_of(string)), title_page=f.flag("title_page"),
                marked=f.optional("marked", integer))
    f.close()
    return out


def parse_rendered_slide(v: object) -> RenderedSlide:
    f, where = _slide_fields(v)
    elements = _elements(f.req("elements", lambda x, at: x), f.at.key("elements"), where, parse_rendered_element)
    out = RenderedSlide(elements=elements, page=f.req("page", integer), frame=f.req("frame", string),
                        label=f.nullable("label", string), size=f.req("size", point), notes=f.nullable("notes", string),
                        left_in_background=f.req("left_in_background", tuple_of(left_in_background)),
                        theme_texts=f.req("theme_texts", tuple_of(theme_text)), panels=f.req("panels", tuple_of(panel)),
                        figure_regions=f.req("figure_regions", tuple_of(box)), stats=f.req("stats", slide_stats),
                        on_layout=f.req("on_layout", tuple_of(string)), title_page=f.flag("title_page"),
                        marked=f.optional("marked", integer), background=f.req("background", string),
                        background_color=f.nullable("background_color", color))
    f.close()
    return out


def source(v: object, at: At) -> Source:
    f = Fields(v, at, "Source")
    out = Source(pdf=f.req("pdf", string), pages=f.req("pages", integer), producer=f.req("producer", string),
                 title=f.req("title", string))
    f.close()
    return out


def deck_stats(v: object, at: At) -> DeckStats:
    f = Fields(v, at, "DeckStats")
    out = DeckStats(chars=f.req("chars", integer), chars_native=f.req("chars_native", integer),
                    native_share=f.req("native_share", number))
    f.close()
    return out


def parse_classified(v: object) -> Deck:
    """deck.json as classify writes it; raises IRError naming the first thing that is not."""
    f = Fields(v, At(where="deck", path=""), "Deck")
    out = Deck(version=f.req("version", integer), source=f.req("source", source), body_size=f.req("body_size", number),
               stats=f.req("stats", deck_stats), layout_texts=f.req("layout_texts", tuple_of(theme_text)),
               slides=tuple(parse_slide(s) for s in f.req("slides", lambda x, at: _items(x, at, "a list of slides"))))
    f.close()
    return out


def parse_rendered(v: object) -> RenderedDeck:
    """deck.json after render: what emit uploads."""
    f = Fields(v, At(where="deck", path=""), "Deck")
    out = RenderedDeck(version=f.req("version", integer), source=f.req("source", source),
                       body_size=f.req("body_size", number), stats=f.req("stats", deck_stats),
                       layout_texts=f.req("layout_texts", tuple_of(theme_text)),
                       slides=tuple(parse_rendered_slide(s)
                                    for s in f.req("slides", lambda x, at: _items(x, at, "a list of slides"))))
    f.close()
    return out


@overload
def parse_deck(v: object, stage: Literal["classified"]) -> Deck: ...
@overload
def parse_deck(v: object, stage: Literal["rendered"]) -> RenderedDeck: ...
def parse_deck(v: object, stage: Stage) -> Deck | RenderedDeck:
    """deck.json at `stage`, typed; raises IRError ("slide page 3, element p3t1: paragraphs[0]...")."""
    match stage:
        case "classified":
            return parse_classified(v)
        case "rendered":
            return parse_rendered(v)
        case _:
            assert_never(stage)

# ------------------------------------------------------------------------------ writing


def _box(b: Box) -> Json:
    return [b[0], b[1], b[2], b[3]]


def _point(p: tuple[float, float]) -> Json:
    return [p[0], p[1]]


def _strs(xs: tuple[str, ...]) -> Json:
    return list(xs)


def _nums(xs: tuple[float, ...]) -> Json:
    return list(xs)


def _ints(xs: tuple[int, ...]) -> Json:
    return list(xs)


def _boxes(xs: tuple[Box, ...]) -> Json:
    return [_box(b) for b in xs]


def _before(words: tuple[BeforeWord, ...]) -> Json:
    return [[w[0], w[1], w[2], w[3], w[4], w[5], w[6]] for w in words]


def _put(out: JsonObject, key: str, value: Json) -> None:
    """A None-absent key: written only when there is something to say."""
    if value is not None:
        out[key] = value


def _flag(out: JsonObject, key: str, value: bool) -> None:
    if value:
        out[key] = True


def _opt(value: T | None, write: Callable[[T], Json]) -> Json:
    return None if value is None else write(value)


def line_json(ln: Line) -> JsonObject:
    return {"baseline": ln.baseline, "x0": ln.x0, "x1": ln.x1}


def run_json(r: AnyRun) -> JsonObject:
    out: JsonObject = {"text": r.text, "font": r.font, "family": r.family, "size": r.size, "bold": r.bold,
                       "italic": r.italic, "smallcaps": r.smallcaps, "color": r.color, "link": r.link,
                       "script": r.script, "underline": r.underline}
    match r:
        case Run():
            _put(out, "strike", r.strike)
            out["highlight"] = r.highlight
        case HoleRun():
            out.update(highlight=r.highlight, hole=r.hole, hole_x0=r.hole_x0, before=_before(r.before),
                       next_x0=r.next_x0)
        case _:
            assert_never(r)
    return out


def label_json(lab: Label) -> JsonObject:
    return {"x0": lab.x0, "baseline": lab.baseline, "font": lab.font, "family": lab.family, "size": lab.size,
            "bold": lab.bold, "italic": lab.italic, "color": lab.color}


def _ink(out: JsonObject, ink: GlyphInk | None) -> None:
    if ink is not None:
        out["ink"], out["fill"] = _box(ink.box), ink.fill


def bullet_json(b: Bullet | RenderedBullet) -> JsonObject:
    match b:
        case GlyphBullet():
            out: JsonObject = {"kind": "glyph", "text": b.text, "color": b.color, "bbox": _box(b.bbox),
                               "label": label_json(b.label)}
            if isinstance(b, RenderedGlyphBullet):
                _ink(out, b.ink)
            return out
        case DrawnBullet():
            out = {"kind": "glyph", "text": b.text, "bbox": _box(b.bbox), "drawn": True}
            if isinstance(b, RenderedDrawnBullet):
                _ink(out, b.ink)
            return out
        case NumberBullet():
            out = {"kind": "number", "text": b.text, "color": b.color, "bbox": _box(b.bbox),
                   "label": label_json(b.label)}
            _flag(out, "patch", b.patch)
            return out
        case ImageBullet():
            out = {"kind": "image", "text": b.text, "bbox": _box(b.bbox), "image": b.image,
                   "label": _opt(b.label, label_json)}
            if isinstance(b, RenderedImageBullet):
                out["color"] = b.color
            return out
        case ShapeBullet():
            return {"kind": "shape", "text": b.text, "bbox": _box(b.bbox), "patch": b.patch, "shape": b.shape,
                    "color": b.color}
        case _:
            assert_never(b)


def paragraph_json(p: Paragraph, theme: bool) -> JsonObject:
    """`theme`: a paragraph of theme text, which writes no `tab_x0` or `wrap_limit` (and has none)."""
    out: JsonObject = {"align": p.align, "level": p.level, "bullet": _opt(p.bullet, bullet_json), "size": p.size,
                       "text_x0": p.text_x0}
    if theme:
        if p.tab_x0 is not None or p.wrap_limit is not None:
            raise IRError("a paragraph of theme text has a tab_x0 or a wrap_limit, which its JSON cannot say")
    else:
        out.update(tab_x0=p.tab_x0, wrap_limit=p.wrap_limit)
    out["lines"] = [line_json(ln) for ln in p.lines]
    out["runs"] = [run_json(r) for r in p.runs]
    _put(out, "direction", p.direction)
    _flag(out, "justified", p.justified)
    _put(out, "line_starts", _opt(p.line_starts, _ints))
    return out


def _paragraphs_json(ps: tuple[Paragraph, ...], theme: bool) -> Json:
    return [paragraph_json(p, theme) for p in ps]


def part_json(p: Part) -> JsonObject:
    return {"kind": p.kind, "bbox": _box(p.bbox), "mark": p.mark}


def theme_text_json(t: ThemeText) -> JsonObject:
    return {"kind": "text", "role": "layout", "bbox": _box(t.bbox), "panel": None, "code": t.code,
            "key": [t.key[0], t.key[1], t.key[2], t.key[3]], "chars": t.chars,
            "paragraphs": _paragraphs_json(t.paragraphs, True), "spans": _strs(t.spans)}


def text_json(el: TextElement) -> JsonObject:
    out: JsonObject = {"id": el.id, "kind": "text", "role": el.role, "bbox": _box(el.bbox), "panel": el.panel,
                       "paragraphs": _paragraphs_json(el.paragraphs, el.role == "footer"), "code": el.code,
                       "spans": _strs(el.spans), "strokes": _boxes(el.strokes)}
    _put(out, "rotation", el.rotation)
    _put(out, "mark", el.mark)
    _put(out, "mark_box", _opt(el.mark_box, _box))
    _put(out, "hidden_spans", _opt(el.hidden_spans, _strs))
    _put(out, "parts", _opt(el.parts, lambda ps: [part_json(p) for p in ps]))
    _put(out, "fill", el.fill)
    _flag(out, "composite", el.composite)
    return out


def mark_json(m: Mark) -> JsonObject:
    return {"x": m.x, "hole_x0": m.hole_x0, "pads": m.pads, "font": m.font, "family": m.family, "size": m.size,
            "bold": m.bold, "italic": m.italic, "before": _before(m.before)}


def number_json(n: Number) -> JsonObject:
    out = label_json(n)
    out.update(text=n.text, center=_point(n.center), height=n.height)
    return out


def image_json(el: ImageElement | FallbackImage) -> JsonObject:
    if isinstance(el, FallbackImage):
        return {"kind": "image", "id": el.id, "role": "fallback", "bbox": _box(el.bbox), "file": el.file}
    out: JsonObject = {"id": el.id, "kind": "image", "role": el.role, "bbox": _box(el.bbox), "spans": _strs(el.spans)}
    _put(out, "alt", el.alt)
    _put(out, "anchor", el.anchor)
    _put(out, "drawings", _opt(el.drawings, _strs))
    _put(out, "image", el.image)
    _put(out, "marks", _opt(el.marks, lambda ms: [mark_json(m) for m in ms]))
    _put(out, "number", _opt(el.number, number_json))
    _flag(out, "overlay", el.overlay)
    _put(out, "mark", el.mark)
    _put(out, "mark_n", el.mark_n)
    if isinstance(el, RenderedImage):
        out.update(file=el.file, px=[el.px[0], el.px[1]])
        _put(out, "picture", el.picture)
    return out


def outline_json(o: Outline) -> JsonObject:
    return {"color": o.color, "width": o.width}


def shape_json(el: ShapeElement | MarkedShape) -> JsonObject:
    out: JsonObject = {"id": el.id, "kind": "shape", "role": el.role, "bbox": _box(el.bbox), "fill": el.fill,
                       "shape": el.shape, "flip": el.flip, "radius": el.radius, "spans": _strs(el.spans)}
    match el:
        case ShapeElement():
            out["drawing"] = el.drawing
            _put(out, "outline", _opt(el.outline, outline_json))
            _put(out, "opacity", el.opacity)
            _put(out, "block", el.block)
            _put(out, "title_bar", _opt(el.title_bar, _box))
            _put(out, "strips", _opt(el.strips, _boxes))
            _put(out, "shadow", _opt(el.shadow, lambda s: {"size": s.size, "pieces": _boxes(s.pieces)}))
            _put(out, "anchor", el.anchor)
            _put(out, "tiles", _opt(el.tiles, _strs))
            _put(out, "frame_drawings", _opt(el.frame_drawings, _strs))
        case MarkedShape():
            out.update(outline=_opt(el.outline, outline_json), drawings=_strs(el.drawings), mark=el.mark)
            _put(out, "picture", _opt(el.picture, _strs))
            _put(out, "opacity", el.opacity)
        case _:
            assert_never(el)
    return out


def column_json(c: Column) -> JsonObject:
    out: JsonObject = {"x0": c.x0, "x1": c.x1, "align": c.align}
    if c.head is not None:
        out.update(head=c.head.align, body=_point(c.head.body))
    _put(out, "centred", _opt(c.centred, _ints))
    return out


def table_json(el: TableElement) -> JsonObject:
    out: JsonObject = {
        "id": el.id, "kind": "table", "role": "table", "bbox": _box(el.bbox),
        "cells": [[[run_json(r) for r in cell] for cell in row] for row in el.cells], "frame": _box(el.frame),
        "size": el.size, "row_baselines": _nums(el.row_baselines), "row_heights": _nums(el.row_heights),
        "columns": [column_json(c) for c in el.columns],
        "rules": [{"row": r.row, "position": r.position, "color": r.color, "weight": r.weight, "y": r.y}
                  for r in el.rules],
        "spans": _strs(el.spans), "bounds": _nums(el.bounds),
        "merges": [{"row": m.row, "col": m.col, "rows": m.rows, "cols": m.cols, "align": m.align} for m in el.merges],
        "borders": [_border_json(b) for b in el.borders]}
    _put(out, "fills", _opt(el.fills, lambda fs: [{"row": f.row, "col": f.col, "color": f.color} for f in fs]))
    _put(out, "bands", _opt(el.bands, lambda bs: [[b[0], b[1], b[2]] for b in bs]))
    _put(out, "row_lines", _opt(el.row_lines, _ints))
    _put(out, "wrapped", _opt(el.wrapped, lambda ws: [[w[0], w[1], _ints(w[2])] for w in ws]))
    _put(out, "justified", _opt(el.justified, lambda js: [[j[0], j[1]] for j in js]))
    _put(out, "merge_x", _opt(el.merge_x, lambda ms: [_point(m) for m in ms]))
    _put(out, "drawings", _opt(el.drawings, _strs))
    _put(out, "mark", el.mark)
    return out


def _border_json(b: Border) -> JsonObject:
    out: JsonObject = {"row": b.row, "col": b.col, "position": b.position, "color": b.color, "weight": b.weight}
    _put(out, "y", b.y)
    return out


def node_json(n: Node) -> JsonObject:
    out: JsonObject = {
        "bbox": _box(n.bbox), "shape": n.shape, "fill": n.fill, "stroke": n.stroke, "width": n.width,
        "paragraphs": [[run_json(r) for r in ln] for ln in n.paragraphs], "baselines": _nums(n.baselines),
        "label_w": n.label_w,
        "text": _opt(n.text, lambda cards: [{"paragraphs": _paragraphs_json(c.paragraphs, False)} for c in cards])}
    _put(out, "radius", n.radius)
    return out


def diagram_line_json(ln: DiagramLine) -> JsonObject:
    out: JsonObject = {"from": _point(ln.from_), "to": _point(ln.to), "arrow_from": ln.arrow_from,
                       "arrow_to": ln.arrow_to, "stroke": ln.stroke, "width": ln.width}
    if ln.elbow is not None:
        out.update(via=_point(ln.elbow.via), bend=ln.elbow.bend)
    return out


def diagram_json(el: DiagramElement) -> JsonObject:
    return {"id": el.id, "kind": "diagram", "role": "figure", "bbox": _box(el.bbox),
            "nodes": [node_json(n) for n in el.nodes], "lines": [diagram_line_json(ln) for ln in el.lines],
            "spans": _strs(el.spans)}


def element_json(el: Element | RenderedElement) -> JsonObject:
    """An element as deck.json (and a base's `ir`) writes it."""
    match el:
        case TextElement():
            return text_json(el)
        case ImageElement() | FallbackImage():
            return image_json(el)
        case ShapeElement() | MarkedShape():
            return shape_json(el)
        case TableElement():
            return table_json(el)
        case DiagramElement():
            return diagram_json(el)
        case _:
            assert_never(el)


def slide_json(s: Slide | RenderedSlide) -> JsonObject:
    out: JsonObject = {
        "page": s.page, "frame": s.frame, "label": s.label, "size": _point(s.size), "notes": s.notes,
        "elements": [element_json(e) for e in s.elements],
        "left_in_background": [{"reason": x.reason, "spans": _strs(x.spans), "bboxes": _boxes(x.bboxes)}
                               for x in s.left_in_background],
        "theme_texts": [theme_text_json(t) for t in s.theme_texts],
        "panels": [{"bbox": _box(p.bbox), "fill": p.fill, "rounded": p.rounded} for p in s.panels],
        "figure_regions": _boxes(s.figure_regions),
        "stats": {"chars": s.stats.chars, "chars_native": s.stats.chars_native}, "on_layout": _strs(s.on_layout)}
    _flag(out, "title_page", s.title_page)
    _put(out, "marked", s.marked)
    if isinstance(s, RenderedSlide):
        out.update(background=s.background, background_color=s.background_color)
    return out


def deck_json(deck: Deck | RenderedDeck) -> JsonObject:
    """deck.json as the producers write it (one canonicalisation: a false flag is left out)."""
    src, stats = deck.source, deck.stats
    return {"version": deck.version,
            "source": {"pdf": src.pdf, "pages": src.pages, "producer": src.producer, "title": src.title},
            "body_size": deck.body_size,
            "stats": {"chars": stats.chars, "chars_native": stats.chars_native, "native_share": stats.native_share},
            "layout_texts": [theme_text_json(t) for t in deck.layout_texts],
            "slides": [slide_json(s) for s in deck.slides]}


__all__ = ["Json", "JsonObject", "IRError", "Deck", "RenderedDeck", "Slide", "RenderedSlide", "Element",
           "RenderedElement", "parse_deck", "parse_classified", "parse_rendered", "parse_element",
           "parse_rendered_element", "deck_json", "element_json"]
