"""What emit reads of a text: its runs, bullets and paragraphs as emit sets them.

The IR's records (`ir_types`) say what the page has; these say what emit writes of it, with what
emit derives on the way: a run set among others (`in_sentence`), a hole's no-break spaces
(`hole_size`), a table cell's run (`cell`). None of that goes back into the IR, which sync diffs.
Emit's planners make them from the parsed IR (`set_run`, `set_bullet`, `set_paragraph`).

Callers that still hold dicts - classify's lines while it measures them, deck_ir's read of a deck,
theme_sync's layout texts, the tests - reach the same code through `run_of`, `bullet_of` and
`paragraph_of`. Those read each key as emit always read it: a key that is absent takes the value
emit's `.get` gave it, and a value of the wrong type is refused.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .ir import Align, Family, ProducerShapeKind, Script, ShapeKind
from .ir_types import (
    ALIGNS, BULLET_KINDS, FAMILIES, PRODUCER_SHAPE_KINDS, SCRIPTS, TEMPLATE_KINDS, AnyRun, Box, Bullet, BulletKind,
    Color, DrawnBullet, GlyphBullet, GlyphInk, HoleRun, ImageBullet, Line, MarkedShape, Number, NumberBullet, Outline,
    Paragraph, RenderedBullet, RenderedDrawnBullet, RenderedGlyphBullet, RenderedImageBullet, Run, ShapeBullet,
    ShapeElement, TextElement, ThemeText,
)
from .json_types import Json, JsonObject
from .typing_compat import assert_never

JsonMap = Mapping[str, Json]
"""A JSON object as the dict readers here take it: read-only, so a dict whose values are narrower
than `Json` (deck_ir's runs, a test's run of strings and numbers) is one too."""

BulletFace = Literal["disc", "circle", "square", "open_square", "triangle", "star", "diamond", "open_diamond"]
"""A bullet glyph Slides has a preset for (emit_metrics.BULLET_SHAPES): the IR's vector shapes and
the glyphs a character maps to (GLYPH_SHAPES)."""
BULLET_FACES: tuple[BulletFace, ...] = ("disc", "circle", "square", "open_square", "triangle", "star", "diamond",
                                        "open_diamond")


@dataclass(frozen=True, kw_only=True)
class SetRun:
    """A run as emit sets it."""
    text: str
    font: str
    family: Family
    size: float
    bold: bool
    italic: bool
    smallcaps: bool
    script: Script | None
    color: Color
    underline: bool
    strike: bool
    highlight: Color | None
    link: str | None
    hole: float | None
    """A formula hole's width (PDF pt) while it is still the IR's; None for words."""
    hole_size: float | None
    """The size of a hole's no-break spaces once it is set (`emit_text.hole_run`)."""
    cell: bool
    """A table cell's run: set at the table's size, never shaped (`FontMapper.shape_ratio`)."""
    in_sentence: bool
    """Shares its paragraph with other words (`emit_text.in_sentence`): sized like them."""


@dataclass(frozen=True, kw_only=True)
class SetBullet:
    """A list bullet as emit writes it: the IR's five bullet records and their stages, read once."""
    kind: BulletKind
    """The JSON kind: a marked page's drawn bullet is a glyph."""
    text: str
    bbox: Box
    color: Color | None
    """None where the page says none (a drawn bullet, a ball whose colour render could not read)."""
    label_size: float | None
    """The size of the font the label is drawn in; None where there is no label."""
    ink: GlyphInk | None
    """A glyph's ink as render measured it; None before render, or when it could not."""
    shape: BulletFace | None
    """A vector bullet's shape; None for the rest."""


@dataclass(frozen=True, kw_only=True)
class SetParagraph:
    """A paragraph as emit sets it: its runs are `SetRun`s."""
    runs: tuple[SetRun, ...]
    lines: tuple[Line, ...]
    size: float
    text_x0: float
    align: Align
    level: int
    bullet: SetBullet | None
    direction: Literal["rtl"] | None
    justified: bool
    wrap_limit: float | None
    tab_x0: float | None
    line_starts: tuple[int, ...] | None


@dataclass(frozen=True, kw_only=True)
class SetText:
    """What a text box's planner reads of a text element, a theme line or a diagram's card."""
    paragraphs: tuple[SetParagraph, ...]
    code: bool
    rotation: float | None
    """The turn of a text laid out in its own frame (classify.rotated_texts); None or 0 when upright."""


@dataclass(frozen=True, kw_only=True)
class Placeholder:
    """A layout placeholder a text is written into (the slide title): its size at creation, pt,
    and how much lower than a text box's its text starts."""
    base_w: float
    base_h: float
    dy: float


@dataclass(frozen=True, kw_only=True)
class SetShape:
    """What a shape's planner reads of a panel, rule or marked shape."""
    bbox: Box
    shape: ShapeKind | ProducerShapeKind
    flip: bool
    radius: float
    fill: Color
    opacity: float
    """1.0 where the element says none."""
    outline: Outline | None
    shadow: float | None
    """The drop shadow's size (PDF pt); None: no shadow."""


@dataclass(frozen=True, kw_only=True)
class Template:
    """The slide's copy of a template shape a shape is duplicated from, and its unscaled size (pt)."""
    id: str
    w: float
    h: float


TemplateKey = tuple[str, float, float | None]
"""A template shape: (preset, corner adjustment, shadow size in slide pt or None)."""


# ------------------------------------------------------------------------------ from the IR


def set_run(r: AnyRun) -> SetRun:
    """A parsed run as emit sets it: a hole run keeps its width, and is never struck."""
    match r:
        case HoleRun():
            hole: float | None = r.hole
            strike = False
        case Run():
            hole, strike = None, bool(r.strike)
        case _:
            assert_never(r)
    return SetRun(text=r.text, font=r.font, family=r.family, size=r.size, bold=r.bold, italic=r.italic,
                  smallcaps=r.smallcaps, script=r.script, color=r.color, underline=r.underline, strike=strike,
                  highlight=r.highlight, link=r.link, hole=hole, hole_size=None, cell=False, in_sentence=False)


def set_runs(runs: Sequence[AnyRun]) -> tuple[SetRun, ...]:
    return tuple(set_run(r) for r in runs)


def set_bullet(b: Bullet | RenderedBullet) -> SetBullet:
    """A parsed bullet of either stage as emit reads it."""
    match b:
        case RenderedGlyphBullet():
            return SetBullet(kind="glyph", text=b.text, bbox=b.bbox, color=b.color, label_size=b.label.size,
                             ink=b.ink, shape=None)
        case GlyphBullet():
            return SetBullet(kind="glyph", text=b.text, bbox=b.bbox, color=b.color, label_size=b.label.size,
                             ink=None, shape=None)
        case RenderedDrawnBullet():
            return SetBullet(kind="glyph", text=b.text, bbox=b.bbox, color=None, label_size=None, ink=b.ink,
                             shape=None)
        case DrawnBullet():
            return SetBullet(kind="glyph", text=b.text, bbox=b.bbox, color=None, label_size=None, ink=None,
                             shape=None)
        case NumberBullet():
            return SetBullet(kind="number", text=b.text, bbox=b.bbox, color=b.color, label_size=b.label.size,
                             ink=None, shape=None)
        case RenderedImageBullet():
            return SetBullet(kind="image", text=b.text, bbox=b.bbox, color=b.color,
                             label_size=None if b.label is None else b.label.size, ink=None, shape=None)
        case ImageBullet():
            return SetBullet(kind="image", text=b.text, bbox=b.bbox, color=None,
                             label_size=None if b.label is None else b.label.size, ink=None, shape=None)
        case ShapeBullet():
            return SetBullet(kind="shape", text=b.text, bbox=b.bbox, color=b.color, label_size=None, ink=None,
                             shape=b.shape)
        case _:
            assert_never(b)


def set_paragraph(p: Paragraph, runs: tuple[SetRun, ...]) -> SetParagraph:
    """A parsed paragraph (either stage: a rendered one narrows its bullet) holding `runs`, the
    runs emit sets for it."""
    return SetParagraph(runs=runs, lines=p.lines, size=p.size, text_x0=p.text_x0, align=p.align, level=p.level,
                        bullet=None if p.bullet is None else set_bullet(p.bullet), direction=p.direction,
                        justified=p.justified, wrap_limit=p.wrap_limit, tab_x0=p.tab_x0, line_starts=p.line_starts)


def set_text(el: TextElement | ThemeText) -> SetText:
    """A parsed text element (either stage) or theme line as its planner reads it: its runs as
    the IR has them (the planner marks them `in_sentence` and sets its holes)."""
    rotation = el.rotation if isinstance(el, TextElement) else None
    return SetText(paragraphs=tuple(set_paragraph(p, set_runs(p.runs)) for p in el.paragraphs), code=el.code,
                   rotation=rotation)


def number_run(n: Number) -> SetRun:
    """A ball's number as the run its box writes: never small caps."""
    return SetRun(text=n.text, font=n.font, family=n.family, size=n.size, bold=n.bold, italic=n.italic,
                  smallcaps=False, script=None, color=n.color, underline=False, strike=False, highlight=None,
                  link=None, hole=None, hole_size=None, cell=False, in_sentence=False)


def number_box(n: Number) -> tuple[SetRun, tuple[float, float], float]:
    """A parsed ball number as its box reads it (`number_box_of`)."""
    return number_run(n), n.center, n.height


def set_shape(el: ShapeElement | MarkedShape) -> SetShape:
    """A parsed shape as its planner reads it. A marked shape with no fill (an outline alone, which
    render pictures from the rendered stage on) has nothing emit can write: it raises, and
    `DeckPlan.contain` makes it the picture of its region."""
    match el:
        case ShapeElement():
            fill: Color = el.fill
            shadow = None if el.shadow is None else el.shadow.size
        case MarkedShape():
            if el.fill is None:
                raise ValueError(f"shape {el.id}: a marked shape with no fill, which emit cannot write")
            fill, shadow = el.fill, None
        case _:
            assert_never(el)
    # (an opacity of 0 is kept: only an absent one is opaque)
    return SetShape(bbox=el.bbox, shape=el.shape, flip=el.flip, radius=el.radius, fill=fill,
                    opacity=1.0 if el.opacity is None else el.opacity, outline=el.outline, shadow=shadow)


# ------------------------------------------------------------------------------ from dicts


def _wrong(value: object, key: str, expected: str) -> TypeError:
    return TypeError(f"{key}: {value!r} is not {expected}")


def _num(value: Json, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _wrong(value, key, "a number")
    return value


def json_number(value: Json, key: str) -> float:
    """A number read where emit still plans from dicts (tables, diagrams): the value as it is."""
    return _num(value, key)


def _opt_num(value: Json, key: str) -> float | None:
    return None if value is None else _num(value, key)


def _str(value: Json, key: str) -> str:
    if not isinstance(value, str):
        raise _wrong(value, key, "a string")
    return value


def _opt_str(value: Json, key: str) -> str | None:
    return None if value is None else _str(value, key)


def _flag(value: Json) -> bool:
    """A key emit read for its truth (`bool(run.get(k))`)."""
    return bool(value)


def _bool(value: Json, key: str) -> bool:
    """A key emit wrote into a request as it came: a bool, or absent (False)."""
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _wrong(value, key, "a bool")
    return value


def _items(value: Json, key: str) -> Sequence[Json]:
    # (a tuple where a list goes: dicts built in Python, never JSON)
    if isinstance(value, (list, tuple)):
        return value
    raise _wrong(value, key, "a list")


def _box(value: Json, key: str) -> Box:
    a, b, c, d = (_num(v, key) for v in _items(value, key))
    return (a, b, c, d)


def _color(value: Json, key: str) -> Color:
    return Color(_str(value, key))


def _family(value: Json) -> Family:
    for f in FAMILIES:
        if value == f:
            return f
    raise _wrong(value, "family", f"one of {FAMILIES}")


def _script(value: Json) -> Script | None:
    if not value:
        return None
    for s in SCRIPTS:
        if value == s:
            return s
    raise _wrong(value, "script", f"one of {SCRIPTS}")


def _object(value: Json, key: str) -> JsonObject:
    if not isinstance(value, dict):
        raise _wrong(value, key, "an object")
    return value


def run_of(d: JsonMap) -> SetRun:
    """A run dict as emit has always read it (`font` and `size` it always read, the rest as `.get`)."""
    return SetRun(text=_str(d.get("text", ""), "text"), font=_str(d["font"], "font"),
                  family=_family(d.get("family", "sans")), size=_num(d["size"], "size"),
                  bold=_bool(d.get("bold"), "bold"), italic=_bool(d.get("italic"), "italic"),
                  smallcaps=_bool(d.get("smallcaps"), "smallcaps"), script=_script(d.get("script")),
                  color=_color(d.get("color", "#000000"), "color"), underline=_flag(d.get("underline")),
                  strike=_flag(d.get("strike")), highlight=None if not d.get("highlight") else
                  _color(d.get("highlight"), "highlight"), link=_opt_str(d.get("link"), "link"),
                  hole=_opt_num(d.get("hole"), "hole"), hole_size=_opt_num(d.get("hole_size"), "hole_size"),
                  cell=_flag(d.get("cell")), in_sentence=_flag(d.get("in_sentence")))


def runs_of(runs: Json) -> tuple[SetRun, ...]:
    return tuple(run_of(_object(r, "runs")) for r in _items(runs, "runs"))


def _bullet_kind(value: Json) -> BulletKind:
    for k in BULLET_KINDS:
        if value == k:
            return k
    raise _wrong(value, "bullet kind", f"one of {BULLET_KINDS}")


def _bullet_face(value: Json) -> BulletFace | None:
    for f in BULLET_FACES:
        if value == f:
            return f
    return None


def bullet_of(d: JsonMap) -> SetBullet:
    """A bullet dict as emit has always read it: `kind` always, the rest as `.get`."""
    ink = d.get("ink")
    label = d.get("label")
    label_size = _object(label, "label").get("size") if label else None
    return SetBullet(kind=_bullet_kind(d["kind"]), text=_str(d.get("text", ""), "text"),
                     bbox=_box(d.get("bbox", [0.0, 0.0, 0.0, 0.0]), "bbox"),
                     color=None if not d.get("color") else _color(d.get("color"), "color"),
                     label_size=_opt_num(label_size, "label.size"),
                     ink=GlyphInk(box=_box(ink, "ink"), fill=_num(d.get("fill") or 0.0, "fill")) if ink else None,
                     shape=_bullet_face(d.get("shape")))


def line_of(d: JsonMap) -> Line:
    return Line(baseline=_num(d.get("baseline", 0.0), "baseline"), x0=_num(d.get("x0") or 0.0, "x0"),
                x1=_num(d.get("x1") or 0.0, "x1"))


def _align(value: Json) -> Align:
    for a in ALIGNS:
        if value == a:
            return a
    raise _wrong(value, "align", f"one of {ALIGNS}")


def paragraph_of(d: JsonMap, runs: tuple[SetRun, ...]) -> SetParagraph:
    """A paragraph dict as emit has always read it, holding `runs` (the runs emit sets for it)."""
    bullet = d.get("bullet")
    starts = d.get("line_starts")
    return SetParagraph(
        runs=runs, lines=tuple(line_of(_object(ln, "lines")) for ln in _items(d.get("lines", []), "lines")),
        size=_num(d.get("size", 0.0), "size"), text_x0=_num(d.get("text_x0", 0.0), "text_x0"),
        align=_align(d.get("align", "left")), level=int(_num(d.get("level", 0), "level")),
        bullet=bullet_of(_object(bullet, "bullet")) if bullet else None,
        direction="rtl" if d.get("direction") == "rtl" else None, justified=_flag(d.get("justified")),
        wrap_limit=_opt_num(d.get("wrap_limit"), "wrap_limit"), tab_x0=_opt_num(d.get("tab_x0"), "tab_x0"),
        line_starts=None if starts is None else tuple(int(_num(v, "line_starts")) for v in _items(starts, "line_starts")))


def paragraphs_of(paragraphs: Json) -> tuple[SetParagraph, ...]:
    """Paragraph dicts with their runs (`paragraph_of`, `runs_of`)."""
    out: list[SetParagraph] = []
    for p in _items(paragraphs, "paragraphs"):
        d = _object(p, "paragraphs")
        out.append(paragraph_of(d, runs_of(d["runs"])))
    return tuple(out)


def text_of(d: JsonMap) -> SetText:
    """A text element dict as emit has always read it: `paragraphs` always, `code` and `rotation` for their truth."""
    return SetText(paragraphs=paragraphs_of(d["paragraphs"]), code=_flag(d.get("code")),
                   rotation=_opt_num(d.get("rotation"), "rotation"))


def placeholder_of(d: JsonMap) -> Placeholder:
    return Placeholder(base_w=_num(d["base_w"], "base_w"), base_h=_num(d["base_h"], "base_h"), dy=_num(d["dy"], "dy"))


def number_run_of(d: JsonMap) -> SetRun:
    """A ball's number dict as the run its box writes (`number_run`)."""
    return run_of({**d, "smallcaps": False})


def point_of(value: Json, key: str) -> tuple[float, float]:
    a, b = (_num(v, key) for v in _items(value, key))
    return (a, b)


def number_box_of(d: JsonMap) -> tuple[SetRun, tuple[float, float], float]:
    """A ball's number dict as its box reads it: (its run, the ball's centre, the ball's height)."""
    return number_run_of(d), point_of(d["center"], "center"), _num(d["height"], "height")


def _shape_kind(value: Json) -> ShapeKind | ProducerShapeKind:
    for k in TEMPLATE_KINDS:
        if value == k:
            return k
    for p in PRODUCER_SHAPE_KINDS:
        if value == p:
            return p
    raise _wrong(value, "shape", f"one of {TEMPLATE_KINDS + PRODUCER_SHAPE_KINDS}")


def shape_of(d: JsonMap) -> SetShape:
    """A shape dict as emit has always read it: `bbox`, `shape`, `flip` and `fill` always, the rest as `.get`."""
    outline, shadow = d.get("outline"), d.get("shadow")
    frame = _object(outline, "outline") if outline else None
    return SetShape(bbox=_box(d["bbox"], "bbox"), shape=_shape_kind(d["shape"]), flip=_bool(d["flip"], "flip"),
                    radius=_num(d.get("radius", 0.0), "radius"), fill=_color(d["fill"], "fill"),
                    opacity=_num(d.get("opacity", 1.0), "opacity"),
                    outline=None if frame is None else Outline(color=_color(frame["color"], "outline.color"),
                                                               width=_num(frame["width"], "outline.width")),
                    shadow=_num(_object(shadow, "shadow")["size"], "shadow.size") if shadow else None)


def shape_measures_of(d: JsonMap) -> tuple[Box, float, float | None]:
    """What a shape dict's template key reads of it: (bbox, corner radius, shadow size or None)."""
    shadow = d.get("shadow")
    return (_box(d["bbox"], "bbox"), _num(d.get("radius", 0.0), "radius"),
            _num(_object(shadow, "shadow")["size"], "shadow.size") if shadow else None)


def template_of(d: JsonMap) -> Template:
    return Template(id=_str(d["id"], "id"), w=_num(d["w"], "w"), h=_num(d["h"], "h"))
