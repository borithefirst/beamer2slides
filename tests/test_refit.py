"""Merged text into a recreated box (`beamer2slides.refit`): formula pictures back over their holes,
the box and the block panel under it as tall as the words written into them need, and a base that
records what that moved as the converter's doing. Synthetic read-backs laid out by the same model
(`text_layout`), so each answer is exact; Google's side is `tests/test_sync_live.py`
(layout-stranded-formula, layout-block-sentence, layout-block-font)."""

from collections.abc import Collection, Mapping, Sequence

from beamer2slides import emit, refit, snapshot
from beamer2slides import text_layout as tl
from beamer2slides.json_types import Json, JsonObject

from .json_reads import jat, jnum, jnums, jobj, jstr

NB = " "
Z = 18.0
LATO: JsonObject = {"fontFamily": "Lato", "fontSize": Z}
HOLE: JsonObject = {"fontFamily": emit.HOLE_FONT, "fontSize": Z}
PAGE = [720, 405]

Parts = list[tuple[str, bool]]
"""A paragraph's pieces: (text, is it a formula hole)."""


def text_box(parts: Parts, box: Sequence[float], z: int, size: float) -> JsonObject:
    """A converter text box: parts = [(text, is_hole)], one paragraph, at stacking `z`, in `size` pt."""
    text = ""
    spans: list[Json] = []
    for s, hole in parts:
        style: JsonObject = {**(HOLE if hole else LATO), "fontSize": size}
        spans.append([len(text), len(text) + len(s), style])
        text += s
    return {"kind": "shape", "shape_style": {"type": "TEXT_BOX", "align": "TOP"}, "text": text + "\n",
            "run_spans": spans, "paragraph_styles": [{"lineSpacing": 100}], "box": [*box],
            "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]], "size": [box[2] - box[0], box[3] - box[1]],
            "parent_group": None, "z": z}


def text_rb(parts: Parts, box: Sequence[float]) -> JsonObject:
    """`text_box` at the body size, where a converter text stacks."""
    return text_box(parts, box, 5, Z)


def shape_rb(box: Sequence[float]) -> JsonObject:
    return {"kind": "shape", "shape_style": {"type": "ROUND_RECTANGLE", "fill": {"color": "#dddddd", "alpha": 1.0}},
            "text": None, "box": [*box], "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]],
            "size": [box[2] - box[0], box[3] - box[1]], "parent_group": None, "z": 1}


def laid(rb: JsonObject) -> tl.Layout:
    lay = tl.layout(rb)
    assert lay is not None
    return lay


def box_of(rb: Json) -> list[float]:
    return jnums(rb, "box")


def obj(slide: JsonObject, oid: str) -> JsonObject:
    """The object `oid` of a slide read-back."""
    return jobj(slide, "objects", oid)


def picture_over(rb: JsonObject, k: int) -> JsonObject:
    """A 30 x 20 formula picture where measuring puts it: centred on hole k, its middle on the band's."""
    w, h = 30.0, 20.0
    hb = laid(rb).holes[k].box
    cx, cy = (hb[0] + hb[2]) / 2, (hb[1] + hb[3]) / 2
    box: list[Json] = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
    return {"kind": "image", "box": box, "transform": [1.0, 0.0, 0.0, 1.0, cx - w / 2, cy - h / 2], "size": [w, h],
            "parent_group": None, "z": 9}


def offset(rb: JsonObject, pic_box: Sequence[float]) -> tuple[float, float]:
    hb = laid(rb).holes[0].box
    return ((pic_box[0] + pic_box[2]) / 2 - (hb[0] + hb[2]) / 2, (pic_box[1] + pic_box[3]) / 2 - (hb[1] + hb[3]) / 2)


def stepped(rb: JsonObject, step: Sequence[float]) -> JsonObject:
    m = snapshot.compose([*step], jnums(rb, "transform"))
    return {**rb, "transform": [*m], "box": [*snapshot.box(m, *jnums(rb, "size"))]}


def job(pictures: Sequence[str], own: Collection[str], theirs: JsonObject | None, slide: str | None,
        names: Mapping[str, str]) -> refit.RefitJob:
    return refit.RefitJob(key="slide s: text/body/0", slide=slide, names=names, text="t",
                          pictures=tuple(pictures), own=frozenset(own), doomed=frozenset(), theirs=theirs)


def a_job() -> refit.RefitJob:
    """The text `t` alone, nothing anchored to it."""
    return job((), ("t",), None, None, {})


def with_pictures(own: Collection[str]) -> refit.RefitJob:
    """The text `t` and its picture `p`, the unit holding `own`."""
    return job(["p"], own, None, None, {})


def apply_all(slide: JsonObject, reshaped: Mapping[str, refit.Reshape]) -> JsonObject:
    return {**slide, "objects": {oid: stepped(jobj(rb), reshaped[oid].step) if oid in reshaped else rb
                                 for oid, rb in jobj(slide, "objects").items()}}


SOURCE: Parts = [("The distance ", False), (NB * 4, True), (" counts the words both sides changed.", False)]
MERGED: Parts = [("The perfectly exact distance ", False), (NB * 4, True),
                 (" counts the words both sides changed.", False)]
BOX = [10.0, 100.0, 700.0, 140.0]


def test_a_formula_picture_follows_its_hole_into_the_words_as_merged() -> None:
    """The person's words before a formula: emit measured the hole in the source's text, the merged
    text pushes it 120 pt right. The picture goes where the hole now is, by the same offset."""
    pre_t = text_rb(SOURCE, BOX)
    pic = picture_over(pre_t, 0)
    fin_t = text_rb(MERGED, BOX)
    pre: JsonObject = {"objects": {"t": pre_t, "p": pic}}
    fin: JsonObject = {"objects": {"t": fin_t, "p": pic}}
    reqs, reshaped, warnings = refit.plan([with_pictures({"t", "p"})], pre, fin, PAGE, None)
    assert not warnings and list(reshaped) == ["p"] and len(reqs) == 1
    before = offset(fin_t, box_of(pic))
    after = offset(fin_t, box_of(stepped(pic, reshaped["p"].step)))
    assert abs(before[0]) > 80
    assert max(abs(after[0]), abs(after[1])) < 0.5
    t = jobj(reqs[0], "updatePageElementTransform")
    assert t["objectId"] == "p" and t["applyMode"] == "RELATIVE"


def test_a_picture_follows_onto_the_next_line() -> None:
    """A merge that wraps the hole onto the next line moves the picture down a line too."""
    long: Parts = [("Words " * 18, False), (NB * 4, True), (" end.", False)]
    pre_t = text_rb(SOURCE, [10, 100, 700, 170])
    pic = picture_over(pre_t, 0)
    fin_t = text_rb(long, [10, 100, 700, 170])
    reqs, reshaped, _ = refit.plan([with_pictures({"t", "p"})], {"objects": {"t": pre_t, "p": pic}},
                                   {"objects": {"t": fin_t, "p": pic}}, PAGE, None)
    moved = box_of(stepped(pic, reshaped["p"].step))
    assert moved[1] > box_of(pic)[1] + 15
    assert max(map(abs, offset(fin_t, moved))) < 0.5


def request_matrix(r: JsonObject) -> list[float]:
    t = jobj(r, "updatePageElementTransform", "transform")
    return [jnum(t, "scaleX"), jnum(t, "shearX"), jnum(t, "shearY"), jnum(t, "scaleY"),
            jnum(t, "translateX") / snapshot.EMU_PER_PT, jnum(t, "translateY") / snapshot.EMU_PER_PT]


def close(a: Sequence[float], b: Sequence[float], tol: float) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def in_group(slide: JsonObject, group_t: list[float], members: list[str]) -> JsonObject:
    """The slide's members as children of a group whose transform is `group_t` (read-backs are
    absolute: `snapshot.read_slide` composes the group's in)."""
    out: JsonObject = {oid: {**stepped(jobj(rb), group_t), "parent_group": "g"} if oid in members else rb
                       for oid, rb in jobj(slide, "objects").items()}
    kids = [box_of(out[m]) for m in members]
    out["g"] = {"kind": "elementGroup", "transform": [*group_t], "size": [0, 0], "children": [*members],
                "box": [min(k[0] for k in kids), min(k[1] for k in kids), max(k[2] for k in kids), max(k[3] for k in kids)]}
    return {"objects": out}


def test_a_groups_child_takes_the_step_in_page_space() -> None:
    """Slides applies a RELATIVE transform to a group's child on its absolute transform, in page
    space, whatever the group's own transform. Conjugated by the group's (G^-1 . S . G), as refit
    once wrote it, it was right only on the converter's identity groups: live fuzz r8006, a block
    body grown about its top in a group the person had moved (+20, -20) had its top rise 4.0 pt
    (101.5 for the promised 105.48); r8011, a picture in a group scaled 1.15 moved 134.0 pt of a
    planned 154.1. The request is the page step itself, for a picture and for a growing box."""
    person = [1, 0, 0, 1, 20.0, -20.0]
    pre = block(SOURCE, None)
    jobj(pre, "objects")["p"] = picture_over(obj(pre, "t"), 0)
    fin = in_group(pre, person, ["t", "p", "panel"])
    longer = MERGED[:2] + [(MERGED[2][0] + " And a sentence of the person's, long enough to take the words"
                            " onto a second line of the box.", False)]
    jobj(fin, "objects")["t"] = {**text_rb(longer, box_of(obj(fin, "t"))), "parent_group": "g"}
    reqs, reshaped, warnings = refit.plan([with_pictures({"t", "p", "g"})], pre, fin, PAGE, None)
    assert not warnings and set(reshaped) == {"t", "p", "panel"}
    deck = dict(jobj(fin, "objects"))
    for r in reqs:                                   # what Slides does with each: M . absolute
        oid = jstr(r, "updatePageElementTransform", "objectId")
        assert close(request_matrix(r), reshaped[oid].step, 1e-3)
        deck[oid] = stepped(jobj(deck[oid]), request_matrix(r))
    assert jnums(deck, "t", "transform")[3] > 1.05                               # it did grow
    assert abs(box_of(deck["t"])[1] - box_of(obj(fin, "t"))[1]) < 0.01          # about its own top
    assert abs(box_of(deck["panel"])[1] - box_of(obj(fin, "panel"))[1]) < 0.01
    assert max(map(abs, offset(jobj(deck["t"]), box_of(deck["p"])))) < 0.5


def test_in_a_group_the_person_scaled_a_picture_keeps_the_persons_scale() -> None:
    """r8011: the person scaled the unit's group 1.15; the merge moves the hole. The hole's move is
    measured in the converter's box and the person's scale put on top of it (E . T . E^-1), so the
    deck stays E times what the converter would have made of the merged words. A group's scale is
    its box's size, not its text's (Slides keeps a shape's size in its scale): measured in the
    person's wider box, the move comes out unscaled and the picture slides 0.15 of it against the
    unit, sync after sync (r8011: 23 pt). How far the person's resize alone strands it stays theirs."""
    person = [1.15, 0, 0, 1.15, -4.23, -18.82]
    pre_t = text_rb(SOURCE, BOX)
    pre: JsonObject = {"objects": {"t": pre_t, "p": picture_over(pre_t, 0)}}
    fin = in_group(pre, person, ["t", "p"])
    jobj(fin, "objects")["t"] = {**stepped(text_rb(MERGED, BOX), person), "parent_group": "g"}
    reqs, reshaped, warnings = refit.plan([with_pictures({"t", "p", "g"})], pre, fin, PAGE, None)
    assert not warnings and list(reshaped) == ["p"] and len(reqs) == 1
    moved = stepped(obj(fin, "p"), request_matrix(reqs[0]))
    dx = offset(text_rb(MERGED, BOX), box_of(obj(pre, "p")))[0]       # the hole's move, converter's frame
    want = snapshot.compose(person, snapshot.compose([1, 0, 0, 1, -dx, 0], jnums(obj(pre, "p"), "transform")))
    assert abs(dx) > 80 and close(jnums(moved, "transform"), want, 0.05)
    converters = picture_over(text_rb(MERGED, BOX), 0)                          # emit's, for the merged words
    assert close(jnums(moved, "transform"), snapshot.compose(person, jnums(converters, "transform")), 0.05)
    slides: list[JsonObject] = [{"key": "s", "elements": [{"key": "image/math/0", "main": "p", "objects": ["p"],
                                                           "readback": {"p": obj(pre, "p")}}]}]
    note = jobj(refit.reshape_base(slides, reshaped)[0], "elements", 0, "readback", "p")
    shift = jnums(note, "refit")
    assert abs(shift[0] + dx) < 0.05 and abs(shift[1]) < 0.05   # the base frame's shift
    assert close(snapshot.compose(person, jnums(note, "transform")), jnums(moved, "transform"), 0.05)
    said = refit.moves([job(["p"], ("t",), None, "s", {"t": "text/body/0", "p": "image/math/0"})],
                       reshaped, pre, fin)                                  # the report's `refit`
    assert [refit.moved_json(m) for m in said] == [{"slide": "s", "element": "image/math/0", "object": "p", "what": "picture",
                     "shift": note["refit"], "grown": 0.0}]


def test_a_hole_the_person_deleted_leaves_its_picture_and_says_so() -> None:
    pre_t = text_rb(SOURCE, BOX)
    pic = picture_over(pre_t, 0)
    fin_t = text_rb([("The words around the formula went.", False)], BOX)
    reqs, reshaped, warnings = refit.plan([with_pictures({"t", "p"})], {"objects": {"t": pre_t, "p": pic}},
                                          {"objects": {"t": fin_t, "p": pic}}, PAGE, None)
    assert not reqs and not reshaped
    assert len(warnings) == 1 and "lost its place" in warnings[0]


def test_what_the_geometry_alone_did_stays_the_persons() -> None:
    """The person narrowed the unit (the source's words rewrap under its pictures): the same words
    written back move nothing and grow nothing - that look is theirs (deck edits win)."""
    pre_t = text_rb(SOURCE, BOX)
    pic = picture_over(pre_t, 0)
    fin_t = text_rb(SOURCE, [10, 100, 200, 140])
    reqs, reshaped, _ = refit.plan([with_pictures({"t", "p"})], {"objects": {"t": pre_t, "p": pic}},
                                   {"objects": {"t": fin_t, "p": pic}}, PAGE, None)
    assert not reqs and not reshaped


SHORT: Parts = [("A source change is reported as a conflict.", False)]
LONG: Parts = [("A source change to a field the deck also edited is kept aside and listed as a conflict, "
                "and the deck adds this sentence of its own, which takes the words onto a second line.", False)]


def block(text_parts: Parts, below: str | None) -> JsonObject:
    """A block: its panel (drawn first), its body text on it, and maybe a text under the block."""
    body = [10.0, 170.0, 700.0, 170.0]
    lay = laid(text_rb(text_parts, body))
    rb = text_rb(text_parts, [10.0, 170.0, 700.0, tl.needed_bottom(lay)])   # as tall as emit makes it
    objects: JsonObject = {"panel": shape_rb([11.0, 150.0, 709.0, box_of(rb)[3] + 1.0]), "t": rb}
    if below is not None:
        objects["next"] = text_box([(below, False)], [10.0, 224.0, 700.0, 255.0], 7, Z)
    return {"objects": objects}


def test_a_box_and_its_panel_grow_with_the_words_written_into_them() -> None:
    """The person's longer sentence merged into a block body emit sized for the source's one line:
    the box grows to what emit would have made it, the panel keeps its margin under the words."""
    pre = block(SHORT, None)
    fin: JsonObject = {"objects": {**jobj(pre, "objects"), "t": text_rb(LONG, box_of(obj(pre, "t")))}}
    reqs, reshaped, warnings = refit.plan([a_job()], pre, fin, PAGE, None)
    assert not warnings and set(reshaped) == {"t", "panel"}
    grown = jobj(apply_all(fin, reshaped), "objects")
    lay = laid(jobj(grown, "t"))
    assert len(lay.lines) == 2
    assert abs(box_of(grown["t"])[3] - tl.needed_bottom(lay)) < 0.05
    assert box_of(grown["t"])[1] == box_of(obj(fin, "t"))[1]          # grown downwards
    pad = box_of(obj(pre, "panel"))[3] - laid(obj(pre, "t")).bottom
    assert abs(box_of(grown["panel"])[3] - (lay.bottom + pad)) < 0.05
    assert box_of(grown["panel"])[1] == 150.0


def test_a_larger_font_grows_them_too() -> None:
    pre = block(SHORT, None)
    rb = text_box(SHORT, box_of(obj(pre, "t")), 5, 30)
    fin: JsonObject = {"objects": {**jobj(pre, "objects"), "t": rb}}
    _, reshaped, _ = refit.plan([a_job()], pre, fin, PAGE, None)
    grown = jobj(apply_all(fin, reshaped), "objects")
    assert box_of(grown["t"])[3] >= tl.needed_bottom(laid(jobj(grown, "t"))) - 0.05
    assert box_of(grown["panel"])[3] > laid(jobj(grown, "t")).bottom


def test_a_placeholder_titles_own_box_never_grows_but_its_panel_does() -> None:
    """A recreated title goes back into its live placeholder, at the box already found there - it
    may be the person's own resize, never the converter's to grow (sync.py's `refit_jobs`,
    `in_place`). The panel a theme swap drew under it is the converter's own object, and still
    grows to the person's larger font (edit hunt h5-6: a title enlarged in the deck, then a theme
    swap that added a panel under it, left the title's second line hanging 29.8 pt below the panel
    with nothing said)."""
    pre = block(SHORT, None)
    jobj(pre, "objects")["t"] = {**obj(pre, "t"), "placeholder": "CENTERED_TITLE"}
    fin_t: JsonObject = {**text_box(SHORT, box_of(obj(pre, "t")), 5, 30), "placeholder": "CENTERED_TITLE"}
    fin: JsonObject = {"objects": {**jobj(pre, "objects"), "t": fin_t}}
    _, reshaped, warnings = refit.plan([a_job()], pre, fin, PAGE, None)
    assert "t" not in reshaped
    assert not warnings
    assert "panel" in reshaped
    grown = jobj(apply_all(fin, reshaped), "objects")
    assert jat(grown, "t", "box") == fin_t["box"]                       # its own box untouched
    assert box_of(grown["panel"])[3] > laid(jobj(grown, "t")).bottom


def test_a_panel_stops_short_of_the_words_below_it_and_the_report_says_so() -> None:
    pre = block(SHORT, "Both versions go into the report.")
    fin: JsonObject = {"objects": {**jobj(pre, "objects"), "t": text_rb(LONG * 2, box_of(obj(pre, "t")))}}
    _, reshaped, warnings = refit.plan([a_job()], pre, fin, PAGE, None)
    grown = jobj(apply_all(fin, reshaped), "objects")
    ink_top = min(ln.box[1] for ln in laid(jobj(grown, "next")).lines)
    assert box_of(grown["panel"])[3] <= ink_top - refit.CLEAR + 0.05
    assert len(warnings) == 1 and "'Both versions go into the report.'" in warnings[0]


def test_the_persons_own_overflow_stays_theirs() -> None:
    """Before the sync the person's font already ran the words out of the box and panel: the merge
    changes nothing about that, so nothing grows."""
    pre = block(SHORT, None)
    theirs = text_box(SHORT, box_of(obj(pre, "t")), 5, 30)
    fin: JsonObject = {"objects": {**jobj(pre, "objects"), "t": text_box(SHORT, box_of(obj(pre, "t")), 5, 30)}}
    reqs, reshaped, _ = refit.plan([job((), ("t",), theirs, None, {})], pre, fin, PAGE,
                                   {"objects": {"panel": obj(pre, "panel"), "old": theirs}})
    assert not reqs and not reshaped


def test_a_box_is_not_grown_off_the_page() -> None:
    rb = text_rb(SHORT, [600, 380, 700, 400])
    fin_rb = text_rb(LONG, [600, 380, 700, 400])
    _, reshaped, warnings = refit.plan([a_job()], {"objects": {"t": rb}}, {"objects": {"t": fin_rb}}, PAGE, None)
    grown = obj(apply_all({"objects": {"t": fin_rb}}, reshaped), "t")
    assert box_of(grown)[3] <= 405.05
    assert any("the page ends" in w for w in warnings)


def test_the_base_records_the_step_as_the_converters() -> None:
    """R' = R . F^-1 . S . F: the person's own step E (deck = E . base) stays the same on every
    member of the unit, so the next sync reads the picture as following its unit, and the group's
    box is its children's union again. The old base's dicts are not touched."""
    person = [1, 0, 0, 1, 0.0, 40.0]                  # the person moved the unit 40 pt down
    r_pic: JsonObject = {"kind": "image", "transform": [1.0, 0, 0, 1.0, 400.0, 110.0], "size": [30.0, 20.0],
                         "box": [400.0, 110.0, 430.0, 130.0], "parent_group": "t_g"}
    r_text: JsonObject = {"kind": "shape", "transform": [1.0, 0, 0, 1.0, 10.0, 100.0], "size": [690.0, 40.0],
                          "box": [10.0, 100.0, 700.0, 140.0], "parent_group": "t_g"}
    r_group: JsonObject = {"kind": "elementGroup", "transform": [1, 0, 0, 1, 0, 0], "size": [0, 0],
                           "box": [10.0, 100.0, 700.0, 140.0], "children": ["t", "p"]}
    slides: list[JsonObject] = [{"key": "s", "elements": [
        {"key": "text/body/0", "main": "t", "objects": ["t", "t_g"], "readback": {"t": r_text, "t_g": r_group}},
        {"key": "image/math/0", "main": "p", "objects": ["p"], "readback": {"p": r_pic}}]}]
    f = snapshot.compose(person, jnums(r_pic, "transform"))     # where the picture stood after the overrides
    step = [1, 0, 0, 1, 250.0, 25.0]                            # and the refit step on the page
    out = refit.reshape_base(slides, {"p": refit.Reshape(step=step, before=f)})
    new_pic = jobj(out[0], "elements", 1, "readback", "p")
    deck = snapshot.compose(step, f)
    assert all(abs(a - b) < 1e-6 for a, b in zip(snapshot.compose(person, jnums(new_pic, "transform")), deck))
    assert new_pic["box"] == [650.0, 135.0, 680.0, 155.0]
    assert jat(out[0], "elements", 0, "readback", "t_g", "box") == [10.0, 100.0, 700.0, 155.0]
    assert jat(slides[0], "elements", 1, "readback", "p", "box") == [400.0, 110.0, 430.0, 130.0]
    assert refit.reshape_base(slides, {}) is slides


def test_pictures_pair_with_the_holes_they_stand_over() -> None:
    """More pictures than holes (the person deleted the words holding one): the one far from every
    hole is not given one."""
    two: Parts = [("A ", False), (NB * 4, True), (" and ", False), (NB * 4, True), (" z.", False)]
    rb = text_rb(two, BOX)
    lay = laid(rb)
    pics: dict[str, JsonObject] = {"p0": picture_over(rb, 0), "p1": picture_over(rb, 1),
                                   "stray": {**picture_over(rb, 0), "box": [600.0, 300.0, 630.0, 320.0]}}
    assert refit.pair_pictures(lay, pics) == {"p0": 0, "p1": 1}
