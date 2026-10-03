"""What emit reads of a text: its runs, bullets and paragraphs as emit sets them.

The IR's records (`ir_types`) say what the page has; these say what emit writes of it, with what
emit derives on the way: a run set among others (`in_sentence`), a hole's no-break spaces
(`hole_size`), a table cell's run (`cell`). None of that goes back into the IR, which sync diffs.
Emit's planners make them from the parsed IR (`set_run`, `set_bullet`, `set_paragraph`).

A table is read the same way (`set_table`, `SetTable`), and the empty table the .pptx carries for
it is a record too (`PptxTable`).

Callers that still hold dicts - classify's lines while it measures them, deck_ir's read of a deck,
theme_sync's layout texts, sync's tables, the tests - reach the same code through `run_of`,
`bullet_of`, `paragraph_of` and `table_of`. Those read each key as emit always read it: a key that is absent takes the value
emit's `.get` gave it, and a value of the wrong type is refused.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

from . import ir_types
from .ir import Align, Family, ProducerShapeKind, Script, ShapeKind, TemplateKind
from .ir_types import (
    ALIGNS, BULLET_KINDS, FAMILIES, PRODUCER_SHAPE_KINDS, SCRIPTS, TEMPLATE_KINDS, AnyRun, BeforeWord, Border, Box,
    Bullet, BulletKind, CardBox, CellFill, Color, Column, DrawnBullet, GlyphBullet, GlyphInk, HoleRun, ImageBullet,
    ImageElement, Line, Mark, MarkedShape, Merge, Node, Number, NumberBullet, Outline, Paragraph, RenderedBullet,
    RenderedDrawnBullet,
    RenderedGlyphBullet, RenderedImageBullet, Rule, Run, ShapeBullet, ShapeElement, TableElement, TextElement, ThemeText,
)
from .json_types import Json, JsonObject
from .typing_compat import assert_never

T = TypeVar("T")

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
class Shell:
    """A text box the .pptx carried (`PptxText`) that a text is written into: its size as the
    import made it, pt (it is resized through the transform's scale, as a placeholder is)."""
    base_w: float
    base_h: float


@dataclass(frozen=True, kw_only=True)
class ShellParagraph:
    """A paragraph of a text shell: one placeholder character the API pass replaces by its words,
    and the bullet the .pptx gives it (`a:buChar`; the API has no preset drawing it)."""
    level: int
    """The nesting level (`lvl`), relative to the box's shallowest bulleted paragraph."""
    char: str | None
    """The bullet's character; None: no bullet."""
    color: Color | None
    """The bullet's colour (its placeholder character's); None: the text's default."""
    size: float
    """The placeholder character's size, pt: the bullet's where there is one (it is drawn at
    100% of it), else the paragraph's."""
    font: str
    """The placeholder character's family."""
    text_size: float
    """The size of the paragraph's end (`a:endParaRPr`), pt: its text's."""


@dataclass(frozen=True, kw_only=True)
class PptxText:
    """The text shell the .pptx carries for a text element whose bullets no preset draws
    (`emit_text.text_shell_of`, emit_pptx._add_text_shell), Slides pt."""
    box: Box
    paragraphs: tuple[ShellParagraph, ...]


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


TemplateKey = tuple[str, float | None, float | None]
"""A template shape: (preset, corner adjustment, shadow size in slide pt or None). A diagram node
whose label goes inside it and whose corners have no radius has no adjustment (None)."""


@dataclass(frozen=True, kw_only=True)
class NodeLook:
    """What a diagram node's template and its label's place are read from (`emit_diagrams`)."""
    bbox: Box
    shape: TemplateKind | None
    """None: a free label."""
    text: str
    """Its label's words, joined and stripped."""
    label_w: float
    radius: float | None
    adjust: float | None
    """The preset's adjustment (ir.Node `adjust`), None where it takes none or the node says none."""

CellGrid = tuple[tuple[tuple[SetRun, ...], ...], ...]
"""A table's cells: per row, per column, the runs."""


@dataclass(frozen=True, kw_only=True)
class SetTable:
    """What a table's planner reads of a table element. What the IR leaves out is empty here, as
    emit's `.get` read it."""
    cells: CellGrid
    """The runs as the IR has them (the layout marks them `cell` and `in_sentence`)."""
    frame: Box
    size: float
    row_baselines: tuple[float, ...]
    row_heights: tuple[float, ...]
    row_lines: tuple[int, ...]
    """Lines per row (a wrapped cell's); empty: one each."""
    columns: tuple[Column, ...]
    rules: tuple[Rule, ...]
    bounds: tuple[float, ...]
    """The PDF's column boundaries; empty: between the columns' words."""
    merges: tuple[Merge, ...]
    borders: tuple[Border, ...]
    fills: tuple[CellFill, ...]
    bands: tuple[tuple[int, float, float], ...]
    wrapped: tuple[tuple[int, int, tuple[int, ...]], ...]
    justified: tuple[tuple[int, int], ...]
    merge_x: tuple[tuple[float, float], ...]
    """Where each merge's words run, by its index in `merges`."""


Margins = tuple[float, float, float, float]
"""A table row's cell margins: left, top, right, bottom (Slides pt)."""


@dataclass(frozen=True, kw_only=True)
class PptxTable:
    """The empty table the .pptx carries for a table element (`emit_tables.pptx_table_of`), Slides pt."""
    x: float
    y: float
    widths: tuple[float, ...]
    heights: tuple[float, ...]
    margins: tuple[Margins, ...]
    """Per row."""
    middle: tuple[tuple[int, int], ...]
    """(row, col) of the cells that span rows: a \\multirow, centred with no top margin of its own."""


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


def set_card(box: CardBox) -> SetText:
    """A diagram card's text box (classify.card_text) as its planner reads it."""
    return SetText(paragraphs=tuple(set_paragraph(p, set_runs(p.runs)) for p in box.paragraphs), code=False,
                   rotation=None)


def node_look(n: Node) -> NodeLook:
    """A parsed diagram node as its template and label placement read it."""
    return NodeLook(bbox=n.bbox, shape=n.shape, text="".join(r.text for runs in n.paragraphs for r in runs).strip(),
                    label_w=n.label_w, radius=n.radius, adjust=n.adjust)


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


def set_table(el: TableElement) -> SetTable:
    """A parsed table as its planner reads it."""
    return SetTable(cells=tuple(tuple(set_runs(cell) for cell in row) for row in el.cells), frame=el.frame,
                    size=el.size, row_baselines=el.row_baselines, row_heights=el.row_heights,
                    row_lines=el.row_lines or (), columns=el.columns, rules=el.rules, bounds=el.bounds,
                    merges=el.merges, borders=el.borders, fills=el.fills or (), bands=el.bands or (),
                    wrapped=el.wrapped or (), justified=el.justified or (), merge_x=el.merge_x or ())


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


def align_of(value: Json) -> Align:
    """A paragraph's `align` read where emit still reads dicts (the theme's titles)."""
    return _align(value)


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


def box_of(value: Json, key: str) -> Box:
    """A bbox read where emit still reads element dicts (groups, placed pictures)."""
    return _box(value, key)


def _template_kind(value: Json) -> TemplateKind | None:
    if value is None:
        return None
    for k in TEMPLATE_KINDS:
        if value == k:
            return k
    raise _wrong(value, "shape", f"one of {TEMPLATE_KINDS}")


def node_look_of(d: JsonMap) -> NodeLook:
    """A node dict as `label_inside` and `node_template_key` always read it: `bbox` and `shape`
    always, `label_w` (0), `radius` and `adjust` as `.get`. (Its `paragraphs` too: none is no label.)"""
    words = "".join(_str(_object(r, "paragraphs")["text"], "text") for runs in _items(d.get("paragraphs", []), "paragraphs")
                    for r in _items(runs, "paragraphs"))
    return NodeLook(bbox=_box(d["bbox"], "bbox"), shape=_template_kind(d["shape"]), text=words.strip(),
                    label_w=_num(d.get("label_w", 0.0), "label_w"), radius=_opt_num(d.get("radius"), "radius"),
                    adjust=_opt_num(d.get("adjust"), "adjust"))


NodeSite = tuple[Box, TemplateKind | None, float | None, float | None]
"""A node as a line's connection reads it: its box, its shape, its preset's adjustment and its
turn (ir.Node `rotation`: the box is the one before it)."""


def node_site_of(d: JsonMap) -> NodeSite:
    """A node dict as `connection` reads it."""
    return (_box(d["bbox"], "bbox"), _template_kind(d["shape"]), _opt_num(d.get("adjust"), "adjust"),
            _opt_num(d.get("rotation"), "rotation"))


ObjectMap = Mapping[str, object]
"""A dict not yet read, whatever its values' types say: classify's table while it decides whether
it fits (its merges are its own TypedDicts), sync's base element. Only the IR's readers take it."""

ElementDict = TypeVar("ElementDict", bound=JsonObject)
"""An element dict a caller hands in and gets back (a copy where it changed), its type the caller's own."""


def _listed(d: ObjectMap, key: str, parse: ir_types.Parse[T]) -> tuple[T, ...]:
    """A list of records the IR's own reader reads, where emit read the key as `.get(key, [])`:
    absent (or empty) is none."""
    value = d.get(key)
    return () if not value else ir_types.tuple_of(parse)(value, ir_types.At(where="table", path=key))


def _required(d: ObjectMap, key: str, parse: ir_types.Parse[T]) -> T:
    return parse(d[key], ir_types.At(where="table", path=key))


def _sequence(value: object, key: str) -> Sequence[object]:
    # (a tuple where a list goes: dicts built in Python, never JSON)
    if isinstance(value, (list, tuple)):
        return value
    raise _wrong(value, key, "a list")


def _run_dict(value: object) -> JsonMap:
    if not isinstance(value, dict):
        raise _wrong(value, "cells", "an object")
    return value


def _cells(value: object) -> CellGrid:
    return tuple(tuple(tuple(run_of(_run_dict(r)) for r in _sequence(cell, "cells")) for cell in _sequence(row, "cells"))
                 for row in _sequence(value, "cells"))


def table_of(d: ObjectMap) -> SetTable:
    """A table dict as emit has always read it: its cells' runs (`run_of`), `frame`, `size`,
    `row_baselines`, `row_heights`, `columns` and `rules` always, the rest as `.get`. Its columns,
    rules, merges and the like are read by the IR's own readers (a rule has its `y`)."""
    numbers = ir_types.tuple_of(ir_types.number)
    return SetTable(cells=_cells(d["cells"]), frame=_required(d, "frame", ir_types.box),
                    size=_required(d, "size", ir_types.number),
                    row_baselines=_required(d, "row_baselines", numbers),
                    row_heights=_required(d, "row_heights", numbers),
                    row_lines=_listed(d, "row_lines", ir_types.integer),
                    columns=_required(d, "columns", ir_types.tuple_of(ir_types.column)),
                    rules=_required(d, "rules", ir_types.tuple_of(ir_types.rule)),
                    bounds=_listed(d, "bounds", ir_types.number), merges=_listed(d, "merges", ir_types.merge),
                    borders=_listed(d, "borders", ir_types.border), fills=_listed(d, "fills", ir_types.cell_fill),
                    bands=_listed(d, "bands", ir_types.band), wrapped=_listed(d, "wrapped", ir_types.wrapped_cell),
                    justified=_listed(d, "justified", ir_types.int_pair), merge_x=_listed(d, "merge_x", ir_types.pair))


def columns_of(cols: Sequence[JsonMap]) -> tuple[Column, ...]:
    """Column dicts (a test's `fit_columns`), read by the IR's reader."""
    return tuple(ir_types.column(dict(c), ir_types.At(where="columns", path=f"[{i}]")) for i, c in enumerate(cols))


def objects_of(value: Json, key: str) -> list[JsonObject]:
    """A list of objects read where emit still reads dicts (a slide's elements, a paragraph's runs):
    the same objects, not copies."""
    return [_object(v, key) for v in _items(value, key)]


def _map(value: object, key: str) -> JsonMap:
    if not isinstance(value, dict):
        raise _wrong(value, key, "an object")
    return value


def dict_of(value: object, key: str) -> JsonObject:
    """A dict not yet read (`ObjectMap`: sync's deck) as the JSON object it is: the same dict."""
    if not isinstance(value, dict):
        raise _wrong(value, key, "an object")
    return value


def maps_of(value: object, key: str) -> list[JsonMap]:
    """`objects_of` a value of a dict not yet read (`ObjectMap`: sync's copy of a slide)."""
    return [_map(v, key) for v in _sequence(value, key)]


def block_of(e: JsonMap) -> int | None:
    """A shape's block (the beamer block it belongs to), if any."""
    block = e.get("block")
    if block is None:
        return None
    if isinstance(block, bool) or not isinstance(block, int):
        raise TypeError(f"block: {block!r} is not an integer")
    return block


# ------------------------------------------------------------------------------ holes and overlays


@dataclass(frozen=True, kw_only=True)
class Gap:
    """Where the PDF has a formula hole: what a hole run says beyond the run emit sets."""
    width: float
    """The hole's width (`hole`, PDF pt): never 0, a hole of no width is none."""
    x0: float
    """Where it starts (`hole_x0`)."""
    before: tuple[BeforeWord, ...]
    """The words before it on its line."""
    next_x0: float | None
    """Where the word after it starts, when nothing separates them."""


@dataclass(frozen=True, kw_only=True)
class HoleParagraph:
    """A paragraph as the pictures over its holes are placed from it (`emit_holes`)."""
    align: Align
    baselines: tuple[float, ...]
    runs: tuple[SetRun, ...]
    gaps: tuple[Gap | None, ...]
    """Per run: where the PDF has its hole; None for words."""


@dataclass(frozen=True, kw_only=True)
class HoleText:
    """A text box as the pictures anchored to it are placed from it."""
    id: str
    paragraphs: tuple[HoleParagraph, ...]


@dataclass(frozen=True, kw_only=True)
class Anchored:
    """A picture placed at words: a formula over its hole, an overlay (arrows, braces) over the
    words its marks lie on."""
    id: str
    bbox: Box
    anchor: str | None
    marks: tuple[Mark, ...]
    """An overlay's marks; none for a formula."""


@dataclass(frozen=True, kw_only=True)
class Place:
    """How far `emit.measure_places` found a picture from its predicted place, slide pt."""
    dx: float
    """The move of its left edge."""
    dy: float
    sx: float | None
    """An overlay's stretch; None for a formula, which is only moved."""


def _gap(r: AnyRun) -> Gap | None:
    return Gap(width=r.hole, x0=r.hole_x0, before=r.before, next_x0=r.next_x0) \
        if isinstance(r, HoleRun) and r.hole else None


def hole_paragraph(p: Paragraph) -> HoleParagraph:
    """A parsed paragraph as its holes' pictures are placed from it."""
    return HoleParagraph(align=p.align, baselines=tuple(ln.baseline for ln in p.lines), runs=set_runs(p.runs),
                         gaps=tuple(_gap(r) for r in p.runs))


def hole_text(el: TextElement) -> HoleText:
    return HoleText(id=el.id, paragraphs=tuple(hole_paragraph(p) for p in el.paragraphs))


def anchored(el: ImageElement) -> Anchored:
    return Anchored(id=el.id, bbox=el.bbox, anchor=el.anchor, marks=el.marks or ())


def before_of(value: Json) -> tuple[BeforeWord, ...]:
    """The words before a hole or a mark, as classify writes them: (width, font, family, bold,
    italic, text, x0)."""
    out: list[BeforeWord] = []
    for word in _items(value, "before"):
        items = _items(word, "before")
        if len(items) != 7:
            raise _wrong(word, "before", "a word of 7 items")
        w, font, family, bold, italic, text, x0 = items
        out.append((_num(w, "before"), _str(font, "before"), _family(family), _bool(bold, "before"),
                    _bool(italic, "before"), _str(text, "before"), _num(x0, "before")))
    return tuple(out)


def gap_of(d: JsonMap) -> Gap | None:
    """A run dict's hole as emit has always read it: `hole_x0` always, `before` and `next_x0` as
    `.get`; None for words."""
    hole = d.get("hole")
    if not hole:
        return None
    return Gap(width=_num(hole, "hole"), x0=_num(d["hole_x0"], "hole_x0"), before=before_of(d.get("before", [])),
               next_x0=_opt_num(d.get("next_x0"), "next_x0"))


def hole_paragraph_of(d: JsonMap) -> HoleParagraph:
    """A paragraph dict as its holes' pictures have always been placed from it: its runs always,
    `align` and `lines` as `.get`, each line's `baseline` always."""
    runs = objects_of(d["runs"], "runs")
    return HoleParagraph(align=_align(d.get("align", "left")),
                         baselines=tuple(_num(ln["baseline"], "baseline") for ln in objects_of(d.get("lines", []), "lines")),
                         runs=tuple(run_of(r) for r in runs), gaps=tuple(gap_of(r) for r in runs))


def hole_text_of(d: JsonMap) -> HoleText:
    return HoleText(id=_str(d["id"], "id"),
                    paragraphs=tuple(hole_paragraph_of(p) for p in objects_of(d["paragraphs"], "paragraphs")))


def mark_of(d: JsonMap) -> Mark:
    """An overlay's mark dict as emit has always read it: `pads` as `.get`, the rest always."""
    return Mark(x=_num(d["x"], "x"), hole_x0=_num(d["hole_x0"], "hole_x0"), pads=_num(d.get("pads", 0.0), "pads"),
                font=_str(d["font"], "font"), family=_family(d["family"]), size=_num(d["size"], "size"),
                bold=_bool(d["bold"], "bold"), italic=_bool(d["italic"], "italic"), before=before_of(d["before"]))


def anchored_of(d: JsonMap) -> Anchored:
    """A picture dict placed at words: `id` and `bbox` always, `anchor` and `marks` as `.get`."""
    return Anchored(id=_str(d["id"], "id"), bbox=_box(d["bbox"], "bbox"), anchor=_opt_str(d.get("anchor"), "anchor"),
                    marks=tuple(mark_of(m) for m in objects_of(d.get("marks") or [], "marks")))
