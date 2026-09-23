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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from beamer2slides import merge
from beamer2slides.devtools import fuzz_sync
from beamer2slides.devtools import fuzz_world as W
from beamer2slides.devtools import loss_oracle
from beamer2slides import snapshot

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

@pytest.mark.parametrize("seed", [*range(ROUNDS), *REGRESSIONS])
def test_offline_round_loses_nothing(seed):
    result = fuzz_sync.offline_round(seed)
    if result["failures"]:
        small = fuzz_sync.shrink_offline(result)
        pytest.fail(f"seed {seed} lost something\n{loss_oracle.describe(small['failures'])}\n"
                    f"  deck:   {'; '.join(small['deck'])}\n  source: {'; '.join(small['source'])}")


@pytest.mark.parametrize("seed", [*range(ADOPT_ROUNDS), *ADOPT_REGRESSIONS])
def test_offline_round_on_an_adopt_shaped_deck_loses_nothing(seed):
    result = fuzz_sync.offline_round(seed, shape="adopt")
    if result["failures"]:
        small = fuzz_sync.shrink_offline(result, shape="adopt")
        pytest.fail(f"adopt seed {seed} lost something\n{loss_oracle.describe(small['failures'])}\n"
                    f"  deck:   {'; '.join(small['deck'])}\n  source: {'; '.join(small['source'])}")


def test_the_first_sync_world_leaves_the_persons_unpaired_boxes_on_their_slide(tmp_path):
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
    live = {s["objectId"]: s for s in W.live_of(base)["slides"]}
    left = [el for s in base["slides"] for el in s["elements"] if el.get("left_object")]
    drawn = [el for s in base["slides"] for el in s["elements"] if el.get("drawn_from")]
    assert left and drawn, "the world draws both kinds of element tied to no object"
    assert not any(el.get("objects") for el in left + drawn)
    for s in base["slides"]:
        read = live[s["objectId"]]
        theirs = {o["objectId"] for o in merge.user_objects(s, read)}
        assert theirs == set(s.get("left_alone") or ()), "the person's own boxes, and only those"
        assert all(el["left_object"] in read["objects"] and el["left_object"] in read["order"]
                   for el in s["elements"] if el.get("left_object"))
        tied = {el["main"] for el in s["elements"] if el.get("main")}
        assert set(read["objects"]) == tied | theirs, \
            "and nothing standing for a picture the converter read out of one of them"


def test_a_broken_label_invariant_sends_far_fewer_slides_to_the_wrong_frame(tmp_path):
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

    rounds = [r for seed in range(100) for r in fuzz_labels.round_once(seed, 0.5, tmp_path)]
    sound = [r for r in rounds if not r["broke"]]
    broken = [r for r in rounds if r["broke"] and not r["reordered"]]
    assert len(sound) > 30 and len(broken) > 30  # the harness really does break labels, and not always
    assert [r["said"] for r in sound] == ["quiet"] * len(sound)  # never a word about a sound source
    assert sum(r["wrong"]["now"] for r in rounds if not r["broke"]) == 0, \
        "a source that kept its labels lost a frame's slide anyway"
    was, now = (sum(r["wrong"][k] for r in broken) for k in ("before", "now"))
    assert now * 5 < was, f"the check is not earning its place: {was} -> {now}"
    wrong = [r for r in broken if r["wrong"]["now"]]
    silent = [r for r in wrong if r["said"] == "quiet"]
    assert len(silent) <= 1 and len(wrong) <= 2, \
        f"too much goes wrong unsaid: {[(r['seed'], r['ops']) for r in wrong]}"
    assert all(r["wrong"]["now"] <= r["wrong"]["before"] for r in rounds)  # never worse than before


def test_labels_are_the_only_thing_holding_an_adopt_shaped_deck_together(tmp_path):
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

    rounds = [r for seed in range(150) for r in fuzz_labels.round_once(seed, 0.5, tmp_path, shape="adopt")]
    sound = [r for r in rounds if not r["broke"]]
    broken = [r for r in rounds if r["broke"]]
    assert len(sound) > 30 and len(broken) > 30
    assert sum(r["wrong"]["now"] for r in sound) == 0, "a source that kept its labels lost a frame's slide"
    assert [r["said"] for r in sound] == ["quiet"] * len(sound)   # and never a word about a sound one
    order, before, now = (sum(r["wrong"][k] for r in broken) for k in ("order", "before", "now"))
    assert order > 10, "the harness stopped breaking labels, so this proves nothing"
    assert before == order, f"the leftover passes now recover something here ({order} -> {before}): say so"
    assert now * 2 < order, f"the moved-label check is not earning its place: {order} -> {now}"


def test_a_frame_the_source_moved_across_another_keeps_its_slide(tmp_path):
    """The other half of identity, on the same tagged truth: a frame that crossed another one.

    The alignment keeps the order, so of two frames that swapped only one stays in the chain; the
    other falls out and comes back as a new frame, and the slide it was made from is read as
    dropped - nobody's words are deleted, but the source writes them onto the slide next door.
    `identity.cross_pairs` pairs those leftovers when the content is unmistakable. Measured over
    3000 rounds: 5.53% -> 0.00% of the frames in sound rounds that moved one, and 19.15% -> 1.18%
    when a label was broken in the same round. The 300 seeds here hold that where it matters."""
    from beamer2slides.devtools import fuzz_labels

    rounds = [r for seed in range(300) for r in fuzz_labels.round_once(seed, 0.5, tmp_path)]
    moved = [r for r in rounds if r["reordered"]]
    assert len(moved) >= 15, "the harness stopped moving frames, so this proves nothing"
    assert sum(r["wrong"]["order"] for r in moved) > 0, \
        "the order-keeping pass alone gets these right: the leftovers pass is not earning its place"
    assert sum(r["wrong"]["now"] for r in moved) == 0, \
        f"frames lost their slide: {[(r['seed'], r['wrong'], r['ops']) for r in moved if r['wrong']['now']]}"
    assert all(r["wrong"]["before"] <= r["wrong"]["order"] for r in rounds)  # never worse than the order alone


def test_the_applier_keeps_styling_the_real_sync_would_put_back():
    """The reference applier decides for itself whether the person's run styling has anywhere to go
    when an element is recreated, and it has to decide it the way `sync.style_range_requests` really
    does: by mapping each styled span through the matching blocks of the old text and the new one.

    Reading it word by word instead threw styling away that sync re-applies, and the campaign then
    accused the merge of losing it (offline seed 23599 --shape adopt, chained: the person bolded a
    word, reworded that very sentence themselves two syncs later - which clips the bold to two
    letters inside another word, as Slides does - and the source then reworded it too)."""
    from beamer2slides.devtools import fuzz_world

    plain, bold = {"fontFamily": "Lato"}, {"fontFamily": "Lato", "bold": True}
    base_rb = {"text_styles": [plain]}
    live = {"text": "policy-theirs colleague source\n", "run_spans": [[11, 13, bold], [13, 30, plain]]}
    assert not fuzz_world._styling_ends(base_rb, live, "policy-theirs colleague-ours source\n")
    # The other way: the source replaced the word those letters were in, so the styling really ends.
    assert fuzz_world._styling_ends(base_rb, live, "wording colleague source\n")
    # The converter's own styling is not the person's, and never counts as lost.
    only_ours = {"text": live["text"], "run_spans": [[0, 30, plain]]}
    assert not fuzz_world._styling_ends(base_rb, only_ours, "wording colleague source\n")


def test_the_round_notices_a_base_that_would_not_settle(tmp_path):
    """The settle check with a base broken on purpose: one that has forgotten a slide makes the next
    sync create it again, and the round has to say so."""
    result = fuzz_sync.offline_round(3, source_ops=["reword"], deck_ops=["add_text_box", "reword"], work=tmp_path)
    st = result["state"]
    assert fuzz_sync._settled(st["doc"], st["next_base"], st["after"], tmp_path) == []
    forgetful = copy.deepcopy(st["next_base"])
    forgetful["slides"] = forgetful["slides"][1:]
    found = fuzz_sync._settled(st["doc"], forgetful, st["after"], tmp_path)
    assert [f["kind"] for f in found] == ["second_sync_writes"] and found[0]["severity"] == "report"


def test_the_fuzzer_only_makes_decks_slides_could_hand_back():
    """Another guard on the harness. Slides never gives two things one objectId, and a deck that
    does is not a deck a sync could ever meet: the two slides read back as one, so the oracle saw a
    picture the person had added disappear and the report list one created slide where two appeared.
    That was converted seed 9200614 at chain 6 - three findings, every one of them the fuzzer's own
    hand - and six digits drawn afresh per slide collide long before a campaign is over.
    `fuzz_sync._fresh` gives a drawn id a tail rather than drawing again, so an op still takes
    exactly one number from its rng and a campaign's every other round is unchanged."""
    import random
    from beamer2slides.devtools import fuzz_world

    def slide(sid, text):
        return {"objectId": sid, "layoutObjectId": "L", "background": None, "notes": "",
                "notes_id": f"{sid}_n", "order": [f"{sid}_t"],
                "objects": {f"{sid}_t": fuzz_world.readback("shape", [0, 0, 100, 40], text=text)}}

    class Stuck(random.Random):
        """An rng whose every draw is the collision."""
        def randrange(self, start, stop=None, step=1):
            return 790791 % start if stop is None else start

    live = {"slides": [slide("b2s_s000", "the converter's own\n")]}
    for _ in range(3):
        fuzz_sync.deck_duplicate_slide(Stuck(), {"slides": []}, live)
        fuzz_sync.deck_add_slide(Stuck(), {"slides": []}, live)
    ids = [x for s in live["slides"] for x in [s["objectId"], s["notes_id"], *s["objects"]]]
    assert len(set(ids)) == len(ids), sorted(ids)
    # ... nor makes a group a child of itself, which is what `_take_place` did while the group's
    # `children` was the very list the op still held: it put the new id wherever the old one stood,
    # its own children among them, and the op said so out loud (`group [a, g] as g`).
    one = {"slides": [slide("b2s_s000", "a\n")]}
    one["slides"][0]["objects"]["b2s_s000_u"] = fuzz_world.readback("shape", [0, 50, 100, 90], text="b\n")
    one["slides"][0]["order"].append("b2s_s000_u")
    fuzz_sync.deck_group(Stuck(), {"slides": []}, one)
    gid, rb = next((o, r) for o, r in one["slides"][0]["objects"].items() if r["kind"] == "elementGroup")
    assert sorted(rb["children"]) == ["b2s_s000_t", "b2s_s000_u"] and gid not in rb["children"]
    # and the campaign says so out loud rather than blaming the merge for what it did itself
    live["slides"].append(copy.deepcopy(live["slides"][0]))
    with pytest.raises(AssertionError, match="two things at once"):
        fuzz_sync._sync_step(3, 0, {}, {"slides": []}, live, Path("."), [], [], False)


def test_the_rounds_really_exercise_sync():
    """A guard on the harness: a round that edited nothing would pass the oracle for free."""
    result = fuzz_sync.offline_round(3, source_ops=["reword", "move_element"],
                                     deck_ops=["add_text_box", "reword", "add_slide"])
    report = result["state"]["report"]
    assert report["applied"] and report["overrides"] and report["user_objects"]
    assert any("not applicable" not in d for d in result["deck"])


# ---------------------------------------------------------------- the oracle, on a clean round

@pytest.fixture(scope="module")
def clean():
    """A round with a user object, a user slide, a reworded converter text and a moved element."""
    result = fuzz_sync.offline_round(3, source_ops=["reword", "move_element"],
                                     deck_ops=["add_text_box", "reword", "add_slide"])
    assert not result["failures"], loss_oracle.describe(result["failures"])
    return result["state"]


def checked(state, **changed):
    st = {**state, **changed}
    return loss_oracle.check(st["base"], st["before"], st["after"], st["report"], st["ours"])


def kinds(findings):
    return {f["kind"] for f in findings}


def slide_of(read, oid):
    return next(s for s in read["slides"] if s["objectId"] == oid)


def user_object(state):
    """(slide id, object id) of an object the person added."""
    entry = state["report"]["user_objects"][0]
    sid = next(s["objectId"] for s in state["base"]["slides"] if s["key"] == entry["slide"])
    return sid, entry["objectId"]


def typed_word(state):
    """(slide id, object id, word) a person typed into a converter object."""
    for b in state["base"]["slides"]:
        bs = slide_of(state["before"], b["objectId"])
        for el in b["elements"]:
            main = el.get("main")
            was, now = (el.get("readback") or {}).get(main), bs["objects"].get(main)
            if not main or was is None or now is None:
                continue
            old = loss_oracle.words(was.get("text"))
            for hunk in loss_oracle.hunks(was.get("text"), now.get("text")):
                for word in sorted(loss_oracle.words("".join(hunk[2])) - old):
                    return b["objectId"], main, word
    raise AssertionError("the round has no deck word edit")


def test_a_clean_round_is_clean(clean):
    assert loss_oracle.check(clean["base"], clean["before"], clean["after"], clean["report"], clean["ours"]) == []


def test_a_sync_that_wrote_nothing_is_clean(clean):
    """The control in the other direction: leaving the deck exactly as it was loses nothing."""
    assert loss_oracle.check(clean["base"], clean["before"], copy.deepcopy(clean["before"]), None, clean["ours"]) == []


# ---------------------------------------------------------------- losses injected on purpose

def test_catches_a_dropped_user_object(clean):
    sid, oid = user_object(clean)
    after = copy.deepcopy(clean["after"])
    slide_of(after, sid)["objects"].pop(oid)
    found = checked(clean, after=after)
    assert "user_object_deleted" in kinds(found) and any(f["object"] == oid for f in found)


def test_catches_a_user_object_put_back_where_it_was(clean):
    sid, oid = user_object(clean)
    after = copy.deepcopy(clean["after"])
    rb = slide_of(after, sid)["objects"][oid]
    rb["box"] = [v + 30 for v in rb["box"]]
    rb["transform"] = [*rb["transform"][:4], rb["transform"][4] + 30, rb["transform"][5] + 30]
    assert "user_object_moved" in kinds(checked(clean, after=after))


def test_catches_a_user_object_taken_out_of_a_group_that_is_still_there(clean):
    """The person's object leaving a group is their work undone - unless the group itself is gone,
    which Slides does on its own once a group is down to one child."""
    sid, oid = user_object(clean)
    after = copy.deepcopy(clean["after"])
    objects = slide_of(after, sid)["objects"]
    objects["user_group"] = {"kind": "elementGroup", "children": [oid, "something_else"], "box": [0, 0, 10, 10],
                             "transform": [1, 0, 0, 1, 0, 0], "parent_group": None}
    objects[oid]["parent_group"] = "user_group"
    assert "user_object_regrouped" in kinds(checked(clean, after=after))
    before = copy.deepcopy(clean["before"])
    slide_of(before, sid)["objects"][oid]["parent_group"] = "gone_group"  # the group Slides dropped
    assert "user_object_regrouped" not in kinds(checked(clean, before=before))


def test_catches_a_swallowed_word(clean):
    """The person's word is nowhere on the slide afterwards and no conflict mentions it."""
    sid, oid, word = typed_word(clean)
    after = copy.deepcopy(clean["after"])
    for rb in slide_of(after, sid)["objects"].values():
        if rb.get("text"):
            rb["text"] = re.sub(word, "", rb["text"], flags=re.IGNORECASE)
    found = kinds(checked(clean, after=after))
    assert found & {"word_lost", "overwritten_unreported"}


def test_a_swallowed_word_reproduced_in_a_conflict_is_accounted_for(clean):
    """... unless the report shows the author what was replaced: then it is a reported conflict."""
    sid, oid, word = typed_word(clean)
    after = copy.deepcopy(clean["after"])
    for rb in slide_of(after, sid)["objects"].values():
        if rb.get("text"):
            rb["text"] = re.sub(word, "", rb["text"], flags=re.IGNORECASE)
    skey = next(s["key"] for s in clean["base"]["slides"] if s["objectId"] == sid)
    report = copy.deepcopy(clean["report"])
    report["conflicts"].append({"slide": skey, "element": None, "field": "text", "theirs": f"...{word}...",
                                "resolution": "source kept"})
    assert not (kinds(checked(clean, after=after, report=report)) & {"word_lost", "overwritten_unreported"})


def test_catches_a_silently_deleted_slide(clean):
    """A converter slide the report doesn't list, and a slide the person added (never allowed)."""
    after = copy.deepcopy(clean["after"])
    after["slides"] = [s for s in after["slides"] if s["objectId"] != clean["base"]["slides"][1]["objectId"]]
    assert "slide_deleted_unreported" in kinds(checked(clean, after=after))
    user_slide = clean["report"]["slides"]["user_added"][0]["objectId"]
    after = copy.deepcopy(clean["after"])
    after["slides"] = [s for s in after["slides"] if s["objectId"] != user_slide]
    report = copy.deepcopy(clean["report"])
    report["slides"]["deleted"] = [user_slide]  # even claiming it doesn't help
    assert "user_slide_deleted" in kinds(checked(clean, after=after, report=report))


def test_catches_a_slide_quietly_put_back_in_another_place(clean):
    after = copy.deepcopy(clean["after"])
    after["slides"] = [after["slides"][2], *after["slides"][:2], *after["slides"][3:]]
    assert "slide_moved_unreported" in kinds(checked(clean, after=after))


def test_catches_a_converter_object_deleted_without_a_word_in_the_report(clean):
    """The source still draws it, the report is silent, the object is gone."""
    b = clean["base"]["slides"][1]
    el = next(e for e in b["elements"] if e.get("main"))
    after = copy.deepcopy(clean["after"])
    for oid in el["objects"]:
        slide_of(after, b["objectId"])["objects"].pop(oid, None)
    found = kinds(checked(clean, after=after))
    assert "object_deleted_unreported" in found or "element_vanished" in found


def test_catches_a_report_that_claims_work_it_did_not_do(clean):
    """`applied` on an element nothing happened to: a lying report hides the real writes."""
    b = clean["base"]["slides"][1]
    el = next(e for e in b["elements"] if e.get("main"))
    report = copy.deepcopy(clean["report"])
    report["applied"].append({"slide": b["key"], "element": el["key"], "fields": ["text"]})
    found = checked(clean, report=report)
    assert "applied_no_change" in kinds(found)


def test_catches_a_converged_claim_on_an_element_that_changed(clean):
    """`converged` means nothing was written; an element that changed contradicts it."""
    entry = clean["report"]["applied"][0]
    report = copy.deepcopy(clean["report"])
    report["applied"] = [e for e in report["applied"]
                         if (e.get("slide"), e.get("element")) != (entry["slide"], entry["element"])]
    report["converged"].append({"slide": entry["slide"], "element": entry["element"], "field": "text"})
    assert "converged_but_changed" in kinds(checked(clean, report=report))


def test_catches_notes_and_a_background_the_person_set_being_overwritten(clean):
    after = copy.deepcopy(clean["after"])
    before = copy.deepcopy(clean["before"])
    sid = clean["base"]["slides"][0]["objectId"]
    slide_of(before, sid)["notes"] = "remember the funding slide"
    slide_of(after, sid)["notes"] = ""
    slide_of(before, sid)["background"] = {"state": "RENDERED", "solid": "#102030"}
    slide_of(after, sid)["background"] = {"state": "RENDERED", "solid": "#ffffff"}
    found = kinds(checked(clean, before=before, after=after))
    assert "notes_word_lost" in found and "background_lost" in found


def test_catches_the_notes_of_a_slide_the_person_added_being_dropped():
    """A slide the base never saw has nothing to merge against: its notes and its background must
    come through untouched, whatever the report says."""
    base = {"slides": [{"key": "f1", "objectId": "s1", "elements": []}]}
    before = {"slides": [{"objectId": "s1", "objects": {}}, {"objectId": "mine", "objects": {},
                                                            "notes": "ask about the budget", "background": {"solid": "#102030"}}]}
    after = copy.deepcopy(before)
    after["slides"][1]["notes"] = ""
    found = [f["kind"] for f in loss_oracle.user_slide_findings(base, before, after)]
    assert set(found) == {"notes_word_lost"} and len(found) == 4  # one per word of the note
    after["slides"][1]["background"] = {"solid": "#ffffff"}
    assert "background_lost" in [f["kind"] for f in loss_oracle.user_slide_findings(base, before, after)]
    assert loss_oracle.user_slide_findings(base, before, before) == []


# ---------------------------------------------------------------- oracle units

def test_normalise_report_reads_both_shapes():
    nested = loss_oracle.normalise_report({"slides": {"deleted": ["a"], "moved": ["b"]}})
    flat = loss_oracle.normalise_report({"slides_deleted": ["a"], "slides_moved": ["b"]})
    assert nested["slides_deleted"] == flat["slides_deleted"] == ["a"]
    assert nested["slides_moved"] == flat["slides_moved"] == ["b"]
    assert loss_oracle.normalise_report(None)["conflicts"] == []


def test_hunks_are_the_words_the_person_added():
    got = loss_oracle.hunks("we measured the speed", "we measured the raw speed")
    assert [w for h in got for w in loss_oracle.words("".join(h[2]))] == ["raw"]


def test_words_split_like_merge_tokens():
    """The oracle has to split exactly like the merge, or it invents losses on hyphens."""
    from beamer2slides import merge
    text = "figure-theirs, 3rd try"
    assert loss_oracle.words(text) == {w.lower() for w in merge.tokens(text) if w.strip() and w.isalnum()}


def test_element_objects_finds_created_and_tagged_objects():
    skey, ekey = "results", "text/body/0"
    made = f"b2s_{loss_oracle.h6(skey)}_{loss_oracle.h6(ekey)}_7f"
    read = {"objects": {"old": {"title": None}, made: {"title": None},
                        "adopted": {"title": snapshot.tag(skey, ekey)}, "other": {"title": "b2s:results/text/body/1"}}}
    base_el = {"objects": ["old"]}
    assert loss_oracle.element_objects(skey, ekey, base_el, read) == {"old", made, "adopted"}


def test_catches_a_picture_the_person_chose_being_overwritten():
    """Compared by pixel signature: Google hands out a new contentUrl for an unchanged picture."""
    from beamer2slides.devtools import fuzz_world as W
    mine = {"contentHash": "m", "signature": W.picture_signature(b"person picture")}
    theirs = {"contentHash": "c", "signature": W.picture_signature(b"source picture")}
    base = {"slides": [{"key": "f1", "objectId": "s1", "elements": [
        {"key": "image/figure/0", "main": "o", "objects": ["o"], "readback": {"o": {"image": theirs}}}]}]}
    before = {"slides": [{"objectId": "s1", "objects": {"o": {"image": mine}}}]}
    after = {"slides": [{"objectId": "s1", "objects": {"o": {"image": theirs}}}]}
    empty = loss_oracle.normalise_report({})
    assert [f["kind"] for f in loss_oracle.picture_findings(base, before, after, empty)] == ["picture_reverted"]
    kept = {"slides": [{"objectId": "s1", "objects": {"o": {"image": mine}}}]}
    assert loss_oracle.picture_findings(base, before, kept, empty) == []
    said = loss_oracle.normalise_report({"conflicts": [{"slide": "f1", "element": "image/figure/0", "field": "image"}]})
    assert loss_oracle.picture_findings(base, before, after, said) == []


def test_catches_an_element_put_back_at_the_converters_box():
    """The person moved the element, the source moved it too, and the sync left it at the box the
    base recorded. `deck_placement` says where the rewritten element belongs - the conversion's new
    box with the person's step on it - so the box it *would* have had is no excuse for any other."""
    base = {"slides": [{"key": "f1", "objectId": "s1", "elements": [
        {"key": "text/body/0", "main": "o", "objects": ["o"], "fingerprint": {"bbox": [10, 10, 60, 30]},
         "readback": {"o": {"box": [20, 20, 120, 60], "transform": [2, 0, 0, 2, 20, 20]}}},
        {"key": "text/title/0", "main": "t", "objects": ["t"], "fingerprint": {"bbox": [10, 5, 110, 15]},
         "readback": {"t": {"box": [20, 10, 220, 30], "transform": [2, 0, 0, 2, 20, 10]}}},
        {"key": "image/figure/0", "main": "f", "objects": ["f"], "fingerprint": {"bbox": [70, 40, 120, 90]},
         "readback": {"f": {"box": [140, 80, 240, 180], "transform": [2, 0, 0, 2, 140, 80]}}}]}]}
    moved = {"box": [30, 0, 130, 40], "transform": [2, 0, 0, 2, 30, 0]}   # the person moved it (+10, -20)
    before = {"slides": [{"objectId": "s1", "objects": {"o": moved}}]}
    after = {"slides": [{"objectId": "s1", "objects": {"o": dict(base["slides"][0]["elements"][0]["readback"]["o"])}}]}
    empty = loss_oracle.normalise_report({})
    assert loss_oracle.deck_placement(base) == (2.0, 0.0, 0.0)
    # The source left the element where it was: the base's box is the converter's, plainly a revert.
    still = {"slides": [{"key": "f1", "elements": [{"key": "text/body/0", "fingerprint": {"bbox": [10, 10, 60, 30]}}]}]}
    assert [f["kind"] for f in loss_oracle.geometry_findings(base, before, after, empty, still)] == ["geometry_reverted"]
    # The source moved it to where the person's step lands it on the old box: nothing was reverted.
    there = {"slides": [{"key": "f1", "elements": [{"key": "text/body/0", "fingerprint": {"bbox": [5, 20, 55, 40]}}]}]}
    assert loss_oracle.geometry_findings(base, before, after, empty, there) == []
    # ... but the person's step still has to be on top of it.
    assert [f["kind"] for f in loss_oracle.geometry_findings(base, before, after, empty, None)] == ["geometry_reverted"]
    # A conflict excuses it - except the one that promises the person's move was carried onto the
    # source's new place (both moved it): that is a promise to keep, not an excuse.
    def said(resolution):
        return loss_oracle.normalise_report({"conflicts": [
            {"slide": "f1", "element": "text/body/0", "field": "geometry", "resolution": resolution}]})
    assert loss_oracle.geometry_findings(base, before, after, said("deck kept"), still) == []
    assert [f["kind"] for f in loss_oracle.geometry_findings(base, before, after, said(merge.GEOMETRY_CARRIED), None)] \
        == ["geometry_reverted"]
    # ... and it says where: the person's corner plus the source's move of the IR corner.
    promised = said(merge.GEOMETRY_CARRIED)
    assert loss_oracle.geometry_findings(base, before, after, promised, there) == []   # (-10, +20) from (30, 0)
    assert [f["kind"] for f in loss_oracle.geometry_findings(base, before, after, promised, still)] \
        == ["geometry_not_carried"]
    # the deck's absolute place, which geometry mode `theirs` wrote, is not what was promised either
    pinned = {"slides": [{"objectId": "s1", "objects": {"o": dict(moved)}}]}
    assert [f["kind"] for f in loss_oracle.geometry_findings(base, before, pinned, promised, there)] \
        == ["geometry_not_carried"]
    assert loss_oracle.geometry_findings(base, before, pinned, empty, there) == []


def test_catches_a_resize_dropped_where_the_source_moved_the_element():
    """layout-grown-box-moved: the person made a box taller, the source moved it down, and the sync
    put it at the source's new place with the converter's height. Its place is not the base's, so the
    place check alone is silent; the size is the converter's again."""
    base = {"slides": [{"key": "f1", "objectId": "s1", "elements": [
        {"key": "text/body/0", "main": "o", "objects": ["o"], "fingerprint": {"bbox": [10, 10, 110, 40]},
         "readback": {"o": {"box": [10, 10, 110, 40], "transform": [1, 0, 0, 1, 10, 10]}}}]}]}
    taller = {"box": [10, 10, 110, 55], "transform": [1, 0, 0, 1.5, 10, 10]}
    before = {"slides": [{"objectId": "s1", "objects": {"o": taller}}]}
    ours = {"slides": [{"key": "f1", "elements": [{"key": "text/body/0", "fingerprint": {"bbox": [10, 41, 110, 71]}}]}]}
    report = loss_oracle.normalise_report({"conflicts": [
        {"slide": "f1", "element": "text/body/0", "field": "geometry", "resolution": merge.GEOMETRY_CARRIED}]})

    def after(box):
        return {"slides": [{"objectId": "s1", "objects": {"new": {"box": box, "transform": [1, 0, 0, 1, box[0], box[1]],
                                                                  "title": snapshot.tag("f1", "text/body/0")}}}]}
    dropped = loss_oracle.geometry_findings(base, before, after([10, 41, 110, 71]), report, ours)
    assert [f["kind"] for f in dropped] == ["geometry_reverted"] and "size" in dropped[0]["detail"]
    assert loss_oracle.geometry_findings(base, before, after([10, 41, 110, 86]), report, ours) == []
    # the source resized it too: nothing to hold the converter's size against
    grown = {"slides": [{"key": "f1", "elements": [{"key": "text/body/0", "fingerprint": {"bbox": [10, 41, 110, 90]}}]}]}
    assert loss_oracle.geometry_findings(base, before, after([10, 41, 110, 71]), report, grown) == []


def test_catches_styling_put_back_the_way_the_converter_had_it():
    base = {"slides": [{"key": "f1", "objectId": "s1", "elements": [
        {"key": "text/body/0", "main": "o", "objects": ["o"], "readback": {"o": {"text_style_hash": "converter"}}}]}]}
    before = {"slides": [{"objectId": "s1", "objects": {"o": {"text_style_hash": "person"}}}]}
    after = {"slides": [{"objectId": "s1", "objects": {"o": {"text_style_hash": "converter"}}}]}
    empty = loss_oracle.normalise_report({})
    assert [f["kind"] for f in loss_oracle.style_findings(base, before, after, empty)] == ["style_reverted"]
    kept = {"slides": [{"objectId": "s1", "objects": {"o": {"text_style_hash": "person"}}}]}
    assert loss_oracle.style_findings(base, before, kept, empty) == []
    # An `overrides` entry claims the deck's styling was kept, so it excuses nothing: only a
    # conflict (the report saying the styling gave way) accounts for the loss.
    claimed = loss_oracle.normalise_report({"overrides": [{"slide": "f1", "element": "text/body/0", "fields": ["text_style"]}]})
    assert [f["kind"] for f in loss_oracle.style_findings(base, before, after, claimed)] == ["style_reverted"]
    said = loss_oracle.normalise_report({"conflicts": [{"slide": "f1", "element": "text/body/0", "field": "text_style"}]})
    assert loss_oracle.style_findings(base, before, after, said) == []


def test_a_reported_swap_does_not_accuse_the_slides_between_it():
    """Two slides swapping puts a third at the same index with other neighbours: the order is
    accounted for when taking the reported moves out of both orders leaves the same sequence."""
    base = {"slides": [{"key": f"k{i}", "objectId": f"s{i}", "elements": []} for i in range(5)]}
    read = lambda order: {"slides": [{"objectId": s, "objects": {}} for s in order]}  # noqa: E731
    before, after = read(["s0", "s1", "s2", "s3", "s4"]), read(["s0", "s3", "s2", "s1", "s4"])
    assert loss_oracle.order_findings(base, before, after, loss_oracle.normalise_report(
        {"slides": {"moved": ["k1", "k3"]}})) == []
    half = loss_oracle.order_findings(base, before, after, loss_oracle.normalise_report({"slides": {"moved": ["k1"]}}))
    assert [f["kind"] for f in half] == ["slide_moved_unreported"]


def test_a_copy_travelling_with_the_slide_it_follows_accuses_nobody():
    """Live round 303: the person duplicated a slide, the source moved the original, and sync moved
    the copy along behind it. Which of two slides that changed places "moved" has no single answer,
    so the report naming the source's own move has to be enough - the slide it passed did not move
    by itself, and neither did the copy that rode along with it."""
    base = {"slides": [{"key": f"k{i}", "objectId": f"s{i}", "elements": []} for i in range(4)]}
    read = lambda order: {"slides": [{"objectId": s, "objects": {}} for s in order]}  # noqa: E731
    # k2 moves up past k1; "u", the person's copy of k2, keeps sitting behind it.
    before, after = read(["s0", "s1", "s2", "u", "s3"]), read(["s0", "s2", "u", "s1", "s3"])
    rep = loss_oracle.normalise_report({"slides": {"moved": ["k2"]}})
    assert loss_oracle.order_findings(base, before, after, rep) == []
    # A copy that went somewhere of its own, behind a slide it never followed, is still caught.
    away = read(["u", "s0", "s1", "s2", "s3"])
    assert [f["kind"] for f in loss_oracle.order_findings(base, before, away, rep)] == ["user_slide_moved"]


def test_a_word_the_source_put_in_another_element_is_no_undone_deletion():
    el, was, now = {"key": "text/body/1", "main": "o"}, {"text": "the author wrote this"}, {"text": "the wrote this"}
    args = dict(before_words=loss_oracle.words(now["text"]), after_words={"the", "wrote", "this", "author", "retitled"},
                conflicts=[], ours_el=None)
    # 'author' is back on the slide, but in the retitled frame title, not where the person deleted it
    assert loss_oracle.word_findings("f4", el, was, now, after_el_words={"the", "wrote", "this"}, **args) == []
    back = loss_oracle.word_findings("f4", el, was, now, after_el_words={"the", "author", "wrote", "this"}, **args)
    assert [f["kind"] for f in back] == ["deletion_undone"]


def _occlusion_world():
    """A slide with a text object `t` on an opaque block panel `p` (both in the block group `g`,
    panel first) and a user text box `u` elsewhere, as the read-back before a sync."""
    fill = {"fill": {"color": "#dde4f0", "alpha": 1.0}}
    objects = {
        "g": {"kind": "elementGroup", "box": [50, 100, 400, 156], "children": ["p", "t"]},
        "p": {"kind": "shape", "box": [50, 100, 400, 156], "text": "", "parent_group": "g", "shape_style": fill},
        "t": {"kind": "shape", "box": [60, 116, 390, 140], "text": "words the person edited\n", "parent_group": "g",
              "shape_style": {"fill": None}},
        "u": {"kind": "shape", "box": [300, 300, 460, 330], "text": "a note of the person's\n",
              "shape_style": {"fill": None}},
    }
    base = {"slides": [{"key": "f1", "objectId": "s1", "groups": ["g"], "elements": [
        {"key": "shape/panel/0", "main": "p", "objects": ["p"]},
        {"key": "text/body/0", "main": "t", "objects": ["t"]}]}]}
    before = {"slides": [{"objectId": "s1", "order": ["g", "u"], "objects": objects}]}
    return base, before


def test_catches_text_hidden_under_a_shape_the_sync_created():
    """Nothing is deleted when a sync stacks a new panel over the words on it, so every other check
    passes: the text is still there, just unreadable (a live sync did this to a block the person had
    edited, dc8523a). The rebuilt panel has to go where the old one was, under the text."""
    base, before = _occlusion_world()
    new = f"b2s_{loss_oracle.h6('f1')}_{loss_oracle.h6('shape/panel/0')}_1zz"

    def synced(children):
        after = copy.deepcopy(before)
        objects = after["slides"][0]["objects"]
        objects[new] = {**objects.pop("p"), "box": [50, 96, 400, 160]}  # the source resized the panel
        objects["g"]["children"] = children
        return after
    ours_order = {"slides": [{"key": "f1", "elements": [{"key": "shape/panel/0"}, {"key": "text/body/0"}]}]}
    found = loss_oracle.occlusion_findings(base, before, synced(["t", new]), ours_order)
    assert [(f["kind"], f["object"], f["element"]) for f in found] == [("text_hidden", "t", "text/body/0")]
    assert "loss" in {f["severity"] for f in found}
    assert loss_oracle.occlusion_findings(base, before, synced([new, "t"]), ours_order) == []   # the fix
    # A text the sync rebuilt as well counts through its element (its new object is not `t`).
    after = synced(["t", new])
    text = f"b2s_{loss_oracle.h6('f1')}_{loss_oracle.h6('text/body/0')}_1zz"
    objs = after["slides"][0]["objects"]
    objs[text] = objs.pop("t")
    objs["g"]["children"] = [text, new]
    assert [f["object"] for f in loss_oracle.occlusion_findings(base, before, after, ours_order)] == [text]
    # The new conversion stacking the shape above that text itself: the sync kept the source's order.
    above = {"slides": [{"key": "f1", "elements": [{"key": "text/body/0"}, {"key": "shape/panel/0"}]}]}
    assert loss_oracle.occlusion_findings(base, before, synced(["t", new]), above) == []


def test_the_fuzz_world_hands_the_merge_everything_sync_does_about_identity(tmp_path):
    """`fuzz_world.build_ours` is a copy of `sync.build_ours`, and a copy drifts. It left out
    `weak_pairs` and `near_misses` - the two things `merge.plan_merge` turns into its warnings
    about which frame is which - so every campaign round ran against a world where the person is
    never told the pairing was a guess, and a frame put on a look-alike slide read as a loss in
    silence. What sync hands the merge, the harness hands it too."""
    import inspect
    import random

    from beamer2slides import sync
    from beamer2slides.devtools import fuzz_world as W

    said = inspect.getsource(sync.build_ours)
    wanted = {k for k in ("pairs", "label_moves", "weak_pairs", "near_misses") if f'"{k}"' in said}
    assert wanted == {"pairs", "label_moves", "weak_pairs", "near_misses"}, "sync stopped saying one"
    doc = W.make("adopt", random.Random(7), tmp_path)
    ours = W.build_ours(doc, W.build_base(doc, tmp_path), tmp_path)
    assert wanted <= set(ours), f"the harness says less than sync does: {wanted - set(ours)}"


def test_hidden_text_on_a_slide_the_report_calls_uncertain_is_a_note():
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
    after["slides"][0]["objects"][new] = {**before["slides"][0]["objects"]["p"], "parent_group": "g"}
    after["slides"][0]["objects"]["g"]["children"] = ["t", new]
    ours = {"slides": [{"key": "f1", "elements": [{"key": "shape/panel/0"}, {"key": "text/body/0"}]}]}
    assert [f["severity"] for f in loss_oracle.occlusion_findings(base, before, after, ours)] == ["loss"]

    # a label the content says is on another frame now: the report carries a conflict per slide
    moved = {**ours, "label_moves": [{"label": "x", "verdict": "unsure", "slide": "f1", "frame_is": None}]}
    found = loss_oracle.occlusion_findings(base, before, after, moved)
    assert [f["severity"] for f in found] == ["note"] and "may be the wrong one" in found[0]["detail"]
    # ... and a pairing the words could as well have made next door, for an unlabelled frame
    twins = {**ours, "pairs": {0: 0}, "weak_pairs": {0: "twins"}}
    assert [f["severity"] for f in loss_oracle.occlusion_findings(base, before, after, twins)] == ["note"]
    # the same frame with a label of its own is not what `merge.plan_merge` warns about
    labelled = {"slides": [{**ours["slides"][0], "label": "f1"}], "pairs": {0: 0}, "weak_pairs": {0: "twins"}}
    assert [f["severity"] for f in loss_oracle.occlusion_findings(base, before, after, labelled)] == ["loss"]


def _folded_world():
    """The person folded a converter panel into a group of their own, beside their own heading; the
    body text it belongs with stays in the converter's block group, below that group on the page."""
    opaque = {"fill": {"color": "#dddddd", "alpha": 1.0}}
    objects = {
        "g": {"kind": "elementGroup", "box": [80, 328, 430, 384], "children": ["t"]},
        "t": {"kind": "shape", "box": [90, 344, 420, 368], "text": "bullet editor picture table\n",
              "parent_group": "g", "shape_style": {"fill": None}},
        "ug": {"kind": "elementGroup", "box": [40, 40, 400, 332], "children": ["mine", "p"]},
        "mine": {"kind": "shape", "box": [40, 40, 256, 68], "text": "the person's own heading\n",
                 "parent_group": "ug", "shape_style": {"fill": None}},
        "p": {"kind": "shape", "box": [50, 272, 400, 332], "text": "", "parent_group": "ug", "shape_style": opaque},
    }
    base = {"slides": [{"key": "f1", "objectId": "s1", "groups": ["g"], "elements": [
        {"key": "shape/panel/0", "main": "p", "objects": ["p"]},
        {"key": "text/body/0", "main": "t", "objects": ["t"]}]}]}
    before = {"slides": [{"objectId": "s1", "order": ["g", "ug"], "objects": objects}]}
    return base, before


def test_a_shape_in_a_group_the_person_made_is_a_note_not_a_loss():
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
    objs = after["slides"][0]["objects"]
    objs[new] = {**objs.pop("p"), "box": [50, 272, 370, 352]}   # the source redrew it taller
    objs["ug"]["children"] = ["mine", new]
    ours = {"slides": [{"key": "f1", "elements": [{"key": "shape/panel/0"}, {"key": "text/body/0"}]}]}
    found = loss_oracle.occlusion_findings(base, before, after, ours)
    assert [(f["kind"], f["severity"], f["object"]) for f in found] == [("text_hidden", "note", "t")]
    assert "in a group the person made" in found[0]["detail"]
    # ... and the very same panel standing on the page itself is a loss: `restack` could order it.
    page = copy.deepcopy(after)
    page["slides"][0]["order"] = ["g", new]
    page["slides"][0]["objects"][new]["parent_group"] = None
    del page["slides"][0]["objects"]["ug"], page["slides"][0]["objects"]["mine"]
    assert [f["severity"] for f in loss_oracle.occlusion_findings(base, before, page, ours)] == ["loss"]


def test_a_shape_the_source_itself_added_above_the_text_is_no_finding():
    """The same excuse for an element the *source added*: it has no base element, so an oracle that
    named objects by the base alone could not tell which shape it was looking at - and
    `_source_stacks_above` can excuse nothing it cannot name. Every adopt-shaped round where a new
    source drew a panel over its own text was reported as a loss."""
    base, before = _occlusion_world()
    added = f"b2s_{loss_oracle.h6('f1')}_{loss_oracle.h6('shape/panel/1')}_1zz"
    after = copy.deepcopy(before)
    objs = after["slides"][0]["objects"]
    objs[added] = {**objs["p"], "box": [50, 96, 400, 160], "parent_group": None}
    after["slides"][0]["order"].append(added)
    els = [{"key": "shape/panel/0"}, {"key": "text/body/0"}, {"key": "shape/panel/1"}]
    assert loss_oracle.occlusion_findings(base, before, after, {"slides": [{"key": "f1", "elements": els}]}) == []
    below = [els[0], els[2], els[1]]   # ... and it is a loss again when the source draws it under
    found = loss_oracle.occlusion_findings(base, before, after, {"slides": [{"key": "f1", "elements": below}]})
    assert [(f["kind"], f["object"]) for f in found] == [("text_hidden", "t")]


def test_text_already_hidden_or_under_something_else_is_no_finding():
    base, before = _occlusion_world()
    new = "b2s_000000_111111_1zz"
    opaque = {"kind": "shape", "text": "", "shape_style": {"fill": {"color": "#000000", "alpha": 1.0}}}
    # the person had already put an opaque shape over their own note: nothing readable to lose
    covered = copy.deepcopy(before)
    covered["slides"][0]["objects"]["mine"] = {**opaque, "box": [290, 290, 470, 340]}
    covered["slides"][0]["order"].append("mine")
    after = copy.deepcopy(covered)
    after["slides"][0]["objects"][new] = {**opaque, "box": [290, 290, 470, 340]}
    after["slides"][0]["order"].append(new)
    assert loss_oracle.occlusion_findings(base, covered, after) == []
    # ... but on the readable slide the same new shape hides the note
    after = copy.deepcopy(before)
    after["slides"][0]["objects"][new] = {**opaque, "box": [290, 290, 470, 340]}
    after["slides"][0]["order"].append(new)
    assert [f["object"] for f in loss_oracle.occlusion_findings(base, before, after)] == ["u"]
    # a see-through fill, a corner overlap and a shape below it hide nothing
    for rb, where in (({**opaque, "shape_style": {"fill": {"color": "#000000", "alpha": 0.5}}, "box": [290, 290, 470, 340]}, "top"),
                      ({**opaque, "box": [440, 320, 520, 400]}, "top"),
                      ({**opaque, "box": [290, 290, 470, 340]}, "bottom")):
        after = copy.deepcopy(before)
        after["slides"][0]["objects"][new] = rb
        order = after["slides"][0]["order"]
        order.insert(0, new) if where == "bottom" else order.append(new)
        assert loss_oracle.occlusion_findings(base, before, after) == [], rb


def test_the_offline_fuzz_stacks_a_rebuilt_block_as_sync_does(monkeypatch):
    """`fuzz_sync._stacked` replays `sync.Sync.regroup_requests` through Slides' z-order rules (a
    group keeps the page order its children had), so taking the restack out of sync - which is what
    hid a block's body text under its rebuilt panel live - fails the offline fuzz: the source resizes
    a block's panel, sync rebuilds it, and the text it kept is under it. With the restack, clean."""
    from beamer2slides.sync import Sync
    ops = dict(source_ops=["resize_element"], deck_ops=["edit_cell"])   # seed 373, as the fuzz shrank it
    result = fuzz_sync.offline_round(373, **ops)
    assert not result["failures"], loss_oracle.describe(result["failures"])
    assert any("resize p0k0" in s for s in result["source"])   # (p<page>k0: a block's panel)
    kept = Sync.regroup_requests

    def unstacked(*args):
        return [r for r in kept(*args) if "updatePageElementsZOrder" not in r]
    monkeypatch.setattr(Sync, "regroup_requests", staticmethod(unstacked))
    found = fuzz_sync.offline_round(373, **ops)["failures"]
    assert [f["kind"] for f in found] == ["text_hidden"], loss_oracle.describe(found)
    assert "regroup_requests" in found[0]["detail"]
    # and over the default rounds too, not only on a seed picked for it
    assert any(fuzz_sync.offline_round(seed)["failures"] for seed in (156, 250, 263, 290, 349))


def test_the_offline_fuzz_orders_the_page_as_sync_does(monkeypatch):
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
    assert not fuzz_sync.offline_chain(19, 6)["failures"]
    monkeypatch.setattr(Sync, "restack", lambda self, w, before, now: [])
    found = fuzz_sync.offline_chain(19, 6)["failures"]
    assert [f["kind"] for f in found] == ["text_hidden"], loss_oracle.describe(found)


def test_failures_are_the_severities_that_matter():
    made = [loss_oracle.finding("x", "note", "unverified"), loss_oracle.finding("y", "loss", "gone")]
    assert [f["kind"] for f in loss_oracle.failures(made)] == ["y"]
    assert "gone" in loss_oracle.describe(made)


# ---------------------------------------------------------------- live campaign (opt-in)

@pytest.mark.sync
def test_live_fuzz_rounds():
    """Real conversions, real Slides edits, real syncs. Decks of passing rounds are deleted."""
    rounds = int(os.environ.get("B2S_FUZZ_LIVE_ROUNDS", "3"))
    out = Path(os.environ.get("B2S_FUZZ_OUT", ROOT / "out" / "sync-fuzz"))
    bad = fuzz_sync.run_live(rounds, int(os.environ.get("B2S_FUZZ_LIVE_SEED", "0")), 3,
                             int(os.environ.get("B2S_FUZZ_LIVE_CHAIN", "1")), False, out, False)
    assert not bad, "\n".join(f"seed {r['seed']} ({r.get('deck')}): " + "; ".join(r["problems"]) for r in bad)
