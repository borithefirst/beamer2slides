r"""A frame label that moved to another frame (identity.label_moves, docs/sync.md).

Nothing in the PDF says a label moved: `[label=mobile]` is simply on a different frame, and the
destination `mobile` lands on a different page. Following it carries a person's deck edits onto a
slide they never touched - and, on a live deck, brings the frame that lost the label back as a
duplicate. The only witness is what the slides on both sides say, so these tests are about the
line between "this label is somewhere else now" and the plausible edits that look like it.
"""

from beamer2slides import identity, merge


def info(title, text, label=None, page=0):
    return {"label": label, "title": title, "text": text, "page": page}


MOVING = "we keep the label on the frame it was written for whatever else changes"
ARRIVING = "a label that lands on another frame takes the deck slide with it"


def moved_pair():
    """The source moved `[label=mobile]` off "Moving labels" and onto the frame after it."""
    base = [info("Moving labels", MOVING, "mobile"), info("Arriving labels", ARRIVING)]
    ours = [info("Moving labels", MOVING), info("Arriving labels", ARRIVING, "mobile")]
    return base, ours


# ---------------------------------------------------------------- what is found

def test_a_label_moved_to_the_next_frame_is_seen():
    base, ours = moved_pair()
    (m,) = identity.label_moves(base, ours)
    assert m["verdict"] == "moved" and m["label"] == "mobile"
    assert m["base_title"] == "Moving labels" and m["ours_title"] == "Arriving labels"
    # both halves of the story: where the frame's words are in the base, where the slide's are now
    assert m["frame_is"] == 1 and m["slide_is"] == 0


def test_the_content_decides_and_nobody_moves_slide():
    """The point of finding it: the deck's slide keeps the frame whose words it holds. Following
    the label would have given "Arriving labels" the identity of the slide "Moving labels"."""
    base, ours = moved_pair()
    keys = identity.slide_keys(base)
    assert keys == ["mobile", "title:arriving labels#1"]
    got, pairs = identity.inherit_slide_keys(base, keys, ours)
    assert pairs == {0: 0, 1: 1} and got == ["mobile", "title:arriving labels#1"]


def test_without_the_check_the_label_would_carry_the_slide_away():
    """The mutation: pair by label alone, as sync did before this existed."""
    base, ours = moved_pair()
    assert identity.label_pairs(base, ours) == {1: 0}


def test_a_label_moves_while_every_title_in_the_deck_is_renamed():
    """What the live stress deck does (tests/decks/stress, variant `identity`): the source moves a
    label *and* retitles all 48 frames in the same version. A yes-or-no "same title?" says no to
    every pair at once, and the frame the label left is then no better an explanation than the one
    it landed on - so the title has to count by degree (`identity._title_alike`)."""
    base = [info("Moving labels", f"Moving labels {MOVING}", "mobile"),
            info("Arriving labels", f"Arriving labels {ARRIVING}", "arriving")]
    ours = [info("Moving labels v2", f"Moving labels v2 {MOVING}"),
            info("Arriving labels v2", f"Arriving labels v2 {ARRIVING}", "mobile")]
    (m,) = identity.label_moves(base, ours)
    assert m["verdict"] == "moved" and m["frame_is"] == 1 and m["slide_is"] == 0
    assert identity.align_slides(base, ours) == {0: 0, 1: 1}


def test_two_labels_swapped_between_frames_are_both_re_paired():
    """Neither pairing is settled, so each one's slides are free to be the other's partner - which
    is the only way a swap can be seen at all."""
    base = [info("Moving labels", MOVING, "a"), info("Arriving labels", ARRIVING, "b")]
    ours = [info("Moving labels", MOVING, "b"), info("Arriving labels", ARRIVING, "a")]
    assert sorted(m["label"] for m in identity.label_moves(base, ours)) == ["a", "b"]
    assert identity.align_slides(base, ours) == {0: 0, 1: 1}


QUARTER3 = "the table notes review and export figures for the third quarter of the year"
QUARTER4 = "the table notes review and export figures for the fourth quarter of the year"


def test_labels_swapped_between_two_frames_that_say_nearly_the_same_thing():
    """`LABEL_MARGIN` cannot see this one. The two frames differ by a word, so the pairing the swap
    leaves behind is already 0.9 alike and nothing can beat it by half - which is why a deck full of
    near-twins (every deck `adopt` writes) used to take this in silence. What a swap does do is come
    out word for word right on *both* sides while its own pairing is neither."""
    base = [info("", QUARTER3, "q3"), info("", QUARTER4, "q4")]
    ours = [info("", QUARTER3, "q4"), info("", QUARTER4, "q3")]
    moves = identity.label_moves(base, ours)
    assert sorted(m["label"] for m in moves) == ["q3", "q4"]
    assert {m["verdict"] for m in moves} == {"moved"}
    assert identity.align_slides(base, ours) == {0: 0, 1: 1}, "the words decide, and they are right"
    assert identity.label_pairs(base, ours) == {0: 1, 1: 0}, "the label alone would have crossed them"


def test_one_twin_edited_is_not_a_swap():
    """The mirror image, and the reason the exactness is asked for on both sides: two frames that
    say word for word the same thing, nobody touching a label, and the source rewording one of
    them. The untouched twin then explains the edited one's slide exactly - one side of the story,
    which is what a frame edited hard always looks like. Requiring only that side would cry wolf on
    every deck that repeats itself."""
    twin = "the table notes review and export figures for the quarter just gone"
    base = [info("", twin, "first"), info("", twin, "second")]
    ours = [info("", twin.replace("just gone", "about to start"), "first"), base[1]]
    assert identity.label_moves(base, ours) == []
    assert identity.align_slides(base, ours) == {0: 0, 1: 1}


# Two frames of one talk, half their words shared, and a third that has nothing to do with either.
AGENDA = "the agenda for today welcome demos questions and the break in the middle"
ROADMAP = "the agenda for today welcome demos budgets owners dates and the year ahead"
NEWWORDS = "a page about the sponsors and where to find coffee during the break"


def test_a_label_on_a_frame_that_is_word_for_word_another_slide_is_asked_about():
    """The last silent misidentification the 4,000-round adopt-shaped sync campaign had (seed
    22204): the source moved a label onto the frame after it *and* dropped a frame in the same
    version, so the slide the label left had no frame left to explain it - one side of the story
    only, and that side missed `LABEL_MARGIN` by 0.013.

    The side it has is exact: the frame carrying the label says, word for word, what another slide
    said, while the label's own pairing (0.62 here) says half of it. The margin cannot see that -
    1.0 beats 0.62 by 0.38 - and the source cannot have written it by editing this frame either:
    it would have had to rewrite it into a copy of another one. So the question is worth asking,
    even though one side proves nothing and nothing is re-paired."""
    base = [info("", AGENDA, "agenda"), info("", ROADMAP, "roadmap")]
    ours = [info("", NEWWORDS), info("", ROADMAP, "agenda")]
    (m,) = identity.label_moves(base, ours)
    assert m["verdict"] == "unsure" and m["label"] == "agenda"
    assert m["frame_is"] == 1 and m["slide_is"] is None
    assert m["frame_score"] == 1.0 and m["similarity"] == 0.62, "the margin alone would have missed it"
    assert identity.align_slides(base, ours) == {1: 0}, "the label is followed; the report asks"


def test_the_other_side_alone_is_not_enough_when_the_deck_has_twins():
    """Why only one of the two sides may stand on its own. Here the exact match is on the other
    side: some frame says word for word what this label's slide said. On a deck of twins that is
    true without anybody moving anything - the untouched twin always explains it - so a source
    that merely rewords the labelled frame would set this off on every deck that repeats itself.
    (`test_one_twin_edited_is_not_a_swap` says the same with both frames labelled, where their own
    labels settle them and there is nothing free to explain anything.)"""
    twin = "the table notes review and export figures for the quarter just gone"
    base = [info("", twin, "first"), info("", twin)]
    ours = [info("", twin.replace("just gone", "now ending"), "first"), info("", twin)]
    assert identity.label_moves(base, ours) == []


def test_a_label_moved_onto_a_frame_that_did_not_exist_before_is_only_a_question():
    """The frame that had the label is gone from the source, so only one half of the story can be
    checked. Nothing is re-paired; the report asks."""
    base = [info("Moving labels", MOVING, "mobile"), info("Arriving labels", ARRIVING)]
    ours = [info("Arriving labels", ARRIVING, "mobile"), info("Something else", "a page of new words entirely")]
    (m,) = identity.label_moves(base, ours)
    assert m["verdict"] == "unsure" and m["frame_is"] == 1 and m["slide_is"] is None
    assert identity.align_slides(base, ours)[0] == 0  # the label was followed


# ---------------------------------------------------------------- what is not found

def test_a_frame_rewritten_from_scratch_keeps_its_label_and_its_slide():
    """The plausible edit this check must never touch: same frame, all new words. It looks exactly
    like a label move from one side, and there is no second side to it."""
    base = [info("Intro", "why we do this and what it costs", "intro"), info("End", "thanks for listening", "end")]
    ours = [info("Intro", "an entirely different opening about something else", "intro"), base[1]]
    assert identity.label_moves(base, ours) == []
    assert identity.align_slides(base, ours) == {0: 0, 1: 1}


def test_overlay_steps_of_one_frame_are_never_a_move():
    """Steps share the frame's label and differ by a bullet; the step before is always the best
    other candidate there is. The margin is what keeps it quiet."""
    def step(n, page):
        return info("Building up", " ".join(["a bullet about decks"] * (n + 1)), "build", page)
    base = [step(0, 0), step(1, 1), step(2, 2)]
    assert identity.label_moves(base, [step(1, 0), step(2, 1)]) == []       # first step dropped
    assert identity.label_moves(base, [step(n, n) for n in range(4)]) == []  # a step added


def test_repeated_titles_do_not_pull_a_label_off_its_frame():
    """Three frames called "Results" whose text is close: the slides that could be confused are
    all settled by their own labels, so none of them is free to be somebody else's frame."""
    base = [info("Results", f"the numbers for quarter {q} went up", f"r{q}") for q in (1, 2, 3)]
    ours = [info("Results", "the numbers for quarter 2 went up a lot", "r1")] + base[1:]
    assert identity.label_moves(base, ours) == []


def test_frames_that_say_nothing_are_left_to_their_labels():
    """Two full-page pictures, no title and no words between them: they are alike by default, and
    that is evidence of nothing (identity._evidence). The label is all there is, so it is kept -
    here the source reordered the two frames and the labels went with them, as they should."""
    base = [info("", "", "pic1"), info("", "", "pic2")]
    ours = [info("", "", "pic2"), info("", "", "pic1")]
    assert identity.label_moves(base, ours) == []
    assert identity.align_slides(base, ours) == {0: 1, 1: 0}


def test_a_label_written_into_a_frame_that_had_none_is_not_a_move():
    """What `beamer2slides label --apply` does to a source whose deck already exists."""
    base = [info("Intro", "why we do this and what it costs"), info("End", "thanks for listening")]
    ours = [info("Intro", "why we do this and what it costs", "intro"),
            info("End", "thanks for listening", "end")]
    assert identity.label_moves(base, ours) == []
    assert identity.align_slides(base, ours) == {0: 0, 1: 1}


# ---------------------------------------------------------------- what the report says

def report_of(moves):
    report = merge.empty_report()
    merge.report_label_moves(moves, report)
    return report


def test_a_move_is_reported_as_a_conflict_with_both_sides_of_the_story():
    base, ours = moved_pair()
    moves = identity.label_moves(base, ours)
    for m in moves:  # sync puts the base's keys in before the report is written
        m["slide"], m["frame_is"], m["slide_is"] = "mobile", "title:arriving labels#1", "Moving labels"
    (c,) = report_of(moves)["conflicts"]
    assert c["field"] == "label" and c["slide"] == "mobile"
    assert "Moving labels" in c["base"] and "Arriving labels" in c["ours"]
    assert "content" in c["resolution"]


def test_an_unsure_move_says_that_it_followed_the_label():
    (c,) = report_of([{"label": "mobile", "verdict": "unsure", "ours": 0, "base": 0, "similarity": 0.3,
                       "base_title": "Moving labels", "ours_title": "Arriving labels",
                       "slide_is": None, "slide_score": None, "frame_is": "x", "frame_score": 1.4}])["conflicts"]
    assert "followed the label" in c["resolution"] and "nothing re-paired" in c["resolution"]


def test_a_label_that_was_renamed_is_a_warning_not_a_conflict():
    """The content recognised the frame, so nothing is at risk this time - but the source has one
    hook fewer to hang the next version on, and that is worth saying."""
    base = {"slides": [{"key": "intro", "label": "intro", "objectId": "s1", "elements": [], "title": "Intro",
                        "background": None, "notes": "", "layout": "TITLE_ONLY"}]}
    ours = {"slides": [{"key": "intro", "label": "introduction", "elements": [], "title": "Intro",
                        "background": None, "notes": "", "layout": "TITLE_ONLY"}], "pairs": {0: 0}}
    theirs = {"slides": [{"objectId": "s1", "objects": {}, "notes": "", "background": None}]}
    report = merge.plan_merge(base, ours, theirs)["report"]
    assert report["conflicts"] == []
    (w,) = report["warnings"]
    assert "`introduction` now" in w and "`intro`" in w
