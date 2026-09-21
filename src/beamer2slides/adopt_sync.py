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
"""

import json
from difflib import SequenceMatcher
from pathlib import Path

from . import guard, identity, merge, snapshot
from .emit import SLIDE_W

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


class FirstSyncRefused(Exception):
    """Raised instead of writing into an adopted deck that must not be written into yet."""

    def __init__(self, message: str, problems: list[dict]):
        super().__init__(message)
        self.problems = problems


# ---------------------------------------------------------------- pairing

def deck_objects(slide: dict) -> list[dict]:
    """The elements of an adopt-read slide that really are objects *on* that slide.

    What the layout and the master draw is read into the slide too (`deck_ir.inherited_chain`,
    `inherited`), because that is where a foreign deck keeps its look and adopt has to draw it -
    but those are not the slide's objects, and a sync that wrote to one would edit the template
    under every other slide. A fill `deck_fills` read off the thumbnail has no object at all."""
    return [e for e in slide.get("elements", []) if e.get("object") and not e.get("inherited")]


def layout_elements(slide: dict) -> list[dict]:
    """What the deck's layout and its master draw on this slide - `deck_objects`' complement.

    `adopt` recovers those as a beamer theme (`adopt_theme.py`: a background template per layout),
    so the source draws them again on every slide that inherits them and the conversion of that
    source has an element for each. There is nothing on the slide for one to pair with, and that is
    not a miss: the object exists, one level up, where this program may not write."""
    return [e for e in slide.get("elements", []) if e.get("inherited")]


def conv_words(el: dict) -> str:
    """The words of an element of the conversion (classify's IR, which `identity` is written for)."""
    return " ".join(identity.plain_text(el).split())


def deck_words(el: dict) -> str:
    """The words of an element of the adopt-read deck.

    Its IR is `deck_ir`'s, which is the same shape as classify's only where the two agree: a deck
    table keeps its rows as plain strings and its `cells` are cell records, not runs, so
    `identity.plain_text` cannot read one. Everything else says its words in `paragraphs` (a shape
    carries its own text) or, for a diagram, in its nodes'."""
    if el.get("kind") == "table":
        return " ".join(" ".join(row) for row in el.get("rows") or []).strip()
    parts = [identity.run_text(p.get("runs") or []) for p in el.get("paragraphs") or []]
    for node in el.get("nodes") or []:
        parts += [identity.run_text(r) for r in node.get("paragraphs") or []]
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


def similarity(conv: dict, deck: dict) -> float:
    """How surely a converted element is the deck object it was drawn from. Both boxes are in PDF
    pt (`deck_ir.element_of` divides the deck's by the scale), so they are directly comparable."""
    geom = identity._geometry(conv["bbox"], deck["bbox"])
    if geom <= 0.0:
        return 0.0
    ct, dt = conv["text"], deck["text"]
    if ct or dt:
        score = 0.55 * SequenceMatcher(None, ct, dt, autojunk=False).ratio() + 0.45 * geom
    else:
        score = geom
    return round(kind_fit(conv["kind"], deck["kind"]) * score, 4)


NO_CANDIDATE = "nothing on that slide stands where it does and says what it says"
NOT_BEST = "another element of the source explains the same object better"
AMBIGUOUS = "two of the deck's objects are equally close: which one it is cannot be told"
FROM_LAYOUT = "the deck's layout draws this, not the slide"
IN_A_TABLE = "it stands inside a table of the deck, which the converter reads back as loose words"
DRAWN_FROM = "it was drawn out of an object another element of the source is already tied to"


def pair_elements(conv: list[dict], deck: list[dict]) -> tuple[dict[int, int], dict[int, str]]:
    """Which object of the deck each converted element was drawn from: `{conv index: deck index}`,
    and for every converted element left over, why it has none.

    Mutual best, by a clear margin. A pair is made only when this element's best candidate is also
    that candidate's best element, the score clears `PAIR_SURE`, and the runner-up on *both* sides
    is `PAIR_MARGIN` behind. Two identical boxes side by side (a foreign deck is full of them) pair
    with neither, which is the point: the first sync then says so instead of picking one."""
    a = [{"kind": e["kind"], "bbox": e["bbox"], "text": conv_words(e)} for e in conv]
    b = [{"kind": e["kind"], "bbox": e["bbox"], "text": deck_words(e)} for e in deck]
    scores = [[similarity(x, y) for y in b] for x in a]
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


def _inside(a: list[float], b: list[float], slack: float = 2.0) -> bool:
    return a[0] >= b[0] - slack and a[1] >= b[1] - slack and a[2] <= b[2] + slack and a[3] <= b[3] + slack


def same_drawing(a: dict, b: dict) -> bool:
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
    if a["text"] and a["text"] == b["text"] and _inside(a["bbox"], b["bbox"]):
        return True
    for lo, hi in ((0, 2), (1, 3)):
        pa, pb = a["bbox"][hi] - a["bbox"][lo], b["bbox"][hi] - b["bbox"][lo]
        if abs(pa - pb) > SAME_SLACK + SAME_SHARE * max(pa, pb):
            return False
    return True


def explained_by_layout(conv: list[dict], why: dict[int, str], slide: dict) -> set[int]:
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
    b = [{"kind": e["kind"], "bbox": e["bbox"], "text": deck_words(e)} for e in drawn]
    out = set()
    for i in why:
        a = {"kind": conv[i]["kind"], "bbox": conv[i]["bbox"], "text": conv_words(conv[i])}
        if any(similarity(a, y) >= PAIR_SURE and same_drawing(a, y) for y in b):
            out.add(i)
    return out


def inside_tables(conv: list[dict], why: dict[int, str], objects: list[dict]) -> set[int]:
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
    tables = [o for o in objects if o["kind"] == "table"]
    if not tables:
        return set()
    return {i for i in why if any(_holds(conv[i]["bbox"], t["bbox"]) for t in tables)}


def drawn_from(conv: list[dict], why: dict[int, str], pairs: dict[int, int],
               objects: list[dict]) -> dict[int, int]:
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
    out = {}
    for i in why:
        for k, mate in tied.items():
            if _holds(conv[i]["bbox"], objects[k]["bbox"]):
                out[i] = mate
                break
    return out


# ---------------------------------------------------------------- one box, read back as several

# How much of a converted element has to lie inside the deck's box for that box to hold it, and
# how near the elements it holds have to come to saying what it says.
HOLDS = 0.85
SAYS = 0.85


def _holds(inner: list[float], outer: list[float]) -> bool:
    """Whether the deck's box holds this converted element whole."""
    x0, y0 = max(inner[0], outer[0]), max(inner[1], outer[1])
    x1, y1 = min(inner[2], outer[2]), min(inner[3], outer[3])
    if x1 <= x0 or y1 <= y0:
        return False
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return area > 0 and (x1 - x0) * (y1 - y0) / area >= HOLDS


def object_records(elements: list[dict]) -> list[dict]:
    """The deck's objects as the fold reads them: what kind they are, where they stand, what they
    say. `build_base` takes them off the deck it just read and a later `sync` off the base, so
    both sides of a sync fold the same source the same way."""
    return [{"object": e.get("object"), "kind": e["kind"], "bbox": e["bbox"], "text": deck_words(e)}
            for e in elements]


def deck_folds(target: dict) -> dict[str, list[dict]]:
    """The deck's own boxes, per frame label: what `convert_source` and `sync.build_ours` fold
    against.

    Keyed by **label** and not by slide index, because folding comes before the slides are paired
    and pairing reads the elements folding changes (`identity.slide_info`). `adopt.frame_labels`
    names every frame after the slide's own `objectId`, so the label is the one name both sides
    already agree on with nothing computed."""
    from .adopt import frame_labels
    return {label: object_records(deck_objects(slide))
            for label, slide in zip(frame_labels(target), target.get("slides") or []) if label}


def composites(conv: list[dict], objects: list[dict]) -> dict[int, list[int]]:
    """{object index: the converted elements that are that one box of the deck}, in reading order.
    Both sides are records of kind, box and words (`object_records`, and the same three read off
    the conversion), so this can be asked of a dump as well as of a live conversion.

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
        if obj["kind"] != "text" or not obj["text"].strip():
            continue
        held = [i for i, e in enumerate(conv) if e["kind"] == "text" and _holds(e["bbox"], obj["bbox"])]
        if len(held) < 2:
            continue
        held.sort(key=lambda i: (round(conv[i]["bbox"][1], 1), conv[i]["bbox"][0]))
        said = [conv[i]["text"] for i in held]
        if not all(s.strip() for s in said):
            continue
        if any(_says_it(s, obj["text"]) for s in said):
            continue
        if _says_it(" ".join(said), obj["text"]):
            out[k] = held
    claimed: dict[int, int] = {}
    for k, held in out.items():
        for i in held:
            claimed[i] = claimed.get(i, 0) + 1
    return {k: held for k, held in out.items() if all(claimed[i] == 1 for i in held)}


def _says_it(said: str, wanted: str) -> bool:
    return SequenceMatcher(None, said, wanted, autojunk=False).ratio() >= SAYS


def fold_composites(elements: list[dict], objects: list[dict]) -> tuple[list[dict], list[str]]:
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
    groups = composites([{"kind": e["kind"], "bbox": e["bbox"], "text": conv_words(e)} for e in elements],
                        objects)
    folded = {i: idx for idx in groups.values() for i in idx}
    if not folded:
        return elements, []
    gone: dict[str, str] = {}
    for idx in groups.values():
        for i in idx[1:]:
            gone[elements[i]["id"]] = elements[idx[0]]["id"]
    out = []
    for i, el in enumerate(elements):
        if i in folded and folded[i][0] != i:
            continue
        if i in folded:
            parts = [elements[j] for j in folded[i]]
            box = [min(p["bbox"][0] for p in parts), min(p["bbox"][1] for p in parts),
                   max(p["bbox"][2] for p in parts), max(p["bbox"][3] for p in parts)]
            el = {**el, "bbox": box,
                  "paragraphs": [p for part in parts for p in part["paragraphs"]],
                  "code": all(part.get("code") for part in parts),
                  "spans": [s for part in parts for s in part.get("spans") or []],
                  "strokes": [s for part in parts for s in part.get("strokes") or []],
                  "composite": True}
        if el.get("anchor") in gone:
            el = {**el, "anchor": gone[el["anchor"]]}
        out.append(el)
    return out, sorted(gone)


def fold_slides(deck: dict, folds: dict[str, list[dict]]) -> None:
    """Fold every slide of a conversion against the objects of the deck slide it was written from,
    in place. Slides are found by their **label**, which for an adopted deck is a slug of the
    slide's own `objectId` (`adopt.frame_labels`) - the one name both sides of a sync agree on
    without having to pair anything first."""
    for slide in deck.get("slides", []):
        objects = folds.get(slide.get("label") or "")
        if objects:
            slide["elements"], _ = fold_composites(slide["elements"], objects)


# ---------------------------------------------------------------- the base

def convert_source(tex: Path, work: Path, engine: str | None = None,
                   page_width: float = SLIDE_W,
                   folds: dict[str, list[dict]] | None = None) -> tuple[dict | None, str]:
    """Compile the source tree at `tex` and convert it exactly as a later `sync` will
    (`sync.build_ours`'s first half): the base's IR side has to be what the *converter* makes of
    that source, not what adopt read from the deck, or every element would read as changed on the
    first sync. `page_width` is the deck's own, for the same reason - the plan's scale, and with it
    every hole width it fits, is PDF pt to *that* deck's points. `folds` is the deck's own boxes
    per frame label (`object_records`), which put back together what one of them the converter read
    as several (`fold_composites`) - applied after the backgrounds are rendered, so a fold changes
    what is *paired*, never what is painted.
    Returns ({"deck", "out", "pdf", "plan"}, "") or (None, the compile error)."""
    from .classify import classify
    from .emit import DeckPlan, merge_blocks
    from .extract import extract, select_overlays
    from .inverse import Workspace
    from .notes import prepare
    from .render import render_backgrounds

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
    deck = classify(raw)
    render_backgrounds(prepared.pdf, raw, deck, out)
    if folds:
        fold_slides(deck, folds)
    plan = DeckPlan({**deck, "slides": [{**s, "elements": merge_blocks(s["elements"])} for s in deck["slides"]]},
                    page_width)
    return {"deck": plan.deck, "out": out, "pdf": pdf, "plan": plan}, ""


def build_base(conv_deck: dict, conv_out: Path, target: dict, pres: dict, pdf: Path,
               overlays: str = "last", folds: dict[str, list[dict]] | None = None) -> dict:
    """A base of `snapshot.build_base`'s shape for a deck this converter never wrote.

    The IR side is the conversion of the source adopt left on disk; the deck side is the live
    objects that conversion was drawn from, each element's `objects`/`main` being the person's own
    `objectId` and its `readback` the same normalised read `sync` compares against. No alt text is
    written anywhere: tagging the objects would be a write into someone else's deck, and the base
    naming the ids does the same job."""
    read = snapshot.read_presentation(pres)
    by_id = {s["objectId"]: s for s in read["slides"]}
    state_slides, whys, layouts, celled, mates, leftovers = [], [], [], [], [], []
    for conv_slide, tgt in zip(conv_deck["slides"], target["slides"]):
        sid = tgt.get("objectId")
        live = by_id.get(sid)
        on_slide = [e for e in deck_objects(tgt) if live and e["object"] in live["objects"]]
        pairs, why = pair_elements(conv_slide["elements"], on_slide)
        layouts.append(explained_by_layout(conv_slide["elements"], why, tgt))
        celled.append(inside_tables(conv_slide["elements"], why, on_slide) - layouts[-1])
        mates.append({i: j for i, j in
                      drawn_from(conv_slide["elements"], why, pairs, on_slide).items()
                      if i not in layouts[-1] and i not in celled[-1]})
        state_slides.append({"objectId": sid, "elements": [e["id"] for e in conv_slide["elements"]],
                             "objects": [[on_slide[pairs[i]]["object"]] if i in pairs else []
                                         for i in range(len(conv_slide["elements"]))],
                             "groups": []})
        whys.append(why)
        taken = set(pairs.values())
        leftovers.append([e["object"] for k, e in enumerate(on_slide) if k not in taken])
    page_w = conv_deck["slides"][0]["size"][0] if conv_deck["slides"] else SLIDE_W
    state = {"presentationId": read["presentationId"],
             "scale": (read["page_size"][0] / page_w) if page_w else None, "slides": state_slides}
    base = snapshot.build_base(conv_deck, conv_out, pres, state, Path(pdf), generation=0, overlays=overlays)
    # The master's background belongs to the person's deck, not to this conversion: naming one
    # would let a source background change copy the *deck's* master fill onto a slide
    # (`sync.background_requests`). Every background this base writes is written explicitly.
    base["master_background"] = None
    base["origin"] = ORIGIN
    unpaired, from_layout, from_box = [], [], []
    for entry, oids in zip(base["slides"], leftovers):
        # The person's own objects this conversion draws nothing for. They are on the slide and
        # they are not an edit: `merge.slide_touched` would otherwise read a deck a person built
        # as one they had just added objects to, and say so about every slide of it. Recorded per
        # slide and not only in the summary below, because that is where the merge reads, and
        # carried by every later base (`sync.new_base`): they are never adopted, so a sync that
        # forgot them would start calling them added at generation 2.
        if oids:
            entry["left_alone"] = oids
    for entry, why, lay, cells, mate in zip(base["slides"], whys, layouts, celled, mates):
        for i, el in enumerate(entry["elements"]):
            if i not in why:
                continue
            if i in lay:
                # The merge has to know one from the other, and it reads the base's elements and
                # not this summary (`merge.plan_unit`'s `blind`).
                el["from_layout"] = True
            elif i in cells:
                el["in_table"] = True
            elif i in mate:
                el["drawn_from"] = entry["elements"][mate[i]]["key"]
            item = {"slide": entry["key"], "element": el["key"], "kind": el["kind"],
                    "why": FROM_LAYOUT if i in lay else IN_A_TABLE if i in cells else
                    DRAWN_FROM if i in mate else why[i]}
            (from_layout if i in lay else from_box if i in mate else unpaired).append(item)
    paired = sum(1 for e in base["slides"] for el in e["elements"] if el.get("main"))
    base["adopt"] = {"presentationId": read["presentationId"], "deck_page_size": read["page_size"],
                     "frame_width": read["page_size"][0], "slides": len(base["slides"]), "paired": paired,
                     "unpaired": unpaired, "from_layout": from_layout, "drawn_from": from_box,
                     "left_alone": [{"slide": e["key"], "objects": oids}
                                    for e, oids in zip(base["slides"], leftovers) if oids],
                     # The boxes this conversion was folded against, so that every later sync folds
                     # its own conversion the same way (`sync.build_ours`). They are the deck's
                     # geometry and not a decision, which is why they are recorded rather than the
                     # folds themselves: the source changes, the boxes do not.
                     "boxes": folds or {}}
    return base


def labels_match(conv_deck: dict, target: dict) -> str | None:
    """Why the conversion's slides are not the deck's slides, one for one (None: they are).

    `adopt.frame_labels` writes a label per deck slide, slugged from its `objectId`; the compiled
    PDF carries them back as named destinations and `classify` puts them on the slides. If that
    chain does not come out exactly as it went in, nothing else in this file may be believed - the
    base would tie one slide's source to another slide's objects."""
    from .adopt import frame_labels
    want = frame_labels(target)
    got = [s.get("label") for s in conv_deck["slides"]]
    if len(got) != len(want):
        return (f"the source compiles to {len(got)} slide(s) and the deck has {len(want)}: "
                f"a frame per slide is what ties the two together")
    wrong = [(w, g) for w, g in zip(want, got) if w != g]
    if wrong:
        return (f"{len(wrong)} frame(s) do not carry the label adopt wrote (first: `{wrong[0][0]}` came back as "
                f"`{wrong[0][1] or 'no label'}`): the slides cannot be paired with the deck's")
    return None


def record(tex: Path, work: Path, target: dict, pres: dict, engine: str | None = None,
           overlays: str = "last", log=print) -> tuple[dict | None, str | None]:
    """The whole of it: compile the source adopt wrote, pair its conversion with the deck it was
    written from, and return (base, None) or (None, why there is none). Never writes to Google."""
    if not target.get("slides"):
        return None, "the deck has no slides"
    if not pres.get("slides"):
        return None, "the deck was read without its presentation (no read-back to record)"
    log("recording a sync base for the adopted deck...")
    folds = deck_folds(target)
    conv, err = convert_source(Path(tex), Path(work), engine, float(snapshot.page_size(pres)[0]), folds)
    if conv is None:
        return None, f"the source does not compile:\n{err}"
    problem = labels_match(conv["deck"], target)
    if problem:
        return None, problem
    base = build_base(conv["deck"], conv["out"], target, pres, conv["pdf"], overlays, folds)
    return base, None


def store(base: dict, out: Path, drive=None) -> Path:
    """Write the base where `sync --deck <folder>` looks for it. Drive only when asked for."""
    path = snapshot.save_local(base, Path(out))
    if drive is not None:
        snapshot.save_drive(drive, base)
    return path


def next_command(pdf: Path | str, out: Path) -> str:
    return f"python -m beamer2slides sync {pdf} --deck {out}"


# ---------------------------------------------------------------- the first sync

def deck_page(base: dict) -> list[float]:
    return base.get("deck_page_size") or (base.get("adopt") or {}).get("deck_page_size") or [SLIDE_W]


def deck_width(base: dict) -> float:
    """How wide the deck is, in slide pt - which is what `sync` plans its boxes in. `convert` makes
    a deck SLIDE_W wide and nothing else; a deck `adopt` took over is whatever the person made."""
    return float(deck_page(base)[0])


def aspect_mismatch(base: dict) -> tuple[float, float] | None:
    """(the deck's aspect, the compiled page's) when one scale cannot carry the plan onto the deck.

    `emit.DeckPlan` turns the PDF's points into the deck's with a single number, so a page the
    source compiles to that is not the deck's shape puts everything right in x and wrong in y (or
    the other way about) - silently, since the boxes are valid. `adopt` writes the page from the
    deck (`deck_ir.page_size_for`, `adopt.page_setup`), so this is a source somebody changed the
    paper of, not the ordinary case."""
    deck, page = deck_page(base), base.get("page_size")
    if not page or len(deck) < 2 or not page[1] or not deck[1]:
        return None
    a, b = deck[0] / deck[1], page[0] / page[1]
    return None if abs(a - b) <= ASPECT_TOLERANCE * b else (a, b)


def _creations(mplan: dict) -> list[dict]:
    """Slides and element units this plan would write objects for."""
    out = [{"slide": p["key"], "element": None} for p in mplan["slides"] if p["action"] == "create"]
    for p in mplan["slides"]:
        if p["action"] != "update":
            continue
        out += [{"slide": p["key"], "element": u["key"]} for u in p["units"]
                if u["action"] in ("create", "recreate")]
    return out


def touches_deck(mplan: dict, theirs: dict) -> bool:
    """Whether this plan would put anything into the deck. `merge.has_writes` asks whether objects
    are made, removed, moved or reordered; a unit the source merely reworded is written too, and a
    sync that only does that is still the first one to touch a person's deck."""
    if merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]]):
        return True
    return any(u.get("source") for p in mplan["slides"] if p["action"] == "update" for u in p["units"])


def problems(base: dict, mplan: dict, theirs: dict, way_back: dict | None = None,
             backup_mode: str = "auto") -> list[dict]:
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
    if not touches_deck(mplan, theirs):
        return []
    out: list[dict] = []
    makes = _creations(mplan)
    shape = aspect_mismatch(base)
    if makes and shape is not None:
        out.append({"reason": "page-shape", "deck": shape[0], "page": shape[1],
                    "creations": makes[:3], "count": len(makes)})
    blind = []
    for p in mplan["slides"]:
        if p["action"] != "update" or p.get("base") is None:
            continue
        b = base["slides"][p["base"]]
        # The unit's members as the merge itself read them (`merge.units`), not as a map of their
        # keys: a base slide can answer to one key twice - a unit kept though the source dropped it
        # keeps the key the next conversion has since given to something else - and looking one up
        # read the person's own unpaired icon as a member of the source's new unit, refusing this
        # whole sync over a unit that was never blind (adopt-shaped seed 86066 at chain 8). That is
        # fixed where it is made (`merge.keys_the_source_took`); this is the gate not asking the
        # question in a way the answer can depend on.
        bunits = merge.units(b["elements"])
        for u in p["units"]:
            if u["action"] not in ("recreate", "move"):
                continue
            blind += [{"slide": b["key"], "element": mk}
                      for mk in merge.blind_members(bunits.get(u["key"]) or [])]
    if blind:
        out.append({"reason": "unpaired", "elements": blind})
    if base.get("generation", 0) != 0:
        return out
    if backup_mode not in ("none", None) and not guard.way_back_kept(way_back or {}):
        out.append({"reason": "no-way-back", "warnings": (way_back or {}).get("warnings", [])})
    deleted = [p["key"] for p in mplan["slides"] if p["action"] == "delete"]
    if deleted:
        out.append({"reason": "slides-deleted", "slides": deleted})
    return out


HEAD = "refusing to sync into this adopted deck"
WHY = ("  This deck was made by a person, not by this converter: none of its objects carries a tag "
       "saying which\n  part of the source it came from. `adopt` tied them to the source it wrote by "
       "where they stand and\n  what they say, and this sync would go past what that pairing can carry.")


def _some(names: list[str], most: int = 3) -> str:
    """The first few of them, ending the sentence."""
    return ", ".join(names[:most]) + (", ..." if len(names) > most else ".")


def _lines(p: dict) -> list[str]:
    if p["reason"] == "no-way-back":
        out = ["  - no way back: no backup of the deck was kept, and Drive's version history cannot be read "
               "back (docs/sync.md)."]
        out += [f"      {w}" for w in p.get("warnings", [])]
        return out
    if p["reason"] == "slides-deleted":
        return [f"  - {len(p['slides'])} slide(s) of the deck would be deleted, because no frame of the source "
                f"accounts for them any more: {_some(p['slides'])}",
                "      On the first sync that is usually a label that moved, not a slide the author meant to drop."]
    if p["reason"] == "unpaired":
        named = [e["slide"] + "/" + e["element"] for e in p["elements"]]
        return [f"  - {len(named)} element(s) the source changed could not be tied to any object of the "
                f"deck: {_some(named)}",
                "      Writing them would put a second object beside the person's, not over it."]
    if p["reason"] == "page-shape":
        return [f"  - the deck's slides are {p['deck']:.3f} wide for every 1 high and the page the source "
                f"compiles to is {p['page']:.3f},",
                f"      so the {p['count']} object(s) this sync would create land at the right place across "
                f"and the wrong one down."]
    return [f"  - {p['reason']}"]


def refusal_message(pid: str, out: Path, pdf: Path | str, problems_found: list[dict]) -> str:
    """What a person sees instead of a sync that could not be trusted. Nothing was written."""
    cmd = next_command(pdf, out)
    reasons = {p["reason"] for p in problems_found}
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


def report_lines(base: dict) -> list[str]:
    """What `adopt` prints about the base it just recorded, and `sync` about the one it read."""
    info = base.get("adopt") or {}
    drawn = info.get("from_layout") or []
    out_of = info.get("drawn_from") or []
    total = info.get("paired", 0) + len(info.get("unpaired") or []) + len(drawn) + len(out_of)
    lines = [f"sync base: {info.get('slides', 0)} slides, {info.get('paired', 0)} of {total} elements tied to an "
             f"object of the deck"]
    if drawn:
        lines.append(f"  {len(drawn)} of them are drawn by the deck's own layouts and master, which this "
                     f"converter never writes to: change those on the layout, in Slides")
    if out_of:
        lines.append(f"  {len(out_of)} of them this converter drew out of a box beside them (an icon in a "
                     f"line, a formula in prose): those go in with that box")
    if info.get("unpaired"):
        lines.append(f"  {len(info['unpaired'])} element(s) could not be tied to one; a sync that changes one "
                     f"keeps the deck's version of it and says so in the report")
    left = sum(len(x["objects"]) for x in info.get("left_alone") or [])
    if left:
        lines.append(f"  {left} object(s) of the deck the source does not draw: sync never touches them")
    return lines


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
