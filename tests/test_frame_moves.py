"""A frame the source moved across another (identity.cross_pairs, docs/sync.md "Identity").

Frames without a label pair with the base by an order-keeping alignment, so of two frames that
crossed, only one can stay in the chain: the other falls out and is read as a new frame, while the
slide it was made from is read as one the source dropped. Nobody loses a word that way - a slide
the person edited is kept - but their edits end up beside the frame's old words while the source
writes the new ones onto a slide next to it. That is the same harm a label pointing at the wrong
frame does, so it is settled the same way: by what the two sides say, and only when they say it
clearly.
"""

from beamer2slides import identity

MOTIV = "decks and sources drift apart as soon as somebody opens the deck in slides"
METHOD = "the merge reads the base the conversion and the live deck and decides per field"
RESULTS = "every deck edit survived the sync and the report explains what it did"
SUMMARY = "labels decide identity and the content decides when a label cannot"
SAME = "the table below repeats the measured numbers row after row after row"


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


def test_the_leftovers_obey_the_labels_too():
    """Two labels that both exist on both sides belong to two frames that both exist, so the
    leftovers are no more free to pair across them than the alignment is (`align_slides.pairable`)."""
    base = [info("Motivation", MOTIV, "intro")]
    ours = [info("Motivation", MOTIV, "opening")]
    assert identity.cross_pairs(base, ours, {}, lambda i, j: False) == {}
    assert identity.cross_pairs(base, ours, {}, lambda i, j: True) == {0: 0}
