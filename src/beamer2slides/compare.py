"""IR vs IR: what differs between a current deck (classify of a candidate source) and a target
(classify of another PDF, a synthetic edit, or `deck_ir` of a live Slides deck).

Both sides are deck.json-shaped. Text is compared by paragraph across the whole slide (the
classifier may group paragraphs into boxes differently than the deck does), elements by the
paragraphs they hold; pictures and shapes by kind, box and picture hash.

Residual kinds: slide_missing, slide_extra, slide_order, notes, background, paragraph_missing,
paragraph_extra, paragraph_order, text, style, bullet, align, element_missing, element_extra,
geometry, image, shape, table, diagram: one record each (`Residual`, written as JSON by
`residual_json`). Every residual carries `within` (inside the tolerance).

What compare reads it reads into records of its own (`SlideView` and the element views), not into
`ir_types`: the two sides are not one stage of deck.json. The current side is classify's, with the
frame label of each slide beside it (`Current`: a pull candidate knows them from SyncTeX, and the
IR has no place for them); the target is often `deck_ir`'s reading of a live deck (a Slides box's `anchor`, `box` and
`wrap_width`, tables as rows of strings, keys like `objectId` and `shape_type`), which the IR does
not model. So a deck given as a dict is read here key by key, as far as compare reads it, and a
deck already parsed (`ir_types.Deck` or `RenderedDeck`) is written back to its JSON and read the
same way, each element keeping its identity for `hashes`.
"""

import bisect
import dataclasses
import difflib
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar, Union

from .classify import FRAME_COUNTER_RE
from .emit import merge_blocks
from .fonts import google_font
from .deck_ir_types import TargetDeck, target_json
from .ir_types import At, Box, Deck, Parse, RenderedDeck, box, deck_json, integer, number, one_of, pair, point, string
from .json_types import Json, JsonArray, JsonObject, JsonShapeError, as_array, as_object, as_objects, as_str
from .typing_compat import assert_never

if TYPE_CHECKING:
    from PIL import Image

TOL: dict[str, float] = {
    "pos": 2.0,          # PDF pt: text anchors, picture edges
    "size": 3.0,         # PDF pt: picture width and height
    "font": 0.06,        # relative font size
    "color": 24,         # summed RGB channel difference (0..765)
    "phash": 0.15,       # mean abs difference of 16x16 grey thumbnails (0..1)
    "inline_phash": 0.3,  # the same for formula/icon pictures (transparent, on coloured panels)
}
IGNORED_ROLES = frozenset({"footer", "math", "icon", "overlay", "highlight"})
NORMALISE = str.maketrans({" ": " ", " ": " ", " ": " ", "\t": " ", "\x0b": " ", "“": '"', "”": '"',
                           "‘": "'", "’": "'", "…": "...", "−": "-", "​": ""})
HOLE = ""

T = TypeVar("T")
U = TypeVar("U")


def norm_text(t: str) -> str:
    return " ".join(unicodedata.normalize("NFC", t.translate(NORMALISE)).split())


# ---------------------------------------------------------------- deck.json as a dict, read loosely
# These read what a caller hands over as dicts (inverse's slides, a test's fragment of one): only
# the keys they need, a missing one a KeyError where the value is needed.

def run_text(r: JsonObject) -> str:
    return HOLE if r.get("hole") else as_str(r["text"], "run text")


def para_text(p: JsonObject) -> str:
    return "".join(run_text(r) for r in as_objects(p["runs"], "paragraph runs"))


def element_text(el: JsonObject) -> str:
    return "\n".join(para_text(p) for p in as_objects(el.get("paragraphs", []), "element paragraphs"))


def _get(o: JsonObject, key: str, parse: Parse[T], where: str) -> T | None:
    """`o.get(key)` read as `parse` reads it: absent and null are both None."""
    v = o.get(key)
    return None if v is None else parse(v, At(where=where, path=key))


def _need(o: JsonObject, key: str, parse: Parse[T], where: str) -> T:
    """`o[key]`: a KeyError when it is missing, as the dict read was."""
    return parse(o[key], At(where=where, path=key))


def _opt_str(v: Json, where: str) -> str | None:
    if v is None or isinstance(v, str):
        return v
    raise JsonShapeError(f"{where}: a string was expected, found {type(v).__name__}")


def _opt_box(v: Json, where: str) -> Box | None:
    """A box where an empty or null one is none (a bullet's `bbox` when `deck_ir` has none)."""
    return box(v, At(where=where, path="bbox")) if v else None


def _level(p: JsonObject, where: str) -> int:
    return integer(p.get("level", 0), At(where=where, path="level"))


def similarity(a: str, b: str) -> float:
    wa, wb = norm_text(a).split(), norm_text(b).split()
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def colour_distance(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 0 if a == b else 765
    a, b = a.lstrip("#"), b.lstrip("#")
    return sum(abs(int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) for i in (0, 2, 4))


# ---------------------------------------------------------------- what compare reads of a slide

@dataclass(frozen=True, kw_only=True)
class RunView:
    text: str              # HOLE for a formula hole
    font: str
    bold: bool
    italic: bool
    underline: bool
    color: str             # lower case, black when the run says none
    size: float | None     # None (or 0): the paragraph's
    mono: bool
    script: bool


@dataclass(frozen=True, kw_only=True)
class BulletView:
    kind: str | None
    text: str
    bbox: Box | None       # deck_ir knows none for a bullet Slides draws itself


@dataclass(frozen=True, kw_only=True)
class LineView:
    x0: float
    x1: float
    baseline: float | None  # deck_ir: None where the thumbnail showed no baseline


@dataclass(frozen=True, kw_only=True)
class ParaView:
    text: str              # the runs' text, a hole as HOLE
    runs: tuple[RunView, ...]
    size: float | None
    align: str
    level: int
    bullet: BulletView | None
    text_x0: float
    lines: tuple[LineView, ...]


@dataclass(frozen=True, kw_only=True)
class TextBox:
    """How `deck_ir`'s Slides box holds its lines: its vertical alignment and the span from the
    first baseline to the last (`deck_ir.stacked_baseline`)."""
    valign: str | None
    span: float | None


@dataclass(frozen=True, kw_only=True)
class TextView:
    id: str
    role: str | None
    mark: str | None
    bbox: Box
    paragraphs: tuple[ParaView, ...]
    anchor: tuple[float, float] | None   # deck_ir: (x, first baseline) of the Slides box
    box: TextBox | None                  # deck_ir only
    wrap_width: float | None             # deck_ir only


@dataclass(frozen=True, kw_only=True)
class Placed:
    """An element compared by its box: a picture, shape, table or diagram."""
    ref: int | None        # id() of what it was read from: the key its picture has in `hashes`
    id: str
    role: str | None
    mark: str | None
    bbox: Box
    fill: str | None
    file: str | None


@dataclass(frozen=True, kw_only=True)
class ImageView(Placed):
    pass


@dataclass(frozen=True, kw_only=True)
class ShapeView(Placed):
    pass


@dataclass(frozen=True, kw_only=True)
class TableView(Placed):
    cells: tuple[tuple[str, ...], ...]   # `table_text`


@dataclass(frozen=True, kw_only=True)
class DiagramView(Placed):
    texts: tuple[str, ...]               # `diagram_text`


ElementView = Union[TextView, ImageView, ShapeView, TableView, DiagramView]
BoxView = Union[ImageView, ShapeView, TableView, DiagramView]
B = TypeVar("B", ImageView, ShapeView, TableView, DiagramView)
Kind = Literal["text", "image", "shape", "table", "diagram"]
KINDS: tuple[Kind, ...] = ("text", "image", "shape", "table", "diagram")


@dataclass(frozen=True, kw_only=True)
class SlideSummary:
    """What pairs slides: their keys, and their title, text and kinds of elements."""
    key: str | None
    object_id: str | None
    title: str
    fingerprint: str
    kinds: tuple[str, ...]      # sorted, of the elements not in IGNORED_ROLES


@dataclass(frozen=True, kw_only=True)
class SlideView:
    summary: SlideSummary
    size: tuple[float, float] | None
    notes: str | None
    background_color: str | None
    elements: tuple[ElementView, ...]
    shapes: tuple[ShapeView, ...]   # as emit writes them: a block's body reaching up under its title bar


def run_view(r: JsonObject, where: str) -> RunView:
    return RunView(text=run_text(r), font=_get(r, "font", string, where) or "", bold=bool(r.get("bold")),
                   italic=bool(r.get("italic")), underline=bool(r.get("underline")),
                   color=(_get(r, "color", string, where) or "#000000").lower(), size=_get(r, "size", number, where),
                   mono=r.get("family") == "mono", script=bool(r.get("script")))


def runs_of(p: JsonObject, where: str) -> tuple[RunView, ...]:
    return tuple(run_view(r, where) for r in as_objects(p["runs"], f"{where}: runs"))


def bullet_view(v: Json, where: str) -> BulletView | None:
    """None for a paragraph without one (an empty bullet included, as `p.get("bullet")` reads)."""
    if not v:
        return None
    b = as_object(v, f"{where}: bullet")
    return BulletView(kind=_get(b, "kind", string, where), text=str(b.get("text", "")),
                      bbox=_opt_box(b.get("bbox"), where))


def line_view(ln: JsonObject, where: str) -> LineView:
    return LineView(x0=_need(ln, "x0", number, where), x1=_need(ln, "x1", number, where),
                    baseline=_get(ln, "baseline", number, where))


def para_view(p: JsonObject, where: str) -> ParaView:
    runs = runs_of(p, where)
    return ParaView(text="".join(r.text for r in runs), runs=runs, size=_get(p, "size", number, where),
                    align=_need(p, "align", string, where), level=_level(p, where),
                    bullet=bullet_view(p.get("bullet"), where), text_x0=_need(p, "text_x0", number, where),
                    lines=tuple(line_view(ln, where) for ln in as_objects(p["lines"], f"{where}: lines")))


def anchor_value(v: Json, where: str) -> tuple[float, float] | None:
    """A text element's `anchor` (`deck_ir`), none when it is absent, null or empty."""
    return point(v, At(where=where, path="anchor")) if v else None


def text_box(v: Json, where: str) -> TextBox | None:
    """`deck_ir`'s `box` of a text element; a wordart's box is a list, and says nothing here."""
    if not isinstance(v, dict):
        return None
    return TextBox(valign=_get(v, "valign", string, where), span=_get(v, "span", number, where))


def text_view(e: JsonObject, where: str) -> TextView:
    return TextView(id=_need(e, "id", string, where), role=_get(e, "role", string, where),
                    mark=_get(e, "mark", string, where), bbox=_need(e, "bbox", box, where),
                    paragraphs=tuple(para_view(p, where) for p in as_objects(e.get("paragraphs", []), where)),
                    anchor=anchor_value(e.get("anchor"), where), box=text_box(e.get("box"), where),
                    wrap_width=_get(e, "wrap_width", number, where))


def _placed(e: JsonObject, ref: int | None, where: str) -> tuple[int | None, str, str | None, str | None, Box,
                                                                 str | None, str | None]:
    return (ref, _need(e, "id", string, where), _get(e, "role", string, where), _get(e, "mark", string, where),
            _need(e, "bbox", box, where), _get(e, "fill", string, where), _get(e, "file", string, where))


def shape_view(e: JsonObject, ref: int | None, where: str) -> ShapeView:
    ref_, id_, role, mark, bbox, fill, file = _placed(e, ref, where)
    return ShapeView(ref=ref_, id=id_, role=role, mark=mark, bbox=bbox, fill=fill, file=file)


def element_view(e: JsonObject, ref: int, where: str) -> ElementView:
    kind = _need(e, "kind", one_of(KINDS), where)
    if kind == "text":
        return text_view(e, where)
    ref_, id_, role, mark, bbox, fill, file = _placed(e, ref, where)
    if kind == "image":
        return ImageView(ref=ref_, id=id_, role=role, mark=mark, bbox=bbox, fill=fill, file=file)
    if kind == "shape":
        return ShapeView(ref=ref_, id=id_, role=role, mark=mark, bbox=bbox, fill=fill, file=file)
    if kind == "table":
        return TableView(ref=ref_, id=id_, role=role, mark=mark, bbox=bbox, fill=fill, file=file,
                         cells=tuple(tuple(row) for row in table_text(e)))
    if kind == "diagram":
        return DiagramView(ref=ref_, id=id_, role=role, mark=mark, bbox=bbox, fill=fill, file=file,
                           texts=tuple(diagram_text(e)))
    assert_never(kind)


def slide_summary(s: JsonObject) -> SlideSummary:
    elements = as_objects(s["elements"], "slide elements")
    return SlideSummary(key=_opt_str(s.get("key"), "slide key"), object_id=_opt_str(s.get("objectId"), "slide objectId"),
                        title=slide_title(s), fingerprint=slide_fingerprint(elements),
                        kinds=tuple(sorted(as_str(e["kind"], "element kind") for e in elements
                                           if e.get("role") not in IGNORED_ROLES)))


def slide_view(s: JsonObject, refs: Sequence[int], where: str) -> SlideView:
    """A slide as compare reads it; `refs` are its elements' identities, in order (the keys of
    `hashes`)."""
    elements = as_objects(s["elements"], f"{where}: elements")
    views = tuple(element_view(e, ref, f"{where} element {i}") for i, (e, ref) in enumerate(zip(elements, refs)))
    by_id = {id(e): ref for e, ref in zip(elements, refs)}
    # a merged block body is a new dict: it has no picture in `hashes`
    shapes = tuple(shape_view(e, by_id.get(id(e)), f"{where} shape") for e in merge_blocks(elements)
                   if e["kind"] == "shape")
    size = s.get("size")
    return SlideView(summary=slide_summary(s), size=pair(size, At(where=where, path="size")) if size else None,
                     notes=_opt_str(s.get("notes"), f"{where}: notes"),
                     background_color=_opt_str(s.get("background_color"), f"{where}: background_color"),
                     elements=views, shapes=shapes)


DeckInput = Union[JsonObject, Deck, RenderedDeck, TargetDeck]
"""A side of a comparison: deck.json as a dict or parsed, or `deck_ir`'s read of a deck (a pull's
target, `deck_ir_types`)."""


def deck_views(deck: DeckInput, side: str) -> list[SlideView]:
    """Each slide of a deck given as deck.json's dict, or parsed (deck.json's types or a target's):
    written back to its JSON and read the same way, its elements named by the typed values'
    identities (what a caller holding them keys `hashes` by)."""
    if isinstance(deck, dict):
        slides = as_objects(deck["slides"], f"{side} slides")
        return [slide_view(s, [id(e) for e in as_objects(s["elements"], f"{side} slide {i}")], f"{side} slide {i}")
                for i, s in enumerate(slides)]
    data = target_json(deck) if isinstance(deck, TargetDeck) else deck_json(deck)
    slides = as_objects(data["slides"], f"{side} slides")
    return [slide_view(s, [id(e) for e in typed.elements], f"{side} slide {i}")
            for i, (s, typed) in enumerate(zip(slides, deck.slides))]


# ---------------------------------------------------------------- geometry

def text_left(el: TextView) -> float:
    """Left edge of an element's text as emit places it (bullets included)."""
    return min(p.bullet.bbox[0] if p.bullet is not None and p.bullet.bbox is not None else
               min([p.text_x0] + ([ln.x0 for ln in p.lines] if p.align != "left" else [])) for p in el.paragraphs)


def anchor_of(el: TextView) -> tuple[float, float | None]:
    """(x, first baseline) in PDF pt; x is the left edge, the centre or the right edge by the
    paragraphs' alignment. `deck_ir` sets `anchor` from the Slides box instead. The baseline is
    None where `deck_ir` read none. An element without lines has no anchor: a ValueError, or an
    IndexError when its first paragraph has none."""
    if el.anchor is not None:
        return el.anchor
    ps = el.paragraphs
    aligns = {p.align for p in ps}
    left = text_left(el)
    right = max(ln.x1 for p in ps for ln in p.lines)
    x = (left + right) / 2 if aligns == {"center"} else right if aligns == {"right"} else left
    return x, ps[0].lines[0].baseline


def text_anchor(el: JsonObject | None) -> tuple[float, float]:
    """`anchor_of` a text element given as a dict (inverse places boxes by it). A baseline
    `deck_ir` could not read is a TypeError here, where the arithmetic on it used to fail; so is no
    element at all (inverse's lookup of one can come back empty)."""
    if el is None:
        raise TypeError("no text element to anchor")
    x, y = anchor_of(text_view(el, "text element"))
    if y is None:
        raise TypeError(f"text element {el.get('id')}: its first line has no baseline")
    return x, y


def anchor_align(el: TextView) -> str:
    aligns = {p.align for p in el.paragraphs}
    return aligns.pop() if len(aligns) == 1 else "left"


# ---------------------------------------------------------------- alignment helpers

def align_sequences(a: Sequence[T], b: Sequence[U], sim: Callable[[T, U], float],
                    threshold: float) -> list[tuple[int | None, int | None]]:
    """Order-preserving alignment (Needleman-Wunsch) maximising summed similarity over pairs
    at or above `threshold`."""
    n, m = len(a), len(b)
    s = [[sim(x, y) for y in b] for x in a]
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best = max(score[i + 1][j], score[i][j + 1])
            if s[i][j] >= threshold:
                best = max(best, s[i][j] + score[i + 1][j + 1])
            score[i][j] = best
    out: list[tuple[int | None, int | None]] = []
    i, j = 0, 0
    while i < n and j < m:
        if s[i][j] >= threshold and abs(score[i][j] - (s[i][j] + score[i + 1][j + 1])) < 1e-9:
            out.append((i, j))
            i, j = i + 1, j + 1
        elif abs(score[i][j] - score[i + 1][j]) < 1e-9:
            out.append((i, None))
            i += 1
        else:
            out.append((None, j))
            j += 1
    out += [(k, None) for k in range(i, n)]
    out += [(None, k) for k in range(j, m)]
    return out


def longest_increasing(values: Sequence[int]) -> set[int]:
    """Indices (into values) of one longest strictly increasing subsequence."""
    if not values:
        return set()
    tails: list[int] = []
    prev = [-1] * len(values)
    for i, v in enumerate(values):
        k = bisect.bisect_left([values[t] for t in tails], v)
        if k == len(tails):
            tails.append(i)
        else:
            tails[k] = i
        prev[i] = tails[k - 1] if k else -1
    out: set[int] = set()
    i = tails[-1]
    while i >= 0:
        out.add(i)
        i = prev[i]
    return out


# ---------------------------------------------------------------- slides

def slide_title(slide: JsonObject) -> str:
    for e in as_objects(slide["elements"], "slide elements"):
        if e["kind"] == "text" and e.get("role") == "title":
            return norm_text(element_text(e))
    return ""


def slide_fingerprint(elements: Iterable[JsonObject]) -> str:
    return " ".join(norm_text(element_text(e)) for e in elements
                    if e["kind"] == "text" and e.get("role") not in IGNORED_ROLES)


def slide_similarity(a: SlideSummary, b: SlideSummary) -> float:
    title = similarity(a.title, b.title) if (a.title or b.title) else 0.5
    body = similarity(a.fingerprint, b.fingerprint)
    shape = difflib.SequenceMatcher(None, a.kinds, b.kinds).ratio()
    return 0.45 * title + 0.4 * body + 0.15 * shape


def target_keys(tgt: Sequence[SlideSummary]) -> list[str | None]:
    """Each target slide's key, and for a slide of a deck nobody converted (no key: `deck_ir`
    reads one only from alt-text tags or a base) the label `adopt.frame_labels` gave its frame.
    Without it a foreign deck's slides paired by title and text alone: jeb-arch's six untitled
    slides and 19 of sc-memphis' 20 found no mate, and the loop deleted every one of those frames
    and wrote a bare flow frame for each slide it thought missing."""
    keys = [s.key for s in tgt]
    if all(keys) or not any(s.object_id for s in tgt):
        return keys
    from .adopt import frame_labels
    named: list[Json] = [{"objectId": s.object_id, "key": s.key} for s in tgt]
    return [k or label for k, label in zip(keys, frame_labels({"slides": named}))]


def pair_slides(cur: Sequence[SlideSummary], tgt: Sequence[SlideSummary]) -> list[tuple[int | None, int | None]]:
    """Pairs (current index, target index): equal keys first, the rest by an order-preserving
    alignment of title and text similarity."""
    pairs: dict[int, int] = {}
    ckeys = {s.key: i for i, s in enumerate(cur) if s.key}
    for j, key in enumerate(target_keys(tgt)):
        if key and key in ckeys and ckeys[key] not in pairs:
            pairs[ckeys[key]] = j
    free_c = [i for i in range(len(cur)) if i not in pairs]
    free_t = [j for j in range(len(tgt)) if j not in pairs.values()]
    for a, b in align_sequences([cur[i] for i in free_c], [tgt[j] for j in free_t], slide_similarity, 0.45):
        if a is not None and b is not None:
            pairs[free_c[a]] = free_t[b]
    # slides moved past others: clearly similar leftovers pair up out of order (slide_order)
    free_c = [i for i in range(len(cur)) if i not in pairs]
    free_t = [j for j in range(len(tgt)) if j not in pairs.values()]
    cands = sorted(((slide_similarity(cur[i], tgt[j]), i, j) for i in free_c for j in free_t), reverse=True)
    for score, i, j in cands:
        if score >= 0.6 and i not in pairs and j not in pairs.values():
            pairs[i] = j
    out: list[tuple[int | None, int | None]] = [(i, j) for i, j in pairs.items()]
    out += [(i, None) for i in range(len(cur)) if i not in pairs]
    out += [(None, j) for j in range(len(tgt)) if j not in pairs.values()]
    return out


def match_slides(cur: Sequence[JsonObject], tgt: Sequence[JsonObject]) -> list[tuple[int | None, int | None]]:
    """`pair_slides` of slides given as dicts."""
    return pair_slides([slide_summary(s) for s in cur], [slide_summary(s) for s in tgt])


# ---------------------------------------------------------------- paragraphs

@dataclass(frozen=True, kw_only=True)
class SlidePara:
    el: TextView
    ei: int        # element index in the slide
    pi: int        # paragraph index in the element
    order: int     # reading order on the slide
    text: str

    @property
    def p(self) -> ParaView:
        return self.el.paragraphs[self.pi]


@dataclass(frozen=True, kw_only=True)
class Para:
    """A `SlidePara` of a slide held as a dict (inverse finds each in its source by `el["id"]`)."""
    el: JsonObject
    ei: int
    pi: int
    order: int
    text: str

    @property
    def p(self) -> JsonObject:
        return as_objects(self.el["paragraphs"], "paragraphs")[self.pi]


def counts_as_text(el: TextView) -> bool:
    """Text elements that are compared: not footers (frame counters "3 / 9" included, which a live
    deck without keys can't tell apart), formulas or overlays. A title is never a counter: a slide
    whose title is a big "7" had it left out, so the loop deleted it and called the slide converged."""
    if el.role in IGNORED_ROLES:
        return False
    return el.role == "title" or not FRAME_COUNTER_RE.match(norm_text(view_text(el)))


def view_text(el: TextView) -> str:
    return "\n".join(p.text for p in el.paragraphs)


def compared_texts(slide: SlideView) -> list[tuple[int, TextView]]:
    return [(i, e) for i, e in enumerate(slide.elements) if isinstance(e, TextView) and counts_as_text(e)]


def reading_order(slide: SlideView) -> list[tuple[int, TextView]]:
    """Text elements, title first, then top to bottom and left to right (a live deck lists its
    elements in z-order)."""
    def key(item: tuple[int, TextView]) -> tuple[bool, int, float]:
        e = item[1]
        try:
            x, y = anchor_of(e)
        except (IndexError, ValueError):
            x, y = e.bbox[0], e.bbox[1]
        return (e.role != "title", round((y or 0) / 4), x or 0)
    return sorted([(i, e) for i, e in compared_texts(slide) if e.paragraphs], key=key)


def paragraphs_of(slide: SlideView) -> list[SlidePara]:
    out: list[SlidePara] = []
    for ei, el in reading_order(slide):
        for pi, p in enumerate(el.paragraphs):
            text = norm_text(p.text)
            if text.replace(HOLE, "").strip():
                out.append(SlidePara(el=el, ei=ei, pi=pi, order=len(out), text=text))
    return out


def slide_paragraphs(slide: JsonObject) -> list[Para]:
    """`paragraphs_of` a slide given as a dict, each naming its element's dict."""
    elements = as_objects(slide["elements"], "slide elements")
    view = slide_view(slide, [id(e) for e in elements], "slide")
    return [Para(el=elements[p.ei], ei=p.ei, pi=p.pi, order=p.order, text=p.text) for p in paragraphs_of(view)]


def target_key(el: ElementView) -> str | None:
    """The mark a target (`deck_ir`) element's object carries on an adopted page (`adopt.mark_key`)."""
    from .adopt import mark_key
    return mark_key(el.id)


def keyed_elements(cur: Sequence[ElementView], tgt: Sequence[ElementView]) -> list[tuple[int, int]]:
    """(current, target) index pairs of elements that are one deck object by its key: an adopted
    page's element says which object it was written from (`marked.py`: `mark`), so no guess pairs it."""
    by_key: dict[str, int] = {}
    for j, e in enumerate(tgt):
        k = target_key(e)
        if k:
            by_key.setdefault(k, j)
    out: list[tuple[int, int]] = []
    used: set[int] = set()
    for i, e in enumerate(cur):
        j = by_key.get(e.mark) if e.mark else None
        if j is not None and j not in used:
            used.add(j)
            out.append((i, j))
    return out


def keyed_paragraphs(cur: Sequence[SlidePara], tgt: Sequence[SlidePara]) -> list[tuple[int, int, float]]:
    """Paragraph pairs inside keyed element pairs: the element's paragraphs aligned in order, on
    a low bar (they are one box's; only what the text shares decides which is which)."""
    c_els = list({id(p.el): p.el for p in cur}.values())
    t_els = list({id(p.el): p.el for p in tgt}.values())
    out: list[tuple[int, int, float]] = []
    for a, b in keyed_elements(c_els, t_els):
        ce, te = c_els[a], t_els[b]
        ci = [i for i, p in enumerate(cur) if p.el is ce]
        ti = [j for j, p in enumerate(tgt) if p.el is te]
        for x, y in align_sequences(ci, ti, lambda i, j: similarity(cur[i].text, tgt[j].text), 0.2):
            if x is not None and y is not None:
                out.append((ci[x], ti[y], similarity(cur[ci[x]].text, tgt[ti[y]].text)))
    return out


def pair_paragraphs(cur: Sequence[SlidePara], tgt: Sequence[SlidePara]) -> list[tuple[int, int, float]]:
    """Paragraphs of keyed elements by their key (`keyed_paragraphs`), the rest by greedy
    assignment by word similarity (ties: same title role, reading order, position)."""
    fixed = keyed_paragraphs(cur, tgt)
    done_c, done_t = {i for i, _, _ in fixed}, {j for _, j, _ in fixed}
    cands: list[tuple[float, int, int, float]] = []
    for i, a in enumerate(cur):
        if i in done_c:
            continue
        for j, b in enumerate(tgt):
            if j in done_t:
                continue
            r = similarity(a.text, b.text)
            if r < 0.34 and not (a.text and b.text and len(a.text.split()) <= 3 and
                                 difflib.SequenceMatcher(None, a.text, b.text).ratio() >= 0.6):
                continue
            role = 0.1 if (a.el.role == "title") == (b.el.role == "title") else -0.3
            order = 0.05 * (1 - min(1.0, abs(a.order / max(1, len(cur)) - b.order / max(1, len(tgt))) * 2))
            cands.append((r + role + order, i, j, r))
    cands.sort(reverse=True)
    used_c, used_t, out = set(done_c), set(done_t), list(fixed)
    for _, i, j, r in cands:
        if i in used_c or j in used_t:
            continue
        used_c.add(i)
        used_t.add(j)
        out.append((i, j, r))
    return sorted(out, key=lambda t: t[1])


def match_paragraphs(cur: Sequence[Para], tgt: Sequence[Para]) -> list[tuple[int, int, float]]:
    """`pair_paragraphs` of paragraphs of slides held as dicts (`slide_paragraphs`)."""
    views: dict[int, TextView] = {}

    def typed(p: Para) -> SlidePara:
        el = views.get(id(p.el))
        if el is None:
            el = views[id(p.el)] = text_view(p.el, f"element {p.ei}")
        return SlidePara(el=el, ei=p.ei, pi=p.pi, order=p.order, text=p.text)
    return pair_paragraphs([typed(p) for p in cur], [typed(p) for p in tgt])


StyleField = Literal["bold", "italic", "underline", "color", "size", "mono"]
STYLE_FIELDS: tuple[StyleField, ...] = ("bold", "italic", "underline", "color", "size", "mono")
FlagField = Literal["bold", "italic", "underline", "mono"]


@dataclass(frozen=True, kw_only=True)
class CharStyle:
    bold: bool
    italic: bool
    underline: bool
    color: str
    size: float | None
    mono: bool
    script: bool


@dataclass(frozen=True, kw_only=True)
class FlagChange:
    """Bold, italic, underline or monospace on one side and not on the other."""
    field: FlagField
    cur: bool
    tgt: bool


@dataclass(frozen=True, kw_only=True)
class ColorChange:
    field: Literal["color"]
    cur: str
    tgt: str


@dataclass(frozen=True, kw_only=True)
class SizeChange:
    """Both sides say a size (a size only one side says is no difference)."""
    field: Literal["size"]
    cur: float
    tgt: float


StyleChange = Union[FlagChange, ColorChange, SizeChange]


def flag_change(fld: FlagField, cur: bool, tgt: bool) -> FlagChange | None:
    return FlagChange(field=fld, cur=cur, tgt=tgt) if cur != tgt else None


def style_change(fld: StyleField, c: CharStyle, t: CharStyle, tol: Mapping[str, float]) -> StyleChange | None:
    """How one character's `fld` differs between the current and the target side, beyond `tol`."""
    if fld == "color":
        return ColorChange(field="color", cur=c.color, tgt=t.color) \
            if colour_distance(c.color, t.color) > tol["color"] else None
    if fld == "size":
        a, b = c.size, t.size
        if a and b and abs(a - b) > tol["font"] * max(a, b):
            return SizeChange(field="size", cur=a, tgt=b)
        return None
    if fld == "bold":
        return flag_change("bold", c.bold, t.bold)
    if fld == "italic":
        return flag_change("italic", c.italic, t.italic)
    if fld == "underline":
        return flag_change("underline", c.underline, t.underline)
    if fld == "mono":
        return flag_change("mono", c.mono, t.mono)
    assert_never(fld)


def char_styles(runs: Sequence[RunView], size: float | None) -> tuple[str, list[CharStyle]]:
    text = ""
    styles: list[CharStyle] = []
    for r in runs:
        google = google_font(r.font)  # emit writes a Google font's own weight and slant
        st = CharStyle(bold=r.bold or bool(google and google[1] >= 600), italic=r.italic or bool(google and google[2]),
                       underline=r.underline, color=r.color, size=r.size or size, mono=r.mono, script=r.script)
        text += r.text
        styles += [st] * len(r.text)
    return text, styles


@dataclass(frozen=True, kw_only=True)
class StyleDiff:
    """A range of equal text whose style differs (offsets in the target's and the current
    paragraph's text), and how it differs where the range starts: one `style` residual's own
    fields."""
    change: StyleChange
    t0: int
    t1: int
    c0: int
    c1: int
    text: str


def run_style_diffs(cur: tuple[str, list[CharStyle]], tgt: tuple[str, list[CharStyle]],
                    tol: Mapping[str, float]) -> list[StyleDiff]:
    ct, cs = cur
    tt, ts = tgt
    sm = difflib.SequenceMatcher(None, ct, tt, autojunk=False)
    out: list[StyleDiff] = []

    def off(k: int, fld: StyleField, a: int, b: int) -> StyleChange | None:
        c, t = cs[a + k], ts[b + k]
        if tt[b + k] == HOLE or (fld == "size" and (c.script or t.script)):
            return None
        return style_change(fld, c, t, tol)

    for a, b, n in sm.get_matching_blocks():
        for fld in STYLE_FIELDS:
            k = 0
            while k < n:
                change = off(k, fld, a, b)
                if change is None or tt[b + k].isspace():
                    k += 1
                    continue
                start = k
                while k < n and (off(k, fld, a, b) is not None or tt[b + k].isspace()):
                    k += 1
                end = k
                while end > start and tt[b + end - 1].isspace():
                    end -= 1
                out.append(StyleDiff(change=change, t0=b + start, t1=b + end, c0=a + start, c1=a + end,
                                     text=tt[b + start:b + end]))
    return out


def style_diffs(cp: JsonObject, tp: JsonObject, tol: Mapping[str, float]) -> list[StyleDiff]:
    """Ranges of equal text whose style differs, of two paragraphs given as dicts."""
    def styled(p: JsonObject) -> tuple[str, list[CharStyle]]:
        return char_styles(runs_of(p, "paragraph"), _get(p, "size", number, "paragraph"))
    return run_style_diffs(styled(cp), styled(tp), tol)


WordOpKind = Literal["replace", "delete", "insert"]


@dataclass(frozen=True, kw_only=True)
class WordOp:
    """Words `c` of the current paragraph become words `t` of the target's (half-open ranges)."""
    op: WordOpKind
    cur: str
    tgt: str
    c: tuple[int, int]
    t: tuple[int, int]


def word_op_kind(tag: str) -> WordOpKind | None:
    """difflib's opcode tag; None for "equal"."""
    if tag == "replace":
        return "replace"
    if tag == "delete":
        return "delete"
    if tag == "insert":
        return "insert"
    if tag == "equal":
        return None
    raise ValueError(f"difflib opcode {tag!r}")


def word_diff(a: str, b: str) -> list[WordOp]:
    wa, wb = a.split(), b.split()
    ops: list[WordOp] = []
    for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        op = word_op_kind(tag)
        if op is not None:
            ops.append(WordOp(op=op, cur=" ".join(wa[a0:a1]), tgt=" ".join(wb[b0:b1]), c=(a0, a1), t=(b0, b1)))
    return ops


def word_op_json(o: WordOp) -> JsonObject:
    return {"op": o.op, "cur": o.cur, "tgt": o.tgt, "c": [o.c[0], o.c[1]], "t": [o.t[0], o.t[1]]}


BulletSig = tuple[Literal["number", "bullet"] | None, int]


def signature(b: BulletView | None, level: int, base: int) -> BulletSig:
    if b is None:
        return (None, 0)
    return ("number" if b.kind == "number" or b.text.rstrip(".)").isdigit() else "bullet", level - base)


def bullet_base(el: TextView) -> int:
    return min((q.level for q in el.paragraphs if q.bullet is not None), default=0)


def bullet_sig(p: JsonObject, el: JsonObject) -> BulletSig:
    """(bullet or number or None, level). Levels count from the element's shallowest bullet: Slides
    has no absolute level (classify gives a lone picture-bullet list level 1)."""
    base = min((_level(q, "paragraph") for q in as_objects(el.get("paragraphs", []), "element paragraphs")
                if q.get("bullet")), default=0)
    return signature(bullet_view(p.get("bullet"), "paragraph"), _level(p, "paragraph"), base)


# ---------------------------------------------------------------- pictures and shapes

@dataclass(frozen=True)
class PicHash:
    """A 16x16 grey thumbnail (on white) and how much of the picture is opaque. Positional:
    inverse builds one so."""
    grey: list[int]
    coverage: float


# What `hashes` holds for a picture: a thumbnail with its coverage, a bare thumbnail (a crop of the
# candidate's page, opaque), or nothing readable.
PictureHash = Union[PicHash, list[int], None]


def match_boxes(cur: Sequence[B], tgt: Sequence[B], hashes: Mapping[int, PictureHash] | None) -> list[tuple[int, int]]:
    """Keyed elements by their key (`keyed_elements`), the rest by box, aspect, picture and fill."""
    fixed = keyed_elements(cur, tgt)
    cands: list[tuple[float, int, int]] = []
    for i, a in enumerate(cur):
        if any(i == x for x, _ in fixed):
            continue
        for j, b in enumerate(tgt):
            if any(j == y for _, y in fixed):
                continue
            d = sum(abs(x - y) for x, y in zip(a.bbox, b.bbox)) / 4
            aw, ah = a.bbox[2] - a.bbox[0], a.bbox[3] - a.bbox[1]
            bw, bh = b.bbox[2] - b.bbox[0], b.bbox[3] - b.bbox[1]
            aspect = abs(math.log(max(aw, 1) / max(ah, 1)) - math.log(max(bw, 1) / max(bh, 1)))
            score = math.exp(-d / 60) + 0.5 * math.exp(-aspect * 4)
            if hashes:
                h = hash_distance(picture_of(hashes, a), picture_of(hashes, b))
                if h is not None:
                    score += 1.0 - min(1.0, h * 4)
            if a.fill and b.fill:
                score += 0.3 * (colour_distance(a.fill, b.fill) <= TOL["color"])
            cands.append((score, i, j))
    cands.sort(reverse=True)
    used_c, used_t, out = {x for x, _ in fixed}, {y for _, y in fixed}, list(fixed)
    for score, i, j in cands:
        if score < 0.45 or i in used_c or j in used_t:
            continue
        used_c.add(i)
        used_t.add(j)
        out.append((i, j))
    return out


def picture_of(hashes: Mapping[int, PictureHash], el: Placed) -> PictureHash:
    return hashes.get(el.ref) if el.ref is not None else None


def hash_grey(h: PictureHash) -> list[int] | None:
    return h.grey if isinstance(h, PicHash) else h


def hash_coverage(h: PictureHash) -> float:
    return h.coverage if isinstance(h, PicHash) else 1.0


def grey_distance(x: Sequence[int], y: Sequence[int]) -> float:
    return sum(abs(u - v) for u, v in zip(x, y)) / (255 * len(x))


def hash_distance(a: PictureHash, b: PictureHash) -> float | None:
    x, y = hash_grey(a), hash_grey(b)
    if not x or not y or len(x) != len(y):
        return None
    return grey_distance(x, y)


def picture_differs(a: PictureHash, b: PictureHash, tol: float) -> bool:
    """Two pictures that don't show the same thing. The mean difference of the thumbnails misses a
    replaced figure on white (a curve for bars: 0.11), so opaque pictures with some contrast must
    also correlate. Transparent ones (formula and overlay pictures: the page shows through) are
    judged by the mean alone."""
    x, y = hash_grey(a), hash_grey(b)
    if not x or not y or len(x) != len(y):
        return False
    d = grey_distance(x, y)
    if d > tol:
        return True
    if d <= 0.03 or min(hash_coverage(a), hash_coverage(b)) < 0.5:
        return False
    mx, my = sum(x) / len(x), sum(y) / len(y)
    vx = sum((v - mx) ** 2 for v in x) / len(x)
    vy = sum((v - my) ** 2 for v in y) / len(y)
    if min(vx, vy) < 36:
        return False
    cov = sum((u - mx) * (v - my) for u, v in zip(x, y)) / len(x)
    return cov / math.sqrt(vx * vy) < 0.75


def picture_hash(path: Union[str, Path]) -> PicHash | None:
    """Thumbnail of a picture file: survives Google's re-encoding."""
    try:
        from PIL import Image
        img = Image.open(path)
        img.seek(0)
        return pic_hash(img)
    except (OSError, ValueError, EOFError):
        return None


def pic_hash(img: "Image.Image") -> PicHash:
    coverage = 1.0
    if img.mode in ("RGBA", "LA", "P"):
        from PIL import Image
        rgba = img.convert("RGBA")
        small = rgba.getchannel("A").resize((16, 16), Image.Resampling.BILINEAR)
        coverage = sum(1 for v in small.tobytes() if v > 128) / 256
        ground = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        ground.alpha_composite(rgba)
        img = ground
    return PicHash(grey16(img), coverage)


def grey16(img: "Image.Image") -> list[int]:
    """The 16x16 grey thumbnail, row by row (an "L" image's bytes are its pixels in that order)."""
    from PIL import Image
    small = img.convert("L").resize((16, 16), Image.Resampling.BILINEAR)
    return list(small.tobytes())


PICTURE_EDITS = ("crop", "rotation", "flip", "opacity", "brightness", "contrast", "recolor")


def _number(v: Json, where: str) -> float:
    return number(v, At(where=where, path=""))


def adjusted_picture(img: "Image.Image", el: Mapping[str, Json]) -> "Image.Image":
    """Brightness, contrast and recolour of a Slides picture applied to its pixels (RGBA). Slides'
    own formulas aren't documented. Recolour maps luminance onto the gradient of its stops; then
    contrast scales around mid grey (1 + c, or 1 / (1 - c) above 0) and brightness scales the
    colour: by 1 + b below 0, 1 / (1 - b) above (measured on the adopt corpus' thumbnails: -0.32,
    -0.5 and -0.7 give 0.68, 0.49 and 0.30 x the file's values, 0.34 about 1.48 x, and ap-bio-stats'
    CUSTOM ramp under 0.6 comes out 2.5 x the ramp's colours - an offset of b would black out a photo
    at -0.5 that the deck shows dimmed)."""
    import numpy as np
    from PIL import Image
    arr = np.asarray(img.convert("RGBA"), np.float32) / np.float32(255)
    rgb, alpha = arr[..., :3], arr[..., 3:]
    recolor = as_object(el.get("recolor") or {}, "picture recolor")
    stops = sorted(((_number(s["position"], "recolor stop position"), as_str(s["color"], "recolor stop colour"))
                    for s in as_objects(recolor.get("stops") or [], "recolor stops")), key=lambda s: s[0])
    if stops:
        lum = np.clip(rgb @ np.array([0.299, 0.587, 0.114], np.float32), 0, 1)
        pos = np.array([p for p, _ in stops], np.float32)
        cols = np.array([[int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)] for _, c in stops], np.float32)
        rgb = np.stack([np.interp(lum, pos, cols[:, ch]) for ch in range(3)], -1)
    elif recolor.get("name") == "GRAYSCALE":
        lum = rgb @ np.array([0.299, 0.587, 0.114], np.float32)
        rgb = np.repeat(lum[..., None], 3, -1)
    c = _number(el.get("contrast") or 0.0, "picture contrast")
    b = _number(el.get("brightness") or 0.0, "picture brightness")
    if c:
        k = 1 / max(1e-3, 1 - c) if c > 0 else 1 + c
        rgb = (rgb - 0.5) * k + 0.5
    if b:
        rgb = rgb * (1 + b if b < 0 else 1 / max(1e-3, 1 - b))
    out = np.concatenate([np.clip(rgb, 0, 1), alpha], -1)
    return Image.fromarray((out * 255 + 0.5).astype(np.uint8), "RGBA")


def cropped_picture(img: "Image.Image", crop: Mapping[str, Json] | None) -> "Image.Image":
    if not crop:
        return img
    w, h = img.size
    left, top = _number(crop["l"], "crop l"), _number(crop["t"], "crop t")
    right, bottom = _number(crop["r"], "crop r"), _number(crop["b"], "crop b")
    area = (round(left * w), round(top * h), round((1 - right) * w), round((1 - bottom) * h))
    if area[2] - area[0] < 1 or area[3] - area[1] < 1:
        return img
    return img.crop(area)


def displayed_picture(el: Mapping[str, Json]) -> "Image.Image | None":
    """A deck picture as Slides shows it inside its axis-aligned box, on white: crop, adjustments,
    transparency, mirroring and rotation applied to its file. None without a readable file."""
    from PIL import Image, ImageOps
    try:
        img = Image.open(as_str(el["file"], "picture file"))
        img.seek(0)
        img = img.convert("RGBA")
    except (OSError, ValueError, KeyError, EOFError):
        return None
    crop = el.get("crop")
    img = cropped_picture(img, as_object(crop, "picture crop") if crop else None)
    if any(el.get(k) for k in ("brightness", "contrast", "recolor")):
        img = adjusted_picture(img, el)
    x0, y0, x1, y1 = box(el.get("box") or el["bbox"], At(where="picture", path="box"))
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    size = (max(8, round(4 * w)), max(8, round(4 * h)))  # 4 px per pt, like the candidate's crop
    img = img.resize(size, Image.Resampling.BILINEAR)
    opacity = el.get("opacity")
    if opacity is not None:
        alpha = _number(opacity, "picture opacity")
        if alpha < 1:
            img.putalpha(img.getchannel("A").point(lambda v: round(v * alpha)))
    if el.get("flip"):
        img = ImageOps.mirror(img)
    rotation = el.get("rotation")
    if rotation:
        img = img.rotate(-_number(rotation, "picture rotation"), resample=Image.Resampling.BILINEAR, expand=True)
    ground = Image.new("RGBA", img.size, (255, 255, 255, 255))
    ground.alpha_composite(img)
    return ground.convert("RGB")


# ---------------------------------------------------------------- compare

# Each residual kind is a record of its own (`Residual`, the union), its `kind` the one it has in
# the JSON reports and agents read (`residual_json`: key for key, in the order compare always wrote
# them). `within`: inside the tolerance, or owned by the theme; `Comparison.open` leaves those out.
# A residual of a slide both sides have names it on both (`slide`, `target_slide`); an element or a
# paragraph by its id and index on the side it is on.

BoxKind = Literal["image", "shape", "table", "diagram"]


@dataclass(frozen=True, kw_only=True)
class After:
    """The current paragraph a missing or moved one goes after."""
    element: str
    para: int


@dataclass(frozen=True, kw_only=True)
class SlideOrder:
    kind: Literal["slide_order"]
    within: bool
    slide: int
    target_slide: int


@dataclass(frozen=True, kw_only=True)
class SlideMissing:
    kind: Literal["slide_missing"]
    within: bool
    target_slide: int
    title: str


@dataclass(frozen=True, kw_only=True)
class SlideExtra:
    kind: Literal["slide_extra"]
    within: bool
    slide: int
    title: str


@dataclass(frozen=True, kw_only=True)
class NotesResidual:
    kind: Literal["notes"]
    within: bool
    slide: int
    target_slide: int
    cur: str | None
    tgt: str | None


@dataclass(frozen=True, kw_only=True)
class BackgroundResidual:
    kind: Literal["background"]
    within: bool
    slide: int
    target_slide: int
    cur: str
    tgt: str


@dataclass(frozen=True, kw_only=True)
class ParagraphMissing:
    kind: Literal["paragraph_missing"]
    within: bool
    slide: int
    target_slide: int
    target_element: str
    target_para: int
    text: str
    after: After | None
    off_page: bool         # its element lies wholly off the page (then `within`)


@dataclass(frozen=True, kw_only=True)
class ParagraphExtra:
    kind: Literal["paragraph_extra"]
    within: bool
    slide: int
    target_slide: int
    element: str
    para: int
    text: str


@dataclass(frozen=True, kw_only=True)
class ParagraphOrder:
    kind: Literal["paragraph_order"]
    within: bool
    slide: int
    target_slide: int
    element: str
    para: int
    target_element: str
    target_para: int
    text: str
    after: After | None


@dataclass(frozen=True, kw_only=True)
class TextResidual:
    kind: Literal["text"]
    within: bool
    slide: int
    target_slide: int
    element: str
    para: int
    target_element: str
    target_para: int
    cur: str
    tgt: str
    ops: tuple[WordOp, ...]


@dataclass(frozen=True, kw_only=True)
class StyleResidual:
    kind: Literal["style"]
    within: bool
    slide: int
    target_slide: int
    element: str
    para: int
    target_element: str
    target_para: int
    change: StyleChange
    t0: int
    t1: int
    c0: int
    c1: int
    text: str
    theme: bool            # a whole title's size or colour: the theme's (then `within`)


@dataclass(frozen=True, kw_only=True)
class BulletResidual:
    kind: Literal["bullet"]
    within: bool
    slide: int
    target_slide: int
    element: str
    para: int
    target_element: str
    target_para: int
    cur: "BulletSig"
    tgt: "BulletSig"


@dataclass(frozen=True, kw_only=True)
class AlignResidual:
    kind: Literal["align"]
    within: bool
    slide: int
    target_slide: int
    element: str
    para: int
    target_element: str
    target_para: int
    cur: str
    tgt: str


@dataclass(frozen=True, kw_only=True)
class TextMissing:
    kind: Literal["element_missing"]
    within: bool
    slide: int
    target_slide: int
    target_element: str
    el_kind: Literal["text"]
    text: str
    role: str | None
    off_page: bool


@dataclass(frozen=True, kw_only=True)
class BoxMissing:
    kind: Literal["element_missing"]
    within: bool
    slide: int
    target_slide: int
    target_element: str
    el_kind: BoxKind
    bbox: Box
    file: str | None
    off_page: bool


@dataclass(frozen=True, kw_only=True)
class TextGeometry:
    """A text element's anchor (`anchor_of`) moved: `cur` and `tgt` are (x, baseline)."""
    kind: Literal["geometry"]
    within: bool
    slide: int
    target_slide: int
    element: str
    target_element: str
    para: int
    dx: float
    dy: float
    cur: tuple[float, float]
    tgt: tuple[float, float]
    align: str
    wrap_width: float | None
    mixed: bool
    theme: bool            # a frame title moved beyond the tolerance: the theme places it


@dataclass(frozen=True, kw_only=True)
class BoxGeometry:
    kind: Literal["geometry"]
    within: bool
    slide: int
    target_slide: int
    element: str
    target_element: str
    el_kind: BoxKind
    dx: float
    dy: float
    dw: float
    dh: float
    cur: Box
    tgt: Box


@dataclass(frozen=True, kw_only=True)
class TextExtra:
    kind: Literal["element_extra"]
    within: bool
    slide: int
    target_slide: int
    element: str
    el_kind: Literal["text"]
    text: str


@dataclass(frozen=True, kw_only=True)
class BoxExtra:
    kind: Literal["element_extra"]
    within: bool
    slide: int
    target_slide: int
    element: str
    el_kind: BoxKind
    bbox: Box


@dataclass(frozen=True, kw_only=True)
class DiagramResidual:
    kind: Literal["diagram"]
    within: bool
    slide: int
    target_slide: int
    element: str
    target_element: str
    cur: tuple[str, ...]
    tgt: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ImageResidual:
    """A paired picture shows something else."""
    kind: Literal["image"]
    within: bool
    slide: int
    target_slide: int
    element: str
    target_element: str
    distance: float
    file: str | None


@dataclass(frozen=True, kw_only=True)
class InlineImage:
    """A formula or icon picture in a text line shows something else (never written back)."""
    kind: Literal["image"]
    within: bool
    slide: int
    target_slide: int
    element: str
    target_element: str
    distance: float
    role: str | None
    file: str | None


@dataclass(frozen=True, kw_only=True)
class ShapeResidual:
    kind: Literal["shape"]
    within: bool
    slide: int
    target_slide: int
    element: str
    target_element: str
    cur: str | None
    tgt: str | None


@dataclass(frozen=True, kw_only=True)
class TableResidual:
    kind: Literal["table"]
    within: bool
    slide: int
    target_slide: int
    element: str
    target_element: str
    cur: tuple[tuple[str, ...], ...]
    tgt: tuple[tuple[str, ...], ...]


SlideResidual = Union[SlideOrder, SlideMissing, SlideExtra]
ListResidual = Union[ParagraphMissing, ParagraphExtra, ParagraphOrder, BulletResidual]
ElementMissing = Union[TextMissing, BoxMissing]
ElementExtra = Union[TextExtra, BoxExtra]
Geometry = Union[TextGeometry, BoxGeometry]
ImageChange = Union[ImageResidual, InlineImage]
# every residual of a slide both sides have
PairedResidual = Union[NotesResidual, BackgroundResidual, ParagraphMissing, ParagraphExtra, ParagraphOrder,
                       TextResidual, StyleResidual, BulletResidual, AlignResidual, TextMissing, BoxMissing,
                       TextGeometry, BoxGeometry, TextExtra, BoxExtra, DiagramResidual, ImageResidual, InlineImage,
                       ShapeResidual, TableResidual]
Residual = Union[SlideOrder, SlideMissing, SlideExtra, PairedResidual]


def split_residuals(rs: Iterable[Residual]) -> tuple[list[SlideResidual], list[PairedResidual]]:
    """Residuals about which slides there are, and those about slides both sides have."""
    slides: list[SlideResidual] = []
    paired: list[PairedResidual] = []
    for r in rs:
        if isinstance(r, (SlideOrder, SlideMissing, SlideExtra)):
            slides.append(r)
        else:
            paired.append(r)
    return slides, paired


def target_slide_of(r: Residual) -> int | None:
    """The target slide a residual is about; None for a current slide the target does not have."""
    return None if isinstance(r, SlideExtra) else r.target_slide


def current_slide_of(r: Residual) -> int | None:
    """The current slide a residual is about; None for a target slide the current deck lacks."""
    return None if isinstance(r, SlideMissing) else r.slide


def is_theme(r: Residual) -> bool:
    """A difference the beamer theme owns (a frame title's size, colour or place)."""
    return isinstance(r, (StyleResidual, TextGeometry)) and r.theme


def _pair(p: tuple[float, float]) -> JsonArray:
    return [p[0], p[1]]


def _box(b: Box) -> JsonArray:
    return [b[0], b[1], b[2], b[3]]


def _strs(v: tuple[str, ...]) -> JsonArray:
    return [s for s in v]


def _after(a: After | None) -> Json:
    return None if a is None else {"element": a.element, "para": a.para}


def _bullet(b: "BulletSig") -> JsonArray:
    return [b[0], b[1]]


def _change(c: StyleChange) -> JsonObject:
    return {"field": c.field, "cur": c.cur, "tgt": c.tgt}


def residual_json(r: Residual) -> JsonObject:
    """A residual as the reports, edits.json and agents have always read it: its kind, `within`,
    then its own keys in compare's order (`off_page` and `theme` only when true)."""
    out: JsonObject = {"kind": r.kind, "within": r.within}
    if isinstance(r, SlideOrder):
        out.update({"slide": r.slide, "target_slide": r.target_slide})
    elif isinstance(r, SlideMissing):
        out.update({"target_slide": r.target_slide, "title": r.title})
    elif isinstance(r, SlideExtra):
        out.update({"slide": r.slide, "title": r.title})
    elif isinstance(r, (NotesResidual, BackgroundResidual)):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "cur": r.cur, "tgt": r.tgt})
    elif isinstance(r, ParagraphMissing):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "target_element": r.target_element,
                    "target_para": r.target_para, "text": r.text, "after": _after(r.after)})
        if r.off_page:
            out["off_page"] = True
    elif isinstance(r, ParagraphExtra):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element, "para": r.para,
                    "text": r.text})
    elif isinstance(r, ParagraphOrder):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element, "para": r.para,
                    "target_element": r.target_element, "target_para": r.target_para, "text": r.text,
                    "after": _after(r.after)})
    elif isinstance(r, (TextResidual, StyleResidual, BulletResidual, AlignResidual)):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element, "para": r.para,
                    "target_element": r.target_element, "target_para": r.target_para})
        if isinstance(r, TextResidual):
            out.update({"cur": r.cur, "tgt": r.tgt, "ops": [word_op_json(o) for o in r.ops]})
        elif isinstance(r, StyleResidual):
            out.update(_change(r.change))
            out.update({"t0": r.t0, "t1": r.t1, "c0": r.c0, "c1": r.c1, "text": r.text})
            if r.theme:
                out["theme"] = True
        elif isinstance(r, BulletResidual):
            out.update({"cur": _bullet(r.cur), "tgt": _bullet(r.tgt)})
        else:
            out.update({"cur": r.cur, "tgt": r.tgt})
    elif isinstance(r, TextMissing):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "target_element": r.target_element,
                    "el_kind": r.el_kind, "text": r.text, "role": r.role})
        if r.off_page:
            out["off_page"] = True
    elif isinstance(r, BoxMissing):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "target_element": r.target_element,
                    "el_kind": r.el_kind, "bbox": _box(r.bbox), "file": r.file})
        if r.off_page:
            out["off_page"] = True
    elif isinstance(r, TextGeometry):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element,
                    "target_element": r.target_element, "para": r.para, "dx": r.dx, "dy": r.dy,
                    "cur": _pair(r.cur), "tgt": _pair(r.tgt), "align": r.align, "wrap_width": r.wrap_width,
                    "mixed": r.mixed})
        if r.theme:
            out["theme"] = True
    elif isinstance(r, BoxGeometry):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element,
                    "target_element": r.target_element, "el_kind": r.el_kind, "dx": r.dx, "dy": r.dy,
                    "dw": r.dw, "dh": r.dh, "cur": _box(r.cur), "tgt": _box(r.tgt)})
    elif isinstance(r, TextExtra):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element,
                    "el_kind": r.el_kind, "text": r.text})
    elif isinstance(r, BoxExtra):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element,
                    "el_kind": r.el_kind, "bbox": _box(r.bbox)})
    elif isinstance(r, DiagramResidual):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element,
                    "target_element": r.target_element, "cur": _strs(r.cur), "tgt": _strs(r.tgt)})
    elif isinstance(r, ImageResidual):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element,
                    "target_element": r.target_element, "distance": r.distance, "file": r.file})
    elif isinstance(r, InlineImage):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element,
                    "target_element": r.target_element, "distance": r.distance, "role": r.role, "file": r.file})
    elif isinstance(r, ShapeResidual):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element,
                    "target_element": r.target_element, "cur": r.cur, "tgt": r.tgt})
    elif isinstance(r, TableResidual):
        out.update({"slide": r.slide, "target_slide": r.target_slide, "element": r.element,
                    "target_element": r.target_element, "cur": [_strs(row) for row in r.cur],
                    "tgt": [_strs(row) for row in r.tgt]})
    else:
        assert_never(r)
    return out


@dataclass(frozen=True, kw_only=True)
class Comparison:
    slides: tuple[tuple[int | None, int | None], ...]       # (current index, target index) pairs
    residuals: tuple[Residual, ...]
    elements: Mapping[tuple[int, str], tuple[int, str]]   # (ci, element id) -> (ti, target element id)

    def open(self) -> list[Residual]:
        return [r for r in self.residuals if not r.within]

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.open():
            out[r.kind] = out.get(r.kind, 0) + 1
        return out


@dataclass(frozen=True, kw_only=True)
class Current:
    """The current side: a deck as classify reads the candidate's PDF, and the label of the frame
    each of its slides comes from (None: an unlabelled frame, or no frame). The labels pair slides
    first (`pair_slides`); deck.json has no place for them, a pull candidate knowing them from
    SyncTeX (`inverse.Candidate.keys`)."""
    deck: DeckInput
    keys: tuple[str | None, ...]


def without_keys(deck: DeckInput) -> Current:
    """A deck compared as it is: no slide is known by a frame label."""
    n = len(as_array(deck["slides"], "slides")) if isinstance(deck, dict) else len(deck.slides)
    return Current(deck=deck, keys=(None,) * n)


def compare(cur: Current, tgt: DeckInput, tol: Mapping[str, float], hashes: Mapping[int, PictureHash]) -> Comparison:
    """`tol`: every limit of `TOL`. `hashes`: id(element) -> picture_hash, for pictures on either
    side (the element's dict, or its typed value when the deck is given parsed); empty for none."""
    views = deck_views(cur.deck, "current")
    if len(cur.keys) != len(views):
        raise ValueError(f"{len(cur.keys)} frame labels for {len(views)} current slides")
    cs = [dataclasses.replace(v, summary=dataclasses.replace(v.summary, key=k)) for v, k in zip(views, cur.keys)]
    ts = deck_views(tgt, "target")
    pairs = pair_slides([s.summary for s in cs], [s.summary for s in ts])
    res: list[Residual] = []
    elements: dict[tuple[int, str], tuple[int, str]] = {}

    matched = sorted((j, i) for i, j in pairs if i is not None and j is not None)
    lis = longest_increasing([i for _, i in matched])
    for k, (j, i) in enumerate(matched):
        if k not in lis:
            res.append(SlideOrder(kind="slide_order", within=False, slide=i, target_slide=j))
    for i, j in pairs:
        if i is None and j is not None:
            res.append(SlideMissing(kind="slide_missing", within=False, target_slide=j, title=ts[j].summary.title))
        elif j is None and i is not None:
            res.append(SlideExtra(kind="slide_extra", within=False, slide=i, title=cs[i].summary.title))
        elif i is not None and j is not None:
            res += compare_slide(cs[i], ts[j], i, j, tol, elements, hashes)
    return Comparison(slides=tuple(pairs), residuals=tuple(res), elements=elements)


# A hyphen between letters, and the line break after it: TeX breaks a note's lines where Slides
# breaks none, and a note page reads "cul- tural" and "hyper- modern" (saudi-cats). Neither
# \hyphenpenalty nor \automatichyphenmode in the note page's template kept LuaTeX from breaking there.
NOTE_HYPHEN_RE = re.compile(r"(?<=[^\W\d_])-\s*(?=[^\W\d_])")


def norm_notes(text: str) -> str:
    return norm_text(NOTE_HYPHEN_RE.sub("", text))


def off_page(el: ElementView, size: tuple[float, float] | None) -> bool:
    """A target element wholly outside the page (a deck object parked beside its slide): no PDF
    shows it, so its absence is no difference a source could make up."""
    if not size:
        return False
    x0, y0, x1, y1 = el.bbox
    return x0 >= size[0] or x1 <= 0 or y0 >= size[1] or y1 <= 0


def compare_slide(c: SlideView, t: SlideView, ci: int, ti: int, tol: Mapping[str, float],
                  elements: dict[tuple[int, str], tuple[int, str]],
                  hashes: Mapping[int, PictureHash]) -> list[PairedResidual]:
    """The residuals of one pair of slides, in the order compare has always reported them; each
    element the two share goes into `elements`."""
    res: list[PairedResidual] = []

    def parked(el: ElementView) -> bool:
        return off_page(el, c.size)

    cn, tn = norm_notes(c.notes or ""), norm_notes(t.notes or "")
    if cn != tn:
        res.append(NotesResidual(kind="notes", within=False, slide=ci, target_slide=ti, cur=c.notes, tgt=t.notes))
    if t.background_color and c.background_color and \
            colour_distance(c.background_color, t.background_color) > tol["color"]:
        res.append(BackgroundResidual(kind="background", within=False, slide=ci, target_slide=ti,
                                      cur=c.background_color, tgt=t.background_color))

    cps, tps = paragraphs_of(c), paragraphs_of(t)
    pmatch = pair_paragraphs(cps, tps)
    c_of_t = {j: i for i, j, _ in pmatch}
    t_of_c = {i: j for i, j, _ in pmatch}
    for j, tp in enumerate(tps):
        if j not in c_of_t:
            res.append(ParagraphMissing(kind="paragraph_missing", within=parked(tp.el), slide=ci, target_slide=ti,
                                        target_element=tp.el.id, target_para=tp.pi, text=tp.text,
                                        after=_previous_match(j, c_of_t, cps), off_page=parked(tp.el)))
    for i, cp in enumerate(cps):
        if i not in t_of_c:
            res.append(ParagraphExtra(kind="paragraph_extra", within=False, slide=ci, target_slide=ti,
                                      element=cp.el.id, para=cp.pi, text=cp.text))

    # paragraph order within the slide (reading order of the matched pairs); a marked box's
    # paragraphs only among themselves: which box reads first is where each stands, and a box
    # a key paired is compared where it stands (geometry), not again by reading order
    by_box: dict[int | None, list[tuple[int, int]]] = {}
    for i, j, _ in pmatch:
        by_box.setdefault(id(cps[i].el) if cps[i].el.mark else None, []).append((j, i))
    for box_pairs in by_box.values():
        order = sorted(box_pairs)
        lis = longest_increasing([i for _, i in order])
        kept = {order[k][0]: order[k][1] for k in lis}
        for k, (j, i) in enumerate(order):
            if k not in lis:
                res.append(ParagraphOrder(kind="paragraph_order", within=False, slide=ci, target_slide=ti,
                                          element=cps[i].el.id, para=cps[i].pi, target_element=tps[j].el.id,
                                          target_para=tps[j].pi, text=tps[j].text,
                                          after=_previous_match(j, kept, cps)))

    for i, j, _ in pmatch:
        cp, tp = cps[i], tps[j]
        el, para, t_el, t_para = cp.el.id, cp.pi, tp.el.id, tp.pi
        if cp.text != tp.text:
            res.append(TextResidual(kind="text", within=False, slide=ci, target_slide=ti, element=el, para=para,
                                    target_element=t_el, target_para=t_para, cur=cp.text, tgt=tp.text,
                                    ops=tuple(word_diff(cp.text, tp.text))))
        titles = cp.el.role == "title" and tp.el.role == "title"
        for d in run_style_diffs(char_styles(cp.p.runs, cp.p.size), char_styles(tp.p.runs, tp.p.size), tol):
            # a whole title's size or colour is the theme's (a slide added in Slides takes its layout's)
            theme = titles and d.change.field in ("size", "color") and d.t1 - d.t0 >= 0.9 * len(tp.text)
            res.append(StyleResidual(kind="style", within=theme, slide=ci, target_slide=ti, element=el, para=para,
                                     target_element=t_el, target_para=t_para, change=d.change, t0=d.t0, t1=d.t1,
                                     c0=d.c0, c1=d.c1, text=d.text, theme=theme))
        c_sig = signature(cp.p.bullet, cp.p.level, bullet_base(cp.el))
        t_sig = signature(tp.p.bullet, tp.p.level, bullet_base(tp.el))
        if c_sig != t_sig:
            res.append(BulletResidual(kind="bullet", within=False, slide=ci, target_slide=ti, element=el, para=para,
                                      target_element=t_el, target_para=t_para, cur=c_sig, tgt=t_sig))
        if cp.p.align != tp.p.align and len(tp.text) > 0:
            res.append(AlignResidual(kind="align",
                                     within=len(tp.p.lines) <= 1 and "center" not in (cp.p.align, tp.p.align),
                                     slide=ci, target_slide=ti, element=el, para=para, target_element=t_el,
                                     target_para=t_para, cur=cp.p.align, tgt=tp.p.align))

    # elements: a target text element corresponds to the current element holding most of its paragraphs
    for _, te in compared_texts(t):
        mine = [j for j, tp in enumerate(tps) if tp.el is te]
        if not mine:
            continue
        hits = [cps[c_of_t[j]] for j in mine if j in c_of_t]
        if not hits:
            res.append(TextMissing(kind="element_missing", within=parked(te), slide=ci, target_slide=ti,
                                   target_element=te.id, el_kind="text", text=view_text(te), role=te.role,
                                   off_page=parked(te)))
            continue
        first = cps[c_of_t[mine[0]]] if mine[0] in c_of_t else hits[0]
        ce = first.el
        elements[(ci, ce.id)] = (ti, te.id)
        if mine[0] not in c_of_t:
            continue  # its first paragraph is missing: placed once that is there
        (cx, cy), (tx, ty) = text_anchor_for(ce, first.pi), anchor_of(te)
        # a middle- or bottom-aligned box of a foreign deck: where its lines' middle or bottom stand,
        # which the box fixes however they wrap (when the current element is this box's alone)
        own = [k for k, p in enumerate(cps) if p.el is ce]
        rule = stack_rule(te.anchor, te.box) if first.pi == 0 and all(t_of_c.get(k) in mine for k in own) else None
        stacked = stacked_on(rule, view_baselines(ce)) if rule is not None else None
        if stacked:
            cy, ty = stacked
        if cy is None or ty is None:
            raise TypeError(f"text element {ce.id} or {te.id}: a first line without a baseline cannot be placed")
        dx, dy = tx - cx, ty - cy
        # frame titles sit where the theme puts them: a moved title is noted, not written back
        theme = te.role == "title" and ce.role == "title"
        res.append(TextGeometry(kind="geometry", within=theme or (abs(dx) <= tol["pos"] and abs(dy) <= tol["pos"]),
                                slide=ci, target_slide=ti, element=ce.id, target_element=te.id, para=first.pi,
                                dx=round(dx, 2), dy=round(dy, 2), cur=(round(cx, 2), round(cy, 2)),
                                tgt=(round(tx, 2), round(ty, 2)), align=anchor_align(te), wrap_width=te.wrap_width,
                                mixed=len({id(h.el) for h in hits}) > 1,
                                theme=theme and (abs(dx) > tol["pos"] or abs(dy) > tol["pos"])))
    for _, ce in compared_texts(c):
        if not any(p.el is ce and k in t_of_c for k, p in enumerate(cps)) and any(p.el is ce for p in cps):
            res.append(TextExtra(kind="element_extra", within=False, slide=ci, target_slide=ti, element=ce.id,
                                 el_kind="text", text=view_text(ce)))

    def compare_boxes(kind: BoxKind, cc: Sequence[B], tt: Sequence[B]) -> None:
        got = match_boxes(cc, tt, hashes)
        for a, b in got:
            ce, te = cc[a], tt[b]
            elements[(ci, ce.id)] = (ti, te.id)
            cb, tb = ce.bbox, te.bbox
            d = [round(y - x, 2) for x, y in zip(cb, tb)]
            dw, dh = d[2] - d[0], d[3] - d[1]
            if kind == "table":  # Slides sets row heights (cell padding) and column widths: the corner counts
                within = abs(d[0]) <= 1.5 * tol["pos"] and abs(d[1]) <= 1.5 * tol["pos"]
            else:
                within = abs(d[0]) <= tol["pos"] and abs(d[1]) <= tol["pos"] and abs(dw) <= tol["size"] and abs(dh) <= tol["size"]
            res.append(BoxGeometry(kind="geometry", within=within, slide=ci, target_slide=ti, element=ce.id,
                                   target_element=te.id, el_kind=kind, dx=d[0], dy=d[1], dw=round(dw, 2),
                                   dh=round(dh, 2), cur=cb, tgt=tb))
            box_residuals(ce, te)
        for b, te in enumerate(tt):
            if b not in {y for _, y in got}:
                res.append(BoxMissing(kind="element_missing", within=parked(te), slide=ci, target_slide=ti,
                                      target_element=te.id, el_kind=kind, bbox=te.bbox, file=te.file,
                                      off_page=parked(te)))
        for a, ce in enumerate(cc):
            if a not in {x for x, _ in got}:
                res.append(BoxExtra(kind="element_extra", within=False, slide=ci, target_slide=ti, element=ce.id,
                                    el_kind=kind, bbox=ce.bbox))

    def box_residuals(ce: BoxView, te: BoxView) -> None:
        """What differs between two paired elements of one kind besides their box."""
        if isinstance(ce, DiagramView):
            if isinstance(te, DiagramView) and ce.texts != te.texts:
                res.append(DiagramResidual(kind="diagram", within=False, slide=ci, target_slide=ti, element=ce.id,
                                           target_element=te.id, cur=ce.texts, tgt=te.texts))
        elif isinstance(ce, ImageView):
            if hashes:
                hc, ht = picture_of(hashes, ce), picture_of(hashes, te)
                distance = hash_distance(hc, ht)
                if distance is not None and picture_differs(hc, ht, tol["phash"]):
                    res.append(ImageResidual(kind="image", within=False, slide=ci, target_slide=ti, element=ce.id,
                                             target_element=te.id, distance=round(distance, 3), file=te.file))
        elif isinstance(ce, ShapeView):
            if colour_distance(ce.fill, te.fill) > tol["color"]:
                res.append(ShapeResidual(kind="shape", within=False, slide=ci, target_slide=ti, element=ce.id,
                                         target_element=te.id, cur=ce.fill, tgt=te.fill))
        elif isinstance(ce, TableView):
            if isinstance(te, TableView) and ce.cells != te.cells:
                res.append(TableResidual(kind="table", within=False, slide=ci, target_slide=ti, element=ce.id,
                                         target_element=te.id, cur=ce.cells, tgt=te.cells))
        else:
            assert_never(ce)

    def placed(slide: tuple[ElementView, ...], cls: type[B]) -> list[B]:
        return [e for e in slide if isinstance(e, cls) and e.role not in IGNORED_ROLES]

    compare_boxes("image", placed(c.elements, ImageView), placed(t.elements, ImageView))
    compare_boxes("shape", placed(c.shapes, ShapeView), placed(t.elements, ShapeView))
    compare_boxes("table", placed(c.elements, TableView), placed(t.elements, TableView))
    compare_boxes("diagram", placed(c.elements, DiagramView), placed(t.elements, DiagramView))
    if hashes:  # formula and icon pictures in text lines: a replaced one is reported (never written)
        cm = [e for e in c.elements if isinstance(e, ImageView) and e.role in ("math", "icon")]
        tm = [e for e in t.elements if isinstance(e, ImageView) and e.role in ("math", "icon")]
        for a, b in match_boxes(cm, tm, hashes):
            h = hash_distance(picture_of(hashes, cm[a]), picture_of(hashes, tm[b]))
            if h is not None and h > tol["inline_phash"]:  # the mean alone: they are transparent
                res.append(InlineImage(kind="image", within=False, slide=ci, target_slide=ti, element=cm[a].id,
                                       target_element=tm[b].id, distance=round(h, 3), role=cm[a].role,
                                       file=tm[b].file))
    return res


StackRule = tuple[Literal["middle", "bottom"], float, float]


def stack_rule(anchor: tuple[float, float] | None, tbox: TextBox | None) -> StackRule | None:
    """(alignment, first baseline, span) of a target box aligned to its middle or bottom that says
    the span of its lines (`deck_ir.stacked_baseline`); None for anything else."""
    if tbox is None or tbox.span is None or anchor is None:
        return None
    if tbox.valign == "middle":
        return ("middle", anchor[1], tbox.span)
    if tbox.valign == "bottom":
        return ("bottom", anchor[1], tbox.span)
    return None


def stacked_on(rule: StackRule, baselines: Sequence[float]) -> tuple[float, float] | None:
    """(current, target) y to compare: the last baseline of a bottom-aligned box, the one halfway
    between the first and the last of a middle-aligned one. Its first baseline moves with every
    line Slides wraps that TeX does not, or the other way round; these do not."""
    if not baselines:
        return None
    valign, y, span = rule
    if valign == "bottom":
        return baselines[-1], y + span
    return (baselines[0] + baselines[-1]) / 2, y + span / 2


def view_baselines(el: TextView) -> list[float]:
    return [ln.baseline for p in el.paragraphs for ln in p.lines if ln.baseline is not None]


def stacked_y(cur: JsonObject, tgt: JsonObject) -> tuple[float, float] | None:
    """`stacked_on` for a current and a target text element given as dicts; None when the target
    says no span of its lines."""
    rule = stack_rule(anchor_value(tgt.get("anchor"), "target text"), text_box(tgt.get("box"), "target text"))
    if rule is None:
        return None
    baselines = [b for p in as_objects(cur["paragraphs"], "current paragraphs")
                 for ln in as_objects(p.get("lines", []), "current lines")
                 if (b := _get(ln, "baseline", number, "current line")) is not None]
    return stacked_on(rule, baselines)


def text_anchor_for(el: TextView, pi: int) -> tuple[float, float | None]:
    if pi == 0 or el.anchor is not None:
        return anchor_of(el)
    return anchor_of(dataclasses.replace(el, paragraphs=el.paragraphs[pi:]))


def table_text(el: JsonObject) -> list[list[str]]:
    """Cell texts: classify's cells are lists of runs, deck_ir's rows plain strings."""
    rows = el.get("rows") or el.get("cells") or []
    return [[norm_text(c if isinstance(c, str) else "".join(run_text(r) for r in as_objects(c, "table cell")))
             for c in as_array(row, "table row")] for row in as_array(rows, "table rows")]


def diagram_text(el: JsonObject) -> list[str]:
    """Node and label texts of a diagram, sorted (Slides keeps no order between them)."""
    texts = [norm_text(" ".join("".join(run_text(r) for r in as_objects(runs, "diagram paragraph"))
                                for runs in as_array(n.get("paragraphs") or [], "diagram node paragraphs")))
             for n in as_objects(el.get("nodes", []), "diagram nodes")]
    return sorted(t for t in texts if t)


def _previous_match(j: int, c_of_t: Mapping[int, int], cps: Sequence[SlidePara]) -> After | None:
    """The current paragraph matched to the nearest earlier target paragraph."""
    for k in range(j - 1, -1, -1):
        if k in c_of_t:
            cp = cps[c_of_t[k]]
            return After(element=cp.el.id, para=cp.pi)
    return None


def _word_ops(v: object) -> list[Mapping[str, object]]:
    if not isinstance(v, list):
        raise TypeError("a text residual's ops are a list")
    return [o for o in v if isinstance(o, dict)]


def residual_line(r: Mapping[str, object]) -> str:
    """One line per residual for reports."""
    where = f"slide {r.get('target_slide', r.get('slide', '?'))}"
    k = r["kind"]
    if k == "text":
        ops = "; ".join(f"{o['op']} '{o['cur']}' -> '{o['tgt']}'" for o in _word_ops(r["ops"]))
        return f"{where} {r['target_element']} ¶{r['target_para']}: text {ops}"
    if k == "style":
        return f"{where} {r['target_element']} ¶{r['target_para']}: {r['field']} '{r['text']}' {r['cur']} -> {r['tgt']}"
    if k == "geometry":
        return f"{where} {r['target_element']}: moved dx={r['dx']:+.1f} dy={r['dy']:+.1f}" + \
            (f" dw={r['dw']:+.1f} dh={r['dh']:+.1f}" if "dw" in r else "")
    if k in ("paragraph_missing", "paragraph_extra"):
        return f"{where}: {k} '{str(r['text'])[:60]}'"
    if k in ("slide_missing", "slide_extra", "slide_order"):
        return f"{where}: {k} '{r.get('title', '')}'"
    if k in ("element_missing", "element_extra"):
        return f"{where}: {k} {r.get('el_kind')} '{str(r.get('text', r.get('bbox', '')))[:60]}'"
    if k == "image":
        return f"{where} {r['target_element']}: {r.get('role') or 'picture'} replaced (distance {r.get('distance')})"
    return f"{where}: {k} {r.get('cur')!r} -> {r.get('tgt')!r}"


def plain(r: Mapping[str, object]) -> dict[str, object]:
    return {k: v for k, v in r.items()}


def slide_key_of(label: str | None, title: str, n: int) -> str:
    if label:
        return label
    t = re.sub(r"\W+", "-", title.casefold()).strip("-")
    return f"title:{t}#{n}" if t else f"page:{n}"
