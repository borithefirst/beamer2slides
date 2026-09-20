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


# ---------------------------------------------------------------- the base

def convert_source(tex: Path, work: Path, engine: str | None = None,
                   page_width: float = SLIDE_W) -> tuple[dict | None, str]:
    """Compile the source tree at `tex` and convert it exactly as a later `sync` will
    (`sync.build_ours`'s first half): the base's IR side has to be what the *converter* makes of
    that source, not what adopt read from the deck, or every element would read as changed on the
    first sync. `page_width` is the deck's own, for the same reason - the plan's scale, and with it
    every hole width it fits, is PDF pt to *that* deck's points.
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
    plan = DeckPlan({**deck, "slides": [{**s, "elements": merge_blocks(s["elements"])} for s in deck["slides"]]},
                    page_width)
    return {"deck": plan.deck, "out": out, "pdf": pdf, "plan": plan}, ""


def build_base(conv_deck: dict, conv_out: Path, target: dict, pres: dict, pdf: Path,
               overlays: str = "last") -> dict:
    """A base of `snapshot.build_base`'s shape for a deck this converter never wrote.

    The IR side is the conversion of the source adopt left on disk; the deck side is the live
    objects that conversion was drawn from, each element's `objects`/`main` being the person's own
    `objectId` and its `readback` the same normalised read `sync` compares against. No alt text is
    written anywhere: tagging the objects would be a write into someone else's deck, and the base
    naming the ids does the same job."""
    read = snapshot.read_presentation(pres)
    by_id = {s["objectId"]: s for s in read["slides"]}
    state_slides, whys, leftovers = [], [], []
    for conv_slide, tgt in zip(conv_deck["slides"], target["slides"]):
        sid = tgt.get("objectId")
        live = by_id.get(sid)
        on_slide = [e for e in deck_objects(tgt) if live and e["object"] in live["objects"]]
        pairs, why = pair_elements(conv_slide["elements"], on_slide)
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
    unpaired = []
    for entry, why in zip(base["slides"], whys):
        for i, el in enumerate(entry["elements"]):
            if i in why:
                unpaired.append({"slide": entry["key"], "element": el["key"], "kind": el["kind"], "why": why[i]})
    paired = sum(1 for e in base["slides"] for el in e["elements"] if el.get("main"))
    base["adopt"] = {"presentationId": read["presentationId"], "deck_page_size": read["page_size"],
                     "frame_width": read["page_size"][0], "slides": len(base["slides"]), "paired": paired,
                     "unpaired": unpaired,
                     "left_alone": [{"slide": e["key"], "objects": oids}
                                    for e, oids in zip(base["slides"], leftovers) if oids]}
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
    conv, err = convert_source(Path(tex), Path(work), engine, float(snapshot.page_size(pres)[0]))
    if conv is None:
        return None, f"the source does not compile:\n{err}"
    problem = labels_match(conv["deck"], target)
    if problem:
        return None, problem
    base = build_base(conv["deck"], conv["out"], target, pres, conv["pdf"], overlays)
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

    `unpaired` is the gate behind a decision the merge now makes itself: `merge.plan_unit` keeps
    such a unit as the deck has it and reports a conflict, so an ordinary sync never brings one
    here and the rest of the deck syncs (it used to refuse whole syncs by the hundred - see the
    comment there). What is left is the last thing between a plan and a write into somebody's
    deck, for a plan that says otherwise however it came to say it."""
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
        els = {e["key"]: e for e in b["elements"]}
        for u in p["units"]:
            if u["action"] not in ("recreate", "move"):
                continue
            for mk in u.get("base_members", []):
                el = els.get(mk)
                if el is not None and not el.get("objects"):
                    blind.append({"slide": b["key"], "element": mk})
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
    total = info.get("paired", 0) + len(info.get("unpaired") or [])
    lines = [f"sync base: {info.get('slides', 0)} slides, {info.get('paired', 0)} of {total} elements tied to an "
             f"object of the deck"]
    if info.get("unpaired"):
        lines.append(f"  {len(info['unpaired'])} element(s) could not be tied to one; a sync that changes them "
                     f"refuses rather than write beside the person's object")
    left = sum(len(x["objects"]) for x in info.get("left_alone") or [])
    if left:
        lines.append(f"  {left} object(s) of the deck the source does not draw: sync never touches them")
    return lines


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
