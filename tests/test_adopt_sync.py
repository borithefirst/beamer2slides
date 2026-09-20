"""The sync base `adopt` records, and the refusals a sync into an adopted deck makes
(src/beamer2slides/adopt_sync.py, docs/sync.md "Adopt").

Offline: no TeX, no Google. A hand-built `presentations.get` answer stands for the deck, a
hand-built classify IR for the conversion of the source adopt wrote, and the round trip - adopt,
the source changes, the person edits the deck, sync - is played out in `fuzz_world`'s reference
world, judged by `loss_oracle`.

Every refusal's message is asserted word for word: what a person reads when nothing was written is
the whole of what they get, so it is part of the product.
"""

import copy
import json
import random
from pathlib import Path

import pytest

from beamer2slides import adopt, adopt_sync, merge, snapshot, sync as sync_mod
from beamer2slides.devtools import fuzz_sync, fuzz_world as W, loss_oracle

EMU = 12700


# ---------------------------------------------------------------- a deck and its conversion

def pt(v: float) -> dict:
    return {"magnitude": v * EMU, "unit": "EMU"}


def live_shape(oid: str, box, text: str | None = None) -> dict:
    x0, y0, x1, y1 = box
    shape = {"shapeType": "TEXT_BOX"}
    if text is not None:
        shape["text"] = {"textElements": [{"paragraphMarker": {"style": {"alignment": "START"}}},
                                          {"textRun": {"content": text + "\n",
                                                       "style": {"fontFamily": "Roboto", "fontSize": pt(14)}}}]}
    return {"objectId": oid, "title": None, "description": None,
            "size": {"width": pt(x1 - x0), "height": pt(y1 - y0)},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x0 * EMU, "translateY": y0 * EMU, "unit": "EMU"},
            "shape": shape}


def presentation(slides: list[list[dict]], width: float = 720.0, height: float = 405.0) -> dict:
    """`presentations.get` of a deck a person built: object ids of their own, no alt text."""
    return {"presentationId": "PERSONS_DECK", "revisionId": "rev1",
            "pageSize": {"width": pt(width), "height": pt(height)},
            "masters": [{"objectId": "M", "pageProperties": {}}],
            "layouts": [{"objectId": "L", "layoutProperties": {"name": "BLANK", "masterObjectId": "M"}}],
            "slides": [{"objectId": f"gSLIDE{n}", "slideProperties": {"layoutObjectId": "L"},
                        "pageElements": els} for n, els in enumerate(slides)]}


def deck_element(oid: str, box, text: str, **extra) -> dict:
    """One element of the IR `deck_ir(foreign=True)` gives adopt: the deck's own object, boxed in
    PDF pt."""
    return {"id": oid, "object": oid, "kind": "text", "role": "body", "bbox": list(box),
            "paragraphs": [{"runs": [{"text": text}]}], **extra}


def target(slides: list[list[dict]], size=(453.54, 255.12)) -> dict:
    return {"slides": [{"page": n, "objectId": f"gSLIDE{n}", "size": list(size), "notes": "",
                        "background_color": "#ffffff", "elements": els} for n, els in enumerate(slides)]}


def conv_element(eid: str, box, text: str, role: str = "body") -> dict:
    """One element of the conversion of the source adopt wrote (classify's IR)."""
    return {"id": eid, "kind": "text", "role": role, "bbox": list(box),
            "paragraphs": [{"runs": [{"text": text}], "style": {}}]}


def conversion(tgt: dict, slides: list[list[dict]], size=(453.54, 255.12)) -> dict:
    """The classify IR of that source, labelled the way `adopt.frame_labels` labels the frames."""
    labels = adopt.frame_labels(tgt)
    return {"slides": [{"page": n, "label": labels[n], "size": list(size), "notes": "",
                        "background_color": "#ffffff", "elements": els} for n, els in enumerate(slides)]}


# ---------------------------------------------------------------- pairing

def test_an_element_pairs_with_the_object_it_was_drawn_from():
    conv = [conv_element("a", (30, 40, 130, 56), "Why it matters"),
            conv_element("b", (30, 90, 200, 106), "Three things happened")]
    deck = [deck_element("gA", (30, 40, 130, 56), "Why it matters"),
            deck_element("gB", (30, 90, 200, 106), "Three things happened")]
    pairs, why = adopt_sync.pair_elements(conv, deck)
    assert pairs == {0: 0, 1: 1}
    assert why == {}


def test_two_objects_that_say_the_same_thing_at_the_same_place_pair_with_neither():
    """A foreign deck is full of these (a row of cards, a table of icons). Picking one would tie
    the source to an object it was not drawn from, and a later sync would write over it."""
    conv = [conv_element("a", (30, 40, 130, 56), "Learn more")]
    deck = [deck_element("gA", (30, 40, 130, 56), "Learn more"),
            deck_element("gB", (32, 41, 132, 57), "Learn more")]
    pairs, why = adopt_sync.pair_elements(conv, deck)
    assert pairs == {}
    assert why[0] == adopt_sync.AMBIGUOUS


def test_an_element_with_nothing_like_it_in_the_deck_pairs_with_nothing():
    conv = [conv_element("a", (30, 40, 130, 56), "Why it matters")]
    deck = [deck_element("gA", (300, 200, 420, 216), "Entirely different words here")]
    pairs, why = adopt_sync.pair_elements(conv, deck)
    assert pairs == {}
    assert why[0] == adopt_sync.NO_CANDIDATE


def test_one_object_is_explained_by_one_element():
    """Two elements of the source over one object of the deck: the better one takes it, the other
    is left unpaired rather than sharing it."""
    conv = [conv_element("a", (30, 40, 130, 56), "Why it matters"),
            conv_element("b", (30, 41, 130, 57), "Why it matters at all, really, truly")]
    deck = [deck_element("gA", (30, 40, 130, 56), "Why it matters")]
    pairs, why = adopt_sync.pair_elements(conv, deck)
    assert sorted(pairs) == [0]
    assert why[1] in (adopt_sync.NOT_BEST, adopt_sync.AMBIGUOUS)


def test_what_the_layout_draws_is_never_paired():
    """`deck_ir(foreign=True)` gives a slide what its layout and master draw, because adopt has to
    draw them too - but those are not the slide's objects, and writing to one would edit the
    template under every other slide."""
    tgt = target([[deck_element("gA", (30, 40, 130, 56), "Why it matters"),
                   deck_element("L~gDeco", (0, 0, 453, 20), "conference 2026", inherited="L")]])
    assert [e["id"] for e in adopt_sync.deck_objects(tgt["slides"][0])] == ["gA"]
    assert [e["id"] for e in adopt_sync.layout_elements(tgt["slides"][0])] == ["L~gDeco"]


# ---------------------------------------------------------------- what the layout draws

def test_an_element_the_layout_draws_is_recognised_and_not_counted_as_a_miss():
    """`adopt` recovers the deck's layouts as a beamer theme, so the source draws the footer on
    every slide that inherits it and the conversion has an element for each - with nothing on the
    slide to pair with, for ever. That is not the pairing failing: the object is there, one level
    up, where nothing here may write. Told apart, the person gets the one instruction that works
    (Slide > Edit theme) instead of being sent to look for a box that is not on the slide."""
    conv = [conv_element("a", (30, 40, 130, 56), "Why it matters"),
            conv_element("b", (12, 4, 118, 18), "conference 2026")]
    tgt = target([[deck_element("gA", (30, 40, 130, 56), "Why it matters"),
                   deck_element("L~gDeco", (10, 2, 120, 20), "conference 2026", inherited="L")]])
    slide = tgt["slides"][0]
    pairs, why = adopt_sync.pair_elements(conv, adopt_sync.deck_objects(slide))
    assert pairs == {0: 0} and list(why) == [1]
    assert adopt_sync.explained_by_layout(conv, why, slide) == {1}


def test_the_slides_own_object_is_asked_first():
    """A person who put a box of their own over the template's is the one this sync writes to, so
    the layout is only ever asked about what `pair_elements` left over."""
    conv = [conv_element("a", (10, 2, 120, 20), "conference 2026")]
    tgt = target([[deck_element("gA", (10, 2, 120, 20), "conference 2026"),
                   deck_element("L~gDeco", (10, 2, 120, 20), "conference 2026", inherited="L")]])
    slide = tgt["slides"][0]
    pairs, why = adopt_sync.pair_elements(conv, adopt_sync.deck_objects(slide))
    assert pairs == {0: 0} and adopt_sync.explained_by_layout(conv, why, slide) == set()


def test_an_icon_in_the_middle_of_a_full_bleed_background_is_not_the_layouts():
    """`identity._geometry` scores two boxes by the better of their overlap and how close their
    centres are, which is right for two readings of one element and wrong here: a layout that draws
    a picture over the whole slide shares its centre with everything a person put in the middle of
    it. Measured on the corpus deck sc-dark-modern, where a 35 pt icon scored 0.45 against the
    background and would have been reported as a thing to go and change on the layout."""
    page = {"kind": "image", "bbox": [0, 1, 454, 254], "text": ""}
    icon = {"kind": "image", "bbox": [226, 82, 261, 117], "text": ""}
    assert adopt_sync.similarity(icon, page) >= adopt_sync.PAIR_SURE
    assert not adopt_sync.same_drawing(icon, page)
    assert adopt_sync.same_drawing({"kind": "image", "bbox": [0, 0, 453, 255], "text": ""}, page)


def test_a_layouts_words_are_the_same_drawing_however_much_room_they_have():
    """The one thing sizes cannot decide. A layout's `Thank you!` placeholder is 296 pt wide and
    the converter reads the words back at the 109 pt they cover (firebase-jam), so the same words
    inside the template's own box are the same drawing whatever the room around them."""
    place = {"kind": "text", "bbox": [79, 120, 375, 164], "text": "Thank you!"}
    ink = {"kind": "text", "bbox": [172, 128, 281, 151], "text": "Thank you!"}
    assert adopt_sync.same_drawing(ink, place)
    assert not adopt_sync.same_drawing({**ink, "text": "Something else"}, place)
    assert not adopt_sync.same_drawing({**ink, "bbox": [172, 200, 281, 223]}, place), "outside it"


# ---------------------------------------------------------------- the base

@pytest.fixture
def adopted(tmp_path):
    """A two-slide deck a person built, and the conversion of the source adopt wrote from it: one
    element pairs on each slide, and slide 1 has a second element nothing can be paired to."""
    tgt = target([[deck_element("gA", (30, 40, 130, 56), "Why it matters")],
                  [deck_element("gB", (30, 40, 200, 56), "Three things happened"),
                   deck_element("gC", (30, 90, 200, 106), "Learn more"),
                   deck_element("gD", (33, 92, 203, 108), "Learn more")]])
    pres = presentation([[live_shape("gA", (48, 64, 206, 89), "Why it matters")],
                         [live_shape("gB", (48, 64, 318, 89), "Three things happened"),
                          live_shape("gC", (48, 143, 318, 168), "Learn more"),
                          live_shape("gD", (52, 146, 322, 171), "Learn more")]])
    conv = conversion(tgt, [[conv_element("p0e0", (30, 40, 130, 56), "Why it matters")],
                            [conv_element("p1e0", (30, 40, 200, 56), "Three things happened"),
                             conv_element("p1e1", (30, 90, 200, 106), "Learn more")]])
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    base = adopt_sync.build_base(conv, tmp_path, tgt, pres, pdf)
    return {"base": base, "target": tgt, "pres": pres, "conv": conv}


def test_the_base_names_the_persons_own_objects(adopted):
    base = adopted["base"]
    assert base["origin"] == "adopt"
    assert base["generation"] == 0, "nothing has ever been written to this deck"
    assert [s["objectId"] for s in base["slides"]] == ["gSLIDE0", "gSLIDE1"]
    first = base["slides"][0]["elements"][0]
    assert first["objects"] == ["gA"] and first["main"] == "gA"
    assert first["readback"]["gA"]["text"] == "Why it matters\n", "the live object, read as sync reads it"
    assert all(s["groups"] == [] for s in base["slides"]), "a group on an adopted slide is the person's"


def test_the_base_refuses_to_claim_an_object_it_could_not_tell_apart(adopted):
    """The deck has two boxes saying "Learn more" on top of each other; the source has one. The
    element is recorded with no object, and the base says why."""
    el = next(e for e in adopted["base"]["slides"][1]["elements"] if e["ir"]["id"] == "p1e1")
    assert el["objects"] == [] and el["main"] is None
    said = adopted["base"]["adopt"]["unpaired"]
    assert [(u["element"], u["why"]) for u in said] == [(el["key"], adopt_sync.AMBIGUOUS)]
    assert adopted["base"]["adopt"]["paired"] == 2


def test_the_base_counts_what_the_layout_draws_apart_from_what_it_could_not_place(tmp_path):
    """Two different answers to "no object": one is a pairing that could not be made and refuses a
    sync that touches it, the other is an object that exists on the template. Kept apart in the
    base, because the merge reads the elements and a person reads the summary. Measured on the
    corpus: 57 of hebrew-lesson's 215 misses are its own theme drawn again, and 11 of 40 of
    apps-edu-zh's are one header."""
    tgt = target([[deck_element("gA", (30, 40, 130, 56), "Why it matters"),
                   deck_element("L~gDeco", (10, 2, 120, 20), "conference 2026", inherited="L")]])
    pres = presentation([[live_shape("gA", (48, 64, 206, 89), "Why it matters")]])
    conv = conversion(tgt, [[conv_element("p0e0", (30, 40, 130, 56), "Why it matters"),
                             conv_element("p0e1", (12, 4, 118, 18), "conference 2026")]])
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    base = adopt_sync.build_base(conv, tmp_path, tgt, pres, pdf)
    el = base["slides"][0]["elements"][1]
    assert el["objects"] == [] and el["from_layout"] is True, "the merge reads this, not the summary"
    assert base["adopt"]["unpaired"] == []
    assert [(u["element"], u["why"]) for u in base["adopt"]["from_layout"]] == [(el["key"], adopt_sync.FROM_LAYOUT)]
    assert adopt_sync.report_lines(base) == [
        "sync base: 1 slides, 1 of 2 elements tied to an object of the deck",
        "  1 of them are drawn by the deck's own layouts and master, which this converter never "
        "writes to: change those on the layout, in Slides"]


def test_the_base_names_the_deck_objects_the_source_does_not_draw(adopted):
    """Reported, never written: `merge.user_objects` leaves them alone and so does every sync."""
    left = {x["slide"]: x["objects"] for x in adopted["base"]["adopt"]["left_alone"]}
    assert sorted(o for oids in left.values() for o in oids) == ["gC", "gD"]


def test_the_base_records_the_decks_own_page_and_scale(adopted):
    base = adopted["base"]
    assert base["deck_page_size"] == [720.0, 405.0]
    assert base["page_size"] == [453.54, 255.12]
    assert base["scale"] == pytest.approx(720.0 / 453.54)
    assert merge.deck_scale(base) == pytest.approx(720.0 / 453.54)


def test_the_base_claims_no_master_background(adopted):
    """`sync.background_requests` copies the base's master background onto a slide whose background
    the source changed. On an adopted deck that master is the person's, and copying it would paint
    their template onto a slide they never asked to change."""
    assert adopted["base"]["master_background"] is None


def test_the_base_is_written_where_sync_looks_for_it(adopted, tmp_path):
    out = tmp_path / "adopt-work"
    path = adopt_sync.store(adopted["base"], out)
    assert path == snapshot.local_path(out) == out / "sync" / "base.json"
    assert json.loads(path.read_text(encoding="utf-8"))["origin"] == "adopt"
    assert sync_mod.resolve_deck(str(out)) == ("PERSONS_DECK", out), "sync --deck <the adopt folder>"
    assert adopt_sync.next_command("main.pdf", out) == f"python -m beamer2slides sync main.pdf --deck {out}"


def test_a_source_whose_frames_are_not_the_decks_slides_gets_no_base(adopted):
    """The labels `adopt.frame_labels` wrote are the only thing that says which frame is which
    slide. If they do not come back out of the PDF, nothing below may be believed."""
    conv = copy.deepcopy(adopted["conv"])
    conv["slides"][1]["label"] = "something-else"
    why = adopt_sync.labels_match(conv, adopted["target"])
    assert why and "do not carry the label adopt wrote" in why
    assert adopt_sync.labels_match(conv["slides"] and {"slides": conv["slides"][:1]}, adopted["target"]) \
        .startswith("the source compiles to 1 slide(s) and the deck has 2")
    assert adopt_sync.labels_match(adopted["conv"], adopted["target"]) is None


def test_a_deck_read_without_its_presentation_gets_no_base(tmp_path, adopted):
    base, why = adopt_sync.record(tmp_path / "main.tex", tmp_path, adopted["target"], {"slides": []})
    assert base is None
    assert why == "the deck was read without its presentation (no read-back to record)"


def test_adopt_finds_the_presentation_stored_beside_an_offline_target(tmp_path):
    """`--deck <deck.json>` is the offline route (the corpus keeps `presentation.json` beside it)."""
    (tmp_path / "presentation.json").write_text(json.dumps({"presentationId": "P"}), encoding="utf-8")
    assert adopt.presentation_beside(tmp_path / "target.json") == {"presentationId": "P"}
    assert adopt.presentation_beside(tmp_path / "elsewhere" / "target.json") is None


# ---------------------------------------------------------------- the refusals

OUT = Path("out") / "talk"          # (str(OUT) is "out\\talk" on Windows and "out/talk" elsewhere)
CMD = f"python -m beamer2slides sync new.pdf --deck {OUT}"
# The commands under "What to do instead" line up: the labels are padded to the longest one of
# those offered, which for the no-way-back refusal is "see what it would write, writing nothing".
WIDTH = len("see what it would write, writing nothing")


def way(label: str, tail: str) -> str:
    return f"    {label.ljust(WIDTH)}  {CMD} {tail}"


def refuse(base, mplan, theirs, way_back=None, backup_mode="auto") -> str:
    found = adopt_sync.problems(base, mplan, theirs, way_back, backup_mode)
    assert found, "expected this sync to be refused"
    return adopt_sync.refusal_message("PERSONS_DECK", OUT, "new.pdf", found)


@pytest.fixture
def world(tmp_path):
    """An adopted deck in the reference world, with a source that changed and a person who edited
    it (`fuzz_world`): everything the refusals are asked about is a real merge plan."""
    rng = random.Random(7)
    doc = W.make("adopt", rng, tmp_path)
    base = W.build_adopt_base(doc, tmp_path, random.Random(3))
    live = W.live_of(base)
    return {"doc": doc, "base": base, "live": live, "out": tmp_path}


def plan_of(world, doc=None):
    ours = W.build_ours(doc or world["doc"], world["base"], world["out"])
    return merge.plan_merge(world["base"], ours, world["live"])


KEPT = {"drive": {"presentationId": "a-copy"}}


def test_a_first_sync_with_no_way_back_is_refused(world):
    """`--backup auto` exports the deck as .pptx before sync's first write. A Drive revision of a
    Slides file always exports its *current* content, so that file is the only way back - and an
    adopted deck has no earlier conversion to fall back on either."""
    doc = copy.deepcopy(world["doc"])
    fuzz_sync.src_reword(random.Random(1), doc)
    message = refuse(world["base"], plan_of(world, doc), world["live"], {"warnings": ["could not export the deck"]})
    assert message.splitlines()[0] == "refusing to sync into this adopted deck: 1 thing(s) about it cannot be trusted."
    assert "  https://docs.google.com/presentation/d/PERSONS_DECK/edit" in message
    assert ("  - no way back: no backup of the deck was kept, and Drive's version history cannot be read "
            "back (docs/sync.md)." in message)
    assert "      could not export the deck" in message
    assert "  Nothing was written. What to do instead:" in message
    assert way("see what it would write, writing nothing", "--dry-run") in message
    assert way("keep a copy of the deck in Drive first", "--backup drive") in message
    assert way("keep a .pptx of the deck first", "--backup file") in message
    assert way("write it anyway, saying so out loud", "--force-adopted-deck") in message
    assert message.count("python -m beamer2slides") == 4, "and nothing else is offered"


def test_backup_none_is_how_one_asks_for_a_sync_with_no_way_back(world):
    doc = copy.deepcopy(world["doc"])
    fuzz_sync.src_reword(random.Random(1), doc)
    found = adopt_sync.problems(world["base"], plan_of(world, doc), world["live"], None, "none")
    assert [p["reason"] for p in found] == []


def test_a_slide_of_the_adopted_deck_no_frame_accounts_for_is_kept(world):
    """Those slides were made by a person. A frame that has gone out of the source is as likely to
    be a label that did not survive the round trip as a slide the author meant to drop, and on that
    evidence nothing here may take somebody's own slide - so it is kept and said out loud, and the
    rest of the sync goes in. At every generation: the base records the decision by not accounting
    for the slide, so there is nothing to reverse itself."""
    for generation in (0, 4):
        base = {**copy.deepcopy(world["base"]), "generation": generation}
        doc = copy.deepcopy(world["doc"])
        fuzz_sync.src_delete_slide(random.Random(5), doc)
        mplan = plan_of({**world, "base": base}, doc)
        kept = [k for k in mplan["report"]["slides"]["kept"] if k["reason"] == ["the deck's own"]]
        assert len(kept) == 1 and mplan["report"]["slides"]["deleted"] == []
        assert [p["action"] for p in mplan["slides"] if p["key"] == kept[0]["slide"]] == ["keep_removed"]
        assert any("accounted for by no frame of the source, and were kept" in w and
                   "delete the slide in Slides" in w for w in mplan["report"]["warnings"])
        assert adopt_sync.problems(base, mplan, world["live"], KEPT) == []


def test_a_frame_put_back_finds_the_slide_that_was_kept_for_it(world):
    """What keeping costs: `sync.new_base`'s `keep_removed` takes the label off that base entry - a
    slide the source no longer describes must not hold a live label hostage - so when the author
    puts the frame back, the content alone has to pair them. It does: nothing is created beside the
    kept slide, which is the one way this could have gone wrong."""
    gone = copy.deepcopy(world["doc"])
    fuzz_sync.src_delete_slide(random.Random(5), gone)
    ours = W.build_ours(gone, world["base"], world["out"])
    mplan = merge.plan_merge(world["base"], ours, world["live"])
    kept = [k["slide"] for k in mplan["report"]["slides"]["kept"] if k["reason"] == ["the deck's own"]]
    after = W.apply_plan(world["base"], ours, world["live"], mplan, "t1")
    base2 = W.rebase(world["base"], ours, after, mplan, "t1")
    entry = next(s for s in base2["slides"] if s["key"] == kept[0])
    assert entry["label"] is None and entry["removed"] is True

    again = merge.plan_merge(base2, W.build_ours(world["doc"], base2, world["out"]), after)
    assert again["report"]["slides"]["created"] == [] and again["report"]["slides"]["kept"] == []
    assert again["report"]["slides"]["deleted"] == []


def test_the_slide_gate_still_stands_behind_the_merge(world):
    """As with an unpaired element: the merge decides, and the gate is what a plan saying otherwise
    meets on its way to a write (`adopt_sync.problems`, the first sync only)."""
    doc = copy.deepcopy(world["doc"])
    fuzz_sync.src_delete_slide(random.Random(5), doc)
    mplan = plan_of(world, doc)
    p = next(p for p in mplan["slides"] if p["action"] == "keep_removed")
    p["action"] = "delete"
    message = refuse(world["base"], mplan, world["live"], KEPT)
    assert "  - 1 slide(s) of the deck would be deleted, because no frame of the source accounts for them " \
           "any more: " in message
    assert ("      On the first sync that is usually a label that moved, not a slide the author meant to drop."
            in message)
    assert "    put the frame labels back where adopt wrote them (docs/labels.md), then sync again" in message


def unpair(world, generation: int = 0) -> tuple[dict, dict]:
    """The world's base with its first paired element tied to nothing, and a source that changes
    exactly that element - the shape `adopt_sync.pair_elements` leaves behind when two of a deck's
    boxes are too alike to tell apart."""
    base = {**copy.deepcopy(world["base"]), "generation": generation}
    doc = copy.deepcopy(world["doc"])
    el = next(e for s in base["slides"] for e in s["elements"] if e["objects"])
    el["objects"], el["main"], el["readback"] = [], None, {}
    ir = next(e for s in doc["slides"] for e in s["elements"] if e["id"] == el["ir"]["id"])
    ir["paragraphs"][0]["runs"] = [W.run("the source says something else now")]
    return base, doc


def test_an_element_tied_to_no_object_of_the_deck_is_kept_and_the_rest_syncs(world):
    """Sync deletes a recreated unit's old objects through the base, and an unpaired element names
    none: writing it would leave the person's own box standing and put a second one on top of it.
    So that unit is kept and everything else goes in - one element nothing can be written to
    freezes that element, not the talk. It used to refuse the whole sync, and over 400 first-sync
    campaign rounds that was 734 of ~2,400 syncs writing nothing at all."""
    base, doc = unpair(world)
    fuzz_sync.src_reword(random.Random(1), doc)            # ... and the source changes another slide too
    mplan = merge.plan_merge(base, W.build_ours(doc, base, world["out"]), world["live"])
    held = [(p["key"], u) for p in mplan["slides"] for u in p.get("units") or [] if u.get("unpaired")]
    assert len(held) == 1 and held[0][1]["action"] == "keep"
    c = next(c for c in mplan["report"]["conflicts"] if c["field"] == "unpaired")
    assert (c["slide"], c["element"]) == (held[0][0], held[0][1]["key"])
    assert c["resolution"] == "kept (tied to no object of the deck)" and not c.get("takeable")
    assert any("could not be tied to any object of this deck" in w and "Change them in the deck itself"
               in w for w in mplan["report"]["warnings"])
    assert adopt_sync.problems(base, mplan, world["live"], KEPT) == [], "nothing left to refuse"
    assert merge.has_writes(mplan, [s["objectId"] for s in world["live"]["slides"]]), \
        "and the rest of the deck is synced as usual"


def test_an_element_the_decks_layout_draws_is_named_for_what_it_is(world):
    """The same decision - keep it, write nothing, say so - told in the words that lead somewhere.
    "Could not be tied to any object of this deck. Change them in the deck itself" sends a person
    to look on the slide for a footer that is not on the slide; it is on the layout, and Slides
    has a door for that."""
    base, doc = unpair(world)
    el = next(e for s in base["slides"] for e in s["elements"] if not e["objects"])
    el["from_layout"] = True
    fuzz_sync.src_reword(random.Random(1), doc)            # ... and the source changes another slide too
    mplan = merge.plan_merge(base, W.build_ours(doc, base, world["out"]), world["live"])
    held = [(p["key"], u) for p in mplan["slides"] for u in p.get("units") or [] if u.get("inherited")]
    assert len(held) == 1 and held[0][1]["action"] == "keep"
    assert not any(u.get("unpaired") for p in mplan["slides"] for u in p.get("units") or [])
    c = next(c for c in mplan["report"]["conflicts"] if c["field"] == "inherited")
    assert (c["slide"], c["element"]) == (held[0][0], held[0][1]["key"])
    assert c["resolution"] == "kept (the deck's layout draws this, not the slide)"
    assert any("are drawn by this deck's layouts or its master, not by the slide" in w and
               "Slide > Edit theme" in w for w in mplan["report"]["warnings"])
    assert not any("could not be tied to any object" in w for w in mplan["report"]["warnings"])
    assert adopt_sync.problems(base, mplan, world["live"], KEPT) == [], "nothing left to refuse"
    assert merge.has_writes(mplan, [s["objectId"] for s in world["live"]["slides"]]), \
        "and the rest of the deck is synced as usual"


def test_the_gate_still_stands_behind_the_merge(world):
    """`merge.plan_unit` decides it, `adopt_sync.problems` is the last thing between a plan and a
    write into somebody's deck - for a plan that says recreate anyway, however it came to."""
    base, doc = unpair(world)
    mplan = merge.plan_merge(base, W.build_ours(doc, base, world["out"]), world["live"])
    u = next(u for p in mplan["slides"] for u in p.get("units") or [] if u.get("unpaired"))
    u["action"] = "recreate"
    message = refuse(base, mplan, world["live"], KEPT)
    assert "  - 1 element(s) the source changed could not be tied to any object of the deck: " in message
    assert "      Writing them would put a second object beside the person's, not over it." in message
    assert "    change those elements in the deck instead of in the source, and sync the rest" in message


def a_deck(width: float, height: float) -> dict:
    """A one-slide classify IR whose page is `width` x `height` in PDF pt, with one picture on it."""
    return {"slides": [{"page": 0, "size": [width, height], "notes": "", "background_color": "#ffffff",
                        "elements": [{"id": "p0i0", "kind": "image", "role": "figure", "bbox": [10, 20, 110, 70],
                                      "file": "figures/a.png"}]}]}


def test_a_deck_the_person_made_wider_is_planned_at_its_own_size():
    """The scale is the one number that carries this converter's PDF points onto the deck's page
    (`emit.DeckPlan.scale`), and it is what every box, font size and hole width is multiplied by. A
    deck a person built is whatever size they made it - 1440 x 810 is an ordinary Slides deck - so
    the plan is made for *that* page, not for the 720 pt one `convert` uploads."""
    from beamer2slides.emit import SLIDE_W, DeckPlan

    deck = a_deck(453.54, 255.12)
    ours, theirs = DeckPlan(deck), DeckPlan(deck, 1440.0)
    assert ours.page_width == SLIDE_W and ours.scale == pytest.approx(720.0 / 453.54)
    assert theirs.page_width == 1440.0 and theirs.scale == pytest.approx(1440.0 / 453.54)
    box = theirs.pictures(theirs.deck["slides"][0])[0][1]
    assert box == pytest.approx([v * 2 for v in ours.pictures(ours.deck["slides"][0])[0][1]]), \
        "twice the page, twice the box: the picture lands on the same part of the slide"


def test_a_wider_deck_is_not_refused_anymore(world):
    """What the width used to be refused for is now planned for, so a deck of an ordinary Slides
    size takes new objects like any other."""
    base = {**copy.deepcopy(world["base"]), "deck_page_size": [1440.0, 810.0]}
    assert adopt_sync.deck_width(base) == 1440.0
    assert adopt_sync.aspect_mismatch(base) is None, "1440 x 810 is the page the source compiles to, doubled"
    doc = copy.deepcopy(world["doc"])
    fuzz_sync.src_add_element(random.Random(2), doc)
    found = adopt_sync.problems(base, plan_of({**world, "base": base}, doc), world["live"], KEPT)
    assert [p["reason"] for p in found] == []


def test_a_deck_of_another_shape_than_the_source_compiles_to_is_refused(world):
    """One number cannot carry a 16:10 plan onto a 16:9 page: everything would land at the right
    place across the slide and the wrong one down it, which is exactly the kind of wrong nobody
    sees until the deck is read."""
    base = copy.deepcopy(world["base"])
    base["deck_page_size"] = [1440.0, 900.0]
    doc = copy.deepcopy(world["doc"])
    fuzz_sync.src_add_element(random.Random(2), doc)
    message = refuse(base, plan_of({**world, "base": base}, doc), world["live"], KEPT)
    assert ("  - the deck's slides are 1.600 wide for every 1 high and the page the source compiles to is 1.778,"
            in message)
    assert ("      so the 1 object(s) this sync would create land at the right place across and the wrong one down."
            in message)
    assert "    give the source back the paper adopt wrote for it (`\\geometry`, docs/sync.md)" in message


def test_the_page_shape_refusal_outlives_the_first_sync(world):
    """The way back and the deck's own slides are about a deck nothing has been written to yet.
    The shape of its page is not: it is the same on the fourth sync as on the first."""
    base = {**copy.deepcopy(world["base"]), "generation": 4, "deck_page_size": [1440.0, 900.0]}
    doc = copy.deepcopy(world["doc"])
    fuzz_sync.src_add_element(random.Random(2), doc)
    reasons = [p["reason"] for p in adopt_sync.problems(base, plan_of({**world, "base": base}, doc),
                                                        world["live"], None, "none")]
    assert reasons == ["page-shape"]


def test_an_unpaired_element_is_held_at_every_generation(world):
    """Nor does an unpaired element heal by itself: no sync ever writes it, so no sync ever gives
    it an object, so it is still unpaired at generation 4 and still held there. The offline
    campaign found this at chain depth 2, where the base rebased after the first sync let the
    second one duplicate the person's box (`fuzz_sync._doubled`)."""
    base, doc = unpair(world, generation=4)
    mplan = merge.plan_merge(base, W.build_ours(doc, base, world["out"]), world["live"])
    assert [u["action"] for p in mplan["slides"] for u in p.get("units") or [] if u.get("unpaired")] == ["keep"]
    assert adopt_sync.problems(base, mplan, world["live"], None, "none") == []


def test_a_sync_that_writes_nothing_is_never_refused(world):
    """Not one refusal is about reading the deck: a sync whose source says what the deck already
    says has nothing to vouch for."""
    assert adopt_sync.problems(world["base"], plan_of(world), world["live"], None, "auto") == []


def test_a_converted_deck_is_not_asked_any_of_this(world, tmp_path):
    """Every object of a converted deck is one this converter made, under an id it chose."""
    base = W.build_base(world["doc"], tmp_path)
    ours = W.build_ours(world["doc"], base, tmp_path)
    live = W.live_of(base)
    assert adopt_sync.problems(base, merge.plan_merge(base, ours, live), live, None, "auto") == []


# ---------------------------------------------------------------- the round trip

def round_trip(seed: int, chain: int = 1) -> dict:
    return fuzz_sync.offline_chain(seed, chain, shape="adopt", first_sync=True)


@pytest.mark.parametrize("seed", [0, 3, 11, 29, 57, 104, 211, 333])
def test_the_first_sync_after_an_adopt_loses_nothing(seed, tmp_path):
    """Adopt a deck, change the source, let a person edit the deck, sync: whatever the merge wrote,
    nothing the person put there is gone without the report accounting for it (`loss_oracle`), and
    nothing was written beside an object the base could not pair (`fuzz_sync._doubled`)."""
    result = fuzz_sync.offline_chain(seed, 1, shape="adopt", first_sync=True, work=tmp_path / str(seed))
    assert loss_oracle.describe(result["failures"]) == ""


@pytest.mark.parametrize("seed", [1, 8, 42, 77])
def test_a_chain_of_syncs_after_an_adopt_loses_nothing(seed, tmp_path):
    result = fuzz_sync.offline_chain(seed, 4, shape="adopt", first_sync=True, work=tmp_path / str(seed))
    assert loss_oracle.describe(result["failures"]) == ""


def test_the_campaign_sees_the_duplicate_the_unpaired_hold_prevents(tmp_path, monkeypatch):
    """The loss oracle cannot judge this one: nothing is lost when sync writes a second object
    beside the person's, because sync deletes the old ones through the base and an unpaired element
    names none. `fuzz_sync._doubled` is what sees it - and with the hold and the gate in place it
    never fires, so this is the test that it would. Both doors are opened: the merge stops
    recognising an adopted base, and the gate stops asking about unpaired elements."""
    monkeypatch.setattr(merge, "ADOPTED", "a word no base says")
    real = adopt_sync.problems
    monkeypatch.setattr(adopt_sync, "problems",
                        lambda *a, **kw: [p for p in real(*a, **kw) if p["reason"] != "unpaired"])
    monkeypatch.setattr(fuzz_sync.adopt_sync, "problems", adopt_sync.problems)
    failures = [f for seed in range(40)
                for f in fuzz_sync.offline_chain(seed, 1, shape="adopt", first_sync=True,
                                                 work=tmp_path / str(seed))["failures"]]
    assert [f["kind"] for f in failures].count("adopt_double") > 0
