"""Slides whose frames share a label (docs/sync.md, "Identity").

Every overlay step of a beamer frame carries the frame's label, so a deck converted with
`--overlays all` holds several slides under one label; a source can also reuse a label by hand.
Pairing them all with one base slide left the others unpaired, and an unpaired base slide is one
the source dropped - so a sync of the very same PDF planned to delete a step. Found on a live
deck on 2026-09-18.
"""

from beamer2slides import identity


def info(label, title, text, page):
    return {"label": label, "title": title, "text": text, "page": page}


def frame(label, step, page):
    """One overlay step: same label and title, a little more text than the step before."""
    return info(label, "Why decks and sources diverge", " ".join(["a bullet"] * (step + 1)), page)


def test_overlay_steps_of_one_frame_pair_step_by_step():
    base = [info("title", "A talk", "the title page", 0), frame("motivation", 0, 1), frame("motivation", 1, 2),
            info("conclusions", "Conclusions", "that is all", 3)]
    ours = [dict(b, page=b["page"]) for b in base]
    assert identity.align_slides(base, ours) == {0: 0, 1: 1, 2: 2, 3: 3}


def test_nothing_is_dropped_when_the_source_is_unchanged():
    base = [frame("motivation", 0, 0), frame("motivation", 1, 1), frame("motivation", 2, 2)]
    pairs = identity.align_slides(base, [dict(b) for b in base])
    assert sorted(pairs.values()) == [0, 1, 2]  # every base slide has a partner: nothing to delete


def test_a_step_added_to_the_frame_is_a_new_slide():
    base = [frame("motivation", 0, 0), frame("motivation", 1, 1)]
    ours = [frame("motivation", 0, 0), frame("motivation", 1, 1), frame("motivation", 2, 2)]
    pairs = identity.align_slides(base, ours)
    assert pairs[0] == 0 and pairs[1] == 1 and 2 not in pairs


def test_a_step_removed_leaves_one_base_slide_unpaired():
    base = [frame("motivation", 0, 0), frame("motivation", 1, 1), frame("motivation", 2, 2)]
    ours = [frame("motivation", 0, 0), frame("motivation", 1, 1)]
    pairs = identity.align_slides(base, ours)
    assert sorted(pairs.values()) == [0, 1]


def test_keys_stay_distinct_when_a_label_repeats():
    base = [frame("motivation", 0, 0), frame("motivation", 1, 1)]
    base_keys = ["motivation", "motivation~2"]
    keys, pairs = identity.inherit_slide_keys(base, base_keys, [dict(b) for b in base])
    assert keys == base_keys and pairs == {0: 0, 1: 1}


def test_labelled_frames_still_pair_wherever_they_moved():
    base = [info("intro", "Intro", "hello", 0), info("outro", "Outro", "bye", 1)]
    ours = [info("outro", "Outro", "bye", 0), info("intro", "Intro", "hello", 1)]
    assert identity.align_slides(base, ours) == {0: 1, 1: 0}
