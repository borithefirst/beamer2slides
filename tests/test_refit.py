"""Merged text into a recreated box (`beamer2slides.refit`): formula pictures back over their holes,
the box and the block panel under it as tall as the words written into them need, and a base that
records what that moved as the converter's doing. Synthetic read-backs laid out by the same model
(`text_layout`), so each answer is exact; Google's side is `tests/test_sync_live.py`
(layout-stranded-formula, layout-block-sentence, layout-block-font)."""

from beamer2slides import emit, refit, snapshot
from beamer2slides import text_layout as tl

NB = " "
Z = 18.0
LATO = {"fontFamily": "Lato", "fontSize": Z}
HOLE = {"fontFamily": emit.HOLE_FONT, "fontSize": Z}


def text_rb(parts, box, parent=None, z=5, size=Z):
    """A converter text box: parts = [(text, is_hole)], one paragraph."""
    text, spans = "", []
    for s, hole in parts:
        style = {**(HOLE if hole else LATO), "fontSize": size}
        spans.append([len(text), len(text) + len(s), style])
        text += s
    return {"kind": "shape", "shape_style": {"type": "TEXT_BOX", "align": "TOP"}, "text": text + "\n",
            "run_spans": spans, "paragraph_styles": [{"lineSpacing": 100}], "box": list(box),
            "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]], "size": [box[2] - box[0], box[3] - box[1]],
            "parent_group": parent, "z": z}


def shape_rb(box, fill="#dddddd", parent=None, z=1):
    return {"kind": "shape", "shape_style": {"type": "ROUND_RECTANGLE", "fill": {"color": fill, "alpha": 1.0}},
            "text": None, "box": list(box), "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]],
            "size": [box[2] - box[0], box[3] - box[1]], "parent_group": parent, "z": z}


def picture_over(rb, k=0, parent=None, w=30.0, h=20.0):
    """A formula picture where measuring puts it: centred on hole k, its middle on the band's."""
    lay = tl.layout(rb)
    hb = lay["holes"][k]["box"]
    cx, cy = (hb[0] + hb[2]) / 2, (hb[1] + hb[3]) / 2
    box = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
    return {"kind": "image", "box": box, "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]], "size": [w, h],
            "parent_group": parent, "z": 9}


def offset(rb, pic_box, k=0):
    hb = tl.layout(rb)["holes"][k]["box"]
    return ((pic_box[0] + pic_box[2]) / 2 - (hb[0] + hb[2]) / 2, (pic_box[1] + pic_box[3]) / 2 - (hb[1] + hb[3]) / 2)


def stepped(rb, step):
    m = snapshot.compose(step, rb["transform"])
    return {**rb, "transform": m, "box": snapshot.box(m, *rb["size"])}


def job(**kw):
    return {"key": "slide s: text/body/0", "text": "t", "pictures": [], "own": {"t"}, "doomed": set(),
            "theirs": None, **kw}


def apply_all(slide, reshaped):
    return {**slide, "objects": {oid: stepped(rb, reshaped[oid][0]) if oid in reshaped else rb
                                 for oid, rb in slide["objects"].items()}}


SOURCE = [("The distance ", False), (NB * 4, True), (" counts the words both sides changed.", False)]
MERGED = [("The perfectly exact distance ", False), (NB * 4, True),
          (" counts the words both sides changed.", False)]
BOX = [10.0, 100.0, 700.0, 140.0]


def test_a_formula_picture_follows_its_hole_into_the_words_as_merged():
    """The person's words before a formula: emit measured the hole in the source's text, the merged
    text pushes it 120 pt right. The picture goes where the hole now is, by the same offset."""
    pre_t = text_rb(SOURCE, BOX)
    pic = picture_over(pre_t)
    fin_t = text_rb(MERGED, BOX)
    pre = {"objects": {"t": pre_t, "p": pic}}
    fin = {"objects": {"t": fin_t, "p": pic}}
    reqs, reshaped, warnings = refit.plan([job(pictures=["p"], own={"t", "p"})], pre, fin, [720, 405])
    assert not warnings and list(reshaped) == ["p"] and len(reqs) == 1
    before = offset(fin_t, pic["box"])
    after = offset(fin_t, stepped(pic, reshaped["p"][0])["box"])
    assert abs(before[0]) > 80
    assert max(abs(after[0]), abs(after[1])) < 0.5
    t = reqs[0]["updatePageElementTransform"]
    assert t["objectId"] == "p" and t["applyMode"] == "RELATIVE"


def test_a_picture_follows_onto_the_next_line():
    """A merge that wraps the hole onto the next line moves the picture down a line too."""
    long = [("Words " * 18, False), (NB * 4, True), (" end.", False)]
    pre_t = text_rb(SOURCE, [10, 100, 700, 170])
    pic = picture_over(pre_t)
    fin_t = text_rb(long, [10, 100, 700, 170])
    reqs, reshaped, _ = refit.plan([job(pictures=["p"], own={"t", "p"})], {"objects": {"t": pre_t, "p": pic}},
                                   {"objects": {"t": fin_t, "p": pic}}, [720, 405])
    moved = stepped(pic, reshaped["p"][0])["box"]
    assert moved[1] > pic["box"][1] + 15
    assert max(map(abs, offset(fin_t, moved))) < 0.5


def test_inside_a_group_the_step_is_written_in_the_groups_frame():
    """A picture in the unit's group (or in a group the person scaled): RELATIVE composes with the
    child's own transform inside the group's, so the page step is conjugated by the group's."""
    group = [0.8, 0.0, 0.0, 0.8, 30.0, 12.0]
    step = [1, 0, 0, 1, 120.0, -7.0]
    local = refit.local_step(step, group)
    child = [1.0, 0.0, 0.0, 1.0, 5.0, 9.0]
    got = snapshot.compose(group, snapshot.compose(local, child))
    want = snapshot.compose(step, snapshot.compose(group, child))
    assert all(abs(a - b) < 1e-9 for a, b in zip(got, want))
    assert refit.local_step(step, None) == step


def test_a_hole_the_person_deleted_leaves_its_picture_and_says_so():
    pre_t = text_rb(SOURCE, BOX)
    pic = picture_over(pre_t)
    fin_t = text_rb([("The words around the formula went.", False)], BOX)
    reqs, reshaped, warnings = refit.plan([job(pictures=["p"], own={"t", "p"})], {"objects": {"t": pre_t, "p": pic}},
                                          {"objects": {"t": fin_t, "p": pic}}, [720, 405])
    assert not reqs and not reshaped
    assert len(warnings) == 1 and "lost its place" in warnings[0]


def test_what_the_geometry_alone_did_stays_the_persons():
    """The person narrowed the unit (the source's words rewrap under its pictures): the same words
    written back move nothing and grow nothing - that look is theirs (deck edits win)."""
    pre_t = text_rb(SOURCE, BOX)
    pic = picture_over(pre_t)
    fin_t = text_rb(SOURCE, [10, 100, 200, 140])
    reqs, reshaped, _ = refit.plan([job(pictures=["p"], own={"t", "p"})], {"objects": {"t": pre_t, "p": pic}},
                                   {"objects": {"t": fin_t, "p": pic}}, [720, 405])
    assert not reqs and not reshaped


SHORT = [("A source change is reported as a conflict.", False)]
LONG = [("A source change to a field the deck also edited is kept aside and listed as a conflict, "
         "and the deck adds this sentence of its own, which takes the words onto a second line.", False)]


def block(text_parts, below=None):
    """A block: its panel (drawn first), its body text on it, and maybe a text under the block."""
    body = [10.0, 170.0, 700.0, 170.0]
    rb = text_rb(text_parts, body)
    lay = tl.layout(rb)
    rb = text_rb(text_parts, [10.0, 170.0, 700.0, tl.needed_bottom(lay)])   # as tall as emit makes it
    objects = {"panel": shape_rb([11.0, 150.0, 709.0, rb["box"][3] + 1.0]), "t": rb}
    if below is not None:
        objects["next"] = text_rb([(below, False)], [10.0, 224.0, 700.0, 255.0], z=7)
    return {"objects": objects}


def test_a_box_and_its_panel_grow_with_the_words_written_into_them():
    """The person's longer sentence merged into a block body emit sized for the source's one line:
    the box grows to what emit would have made it, the panel keeps its margin under the words."""
    pre = block(SHORT)
    fin = {"objects": {**pre["objects"], "t": text_rb(LONG, pre["objects"]["t"]["box"])}}
    reqs, reshaped, warnings = refit.plan([job()], pre, fin, [720, 405])
    assert not warnings and set(reshaped) == {"t", "panel"}
    grown = apply_all(fin, reshaped)["objects"]
    lay = tl.layout(grown["t"])
    assert len(lay["lines"]) == 2
    assert abs(grown["t"]["box"][3] - tl.needed_bottom(lay)) < 0.05
    assert grown["t"]["box"][1] == fin["objects"]["t"]["box"][1]          # grown downwards
    pad = pre["objects"]["panel"]["box"][3] - tl.layout(pre["objects"]["t"])["bottom"]
    assert abs(grown["panel"]["box"][3] - (lay["bottom"] + pad)) < 0.05
    assert grown["panel"]["box"][1] == 150.0


def test_a_larger_font_grows_them_too():
    pre = block(SHORT)
    rb = text_rb(SHORT, pre["objects"]["t"]["box"], size=30)
    fin = {"objects": {**pre["objects"], "t": rb}}
    _, reshaped, _ = refit.plan([job()], pre, fin, [720, 405])
    grown = apply_all(fin, reshaped)["objects"]
    assert grown["t"]["box"][3] >= tl.needed_bottom(tl.layout(grown["t"])) - 0.05
    assert grown["panel"]["box"][3] > tl.layout(grown["t"])["bottom"]


def test_a_panel_stops_short_of_the_words_below_it_and_the_report_says_so():
    pre = block(SHORT, below="Both versions go into the report.")
    fin = {"objects": {**pre["objects"], "t": text_rb(LONG * 2, pre["objects"]["t"]["box"])}}
    _, reshaped, warnings = refit.plan([job()], pre, fin, [720, 405])
    grown = apply_all(fin, reshaped)["objects"]
    ink_top = min(ln["box"][1] for ln in tl.layout(grown["next"])["lines"])
    assert grown["panel"]["box"][3] <= ink_top - refit.CLEAR + 0.05
    assert len(warnings) == 1 and "'Both versions go into the report.'" in warnings[0]


def test_the_persons_own_overflow_stays_theirs():
    """Before the sync the person's font already ran the words out of the box and panel: the merge
    changes nothing about that, so nothing grows."""
    pre = block(SHORT)
    theirs = text_rb(SHORT, pre["objects"]["t"]["box"], size=30)
    fin = {"objects": {**pre["objects"], "t": text_rb(SHORT, pre["objects"]["t"]["box"], size=30)}}
    reqs, reshaped, _ = refit.plan([job(theirs=theirs)], pre, fin, [720, 405],
                                   before={"objects": {"panel": pre["objects"]["panel"], "old": theirs}})
    assert not reqs and not reshaped


def test_a_box_is_not_grown_off_the_page():
    rb = text_rb(SHORT, [600, 380, 700, 400])
    fin_rb = text_rb(LONG, [600, 380, 700, 400])
    _, reshaped, warnings = refit.plan([job()], {"objects": {"t": rb}}, {"objects": {"t": fin_rb}}, [720, 405])
    grown = apply_all({"objects": {"t": fin_rb}}, reshaped)["objects"]["t"]
    assert grown["box"][3] <= 405.05
    assert any("the page ends" in w for w in warnings)


def test_the_base_records_the_step_as_the_converters():
    """R' = R . F^-1 . S . F: the person's own step E (deck = E . base) stays the same on every
    member of the unit, so the next sync reads the picture as following its unit, and the group's
    box is its children's union again. The old base's dicts are not touched."""
    person = [1, 0, 0, 1, 0.0, 40.0]                  # the person moved the unit 40 pt down
    r_pic = {"kind": "image", "transform": [1.0, 0, 0, 1.0, 400.0, 110.0], "size": [30.0, 20.0],
             "box": [400.0, 110.0, 430.0, 130.0], "parent_group": "t_g"}
    r_text = {"kind": "shape", "transform": [1.0, 0, 0, 1.0, 10.0, 100.0], "size": [690.0, 40.0],
              "box": [10.0, 100.0, 700.0, 140.0], "parent_group": "t_g"}
    r_group = {"kind": "elementGroup", "transform": [1, 0, 0, 1, 0, 0], "size": [0, 0],
               "box": [10.0, 100.0, 700.0, 140.0], "children": ["t", "p"]}
    slides = [{"key": "s", "elements": [{"key": "text/body/0", "main": "t", "objects": ["t", "t_g"],
                                         "readback": {"t": r_text, "t_g": r_group}},
                                        {"key": "image/math/0", "main": "p", "objects": ["p"], "readback": {"p": r_pic}}]}]
    f = snapshot.compose(person, r_pic["transform"])     # where the picture stood after the overrides
    step = [1, 0, 0, 1, 250.0, 25.0]                      # and the refit step on the page
    out = refit.reshape_base(slides, {"p": (step, f)})
    new_pic = out[0]["elements"][1]["readback"]["p"]
    deck = snapshot.compose(step, f)
    assert all(abs(a - b) < 1e-6 for a, b in zip(snapshot.compose(person, new_pic["transform"]), deck))
    assert new_pic["box"] == [650.0, 135.0, 680.0, 155.0]
    assert out[0]["elements"][0]["readback"]["t_g"]["box"] == [10.0, 100.0, 700.0, 155.0]
    assert slides[0]["elements"][1]["readback"]["p"]["box"] == [400.0, 110.0, 430.0, 130.0]
    assert refit.reshape_base(slides, {}) is slides


def test_pictures_pair_with_the_holes_they_stand_over():
    """More pictures than holes (the person deleted the words holding one): the one far from every
    hole is not given one."""
    two = [("A ", False), (NB * 4, True), (" and ", False), (NB * 4, True), (" z.", False)]
    rb = text_rb(two, BOX)
    lay = tl.layout(rb)
    pics = {"p0": picture_over(rb, 0), "p1": picture_over(rb, 1),
            "stray": {**picture_over(rb, 0), "box": [600.0, 300.0, 630.0, 320.0]}}
    assert refit.pair_pictures(lay, pics) == {"p0": 0, "p1": 1}
