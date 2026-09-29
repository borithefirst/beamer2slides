"""Fuzzing sync against the loss oracle: does a sync ever lose a person's work?

The default run is offline and takes a few seconds: `tools/fuzz_sync.py` builds a synthetic deck,
edits it the way a person would, changes the source the way an author would, plans the merge with
`merge.plan_merge`, applies the plan the way docs/sync.md says sync does (`tools/fuzz_world.py`)
and hands the two read-backs plus the report to `tools/loss_oracle.py`. A round is clean when the
oracle finds nothing.

Each round also checks that the sync settles: with the base the sync recorded, syncing the same
source again must write nothing, or the next sync would rewrite units - which is where a person's
work gets lost.

The second half proves the oracle is not vacuous: a clean round is taken apart again with a loss
injected on purpose (a user object dropped, a person's word swallowed, a slide silently deleted, a
report claiming work it didn't do) and the oracle has to catch every one of them.

The live campaign (real decks, marker `sync`) is opt-in:
    python -m pytest -m sync tests/test_sync_fuzz.py
    B2S_FUZZ_LIVE_ROUNDS=20 python -m pytest -m sync tests/test_sync_fuzz.py
"""

import copy
import os
import random
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from beamer2slides import merge
from beamer2slides.devtools import fuzz_sync
from beamer2slides.devtools import fuzz_world as W
from beamer2slides.devtools import loss_oracle
from beamer2slides import snapshot
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.sync_model import Base, ElementKey, ObjectId, ReadBack, RunSpan

from .json_reads import jarr, jat, jnums, jobj, jobjs, jstr, jstrs

JsonMap = Mapping[str, Json]

# Rounds of the default run; every seed is a different deck, edit set and source change.
ROUNDS = int(os.environ.get("B2S_FUZZ_ROUNDS", "40"))
# Seeds that once failed, kept as regressions: 252/430 merge's move shortcut moved nothing,
# 0/20 the harness and the report's per-unit keys, 3312/3799 two ways the oracle accused a
# blameless sync (a word the source put in another element, an ambiguous reported swap),
# 45/60/108/177 the base kept the slides the deck deleted, so a second sync brought them back.
REGRESSIONS = (0, 20, 45, 60, 108, 177, 252, 430, 3312, 3799)
# The same against adopt-shaped decks (`fuzz_world.make_adopt_doc`): what `adopt` writes rather than
# what `convert` does - many small boxes, most slides with no title, near-identical twins, grouped
# clusters, tables, a shared logo and a footer on every slide. Seeds that once failed: 82/328/441/
# 568/629, a panel the source added stacked over text the source draws above it (`Sync.restack`).
ADOPT_ROUNDS = int(os.environ.get("B2S_FUZZ_ADOPT_ROUNDS", "25"))
ADOPT_REGRESSIONS = (82, 328, 441, 568, 629)


# ---------------------------------------------------------------- the fuzz itself

def offline_round(seed: int, source_ops: Sequence[str] | None, deck_ops: Sequence[str] | None, work: Path | None,
                  shape: W.Shape) -> fuzz_sync.Step:
    """`fuzz_sync.offline_round` of a converted deck's sync (not an adopted deck's first). The
    ops: None draws them from the seed."""
    return fuzz_sync.offline_round(seed, source_ops, deck_ops, work, shape, False)


def a_round(seed: int) -> fuzz_sync.Step:
    """The round the seed draws, on a converted talk."""
    return offline_round(seed, None, None, None, "converted")


def offline_chain(seed: int, chain: int, ops: Sequence[fuzz_sync.Ops] | None) -> fuzz_sync.Chain:
    return fuzz_sync.offline_chain(seed, chain, ops, None, "converted", False)


@pytest.mark.parametrize("seed", [*range(ROUNDS), *REGRESSIONS])
def test_offline_round_loses_nothing(seed: int) -> None:
    result = a_round(seed)
    if result.failures:
        small = fuzz_sync.shrink_offline(result, 200, "converted", False)
        pytest.fail(f"seed {seed} lost something\n{loss_oracle.described(small.failures)}\n"
                    f"  deck:   {'; '.join(small.deck)}\n  source: {'; '.join(small.source)}")


@pytest.mark.parametrize("seed", [*range(ADOPT_ROUNDS), *ADOPT_REGRESSIONS])
def test_offline_round_on_an_adopt_shaped_deck_loses_nothing(seed: int) -> None:
    result = offline_round(seed, None, None, None, "adopt")
    if result.failures:
        small = fuzz_sync.shrink_offline(result, 200, "adopt", False)
        pytest.fail(f"adopt seed {seed} lost something\n{loss_oracle.described(small.failures)}\n"
                    f"  deck:   {'; '.join(small.deck)}\n  source: {'; '.join(small.source)}")


def test_the_first_sync_world_leaves_the_persons_unpaired_boxes_on_their_slide(tmp_path: Path) -> None:
    """What a deck a person built is made of, and what the campaign was blind to until it had them.

    `adopt` ties an element of the conversion to an object of the deck or to nothing, and a real
    deck answers "nothing" for 10-80% of them - but the *box* is still there, on the slide, saying
    what the person wrote (`adopt.left_alone`). This world used to delete it along with the
    pairing, so everything that reads the live deck saw an adopted deck as an empty one:
    `merge.user_objects` found none of the person's work, the loss oracle guarded none of it, and
    `sync.would_hide` - the rule that nothing this converter writes ends up over words only the
    deck has - had nothing on any slide to be measured against.

    The one element that really leaves nothing behind is the picture read out of somebody's text
    box: it was never an object of theirs (`adopt_sync.drawn_from`, `merge.covered`)."""
    rng = random.Random(11)
    doc = W.make("adopt", rng, tmp_path)
    base = W.build_adopt_base(doc, tmp_path, rng)
    live = {jstr(s, "objectId"): s for s in jobjs(W.live_json(W.live_of(base)), "slides")}
    slides = jobjs(base, "slides")
    left = [el for s in slides for el in jobjs(s, "elements") if el.get("left_object")]
    drawn = [el for s in slides for el in jobjs(s, "elements") if el.get("drawn_from")]
    assert left and drawn, "the world draws both kinds of element tied to no object"
    assert not any(el.get("objects") for el in left + drawn)
    for s in slides:
        read = live[jstr(s, "objectId")]
        objects, order = jobj(read, "objects"), jarr(read, "order")
        theirs = {o["objectId"] for o in merge.user_objects(s, read)}
        assert theirs == (set(jstrs(s, "left_alone")) if s.get("left_alone") else set()), \
            "the person's own boxes, and only those"
        assert all(jstr(el, "left_object") in objects and el["left_object"] in order
                   for el in jobjs(s, "elements") if el.get("left_object"))
        tied = {jstr(el, "main") for el in jobjs(s, "elements") if el.get("main")}
        assert set(objects) == tied | theirs, \
            "and nothing standing for a picture the converter read out of one of them"


def test_a_broken_label_invariant_sends_far_fewer_slides_to_the_wrong_frame(tmp_path: Path) -> None:
    """`tools/fuzz_labels.py` on fixed seeds: what the moved-label check is worth, measured against
    a truth the synthetic source knows (every frame is tagged, so a pairing is right or wrong).

    The campaign at 3000 rounds, which is where the thresholds were set: with the labels sound the
    check never once spoke and not one frame of 1524 rounds lost its slide, and with one broken it
    took misidentified frames from 14.68% to 1.04% - and of the rounds still wrong, 35 of 37 were
    reported as a conflict. The 100 seeds here are enough to fail if any of that stops being true;
    on them the check leaves exactly one frame wrong, a label pasted onto a frame added in the same
    version while its own frame was deleted, which nothing in either PDF can tell from a frame
    written from scratch."""
    from beamer2slides.devtools import fuzz_labels

    rounds = [r for seed in range(100) for r in fuzz_labels.round_once(seed, 0.5, tmp_path, 1, "converted")]
    sound = [r for r in rounds if not r.broke]
    broken = [r for r in rounds if r.broke and not r.reordered]
    assert len(sound) > 30 and len(broken) > 30  # the harness really does break labels, and not always
    assert [r.said for r in sound] == ["quiet"] * len(sound)  # never a word about a sound source
    assert sum(r.wrong["now"] for r in rounds if not r.broke) == 0, \
        "a source that kept its labels lost a frame's slide anyway"
    was, now = (sum(r.wrong[k] for r in broken) for k in ("before", "now"))
    assert now * 5 < was, f"the check is not earning its place: {was} -> {now}"
    wrong = [r for r in broken if r.wrong["now"]]
    silent = [r for r in wrong if r.said == "quiet"]
    assert len(silent) <= 1 and len(wrong) <= 2, \
        f"too much goes wrong unsaid: {[(r.seed, r.ops) for r in wrong]}"
    assert all(r.wrong["now"] <= r.wrong["before"] for r in rounds)  # never worse than before


def test_labels_are_the_only_thing_holding_an_adopt_shaped_deck_together(tmp_path: Path) -> None:
    """The same campaign on a deck `adopt` wrote (`--shape adopt`): about half the slides have no
    title, the phrases repeat from slide to slide, and every third slide is a near-twin of the one
    before it. That is where the fallbacks have nothing to work with, and the measurement says so.

    Over 1500 rounds: with the labels kept, not one frame of 4068 went to the wrong slide and the
    check never spoke. With one broken, 12.34% of frames did, and `identity.cross_pairs` /
    `gap_pairs` - which take 6.93% to 0.00% on a converted talk - recovered **nothing at all**
    (`before` equals `order`, frame for frame): one leftover never explains one slide unmistakably
    when six slides say "Next steps", and no gap holds one slide and one frame that share words
    nobody else shares. `identity.label_moves` still earns its place (12.34% -> 2.94%), but 39 of
    the 60 rounds left wrong passed in silence, against 1 of 22 on a converted talk.

    Which is the whole argument for `adopt.frame_labels`: on this shape of deck the label is not the
    best identity, it is the only one."""
    from beamer2slides.devtools import fuzz_labels

    rounds = [r for seed in range(150) for r in fuzz_labels.round_once(seed, 0.5, tmp_path, 1, "adopt")]
    sound = [r for r in rounds if not r.broke]
    broken = [r for r in rounds if r.broke]
    assert len(sound) > 30 and len(broken) > 30
    assert sum(r.wrong["now"] for r in sound) == 0, "a source that kept its labels lost a frame's slide"
    assert [r.said for r in sound] == ["quiet"] * len(sound)   # and never a word about a sound one
    order, before, now = (sum(r.wrong[k] for r in broken) for k in ("order", "before", "now"))
    assert order > 10, "the harness stopped breaking labels, so this proves nothing"
    assert before == order, f"the leftover passes now recover something here ({order} -> {before}): say so"
    assert now * 2 < order, f"the moved-label check is not earning its place: {order} -> {now}"


def test_a_frame_the_source_moved_across_another_keeps_its_slide(tmp_path: Path) -> None:
    """The other half of identity, on the same tagged truth: a frame that crossed another one.

    The alignment keeps the order, so of two frames that swapped only one stays in the chain; the
    other falls out and comes back as a new frame, and the slide it was made from is read as
    dropped - nobody's words are deleted, but the source writes them onto the slide next door.
    `identity.cross_pairs` pairs those leftovers when the content is unmistakable. Measured over
    3000 rounds: 5.53% -> 0.00% of the frames in sound rounds that moved one, and 19.15% -> 1.18%
    when a label was broken in the same round. The 300 seeds here hold that where it matters."""
    from beamer2slides.devtools import fuzz_labels

    rounds = [r for seed in range(300) for r in fuzz_labels.round_once(seed, 0.5, tmp_path, 1, "converted")]
    moved = [r for r in rounds if r.reordered]
    assert len(moved) >= 15, "the harness stopped moving frames, so this proves nothing"
    assert sum(r.wrong["order"] for r in moved) > 0, \
        "the order-keeping pass alone gets these right: the leftovers pass is not earning its place"
    assert sum(r.wrong["now"] for r in moved) == 0, \
        f"frames lost their slide: {[(r.seed, r.wrong, r.ops) for r in moved if r.wrong['now']]}"
    assert all(r.wrong["before"] <= r.wrong["order"] for r in rounds)  # never worse than the order alone


def test_the_applier_keeps_styling_the_real_sync_would_put_back() -> None:
    """The reference applier decides for itself whether the person's run styling has anywhere to go
    when an element is recreated, and it has to decide it the way `sync.style_range_requests` really
    does: by mapping each styled span through the matching blocks of the old text and the new one.

    Reading it word by word instead threw styling away that sync re-applies, and the campaign then
    accused the merge of losing it (offline seed 23599 --shape adopt, chained: the person bolded a
    word, reworded that very sentence themselves two syncs later - which clips the bold to two
    letters inside another word, as Slides does - and the source then reworded it too)."""
    from beamer2slides.devtools import fuzz_world

    def rb(text: str, styles: list[JsonObject], spans: list[RunSpan]) -> ReadBack:
        made = fuzz_world.readback("shape", [0, 0, 100, 40], text=text, image=None, parent=None, title=None, z=0,
                                   table=None, fill=None)
        return replace(made, text_styles=tuple(styles), run_spans=tuple(spans))

    plain: JsonObject = {"fontFamily": "Lato"}
    bold: JsonObject = {"fontFamily": "Lato", "bold": True}
    base_rb = rb("", [plain], [])
    live = rb("policy-theirs colleague source\n", [], [(11, 13, bold), (13, 30, plain)])
    assert live.text is not None
    assert not fuzz_world._styling_ends(base_rb, live, "policy-theirs colleague-ours source\n")
    # The other way: the source replaced the word those letters were in, so the styling really ends.
    assert fuzz_world._styling_ends(base_rb, live, "wording colleague source\n")
    # The converter's own styling is not the person's, and never counts as lost.
    only_ours = rb(live.text, [], [(0, 30, plain)])
    assert not fuzz_world._styling_ends(base_rb, only_ours, "wording colleague source\n")


def test_the_round_notices_a_base_that_would_not_settle(tmp_path: Path) -> None:
    """The settle check with a base broken on purpose: one that has forgotten a slide makes the next
    sync create it again, and the round has to say so."""
    result = offline_round(3, ["reword"], ["add_text_box", "reword"], tmp_path, "converted")
    st = result.state
    assert fuzz_sync._settled(st.doc, st.next_base, st.after, tmp_path, False) == []
    assert st.next_base is not None
    forgetful = copy.deepcopy(st.next_base)
    forgetful["slides"] = jarr(forgetful, "slides")[1:]
    found = fuzz_sync._settled(st.doc, forgetful, st.after, tmp_path, False)
    assert [f.kind for f in found] == ["second_sync_writes"] and found[0].severity == "report"


def test_the_fuzzer_only_makes_decks_slides_could_hand_back() -> None:
    """Another guard on the harness. Slides never gives two things one objectId, and a deck that
    does is not a deck a sync could ever meet: the two slides read back as one, so the oracle saw a
    picture the person had added disappear and the report list one created slide where two appeared.
    That was converted seed 9200614 at chain 6 - three findings, every one of them the fuzzer's own
    hand - and six digits drawn afresh per slide collide long before a campaign is over.
    `fuzz_sync._fresh` gives a drawn id a tail rather than drawing again, so an op still takes
    exactly one number from its rng and a campaign's every other round is unchanged."""
    from beamer2slides import sync_model
    from beamer2slides.devtools import fuzz_world
    from beamer2slides.typing_compat import override

    def box(b: list[float], text: str) -> ReadBack:
        return fuzz_world.readback("shape", b, text=text, image=None, parent=None, title=None, z=0, table=None,
                                   fill=None)

    def slide(sid: str, text: str) -> fuzz_world.LiveSlide:
        return fuzz_world.LiveSlide(object_id=ObjectId(sid), layout_object_id="L", background=None, notes="",
                                    notes_id=f"{sid}_n", order=[ObjectId(f"{sid}_t")],
                                    objects={ObjectId(f"{sid}_t"): box([0, 0, 100, 40], text)})

    def deck(*slides: fuzz_world.LiveSlide) -> fuzz_world.LiveDeck:
        return fuzz_world.LiveDeck(presentation_id="P", revision_id="r0", page_size=None, layouts=None,
                                   master_background=None, slides=list(slides))

    class Stuck(random.Random):
        """An rng whose every draw is the collision."""
        @override
        def randrange(self, start: int, stop: int | None = None, step: int = 1) -> int:  # (random's own defaults)
            return 790791 % start if stop is None else start

    nobody = sync_model.base({"slides": []})
    live = deck(slide("b2s_s000", "the converter's own\n"))
    for _ in range(3):
        fuzz_sync.deck_duplicate_slide(Stuck(), nobody, live)
        fuzz_sync.deck_add_slide(Stuck(), nobody, live)
    ids = [x for s in live.slides for x in [s.object_id, s.notes_id, *s.objects]]
    assert len(set(ids)) == len(ids), sorted(str(x) for x in ids)
    # ... nor makes a group a child of itself, which is what `_take_place` did while the group's
    # `children` was the very list the op still held: it put the new id wherever the old one stood,
    # its own children among them, and the op said so out loud (`group [a, g] as g`).
    one = deck(slide("b2s_s000", "a\n"))
    one.slides[0].objects[ObjectId("b2s_s000_u")] = box([0, 50, 100, 90], "b\n")
    one.slides[0].order.append(ObjectId("b2s_s000_u"))
    fuzz_sync.deck_group(Stuck(), nobody, one)
    gid, rb = next((o, r) for o, r in one.slides[0].objects.items() if r.kind == "elementGroup")
    children = rb.children
    assert children is not None
    assert sorted(children) == ["b2s_s000_t", "b2s_s000_u"] and gid not in children
    # and the campaign says so out loud rather than blaming the merge for what it did itself
    live.slides.append(fuzz_world.copy_slide(live.slides[0]))
    with pytest.raises(AssertionError, match="two things at once"):
        fuzz_sync._sync_step(3, 0, {}, {"slides": []}, live, Path("."), [], [], False)


def test_the_rounds_really_exercise_sync() -> None:
    """A guard on the harness: a round that edited nothing would pass the oracle for free."""
    result = offline_round(3, ["reword", "move_element"], ["add_text_box", "reword", "add_slide"], None, "converted")
    report = result.state.report
    assert report["applied"] and report["overrides"] and report["user_objects"]
    assert any("not applicable" not in d for d in result.deck)


# ---------------------------------------------------------------- the oracle, on a clean round

@dataclass(frozen=True, kw_only=True)
class Clean:
    """A clean round as the JSON the oracle's dict entries take: the base, the deck before and after
    the sync, its report and the new conversion."""
    base: JsonObject
    before: JsonObject
    after: JsonObject
    report: JsonObject
    ours: JsonObject


@pytest.fixture(scope="module")
def clean() -> Clean:
    """A round with a user object, a user slide, a reworded converter text and a moved element, as
    the JSON the oracle's dict entries take."""
    result = offline_round(3, ["reword", "move_element"], ["add_text_box", "reword", "add_slide"], None, "converted")
    assert not result.failures, loss_oracle.described(result.failures)
    st = result.state
    return Clean(base=st.base, before=W.live_json(st.before), after=W.live_json(st.after), report=st.report,
                 ours=st.ours.json)


def checked(state: Clean) -> list[JsonObject]:
    """The oracle's findings over a round (a test changes one of its parts with `replace`)."""
    return O.check(state.base, state.before, state.after, state.report, state.ours)


def _read_back(rb: JsonMap) -> JsonObject:
    """A read-back as a test writes it, with what it leaves out filled in: no transform (nothing to
    compare), a box at the origin, no style hashes - what a dict reader read as absent."""
    return {"kind": "shape", "transform": [], "box": [0, 0, 0, 0], "parent_group": None, "text_style_hash": "",
            "shape_style_hash": "", **rb}


def _entry(el: JsonMap) -> JsonObject:
    """An element entry as a test writes it (a key, and what the check needs), filled in: its kind is
    its key's first part."""
    fields: JsonObject = {k: "" for k in ("text", "position", "size", "style", "image")}
    out: JsonObject = {"kind": jstr(el["key"]).split("/")[0], "ir_hash": "", "fields": fields, **el}
    written: JsonObject = jobj(el["fingerprint"]) if "fingerprint" in el else {}
    out["fingerprint"] = {"text": "", "bbox": [0, 0, 0, 0], **written}
    if "readback" in el:
        out["readback"] = {o: _read_back(jobj(rb)) for o, rb in jobj(el["readback"]).items()}
    return out


def full(snap: JsonMap) -> JsonObject:
    """A base, a read-back or a new conversion as a test writes it, filled in for the parsers the
    oracle's dict entries read it with (a report or anything else is handed on as it is)."""
    written = snap.get("slides")
    if not isinstance(written, list):
        return dict(snap)
    slides: list[Json] = []
    for each in written:
        s = dict(jobj(each))
        if "elements" in s:
            s["elements"] = [_entry(jobj(e)) for e in jarr(s, "elements")]
        objects = s.get("objects")
        if isinstance(objects, dict):
            s["objects"] = {o: _read_back(jobj(rb)) for o, rb in objects.items()}
        slides.append(s)
    return {"pairs": {}, **snap, "slides": slides}


def full_or_none(snap: JsonMap | None) -> JsonObject | None:
    return None if snap is None else full(snap)


class _Oracle:
    """`loss_oracle`'s dict entries over fixtures as the tests write them (`full`)."""

    @staticmethod
    def check(base: JsonMap, before: JsonMap, after: JsonMap, report: JsonMap | None,
              ours: JsonMap | None) -> list[JsonObject]:
        return loss_oracle.check(full(base), full(before), full(after), full_or_none(report), full_or_none(ours))

    @staticmethod
    def user_slide_findings(base: JsonMap, before: JsonMap, after: JsonMap) -> list[JsonObject]:
        return loss_oracle.user_slide_findings(full(base), full(before), full(after))

    @staticmethod
    def geometry_findings(base: JsonMap, before: JsonMap, after: JsonMap, rep: JsonMap,
                          ours: JsonMap | None) -> list[JsonObject]:
        return loss_oracle.geometry_findings(full(base), full(before), full(after), full(rep), full_or_none(ours))

    @staticmethod
    def report_findings(base: JsonMap, before: JsonMap, after: JsonMap, rep: JsonMap) -> list[JsonObject]:
        return loss_oracle.report_findings(full(base), full(before), full(after), full(rep))

    @staticmethod
    def picture_findings(base: JsonMap, before: JsonMap, after: JsonMap, rep: JsonMap) -> list[JsonObject]:
        return loss_oracle.picture_findings(full(base), full(before), full(after), full(rep))

    @staticmethod
    def style_findings(base: JsonMap, before: JsonMap, after: JsonMap, rep: JsonMap) -> list[JsonObject]:
        return loss_oracle.style_findings(full(base), full(before), full(after), full(rep))

    @staticmethod
    def order_findings(base: JsonMap, before: JsonMap, after: JsonMap, rep: JsonMap) -> list[JsonObject]:
        return loss_oracle.order_findings(full(base), full(before), full(after), full(rep))

    @staticmethod
    def occlusion_findings(base: JsonMap, before: JsonMap, after: JsonMap, ours: JsonMap | None) -> list[JsonObject]:
        return loss_oracle.occlusion_findings(full(base), full(before), full(after), full_or_none(ours))

    @staticmethod
    def deck_placement(base: JsonMap) -> loss_oracle.Placement | None:
        return loss_oracle.deck_placement(full(base))


O = _Oracle()


def kinds(findings: list[JsonObject]) -> set[str]:
    return {jstr(f, "kind") for f in findings}


def slide_of(read: JsonObject, oid: str) -> JsonObject:
    return next(s for s in jobjs(read, "slides") if s["objectId"] == oid)


def objects_of(read: JsonObject, oid: str) -> JsonObject:
    """The objects of the slide `oid` of a read-back."""
    return jobj(slide_of(read, oid), "objects")


def user_object(state: Clean) -> tuple[str, str]:
    """(slide id, object id) of an object the person added."""
    entry = jobj(state.report, "user_objects", 0)
    sid = next(jstr(s, "objectId") for s in jobjs(state.base, "slides") if s["key"] == entry["slide"])
    return sid, jstr(entry, "objectId")


def text_of(rb: JsonMap) -> str | None:
    text = rb.get("text")
    assert text is None or isinstance(text, str)
    return text


def typed_word(state: Clean) -> tuple[str, str, str]:
    """(slide id, object id, word) a person typed into a converter object."""
    for b in jobjs(state.base, "slides"):
        sid = jstr(b, "objectId")
        live = objects_of(state.before, sid)
        for el in jobjs(b, "elements"):
            main = el.get("main")
            if not isinstance(main, str) or not main:
                continue
            recorded = el.get("readback")
            was, now = jobj(recorded).get(main) if recorded else None, live.get(main)
            if was is None or now is None:
                continue
            old = loss_oracle.words(text_of(jobj(was)))
            for hunk in loss_oracle.hunks(text_of(jobj(was)), text_of(jobj(now))):
                for word in sorted(loss_oracle.words("".join(hunk[2])) - old):
                    return sid, main, word
    raise AssertionError("the round has no deck word edit")


def test_a_clean_round_is_clean(clean: Clean) -> None:
    assert loss_oracle.check(clean.base, clean.before, clean.after, clean.report, clean.ours) == []


def test_a_sync_that_wrote_nothing_is_clean(clean: Clean) -> None:
    """The control in the other direction: leaving the deck exactly as it was loses nothing."""
    assert loss_oracle.check(clean.base, clean.before, copy.deepcopy(clean.before), None, clean.ours) == []


# ---------------------------------------------------------------- losses injected on purpose

def test_catches_a_dropped_user_object(clean: Clean) -> None:
    sid, oid = user_object(clean)
    after = copy.deepcopy(clean.after)
    objects_of(after, sid).pop(oid)
    found = checked(replace(clean, after=after))
    assert "user_object_deleted" in kinds(found) and any(f["object"] == oid for f in found)


def test_catches_a_user_object_put_back_where_it_was(clean: Clean) -> None:
    sid, oid = user_object(clean)
    after = copy.deepcopy(clean.after)
    rb = jobj(objects_of(after, sid), oid)
    rb["box"] = [v + 30 for v in jnums(rb, "box")]
    transform = jnums(rb, "transform")
    rb["transform"] = [*transform[:4], transform[4] + 30, transform[5] + 30]
    assert "user_object_moved" in kinds(checked(replace(clean, after=after)))


def test_a_person_s_group_follows_its_children(clean: Clean) -> None:
    """A group the person made of converter objects moves when the source moves them (live fuzz
    r7403: a paragraph and its equation pushed down 31 pt). Its box is not the person's placement."""
    sid, oid = user_object(clean)
    group: JsonObject = {"kind": "elementGroup", "children": [oid, "converter_child"], "box": [0, 0, 100, 50],
                         "transform": [1, 0, 0, 1, 0, 0], "parent_group": None}
    before, after = copy.deepcopy(clean.before), copy.deepcopy(clean.after)
    objects_of(before, sid)["user_group"] = group
    objects_of(after, sid)["user_group"] = {**group, "box": [0, 31, 100, 81], "transform": [1, 0, 0, 1, 0, 31]}
    for read in (before, after):
        jobj(objects_of(read, sid), oid)["parent_group"] = "user_group"
    assert "user_object_moved" not in kinds(checked(replace(clean, before=before, after=after)))


def test_catches_a_user_object_taken_out_of_a_group_that_is_still_there(clean: Clean) -> None:
    """The person's object leaving a group is their work undone - unless the group itself is gone,
    which Slides does on its own once a group is down to one child."""
    sid, oid = user_object(clean)
    after = copy.deepcopy(clean.after)
    objects = objects_of(after, sid)
    objects["user_group"] = {"kind": "elementGroup", "children": [oid, "something_else"], "box": [0, 0, 10, 10],
                             "transform": [1, 0, 0, 1, 0, 0], "parent_group": None}
    jobj(objects, oid)["parent_group"] = "user_group"
    assert "user_object_regrouped" in kinds(checked(replace(clean, after=after)))
    before = copy.deepcopy(clean.before)
    jobj(objects_of(before, sid), oid)["parent_group"] = "gone_group"  # the group Slides dropped
    assert "user_object_regrouped" not in kinds(checked(replace(clean, before=before)))


def swallowed(after: JsonObject, sid: str, word: str) -> None:
    """`word` taken out of every text on the slide `sid` of `after`."""
    for rb in objects_of(after, sid).values():
        o = jobj(rb)
        text = o.get("text")
        if isinstance(text, str) and text:
            o["text"] = re.sub(word, "", text, flags=re.IGNORECASE)


def test_catches_a_swallowed_word(clean: Clean) -> None:
    """The person's word is nowhere on the slide afterwards and no conflict mentions it."""
    sid, oid, word = typed_word(clean)
    after = copy.deepcopy(clean.after)
    swallowed(after, sid, word)
    found = kinds(checked(replace(clean, after=after)))
    assert found & {"word_lost", "overwritten_unreported"}


def test_a_swallowed_word_reproduced_in_a_conflict_is_accounted_for(clean: Clean) -> None:
    """... unless the report shows the author what was replaced: then it is a reported conflict."""
    sid, oid, word = typed_word(clean)
    after = copy.deepcopy(clean.after)
    swallowed(after, sid, word)
    skey = next(s["key"] for s in jobjs(clean.base, "slides") if s["objectId"] == sid)
    report = copy.deepcopy(clean.report)
    jarr(report, "conflicts").append({"slide": skey, "element": None, "field": "text", "theirs": f"...{word}...",
                                      "resolution": "source kept"})
    assert not (kinds(checked(replace(clean, after=after, report=report))) & {"word_lost", "overwritten_unreported"})


def test_catches_a_silently_deleted_slide(clean: Clean) -> None:
    """A converter slide the report doesn't list, and a slide the person added (never allowed)."""
    after = copy.deepcopy(clean.after)
    after["slides"] = [s for s in jobjs(after, "slides") if s["objectId"] != jat(clean.base, "slides", 1, "objectId")]
    assert "slide_deleted_unreported" in kinds(checked(replace(clean, after=after)))
    user_slide = jstr(clean.report, "slides", "user_added", 0, "objectId")
    after = copy.deepcopy(clean.after)
    after["slides"] = [s for s in jobjs(after, "slides") if s["objectId"] != user_slide]
    report = copy.deepcopy(clean.report)
    jobj(report, "slides")["deleted"] = [user_slide]  # even claiming it doesn't help
    assert "user_slide_deleted" in kinds(checked(replace(clean, after=after, report=report)))


def test_catches_a_slide_quietly_put_back_in_another_place(clean: Clean) -> None:
    after = copy.deepcopy(clean.after)
    slides = jarr(after, "slides")
    after["slides"] = [slides[2], *slides[:2], *slides[3:]]
    assert "slide_moved_unreported" in kinds(checked(replace(clean, after=after)))


def test_catches_a_converter_object_deleted_without_a_word_in_the_report(clean: Clean) -> None:
    """The source still draws it, the report is silent, the object is gone."""
    b = jobj(clean.base, "slides", 1)
    el = next(e for e in jobjs(b, "elements") if e.get("main"))
    after = copy.deepcopy(clean.after)
    for oid in jstrs(el, "objects"):
        objects_of(after, jstr(b, "objectId")).pop(oid, None)
    found = kinds(checked(replace(clean, after=after)))
    assert "object_deleted_unreported" in found or "element_vanished" in found


def test_catches_a_report_that_claims_work_it_did_not_do(clean: Clean) -> None:
    """`applied` on an element nothing happened to: a lying report hides the real writes."""
    b = jobj(clean.base, "slides", 1)
    el = next(e for e in jobjs(b, "elements") if e.get("main"))
    report = copy.deepcopy(clean.report)
    jarr(report, "applied").append({"slide": b["key"], "element": el["key"], "fields": ["text"]})
    found = checked(replace(clean, report=report))
    assert "applied_no_change" in kinds(found)


def test_a_background_the_theme_carries_to_an_inheriting_slide_is_applied(clean: Clean) -> None:
    """A retheme reaches a slide that inherits its background through its layout's decoration
    (theme_sync), nothing written on the slide: its `background` entry is honest when the report
    writes that layout (live seed 0, retheme), and a lie when it writes nothing of the theme."""
    b = jobj(clean.base, "slides", 1)
    before, after = copy.deepcopy(clean.before), copy.deepcopy(clean.after)
    for snap in (before, after):
        read = slide_of(snap, jstr(b, "objectId"))
        read["background"] = {"state": "INHERIT"}
        read["layoutObjectId"] = "p13"
    report = copy.deepcopy(clean.report)
    applied = jarr(report, "applied")
    applied.append({"slide": b["key"], "element": None, "fields": ["background"]})
    assert "applied_no_change" in kinds(checked(replace(clean, before=before, after=after, report=report)))
    applied.append({"slide": "layout Title Only", "element": "p13_i6", "fields": ["theme decoration"], "page": "p13"})
    assert "applied_no_change" not in kinds(checked(replace(clean, before=before, after=after, report=report)))


def test_catches_a_converged_claim_on_an_element_that_changed(clean: Clean) -> None:
    """`converged` means nothing was written; an element that changed contradicts it."""
    entry = jobj(clean.report, "applied", 0)
    report = copy.deepcopy(clean.report)
    report["applied"] = [e for e in jobjs(report, "applied")
                         if (e.get("slide"), e.get("element")) != (entry["slide"], entry["element"])]
    jarr(report, "converged").append({"slide": entry["slide"], "element": entry["element"], "field": "text"})
    assert "converged_but_changed" in kinds(checked(replace(clean, report=report)))


def test_catches_notes_and_a_background_the_person_set_being_overwritten(clean: Clean) -> None:
    after = copy.deepcopy(clean.after)
    before = copy.deepcopy(clean.before)
    sid = jstr(clean.base, "slides", 0, "objectId")
    slide_of(before, sid)["notes"] = "remember the funding slide"
    slide_of(after, sid)["notes"] = ""
    slide_of(before, sid)["background"] = {"state": "RENDERED", "solid": "#102030"}
    slide_of(after, sid)["background"] = {"state": "RENDERED", "solid": "#ffffff"}
    found = kinds(checked(replace(clean, before=before, after=after)))
    assert "notes_word_lost" in found and "background_lost" in found


def test_catches_the_notes_of_a_slide_the_person_added_being_dropped() -> None:
    """A slide the base never saw has nothing to merge against: its notes and its background must
    come through untouched, whatever the report says."""
    base: JsonObject = {"slides": [{"key": "f1", "objectId": "s1", "elements": []}]}
    before: JsonObject = {"slides": [{"objectId": "s1", "objects": {}},
                                     {"objectId": "mine", "objects": {}, "notes": "ask about the budget",
                                      "background": {"solid": "#102030"}}]}
    after = copy.deepcopy(before)
    jobj(after, "slides", 1)["notes"] = ""
    found = [jstr(f, "kind") for f in O.user_slide_findings(base, before, after)]
    assert set(found) == {"notes_word_lost"} and len(found) == 4  # one per word of the note
    jobj(after, "slides", 1)["background"] = {"solid": "#ffffff"}
    assert "background_lost" in [f["kind"] for f in O.user_slide_findings(base, before, after)]
    assert O.user_slide_findings(base, before, before) == []


# ---------------------------------------------------------------- oracle units

def test_normalise_report_reads_both_shapes() -> None:
    nested = loss_oracle.normalise_report({"slides": {"deleted": ["a"], "moved": ["b"]}})
    flat = loss_oracle.normalise_report({"slides_deleted": ["a"], "slides_moved": ["b"]})
    assert nested["slides_deleted"] == flat["slides_deleted"] == ["a"]
    assert nested["slides_moved"] == flat["slides_moved"] == ["b"]
    assert loss_oracle.normalise_report(None)["conflicts"] == []


def test_hunks_are_the_words_the_person_added() -> None:
    got = loss_oracle.hunks("we measured the speed", "we measured the raw speed")
    assert [w for h in got for w in loss_oracle.words("".join(h[2]))] == ["raw"]


def test_words_split_like_merge_tokens() -> None:
    """The oracle has to split exactly like the merge, or it invents losses on hyphens."""
    from beamer2slides import merge
    text = "figure-theirs, 3rd try"
    assert loss_oracle.words(text) == {w.lower() for w in merge.tokens(text) if w.strip() and w.isalnum()}


def test_element_objects_finds_created_and_tagged_objects() -> None:
    skey, ekey = "results", "text/body/0"
    made = f"b2s_{loss_oracle.h6(skey)}_{loss_oracle.h6(ekey)}_7f"
    read: JsonObject = {"objects": {"old": {"title": None}, made: {"title": None},
                                    "adopted": {"title": snapshot.tag(skey, ekey)},
                                    "other": {"title": "b2s:results/text/body/1"}}}
    base_el: JsonObject = {"objects": ["old"]}
    assert loss_oracle.element_objects(skey, ekey, base_el, read) == {"old", made, "adopted"}
    # its own object, retitled for another element of the slide, is that one's now: a sync handed the
    # live subtitle placeholder on to the text that took the role over (live fuzz r8009)
    jobj(read, "objects", "old")["title"] = "b2s:results/text/body/1"
    assert loss_oracle.element_objects(skey, ekey, base_el, read) == {made, "adopted"}
    jobj(read, "objects", "old")["title"] = snapshot.tag(skey, ekey)
    assert loss_oracle.element_objects(skey, ekey, base_el, read) == {"old", made, "adopted"}


Frame = Callable[[list[float], str | None, str | None], JsonObject]


def _title_page(first: str | None, second: str | None) -> tuple[JsonObject, Frame]:
    """A title page as live fuzz r8009 had it, at scale 2: a title placeholder, the authors' lines
    (text/body/0) and a longer line (text/body/1), one of the two in the SUBTITLE placeholder
    (`first`/`second`: "SUBTITLE" or None), and a figure for `deck_placement`. A placeholder's frame
    stands `emit.PPTX_TITLE_DY` lower around its words than a text box's."""
    from beamer2slides.emit import PPTX_TITLE_DY as dy

    def frame(bb: list[float], placeholder: str | None, title: str | None) -> JsonObject:
        box = [2 * bb[0], 2 * bb[1] + (dy if placeholder else 0), 2 * bb[2], 2 * bb[3] + (dy if placeholder else 0)]
        rb: JsonObject = {"box": [*box], "transform": [2, 0, 0, 2, box[0], box[1]], "title": title}
        return {**rb, "placeholder": placeholder} if placeholder else rb
    els: list[tuple[str, str, list[float], str | None]] = [
        ("text/title/0", "t", [10, 5, 110, 15], "CENTERED_TITLE"), ("text/body/0", "a", [40, 40, 80, 60], first),
        ("text/body/1", "b", [20, 100, 140, 110], second), ("image/figure/0", "f", [150, 40, 200, 90], None)]
    elements: list[Json] = [{"key": k, "main": o, "objects": [o], "fingerprint": {"bbox": [*bb]},
                             "readback": {o: frame(bb, ph, snapshot.tag("title", k))}} for k, o, bb, ph in els]
    return {"slides": [{"key": "title", "objectId": "s1", "elements": elements}]}, frame


def test_a_carried_move_is_judged_where_the_words_are() -> None:
    """Live fuzz r8009: the person moved the authors' lines down 30 pt, and the source moved them up
    while a longer line took the title page's subtitle role from them (variant subtitle) - and gave it
    back one sync later (variant deletions). Sync makes the authors a box of their own and hands the
    placeholder, object id and all, to the text that has the role now, and carries the person's step
    onto the frame the new conversion writes. That frame is a placeholder's or a box's, emit setting
    the first `PPTX_TITLE_DY` lower around the same words: the words land at their new place plus the
    person's step, which is what the report promises. The oracle judged the frame's corner by the
    bbox step alone (3.9 pt off, both ways) and took the placeholder, now the other text's, for the
    authors' object."""
    from beamer2slides.emit import PPTX_TITLE_DY as dy
    promised = loss_oracle.normalise_report({"conflicts": [
        {"slide": "title", "element": "text/body/0", "field": "geometry", "resolution": merge.GEOMETRY_CARRIED}]})
    made = f"b2s_{loss_oracle.h6('title')}_{loss_oracle.h6('text/body/0')}_2dm"

    # subtitle: out of the placeholder into a box of its own; the source moved it up 5 (10 deck pt)
    base, frame = _title_page("SUBTITLE", None)
    live: JsonObject = {oid: rb for el in jobjs(base, "slides", 0, "elements")
                        for oid, rb in jobj(el, "readback").items()}
    moved: JsonObject = {**jobj(live, "a"), "box": [80, 110 + dy, 160, 150 + dy],
                         "transform": [2, 0, 0, 2, 80, 110 + dy]}
    before: JsonObject = {"slides": [{"objectId": "s1", "objects": {**live, "a": moved}}]}
    ours: JsonObject = {"slides": [{"key": "title", "elements": [
        {"key": "text/body/0", "fingerprint": {"bbox": [40, 35, 80, 55]}}]}]}

    def after(authors_top: float) -> JsonObject:
        box: list[Json] = [80, authors_top, 160, authors_top + 40]
        return {"slides": [{"objectId": "s1", "objects": {   # (the longer line replaces text/body/1's)
            **{k: v for k, v in live.items() if k != "b"},
            "a": frame([10, 100, 150, 110], "SUBTITLE", snapshot.tag("title", "text/body/1")),
            made: {"box": box, "transform": [2, 0, 0, 2, 80, authors_top],
                   "title": snapshot.tag("title", "text/body/0")}}}]}
    assert O.geometry_findings(base, before, after(100), promised, ours) == []        # 110 - 10
    wrong = O.geometry_findings(base, before, after(100 + dy), promised, ours)      # the frame by the bbox alone
    assert [(f["kind"], f["element"]) for f in wrong] == [("geometry_not_carried", "text/body/0")]

    # deletions: the longer line is gone and the authors (a box the person moved) go back into the
    # placeholder, which moves down 5 (10 deck pt) with them
    base, frame = _title_page(None, "SUBTITLE")
    live = {oid: rb for el in jobjs(base, "slides", 0, "elements") for oid, rb in jobj(el, "readback").items()}
    authors = jobj(base, "slides", 0, "elements", 1)
    authors["main"] = made
    jarr(authors, "objects")[0] = made
    popped = live.pop("a")
    authors["readback"] = {made: popped}
    live[made] = popped
    moved = {**jobj(live, made), "box": [80, 110, 160, 150], "transform": [2, 0, 0, 2, 80, 110]}
    before = {"slides": [{"objectId": "s1", "objects": {**live, made: moved}}]}
    ours = {"slides": [{"key": "title", "elements": [{"key": "text/body/0", "fingerprint": {"bbox": [40, 45, 80, 65]}}]}]}
    report = loss_oracle.normalise_report({**promised, "applied": [
        {"slide": "title", "element": "text/body/1", "fields": ["removed"]}]})

    def handed(top: float) -> JsonObject:
        ph: JsonObject = {"box": [80, top, 160, top + 40], "transform": [2, 0, 0, 2, 80, top],
                          "placeholder": "SUBTITLE", "title": snapshot.tag("title", "text/body/0")}
        return {"slides": [{"objectId": "s1", "objects": {
            **{k: v for k, v in live.items() if k != made}, "b": ph}}]}
    assert O.geometry_findings(base, before, handed(120 + dy), report, ours) == []   # 110 + 10
    assert [f["kind"] for f in O.geometry_findings(base, before, handed(120), report, ours)] \
        == ["geometry_not_carried"]
    # text/body/1 is gone from the slide: its placeholder carries the authors now
    assert O.report_findings(base, before, handed(120 + dy), report) == []
    kept = handed(120 + dy)
    jobj(kept, "slides", 0, "objects", "b")["title"] = snapshot.tag("title", "text/body/1")
    assert [f["kind"] for f in O.report_findings(base, before, kept, report)] == ["reported_remove_not_done"]


def test_catches_a_picture_the_person_chose_being_overwritten() -> None:
    """Compared by pixel signature: Google hands out a new contentUrl for an unchanged picture."""
    from beamer2slides.devtools import fuzz_world as W
    mine: JsonObject = {"contentHash": "m", "signature": W.picture_signature(b"person picture")}
    theirs: JsonObject = {"contentHash": "c", "signature": W.picture_signature(b"source picture")}
    base: JsonObject = {"slides": [{"key": "f1", "objectId": "s1", "elements": [
        {"key": "image/figure/0", "main": "o", "objects": ["o"], "readback": {"o": {"image": theirs}}}]}]}
    before: JsonObject = {"slides": [{"objectId": "s1", "objects": {"o": {"image": mine}}}]}
    after: JsonObject = {"slides": [{"objectId": "s1", "objects": {"o": {"image": theirs}}}]}
    empty = loss_oracle.normalise_report({})
    assert [f["kind"] for f in O.picture_findings(base, before, after, empty)] == ["picture_reverted"]
    kept: JsonObject = {"slides": [{"objectId": "s1", "objects": {"o": {"image": mine}}}]}
    assert O.picture_findings(base, before, kept, empty) == []
    said = loss_oracle.normalise_report({"conflicts": [{"slide": "f1", "element": "image/figure/0", "field": "image"}]})
    assert O.picture_findings(base, before, after, said) == []


def test_the_placement_is_the_scale_the_base_recorded() -> None:
    """A deck with fewer than three pictures was placed by its text boxes, which are wider and
    taller than their words: the edit hunt's talks (720 pt deck of a 362.8 pt PDF) read 3.08 and
    4.61 for 1.98, and three syncs that carried the person's move exactly were accused of
    `geometry_not_carried`. A base that says what emit drew with is taken at its word."""
    text: JsonObject = {"key": "text/body/0", "main": "o", "fingerprint": {"bbox": [40, 116, 325, 160]},
                        "readback": {"o": {"box": [72, 221, 736, 323]}}}              # a frame far wider than its words
    title: JsonObject = {"key": "text/title/0", "main": "t", "fingerprint": {"bbox": [10, 5, 200, 20]},
                         "readback": {"t": {"box": [10, 11, 698, 54]}}}
    base: JsonObject = {"scale": 1.9844, "slides": [{"key": "f", "elements": [text, title, {**text, "key": "text/body/1"}]}]}
    assert O.deck_placement(base) == (1.9844, 0.0, 0.0)
    del base["scale"]
    placed = O.deck_placement(base)
    assert placed is not None and placed[0] > 2.2, "the fit a base without a scale falls back to"


def test_catches_an_element_put_back_at_the_converters_box() -> None:
    """The person moved the element, the source moved it too, and the sync left it at the box the
    base recorded. `deck_placement` says where the rewritten element belongs - the conversion's new
    box with the person's step on it - so the box it *would* have had is no excuse for any other."""
    base: JsonObject = {"slides": [{"key": "f1", "objectId": "s1", "elements": [
        {"key": "text/body/0", "main": "o", "objects": ["o"], "fingerprint": {"bbox": [10, 10, 60, 30]},
         "readback": {"o": {"box": [20, 20, 120, 60], "transform": [2, 0, 0, 2, 20, 20]}}},
        {"key": "text/title/0", "main": "t", "objects": ["t"], "fingerprint": {"bbox": [10, 5, 110, 15]},
         "readback": {"t": {"box": [20, 10, 220, 30], "transform": [2, 0, 0, 2, 20, 10]}}},
        {"key": "image/figure/0", "main": "f", "objects": ["f"], "fingerprint": {"bbox": [70, 40, 120, 90]},
         "readback": {"f": {"box": [140, 80, 240, 180], "transform": [2, 0, 0, 2, 140, 80]}}}]}]}
    moved: JsonObject = {"box": [30, 0, 130, 40], "transform": [2, 0, 0, 2, 30, 0]}   # the person moved it (+10, -20)
    before: JsonObject = {"slides": [{"objectId": "s1", "objects": {"o": moved}}]}
    after: JsonObject = {"slides": [{"objectId": "s1", "objects": {
        "o": dict(jobj(base, "slides", 0, "elements", 0, "readback", "o"))}}]}
    empty = loss_oracle.normalise_report({})
    assert O.deck_placement(base) == (2.0, 0.0, 0.0)
    # The source left the element where it was: the base's box is the converter's, plainly a revert.
    still: JsonObject = {"slides": [{"key": "f1", "elements": [
        {"key": "text/body/0", "fingerprint": {"bbox": [10, 10, 60, 30]}}]}]}
    assert [f["kind"] for f in O.geometry_findings(base, before, after, empty, still)] == ["geometry_reverted"]
    # The source moved it to where the person's step lands it on the old box: nothing was reverted.
    there: JsonObject = {"slides": [{"key": "f1", "elements": [
        {"key": "text/body/0", "fingerprint": {"bbox": [5, 20, 55, 40]}}]}]}
    assert O.geometry_findings(base, before, after, empty, there) == []
    # ... but the person's step still has to be on top of it.
    assert [f["kind"] for f in O.geometry_findings(base, before, after, empty, None)] == ["geometry_reverted"]
    # A conflict excuses it - except the one that promises the person's move was carried onto the
    # source's new place (both moved it): that is a promise to keep, not an excuse.
    def said(resolution: str) -> JsonObject:
        return loss_oracle.normalise_report({"conflicts": [
            {"slide": "f1", "element": "text/body/0", "field": "geometry", "resolution": resolution}]})
    assert O.geometry_findings(base, before, after, said("deck kept"), still) == []
    assert [f["kind"] for f in O.geometry_findings(base, before, after, said(merge.GEOMETRY_CARRIED), None)] \
        == ["geometry_reverted"]
    # ... and it says where: the person's corner plus the source's move of the IR corner.
    promised = said(merge.GEOMETRY_CARRIED)
    assert O.geometry_findings(base, before, after, promised, there) == []   # (-10, +20) from (30, 0)
    assert [f["kind"] for f in O.geometry_findings(base, before, after, promised, still)] \
        == ["geometry_not_carried"]
    # the deck's absolute place, which geometry mode `theirs` wrote, is not what was promised either
    pinned: JsonObject = {"slides": [{"objectId": "s1", "objects": {"o": dict(moved)}}]}
    assert [f["kind"] for f in O.geometry_findings(base, before, pinned, promised, there)] \
        == ["geometry_not_carried"]
    assert O.geometry_findings(base, before, pinned, empty, there) == []


def _refit_round(person: list[float], pic_note: list[float] | None,
                 source_dy: float) -> tuple[JsonObject, JsonObject, JsonObject,
                                            Callable[[list[float], list[float]], JsonObject]]:
    """A block body and its formula picture, the person's edit `person` on both (their group), the
    source moving the unit `source_dy` down; the base's picture maybe noted with the last sync's
    `refit`. Returns base, before, ours and a function giving the after read-back from the text's
    and the picture's corners."""
    def rb(box: list[float]) -> JsonObject:
        return {"box": [*box], "transform": [1, 0, 0, 1, box[0], box[1]], "size": [box[2] - box[0], box[3] - box[1]]}
    t_box: list[float] = [10, 100, 700, 140]
    p_box: list[float] = [400, 110, 430, 130]
    title: list[float] = [20, 10, 220, 30]
    pic = rb(p_box)
    if pic_note:
        pic["refit"] = [*pic_note]
    shift = pic_note[0] if pic_note else 0
    base: JsonObject = {"slides": [{"key": "f1", "objectId": "s1", "elements": [
        {"key": "text/title/0", "main": "h", "objects": ["h"], "fingerprint": {"bbox": [*title]},
         "readback": {"h": rb(title)}},
        {"key": "text/body/0", "main": "t", "objects": ["t"], "fingerprint": {"bbox": [*t_box]},
         "readback": {"t": rb(t_box)}},
        {"key": "image/math/0", "main": "p", "objects": ["p"], "anchor": "text/body/0",
         "fingerprint": {"bbox": [p_box[0] - shift, p_box[1], p_box[2] - shift, p_box[3]]},
         "readback": {"p": pic}}]}]}

    def edited(r: JsonObject) -> JsonObject:
        m = snapshot.compose(person, jnums(r, "transform"))
        return {"transform": [*m], "box": [*snapshot.box(m, *jnums(r, "size"))]}
    before: JsonObject = {"slides": [{"objectId": "s1", "objects": {"t": edited(rb(t_box)), "p": edited(pic)}}]}

    def shifted(b: list[float]) -> list[Json]:
        return [b[0], b[1] + source_dy, b[2], b[3] + source_dy]
    ours: JsonObject = {"slides": [{"key": "f1", "elements": [
        {"key": "text/title/0", "fingerprint": {"bbox": [*title]}},
        {"key": "text/body/0", "fingerprint": {"bbox": shifted(t_box)}},
        {"key": "image/math/0", "fingerprint": {
            "bbox": shifted(jnums(base, "slides", 0, "elements", 2, "fingerprint", "bbox"))}}]}]}

    def after(t_at: list[float], p_at: list[float]) -> JsonObject:
        return {"slides": [{"objectId": "s1", "objects": {"t": {"box": [*t_at, t_at[0] + 1, t_at[1] + 1]},
                                                          "p": {"box": [*p_at, p_at[0] + 1, p_at[1] + 1]}}}]}
    return base, before, ours, after


def _carried_report(*refit: list[float]) -> JsonObject:
    return loss_oracle.normalise_report({
        "conflicts": [{"slide": "f1", "element": "text/body/0", "field": "geometry", "resolution": merge.GEOMETRY_CARRIED}],
        "refit": [{"slide": "f1", "element": "image/math/0", "object": "p", "what": "picture", "shift": [*s]}
                  for s in refit]})


def test_a_picture_refit_moved_is_held_to_what_the_sync_recorded() -> None:
    """r8006: the person moved the block (+20, -20) and wrote words that stranded its formula; the
    source moved the unit 10 pt down; refit took the picture 154.1 pt right, onto its hole in the
    words as merged. The oracle learns that move from the report's `refit` - never forgives a
    picture for being one: without the report's word, or at another place, it is still found."""
    base, before, ours, after = _refit_round([1, 0, 0, 1, 20.0, -20.0], None, 10.0)
    t_at = [30.0, 90.0]                                            # (10, 100) + person + source
    right = after(t_at, [420.0 + 154.1, 100.0])

    def judged(rep: JsonObject, aft: JsonObject) -> list[tuple[Json, Json]]:
        return [(f["kind"], f["element"]) for f in O.geometry_findings(base, before, aft, rep, ours)]
    assert judged(_carried_report([154.1, 0.0]), right) == []
    assert judged(_carried_report(), right) == [("geometry_not_carried", "image/math/0")]
    assert judged(_carried_report([154.1, 0.0]), after(t_at, [420.0 + 134.0, 100.0])) == \
        [("geometry_not_carried", "image/math/0")]
    assert judged(_carried_report(), after(t_at, [420.0, 100.0])) == []    # no refit: carried with its unit


def test_a_refit_the_last_sync_recorded_is_the_converters_under_the_persons_scale() -> None:
    """r8011: last sync's refit moved the picture 154.1 pt in the converter's frame (the base's
    `refit` note) and the person had scaled the unit's group 1.15. This sync's refit moves it the
    same 154.1 again (the merged words did not change): the picture stays where the person had it,
    plus the source's move. A refit that moves it further is judged through the person's scale."""
    person = [1.15, 0, 0, 1.15, -4.23, -18.82]
    base, before, ours, after = _refit_round(person, [154.1, 0.0], -7.6)
    was_t, was_p = (jnums(before, "slides", 0, "objects", o, "box") for o in ("t", "p"))
    t_at = [was_t[0], was_t[1] - 7.6]

    def judged(rep: JsonObject, aft: JsonObject) -> list[tuple[Json, Json]]:
        return [(f["kind"], f["element"]) for f in O.geometry_findings(base, before, aft, rep, ours)]
    assert judged(_carried_report([154.1, 0.0]), after(t_at, [was_p[0], was_p[1] - 7.6])) == []
    assert judged(_carried_report([184.1, 0.0]), after(t_at, [was_p[0] + 34.5, was_p[1] - 7.6])) == []
    assert judged(_carried_report([184.1, 0.0]), after(t_at, [was_p[0] + 30.0, was_p[1] - 7.6])) == \
        [("geometry_not_carried", "image/math/0")]
    # r8011 as it was: the page moved 134.0 of a planned 154.1 x 1.15 (the conjugated step)
    assert judged(_carried_report([154.1, 0.0]), after(t_at, [was_p[0] - 43.2, was_p[1] - 7.6])) == \
        [("geometry_not_carried", "image/math/0")]


def test_catches_a_resize_dropped_where_the_source_moved_the_element() -> None:
    """layout-grown-box-moved: the person made a box taller, the source moved it down, and the sync
    put it at the source's new place with the converter's height. Its place is not the base's, so the
    place check alone is silent; the size is the converter's again."""
    base: JsonObject = {"slides": [{"key": "f1", "objectId": "s1", "elements": [
        {"key": "text/body/0", "main": "o", "objects": ["o"], "fingerprint": {"bbox": [10, 10, 110, 40]},
         "readback": {"o": {"box": [10, 10, 110, 40], "transform": [1, 0, 0, 1, 10, 10]}}}]}]}
    taller: JsonObject = {"box": [10, 10, 110, 55], "transform": [1, 0, 0, 1.5, 10, 10]}
    before: JsonObject = {"slides": [{"objectId": "s1", "objects": {"o": taller}}]}
    ours: JsonObject = {"slides": [{"key": "f1", "elements": [
        {"key": "text/body/0", "fingerprint": {"bbox": [10, 41, 110, 71]}}]}]}
    report = loss_oracle.normalise_report({"conflicts": [
        {"slide": "f1", "element": "text/body/0", "field": "geometry", "resolution": merge.GEOMETRY_CARRIED}]})

    def after(box: list[float]) -> JsonObject:
        return {"slides": [{"objectId": "s1", "objects": {"new": {"box": [*box], "transform": [1, 0, 0, 1, box[0], box[1]],
                                                                  "title": snapshot.tag("f1", "text/body/0")}}}]}
    dropped = O.geometry_findings(base, before, after([10, 41, 110, 71]), report, ours)
    assert [f["kind"] for f in dropped] == ["geometry_reverted"] and "size" in jstr(dropped[0], "detail")
    assert O.geometry_findings(base, before, after([10, 41, 110, 86]), report, ours) == []
    # the source resized it too: nothing to hold the converter's size against
    grown: JsonObject = {"slides": [{"key": "f1", "elements": [
        {"key": "text/body/0", "fingerprint": {"bbox": [10, 41, 110, 90]}}]}]}
    assert O.geometry_findings(base, before, after([10, 41, 110, 71]), report, grown) == []


def test_catches_styling_put_back_the_way_the_converter_had_it() -> None:
    base: JsonObject = {"slides": [{"key": "f1", "objectId": "s1", "elements": [
        {"key": "text/body/0", "main": "o", "objects": ["o"], "readback": {"o": {"text_style_hash": "converter"}}}]}]}
    before: JsonObject = {"slides": [{"objectId": "s1", "objects": {"o": {"text_style_hash": "person"}}}]}
    after: JsonObject = {"slides": [{"objectId": "s1", "objects": {"o": {"text_style_hash": "converter"}}}]}
    empty = loss_oracle.normalise_report({})
    assert [f["kind"] for f in O.style_findings(base, before, after, empty)] == ["style_reverted"]
    kept: JsonObject = {"slides": [{"objectId": "s1", "objects": {"o": {"text_style_hash": "person"}}}]}
    assert O.style_findings(base, before, kept, empty) == []
    # An `overrides` entry claims the deck's styling was kept, so it excuses nothing: only a
    # conflict (the report saying the styling gave way) accounts for the loss.
    claimed = loss_oracle.normalise_report({"overrides": [{"slide": "f1", "element": "text/body/0", "fields": ["text_style"]}]})
    assert [f["kind"] for f in O.style_findings(base, before, after, claimed)] == ["style_reverted"]
    said = loss_oracle.normalise_report({"conflicts": [{"slide": "f1", "element": "text/body/0", "field": "text_style"}]})
    assert O.style_findings(base, before, after, said) == []


def read_order(order: list[str]) -> JsonObject:
    """A read-back of empty slides in `order`."""
    return {"slides": [{"objectId": s, "objects": {}} for s in order]}


def empty_slides(n: int) -> JsonObject:
    """A base of `n` empty slides, keys k0.. and ids s0.."""
    return {"slides": [{"key": f"k{i}", "objectId": f"s{i}", "elements": []} for i in range(n)]}


def test_a_reported_swap_does_not_accuse_the_slides_between_it() -> None:
    """Two slides swapping puts a third at the same index with other neighbours: the order is
    accounted for when taking the reported moves out of both orders leaves the same sequence."""
    base = empty_slides(5)
    read = read_order
    before, after = read(["s0", "s1", "s2", "s3", "s4"]), read(["s0", "s3", "s2", "s1", "s4"])
    assert O.order_findings(base, before, after, loss_oracle.normalise_report(
        {"slides": {"moved": ["k1", "k3"]}})) == []
    half = O.order_findings(base, before, after, loss_oracle.normalise_report({"slides": {"moved": ["k1"]}}))
    assert [f["kind"] for f in half] == ["slide_moved_unreported"]


def test_a_copy_travelling_with_the_slide_it_follows_accuses_nobody() -> None:
    """Live round 303: the person duplicated a slide, the source moved the original, and sync moved
    the copy along behind it. Which of two slides that changed places "moved" has no single answer,
    so the report naming the source's own move has to be enough - the slide it passed did not move
    by itself, and neither did the copy that rode along with it."""
    base = empty_slides(4)
    read = read_order
    # k2 moves up past k1; "u", the person's copy of k2, keeps sitting behind it.
    before, after = read(["s0", "s1", "s2", "u", "s3"]), read(["s0", "s2", "u", "s1", "s3"])
    rep = loss_oracle.normalise_report({"slides": {"moved": ["k2"]}})
    assert O.order_findings(base, before, after, rep) == []
    # A copy that went somewhere of its own, behind a slide it never followed, is still caught.
    away = read(["u", "s0", "s1", "s2", "s3"])
    assert [f["kind"] for f in O.order_findings(base, before, away, rep)] == ["user_slide_moved"]


def test_a_word_the_source_put_in_another_element_is_no_undone_deletion() -> None:
    was, now = "the author wrote this", "the wrote this"
    after_words = {"the", "wrote", "this", "author", "retitled"}

    def findings(after_el_words: set[str]) -> list[loss_oracle.Finding]:
        return loss_oracle.word_findings("f4", ElementKey("text/body/1"), ObjectId("o"), was, now,
                                         loss_oracle.words(now), after_words, after_el_words, [], None)
    # 'author' is back on the slide, but in the retitled frame title, not where the person deleted it
    assert findings({"the", "wrote", "this"}) == []
    assert [f.kind for f in findings({"the", "author", "wrote", "this"})] == ["deletion_undone"]


def _occlusion_world() -> tuple[JsonObject, JsonObject]:
    """A slide with a text object `t` on an opaque block panel `p` (both in the block group `g`,
    panel first) and a user text box `u` elsewhere, as the read-back before a sync."""
    fill: JsonObject = {"fill": {"color": "#dde4f0", "alpha": 1.0}}
    objects: JsonObject = {
        "g": {"kind": "elementGroup", "box": [50, 100, 400, 156], "children": ["p", "t"]},
        "p": {"kind": "shape", "box": [50, 100, 400, 156], "text": "", "parent_group": "g", "shape_style": fill},
        "t": {"kind": "shape", "box": [60, 116, 390, 140], "text": "words the person edited\n", "parent_group": "g",
              "shape_style": {"fill": None}},
        "u": {"kind": "shape", "box": [300, 300, 460, 330], "text": "a note of the person's\n",
              "shape_style": {"fill": None}},
    }
    base: JsonObject = {"slides": [{"key": "f1", "objectId": "s1", "groups": ["g"], "elements": [
        {"key": "shape/panel/0", "main": "p", "objects": ["p"]},
        {"key": "text/body/0", "main": "t", "objects": ["t"]}]}]}
    before: JsonObject = {"slides": [{"objectId": "s1", "order": ["g", "u"], "objects": objects}]}
    return base, before


def test_catches_text_hidden_under_a_shape_the_sync_created() -> None:
    """Nothing is deleted when a sync stacks a new panel over the words on it, so every other check
    passes: the text is still there, just unreadable (a live sync did this to a block the person had
    edited, dc8523a). The rebuilt panel has to go where the old one was, under the text."""
    base, before = _occlusion_world()
    new = f"b2s_{loss_oracle.h6('f1')}_{loss_oracle.h6('shape/panel/0')}_1zz"

    def synced(children: list[Json]) -> JsonObject:
        after = copy.deepcopy(before)
        objects = jobj(after, "slides", 0, "objects")
        objects[new] = {**jobj(objects.pop("p")), "box": [50, 96, 400, 160]}  # the source resized the panel
        jobj(objects, "g")["children"] = children
        return after
    ours_order: JsonObject = {"slides": [{"key": "f1", "elements": [{"key": "shape/panel/0"}, {"key": "text/body/0"}]}]}
    found = O.occlusion_findings(base, before, synced(["t", new]), ours_order)
    assert [(f["kind"], f["object"], f["element"]) for f in found] == [("text_hidden", "t", "text/body/0")]
    assert "loss" in {jstr(f, "severity") for f in found}
    assert O.occlusion_findings(base, before, synced([new, "t"]), ours_order) == []   # the fix
    # A text the sync rebuilt as well counts through its element (its new object is not `t`).
    after = synced(["t", new])
    text = f"b2s_{loss_oracle.h6('f1')}_{loss_oracle.h6('text/body/0')}_1zz"
    objs = jobj(after, "slides", 0, "objects")
    objs[text] = objs.pop("t")
    jobj(objs, "g")["children"] = [text, new]
    assert [f["object"] for f in O.occlusion_findings(base, before, after, ours_order)] == [text]
    # The new conversion stacking the shape above that text itself: the sync kept the source's order.
    above: JsonObject = {"slides": [{"key": "f1", "elements": [{"key": "text/body/0"}, {"key": "shape/panel/0"}]}]}
    assert O.occlusion_findings(base, before, synced(["t", new]), above) == []


def test_the_fuzz_world_hands_the_merge_everything_sync_does_about_identity(tmp_path: Path) -> None:
    """`fuzz_world.build_ours` is a copy of `sync.build_ours`, and a copy drifts. It left out
    `weak_pairs` and `near_misses` - the two things `merge.plan_merge` turns into its warnings
    about which frame is which - so every campaign round ran against a world where the person is
    never told the pairing was a guess, and a frame put on a look-alike slide read as a loss in
    silence. What sync hands the merge, the harness hands it too."""
    import inspect
    import random

    from beamer2slides import sync
    from beamer2slides.devtools import fuzz_world as W

    said = inspect.getsource(sync.ours_json)   # (what `build_ours` hands the merge)
    wanted = {k for k in ("pairs", "label_moves", "weak_pairs", "near_misses") if f'"{k}"' in said}
    assert wanted == {"pairs", "label_moves", "weak_pairs", "near_misses"}, "sync stopped saying one"
    doc = W.make("adopt", random.Random(7), tmp_path)
    ours = W.build_ours(doc, W.build_base(doc, tmp_path), tmp_path).json
    assert wanted <= set(ours), f"the harness says less than sync does: {wanted - set(ours)}"


def test_hidden_text_on_a_slide_the_report_calls_uncertain_is_a_note() -> None:
    """A shape lands on a slide because a frame was written there, so this finding is downstream of
    the pairing: when the sync has already told the person, in the same report, that it may have
    put a frame on the wrong slide, the panel over their words is that frame landing where the
    report said it might - announced, not silent, which is the only thing this oracle judges.

    Seven of the eight rounds the 4,000-round adopt-shaped campaign failed on were exactly this,
    and the campaign could not see it because `fuzz_world.build_ours` left `weak_pairs` out of
    `ours` - a world where the person is never told."""
    base, before = _occlusion_world()
    new = f"b2s_{loss_oracle.h6('f1')}_{loss_oracle.h6('shape/panel/0')}_1zz"
    after = copy.deepcopy(before)
    objects = jobj(after, "slides", 0, "objects")
    objects[new] = {**jobj(before, "slides", 0, "objects", "p"), "parent_group": "g"}
    jobj(objects, "g")["children"] = ["t", new]
    ours: JsonObject = {"slides": [{"key": "f1", "elements": [{"key": "shape/panel/0"}, {"key": "text/body/0"}]}]}
    assert [f["severity"] for f in O.occlusion_findings(base, before, after, ours)] == ["loss"]

    # a label the content says is on another frame now: the report carries a conflict per slide
    moved: JsonObject = {**ours, "label_moves": [
        {"label": "x", "verdict": "unsure", "ours": 0, "slide": "f1", "frame_is": None}]}
    found = O.occlusion_findings(base, before, after, moved)
    assert [f["severity"] for f in found] == ["note"] and "may be the wrong one" in jstr(found[0], "detail")
    # ... and a pairing the words could as well have made next door, for an unlabelled frame
    # (pairs as the JSON has them: keys are strings, `merge.ours_of` reads them as indices)
    twins: JsonObject = {**ours, "pairs": {"0": 0}, "weak_pairs": {"0": "twins"}}
    assert [f["severity"] for f in O.occlusion_findings(base, before, after, twins)] == ["note"]
    # the same frame with a label of its own is not what `merge.plan_merge` warns about
    labelled: JsonObject = {"slides": [{**jobj(ours, "slides", 0), "label": "f1"}], "pairs": {"0": 0},
                            "weak_pairs": {"0": "twins"}}
    assert [f["severity"] for f in O.occlusion_findings(base, before, after, labelled)] == ["loss"]


def _folded_world() -> tuple[JsonObject, JsonObject]:
    """The person folded a converter panel into a group of their own, beside their own heading; the
    body text it belongs with stays in the converter's block group, below that group on the page."""
    opaque: JsonObject = {"fill": {"color": "#dddddd", "alpha": 1.0}}
    objects: JsonObject = {
        "g": {"kind": "elementGroup", "box": [80, 328, 430, 384], "children": ["t"]},
        "t": {"kind": "shape", "box": [90, 344, 420, 368], "text": "bullet editor picture table\n",
              "parent_group": "g", "shape_style": {"fill": None}},
        "ug": {"kind": "elementGroup", "box": [40, 40, 400, 332], "children": ["mine", "p"]},
        "mine": {"kind": "shape", "box": [40, 40, 256, 68], "text": "the person's own heading\n",
                 "parent_group": "ug", "shape_style": {"fill": None}},
        "p": {"kind": "shape", "box": [50, 272, 400, 332], "text": "", "parent_group": "ug", "shape_style": opaque},
    }
    base: JsonObject = {"slides": [{"key": "f1", "objectId": "s1", "groups": ["g"], "elements": [
        {"key": "shape/panel/0", "main": "p", "objects": ["p"]},
        {"key": "text/body/0", "main": "t", "objects": ["t"]}]}]}
    before: JsonObject = {"slides": [{"objectId": "s1", "order": ["g", "ug"], "objects": objects}]}
    return base, before


def test_a_shape_in_a_group_the_person_made_is_a_note_not_a_loss() -> None:
    """The one shape of this defect the sync cannot order its way out of. Z-order is written in two
    places - the page's element order and the children of a group the sync rebuilds - and neither
    reaches a converter element the person folded into a group of their own: its page element is
    that group, so restacking it moves the rest of what they put in there, and children of a group
    nobody rebuilds cannot be reordered at all. The source's order across the two page elements is
    then unrealisable, honouring the grouping is not what hid the words, and `Sync.finish` says so
    in the report (`sync.folded_hiders`). 2 of 2,600 chained rounds; converted seed 670146 at 6."""
    base, before = _folded_world()
    new = f"b2s_{loss_oracle.h6('f1')}_{loss_oracle.h6('shape/panel/0')}_3zz"
    after = copy.deepcopy(before)
    objs = jobj(after, "slides", 0, "objects")
    objs[new] = {**jobj(objs.pop("p")), "box": [50, 272, 370, 352]}   # the source redrew it taller
    jobj(objs, "ug")["children"] = ["mine", new]
    ours: JsonObject = {"slides": [{"key": "f1", "elements": [{"key": "shape/panel/0"}, {"key": "text/body/0"}]}]}
    found = O.occlusion_findings(base, before, after, ours)
    assert [(f["kind"], f["severity"], f["object"]) for f in found] == [("text_hidden", "note", "t")]
    assert "in a group the person made" in jstr(found[0], "detail")
    # ... and the very same panel standing on the page itself is a loss: `restack` could order it.
    page = copy.deepcopy(after)
    on_page = jobj(page, "slides", 0)
    on_page["order"] = ["g", new]
    page_objects = jobj(on_page, "objects")
    jobj(page_objects, new)["parent_group"] = None
    del page_objects["ug"], page_objects["mine"]
    assert [f["severity"] for f in O.occlusion_findings(base, before, page, ours)] == ["loss"]


def test_a_shape_the_source_itself_added_above_the_text_is_no_finding() -> None:
    """The same excuse for an element the *source added*: it has no base element, so an oracle that
    named objects by the base alone could not tell which shape it was looking at - and
    `_source_stacks_above` can excuse nothing it cannot name. Every adopt-shaped round where a new
    source drew a panel over its own text was reported as a loss."""
    base, before = _occlusion_world()
    added = f"b2s_{loss_oracle.h6('f1')}_{loss_oracle.h6('shape/panel/1')}_1zz"
    after = copy.deepcopy(before)
    objs = jobj(after, "slides", 0, "objects")
    objs[added] = {**jobj(objs, "p"), "box": [50, 96, 400, 160], "parent_group": None}
    jarr(after, "slides", 0, "order").append(added)
    els: list[Json] = [{"key": "shape/panel/0"}, {"key": "text/body/0"}, {"key": "shape/panel/1"}]
    assert O.occlusion_findings(base, before, after, {"slides": [{"key": "f1", "elements": els}]}) == []
    below: list[Json] = [els[0], els[2], els[1]]   # ... and it is a loss again when the source draws it under
    found = O.occlusion_findings(base, before, after, {"slides": [{"key": "f1", "elements": below}]})
    assert [(f["kind"], f["object"]) for f in found] == [("text_hidden", "t")]


def placed_over(read: JsonObject, oid: str, rb: JsonObject, where: str) -> JsonObject:
    """A copy of `read` with `rb` as its first slide's object `oid`, at the top of its page order or
    ("bottom") under everything."""
    out = copy.deepcopy(read)
    jobj(out, "slides", 0, "objects")[oid] = rb
    order = jarr(out, "slides", 0, "order")
    if where == "bottom":
        order.insert(0, oid)
    else:
        order.append(oid)
    return out


def test_text_already_hidden_or_under_something_else_is_no_finding() -> None:
    base, before = _occlusion_world()
    new = "b2s_000000_111111_1zz"
    opaque: JsonObject = {"kind": "shape", "text": "", "shape_style": {"fill": {"color": "#000000", "alpha": 1.0}}}
    # the person had already put an opaque shape over their own note: nothing readable to lose
    covered = placed_over(before, "mine", {**opaque, "box": [290, 290, 470, 340]}, "top")
    after = placed_over(covered, new, {**opaque, "box": [290, 290, 470, 340]}, "top")
    assert O.occlusion_findings(base, covered, after, None) == []
    # ... but on the readable slide the same new shape hides the note
    after = placed_over(before, new, {**opaque, "box": [290, 290, 470, 340]}, "top")
    assert [f["object"] for f in O.occlusion_findings(base, before, after, None)] == ["u"]
    # a see-through fill, a corner overlap and a shape below it hide nothing
    cases: list[tuple[JsonObject, str]] = [
        ({**opaque, "shape_style": {"fill": {"color": "#000000", "alpha": 0.5}}, "box": [290, 290, 470, 340]}, "top"),
        ({**opaque, "box": [440, 320, 520, 400]}, "top"),
        ({**opaque, "box": [290, 290, 470, 340]}, "bottom")]
    for rb, where in cases:
        after = placed_over(before, new, rb, where)
        assert O.occlusion_findings(base, before, after, None) == [], rb


def test_the_offline_fuzz_stacks_a_rebuilt_block_as_sync_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """`fuzz_sync._stacked` replays `sync.Sync.regroup_requests` through Slides' z-order rules (a
    group keeps the page order its children had), so taking the restack out of sync - which is what
    hid a block's body text under its rebuilt panel live - fails the offline fuzz: the source resizes
    a block's panel, sync rebuilds it, and the text it kept is under it. With the restack, clean."""
    from beamer2slides.sync import Regroup, Sync

    def shrunk() -> fuzz_sync.Step:   # seed 373, as the fuzz shrank it
        return offline_round(373, ["resize_element"], ["edit_cell"], None, "converted")
    result = shrunk()
    assert not result.failures, loss_oracle.described(result.failures)
    assert any("resize p0k0" in s for s in result.source)   # (p<page>k0: a block's panel)
    kept = Sync.regroup_requests

    def unstacked(regroup: Mapping[ObjectId, Regroup], depth: Mapping[ObjectId, int], objects: JsonMap,
                  tops: Mapping[str, str], keep_ids: Collection[str], rank: Mapping[str, int]) -> list[JsonObject]:
        return [r for r in kept(regroup, depth, objects, tops, keep_ids, rank) if "updatePageElementsZOrder" not in r]
    monkeypatch.setattr(Sync, "regroup_requests", staticmethod(unstacked))
    found = shrunk().failures
    assert [f.kind for f in found] == ["text_hidden"], loss_oracle.described(found)
    assert "regroup_requests" in found[0].detail
    # and over the default rounds too, not only on a seed picked for it
    assert any(a_round(seed).failures for seed in (156, 250, 263, 290, 349))


def test_the_offline_fuzz_orders_the_page_as_sync_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same for the page's own element order. `fuzz_sync._restacked` replays `Sync.restack`'s
    requests over the slide as the content batch leaves it, so what the campaign judges there is the
    mechanism and not the applier's hand-written copy of it (`fuzz_world._page_order` / `_restack` /
    `_not_over_kept`). Those are two files that have to be fixed together, and until this there was
    nothing to say when they drift apart: the occlusion defect at converted seed 8300231 was caught
    only because both halves happened to be wrong in the same way. With `restack` writing nothing, 6
    of the first 120 converted rounds at chain 6 end with words a person could read under a shape the
    sync made; with it, clean. It reaches what the group rule cannot: an element that stands on the
    page itself, which no `groupObjects` can reorder."""
    from beamer2slides.sync import Sync
    assert not offline_chain(19, 6, None).failures

    def no_restack(self: Sync, w: Mapping[str, object], before: JsonMap, now: JsonMap) -> list[JsonObject]:
        return []
    monkeypatch.setattr(Sync, "restack", no_restack)
    found = offline_chain(19, 6, None).failures
    assert [f.kind for f in found] == ["text_hidden"], loss_oracle.described(found)


def test_the_offline_base_is_what_the_sync_made_not_what_it_left(monkeypatch: pytest.MonkeyPatch) -> None:
    """`fuzz_world.rebase` records a recreated object as the sync made it, before the deck's
    overrides went back on - `Sync.new_base` reads `Sync.created`, docs/sync.md "After writing".
    It used to record the final deck, so a merged edit became the base's own: the person's move
    carried onto a recreated box read as unmoved from then on. The next source change to that box
    put it back at the converter's place and the oracle, judging against that base, saw nothing;
    and a second move of it was carried twice (converted seed 93863 at chain 4,
    `geometry_not_carried`). Here: the person moves `p5t2`'s box, then the source rewords it twice
    with the deck left alone - the box stays where the person put it."""
    assert not offline_chain(93863, 4, None).failures

    def reword(rng: random.Random, doc: JsonObject, ctx: fuzz_sync.SourceContext) -> str | None:
        el = next(e for s in jobjs(doc, "slides") for e in jobjs(s, "elements") if e["id"] == "p5t2")
        run = jobj(el, "paragraphs", 0, "runs", 0)
        run["text"] = jstr(run, "text") + " again-ours"
        return "reword p5t2"

    def nothing(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
        return None
    monkeypatch.setitem(fuzz_sync.SOURCE_OPS, "pinned", reword)
    monkeypatch.setitem(fuzz_sync.DECK_OPS, "nothing", nothing)
    ops = [*offline_chain(93863, 1, None).ops, *[fuzz_sync.Ops(deck=("nothing",), source=("pinned",))] * 2]
    steps = offline_chain(93863, 3, ops).steps
    moved = [40.0, 140.0, 390.0, 212.0]           # step 0: the person moved it 10, 20 up and left
    for s in steps[1:]:
        next_base = s.state.next_base
        assert next_base is not None
        f5 = next(b for b in jobjs(next_base, "slides") if b["key"] == "f5")
        el = next(e for e in jobjs(f5, "elements") if e["key"] == "text/body/1")
        main = jstr(el, "main")
        assert jat(el, "readback", main, "box") == [50.0, 160.0, 400.0, 232.0]   # the converter's
        after = next(a for a in s.state.after.slides if a.object_id == f5["objectId"])
        assert list(after.objects[ObjectId(main)].box) == moved, f"step {s.step}"


def test_failures_are_the_severities_that_matter() -> None:
    def finding(kind: loss_oracle.FindingKind, severity: loss_oracle.Severity, detail: str) -> loss_oracle.Finding:
        return loss_oracle.Finding(kind=kind, severity=severity, slide="f1", element=None, object=None, detail=detail)
    made = [finding("picture_unverified", "note", "unverified"), finding("word_lost", "loss", "gone")]
    assert [f.kind for f in loss_oracle.failing(made)] == ["word_lost"]
    assert "gone" in loss_oracle.described(made)
    # ... and the same over the JSON the archives hold
    as_json = [loss_oracle.finding_json(f) for f in made]
    assert [f["kind"] for f in loss_oracle.failures(as_json)] == ["word_lost"]
    assert loss_oracle.describe(as_json) == loss_oracle.described(made)


def test_a_finding_reads_back_as_it_was_written() -> None:
    """`finding_of` reads the kinds `FindingKind` names and nothing else: the two lists are one."""
    import typing
    assert set(loss_oracle.KINDS) == set(typing.get_args(loss_oracle.FindingKind))
    assert len(loss_oracle.KINDS) == len(set(loss_oracle.KINDS))
    f = loss_oracle.Finding(kind="text_hidden", severity="loss", slide="f1", element="text/body/0", object="t",
                            detail="under a panel")
    assert loss_oracle.finding_of(loss_oracle.finding_json(f), "f") == f
    with pytest.raises(ValueError):
        loss_oracle.finding_of({**loss_oracle.finding_json(f), "kind": "no_such_kind"}, "f")


def test_a_live_round_record_writes_back_the_bytes_it_was_read_from() -> None:
    """round.json is read back by tools/layout_oracle.py and tools/fuzz_reach.py, so `record_json`
    writes the keys in the order the file has always had: a record read and written back is the same
    text, with the late cost of a round that died after its steps and without a reuse or a deck."""
    import json

    step: JsonObject = {"step": 0, "variant": "reword", "edits": [{"kind": "move", "dx": 10}],
            "findings": [{"kind": "word_lost", "severity": "loss", "slide": "f1", "element": "text/body/0",
                          "object": "o", "detail": "'raw', typed by the person, is nowhere on the slide any more"}],
            "cost": {"seconds": 1.5, "phases": {"sync": {"seconds": 1.0}}}, "layout": {"text_overlap:loss": 1},
            "reach": {"hole": 1}}
    whole: JsonObject = {"seed": 7, "steps": [step, {"step": 1, "variant": "addbullet", "edits": [], "findings": []}],
             "edits_mode": "batched", "focus": "layout", "reuse": True, "reuse_ids": {"named": 3, "missing": 0},
             "deck": "https://docs.google.com/presentation/d/x", "cost": {"seconds": 12.5, "phases": {}},
             "problems": ["step 0 (reword): gone"]}
    late: JsonObject = {"seed": 8, "steps": [], "edits_mode": "reread", "focus": None, "reuse": False,
                        "problems": ["crashed: boom"], "cost": {"phases": {"build": {"seconds": 3.0}}}}
    for record in (whole, late):
        text = json.dumps(record, indent=1, ensure_ascii=False)
        read: Json = json.loads(text)
        back = fuzz_sync.record_json(fuzz_sync.live_record(read, "round.json"))
        assert json.dumps(back, indent=1, ensure_ascii=False) == text


# ---------------------------------------------------------------- live campaign (opt-in)

@pytest.mark.sync
def test_live_fuzz_rounds() -> None:
    """Real conversions, real Slides edits, real syncs. Decks of passing rounds are deleted."""
    rounds = int(os.environ.get("B2S_FUZZ_LIVE_ROUNDS", "3"))
    out = Path(os.environ.get("B2S_FUZZ_OUT", ROOT / "out" / "sync-fuzz"))
    bad = fuzz_sync.run_live(rounds, int(os.environ.get("B2S_FUZZ_LIVE_SEED", "0")), 3,
                             int(os.environ.get("B2S_FUZZ_LIVE_CHAIN", "1")), False, out, False, "batched", None, False)
    assert not bad, "\n".join(f"seed {r.seed} ({r.deck}): " + "; ".join(r.problems) for r in bad)
