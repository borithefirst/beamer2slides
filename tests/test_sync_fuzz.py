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
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fuzz_sync  # noqa: E402
import loss_oracle  # noqa: E402
from beamer2slides import snapshot  # noqa: E402

# Rounds of the default run; every seed is a different deck, edit set and source change.
ROUNDS = int(os.environ.get("B2S_FUZZ_ROUNDS", "40"))
# Seeds that once failed, kept as regressions: 252/430 merge's move shortcut moved nothing,
# 0/20 the harness and the report's per-unit keys, 3312/3799 two ways the oracle accused a
# blameless sync (a word the source put in another element, an ambiguous reported swap),
# 45/60/108/177 the base kept the slides the deck deleted, so a second sync brought them back.
REGRESSIONS = (0, 20, 45, 60, 108, 177, 252, 430, 3312, 3799)


# ---------------------------------------------------------------- the fuzz itself

@pytest.mark.parametrize("seed", [*range(ROUNDS), *REGRESSIONS])
def test_offline_round_loses_nothing(seed):
    result = fuzz_sync.offline_round(seed)
    if result["failures"]:
        small = fuzz_sync.shrink_offline(result)
        pytest.fail(f"seed {seed} lost something\n{loss_oracle.describe(small['failures'])}\n"
                    f"  deck:   {'; '.join(small['deck'])}\n  source: {'; '.join(small['source'])}")


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
    sys.path.insert(0, str(ROOT / "tools"))
    import fuzz_labels

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


def test_a_frame_the_source_moved_across_another_keeps_its_slide(tmp_path):
    """The other half of identity, on the same tagged truth: a frame that crossed another one.

    The alignment keeps the order, so of two frames that swapped only one stays in the chain; the
    other falls out and comes back as a new frame, and the slide it was made from is read as
    dropped - nobody's words are deleted, but the source writes them onto the slide next door.
    `identity.cross_pairs` pairs those leftovers when the content is unmistakable. Measured over
    3000 rounds: 5.53% -> 0.00% of the frames in sound rounds that moved one, and 19.15% -> 1.18%
    when a label was broken in the same round. The 300 seeds here hold that where it matters."""
    sys.path.insert(0, str(ROOT / "tools"))
    import fuzz_labels

    rounds = [r for seed in range(300) for r in fuzz_labels.round_once(seed, 0.5, tmp_path)]
    moved = [r for r in rounds if r["reordered"]]
    assert len(moved) >= 15, "the harness stopped moving frames, so this proves nothing"
    assert sum(r["wrong"]["order"] for r in moved) > 0, \
        "the order-keeping pass alone gets these right: the leftovers pass is not earning its place"
    assert sum(r["wrong"]["now"] for r in moved) == 0, \
        f"frames lost their slide: {[(r['seed'], r['wrong'], r['ops']) for r in moved if r['wrong']['now']]}"
    assert all(r["wrong"]["before"] <= r["wrong"]["order"] for r in rounds)  # never worse than the order alone


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
    import fuzz_world as W
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
