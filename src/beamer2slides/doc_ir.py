"""The Google Docs intermediate representation, and the canonical HTML dialect.

The Slides pipeline's IR is `deck.json`; this is its Docs counterpart, and it has
two writers and two readers, because HTML — not the IR — is the artefact the user
keeps in git (docs/google-docs.md):

    canonical HTML  --from_html-->  IR  --to_html-->  canonical HTML
    live document   --from_document->  IR

`from_html(to_html(ir)) == ir` is the law the offline tests hold us to. The live
document is the lossy side: Google's importer builds only what HTML can say
(measured in docs/google-docs.md), and reading a document back finds objects no
HTML can create — chips, equations, dropdowns. Those come back as **frozen** runs:
they carry enough to be shown in the canonical file and diffed, and the merge
refuses to rewrite them, so a chip a human inserted survives every later push.

Indices are UTF-16 code units, the Docs API's own unit, and every block read from
a live document keeps the span it came from so edits can be planned against it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from html import escape
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Final, Literal, NamedTuple, NewType, TypedDict, TypeVar

from .google_types import (DocsDimension, DocsDocumentTab, DocsInlineObject, DocsList,
                           DocsNamedRanges, DocsParagraph, DocsParagraphBorder,
                           DocsParagraphElement, DocsParagraphStyle, DocsRequest, DocsRgbColor,
                           DocsStructuralElement, DocsTab, DocsTable, DocsTextStyle, Document)
from .json_types import (Json, JsonObject, JsonShapeError, as_array, as_int, as_object,
                         as_optional_str, as_str)
from .typing_compat import assert_never, override

if TYPE_CHECKING:
    from typing_extensions import Required

# ---------------------------------------------------------------- the IR's types
#
# The IR is plain JSON at runtime — the canonical file's reader and writer, the base
# on Drive and in `.b2s/`, and every test hand it about as dicts — so its types are
# TypedDicts, the way `ir.py` types deck.json: a block is one `Block` whatever its
# kind, `kind` is closed, and what the merge notes on a block while it plans is
# declared here beside what the file says, so the checker sees every key either side
# reads.

U16 = NewType("U16", int)
"""An index into a document, or a length in its index space: UTF-16 code units, the
Docs API's own unit. Never a `str` index — an astral character is two of these."""

Kind = Literal["paragraph", "heading", "item", "title", "subtitle", "table", "toc"]
Align = Literal["left", "center", "right", "justify"]
Script = Literal["super", "sub", "none"]
# A mark a run is on, off or silent about, and the `textStyle` field each one is.
Mark = Literal["bold", "italic", "underline", "strike", "smallcaps"]
MarkApi = Literal["bold", "italic", "underline", "strikethrough", "smallCaps"]
# A paragraph's measurements, by the type of their value.
FloatMeasure = Literal["indent", "indent_first", "line_spacing", "space_above", "space_below"]
BorderKey = Literal["border_top", "border_bottom", "border_left", "border_right"]
BorderApi = Literal["borderTop", "borderBottom", "borderLeft", "borderRight"]
TextMeasure = Literal["shading", "border_top", "border_bottom", "border_left", "border_right"]
FlagMeasure = Literal["page_break", "keep_with_next"]
FlagApi = Literal["pageBreakBefore", "keepWithNext"]
Measure = Literal["indent", "indent_first", "line_spacing", "shading", "space_above",
                  "space_below", "border_top", "border_bottom", "border_left", "border_right",
                  "page_break", "keep_with_next"]
# What the merge says became of a block (`doc_merge.merge`).
Origin = Literal["added in the document", "unknown to the base", "kept over a source delete",
                 "added by the source", "merged", "frozen content differs",
                 "kept from the document", "table grid differs", "the grid the source has"]
# The bullet a settle puts on or takes off (`doc_merge.carry_unimported`).
BulletFix = Literal["none", "ordered", "unordered"]
# A table's rows and columns, and a regrid of one of them (`doc_merge._grid_ops`).
LineName = Literal["row", "column"]
GridHow = Literal["insert", "delete"]
GridOp = tuple[LineName, GridHow, int]
Size = tuple[int, int]
# One stretch of a block's words and the text style it has to be given.
StyledRange = tuple[int, int, DocsTextStyle]


class Style(TypedDict, total=False):
    """How a run's words look: what `_style_of` reads from a `textStyle`, what the
    HTML reader's style frames hold, and what a merge compares run against run."""
    bold: bool
    italic: bool
    underline: bool
    strike: bool
    smallcaps: bool
    code: bool                  # `<code>` in a file written before faces were carried
    script: Script
    font: str
    fontsize: float
    color: str
    highlight: str
    link: str


class Run(Style, total=False):
    """A stretch of one block's words that wear one style, or one frozen object.

    `text` is all a run must have. A run whose text is empty, that holds no index
    units and that is not frozen is nothing, and `merge_runs` drops it."""
    text: Required[str]
    width: U16                  # index units the run holds in the live document
    # A frozen run: a chip, a picture, an equation, an object the API will not describe.
    chip: str
    frozen: bool
    value: str
    format: str
    locale: str
    mime: str
    # A picture's: its file, alt text and title, its size in CSS pixels, where its
    # pixels can be fetched, a digest of its bytes, and whether its file is missing.
    src: str
    alt: str
    title: str
    size: list[int]
    uri: str
    sha: str
    missing: bool


class Measures(TypedDict, total=False):
    """How a paragraph is set, past its kind: what `PARAGRAPH_CSS` and
    `PARAGRAPH_DATA` carry, and its alignment. Absent is inherited."""
    align: Align
    indent: float
    indent_first: float
    line_spacing: float
    shading: str
    space_above: float
    space_below: float
    border_top: str
    border_bottom: str
    border_left: str
    border_right: str
    page_break: bool
    keep_with_next: bool


class GridLine(NamedTuple):
    """One row or column of a merged table: where it is in each side, if anywhere,
    and whether it is about to be taken out of the document (`doc_merge._table_lines`).

    A tuple, because the matching is compared and hashed as one; `gone` defaults to
    False because a line is kept unless the merge says otherwise, and the tests build
    lines as `doc_merge._Line(was, live, mine)`."""
    was: int | None
    live: int | None
    mine: int | None
    gone: bool = False


class Dropped(TypedDict):
    """The file's lines a merge settled as not in the grid, by dimension."""
    row: list[int]
    column: list[int]


class TableLines(TypedDict):
    """How a merged table's lines match the three sides (`doc_merge._merge_table`)."""
    row: list[GridLine]
    column: list[GridLine]
    dropped: Dropped
    mine: Size | None


class Aligned(TypedDict):
    """How a rebased table's lines match the grids the two sides have now
    (`doc_merge.rebase_tables`), so the next pass does not guess them again."""
    live: Size | None
    mine: Size | None
    row_dropped: list[int]
    column_dropped: list[int]
    row_live: list[tuple[int, int]]
    column_live: list[tuple[int, int]]
    row_mine: list[tuple[int, int]]
    column_mine: list[tuple[int, int]]


class WholeStyle(TypedDict):
    """A paragraph style written whole, and the fields it is written under."""
    style: DocsParagraphStyle
    fields: str


class Unimported(TypedDict):
    """What a plan asked for that the document has not got (`doc_merge.carry_unimported`)."""
    paragraph: Measures
    runs: list[StyledRange]
    named: str | None
    bullet: BulletFix | None
    whole: WholeStyle | None


class Block(Measures, total=False):
    """One block: a paragraph of some kind, a table, or a table of contents.

    The first group is what the file and a read say; the second is what the merge
    notes on a block while it plans, and never reaches a file."""
    kind: Required[Kind]
    runs: list[Run]
    rows: list[list[list[Block]]]      # a table's rows, each cell a list of blocks
    span: list[U16]                    # [start, end) in the document it was read from
    key: str
    rangeId: str
    range: list[U16]                   # where its named range really is
    level: int
    ordered: bool | None               # None: an import that cannot say (`_ordered`)
    frozen: bool
    # The merge's notes.
    origin: Origin
    guessed: bool
    unimported: Unimported
    build: Size
    regrid: list[GridOp]
    lines: TableLines | None           # None: the tests' way of saying "no matching"
    aligned: Aligned
    joined: bool
    rewrite: bool
    restyle: bool
    moved: bool
    nowhere: bool
    paragraph_written: bool
    paragraph_merged: bool
    list: str                          # the document's list id (last: it shadows a name)


class Ir(TypedDict, total=False):
    """A document, a canonical file, or one tab of either: its blocks and what
    stands around them. The first tab is the IR itself; the others are `tabs`."""
    blocks: Required[list[Block]]
    title: str
    tab: str | None
    parent: str
    tabs: list[Ir]
    document: str
    tab_title: str
    # The empty paragraphs a body keeps around a table that opens or ends it
    # (`_hide_trailer`).
    trailer: list[U16]
    trailer_kind: Kind
    lead: list[U16]
    lead_kind: Kind
    stray: list[str]                   # words the HTML reader found in no block
    orphans: list[str]                 # named ranges no block is known by
    unsupported: list[str]             # what the file says that no sync can carry


MeasureValue = float | str | bool


class ReadMeasures(TypedDict):
    """A paragraph's measurements as a `paragraphStyle` sets them
    (`_paragraph_measures`): every one named, None where it is inherited."""
    indent: float | None
    indent_first: float | None
    line_spacing: float | None
    shading: str | None
    space_above: float | None
    space_below: float | None
    border_top: str | None
    border_bottom: str | None
    border_left: str | None
    border_right: str | None
    page_break: bool | None
    keep_with_next: bool | None


class NamedDefault(ReadMeasures):
    """What a named style says (`_named_defaults`): the marks it puts on, whether it
    raises its runs, its face, size and alignment, and its paragraph measurements.
    None where the style says nothing."""
    marks: set[Mark]
    script: Script | None
    font: str | None
    fontsize: float | None
    align: Align | None


def measure_of(measures: Measures, key: Measure) -> MeasureValue | None:
    """One measurement a block sets; None where it inherits."""
    match key:
        case "indent":
            return measures.get("indent")
        case "indent_first":
            return measures.get("indent_first")
        case "line_spacing":
            return measures.get("line_spacing")
        case "shading":
            return measures.get("shading")
        case "space_above":
            return measures.get("space_above")
        case "space_below":
            return measures.get("space_below")
        case "border_top":
            return measures.get("border_top")
        case "border_bottom":
            return measures.get("border_bottom")
        case "border_left":
            return measures.get("border_left")
        case "border_right":
            return measures.get("border_right")
        case "page_break":
            return measures.get("page_break")
        case "keep_with_next":
            return measures.get("keep_with_next")
        case _:
            assert_never(key)


def read_measure(measures: ReadMeasures, key: Measure) -> MeasureValue | None:
    """One measurement of a read `paragraphStyle` or named style; None: not set."""
    match key:
        case "indent":
            return measures["indent"]
        case "indent_first":
            return measures["indent_first"]
        case "line_spacing":
            return measures["line_spacing"]
        case "shading":
            return measures["shading"]
        case "space_above":
            return measures["space_above"]
        case "space_below":
            return measures["space_below"]
        case "border_top":
            return measures["border_top"]
        case "border_bottom":
            return measures["border_bottom"]
        case "border_left":
            return measures["border_left"]
        case "border_right":
            return measures["border_right"]
        case "page_break":
            return measures["page_break"]
        case "keep_with_next":
            return measures["keep_with_next"]
        case _:
            assert_never(key)


def as_float(value: MeasureValue) -> float:
    """A length or a spacing, which is what a float measurement holds."""
    if isinstance(value, (bool, str)):
        raise TypeError(f"a measurement of the wrong type: {value!r}")
    return value


def as_text(value: MeasureValue) -> str:
    """A colour or a border as the file spells it."""
    if not isinstance(value, str):
        raise TypeError(f"a measurement of the wrong type: {value!r}")
    return value


def as_flag(value: MeasureValue) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"a measurement of the wrong type: {value!r}")
    return value


def set_measure(measures: Measures, key: Measure, value: MeasureValue) -> None:
    """Give a block one measurement, of the type its key holds."""
    match key:
        case "indent":
            measures["indent"] = as_float(value)
        case "indent_first":
            measures["indent_first"] = as_float(value)
        case "line_spacing":
            measures["line_spacing"] = as_float(value)
        case "shading":
            measures["shading"] = as_text(value)
        case "space_above":
            measures["space_above"] = as_float(value)
        case "space_below":
            measures["space_below"] = as_float(value)
        case "border_top":
            measures["border_top"] = as_text(value)
        case "border_bottom":
            measures["border_bottom"] = as_text(value)
        case "border_left":
            measures["border_left"] = as_text(value)
        case "border_right":
            measures["border_right"] = as_text(value)
        case "page_break":
            measures["page_break"] = as_flag(value)
        case "keep_with_next":
            measures["keep_with_next"] = as_flag(value)
        case _:
            assert_never(key)


def pop_measure(measures: Measures, key: Measure) -> None:
    """Take one measurement off a block: it inherits again."""
    match key:
        case "indent":
            measures.pop("indent", None)
        case "indent_first":
            measures.pop("indent_first", None)
        case "line_spacing":
            measures.pop("line_spacing", None)
        case "shading":
            measures.pop("shading", None)
        case "space_above":
            measures.pop("space_above", None)
        case "space_below":
            measures.pop("space_below", None)
        case "border_top":
            measures.pop("border_top", None)
        case "border_bottom":
            measures.pop("border_bottom", None)
        case "border_left":
            measures.pop("border_left", None)
        case "border_right":
            measures.pop("border_right", None)
        case "page_break":
            measures.pop("page_break", None)
        case "keep_with_next":
            measures.pop("keep_with_next", None)
        case _:
            assert_never(key)


# ---------------------------------------------------------------- an IR through JSON
#
# The one IR that reaches us as JSON is a sync base (`doc_sync.load_base`: Drive's copy
# and the cache beside the file); the file is read by `from_html` and the document by
# `from_document`. A base is parsed here, where it enters, into the types above.

_S = TypeVar("_S", bound=str)
_MEASURE_NAMES: Final[dict[str, Measure]] = {"indent": "indent", "indent_first": "indent_first",
                                             "line_spacing": "line_spacing", "shading": "shading",
                                             "space_above": "space_above",
                                             "space_below": "space_below",
                                             "border_top": "border_top",
                                             "border_bottom": "border_bottom",
                                             "border_left": "border_left",
                                             "border_right": "border_right",
                                             "page_break": "page_break",
                                             "keep_with_next": "keep_with_next"}


def parse_ir(value: Json, where: str) -> Ir:
    """An IR that has been through JSON, as the types above say it, or `JsonShapeError`
    naming the first value of another shape.

    A base is a *read* of the document (`doc_sync.settle` stores what it read), so
    what is parsed is what a read and a settle put on a block. What the merge notes on
    a block while it plans (`origin`, `lines`, `aligned`, ...) is never stored, and a
    key nobody reads is left behind rather than refusing the base: a base refused is the
    `--assume-base` dialog, where either answer throws somebody's work away."""
    obj = as_object(value, where)
    ir: Ir = {"blocks": parse_blocks(obj.get("blocks"), f"{where}.blocks")}
    read_ir_fields(ir, obj, where)
    return ir


def read_ir_fields(ir: Ir, obj: JsonObject, where: str) -> None:
    """Every key of `obj` an `Ir` holds but its blocks, parsed onto `ir` (`parse_ir`;
    `doc_sync.parse_base` reads a base's own keys beside them)."""
    for key, raw in obj.items():
        at = f"{where}.{key}"
        if raw is None and key != "tab":
            continue      # null is what a writer that meant "nothing" said
        match key:
            case "title":
                ir["title"] = as_str(raw, at)
            case "tab":
                ir["tab"] = as_optional_str(raw, at)
            case "parent":
                ir["parent"] = as_str(raw, at)
            case "tabs":
                ir["tabs"] = [parse_ir(tab, f"{at}[{n}]") for n, tab in enumerate(as_array(raw, at))]
            case "document":
                ir["document"] = as_str(raw, at)
            case "tab_title":
                ir["tab_title"] = as_str(raw, at)
            case "trailer":
                ir["trailer"] = _u16s(raw, at)
            case "trailer_kind":
                ir["trailer_kind"] = _one_of(raw, KINDS, at)
            case "lead":
                ir["lead"] = _u16s(raw, at)
            case "lead_kind":
                ir["lead_kind"] = _one_of(raw, KINDS, at)
            case "stray":
                ir["stray"] = _strs(raw, at)
            case "orphans":
                ir["orphans"] = _strs(raw, at)
            case "unsupported":
                ir["unsupported"] = _strs(raw, at)
            case _:
                pass


def parse_blocks(value: Json, where: str) -> list[Block]:
    return [_parse_block(block, f"{where}[{n}]") for n, block in enumerate(as_array(value, where))]


def _parse_block(value: Json, where: str) -> Block:
    obj = as_object(value, where)
    block: Block = {"kind": _one_of(obj.get("kind"), KINDS, f"{where}.kind")}
    for key, raw in obj.items():
        at = f"{where}.{key}"
        if raw is None and key != "ordered":
            continue
        match key:
            case "runs":
                block["runs"] = [_parse_run(run, f"{at}[{n}]")
                                 for n, run in enumerate(as_array(raw, at))]
            case "rows":
                block["rows"] = [[parse_blocks(cell, f"{at}[{r}][{c}]")
                                  for c, cell in enumerate(as_array(row, f"{at}[{r}]"))]
                                 for r, row in enumerate(as_array(raw, at))]
            case "span":
                block["span"] = _u16s(raw, at)
            case "range":
                block["range"] = _u16s(raw, at)
            case "key":
                block["key"] = as_str(raw, at)
            case "rangeId":
                block["rangeId"] = as_str(raw, at)
            case "list":
                block["list"] = as_str(raw, at)
            case "level":
                block["level"] = as_int(raw, at)
            case "ordered":
                block["ordered"] = None if raw is None else _flag(raw, at)
            case "frozen":
                block["frozen"] = _flag(raw, at)
            case "guessed":
                block["guessed"] = _flag(raw, at)
            case "align":
                block["align"] = _one_of(raw, ALIGNS, at)
            case _:
                if (measure := _MEASURE_NAMES.get(key)) is not None:
                    try:
                        set_measure(block, measure, _measure_json(raw, at))
                    except TypeError as err:
                        raise JsonShapeError(f"{at}: {err}") from err
    return block


def _parse_run(value: Json, where: str) -> Run:
    obj = as_object(value, where)
    run: Run = {"text": as_str(obj.get("text"), f"{where}.text")}
    for key, raw in obj.items():
        at = f"{where}.{key}"
        if raw is None:
            continue
        match key:
            case "bold":
                run["bold"] = _flag(raw, at)
            case "italic":
                run["italic"] = _flag(raw, at)
            case "underline":
                run["underline"] = _flag(raw, at)
            case "strike":
                run["strike"] = _flag(raw, at)
            case "smallcaps":
                run["smallcaps"] = _flag(raw, at)
            case "code":
                run["code"] = _flag(raw, at)
            case "script":
                run["script"] = _one_of(raw, SCRIPT_NAMES, at)
            case "font":
                run["font"] = as_str(raw, at)
            case "fontsize":
                run["fontsize"] = _json_number(raw, at)
            case "color":
                run["color"] = as_str(raw, at)
            case "highlight":
                run["highlight"] = as_str(raw, at)
            case "link":
                run["link"] = as_str(raw, at)
            case "width":
                run["width"] = U16(as_int(raw, at))
            case "chip":
                run["chip"] = as_str(raw, at)
            case "frozen":
                run["frozen"] = _flag(raw, at)
            case "value":
                run["value"] = as_str(raw, at)
            case "format":
                run["format"] = as_str(raw, at)
            case "locale":
                run["locale"] = as_str(raw, at)
            case "mime":
                run["mime"] = as_str(raw, at)
            case "src":
                run["src"] = as_str(raw, at)
            case "alt":
                run["alt"] = as_str(raw, at)
            case "title":
                run["title"] = as_str(raw, at)
            case "size":
                run["size"] = [as_int(side, f"{at}[{n}]") for n, side in enumerate(as_array(raw, at))]
            case "uri":
                run["uri"] = as_str(raw, at)
            case "sha":
                run["sha"] = as_str(raw, at)
            case "missing":
                run["missing"] = _flag(raw, at)
            case _:
                pass
    return run


def _flag(value: Json, where: str) -> bool:
    if isinstance(value, bool):
        return value
    raise JsonShapeError(f"{where}: true or false was expected, found {value!r}")


def _json_number(value: Json, where: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    raise JsonShapeError(f"{where}: a number was expected, found {value!r}")


def _measure_json(value: Json, where: str) -> MeasureValue:
    if isinstance(value, (bool, int, float, str)):
        return value
    raise JsonShapeError(f"{where}: a measurement was expected, found {value!r}")


def _u16s(value: Json, where: str) -> list[U16]:
    return [U16(as_int(each, f"{where}[{n}]")) for n, each in enumerate(as_array(value, where))]


def _strs(value: Json, where: str) -> list[str]:
    return [as_str(each, f"{where}[{n}]") for n, each in enumerate(as_array(value, where))]


def _one_of(value: Json, allowed: Sequence[_S], where: str) -> _S:
    """`value` as the one of a closed set of words it spells."""
    said = as_str(value, where)
    for each in allowed:
        if each == said:
            return each
    raise JsonShapeError(f"{where}: one of {', '.join(allowed)} was expected, found {said!r}")


class Beside(TypedDict):
    """The words next to an equation, and what stops them (`_beside`)."""
    text: str
    then: Literal["edge", "equation", "object"]


class EquationSpot(TypedDict):
    """An equation of a document and the words on either side of it."""
    tab: str | None
    start: int
    before: Beside
    after: Beside


class Unmodelled(TypedDict):
    """What `unmodelled` found at one path: how often, and one example."""
    count: int
    example: str


class UnreadBlock(TypedDict):
    """A block carrying something `unmodelled_in` names (`unread_blocks`)."""
    tab: str | None
    span: list[int]
    kind: Kind
    words: str
    unread: dict[str, Unmodelled]


# textStyle keys we carry as a tag of their own, and the tag each becomes.
# `code` is no longer *read* from a document — a face is carried as the face it is
# (`font`), so Consolas and Roboto Mono stop being the same thing — but the tag is
# still written and still understood, so a file written before that keeps working.
MARKS: Final[tuple[tuple[Literal["bold", "italic", "underline", "strike", "code"], str], ...]] = (
    ("bold", "b"), ("italic", "i"), ("underline", "u"), ("strike", "s"), ("code", "code"))
# The marks themselves, and the `textStyle` field each one is. A mark is True, absent
# — or **False**, which is not the same thing: a theme that makes its headings bold
# leaves a reader who un-bolds one word with a run that says `bold: false`, and a file
# that could only say "bold" or nothing would have that word bold again the first time
# the source restyled the block (`_style_of`, `data-off`).
MARK_FIELDS: Final[tuple[tuple[Mark, MarkApi], ...]] = (
    ("bold", "bold"), ("italic", "italic"), ("underline", "underline"),
    ("strike", "strikethrough"), ("smallcaps", "smallCaps"))
# A run raised or lowered from the baseline. Not a mark, because it is not a flag:
# `baselineOffset` is one of NONE / SUPERSCRIPT / SUBSCRIPT, so the run says which,
# and "none" is the third value rather than the absence of the other two — a theme
# could raise a whole named style, and a reader who puts one word back on the
# baseline must be able to say so (the `data-off` reasoning, one value wider).
SCRIPTS: Final[dict[str, Script]] = {"SUPERSCRIPT": "super", "SUBSCRIPT": "sub", "NONE": "none"}
TO_SCRIPT: Final[dict[Script, str]] = {v: k for k, v in SCRIPTS.items()}
SCRIPT_TAGS: Final[dict[Script, str]] = {"super": "sup", "sub": "sub"}
# What `<code>` has always meant on the write side, and goes on meaning.
CODE_FAMILY = "Courier New"
# A named style is a block kind; everything else is a paragraph.
HEADINGS: Final[dict[str, int]] = {f"HEADING_{n}": n for n in range(1, 7)}
NAMED_STYLE: Final[dict[int, str]] = {n: f"HEADING_{n}" for n in range(1, 7)} | {0: "NORMAL_TEXT"}
# The two named styles that are not a heading level. They are a block kind of their
# own rather than a level, because that is what they are: Docs has NORMAL_TEXT,
# TITLE, SUBTITLE and HEADING_1..6, and nothing else. `namedStyleType` is a field the
# merge owns (`doc_merge.MANAGED_PARAGRAPH`), so a style it cannot name is one it
# writes NORMAL_TEXT over: before these were modelled, a document's Title that the
# source moved, rewrote or restyled came back as body text, and nothing said so.
NAMED_KINDS: Final[dict[str, Kind]] = {"TITLE": "title", "SUBTITLE": "subtitle"}
KIND_STYLE: Final[dict[Kind, str]] = {kind: style for style, kind in NAMED_KINDS.items()}
# Every kind that is one paragraph of text.
TEXT_KINDS: Final[tuple[Kind, ...]] = ("paragraph", "heading", "item", *NAMED_KINDS.values())
# And every kind that is a structural element and not a paragraph. Docs' index rules
# are about these: nothing can be inserted at one's own index, the newline in front of
# one cannot be deleted, a body cannot open or end on one without an empty paragraph
# beside it, and one is deleted by its own span. `doc_merge._structural` is the test.
STRUCTURAL: Final[tuple[Kind, ...]] = ("table", "toc")
KINDS: Final[tuple[Kind, ...]] = (*TEXT_KINDS, *STRUCTURAL)
ALIGNMENTS: Final[dict[str, Align]] = {"START": "left", "CENTER": "center", "END": "right",
                                       "JUSTIFIED": "justify"}
ALIGNS: Final[tuple[Align, ...]] = tuple(ALIGNMENTS.values())
SCRIPT_NAMES: Final[tuple[Script, ...]] = tuple(SCRIPTS.values())
TO_ALIGNMENT: Final[dict[Align, str]] = {v: k for k, v in ALIGNMENTS.items()}
# Paragraph properties the importer keeps, and the CSS each is written as (measured,
# docs/google-docs.md: "Paragraph CSS: `text-align` (incl. justify), `margin-left`,
# `text-indent`, `line-height`"). A length is always written in points.
PARAGRAPH_CSS: Final[dict[FloatMeasure, str]] = {"indent": "margin-left",
                                                 "indent_first": "text-indent",
                                                 "line_spacing": "line-height"}
# Paragraph properties no HTML can carry, and the attribute each is written as. The
# spelling matters: `background-color` on a `<p>` is *not* paragraph shading, it is
# "a character highlight on its runs" (measured), so the CSS would be a lie; and the
# study lists no `margin-top`/`margin-bottom` among what survives. All three are
# written by `batchUpdate` instead (`doc_merge.tidy_requests`), which was measured
# working for `updateParagraphStyle` with `shading`.
PARAGRAPH_DATA: Final[dict[Measure, str]] = {
    "shading": "data-shading", "space_above": "data-space-above",
    "space_below": "data-space-below",
    "border_top": "data-border-top", "border_bottom": "data-border-bottom",
    "border_left": "data-border-left", "border_right": "data-border-right",
    "page_break": "data-page-break",
    "keep_with_next": "data-keep-with-next"}
# Every measurement, in the order the two tables above name them: the order a read
# writes them onto a block.
MEASURES: Final[tuple[Measure, ...]] = ("indent", "indent_first", "line_spacing", "shading",
                                        "space_above", "space_below", "border_top",
                                        "border_bottom", "border_left", "border_right",
                                        "page_break", "keep_with_next")
# The four sides of a paragraph's border, and the `paragraphStyle` field of each.
# Borders sit in the very dialog that sets shading (Format → Paragraph styles →
# Borders and shading), so a document whose shading round-tripped while its rule
# vanished on the first rewrite was a difference nobody could explain.
BORDER_SIDES: Final[dict[BorderKey, BorderApi]] = {
    "border_top": "borderTop", "border_bottom": "borderBottom",
    "border_left": "borderLeft", "border_right": "borderRight"}
# The two paragraph properties that are simply on or off.
PARAGRAPH_FLAGS: Final[dict[FlagMeasure, FlagApi]] = {"page_break": "pageBreakBefore",
                                                      "keep_with_next": "keepWithNext"}
# Docs' dash styles, spelled as CSS spells them, since the file says a border the
# way CSS says one: `<width> <style> <colour>`, and `pad <n>pt` after it when Docs
# leaves a gap between the rule and the text.
DASH_STYLES: Final[dict[str, str]] = {"SOLID": "solid", "DOT": "dotted", "DASH": "dashed"}
TO_DASH_STYLE: Final[dict[str, str]] = {css: api for api, css in DASH_STYLES.items()}
BORDER_RE = re.compile(r"\s*(-?\d+(?:\.\d+)?)pt\s+(solid|dotted|dashed)\s+"
                       r"(#[0-9a-fA-F]{6})(?:\s+pad\s+(-?\d+(?:\.\d+)?)pt)?\s*$")
# What Docs paints a link with when nobody asked: the import's blue, and the editor's.
LINK_COLORS = {"#0000ee", "#1155cc"}
# Docs writes this private-use character where an object sits that the API will not
# describe: a placeholder chip, or a watermark in the header (docs/google-docs.md).
OBJECT_SENTINEL = ""

# ParagraphElement keys that are not a textRun, and the chip kind each becomes.
CHIPS = {"dateElement": "date", "person": "person", "richLink": "link",
         "footnoteReference": "footnote", "equation": "equation",
         "inlineObjectElement": "image", "horizontalRule": "rule"}
SLUG = re.compile(r"[^a-z0-9]+")
# A block's identity in the live document. The deck writes `b2s:<slide>/<element>`
# into an element's alt-text title; a document has no such field, so the same string
# becomes the name of a named range over the paragraph. Docs moves those ranges with
# the text and keeps them through the editor and undo (docs/google-docs.md).
KEY_PREFIX = "b2s:"
# The `<meta>` that tells a canonical file which document it belongs to.
DOCUMENT_META = "b2s-document"
# The first tab's own title. The file's `<title>` is the *document's* name, and a
# document's name is not the name of the tab you are looking at: a document of one tab
# has both, and they differ as soon as somebody renames either. Every other tab says
# its title on its `<section>`, so without this the first tab alone could not be named
# from the file — read back at every settle, so a rename in the file went twice over.
TAB_META = "b2s-tab"
# An `<img width>` is CSS pixels, a document's picture size is points (measured: a
# 60 × 40 px picture imports as 45 × 30 pt).
PT_PER_PX = 0.75
# Picture fields: what the canonical file says about one, and what only a sync keeps.
PICTURE_ATTRS = ("src", "alt", "title")


# ---------------------------------------------------------------- runs

def _style_of(text_style: DocsTextStyle, default: NamedDefault | None = None) -> Style:
    """The marks we keep, from a Docs textStyle.

    `default` is what the paragraph's named style already says (`_named_defaults`):
    a run that only repeats it needs nothing in the canonical file, and clearing the
    field leaves the same face on the page. Without that, a document whose importer
    wrote `font-family` on every run would come back as a file of spans, which is
    the opposite of what a file in git is for.

    A size is kept as the document reports it, to two places. The importer rounds a
    size to a whole point (measured: 7.5pt → 7pt), so a half point written in the
    file is lost at `push` — but the push settles by regenerating the file from the
    document it made, so the file then says 7 and nothing oscillates. Every write
    after that is a `batchUpdate`, which takes the size as given: rounding here
    would instead move a size a reader chose in the editor.
    """
    style: Style = {}
    marks: set[Mark] = default["marks"] if default is not None else set()
    for key, api in MARK_FIELDS:
        if _mark_api(text_style, api):
            _set_mark(style, key, True)
        elif api in text_style and key in marks:
            # The theme says this mark and the run says no. Saying nothing here would
            # leave the file unable to tell that from a run that simply inherits, and
            # the field is named with no value on every restyle (`doc_merge.MANAGED`),
            # which is "back to what you inherit": the reader's choice would be
            # undone by a source edit that never mentioned it.
            _set_mark(style, key, False)
    script = SCRIPTS.get(text_style.get("baselineOffset", ""))
    if script == "none":
        # Said out loud only against a named style that raises the run, exactly as a
        # mark is said off: everywhere else NONE is what a run with nothing on it
        # falls back to, and writing `<span data-script="none">` around every word of
        # an ordinary paragraph is the wall of spans this is all about avoiding.
        script = "none" if default is not None and default["script"] else None
    if script:
        style["script"] = script
    family = text_style.get("weightedFontFamily", {}).get("fontFamily")
    if family and family != (default["font"] if default is not None else None):
        style["font"] = family
    magnitude = text_style.get("fontSize", {}).get("magnitude")
    if magnitude:
        size = round(float(magnitude), 2)
        if size and size != (default["fontsize"] if default is not None else None):
            style["fontsize"] = size
    rgb = text_style.get("foregroundColor", {}).get("color", {}).get("rgbColor")
    if rgb:
        style["color"] = _hex(rgb)
    back = text_style.get("backgroundColor", {}).get("color", {}).get("rgbColor")
    if back:
        style["highlight"] = _hex(back)
    url = text_style.get("link", {}).get("url")
    if url:
        style["link"] = url
        # Docs paints a link blue and underlines it by itself. Keeping that here
        # would put an <u> and a colour into the canonical file for every link, and
        # write them back on the next push, so the styling a link gets for free is
        # dropped and only the deliberate kind survives.
        if style.get("color") in LINK_COLORS:
            style.pop("color")
            style.pop("underline", None)
    return style


def _mark_api(text_style: DocsTextStyle, api: MarkApi) -> bool | None:
    """One mark's field of a Docs textStyle."""
    match api:
        case "bold":
            return text_style.get("bold")
        case "italic":
            return text_style.get("italic")
        case "underline":
            return text_style.get("underline")
        case "strikethrough":
            return text_style.get("strikethrough")
        case "smallCaps":
            return text_style.get("smallCaps")
        case _:
            assert_never(api)


def mark_of(style: Style, key: Mark) -> bool | None:
    """One mark of a run: True, False (said off), or None (silent)."""
    match key:
        case "bold":
            return style.get("bold")
        case "italic":
            return style.get("italic")
        case "underline":
            return style.get("underline")
        case "strike":
            return style.get("strike")
        case "smallcaps":
            return style.get("smallcaps")
        case _:
            assert_never(key)


def _set_mark(style: Style, key: Mark, value: bool) -> None:
    match key:
        case "bold":
            style["bold"] = value
        case "italic":
            style["italic"] = value
        case "underline":
            style["underline"] = value
        case "strike":
            style["strike"] = value
        case "smallcaps":
            style["smallcaps"] = value
        case _:
            assert_never(key)


def _hex(rgb: DocsRgbColor) -> str:
    return "#%02x%02x%02x" % (round(255 * rgb.get("red", 0.0)), round(255 * rgb.get("green", 0.0)),
                              round(255 * rgb.get("blue", 0.0)))


def apply_style(target: Style, style: Style) -> None:
    """Put every field `style` says onto `target`, as `dict.update` would."""
    if (bold := style.get("bold")) is not None:
        target["bold"] = bold
    if (italic := style.get("italic")) is not None:
        target["italic"] = italic
    if (underline := style.get("underline")) is not None:
        target["underline"] = underline
    if (strike := style.get("strike")) is not None:
        target["strike"] = strike
    if (smallcaps := style.get("smallcaps")) is not None:
        target["smallcaps"] = smallcaps
    if (code := style.get("code")) is not None:
        target["code"] = code
    if (script := style.get("script")) is not None:
        target["script"] = script
    if (font := style.get("font")) is not None:
        target["font"] = font
    if (fontsize := style.get("fontsize")) is not None:
        target["fontsize"] = fontsize
    if (color := style.get("color")) is not None:
        target["color"] = color
    if (highlight := style.get("highlight")) is not None:
        target["highlight"] = highlight
    if (link := style.get("link")) is not None:
        target["link"] = link


def apply_measures(target: Measures, measures: Measures) -> None:
    """Put every measurement `measures` says, and its alignment, onto `target`."""
    if (align := measures.get("align")) is not None:
        target["align"] = align
    for key in MEASURES:
        if (value := measure_of(measures, key)) is not None:
            set_measure(target, key, value)


def _chip_run(kind: str, value: DocsParagraphElement) -> Run:
    """A Docs object no HTML can create: keep what identifies it, and freeze it."""
    run: Run = {"chip": kind, "frozen": True, "text": ""}
    if kind == "date":
        date = value.get("dateElement", {}).get("dateElementProperties", {})
        run["text"] = date.get("displayText", "")
        run["value"] = date.get("timestamp", "")
        run["format"] = date.get("dateFormat", "")
        run["locale"] = date.get("locale", "")
    elif kind == "person":
        person = value.get("person", {}).get("personProperties", {})
        run["text"] = person.get("name", "")
        run["value"] = person.get("email", "")
    elif kind == "link":
        link = value.get("richLink", {}).get("richLinkProperties", {})
        run["text"] = link.get("title", "")
        run["value"] = link.get("uri", "")
        if mime := link.get("mimeType"):
            run["mime"] = mime
    elif kind == "footnote":
        note = value.get("footnoteReference", {})
        run["text"] = note.get("footnoteNumber", "")
        run["value"] = note.get("footnoteId", "")
    elif kind == "image":
        run["value"] = value.get("inlineObjectElement", {}).get("inlineObjectId", "")
    return run


def runs_text(runs: Sequence[Run]) -> str:
    """The words of a block, for fingerprints and diffs. A frozen run is one unit."""
    return "".join(r["text"] for r in runs)


def merge_runs(runs: Sequence[Run]) -> list[Run]:
    """Join neighbouring runs that carry the same style; drop empty ones."""
    out: list[Run] = []
    for run in runs:
        if not run.get("frozen") and not run["text"] and not run.get("width"):
            continue
        last = out[-1] if out else None
        if (last and not last.get("frozen") and not run.get("frozen")
                and style_key(last) == style_key(run)):
            last["text"] += run["text"]
            if "width" in last or "width" in run:
                last["width"] = U16(last.get("width", 0) + run.get("width", 0))
        else:
            out.append({**run})
    return out


def style_key(run: Run) -> Run:
    """What decides whether two runs can be one: style, not text or measurements.
    The run with its text emptied and its width left out, so two keys are equal
    exactly when the runs differ in nothing else."""
    key: Run = {**run, "text": ""}
    key.pop("width", None)
    return key


def utf16_len(text: str) -> int:
    """The Docs API counts indices in UTF-16 code units, not characters."""
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------- live document -> IR

def from_document(doc: Document, tab_id: str | None = None) -> Ir:
    """`documents.get` JSON to the IR.

    A document read with `includeTabsContent=True` has no `body` at all — it has
    `tabs` — and one read without the flag silently holds only the first tab
    (docs/google-docs.md). Both shapes are accepted; `tab_id` picks one tab.
    """
    body, tab = _body_of(doc, tab_id)
    ir: Ir = {"title": doc.get("title", ""), "tab": tab, "blocks": []}
    part = _tab_part(doc, tab)
    lists = (doc.get("lists") if "lists" in doc else {}) or part.get("lists", {})
    objects = (doc.get("inlineObjects") if "body" in doc else None) \
        or part.get("inlineObjects", {})
    defaults = _named_defaults(doc, tab)
    for element in body:
        block = _block_of(element, lists, objects, defaults)
        if block:
            ir["blocks"].append(block)
    _hide_trailer(ir, _planted_starts(named_ranges_of(doc, tab)))
    return ir


def _planted_starts(named_ranges: Mapping[str, DocsNamedRanges]) -> list[int]:
    """Where this document's `b2s:` ranges begin: the marks somebody has planted an
    identity on."""
    return sorted(span.get("startIndex", 0)
                  for name, entry in named_ranges.items() if name.startswith(KEY_PREFIX)
                  for ranged in entry.get("namedRanges", [])
                  for span in ranged.get("ranges", []))


def _hide_trailer(ir: Ir, planted: Sequence[int]) -> None:
    """Leave out the empty paragraph a body keeps after a table that ends it.

    A document must end on a paragraph, so a table written last — by an import or by
    `insertTable` — always has an empty one after it that nobody asked for, and that
    no request can delete (measured: the mark in front of it is refused). Read as a
    block it would put a `<p></p>` into the canonical file that the next import makes
    again, and a table appended at the end would never converge. Its span is kept as
    `trailer`, because that is where a block appended after the table is written.
    It is the other half of the paragraph the table was inserted into, so it can come
    with that one's bullet or heading: `trailer_kind` says so, and
    `doc_merge.tidy_requests` makes it a plain paragraph again.

    Both are recognised by their **shape**, and a block of the source's own can have
    that shape: an empty paragraph the file asks for, standing in front of a table, is
    a lead by this rule and vanishes out of the IR — its named range, taken by no
    block, then reads as an orphan and is deleted, so the block loses its identity and
    the file is settled with it somewhere else. A delete is all it takes to put one
    there: the body's opening table goes with its lead, and the paragraph behind it
    becomes the first thing in front of the next table (offline chain-12 seed 630138,
    where the source moved the opening table to the end and the empty paragraph
    between the two came back behind the second one, silently). So a paragraph
    somebody has **planted an identity on** is never scaffolding, whichever end it
    stands at: `planted` is where this document's `b2s:` ranges begin.
    """
    blocks = ir["blocks"]

    def theirs(block: Block) -> bool:
        low, high = block.get("span", [U16(0), U16(0)])
        return any(low <= start < high for start in planted)

    # A body with nothing in it — a tab just added — is one empty paragraph, and that
    # one is a trailer too: what is written there goes *into* it.
    if (blocks and (len(blocks) == 1 or blocks[-2]["kind"] in STRUCTURAL)
            and blocks[-1]["kind"] in TEXT_KINDS and not blocks[-1].get("runs")
            and not theirs(blocks[-1])):
        last = blocks.pop()
        ir["trailer"] = _span(last)
        if last["kind"] != "paragraph" or last.get("align"):
            ir["trailer_kind"] = last["kind"]
    # Its mirror image: a table cannot be the first thing in a body, so one that is has
    # an empty paragraph in front of it that no request can delete either (measured: a
    # tab's first table, made by `insertTable`, always does). That one is the `lead`,
    # and the first block written in front of the table goes into it.
    if (len(blocks) >= 2 and blocks[1]["kind"] in STRUCTURAL
            and blocks[0]["kind"] in TEXT_KINDS and not blocks[0].get("runs")
            and not theirs(blocks[0])):
        first = blocks.pop(0)
        ir["lead"] = _span(first)
        if first["kind"] != "paragraph" or first.get("align"):
            ir["lead_kind"] = first["kind"]


def _span(block: Block) -> list[U16]:
    """A block read from a document: where it stands in it."""
    span = block.get("span")
    if span is None:
        raise KeyError("a block read from a document has a span")
    return span


def _body_of(doc: Document, tab_id: str | None) -> tuple[list[DocsStructuralElement], str | None]:
    if (body := doc.get("body")) is not None:
        return body.get("content", []), None
    for tab in _flatten_tabs(doc.get("tabs", [])):
        props = tab.get("tabProperties", {})
        if tab_id in (None, props.get("tabId")):
            return tab.get("documentTab", {}).get("body", {}).get("content", []), props.get("tabId")
    return [], tab_id


def tabs_of(doc: Document) -> list[DocsTab]:
    """Every tab of a document read with `includeTabsContent`, child tabs included,
    in the order the document shows them."""
    return _flatten_tabs(doc.get("tabs", []))


def parts(ir: Ir) -> list[Ir]:
    """The IR's tabs, each an IR of its own: the first tab is the IR itself (its
    blocks are the file's body), the others are `ir["tabs"]` — `<section>`s in the
    file, each with its `tab` id, `title` and, for a child tab, `parent`."""
    return [ir] + list(ir.get("tabs", []))


def tab_part(ir: Ir | None, tab: str | None) -> Ir | None:
    """The tab of `ir` with that id, past the first; None when it has none."""
    for part in (ir.get("tabs", []) if ir is not None else []):
        if tab and part.get("tab") == tab:
            return part
    return None


# ---------------------------------------------------------------- equations

# How much of the words beside an equation anchors it in the Markdown export.
ANCHOR = 24
# What the export puts between characters that the document's text does not have: an
# escape, and the markers of bold, italic and strikethrough.
_MARKUP = r"[\\*_~]*"
# The start of a Markdown line, past whatever opens the block: a list marker, a
# heading's hashes, a quote, a table cell's bar.
_LINE_START = r"(?:^|(?<=\n))[ \t]*(?:(?:[*+-]|\d+\.|#+|>|\|)[ \t]*)*" + _MARKUP
# The end of a paragraph's line: a hard break, a table cell's bar, or the end.
_LINE_END = _MARKUP + r"[ \t]*(?:\\?\n|\||\Z)"


Piece = tuple[Literal["text", "equation", "object"], str, int]


def equation_spots(doc: Document) -> list[EquationSpot]:
    """Every equation of a document read with `includeTabsContent`, in the order the
    document shows them, with the words on either side of it in its paragraph.

    `before` / `after` are `_beside`'s: the words up to the next thing that is not
    text — another equation, a chip, or the paragraph's edge — and which of those it
    is, since the Markdown export writes them differently (`latex_of`).
    """
    spots: list[EquationSpot] = []
    for tab in _flatten_tabs(doc.get("tabs", [])):
        tab_id = tab.get("tabProperties", {}).get("tabId")
        body = tab.get("documentTab", {}).get("body", {}).get("content", [])
        for paragraph in _paragraphs(body):
            pieces: list[Piece] = []
            for el in paragraph.get("elements", []):
                if (text_run := el.get("textRun")) is not None:
                    pieces.append(("text", text_run.get("content", "").rstrip("\n"), 0))
                elif "equation" in el:
                    pieces.append(("equation", "", el.get("startIndex", 0)))
                else:
                    pieces.append(("object", "", 0))
            for i, (kind, _, start) in enumerate(pieces):
                if kind != "equation":
                    continue
                spots.append({"tab": tab_id, "start": start,
                              "before": _beside(pieces[:i][::-1], True),
                              "after": _beside(pieces[i + 1:], False)})
    return spots


def _paragraphs(content: Sequence[DocsStructuralElement]) -> Iterator[DocsParagraph]:
    for element in content:
        if (paragraph := element.get("paragraph")) is not None:
            yield paragraph
        for row in element.get("table", {}).get("tableRows", []):
            for cell in row.get("tableCells", []):
                yield from _paragraphs(cell.get("content", []))


def _beside(pieces: Sequence[Piece], backwards: bool) -> Beside:
    """The words next to an equation, walking away from it until something that is
    not a word: {"text": ..., "then": "edge" | "equation" | "object"}. `backwards`
    walks the pieces in front of it, nearest first."""
    text: list[str] = []
    then: Literal["edge", "equation", "object"] = "edge"
    for kind, value, _ in pieces:
        if kind != "text":
            then = kind
            break
        text.append(value)
    return {"text": "".join(text[::-1] if backwards else text), "then": then}


EquationAt = tuple[str | None, int]


def latex_of(spots: Sequence[EquationSpot], markdown: str) -> dict[EquationAt, str]:
    """Each equation's LaTeX from the document's Markdown export, by (tab, start).

    The export writes an equation as `$…$` (or `$$…$$` on a line of its own) and
    escapes neither a dollar in the text nor one in the equation (measured: `costs $5`
    and `${x}_{1}+α_$$`), so the dollars alone cannot say where one ends. The words the
    document puts on either side can: each equation is looked for between them, in
    document order, and the shortest LaTeX that fits is taken. An equation that is not
    found with certainty gets none — the file then shows it without its LaTeX, as it
    did before.
    """
    found: dict[EquationAt, str] = {}
    cursor, previous = 0, False
    for spot in spots:
        before, after = spot["before"], spot["after"]
        if before["text"]:
            prefix = _loose(before["text"][-ANCHOR:]) + _MARKUP + r"(?:\]\([^)\n]*\))?" + _MARKUP
        elif before["then"] == "edge":
            prefix = _LINE_START
        elif before["then"] == "equation" and previous:
            prefix = r"\G[ \t]*"
        else:
            prefix = None
        if after["text"]:
            suffix = _MARKUP + r"\[?" + _loose(after["text"][:ANCHOR])
        elif after["then"] == "edge":
            suffix = _LINE_END
        elif after["then"] == "equation":
            suffix = r"[ \t]*" + _MARKUP + r"\$"
        else:
            suffix = None
        previous = False
        if prefix is None and suffix is None:
            continue
        pattern = (prefix or "") + r"(\$\$?)(.+?)\1" + (f"(?={suffix})" if suffix else "")
        if prefix == r"\G[ \t]*":
            match = re.compile(pattern[2:]).match(markdown, cursor)
        else:
            match = re.compile(pattern, re.M).search(markdown, cursor)
        if match is None:
            continue
        found[(spot["tab"], spot["start"])] = match.group(2)
        cursor, previous = match.end(), True
    return found


def _loose(text: str) -> str:
    """A pattern for `text` as the Markdown export writes it: any escape or style
    marker allowed in front of each character, and white space as any white space."""
    out: list[str] = []
    space = False
    for char in text:
        if char.isspace():
            if not space:
                out.append(r"\s+")
            space = True
            continue
        space = False
        out.append(_MARKUP + re.escape(char))
    return "".join(out)


def attach_latex(ir: Ir, found: Mapping[EquationAt, str]) -> int:
    """Write each equation's LaTeX into its frozen run's text, where `latex_of` found
    it: the file then shows what the equation says, where it showed nothing."""
    done = 0
    for part in parts(ir):
        tab = part.get("tab")
        for block in _all_blocks(part["blocks"]):
            at: int = block.get("span", [U16(0)])[0]
            for run in block.get("runs", []):
                if run.get("chip") == "equation" and (tab, at) in found:
                    run["text"] = found[(tab, at)]
                    done += 1
                at += run.get("width", 1 if run.get("frozen") else utf16_len(run["text"]))
    return done


def _all_blocks(blocks: Sequence[Block]) -> Iterator[Block]:
    for block in blocks:
        yield block
        for row in block.get("rows", []):
            for cell in row:
                yield from _all_blocks(cell)


def _flatten_tabs(tabs: Sequence[DocsTab]) -> list[DocsTab]:
    out: list[DocsTab] = []
    for tab in tabs:
        out.append(tab)
        out += _flatten_tabs(tab.get("childTabs", []))
    return out


def _named_defaults(doc: Document, tab_id: str | None) -> dict[str, NamedDefault]:
    """What each named style says, by `namedStyleType`.

    A paragraph and its runs report the properties *set on them*, and an import sets
    plenty that only repeat the style they are already in. Subtracting the named
    style keeps the canonical file down to what somebody chose: everything here is
    what a cleared field falls back to anyway, so nothing is lost by leaving it out.
    """
    styles = (doc.get("namedStyles") if "body" in doc else None) \
        or _tab_part(doc, tab_id).get("namedStyles", {})
    out: dict[str, NamedDefault] = {}
    for style in styles.get("styles", []):
        text, para = style.get("textStyle", {}), style.get("paragraphStyle", {})
        size = text.get("fontSize", {}).get("magnitude")
        measured = _paragraph_measures(para)
        out[style.get("namedStyleType", "")] = {
            # Which marks this style puts on, so that a run saying one of them off
            # can be told from a run inheriting it (`_style_of`). The marks are not
            # subtracted the way the face and the size are: a run that repeats the
            # theme's bold is written bold, which costs the file a `<b>` and keeps it
            # readable on its own.
            "marks": {key for key, api in MARK_FIELDS if _mark_api(text, api)},
            # Whether this style *raises* its runs, for the same reason. A style that
            # says NONE says what every run falls back to anyway, so it is no default
            # to tell a run apart from.
            "script": SCRIPTS.get(text.get("baselineOffset", "")) if
            text.get("baselineOffset") in ("SUPERSCRIPT", "SUBSCRIPT") else None,
            "font": text.get("weightedFontFamily", {}).get("fontFamily"),
            "fontsize": round(float(size), 2) if size else None,
            # A theme that centres its headings says so here, and the heading itself
            # then reports no alignment at all. Without this the file said nothing
            # and the write side put START back: the document's own theme, undone by
            # a source edit that never mentioned alignment.
            "align": ALIGNMENTS.get(para.get("alignment", "")),
            "indent": measured["indent"], "indent_first": measured["indent_first"],
            "space_above": measured["space_above"], "space_below": measured["space_below"],
            "line_spacing": measured["line_spacing"], "shading": measured["shading"],
            "border_top": measured["border_top"], "border_bottom": measured["border_bottom"],
            "border_left": measured["border_left"], "border_right": measured["border_right"],
            "page_break": measured["page_break"], "keep_with_next": measured["keep_with_next"]}
    return out


def _paragraph_measures(style: DocsParagraphStyle) -> ReadMeasures:
    """The paragraph properties the dialect carries, from a Docs paragraphStyle.
    A property the paragraph does not set is None: it is inherited, not zero."""
    spacing = style.get("lineSpacing")
    rgb = style.get("shading", {}).get("backgroundColor", {}).get("color", {}).get("rgbColor")
    return {"indent": _points(style.get("indentStart")),
            "indent_first": _points(style.get("indentFirstLine")),
            "space_above": _points(style.get("spaceAbove")),
            "space_below": _points(style.get("spaceBelow")),
            "line_spacing": round(spacing / 100, 3) if spacing else None,
            "shading": _hex(rgb) if rgb else None,
            "border_top": _border(style.get("borderTop")),
            "border_bottom": _border(style.get("borderBottom")),
            "border_left": _border(style.get("borderLeft")),
            "border_right": _border(style.get("borderRight")),
            "page_break": True if style.get("pageBreakBefore") else None,
            "keep_with_next": True if style.get("keepWithNext") else None}


def _border(side: DocsParagraphBorder | None) -> str | None:
    """One paragraph border as the file spells it: CSS's `<width> <style> <colour>`,
    with `pad <n>pt` after it where Docs leaves a gap between the rule and the text.

    A border of no width is no border — Docs reports a rule somebody took off that
    way, with its colour still on it — so the file says nothing rather than `0pt`,
    which would make taking a rule off read as setting one.
    """
    if not side:
        return None
    width = _points(side.get("width")) or 0.0
    if not width:
        return None
    rgb = side.get("color", {}).get("color", {}).get("rgbColor")
    said = (f"{_number(width)}pt {DASH_STYLES.get(side.get('dashStyle', ''), 'solid')} "
            f"{_hex(rgb) if rgb else '#000000'}")
    pad = _points(side.get("padding")) or 0.0
    return f"{said} pad {_number(pad)}pt" if pad else said


def _points(dimension: DocsDimension | None) -> float | None:
    """A Docs Dimension in points. Rounded, so two reads of one document spell the
    same number and a diff of the file shows only what somebody changed."""
    if not dimension or (magnitude := dimension.get("magnitude")) is None:
        return None
    return round(float(magnitude), 2)


def _tab_part(doc: Document, tab_id: str | None) -> DocsDocumentTab:
    """A tab's own parts — `lists`, `inlineObjects`, `namedStyles` — which sit beside
    its body, not in it."""
    for tab in _flatten_tabs(doc.get("tabs", [])):
        if tab_id in (None, tab.get("tabProperties", {}).get("tabId")):
            return tab.get("documentTab", {})
    return {}


def _picture(run: Run, objects: Mapping[str, DocsInlineObject]) -> Run:
    """What the document says about a picture: its size, its alt text, and where its
    pixels can be fetched for the next half hour (`uri`, never written to a file)."""
    embedded = (objects.get(run.get("value", ""), {}).get("inlineObjectProperties", {})
                .get("embeddedObject", {}))
    size = embedded.get("size", {})
    width = size.get("width", {}).get("magnitude")
    height = size.get("height", {}).get("magnitude")
    if width and height:
        run["size"] = [round(width / PT_PER_PX), round(height / PT_PER_PX)]
    if alt := embedded.get("description"):
        run["alt"] = alt
    if title := embedded.get("title"):
        run["title"] = title
    uri = embedded.get("imageProperties", {}).get("contentUri")
    if uri:
        run["uri"] = uri
    return run


def _element_span(element: DocsStructuralElement) -> list[U16]:
    return [U16(element.get("startIndex", 0)), U16(element.get("endIndex", 0))]


def _block_of(element: DocsStructuralElement, lists: Mapping[str, DocsList],
              objects: Mapping[str, DocsInlineObject],
              defaults: Mapping[str, NamedDefault]) -> Block | None:
    if (table := element.get("table")) is not None:
        return _table_block(element, table, lists, objects, defaults)
    if "tableOfContents" in element:
        # Generated content: readable, never writable (no insertTableOfContents in v1).
        return {"kind": "toc", "frozen": True, "runs": [], "span": _element_span(element)}
    para = element.get("paragraph")
    if para is None:
        return None
    default = defaults.get(para.get("paragraphStyle", {}).get("namedStyleType")
                           or "NORMAL_TEXT")
    runs: list[Run] = []
    for el in para.get("elements", []):
        # `width` is how many index units the run holds in the live document: a chip
        # is one unit however long its words look, and an equation was thirteen.
        width = U16(el.get("endIndex", 0) - el.get("startIndex", 0))
        if (text_run := el.get("textRun")) is not None:
            content = text_run.get("content", "")
            for piece, frozen in _split_sentinel(content):
                if frozen:
                    runs.append({"chip": "object", "frozen": True, "text": piece,
                                 "width": U16(1)})
                elif piece:
                    said: Run = {"text": piece, "width": U16(utf16_len(piece))}
                    apply_style(said, _style_of(text_run.get("textStyle", {}), default))
                    runs.append(said)
            continue
        for key, chip in CHIPS.items():
            if key in el:
                run: Run = {**_chip_run(chip, el), "width": width}
                runs.append(_picture(run, objects) if chip == "image" else run)
                break
        else:
            # A dropdown chip: an element with a span and no content key at all.
            runs.append({"chip": "unknown", "frozen": True, "text": "", "width": width})
    # The trailing newline is the paragraph, not text in it.
    if runs and not runs[-1].get("frozen") and runs[-1]["text"].endswith("\n"):
        runs[-1]["text"] = runs[-1]["text"][:-1]
        runs[-1]["width"] = U16(runs[-1].get("width", 1) - 1)
    style = para.get("paragraphStyle", {})
    named = style.get("namedStyleType", "")
    bullet = para.get("bullet")
    kind: Kind
    if bullet:
        kind = "item"
    elif named in HEADINGS:
        kind = "heading"
    elif (named_kind := NAMED_KINDS.get(named)) is not None:
        kind = named_kind
    else:
        kind = "paragraph"
    block: Block = {"runs": merge_runs(runs), "span": _element_span(element), "kind": kind}
    if bullet:
        level = bullet.get("nestingLevel", 0)
        block["level"] = level
        block["ordered"] = _ordered(lists, bullet.get("listId"), level)
        # Which list it is in. Not part of a block's shape and nothing the file can
        # say — it is the document's own name for the thing a glyph belongs to, which
        # `doc_merge.unwritten_glyphs` needs to know two items share.
        if list_id := bullet.get("listId"):
            block["list"] = list_id
    elif named in HEADINGS:
        block["level"] = HEADINGS[named]
    align = ALIGNMENTS.get(style.get("alignment", ""))
    if align and align != ((default["align"] if default is not None else None) or "left"):
        block["align"] = align
    # A property that only says what the paragraph's named style already says is left
    # out; so are a bullet's own indents, which belong to the list preset and not to
    # anybody's choice — writing them back would fight `createParagraphBullets`, and
    # the file would then differ from the document at every sync.
    measured = _paragraph_measures(style)
    measured["indent_first"] = _relative_first(measured, default)
    for key in MEASURES:
        if kind == "item" and key in ("indent", "indent_first"):
            continue
        value = read_measure(measured, key)
        if value is not None and value != _inherited(key, default):
            set_measure(block, key, value)
    return block


def _relative_first(measured: ReadMeasures, default: NamedDefault | None) -> float | None:
    """The first line's indent as CSS says it — from `margin-left` — out of Docs'
    `indentFirstLine`, which is measured from the page margin like `indentStart`.

    `margin-left:36pt; text-indent:18pt` is a first line at 54 pt, and that is what
    Drive's importer makes of it (measured 2026-09-24: indentStart 36, indentFirstLine
    54; it used to copy the 18 across, which hid that the two are not the same number).
    None when the paragraph sets neither indent: it shows its named style's.
    """
    indent, indent_first = measured["indent"], measured["indent_first"]
    if indent is None and indent_first is None:
        return None
    start = indent if indent is not None else (default["indent"] if default else None) or 0.0
    first = (indent_first if indent_first is not None
             else (default["indent_first"] if default else None) or 0.0)
    return round(first - start, 2)


def _inherited(key: Measure, default: NamedDefault | None) -> MeasureValue | None:
    """What a paragraph that sets nothing shows: its named style's value, or, where
    the style says nothing either, the property's own default — single spacing, no
    indent, no space around it, no shading."""
    if key == "indent_first":
        # The named style's own, measured from its own start (`_relative_first`).
        if default is None:
            return 0.0
        return round((default["indent_first"] or 0.0) - (default["indent"] or 0.0), 2)
    if default is not None and (said := read_measure(default, key)) is not None:
        return said
    match key:
        case "line_spacing":
            return 1.0
        case "indent" | "space_above" | "space_below":
            return 0.0
        case ("shading" | "border_top" | "border_bottom" | "border_left" | "border_right"
              | "page_break" | "keep_with_next"):
            return None
        case _:
            assert_never(key)


def _split_sentinel(content: str) -> list[tuple[str, bool]]:
    """Ordinary text and the U+E907 object sentinels inside it, in order."""
    return [(piece, piece == OBJECT_SENTINEL)
            for piece in re.split(f"({OBJECT_SENTINEL})", content) if piece]


def _ordered(lists: Mapping[str, DocsList], list_id: str | None, level: int) -> bool | None:
    """Ordered levels carry a real glyphType, unordered carry a glyphSymbol.

    A list Drive's HTML import made says neither: every level comes back
    `GLYPH_TYPE_UNSPECIFIED` with no glyphFormat and no glyphSymbol, for `<ol>` and
    `<ul>` alike, though the editor renders the two differently (measured on the
    spike document, docs/google-docs.md). So the answer is **None** — not False —
    the canonical file fills it in, and `doc_merge.bullet_requests` gives the list
    bullets of its own, which read back properly from then on.
    """
    levels = (lists.get(list_id or "", {}).get("listProperties", {}).get("nestingLevels", []))
    if level >= len(levels):
        return None
    glyphs = levels[level]
    if "glyphSymbol" in glyphs:
        return False
    if glyphs.get("glyphType", "GLYPH_TYPE_UNSPECIFIED") != "GLYPH_TYPE_UNSPECIFIED":
        return True
    return None


def _table_block(element: DocsStructuralElement, table: DocsTable,
                 lists: Mapping[str, DocsList], objects: Mapping[str, DocsInlineObject],
                 defaults: Mapping[str, NamedDefault]) -> Block:
    rows: list[list[list[Block]]] = []
    for row in table.get("tableRows", []):
        cells: list[list[Block]] = []
        for cell in row.get("tableCells", []):
            blocks = [b for b in (_block_of(e, lists, objects, defaults)
                                  for e in cell.get("content", [])) if b]
            cells.append(blocks)
        rows.append(cells)
    return {"kind": "table", "rows": rows, "span": _element_span(element)}


# ---------------------------------------------------------------- IR -> canonical HTML

def to_html(ir: Ir) -> str:
    """The canonical file: one block per line, so a git diff reads like the document.

    Everything here is measured to survive Google's importer except the frozen
    runs, which cannot be created by any import and are written so that a human
    can read them and `from_html` can put them back.
    """
    lines = ["<!DOCTYPE html>", "<html>", "<head>", '<meta charset="utf-8">']
    if document := ir.get("document"):
        # Which document this file is. The file is the project: told where it lives, it
        # can be synced from any checkout without a folder of state beside it.
        lines.append(f'<meta name="{DOCUMENT_META}" content="{escape(document, quote=True)}">')
    if tab_title := ir.get("tab_title"):
        lines.append(f'<meta name="{TAB_META}" content="{escape(tab_title, quote=True)}">')
    if title := ir.get("title"):
        lines.append(f"<title>{escape(title)}</title>")
    lines += ["</head>", "<body>"]
    lines += _blocks_html(ir["blocks"], 0)
    for part in ir.get("tabs", []):
        # The tabs past the first. `data-tab` is the document's id for one; a section
        # without it is a tab the source asks for and the next sync creates.
        said = (("data-tab", part.get("tab")), ("title", part.get("title")),
                ("data-parent", part.get("parent")))
        attrs = "".join(f' {name}="{escape(value, quote=True)}"' for name, value in said if value)
        lines += [f"<section{attrs}>"] + _blocks_html(part["blocks"], 1) + ["</section>"]
    lines += ["</body>", "</html>", ""]
    return "\n".join(lines)


def blocks_html(blocks: Sequence[Block]) -> str:
    """The blocks as the file writes them: what two reads of a tab are compared by."""
    return "\n".join(_blocks_html(blocks, 0))


def _blocks_html(blocks: Sequence[Block], depth: int) -> list[str]:
    lines: list[str] = []
    index = 0
    pad = " " * depth
    while index < len(blocks):
        block = blocks[index]
        if block["kind"] == "item":
            run = _item_run(blocks, index)
            lines += _list_html(blocks[index:index + run], depth)
            index += run
            continue
        if block["kind"] == "table":
            lines += _table_html(block, depth)
            index += 1
            continue
        lines.append(pad + _block_html(block))
        index += 1
    return lines


def _table_html(block: Block, depth: int) -> list[str]:
    """A table over several lines, one per row, so a git diff reads like the document.

    The line breaks go between `</tr>` and `<tr>`, and between the table's own tags
    and its rows — the places where an HTML parser has nowhere to put text, so the
    white space cannot become content. A row stays on one line with its cells: it is
    inside a `<td>` that white space *would* be content. (Measured on `from_html`;
    the importer's side of it wants confirming on a live document.)
    """
    pad = " " * depth
    lines = [pad + f"<table{_key_attr(block)}>"]
    for row in block.get("rows", []):
        cells = "".join(f"<td>{''.join(_blocks_html(cell, 0))}</td>" for cell in row)
        lines.append(pad + f" <tr>{cells}</tr>")
    return lines + [pad + "</table>"]


def _item_run(blocks: Sequence[Block], start: int) -> int:
    """How many list items follow, from `start`, that belong to one list."""
    end = start
    ordered = blocks[start].get("ordered", False)
    while (end < len(blocks) and blocks[end]["kind"] == "item"
           and blocks[end].get("ordered", False) == ordered):
        end += 1
    return end - start


def _list_html(items: Sequence[Block], depth: int) -> list[str]:
    tag = "ol" if items[0].get("ordered") else "ul"
    pad = " " * depth
    lines, index = [pad + f"<{tag}>"], 0
    while index < len(items):
        level = items[index].get("level", 0)
        deeper = index + 1
        while deeper < len(items) and items[deeper].get("level", 0) > level:
            deeper += 1
        lines.append(pad + f" <li{_key_attr(items[index])}"
                           f"{_paragraph_attrs(items[index])}>"
                           f"{_runs_html(items[index].get('runs', []))}</li>")
        if deeper > index + 1:
            lines += _list_html(items[index + 1:deeper], depth + 1)
        index = deeper
    lines.append(pad + f"</{tag}>")
    return lines


def _key_attr(block: Block) -> str:
    """The block's identity, carried in the file the way a deck carries alt-text titles.

    Google's importer drops it — only `<a id>` in a heading ever became anything
    (docs/google-docs.md) — and that is fine: the live document's copy of the same
    identity is a named range. This one exists so that a paragraph rewritten from
    end to end can still be recognised as the same paragraph.
    """
    key = block.get("key")
    return f' id="{escape(key, quote=True)}"' if key else ""


def _block_html(block: Block) -> str:
    """One block on one line. A table is the exception (`_table_html`): it is written
    across several, and `_blocks_html` sends it there before this is reached."""
    kind = block["kind"]
    if kind == "toc":
        return f'<p class="b2s-toc"{_key_attr(block)}></p>'
    tag = f"h{block.get('level')}" if kind == "heading" else "p"
    return (f"<{tag}{_key_attr(block)}{_paragraph_attrs(block)}>"
            f"{_runs_html(block.get('runs', []))}</{tag}>")


def _paragraph_attrs(block: Block) -> str:
    """What a paragraph says about itself past its words: its alignment and indents
    as CSS the importer keeps, its shading and the space around it as attributes of
    ours, which only `batchUpdate` can write (`PARAGRAPH_DATA` says why)."""
    # Title and Subtitle have no tag: HTML's headings are levels and these are not,
    # so they go in as ours. Whether the importer makes anything of a `<p>` like this
    # is not measured and does not matter — `push` settles by regenerating the file
    # from the document it made, and every write after that is a `batchUpdate`, which
    # names the style outright.
    out = (f' data-style="{block["kind"]}"' if block["kind"] in KIND_STYLE else "")
    align = block.get("align")
    styles = [f"text-align:{align}"] if align else []
    for key, css in PARAGRAPH_CSS.items():
        if (value := measure_of(block, key)) is not None:
            unit = "" if key == "line_spacing" else "pt"
            styles.append(f"{css}:{_number(value)}{unit}")
    out += f' style="{";".join(styles)}"' if styles else ""
    for measure, name in PARAGRAPH_DATA.items():
        if (said := measure_of(block, measure)) is not None:
            out += f' {name}="{escape(_number(said), quote=True)}"'
    return out


def _number(value: MeasureValue) -> str:
    """A measurement as the file spells it: no trailing zeros, so one number always
    reads the same way and a diff shows only what somebody changed."""
    if isinstance(value, str):
        return value
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _runs_html(runs: Sequence[Run]) -> str:
    return "".join(_run_html(r) for r in runs)


def _img_html(run: Run) -> str:
    """A picture: where its file is, what it says, how big, and which object it is.

    `data-object` is the document's name for it. The document cannot say which file a
    picture came from — `insertInlineImage` records the staging URL, and nothing can
    set a field of ours on it — so this attribute is what ties the two together the
    next time round, the way `id=` does for a block.
    """
    said = (("src", run.get("src")), ("alt", run.get("alt")), ("title", run.get("title")))
    attrs = "".join(f' {k}="{escape(v, quote=True)}"' for k, v in said if v)
    if size := run.get("size"):
        attrs += ' width="%d" height="%d"' % (size[0], size[1])
    if value := run.get("value"):
        attrs += f' data-object="{escape(value, quote=True)}"'
    return f"<img{attrs}>"


def _tag_on(run: Run, key: Literal["bold", "italic", "underline", "strike", "code"]) -> bool:
    """Whether a run wears the tag of one of `MARKS`."""
    match key:
        case "code":
            return bool(run.get("code"))
        case "bold" | "italic" | "underline" | "strike":
            return bool(mark_of(run, key))
        case _:
            assert_never(key)


def _run_html(run: Run) -> str:
    if run.get("chip") == "image":
        return _img_html(run)
    if run.get("frozen"):
        said = (("value", run.get("value")), ("format", run.get("format")),
                ("locale", run.get("locale")), ("mime", run.get("mime")))
        attrs = "".join(f' data-{k}="{escape(v, quote=True)}"' for k, v in said if v)
        return (f'<span class="b2s-chip" data-chip="{run.get("chip")}"{attrs}>'
                f'{escape(run["text"])}</span>')
    out = escape(run["text"])
    styles: list[str] = []
    if color := run.get("color"):
        styles.append(f"color:{color}")
    if highlight := run.get("highlight"):
        styles.append(f"background-color:{highlight}")
    if font := run.get("font"):
        # One name, unquoted, no fallback list: a list is *mangled* on import
        # (measured, docs/google-docs.md: `Georgia, serif` arrives as `Geo`), while a
        # single name survives verbatim, `Comic Sans MS` included.
        styles.append(f"font-family:{font}")
    if fontsize := run.get("fontsize"):
        # In points, as the document says it (the importer rounds a fraction away;
        # `_style_of` says why that is harmless). The key is `fontsize`, not `size`:
        # a picture run's `size` is its width and height.
        styles.append(f"font-size:{_number(fontsize)}pt")
    attrs = f' style="{escape(";".join(styles), quote=True)}"' if styles else ""
    if run.get("smallcaps"):
        # No CSS reaches `smallCaps` through the importer — the study lists it only
        # among the things `updateTextStyle` writes — so the file says it in an
        # attribute of ours and `doc_merge` writes it with `batchUpdate`.
        attrs += ' data-smallcaps="1"'
    script = run.get("script")
    if script == "none":
        # A run put back on the baseline against a named style that raises it. HTML
        # has no opposite of `<sup>` any more than it has one of `<b>`, so the file
        # says it in an attribute of ours, as `data-off` does for the marks.
        attrs += ' data-script="none"'
    off = " ".join(key for key, _ in MARK_FIELDS if mark_of(run, key) is False)
    if off:
        # A mark turned off against a theme that turns it on. HTML has no tag for it
        # — `<b>` has no opposite — so the file names the marks in an attribute of
        # ours, as it does small caps, and `doc_merge` writes them `False`.
        attrs += f' data-off="{off}"'
    if attrs:
        out = f"<span{attrs}>{out}</span>"
    if script is not None and (script_tag := SCRIPT_TAGS.get(script)):
        out = f"<{script_tag}>{out}</{script_tag}>"
    for mark, tag in MARKS:
        if _tag_on(run, mark):
            out = f"<{tag}>{out}</{tag}>"
    if link := run.get("link"):
        out = f'<a href="{escape(link, quote=True)}">{out}</a>'
    return out


# ---------------------------------------------------------------- canonical HTML -> IR

# What a `<p data-style>` names, and what each tag of a frame says.
_STYLE_KINDS: Final[dict[str, Kind]] = {"title": "title", "subtitle": "subtitle"}
_ALIGNS: Final[dict[str, Align]] = {"left": "left", "center": "center", "right": "right",
                                    "justify": "justify"}
_SCRIPTS_SAID: Final[dict[str, Script]] = {"super": "super", "sub": "sub", "none": "none"}
_MARKS_SAID: Final[dict[str, Mark]] = {key: key for key, _ in MARK_FIELDS}


class _Reader(HTMLParser):
    """The dialect's parser. Anything outside the dialect is ignored, not guessed at.

    Markup, that is - never *words*. Text that lands in no block reaches the document
    through nothing at all, and a sync writes the file again from what the document then
    says, so it would go without a trace: one mistyped tag (`<it>` for `<li>`) and a
    person's sentence is dropped twice over, in silence. Such text is collected in
    `ir["stray"]` and `doc_sync.read_file` refuses the file until it is one the dialect
    can read whole. The same rule as `checks.lost_ink` and `doc_ir.unmodelled`: what
    nothing accounts for is said out loud.

    A value the dialect does not have — a `data-script` of neither `super`, `sub` nor
    `none`, a `data-off` naming no mark — is markup outside it, and ignored.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ir: Ir = {"title": "", "blocks": []}
        self.quiet = 0                       # open <script>/<style>: markup, not words
        self.block: Block | None = None
        self.marks: list[Style] = []         # style frames pushed by b/i/u/s/code/span/a
        self.lists: list[bool] = []          # one per open ul/ol: ordered?
        self.chip: Run | None = None
        self.in_title = False
        self.cell: list[Block] | None = None  # blocks of the table cell being read
        self.row: list[list[Block]] | None = None
        self.table: Block | None = None
        self.target: list[Block] = self.ir["blocks"]  # the blocks of the tab being read

    # -- helpers

    def _style(self) -> Style:
        style: Style = {}
        for frame in self.marks:
            apply_style(style, frame)
        return style

    def _emit(self, block: Block) -> None:
        if self.cell is not None:
            self.cell.append(block)
        else:
            self.target.append(block)

    def _lost(self, cell: Sequence[Block]) -> None:
        """The words of a cell no table takes. A `<td>` outside a `<tr>`, a `<tr>` whose
        `<table>` line is missing, a table the file never closes: the words are inside
        `<p>` tags, so `handle_data` sees a block and says nothing, and the row they are
        in is dropped - the class docstring's rule one level down, and the one the
        dialect's own multi-line table makes reachable (moving a table in the file is
        moving several lines, and moving one of them leaves exactly this).
        """
        for block in cell:
            if text := runs_text(block.get("runs", [])).strip():
                self.ir.setdefault("stray", []).append(" ".join(text.split()))

    def _open(self, block: Block) -> None:
        self.block = block

    def _close(self) -> None:
        if self.block is not None:
            self.block["runs"] = merge_runs(self.block.get("runs", []))
            self._emit(self.block)
            self.block = None

    # -- parser

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "meta":
            content = attr.get("content")
            if attr.get("name") == DOCUMENT_META and content:
                self.ir["document"] = content
            elif attr.get("name") == TAB_META and content:
                self.ir["tab_title"] = content
        elif tag == "title":
            self.in_title = True
        elif tag in ("script", "style"):
            self.quiet += 1
        elif tag == "section":
            self._close()
            part: Ir = {"title": attr.get("title") or "", "blocks": []}
            if data_tab := attr.get("data-tab"):
                part["tab"] = data_tab
            if data_parent := attr.get("data-parent"):
                part["parent"] = data_parent
            self.ir.setdefault("tabs", []).append(part)
            self.target = part["blocks"]
        elif tag in ("ul", "ol"):
            self.lists.append(tag == "ol")
        elif tag == "li":
            self._open(_said_block({"kind": "item", "level": max(0, len(self.lists) - 1),
                                    "ordered": bool(self.lists and self.lists[-1]),
                                    "runs": []}, attr))
        elif tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            if attr.get("class") == "b2s-toc":
                toc: Block = {"kind": "toc", "frozen": True, "runs": []}
                if key := attr.get("id"):
                    toc["key"] = key
                self._emit(toc)
                return
            if tag != "p":
                block: Block = {"kind": "heading", "level": int(tag[1]), "runs": []}
            else:
                named = attr.get("data-style") or ""
                block = {"kind": _STYLE_KINDS.get(named, "paragraph"), "runs": []}
            self._open(_said_block(block, attr))
        elif tag == "img":
            # A picture is a frozen run like a chip — one index unit, never rewritten
            # as text — that a sync can nevertheless create (`doc_merge.writable`).
            run: Run = {"chip": "image", "frozen": True, "text": ""}
            if src := attr.get("src"):
                run["src"] = src
            if alt := attr.get("alt"):
                run["alt"] = alt
            if title := attr.get("title"):
                run["title"] = title
            if data_object := attr.get("data-object"):
                run["value"] = data_object
            width, height = _pixels(attr.get("width")), _pixels(attr.get("height"))
            if width and height:
                run["size"] = [width, height]
            if self.block is None:
                # A picture on its own, outside any paragraph: it is one.
                self._open({"kind": "paragraph", "runs": [run]})
                self._close()
            else:
                self.block.setdefault("runs", []).append(run)
        elif tag == "table":
            self.table = {"kind": "table", "rows": []}
            if key := attr.get("id"):
                self.table["key"] = key
        elif tag == "tr":
            self.row = []
        elif tag == "td":
            self.cell = []
        elif tag in ("b", "strong"):
            self.marks.append({"bold": True})
        elif tag in ("i", "em"):
            self.marks.append({"italic": True})
        elif tag == "u":
            self.marks.append({"underline": True})
        elif tag in ("s", "strike", "del"):
            self.marks.append({"strike": True})
        elif tag == "code":
            self.marks.append({"code": True})
        elif tag in ("sup", "sub"):
            self.marks.append({"script": "super" if tag == "sup" else "sub"})
        elif tag == "a":
            self.marks.append({"link": attr.get("href") or ""})
        elif tag == "span":
            if attr.get("class") == "b2s-chip":
                chip: Run = {"chip": attr.get("data-chip") or "object", "frozen": True, "text": ""}
                if value := attr.get("data-value"):
                    chip["value"] = value
                if format_ := attr.get("data-format"):
                    chip["format"] = format_
                if locale := attr.get("data-locale"):
                    chip["locale"] = locale
                if mime := attr.get("data-mime"):
                    chip["mime"] = mime
                self.chip = chip
                self.marks.append({})
            else:
                frame = _span_style(attr.get("style") or "")
                if attr.get("data-smallcaps"):
                    frame["smallcaps"] = True
                if (script := _SCRIPTS_SAID.get(attr.get("data-script") or "")) is not None:
                    frame["script"] = script
                for said in (attr.get("data-off") or "").split():
                    if (mark := _MARKS_SAID.get(said)) is not None:
                        _set_mark(frame, mark, False)
                self.marks.append(frame)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag in ("script", "style"):
            self.quiet = max(0, self.quiet - 1)
        elif tag == "section":
            self._close()
            self.target = self.ir["blocks"]
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
        elif tag in ("li", "p", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._close()
        elif tag == "td":
            if self.row is not None:
                self.row.append(self.cell or [])
            else:
                self._lost(self.cell or [])
            self.cell = None
        elif tag == "tr":
            if self.table is not None and self.row is not None:
                self.table.setdefault("rows", []).append(self.row)
            elif self.row is not None:
                for cell in self.row:
                    self._lost(cell)
            self.row = None
        elif tag == "table":
            if self.table is not None:
                self._emit(self.table)
            self.table = None
        elif tag in ("b", "strong", "i", "em", "u", "s", "strike", "del", "code", "a",
                     "span", "sup", "sub"):
            if tag == "span" and self.chip is not None:
                chip, self.chip = self.chip, None
                if self.block is not None:
                    self.block.setdefault("runs", []).append(chip)
            if self.marks:
                self.marks.pop()

    @override
    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.ir["title"] = self.ir.get("title", "") + data.strip()
            return
        if self.chip is not None:
            self.chip["text"] += data
            return
        if self.block is None:
            # Words no block carries: the white space between the dialect's own lines is
            # layout and says nothing, anything else is a tag that went wrong (see the
            # class docstring). Kept whole here; whoever reports it decides how much of
            # it to quote.
            if data.strip() and not self.quiet:
                self.ir.setdefault("stray", []).append(" ".join(data.split()))
            return
        # Newlines in the file are layout, not content: the dialect puts one block per line.
        text = data.replace("\n", " ")
        runs = self.block.setdefault("runs", [])
        if not text.strip() and not runs:
            return
        run: Run = {"text": text}
        apply_style(run, self._style())
        runs.append(run)

    def finish(self) -> None:
        """What the end of the file leaves open. A `<table>` that is never closed keeps
        every row it has read, so its words go the way `_lost` describes - and so do an
        open `<td>` and `<tr>`, whose own end tags never came.
        """
        self._close()
        if self.cell is not None:
            self._lost(self.cell)
            self.cell = None
        for cell in self.row or []:
            self._lost(cell)
        self.row = None
        for row in (self.table.get("rows", []) if self.table is not None else []):
            for cell in row:
                self._lost(cell)
        self.table = None


def _pixels(value: str | None) -> int | None:
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*(px)?\s*$", value or "")
    return round(float(match.group(1))) if match else None


def _said_block(block: Block, attr: Mapping[str, str | None]) -> Block:
    """A block with what its tag says about the paragraph (`_paragraph_of`) and its
    identity (`id=`) put on."""
    apply_measures(block, _paragraph_of(attr))
    if key := attr.get("id"):
        block["key"] = key
    return block


def _css(style: str) -> Iterator[tuple[str, str]]:
    """The declarations of a `style=` attribute, lowercased property and raw value."""
    for piece in style.split(";"):
        key, _, value = piece.partition(":")
        if key.strip():
            yield key.strip().lower(), value.strip()


def _span_style(style: str) -> Style:
    out: Style = {}
    for key, value in _css(style):
        if key == "color":
            out["color"] = value
        elif key == "background-color":
            out["highlight"] = value
        elif key == "font-family":
            # Read as written: one name, no fallback list (`_run_html` says why).
            out["font"] = value.strip("'\"")
        elif key == "font-size":
            size = _length(value)
            if size:
                out["fontsize"] = size
    return out


def _paragraph_of(attr: Mapping[str, str | None]) -> Measures:
    """What a `<p>`, `<h*>` or `<li>` says about the paragraph itself.

    The CSS half of it is what the importer keeps; the `data-` half is what only
    `batchUpdate` can write (`PARAGRAPH_CSS`, `PARAGRAPH_DATA`).
    """
    out: Measures = {}
    css = {key: value for key, value in _css(attr.get("style") or "")}
    if (align := _ALIGNS.get(css.get("text-align", ""))) is not None:
        out["align"] = align
    for key, name in PARAGRAPH_CSS.items():
        if name in css:
            length = (_ratio(css[name]) if key == "line_spacing" else _length(css[name]))
            if length is not None:
                set_measure(out, key, length)
    for measure, name in PARAGRAPH_DATA.items():
        if said := attr.get(name):
            value: MeasureValue | None
            match measure:
                case "shading":
                    value = said
                case "border_top" | "border_bottom" | "border_left" | "border_right":
                    value = _border_text(said)
                case "page_break" | "keep_with_next":
                    value = True
                case "indent" | "indent_first" | "line_spacing" | "space_above" | "space_below":
                    value = _length(said)
                case _:
                    assert_never(measure)
            if value is not None:
                set_measure(out, measure, value)
    return out


def _border_text(said: str) -> str | None:
    """A border the file spells, read back — normalised, so the same rule always
    reads the same way. One this dialect cannot spell is no border at all rather
    than a guess, as a length in an unknown unit is."""
    match = BORDER_RE.match(said or "")
    if not match:
        return None
    width, dash, colour, pad = match.groups()
    out = f"{_number(float(width))}pt {dash} {colour.lower()}"
    return f"{out} pad {_number(float(pad))}pt" if pad and float(pad) else out


def _length(value: str) -> float | None:
    """A measurement in points. The dialect writes `pt` and nothing else, so a unit
    we don't know is read as no measurement at all rather than guessed at."""
    match = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*(pt)?\s*$", value or "")
    return round(float(match.group(1)), 2) if match else None


def _ratio(value: str) -> float | None:
    """A line height: the unitless multiplier Docs calls `lineSpacing` (× 100)."""
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*$", value or "")
    return round(float(match.group(1)), 3) if match else None


def from_html(html: str) -> Ir:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    reader.finish()
    return reader.ir


# ---------------------------------------------------------------- keys

def slug(text: str, limit: int = 40) -> str:
    return SLUG.sub("-", text.lower()).strip("-")[:limit] or "empty"


def key_blocks(ir: Ir) -> Ir:
    """Give every *unkeyed* block a key: kind, its first words, an occurrence count.

    The same rule as `identity.slide_key` — recognisable in a diff, and stable while
    the words are. A block that already has one keeps it, because that key came from
    the canonical file or from a named range, and both outrank a guess from the text.
    """
    for part in ir.get("tabs", []):
        key_blocks(part)  # a named range belongs to its tab: keys are unique per tab
    # Keys already in hand reserve their occurrence number, so a new block never
    # takes a name a keyed one is using.
    seen: dict[str, int] = {}
    for block in ir["blocks"]:
        if key := block.get("key"):
            stem, _, count = key.partition("#")
            seen[stem] = max(seen.get(stem, 0), int(count or 1))
    for block in ir["blocks"]:
        if block.get("key"):
            continue
        stem = f"{block['kind']}:{slug(_first_words(block))}"
        seen[stem] = seen.get(stem, 0) + 1
        block["key"] = stem if seen[stem] == 1 else f"{stem}#{seen[stem]}"
    return ir


def named_ranges_of(doc: Document, tab_id: str | None = None) -> dict[str, DocsNamedRanges]:
    """The document's named ranges, by name, from either read shape."""
    if "body" in doc:
        return doc.get("namedRanges", {}) or {}
    for tab in _flatten_tabs(doc.get("tabs", [])):
        if tab_id in (None, tab.get("tabProperties", {}).get("tabId")):
            return tab.get("documentTab", {}).get("namedRanges", {}) or {}
    return {}


def apply_keys(ir: Ir, named_ranges: Mapping[str, DocsNamedRanges]) -> Ir:
    """Give every block the key of the `b2s:` named range that starts inside it.

    This is the read half of identity: the file carries the key as an `id`, the
    document carries it as a range, and the two are compared by the merge. A range
    whose paragraph was split shows up twice; the first block wins, which is where
    the range begins and so where the text the key was given to still is.

    What no block takes is left on the IR as `orphans`, for `name_requests` to
    delete — see `orphan_requests` for why a range outliving its block is a key
    waiting to be stolen.
    """
    starts: list[tuple[int, int, str, str]] = []
    for name, entry in named_ranges.items():
        if not name.startswith(KEY_PREFIX):
            continue
        for ranged in entry.get("namedRanges", []):
            for span in ranged.get("ranges", []):
                starts.append((span.get("startIndex", 0), span.get("endIndex", 0),
                               name[len(KEY_PREFIX):], ranged.get("namedRangeId", "")))
    starts.sort()
    taken: set[str] = set()
    for block in ir["blocks"]:
        low, high = block.get("span", [U16(0), U16(0)])
        for start, end, key, range_id in starts:
            if low <= start < high and key not in taken:
                block["key"] = key
                block["rangeId"] = range_id
                # Where the range is, as against where `name_requests` would put it:
                # a range drifts (an insert at its first index pushes it along).
                block["range"] = [U16(start), U16(end)]
                taken.add(key)
                break
    held = {block.get("rangeId") for block in ir["blocks"]}
    orphans = [range_id for _, _, _, range_id in starts if range_id not in held]
    if orphans:
        ir["orphans"] = orphans
    else:
        ir.pop("orphans", None)
    return ir


def anchor_span(block: Block) -> list[U16] | None:
    """The text range a block's named range is planted over.

    A table's own span covers its rows and cells, which is not a run of text the API
    will name, so the range goes into its first cell instead: the table is then the
    block that *contains* the range, which is what `apply_keys` looks for.
    """
    if block["kind"] == "table":
        for row in block.get("rows", []):
            for cell in row:
                for inner in cell:
                    if span := inner.get("span"):
                        return span
        return None
    return block.get("span")


def anchor_range(block: Block) -> tuple[U16, U16] | None:
    """Where a keyed block's named range is planted: its anchor span short of the
    paragraph mark where it can be, so that deleting the newline between two
    paragraphs never silently stretches one block's identity over the other's
    words. An empty paragraph is all mark, so its range is that."""
    span = anchor_span(block) if block.get("key") else None
    if not span:
        return None
    low, high = span
    return low, U16(max(high - 1, low + 1))


def _create_range(key: str, planted: tuple[U16, U16]) -> DocsRequest:
    return {"createNamedRange": {"name": KEY_PREFIX + key,
                                 "range": {"startIndex": planted[0], "endIndex": planted[1]}}}


def replant_requests(ir: Ir) -> list[DocsRequest]:
    """The named ranges that drifted, planted again where `anchor_range` puts them.

    A range drifts: text written *at* its first index pushes it along (Docs' rule),
    so a chip or a word put into an empty paragraph leaves the range on the
    paragraph mark, where the next "\\ntext" appended at that mark takes it away
    with the new block and a structural batch that swallows the mark deletes it.
    A range also *stretches*: text written inside it grows it (Docs' rule again), and
    a block written at the end of the one before it — which is how a block appended to
    a body goes in, "\\ntext" at the last paragraph's mark — grows that block's range
    over the new block's words. Two blocks under one name is one block's key gone: the
    first of them wins in `apply_keys`, and a later delete of *that* one leaves the
    stretched range sitting on the other's words, whose own key it then takes
    (campaign seeds 279 and 361, chain 4). A range may end short of its block, since
    text typed at the mark falls outside it, but it may never end past it.

    The old range goes (`deleteNamedRange`) and the new one is planted, in that
    order, so a name is never carried twice. Nothing here moves an index, so these
    can head any batch planned against `ir`.
    """
    out: list[DocsRequest] = []
    for block in ir["blocks"]:
        planted = anchor_range(block)
        here = block.get("range")
        range_id = block.get("rangeId")
        key = block.get("key")
        if not planted or not range_id or not here or not key \
                or (here[0] == planted[0] and here[1] <= planted[1]):
            continue
        out.append({"deleteNamedRange": {"namedRangeId": range_id}})
        out.append(_create_range(key, planted))
    return out


def orphan_requests(ir: Ir) -> list[DocsRequest]:
    """`deleteNamedRange` for every `b2s:` range no block is known by.

    A range outlives the block it named, and a range with nothing of its own is a
    key waiting to be stolen. The commonest way to make one is the commonest edit
    after typing: a reader backspaces at the start of a paragraph, Docs merges it
    into the one above keeping the first one's style, and *both* ranges are now
    inside the one paragraph that survives. `apply_keys` keeps the first, the
    document reads right, and nothing is wrong — until a source edit rewrites the
    words the winner covers. Deleting them takes its range with them, the loser is
    all that is left, and the block is suddenly known by the name of the paragraph
    that was swallowed: the key the file asserts names nothing, and a checkout that
    has not settled yet reads one block gone and one added (chain-4 seed 70140).

    The mirror of `replant_requests`' stretched range, and the same cure: one block,
    one name. Nothing here moves an index, so these can head any batch.
    """
    return [{"deleteNamedRange": {"namedRangeId": range_id}}
            for range_id in ir.get("orphans", [])]


def name_requests(ir: Ir) -> list[DocsRequest]:
    """`createNamedRange` for every keyed block the document does not name yet — and
    again for one whose range drifted (`replant_requests`) or that no block is known
    by any more (`orphan_requests`)."""
    out = orphan_requests(ir) + replant_requests(ir)
    for block in ir["blocks"]:
        planted = anchor_range(block)
        key = block.get("key")
        if not planted or not key or block.get("rangeId"):
            continue
        out.append(_create_range(key, planted))
    return out


def _first_words(block: Block) -> str:
    if block["kind"] == "table":
        for row in block.get("rows", []):
            for cell in row:
                for inner in cell:
                    if runs_text(inner.get("runs", [])).strip():
                        return runs_text(inner.get("runs", []))
        return ""
    words = runs_text(block.get("runs", []))
    if not words.strip():
        # A paragraph that is a picture is named after the picture's file.
        for run in block.get("runs", []):
            if run.get("chip") == "image" and (run.get("src") or run.get("alt")):
                name = run.get("src") or ""
                return (re.sub(r"\.\w+$", "", name.rsplit("/", 1)[-1]) if name
                        else run.get("alt", ""))
    return words


# ---------------------------------------------------------------- what we do not read

# Every part of a `documents.get` answer this module consults, as a small graph: a
# node's `read` is what the reader takes the value of, and `into` maps a key to the
# node its value is — `[]` for a list of them, `{}` for a map of them. Anything a
# document carries that is in neither is what `unmodelled` reports.
#
# This is the other half of the convergence check. "A second sync writes 0 requests"
# is measured on the IR, so it proves the IR round-trips and says exactly nothing
# about what the IR never looked at: a property no node below names is a property a
# rewrite drops without a word. It is the same question `checks.lost_ink` asks of a
# converted slide — does every difference lie on something we account for? — and the
# same answer: name what is left over, out loud, rather than trust that there is none.
#
# Precision is the point, so a node is split wherever the reader reads less than the
# whole of it: a named style's textStyle is `namedTextStyle`, which reads the face, the
# size and the marks — not its colour, which nothing subtracts.
_NODES: dict[str, tuple[tuple[str, ...], dict[str, str]]] = {
    "document": (("documentId", "title", "revisionId", "suggestionsViewMode"),
                 {"body": "body", "tabs": "tab[]", "lists": "lists{}",
                  "inlineObjects": "inlineObject{}", "namedStyles": "namedStyles",
                  "namedRanges": "namedRangeGroup{}"}),
    "tab": ((), {"tabProperties": "tabProperties", "documentTab": "documentTab",
                 "childTabs": "tab[]"}),
    "tabProperties": (("tabId", "title", "parentTabId", "index", "nestingLevel"), {}),
    "documentTab": ((), {"body": "body", "lists": "lists{}",
                         "inlineObjects": "inlineObject{}", "namedStyles": "namedStyles",
                         "namedRanges": "namedRangeGroup{}"}),
    "body": ((), {"content": "structural[]"}),
    "structural": (("startIndex", "endIndex"),
                   {"paragraph": "paragraph", "table": "table",
                    "tableOfContents": "toc"}),
    # A table of contents is kept whole and frozen, span and all: what it generates is
    # deliberately opaque, not overlooked.
    "toc": (("content",), {}),
    "paragraph": ((), {"elements": "element[]", "paragraphStyle": "paragraphStyle",
                       "bullet": "bullet"}),
    "paragraphStyle": (("namedStyleType", "alignment", "indentStart", "indentFirstLine",
                        "lineSpacing", "spaceAbove", "spaceBelow", "shading",
                        "pageBreakBefore", "keepWithNext") + tuple(BORDER_SIDES.values()),
                       {}),
    "bullet": (("listId", "nestingLevel"), {}),
    "element": (("startIndex", "endIndex"),
                {"textRun": "textRun", "dateElement": "dateElement", "person": "person",
                 "richLink": "richLink", "footnoteReference": "footnoteReference",
                 "equation": "equation", "inlineObjectElement": "inlineObjectElement",
                 "horizontalRule": "horizontalRule"}),
    "textRun": (("content",), {"textStyle": "textStyle"}),
    "textStyle": (("bold", "italic", "underline", "strikethrough", "smallCaps",
                   "baselineOffset", "weightedFontFamily", "fontSize",
                   "foregroundColor", "backgroundColor", "link"), {}),
    "table": (("rows", "columns"), {"tableRows": "tableRow[]"}),
    "tableRow": (("startIndex", "endIndex"), {"tableCells": "tableCell[]"}),
    "tableCell": (("startIndex", "endIndex"), {"content": "structural[]"}),
    # Chips: what identifies each one, and nothing about how it is drawn.
    "dateElement": ((), {"dateElementProperties": "dateProperties"}),
    "dateProperties": (("displayText", "timestamp", "dateFormat", "locale"), {}),
    "person": ((), {"personProperties": "personProperties"}),
    "personProperties": (("name", "email"), {}),
    "richLink": ((), {"richLinkProperties": "richLinkProperties"}),
    "richLinkProperties": (("title", "uri", "mimeType"), {}),
    "footnoteReference": (("footnoteNumber", "footnoteId"), {}),
    # An equation's LaTeX is in no field at all — it comes from the Markdown export
    # (`equation_spots`, `latex_of`) — so there is nothing here to read.
    "equation": ((), {}),
    "inlineObjectElement": (("inlineObjectId",), {}),
    "horizontalRule": ((), {}),
    "inlineObject": (("objectId",), {"inlineObjectProperties": "objectProperties"}),
    "objectProperties": ((), {"embeddedObject": "embeddedObject"}),
    "embeddedObject": (("title", "description"),
                       {"size": "size", "imageProperties": "imageProperties"}),
    "size": ((), {"width": "dimension", "height": "dimension"}),
    "dimension": (("magnitude", "unit"), {}),
    "imageProperties": (("contentUri",), {}),
    "lists": ((), {"listProperties": "listProperties"}),
    "listProperties": ((), {"nestingLevels": "nestingLevel[]"}),
    "nestingLevel": (("glyphSymbol", "glyphType"), {}),
    "namedStyles": ((), {"styles": "namedStyle[]"}),
    "namedStyle": (("namedStyleType",), {"textStyle": "namedTextStyle",
                                         "paragraphStyle": "namedParagraphStyle"}),
    "namedTextStyle": (("weightedFontFamily", "fontSize")
                       + tuple(api for _, api in MARK_FIELDS), {}),
    "namedParagraphStyle": (("alignment", "indentStart", "indentFirstLine", "lineSpacing",
                             "spaceAbove", "spaceBelow", "shading", "pageBreakBefore",
                             "keepWithNext") + tuple(BORDER_SIDES.values()), {}),
    "namedRangeGroup": (("name",), {"namedRanges": "namedRange[]"}),
    "namedRange": (("namedRangeId", "name"), {"ranges": "range[]"}),
    "range": (("startIndex", "endIndex", "segmentId", "tabId"), {}),
}


def unmodelled(doc: Mapping[str, object]) -> dict[str, Unmodelled]:
    """Everything a live document carries that this module never reads.

    A `documents.get` answer, walked against `_NODES`; the answer is, by the path
    each was found at, how many there were and one example. It is what a canonical
    file cannot say and therefore what a sync would drop if it ever rewrote the
    block holding it — the report `adopt` owes whoever hands us a document somebody
    else made, and the test that pins it is how a property Docs adds later shows up
    as a failure rather than as silence.

    A path reads like `structural.paragraph.paragraphStyle.borderLeft`. Empty values
    say nothing and are left out, so a document that sets none of a struct's fields
    does not report the struct.
    """
    found: dict[str, Unmodelled] = {}
    _walk(doc, "document", "", found)
    return dict(sorted(found.items()))


def _fields(value: object) -> list[tuple[str, object]]:
    """The fields of a JSON object; none for anything else."""
    if isinstance(value, Mapping):
        return [(str(key), inner) for key, inner in value.items()]
    return []


def _items(value: object) -> list[object]:
    """The items of a JSON array, or the values of a JSON object."""
    if isinstance(value, list):
        return list(value)
    return [inner for _, inner in _fields(value)]


def _walk(value: object, node: str, path: str, found: dict[str, Unmodelled]) -> None:
    read, into = _NODES[node]
    for key, inner in _fields(value):
        here = f"{path}.{key}".lstrip(".")
        if key in read or _nothing(inner):
            continue
        if key in into:
            target = into[key]
            # A list or a map starts the path again at its node's name, so the
            # structural elements of a table cell and those of the body report at one
            # path and the recursion does not make the answer infinite.
            if target.endswith("[]") or target.endswith("{}"):
                for item in _items(inner):
                    _walk(item, target[:-2], target[:-2], found)
            else:
                _walk(inner, target, here, found)
            continue
        entry = found.setdefault(here, {"count": 0, "example": _example(inner)})
        entry["count"] += 1


def unmodelled_in(element: Mapping[str, object]) -> dict[str, Unmodelled]:
    """`unmodelled` for one structural element: what a rewrite of *this block* drops.

    The document-wide answer is a count with no address, which is the right thing to
    print once and the wrong thing to act on. A property no node of `_NODES` names
    survives an ordinary edit — a request names the fields it writes — and goes when
    the block holding it is written again from nothing, so the question a person
    actually has is not "does this document carry borders" but "is the paragraph I am
    about to rewrite the one with the border on it".
    """
    found: dict[str, Unmodelled] = {}
    _walk(element, "structural", "structural", found)
    return dict(sorted(found.items()))


def unread_blocks(doc: Document) -> list[UnreadBlock]:
    """Every block of every tab that carries something `unmodelled_in` names.

    The most heavily laden first, each with its tab, its span in that tab, its first
    words and what it carries. It is a *risk*, not a loss: a block nobody rewrites
    keeps all of it.
    """
    out: list[UnreadBlock] = []
    tabs: list[DocsTab | None] = [*tabs_of(doc)] or [None]
    for tab in tabs:
        tab_id = tab.get("tabProperties", {}).get("tabId") if tab else None
        body, _ = _body_of(doc, tab_id)
        for element in body:
            found = unmodelled_in(element)
            block = _block_of(element, {}, {}, {})
            # A section break is not a block: no rewrite can reach it, and naming one
            # for it would send a person to a paragraph that is not the one.
            if not found or block is None:
                continue
            out.append({"tab": tab_id,
                        "span": [element.get("startIndex", 0), element.get("endIndex", 0)],
                        "kind": block["kind"],
                        "words": _first_words(block).strip(),
                        "unread": found})
    out.sort(key=lambda e: (-sum(v["count"] for v in e["unread"].values()), e["span"]))
    return out


def _nothing(value: object) -> bool:
    """A value that says nothing at all: Docs leaves plenty of empty structs about."""
    return value is None or value == {} or value == [] or value is False


def _example(value: object) -> str:
    """One example of an unmodelled value, short enough to print in a report."""
    if isinstance(value, Mapping):
        return "{" + ", ".join(sorted(key for key, _ in _fields(value))[:4]) + "}"
    if isinstance(value, list):
        return f"[{len(value)}]"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."

