r"""The sync base an `adopt` leaves behind, and the refusals a sync into an adopted deck must make
(docs/sync.md, "Adopt").

`convert` records its base from a deck it made itself: every object on every slide is one it
created, under an id it chose, carrying a `b2s:<slide>/<element>` alt-text tag that says which
element of the IR it came from. **An adopted deck has none of that.** Its objects were made by a
person, in the browser, years before this program saw them; nothing in them names anything.

What does tie the two together is how `adopt` works: the source is written *from* the deck, element
by element, one `[plain]` frame per slide, each box drawn where the deck's box stands. So the base
is built by pairing the conversion of that source against the deck IR `adopt` read - same slide,
same place, same words - and a pair is only made when it is unambiguous. Where it is not, the base
says so (`adopt.unpaired`) and the first sync refuses rather than write a second object beside the
person's (`problems`, `refusal_message`).

Slides pair exactly, not by guess: `adopt.frame_labels` writes a `label=` per frame from the
slide's own `objectId`, so the conversion's slide keys *are* the deck's slides, one for one, as
long as the labels stay where adopt put them.

Where the base lives is a decision of its own, argued in docs/sync.md: the folder, not Drive.
`snapshot.save_drive` writes the presentation's `appProperties` and creates a file beside it in the
owner's Drive, and `adopt` may be run against a deck the person has read access to and no right to
change. Nothing here calls Drive; `adopt --base-in-drive` is how one asks for the other half.

Types: the deck IR `adopt` read (the target), the conversion and the records the fold keeps are
JSON read through `json_types`' narrowings; what pairing compares is a `Seen` per element. The base
is built as a `sync_model.Base` (`build_base_of`) and written as JSON once (`build_base`). The
first sync's gate has a typed core over `merge.MergePlan` (`problems_of`) and a dict entry over the
plan's JSON (`problems`), which share everything but reading the plan.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import TypedDict

from . import guard, identity, merge, snapshot
from .emit import SLIDE_W, DeckPlan
from .google_types import DriveService, presentation
from .json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects, as_str
from .sync_model import Base, JsonMap, ObjectId, base_json
from .sync_model import base as parse_base

ORIGIN = merge.ADOPTED   # (the word itself lives there: the merge plans by it and cannot import this)
# A pair is a claim that one converted element *is* one object of the deck. Both numbers are about
# refusing rather than guessing: how close the best candidate has to be, and how far it has to beat
# everything else. A miss costs a report line; a wrong pair would write the source over an object
# it was never drawn from.
PAIR_SURE = 0.45
PAIR_MARGIN = 0.08
# How far the deck's shape may be from the shape of the page the source compiles to. One number
# scales PDF pt into the deck's points (`emit.DeckPlan.scale`), so the two have to have the same
# aspect; 0.5% is under half a point on a 720 pt page, and beamer's own pages for a ratio are
# rounded to that sort of figure.
ASPECT_TOLERANCE = 0.005

Box = tuple[float, ...]
"""A box in PDF pt, (x0, y0, x1, y1), its numbers as the JSON had them."""
Folds = Mapping[str, Sequence[JsonObject]]
"""The deck's own boxes per frame label (`deck_folds`, a base's `adopt.boxes`), as `object_records`
writes them."""


class FirstSyncRefused(Exception):
    """Raised instead of writing into an adopted deck that must not be written into yet."""

    def __init__(self, message: str, problems: list[JsonObject]) -> None:
        super().__init__(message)
        self.problems = problems


# ---------------------------------------------------------------- reading JSON

def _number(v: Json, where: str) -> float:
    """A JSON number as it is (an int stays an int: a box compares and rounds the same either way)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise JsonShapeError(f"{where}: a number was expected, found {type(v).__name__}")
    return v


def _box(v: Json, where: str) -> Box:
    return tuple(_number(x, f"{where}[{i}]") for i, x in enumerate(as_array(v, where)))


def _strs(xs: Sequence[str]) -> list[Json]:
    return [x for x in xs]


def _objects_json(xs: Sequence[JsonObject]) -> list[Json]:
    return [x for x in xs]


@dataclass(frozen=True, kw_only=True)
class Seen:
    """An element as the pairing and the fold compare it: its kind, its box in PDF pt and its words
    (`conv_words` for the conversion's, `deck_words` for the deck's)."""
    kind: str
    bbox: Box
    text: str


def _seen(el: JsonMap, text: str, where: str) -> Seen:
    return Seen(kind=as_str(el["kind"], f"{where}.kind"), bbox=_box(el["bbox"], f"{where}.bbox"), text=text)


def _record(r: JsonMap, where: str) -> Seen:
    """A record of kind, box and words as `object_records` writes it (and a caller may hand in)."""
    return _seen(r, as_str(r["text"], f"{where}.text"), where)


# ---------------------------------------------------------------- pairing

def deck_objects(slide: JsonMap) -> list[JsonObject]:
    """The elements of an adopt-read slide that really are objects *on* that slide.

    What the layout and the master draw is read into the slide too (`deck_ir.inherited_chain`,
    `inherited`), because that is where a foreign deck keeps its look and adopt has to draw it -
    but those are not the slide's objects, and a sync that wrote to one would edit the template
    under every other slide. A fill `deck_fills` read off the thumbnail has no object at all."""
    return [e for e in as_objects(slide.get("elements", []), "target slide elements")
            if e.get("object") and not e.get("inherited")]


def layout_elements(slide: JsonMap) -> list[JsonObject]:
    """What the deck's layout and its master draw on this slide - `deck_objects`' complement.

    `adopt` recovers those as a beamer theme (`adopt_theme.py`: a background template per layout),
    so the source draws them again on every slide that inherits them and the conversion of that
    source has an element for each. There is nothing on the slide for one to pair with, and that is
    not a miss: the object exists, one level up, where this program may not write."""
    return [e for e in as_objects(slide.get("elements", []), "target slide elements") if e.get("inherited")]


def conv_words(el: JsonMap) -> str:
    """The words of an element of the conversion (classify's IR, which `identity` is written for)."""
    return " ".join(identity.plain_text(el).split())


def deck_words(el: JsonMap) -> str:
    """The words of an element of the adopt-read deck.

    Its IR is `deck_ir`'s, which is the same shape as classify's only where the two agree: a deck
    table keeps its rows as plain strings and its `cells` are cell records, not runs, so
    `identity.plain_text` cannot read one. Everything else says its words in `paragraphs` (a shape
    carries its own text) or, for a diagram, in its nodes'."""
    if el.get("kind") == "table":
        rows = as_array(el.get("rows") or [], "deck table rows")
        return " ".join(" ".join(as_str(c, "deck table cell") for c in as_array(row, "deck table row"))
                        for row in rows).strip()
    parts = [identity.run_text(as_array(p.get("runs") or [], "deck paragraph runs"))
             for p in as_objects(el.get("paragraphs") or [], "deck paragraphs")]
    for node in as_objects(el.get("nodes") or [], "deck diagram nodes"):
        # (a node's paragraph is its runs: `identity.plain_text` reads a diagram the same way)
        parts += [identity.run_text(as_array(r, "deck node paragraph"))
                  for r in as_array(node.get("paragraphs") or [], "deck node paragraphs")]
    return " ".join(" ".join(parts).split())


def kind_fit(a: str, b: str) -> float:
    """How much a converted element of kind `a` may stand for a deck element of kind `b`.

    The converter re-reads its own PDF, so the kinds do not have to agree: a rectangle adopt draws
    in tikz comes back as a `shape`, a `diagram` or an `image` depending on what else is around it,
    and a picture is a picture. Kinds that can legitimately swap cost a little; the rest cost a
    lot, and the margin rule then usually refuses the pair outright."""
    if a == b:
        return 1.0
    if "image" in (a, b) or "diagram" in (a, b):
        return 0.7
    return 0.5


def similarity(conv: JsonMap, deck: JsonMap) -> float:
    """`_similarity` of two records of kind, box and words (`object_records`' shape)."""
    return _similarity(_record(conv, "conversion record"), _record(deck, "deck record"))


def _similarity(conv: Seen, deck: Seen) -> float:
    """How surely a converted element is the deck object it was drawn from. Both boxes are in PDF
    pt (`deck_ir.element_of` divides the deck's by the scale), so they are directly comparable."""
    geom = identity._geometry(conv.bbox, deck.bbox)
    if geom <= 0.0:
        return 0.0
    ct, dt = conv.text, deck.text
    if ct or dt:
        score = 0.55 * SequenceMatcher(None, ct, dt, autojunk=False).ratio() + 0.45 * geom
    else:
        score = geom
    return round(kind_fit(conv.kind, deck.kind) * score, 4)


NO_CANDIDATE = "nothing on that slide stands where it does and says what it says"
NOT_BEST = "another element of the source explains the same object better"
AMBIGUOUS = "two of the deck's objects are equally close: which one it is cannot be told"
FROM_LAYOUT = "the deck's layout draws this, not the slide"
IN_A_TABLE = "it stands inside a table of the deck, which the converter reads back as loose words"
DRAWN_FROM = "it was drawn out of an object another element of the source is already tied to"


def pair_elements(conv: Sequence[JsonMap], deck: Sequence[JsonMap]) -> tuple[dict[int, int], dict[int, str]]:
    """Which object of the deck each converted element was drawn from: `{conv index: deck index}`,
    and for every converted element left over, why it has none.

    Mutual best, by a clear margin. A pair is made only when this element's best candidate is also
    that candidate's best element, the score clears `PAIR_SURE`, and the runner-up on *both* sides
    is `PAIR_MARGIN` behind. Two identical boxes side by side (a foreign deck is full of them) pair
    with neither, which is the point: the first sync then says so instead of picking one."""
    a = [_seen(e, conv_words(e), "conversion element") for e in conv]
    b = [_seen(e, deck_words(e), "deck element") for e in deck]
    scores = [[_similarity(x, y) for y in b] for x in a]
    pairs: dict[int, int] = {}
    why: dict[int, str] = {}
    for i, row in enumerate(scores):
        if not row or max(row) < PAIR_SURE:
            why[i] = NO_CANDIDATE
            continue
        order = sorted(range(len(row)), key=lambda j: (-row[j], j))
        j = order[0]
        second = row[order[1]] if len(order) > 1 else 0.0
        column = [scores[k][j] for k in range(len(a))]
        rival = max((column[k] for k in range(len(a)) if k != i), default=0.0)
        if rival > row[j]:
            why[i] = NOT_BEST
        elif row[j] - second < PAIR_MARGIN or row[j] - rival < PAIR_MARGIN:
            why[i] = AMBIGUOUS
        else:
            pairs[i] = j
    return pairs, why


# How far two boxes' sides may differ and still be the same drawing, redrawn: a flat allowance for
# a text box the converter tightened to its ink or a rule it read one point tall, and a quarter of
# the bigger side for everything that scales with the drawing itself.
SAME_SLACK = 8.0
SAME_SHARE = 0.25
# How far past the template's box the same words may stand and still be inside it.
INSIDE_SLACK = 2.0


def _inside(a: Box, b: Box, slack: float) -> bool:
    return a[0] >= b[0] - slack and a[1] >= b[1] - slack and a[2] <= b[2] + slack and a[3] <= b[3] + slack


def same_drawing(a: JsonMap, b: JsonMap) -> bool:
    """`_same_drawing` of two records of kind, box and words (`object_records`' shape)."""
    return _same_drawing(_record(a, "conversion record"), _record(b, "layout record"))


def _same_drawing(a: Seen, b: Seen) -> bool:
    """Whether a converted element and one the layout draws are one drawing seen twice, rather
    than two things that happen to be near each other.

    `identity._geometry` scores a pair by the better of their overlap and how close their centres
    are, because the elements it compares are two readings of one source and a box that moved is
    still that box. Here the second box may be the whole slide - a full-bleed background picture
    the layout draws - and everything a person put in the middle of that slide shares its centre,
    so the distance alone calls a 35 pt icon the template's background. Sizes decide it instead:
    adopt draws the layout's element at its own box and the converter reads it back at that box.
    The exception is words, which the converter tightens to their ink: a layout's `Thank you!`
    placeholder is 296 pt wide and comes back 109, so the same words inside the template's own box
    are the same drawing whatever the room around them."""
    if a.text and a.text == b.text and _inside(a.bbox, b.bbox, INSIDE_SLACK):
        return True
    for lo, hi in ((0, 2), (1, 3)):
        pa, pb = a.bbox[hi] - a.bbox[lo], b.bbox[hi] - b.bbox[lo]
        if abs(pa - pb) > SAME_SLACK + SAME_SHARE * max(pa, pb):
            return False
    return True


def explained_by_layout(conv: Sequence[JsonMap], why: Mapping[int, str], slide: JsonMap) -> set[int]:
    """Of the converted elements the slide's own objects cannot explain, the ones its **layout**
    draws (`layout_elements`).

    Asked second, and only of what `pair_elements` left over, because a slide's own object always
    wins: a person who put a box of their own over the template's is the one this sync writes to.
    And asked without the margin, which the pairing needs and this does not - the margin is there
    to decide *which* object to write to, and the answer here is that there is none to write to at
    all. What it buys is a true word for a miss that is no miss: the ornament and the rule and the
    footer that the recovered theme draws on all thirty slides are not thirty things the pairing
    failed at, and a person told to 'change that element in the deck instead' would look for it on
    the slide and not find it - it lives on the layout, behind Slide > Edit theme."""
    drawn = layout_elements(slide)
    if not drawn:
        return set()
    b = [_seen(e, deck_words(e), "layout element") for e in drawn]
    out: set[int] = set()
    for i in why:
        a = _seen(conv[i], conv_words(conv[i]), "conversion element")
        if any(_similarity(a, y) >= PAIR_SURE and _same_drawing(a, y) for y in b):
            out.add(i)
    return out


def inside_tables(conv: Sequence[JsonMap], why: Mapping[int, str], objects: Sequence[JsonMap]) -> set[int]:
    """Of the converted elements nothing could be tied to, the ones standing inside one of the
    deck's **tables**.

    The converter reads a table somebody drew back as loose words: of the corpus's 42 deck tables
    9 pair, one comes back as a table, and the other 32 arrive as a cell text here and a cell text
    there, with the grid's rules as thin pictures. Every one of those is an element the pairing
    misses - 315 of the 1,078 it still misses after the fold, the biggest thing left - and none of
    them can be helped by pairing harder: there is nothing on the ours side shaped like the thing
    it came from. Putting the words back into the cells was tried and refused
    (`tools/probe_deck_tables.py` rebuilds a grid from the geometry and proves it cell by cell
    against the deck's own rows: none of the 42 comes back right, the rules and pictures inside them
    being words no cell says and a wrapped cell arriving as one band for two rows). Writing one
    cell's words into another's is exactly the mistake nothing downstream could see.

    So it is named instead. A person looking at their slide sees their table standing right there
    and is told that nothing on the slide stands where this element does - true of the element, and
    not what they need to hear. What they need to hear is that it is a cell of that table, that
    this converter cannot write into it, and that the cell is theirs to change in Slides."""
    tables = [_box(o["bbox"], "deck table bbox") for o in objects if o["kind"] == "table"]
    if not tables:
        return set()
    return {i for i in why if any(_holds(_box(conv[i]["bbox"], "conversion element bbox"), t) for t in tables)}


def drawn_from(conv: Sequence[JsonMap], why: Mapping[int, str], pairs: Mapping[int, int],
               objects: Sequence[JsonMap]) -> dict[int, int]:
    """Of the converted elements nothing could be tied to, the ones standing inside an object that
    **another converted element already is** tied to: `{the miss: that element}`.

    The fold's twin, for what the fold cannot join. One box of a person's comes back as several
    elements and only some of them are words: the icon at the head of a line, the picture the
    converter made of a formula in the prose, the rule under the heading. The fold takes the words
    (`composites`) and leaves the rest where they stand, and what is left over has no object -
    nothing on the slide is shaped like the icon alone, because the icon alone was never an object.

    That is not the same nothing as an element the pairing simply missed. An element with no object
    is dangerous because a sync deletes a unit's old objects through the base, so one naming none
    leaves the person's box standing and puts a second one on top of it (`merge.plan_unit`'s blind
    branch). Here the box it came out of *is* named - by the element beside it - and if the two are
    one unit, that one delete takes the object away and the unit is written whole. Nothing is left
    behind, because there was never a second thing there.

    The rule is only half of the answer: which elements share a unit is the merge's question, not
    this one's (`merge.covered`). What this says is which object each miss was drawn out of; the
    merge then asks whether that object is going anyway."""
    tied = {k: i for i, k in pairs.items()}
    out: dict[int, int] = {}
    for i in why:
        for k, mate in tied.items():
            if _holds(_box(conv[i]["bbox"], "conversion element bbox"), _box(objects[k]["bbox"], "deck object bbox")):
                out[i] = mate
                break
    return out


# ---------------------------------------------------------------- one box, read back as several

# How much of a converted element has to lie inside the deck's box for that box to hold it, and
# how near the elements it holds have to come to saying what it says.
HOLDS = 0.85
SAYS = 0.85


def _holds(inner: Box, outer: Box) -> bool:
    """Whether the deck's box holds this converted element whole."""
    x0, y0 = max(inner[0], outer[0]), max(inner[1], outer[1])
    x1, y1 = min(inner[2], outer[2]), min(inner[3], outer[3])
    if x1 <= x0 or y1 <= y0:
        return False
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return area > 0 and (x1 - x0) * (y1 - y0) / area >= HOLDS


def object_records(elements: Sequence[JsonMap]) -> list[JsonObject]:
    """The deck's objects as the fold reads them: what kind they are, where they stand, what they
    say. `build_base` takes them off the deck it just read and a later `sync` off the base, so
    both sides of a sync fold the same source the same way."""
    return [{"object": e.get("object"), "kind": e["kind"], "bbox": e["bbox"], "text": deck_words(e)}
            for e in elements]


def deck_folds(target: JsonObject) -> dict[str, list[JsonObject]]:
    """The deck's own boxes, per frame label: what `convert_source` and `sync.build_ours` fold
    against.

    Keyed by **label** and not by slide index, because folding comes before the slides are paired
    and pairing reads the elements folding changes (`identity.slide_info`). `adopt.frame_labels`
    names every frame after the slide's own `objectId`, so the label is the one name both sides
    already agree on with nothing computed."""
    from .adopt import frame_labels
    return {label: object_records(deck_objects(slide))
            for label, slide in zip(frame_labels(target), as_objects(target.get("slides") or [], "target.slides"))
            if label}


def composites(conv: Sequence[JsonMap], objects: Sequence[JsonMap]) -> dict[int, list[int]]:
    """`_composites` of records of kind, box and words (`object_records`, and the same three read
    off the conversion), so this can be asked of a dump as well as of a live conversion."""
    return _composites([_record(c, "conversion record") for c in conv], [_record(o, "deck box") for o in objects])


def _composites(conv: Sequence[Seen], objects: Sequence[Seen]) -> dict[int, list[int]]:
    """{object index: the converted elements that are that one box of the deck}, in reading order.

    `adopt` writes one `slidebox` per object and the converter reads the compiled page back with
    no idea it was ever one box: where the deck's paragraphs stand more than a line and a half
    apart - a heading over its body, an agenda with air between its items - `classify` calls them
    separate elements, and each of them then pairs with nothing, because the thing each one is
    part of is the whole box. Measured over the corpus, folding takes **455 elements** off the
    1,533 the pairing misses (30%), and it falls hardest on the decks adopt is worst at: 146 of
    intro-lecture's 170 misses, 114 of creandum-board's 135, 38 of gdg24's 136.

    What is folded is the box's **words**: the text elements standing in it, and nothing else.
    Anything else the converter drew inside that box - the rule under its heading, the picture of
    a formula in its prose, an icon the person put on top - is left where it is, because a fold
    writes a text box and a text box cannot carry a drawing. That is the one rule with two faces:
    a drawing inside the box does not refuse the fold, but it does not join it either, so the box
    is folded only if the words that *are* folded still say what it says.

    Folding is a claim about somebody's slide, so it is made only where the words say so and
    refused everywhere else. Of the corpus's 310 objects holding two or more converted texts, 125
    are folded (534 elements) and four rules refuse the other 185:

      * the object has to **be text** (91 are not): the commonest thing holding a crowd of
        elements is a panel or a card, and a fold writes a text box - over a person's filled
        shape it would lose the fill and everything standing on it. A table is refused here too
        (33), and that is the biggest thing this cannot do: the converter reads a table the deck
        drew back as loose words (one of the corpus's 42 comes back as a table at all), and
        putting them back into cells has to be right cell by cell or it writes one cell's words
        into another's - `tools/probe_deck_tables.py`, none of the 42;
      * no element may already say what the object says **on its own** (52 do): that element is
        the box and the others are things standing on it;
      * together they have to say what the object says (42 do not), which is what carries the
        drawings left out above;
      * and the object has to say something at all, or an empty text box reads as one the
        converter split into everything drawn over it - `SequenceMatcher` scores two empty
        strings 1.00, the degenerate match `same_drawing` was written for. (Nothing in the corpus
        reaches it, the object-kind rule catching the empty panels first; it is here because that
        rule is about the fill and this one is about the claim.)

    Last, nothing is folded at all where two objects claim one element: an element cannot be part
    of two boxes, and which box it belongs to is exactly what is not known."""
    out: dict[int, list[int]] = {}
    for k, obj in enumerate(objects):
        if obj.kind != "text" or not obj.text.strip():
            continue
        held = [i for i, e in enumerate(conv) if e.kind == "text" and _holds(e.bbox, obj.bbox)]
        if len(held) < 2:
            continue
        held.sort(key=lambda i: (round(conv[i].bbox[1], 1), conv[i].bbox[0]))
        said = [conv[i].text for i in held]
        if not all(s.strip() for s in said):
            continue
        if any(_says_it(s, obj.text) for s in said):
            continue
        if _says_it(" ".join(said), obj.text):
            out[k] = held
    claimed: dict[int, int] = {}
    for k, held in out.items():
        for i in held:
            claimed[i] = claimed.get(i, 0) + 1
    return {k: held for k, held in out.items() if all(claimed[i] == 1 for i in held)}


def _says_it(said: str, wanted: str) -> bool:
    return SequenceMatcher(None, said, wanted, autojunk=False).ratio() >= SAYS


def fold_composites(elements: list[JsonObject], objects: Sequence[JsonMap]) -> tuple[list[JsonObject], list[str]]:
    """`elements` with each composite folded into the one element the deck has, and the ids that
    went. The order of what is left is the order it came in, the fold standing where its first
    part stood.

    The fold is a concatenation and nothing more, which is the whole reason it is safe to write
    back: `emit` lays a text box out from its paragraphs' own lines - `vertical_layout` reads each
    paragraph's `spaceAbove` off the baselines the page really has - so the gaps that made
    `classify` split the box in the first place come back out of the geometry when it is written
    again. Nothing has to remember what the spacing was, because the spacing was never thrown
    away.

    A picture anchored to a part follows it (`anchor`): a formula or an icon in one of those
    paragraphs belongs to the box the paragraphs are now in."""
    seen = [_seen(e, conv_words(e), f"element {i}") for i, e in enumerate(elements)]
    groups = _composites(seen, [_record(o, "deck box") for o in objects])
    folded = {i: idx for idx in groups.values() for i in idx}
    if not folded:
        return elements, []
    gone: dict[str, str] = {}
    for idx in groups.values():
        for i in idx[1:]:
            gone[as_str(elements[i]["id"], "element id")] = as_str(elements[idx[0]]["id"], "element id")
    out: list[JsonObject] = []
    for i, el in enumerate(elements):
        if i in folded and folded[i][0] != i:
            continue
        if i in folded:
            parts = [elements[j] for j in folded[i]]
            boxes = [seen[j].bbox for j in folded[i]]
            box: list[Json] = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                               max(b[2] for b in boxes), max(b[3] for b in boxes)]
            paragraphs: list[Json] = [p for part in parts for p in as_array(part["paragraphs"], "paragraphs")]
            spans: list[Json] = [s for part in parts for s in as_array(part.get("spans") or [], "spans")]
            strokes: list[Json] = [s for part in parts for s in as_array(part.get("strokes") or [], "strokes")]
            el = {**el, "bbox": box, "paragraphs": paragraphs, "code": all(part.get("code") for part in parts),
                  "spans": spans, "strokes": strokes, "composite": True}
        anchor = el.get("anchor")
        if isinstance(anchor, str) and anchor in gone:
            el = {**el, "anchor": gone[anchor]}
        out.append(el)
    return out, sorted(gone)


def fold_slides(deck: JsonObject, folds: Folds) -> None:
    """Fold every slide of a conversion against the objects of the deck slide it was written from,
    in place. Slides are found by their **label**, which for an adopted deck is a slug of the
    slide's own `objectId` (`adopt.frame_labels`) - the one name both sides of a sync agree on
    without having to pair anything first."""
    for slide in as_objects(deck.get("slides", []), "deck.slides"):
        label = slide.get("label")
        objects = folds.get(as_str(label, "deck slide label") if label else "")
        if objects:
            folded, gone = fold_composites(as_objects(slide["elements"], "deck slide elements"), objects)
            if gone:   # (nothing folded: the slide keeps its own list)
                slide["elements"] = _objects_json(folded)


def _marked(deck: JsonObject, kind: str) -> dict[str, JsonObject]:
    """Our elements of `kind` by their mark."""
    ours: dict[str, JsonObject] = {}
    for s in as_objects(deck.get("slides", []), "deck.slides"):
        for e in as_objects(s["elements"], "deck slide elements"):
            mark = e.get("mark")
            if isinstance(mark, str) and mark and e.get("kind") == kind:
                ours[mark] = e
    return ours


def _rehashed(e: JsonObject, new: JsonObject, page_key: Callable[[int], str] | None) -> bool:
    """In place: base element `e` holding `new` as its IR, hashed as `snapshot.slide_entries` hashes
    (its anchor's key, the base's pages: a table's cell can link to a slide). Whether the hash
    changed."""
    anchor = e.get("anchor")
    h, fields = identity.ir_fields(new, None, anchor if isinstance(anchor, str) else None, page_key)
    was = e.get("ir_hash")
    e.update(ir=new, ir_hash=h, fields={**as_object(e.get("fields") or {}, "base element fields"), **fields})
    return h != was


def upgrade_shapes(base: JsonObject, deck: JsonObject,
                   page_key: Callable[[int], str] | None) -> list[snapshot.Rewritten]:
    """In place: a marked shape an adopt base recorded before 688ebf4 (no `flip` or `radius`, its
    outline a bare colour) in today's form, and hashed again, so an unchanged source is no change.
    The width the old form left out is taken from our element of the same mark when its outline
    is the same colour, and from a shape: one object's crop and outline can share a mark (poster's
    fills). Without this, syncing an unchanged source into china's adopted deck recreated four of
    the person's freeforms (audit, 2026-09-29). `page_key`: the base's (`snapshot.base_page_key`)."""
    if base.get("adopt") is None:
        return []
    ours = _marked(deck, "shape")
    done: list[snapshot.Rewritten] = []
    for slide in as_objects(base.get("slides", []), "base.slides"):
        slide_key = as_str(slide["key"], "base slide key")
        for e in as_objects(slide.get("elements", []), f"base slide {slide_key}: elements"):
            ir = as_object(e.get("ir") or {}, f"base slide {slide_key}: element ir")
            mark = ir.get("mark")
            if e.get("kind") != "shape" or not isinstance(mark, str) or not mark or "flip" in ir:
                continue
            new: JsonObject = {**ir, "flip": False, "radius": 0.0}
            colour = ir.get("outline")
            if isinstance(colour, str):
                mine = ours[mark].get("outline") if mark in ours else None
                width = mine.get("width") if isinstance(mine, dict) and mine.get("color") == colour else None
                new["outline"] = {"color": colour, "width": width if isinstance(width, (int, float)) else 1.0}
            hashed = _rehashed(e, new, page_key)
            done.append(snapshot.Rewritten(slide=slide_key, element=as_str(e["key"], f"base slide {slide_key}: element key"),
                                           how="adopt_shape", hashed=hashed))
    return done


def upgrade_tables(base: JsonObject, deck: JsonObject,
                   page_key: Callable[[int], str] | None) -> list[snapshot.Rewritten]:
    """In place: a marked table (`slidetable`) an adopt base recorded before 688ebf4 - its cells and
    box, no layout (`columns`, `rows`, rules...) - in today's form, so an unchanged source is no
    change. The layout is taken from our table of the same mark, and only when everything the old
    form did say is the same there: a source that changed the table still reads as changed. Without
    this, syncing hashing's unchanged source into its adopted deck recreated the person's table
    (`source_changes` {'style'}, audit 2026-09-29). `page_key`: the base's pages, which our side's
    links are hashed against too (a cell linking to a slide read as changed when hashed without)."""
    if base.get("adopt") is None:
        return []
    ours = _marked(deck, "table")
    done: list[snapshot.Rewritten] = []
    for slide in as_objects(base.get("slides", []), "base.slides"):
        slide_key = as_str(slide["key"], "base slide key")
        for e in as_objects(slide.get("elements", []), f"base slide {slide_key}: elements"):
            ir = as_object(e.get("ir") or {}, f"base slide {slide_key}: element ir")
            mark = ir.get("mark")
            now = ours.get(mark) if isinstance(mark, str) else None
            if e.get("kind") != "table" or now is None or "columns" in ir or "columns" not in now:
                continue
            same = all(json.dumps(v, sort_keys=True) == json.dumps(now.get(k), sort_keys=True)
                       for k, v in ir.items() if k != "id")
            if not same:
                continue
            new: JsonObject = {**now, "id": ir.get("id", now["id"])}
            hashed = _rehashed(e, new, page_key)
            done.append(snapshot.Rewritten(slide=slide_key, element=as_str(e["key"], f"base slide {slide_key}: element key"),
                                           how="adopt_table", hashed=hashed))
    return done


# ---------------------------------------------------------------- the base

@dataclass(frozen=True, kw_only=True)
class Converted:
    """The conversion of the source adopt wrote, as a later sync makes it (`convert_source_of`): the
    planned deck, the folder it was rendered into, the compiled PDF and the plan."""
    deck: JsonObject
    out: Path
    pdf: Path
    plan: DeckPlan


class ConvertedDict(TypedDict):
    """`Converted` as `convert_source` hands it to the callers that read a dict."""
    deck: JsonObject
    out: Path
    pdf: Path
    plan: DeckPlan


def convert_source(tex: Path, work: Path, engine: str | None = None, *, page_width: float,
                   folds: Folds | None) -> tuple[ConvertedDict | None, str]:
    """`convert_source_of` as a dict ({"deck", "out", "pdf", "plan"}), for tools/probe_deck_tables.py."""
    made, err = convert_source_of(tex, work, engine, page_width, folds)
    if made is None:
        return None, err
    return {"deck": made.deck, "out": made.out, "pdf": made.pdf, "plan": made.plan}, ""


def convert_source_of(tex: Path, work: Path, engine: str | None, page_width: float,
                      folds: Folds | None) -> tuple[Converted | None, str]:
    """Compile the source tree at `tex` and convert it exactly as a later `sync` will
    (`sync.build_ours`'s first half): the base's IR side has to be what the *converter* makes of
    that source, not what adopt read from the deck, or every element would read as changed on the
    first sync. `page_width` is the deck's own, for the same reason - the plan's scale, and with it
    every hole width it fits, is PDF pt to *that* deck's points. `folds` is the deck's own boxes
    per frame label (`object_records`), which put back together what one of them the converter read
    as several (`fold_composites`) - applied after the backgrounds are rendered, so a fold changes
    what is *paired*, never what is painted. An element emit cannot plan is the picture of its
    region (`sync.planned`), as the sync's own conversion will make it: the plan's `contained`.
    Returns (the conversion, "") or (None, the compile error)."""
    from .classify import classify
    from .extract import extract, select_overlays
    from .inverse import Workspace
    from .notes import prepare
    from .render import render_backgrounds
    from .sync import planned

    ws = Workspace(Path(tex), work / "compile", engine=engine)
    pdf, err = ws.compile()
    if pdf is None:
        return None, err
    out = work / "ours"
    out.mkdir(parents=True, exist_ok=True)
    prepared = prepare(pdf, out)
    raw = extract(prepared.pdf, prepared.labels)
    for page in raw["pages"]:
        page["notes"] = prepared.notes.get(page["index"])
    raw = select_overlays(raw, "last")
    deck: JsonObject = classify(raw)
    render_backgrounds(prepared.pdf, raw, deck, out)
    if folds:
        fold_slides(deck, folds)
    plan = planned(deck, prepared.pdf, out, page_width)
    return Converted(deck=plan.deck, out=out, pdf=pdf, plan=plan), ""


def _object_id(el: JsonMap) -> ObjectId:
    return ObjectId(as_str(el["object"], "deck object"))


def build_base(conv_deck: JsonObject, conv_out: Path, target: JsonObject, pres: JsonObject, pdf: Path,
               overlays: str, folds: Folds | None) -> JsonObject:
    """`build_base_of` as the JSON base.json holds."""
    return base_json(build_base_of(conv_deck, conv_out, target, pres, pdf, overlays, folds))


def build_base_of(conv_deck: JsonObject, conv_out: Path, target: JsonObject, pres: JsonObject, pdf: Path,
                  overlays: str, folds: Folds | None) -> Base:
    """A base of `snapshot.build_base`'s shape for a deck this converter never wrote.

    The IR side is the conversion of the source adopt left on disk; the deck side is the live
    objects that conversion was drawn from, each element's `objects`/`main` being the person's own
    `objectId` and its `readback` the same normalised read `sync` compares against. No alt text is
    written anywhere: tagging the objects would be a write into someone else's deck, and the base
    naming the ids does the same job."""
    live_deck = presentation(pres, "the adopted deck's presentations.get")
    read = snapshot.read_presentation_of(live_deck)
    page = read.page_size
    if page is None:   # (read_presentation_of refuses a presentation with no pageSize first)
        raise JsonShapeError("the adopted deck's presentations.get has no pageSize")
    by_id = {s.object_id: s for s in read.slides}
    conv_slides = as_objects(conv_deck["slides"], "conversion.slides")
    written: list[snapshot.WrittenSlide] = []
    whys: list[dict[int, str]] = []
    layouts: list[set[int]] = []
    celled: list[set[int]] = []
    mates: list[dict[int, int]] = []
    leftovers: list[list[ObjectId]] = []
    for conv_slide, tgt in zip(conv_slides, as_objects(target["slides"], "target.slides")):
        sid = tgt.get("objectId")
        live = by_id.get(ObjectId(sid)) if isinstance(sid, str) else None
        elements = as_objects(conv_slide["elements"], "conversion slide elements")
        on_slide = [e for e in deck_objects(tgt) if live is not None and _object_id(e) in live.objects]
        pairs, why = pair_elements(elements, on_slide)
        layouts.append(explained_by_layout(elements, why, tgt))
        celled.append(inside_tables(elements, why, on_slide) - layouts[-1])
        mates.append({i: j for i, j in drawn_from(elements, why, pairs, on_slide).items()
                      if i not in layouts[-1] and i not in celled[-1]})
        written.append(snapshot.WrittenSlide(
            object_id=ObjectId(sid) if isinstance(sid, str) else None,
            objects=tuple((_object_id(on_slide[pairs[i]]),) if i in pairs else () for i in range(len(elements))),
            groups=(), table_margins={}))
        whys.append(why)
        taken = {_object_id(on_slide[k]) for k in pairs.values()}
        # Every object standing on the slide that this conversion is not tied to, and not only the
        # ones the read made an element of: a person's own **group** is no drawing at all (its
        # children are the elements, `deck_ir(foreign=True)` never folds it) and a shape that draws
        # nothing - an empty placeholder, fill and outline switched off, a fill at alpha 0 - is read
        # as no element either (`deck_ir.foreign_shape`), so neither could ever be named below and both read as
        # objects the person had just added (2,866 groups and 3,845 blank shapes over the corpus,
        # on 214 of 912 slides). They are on the slide and nobody added them.
        leftovers.append([oid for oid in live.objects if oid not in taken] if live is not None else [])
    page_w = _number(as_array(conv_slides[0]["size"], "conversion slide size")[0], "conversion slide size") \
        if conv_slides else SLIDE_W
    scale = (page[0] / page_w) if page_w else None
    base = snapshot.build_base_of(conv_deck, conv_out, live_deck, written, scale, Path(pdf), 0, False, overlays, None)
    slides = list(base.slides)
    for n, (entry, oids) in enumerate(zip(slides, leftovers)):
        # The person's own objects this conversion draws nothing for. They are on the slide and
        # they are not an edit: `merge.slide_touched` would otherwise read a deck a person built
        # as one they had just added objects to, and say so about every slide of it. Recorded per
        # slide and not only in the summary below, because that is where the merge reads, and
        # carried by every later base (`sync.new_base`): they are never adopted, so a sync that
        # forgot them would start calling them added at generation 2.
        if oids:
            slides[n] = replace(entry, left_alone=tuple(oids))
    unpaired: list[Json] = []
    from_layout: list[Json] = []
    from_box: list[Json] = []
    for n, (entry, why, lay, cells, mate) in enumerate(zip(slides, whys, layouts, celled, mates)):
        elements = list(entry.elements)
        for i, el in enumerate(entry.elements):
            if i not in why:
                continue
            if i in lay:
                # The merge has to know one from the other, and it reads the base's elements and
                # not this summary (`merge.plan_unit`'s `blind`).
                elements[i] = replace(el, from_layout=True)
            elif i in cells:
                elements[i] = replace(el, in_table=True)
            elif i in mate:
                elements[i] = replace(el, drawn_from=entry.elements[mate[i]].key)
            item: JsonObject = {"slide": entry.key, "element": el.key, "kind": el.kind,
                                "why": FROM_LAYOUT if i in lay else IN_A_TABLE if i in cells else
                                DRAWN_FROM if i in mate else why[i]}
            (from_layout if i in lay else from_box if i in mate else unpaired).append(item)
        slides[n] = replace(entry, elements=tuple(elements))
    paired = sum(1 for e in slides for el in e.elements if el.main)
    left_alone: list[Json] = [{"slide": e.key, "objects": _strs(oids)} for e, oids in zip(slides, leftovers) if oids]
    boxes: JsonObject = {label: _objects_json(objects) for label, objects in (folds or {}).items()}
    adopt: JsonObject = {"presentationId": read.presentation_id, "deck_page_size": [x for x in page],
                         "frame_width": page[0], "slides": len(slides), "paired": paired,
                         "unpaired": unpaired, "from_layout": from_layout, "drawn_from": from_box,
                         "left_alone": left_alone,
                         # The boxes this conversion was folded against, so that every later sync folds
                         # its own conversion the same way (`sync.build_ours`). They are the deck's
                         # geometry and not a decision, which is why they are recorded rather than the
                         # folds themselves: the source changes, the boxes do not.
                         "boxes": boxes}
    # The master's background belongs to the person's deck, not to this conversion: naming one
    # would let a source background change copy the *deck's* master fill onto a slide
    # (`sync.background_requests`). Every background this base writes is written explicitly.
    return replace(base, master_background=None, origin=ORIGIN, slides=tuple(slides), adopt=adopt)


def labels_match(conv_deck: JsonMap, target: JsonObject) -> str | None:
    """Why the conversion's slides are not the deck's slides, one for one (None: they are).

    `adopt.frame_labels` writes a label per deck slide, slugged from its `objectId`; the compiled
    PDF carries them back as named destinations and `classify` puts them on the slides. If that
    chain does not come out exactly as it went in, nothing else in this file may be believed - the
    base would tie one slide's source to another slide's objects."""
    from .adopt import frame_labels
    want = frame_labels(target)
    got = [s.get("label") for s in as_objects(conv_deck["slides"], "conversion.slides")]
    if len(got) != len(want):
        return (f"the source compiles to {len(got)} slide(s) and the deck has {len(want)}: "
                f"a frame per slide is what ties the two together")
    wrong = [(w, g) for w, g in zip(want, got) if w != g]
    if wrong:
        return (f"{len(wrong)} frame(s) do not carry the label adopt wrote (first: `{wrong[0][0]}` came back as "
                f"`{wrong[0][1] or 'no label'}`): the slides cannot be paired with the deck's")
    return None


def record(tex: Path, work: Path, target: JsonObject, pres: JsonObject, engine: str | None, overlays: str = "last",
           *, log: Callable[[str], None]) -> tuple[JsonObject | None, str | None]:
    """The whole of it: compile the source adopt wrote, pair its conversion with the deck it was
    written from, and return (base, None) or (None, why there is none). Never writes to Google."""
    if not target.get("slides"):
        return None, "the deck has no slides"
    if not pres.get("slides"):
        return None, "the deck was read without its presentation (no read-back to record)"
    log("recording a sync base for the adopted deck...")
    folds = deck_folds(target)
    width = snapshot.page_size(presentation(pres, "the adopted deck's presentations.get"))[0]
    conv, err = convert_source_of(Path(tex), Path(work), engine, float(width), folds)
    if conv is None:
        return None, f"the source does not compile:\n{err}"
    for c in conv.plan.contained:
        log(f"warning: slide {c['page'] + 1}: {c['kind']} {c['id']} could not be planned ({c['error']}); "
            f"the base records a picture of it, as a sync will write it")
    problem = labels_match(conv.deck, target)
    if problem:
        return None, problem
    base = build_base(conv.deck, conv.out, target, pres, conv.pdf, overlays, folds)
    return base, None


def store(base: Mapping[str, object], out: Path, drive: DriveService | None) -> Path:
    """Write the base where `sync --deck <folder>` looks for it. Drive only when asked for."""
    path = snapshot.save_local(base, Path(out))
    if drive is not None:
        snapshot.save_drive(drive, base, None, None)
    return path


def next_command(pdf: Path | str, out: Path) -> str:
    return f"python -m beamer2slides sync {pdf} --deck {out}"


# ---------------------------------------------------------------- the first sync

def deck_page(base: JsonMap) -> list[float]:
    size = base.get("deck_page_size")
    if not size:
        adopt = base.get("adopt")
        size = as_object(adopt, "base.adopt").get("deck_page_size") if adopt else None
    if not size:
        return [SLIDE_W]
    return [_number(x, "base.deck_page_size") for x in as_array(size, "base.deck_page_size")]


def deck_width(base: JsonMap) -> float:
    """How wide the deck is, in slide pt - which is what `sync` plans its boxes in. `convert` makes
    a deck SLIDE_W wide and nothing else; a deck `adopt` took over is whatever the person made."""
    return float(deck_page(base)[0])


def aspect_mismatch(base: JsonMap) -> tuple[float, float] | None:
    """(the deck's aspect, the compiled page's) when one scale cannot carry the plan onto the deck.

    `emit.DeckPlan` turns the PDF's points into the deck's with a single number, so a page the
    source compiles to that is not the deck's shape puts everything right in x and wrong in y (or
    the other way about) - silently, since the boxes are valid. `adopt` writes the page from the
    deck (`deck_ir.page_size_for`, `adopt.page_setup`), so this is a source somebody changed the
    paper of, not the ordinary case."""
    deck, size = deck_page(base), base.get("page_size")
    if not size:
        return None
    page = [_number(x, "base.page_size") for x in as_array(size, "base.page_size")]
    if len(deck) < 2 or not page[1] or not deck[1]:
        return None
    a, b = deck[0] / deck[1], page[0] / page[1]
    return None if abs(a - b) <= ASPECT_TOLERANCE * b else (a, b)


def _creations(mplan: JsonMap) -> list[JsonObject]:
    """Slides and element units this plan would write objects for."""
    slides = as_objects(mplan["slides"], "plan.slides")
    out: list[JsonObject] = [{"slide": p["key"], "element": None} for p in slides if p["action"] == "create"]
    for p in slides:
        if p["action"] != "update":
            continue
        out += [{"slide": p["key"], "element": u["key"]} for u in as_objects(p["units"], "plan.units")
                if u["action"] in ("create", "recreate")]
    return out


def _named(slide: str, element: str | None) -> JsonObject:
    return {"slide": slide, "element": element}


def _creations_of(mplan: merge.MergePlan) -> list[JsonObject]:
    """`_creations` of a typed plan."""
    out = [_named(p.key, None) for p in mplan.slides if isinstance(p, merge.CreateSlide)]
    for p in mplan.slides:
        if isinstance(p, merge.UpdateSlide):
            out += [_named(p.key, u.key) for u in p.units if isinstance(u.decision, merge.CreateUnit | merge.Recreate)]
    return out


def touches_deck(mplan: JsonMap, theirs: JsonMap) -> bool:
    """Whether this plan would put anything into the deck. `merge.has_writes` asks whether objects
    are made, removed, moved or reordered; a unit the source merely reworded is written too, and a
    sync that only does that is still the first one to touch a person's deck."""
    if merge.has_writes(mplan, _live_order(theirs)):
        return True
    return any(u.get("source") for p in as_objects(mplan["slides"], "plan.slides") if p["action"] == "update"
               for u in as_objects(p["units"], "plan.units"))


def touches_deck_of(mplan: merge.MergePlan, theirs: JsonMap) -> bool:
    """`touches_deck` of a typed plan."""
    if _has_writes_of(mplan, _live_order(theirs)):
        return True
    return any(isinstance(u.decision, merge.KeepUnit | merge.Recreate | merge.AdoptUnit | merge.MoveUnit)
               and u.decision.source
               for p in mplan.slides if isinstance(p, merge.UpdateSlide) for u in p.units)


def _live_order(theirs: JsonMap) -> list[str]:
    return [as_str(s["objectId"], "the deck's slide objectId") for s in as_objects(theirs["slides"], "the deck.slides")]


def _has_writes_of(mplan: merge.MergePlan, live_order: Sequence[str]) -> bool:
    """`merge.has_writes` of a typed plan: a slide created or deleted, a unit created, recreated,
    deleted or moved, a background or notes written, or the slides put in another order."""
    for p in mplan.slides:
        if isinstance(p, merge.CreateSlide | merge.DeleteSlide):
            return True
        if isinstance(p, merge.UpdateSlide) and (
                any(isinstance(u.decision, merge.CreateUnit | merge.Recreate | merge.DeleteUnit | merge.MoveUnit)
                    for u in p.units)
                or (p.background_written and p.background) or p.notes is not None):
            return True
    final = [x for x in mplan.order if not x.startswith("new:")]
    current = [s for s in live_order if s in final]
    return current != [s for s in final if s in current]


def problems(base: JsonObject, mplan: JsonMap, theirs: JsonMap, way_back: JsonObject | None,
             backup_mode: str | None = "auto") -> list[JsonObject]:
    """`problems_of` over the plan's JSON (`merge.plan_merge`), for the callers that hold that:
    devtools/fuzz_sync.py and the tests. No parser takes a plan's JSON back to a `MergePlan`, so
    the plan is read here as JSON and the rest is shared (`_refusals`)."""
    if base.get("origin") != ORIGIN:
        return []
    if not touches_deck(mplan, theirs):
        return []
    blind: list[Json] = []
    for p in as_objects(mplan["slides"], "plan.slides"):
        if p["action"] != "update" or p.get("base") is None:
            continue
        b = as_objects(base["slides"], "base.slides")[as_int(p["base"], "plan slide base")]
        # The unit's members as the merge itself read them (`merge.units`), not as a map of their
        # keys: see `problems_of`.
        bunits = merge.units(as_objects(b["elements"], "base slide elements"))
        for u in as_objects(p["units"], "plan.units"):
            if u["action"] not in ("recreate", "move"):
                continue
            blind += [{"slide": b["key"], "element": mk}
                      for mk in merge.blind_members(bunits.get(as_str(u["key"], "plan unit key")) or [])]
    deleted: list[Json] = [p["key"] for p in as_objects(mplan["slides"], "plan.slides") if p["action"] == "delete"]
    return _refusals(base, _creations(mplan), blind, deleted, way_back, backup_mode)


def problems_of(base: JsonObject, mplan: merge.MergePlan, theirs: JsonObject, way_back: JsonObject | None,
                backup_mode: str) -> list[JsonObject]:
    """Why this sync into an adopted deck must not be written (empty: it may be).

    Everything here is about the one thing an adopted deck cannot offer: a way of telling this
    converter's work from a person's. In a converted deck every object is ours and the merge may
    replace it; here every object is theirs, and the only claim we have on one is the pairing
    `build_base` made.

    Two of the four outlive the first sync, because what they are about does not heal by being
    written to once: an element the pairing could not tie to an object never gets one (every sync
    refuses to write it, so no sync ever gives it one), and the deck's page stays the size it is.
    The other two - the way back and the deck's own slides - are about a deck nothing has been
    written to yet, which is true exactly once. Found by the offline campaign at chain depth 2
    (`fuzz_sync._doubled`), where a base rebased after one sync let the second one duplicate an
    unpaired box.

    Two of them are gates behind a decision the merge now makes itself, per element and per slide:
    `merge.plan_unit` keeps an unpaired unit as the deck has it and `plan_merge` keeps a slide no
    frame accounts for (`keep_removed`), both with a conflict or a warning naming it, so an
    ordinary sync brings neither here and the rest of the deck syncs (between them they used to
    refuse whole syncs by the hundred - see the comments there). What is left of them is the last
    thing between a plan and a write into somebody's deck, for a plan that says otherwise however
    it came to say it."""
    if base.get("origin") != ORIGIN:
        return []
    if not touches_deck_of(mplan, theirs):
        return []
    parsed = parse_base(base)
    blind: list[Json] = []
    for p in mplan.slides:
        if not isinstance(p, merge.UpdateSlide):
            continue
        b = parsed.slides[p.base]
        # The unit's members as the merge itself read them (`merge.units_of`), not as a map of their
        # keys: a base slide can answer to one key twice - a unit kept though the source dropped it
        # keeps the key the next conversion has since given to something else - and looking one up
        # read the person's own unpaired icon as a member of the source's new unit, refusing this
        # whole sync over a unit that was never blind (adopt-shaped seed 86066 at chain 8). That is
        # fixed where it is made (`merge.keys_the_source_took`); this is the gate not asking the
        # question in a way the answer can depend on.
        bunits = merge.units_of(b.elements)
        for u in p.units:
            if not isinstance(u.decision, merge.Recreate | merge.MoveUnit):
                continue
            blind += [_named(b.key, mk) for mk in merge.blind_members_of(bunits.get(u.key) or [])]
    deleted: list[Json] = [p.key for p in mplan.slides if isinstance(p, merge.DeleteSlide)]
    return _refusals(base, _creations_of(mplan), blind, deleted, way_back, backup_mode)


def _refusals(base: JsonMap, makes: list[JsonObject], blind: list[Json], deleted: list[Json],
              way_back: JsonObject | None, backup_mode: str | None) -> list[JsonObject]:
    """The refusals of a plan that touches an adopted deck, from what it creates (`makes`), the
    members of the units it writes that nothing ties to an object (`blind`) and the slides it
    deletes."""
    out: list[JsonObject] = []
    shape = aspect_mismatch(base)
    if makes and shape is not None:
        out.append({"reason": "page-shape", "deck": shape[0], "page": shape[1],
                    "creations": _objects_json(makes[:3]), "count": len(makes)})
    if blind:
        out.append({"reason": "unpaired", "elements": blind})
    if base.get("generation", 0) != 0:
        return out
    if backup_mode not in ("none", None) and not guard.way_back_kept(way_back or {}):
        out.append({"reason": "no-way-back", "warnings": (way_back or {}).get("warnings", [])})
    if deleted:
        out.append({"reason": "slides-deleted", "slides": deleted})
    return out


HEAD = "refusing to sync into this adopted deck"
WHY = ("  This deck was made by a person, not by this converter: none of its objects carries a tag "
       "saying which\n  part of the source it came from. `adopt` tied them to the source it wrote by "
       "where they stand and\n  what they say, and this sync would go past what that pairing can carry.")
# How many names a refusal lists before it says there are more.
SOME = 3


def _some(names: Sequence[str], most: int) -> str:
    """The first few of them, ending the sentence."""
    return ", ".join(names[:most]) + (", ..." if len(names) > most else ".")


def _lines(p: JsonMap) -> list[str]:
    reason = as_str(p["reason"], "refusal reason")
    if reason == "no-way-back":
        out = ["  - no way back: no backup of the deck was kept, and Drive's version history cannot be read "
               "back (docs/sync.md)."]
        out += [f"      {w}" for w in as_array(p.get("warnings", []), "refusal warnings")]
        return out
    if reason == "slides-deleted":
        slides = [as_str(s, "refusal slide") for s in as_array(p["slides"], "refusal slides")]
        return [f"  - {len(slides)} slide(s) of the deck would be deleted, because no frame of the source "
                f"accounts for them any more: {_some(slides, SOME)}",
                "      On the first sync that is usually a label that moved, not a slide the author meant to drop."]
    if reason == "unpaired":
        named = [as_str(e["slide"], "refusal slide") + "/" + as_str(e["element"], "refusal element")
                 for e in as_objects(p["elements"], "refusal elements")]
        return [f"  - {len(named)} element(s) the source changed could not be tied to any object of the "
                f"deck: {_some(named, SOME)}",
                "      Writing them would put a second object beside the person's, not over it."]
    if reason == "page-shape":
        return [f"  - the deck's slides are {_number(p['deck'], 'refusal deck'):.3f} wide for every 1 high and "
                f"the page the source compiles to is {_number(p['page'], 'refusal page'):.3f},",
                f"      so the {p['count']} object(s) this sync would create land at the right place across "
                f"and the wrong one down."]
    return [f"  - {reason}"]


def refusal_message(pid: str, out: Path, pdf: Path | str, problems_found: Sequence[JsonMap]) -> str:
    """What a person sees instead of a sync that could not be trusted. Nothing was written."""
    cmd = next_command(pdf, out)
    reasons = {as_str(p["reason"], "refusal reason") for p in problems_found}
    lines = [f"{HEAD}: {len(problems_found)} thing(s) about it cannot be trusted.",
             f"  {guard.deck_url(pid)}", WHY]
    for p in problems_found:
        lines += _lines(p)
    ways = [("see what it would write, writing nothing", f"{cmd} --dry-run")]
    if "no-way-back" in reasons:
        ways.append(("keep a copy of the deck in Drive first", f"{cmd} --backup drive"))
        ways.append(("keep a .pptx of the deck first", f"{cmd} --backup file"))
    if "slides-deleted" in reasons:
        ways.append(("put the frame labels back where adopt wrote them (docs/labels.md), then sync again", ""))
    if "unpaired" in reasons:
        ways.append(("change those elements in the deck instead of in the source, and sync the rest", ""))
    if "page-shape" in reasons:
        ways.append(("give the source back the paper adopt wrote for it (`\\geometry`, docs/sync.md)", ""))
        ways.append(("convert the source into a deck of its own", f"python -m beamer2slides convert {pdf}"))
    ways.append(("write it anyway, saying so out loud", f"{cmd} --force-adopted-deck"))
    width = min(max(len(label) for label, command in ways if command), 44)
    lines.append("  Nothing was written. What to do instead:")
    lines += [f"    {label.ljust(width)}  {command}".rstrip() if command else f"    {label}"
              for label, command in ways]
    return "\n".join(lines)


def report_lines(base: JsonMap) -> list[str]:
    """What `adopt` prints about the base it just recorded, and `sync` about the one it read."""
    adopt = base.get("adopt")
    info: JsonObject = as_object(adopt, "base.adopt") if adopt else {}
    drawn = as_array(info.get("from_layout") or [], "base.adopt.from_layout")
    out_of = as_array(info.get("drawn_from") or [], "base.adopt.drawn_from")
    unpaired = as_array(info.get("unpaired") or [], "base.adopt.unpaired")
    paired = info.get("paired", 0)
    total = as_int(paired, "base.adopt.paired") + len(unpaired) + len(drawn) + len(out_of)
    lines = [f"sync base: {info.get('slides', 0)} slides, {paired} of {total} elements tied to an "
             f"object of the deck"]
    if drawn:
        lines.append(f"  {len(drawn)} of them are drawn by the deck's own layouts and master, which this "
                     f"converter never writes to: change those on the layout, in Slides")
    if out_of:
        lines.append(f"  {len(out_of)} of them this converter drew out of a box beside them (an icon in a "
                     f"line, a formula in prose): those go in with that box")
    if unpaired:
        lines.append(f"  {len(unpaired)} element(s) could not be tied to one; a sync that changes one "
                     f"keeps the deck's version of it and says so in the report")
    left = sum(len(as_array(x["objects"], "base.adopt.left_alone objects"))
               for x in as_objects(info.get("left_alone") or [], "base.adopt.left_alone"))
    if left:
        lines.append(f"  {left} object(s) of the deck no element of the source is tied to (the boxes it "
                     f"draws nothing for, and the deck's own groups around them): sync never touches them")
    return lines


def load_of(path: Path) -> JsonObject:
    """A base.json, read."""
    return as_object(json.loads(Path(path).read_text(encoding="utf-8")), str(path))


def load(path: Path) -> dict:
    """`load_of` untyped, for agent/source_tools.py, which does arithmetic on what it reads."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
