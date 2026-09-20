"""What the order-keeping alignment leaves over (identity.cross_pairs, identity.gap_pairs,
docs/sync.md "Identity").

Frames without a label pair with the base by an order-keeping alignment, so of two frames that
crossed, only one can stay in the chain: the other falls out and is read as a new frame, while the
slide it was made from is read as one the source dropped. A frame nothing moved falls out the same
way when the source replaced its title and half its words. Nobody loses a word either way - a slide
the person edited is kept - but their edits end up beside the frame's old words while the source
writes the new ones onto a slide next to it. That is the same harm a label pointing at the wrong
frame does, so it is settled the same way: by what the two sides say, and where they say it, and
only when they say it clearly.
"""

from beamer2slides import identity

MOTIV = "decks and sources drift apart as soon as somebody opens the deck in slides"
METHOD = "the merge reads the base the conversion and the live deck and decides per field"
RESULTS = "every deck edit survived the sync and the report explains what it did"
SUMMARY = "labels decide identity and the content decides when a label cannot"
SAME = "the table below repeats the measured numbers row after row after row"
# METHOD with half its words rewritten: under `SLIDE_MATCH`, so the alignment lets it go, and
# under `CROSS_SURE` too - the words alone are no longer enough to say which frame this is.
METHOD_HALF = "the merge reads the base and then decides field by field for you"


def info(title, text, label=None, page=0):
    return {"label": label, "title": title, "text": text, "page": page}


def talk():
    return [info("Motivation", MOTIV), info("Method", METHOD), info("Results", RESULTS), info("Takeaways", SUMMARY)]


def test_a_frame_moved_to_the_front_keeps_its_slide():
    base = talk()
    ours = [base[2], base[0], base[1], base[3]]      # Results moved up in front of Motivation
    assert identity.align_slides(base, ours) == {0: 2, 1: 0, 2: 1, 3: 3}


def test_two_frames_swapped_keep_their_own_slides():
    base = talk()
    ours = [base[0], base[2], base[1], base[3]]
    assert identity.align_slides(base, ours) == {0: 0, 1: 2, 2: 1, 3: 3}


def test_the_order_keeping_pass_is_what_leaves_them_over():
    """The pairing above is the leftovers' doing, not the alignment's: without this pass one of the
    two crossed frames is unpaired, which is what sends its slide's edits to the wrong place."""
    base = talk()
    ours = [base[2], base[0], base[1], base[3]]
    by_order = identity.align_slides(base, ours)
    by_order.pop(0)
    assert identity.cross_pairs(base, ours, by_order, lambda i, j: True) == {0: 2}


def test_frames_that_say_the_same_thing_are_left_to_the_order():
    """Three frames called Results with the same table under them (the stress deck has exactly
    that): whichever slide is left over, nothing about it says which frame it was. Pairing it with
    the likeliest one would be a guess, and a guess here writes one frame onto another's slide."""
    base = [info("Results", SAME), info("Results", SAME)]
    ours = [info("Results", SAME)]
    assert identity.cross_pairs(base, ours, {}, lambda i, j: True) == {}


def test_a_frame_rewritten_as_well_as_moved_is_a_new_frame():
    """The limit, on purpose: a frame that moved and kept nothing it used to say is not something
    the content can recognise. It comes back as a new slide, which costs a rebuild of that slide -
    never somebody's words on the wrong one."""
    base = talk()
    ours = [info("Results", "this frame was rewritten from scratch while it was moved"), base[0], base[1], base[3]]
    assert 0 not in identity.align_slides(base, ours)


def test_a_retitled_frame_between_two_that_paired_keeps_its_slide():
    """The other half of the leftovers: a frame nothing moved, but whose title was replaced. It has
    no label, half its words are new, and that is under `SLIDE_MATCH` - but both its neighbours
    paired, on both sides, and the gap they leave holds exactly one slide and exactly one frame.
    `identity.gap_pairs` is that: the place says what the words no longer do."""
    base = [info("Motivation", MOTIV, "intro"), info("Method", METHOD), info("Results", RESULTS, "results")]
    ours = [base[0], info("How the merge decides", METHOD_HALF), base[2]]
    assert identity.align_slides(base, ours) == {0: 0, 1: 1, 2: 2}
    assert identity.cross_pairs(base, ours, {0: 0, 2: 2}, lambda i, j: True) == {}   # not by the words alone


def test_the_last_frame_retitled_keeps_its_slide_too():
    """The ends are gaps as well: nothing follows the last frame on either side, which pins it as
    surely as a neighbour would."""
    base = [info("Motivation", MOTIV), info("Results", RESULTS), info("Method", METHOD)]
    ours = [*base[:2], info("How the merge decides", METHOD_HALF)]
    assert identity.align_slides(base, ours) == {0: 0, 1: 1, 2: 2}


def test_a_frame_deleted_and_another_written_in_its_place_is_not_that_frame():
    """The limit of the gap, and why `GAP_SURE` exists: one slide and one frame alone between the
    same two neighbours can also be a frame the source deleted and a new one put where it stood.
    Sharing the place is not sharing a past, so some of the words have to be the same words."""
    base = talk()
    ours = [base[0], info("Funding", "this frame is about who paid for the work and nothing else"), base[2], base[3]]
    assert 1 not in identity.align_slides(base, ours)


def test_two_frames_left_over_in_one_gap_are_left_alone():
    """The gap pairs one with one. Two of each is a question the place cannot answer - which of
    them is which - and `cross_pairs` has already refused it on the content."""
    base = [info("Motivation", MOTIV, "intro"), info("Method", METHOD), info("Results", RESULTS),
            info("Takeaways", SUMMARY, "end")]
    ours = [base[0], info("How it decides", METHOD), info("What came out", RESULTS), base[3]]
    assert identity.gap_pairs(base, ours, {0: 0, 3: 3}, lambda i, j: True) == {}
    # Take the second of each out, and the one left in the gap is paired (the rule, the other way).
    three_base, three_ours = base[:2] + base[3:], ours[:2] + ours[3:]
    assert identity.gap_pairs(three_base, three_ours, {0: 0, 2: 2}, lambda i, j: True) == {1: 1}


def test_the_two_leftover_passes_cannot_both_claim_the_same_slide():
    """The passes run one after the other, and the second one only gets what the first one left.

    Here the source moved Method to the end and retitled and half-rewrote another frame in its
    place. `cross_pairs` recognises the moved frame by its words; `gap_pairs`, looking at the same
    leftovers, sees exactly one slide and one frame between two paired neighbours and pairs those
    too - the same slide. Written as one tuple of two calls, both answers were merged and two
    frames came out carrying one key. Nothing downstream survives that: sync wrote both frames onto
    that one slide, the other slide's picture was gone, and the report said no slide was created
    (offline fuzz seed 5521, where the source had swapped two frames).
    """
    base = talk()
    ours = [base[0], info("How the merge decides", METHOD_HALF), base[2], base[1]]
    # Each pass on its own, from the leftovers the order-keeping alignment hands them:
    assert identity.cross_pairs(base, ours, {0: 0, 2: 2}, lambda i, j: True) == {3: 1}
    assert identity.gap_pairs(base, ours, {0: 0, 2: 2}, lambda i, j: True) == {1: 1}
    pairs = identity.align_slides(base, ours)
    assert pairs == {0: 0, 2: 2, 3: 1}          # the content wins; the place has nothing left to take
    assert len(set(pairs.values())) == len(pairs)


def test_two_leftovers_that_look_alike_are_reported_though_nothing_pairs_them():
    """A frame retitled, half rewritten *and* moved: no label, too little left for the content, no
    gap to stand in. Every pass refuses it, rightly - and `identity.near_misses` says out loud that
    the question came up, because the author is the only one who can answer it."""
    base = talk()
    ours = [info("How the merge decides", METHOD_HALF), base[0], base[2], base[3]]   # moved to the front
    pairs = identity.align_slides(base, ours)
    assert 0 not in pairs                                                  # nothing claims it
    assert [(m["ours"], m["base"]) for m in identity.near_misses(base, ours, pairs)] == [(0, 1)]
    # A frame that really is new, beside a slide the source really did drop, says nothing.
    other = [info("Funding", "this frame is about who paid for the work and nothing else"), base[0], base[2], base[3]]
    assert identity.near_misses(base, other, identity.align_slides(base, other)) == []


def test_an_unlabelled_frame_between_twins_is_matched_and_said_out_loud():
    """A deck a person built in Slides repeats itself: three slides saying the same thing, one of
    them dropped by the source. Whichever of the survivors the alignment pairs the unlabelled frame
    with, the other is as good a reading and costs the same score, so the answer is a coin toss -
    and a coin toss that decides whose edits get written over must not pass in silence
    (`fuzz_labels --shape adopt` seed 32773: two frames of 20,080 misidentified, both silent).
    """
    base = [info("Results", SAME, "one"), info("Results", SAME), info("Results", SAME),
            info("Takeaways", SUMMARY, "end")]
    ours = [base[0], info("Results", SAME), base[3]]          # one of the twins is gone; which one?
    weak: dict[int, str] = {}
    pairs = identity.align_slides(base, ours, weak=weak)
    assert pairs == {0: 0, 1: 1, 2: 3} and weak == {1: "twins"}
    # The same frame with a label of its own is no coin toss: the label pairs it and nothing is said.
    named = [base[0], info("Results", SAME, "middle"), base[3]]
    base_named = [base[0], info("Results", SAME, "middle"), base[2], base[3]]
    weak = {}
    assert identity.align_slides(base_named, named, weak=weak) == {0: 0, 1: 1, 2: 3} and weak == {}


def test_a_frame_prefers_the_slide_the_source_still_describes_to_the_one_it_dropped():
    """A slide the source dropped that the deck's own edits keep alive stays in the base
    (`sync.new_base`'s `keep_removed`), saying what that frame said and carrying no label any more -
    and `sync.base_order` puts it back beside the slide the frame really lives on. So for the frame
    that carries those words the two entries read exactly alike, the traceback takes the earlier of
    two alignments of one score, and the frame flips onto the dead entry: the next sync then plans
    to *delete* the slide this one has just written, with the person's edits on it (converted fuzz
    seed 2100403 at chain 6, and 2100135). `removed` breaks the tie towards the living slide."""
    base = [info("Motivation", MOTIV), {**info("Results", RESULTS), "removed": True},
            info("Results", RESULTS), info("Takeaways", SUMMARY)]
    ours = [base[0], info("Results", RESULTS), base[3]]
    assert identity.align_slides(base, ours) == {0: 0, 1: 2, 2: 3}
    # It is a tie-break and nothing more: with no living twin the frame comes back to the very slide
    # the source had dropped, which is a frame the author put back and its edits waiting for it.
    back = [base[0], base[1], base[3]]
    assert identity.align_slides(back, ours) == {0: 0, 1: 1, 2: 2}


def test_the_leftovers_obey_the_labels_too():
    """Two labels that both exist on both sides belong to two frames that both exist, so the
    leftovers are no more free to pair across them than the alignment is (`align_slides.pairable`)."""
    base = [info("Motivation", MOTIV, "intro")]
    ours = [info("Motivation", MOTIV, "opening")]
    assert identity.cross_pairs(base, ours, {}, lambda i, j: False) == {}
    assert identity.cross_pairs(base, ours, {}, lambda i, j: True) == {0: 0}
