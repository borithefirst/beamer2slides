"""Theme sync (theme_sync.py): the master, the layouts' decoration pictures and their placeholders'
styles, merged three ways - offline, on the sync test talk and a made-up deck read."""

import copy
import json
from pathlib import Path

import pytest

from beamer2slides import emit, identity, snapshot, sync, theme_sync
from beamer2slides.theme_sync import DECORATION

SYNC_DECKS = Path(__file__).resolve().parent / "decks" / "sync" / "out"
EMU = 12700


def need(*names):
    missing = [n for n in names if not (SYNC_DECKS / f"{n}.pdf").exists()]
    if missing:
        pytest.skip(f"build the sync test talk first (tests/decks/sync/build.py): {missing}")


# ---------------------------------------------------------------- a made-up deck read

def size(w, h):
    return {"width": {"magnitude": w * EMU, "unit": "EMU"}, "height": {"magnitude": h * EMU, "unit": "EMU"}}


def at(x, y):
    return {"scaleX": 1, "scaleY": 1, "translateX": x * EMU, "translateY": y * EMU, "unit": "EMU"}


def picture(oid, url="https://lh3.example/deco-a=s0", x=0.0):
    return {"objectId": oid, "size": size(720, 405), "transform": at(x, 0), "image": {"contentUrl": url},
            "description": DECORATION}


def placeholder(oid, kind, colour=None, y=10.0):
    style = {"foregroundColor": {"opaqueColor": {"rgbColor": colour}}} if colour else {}
    return {"objectId": oid, "size": size(600, 60), "transform": at(20, y),
            "shape": {"placeholder": {"type": kind}, "text": {"textElements": [
                {"endIndex": 1, "paragraphMarker": {"style": {}}},
                {"endIndex": 1, "textRun": {"content": "\n", "style": style}}]}}}


def layout(oid, name, elements):
    return {"objectId": oid, "layoutProperties": {"name": name, "displayName": name.title().replace("_", " ")},
            "pageElements": elements}


def made_up_deck(n_slides):
    """The deck `convert` makes of the sync test talk, as presentations.get gives it: the master,
    three layouts carrying the decoration, a title slide and frames on TITLE_ONLY."""
    return {"presentationId": "P", "revisionId": "r1", "pageSize": size(720, 405),
            "masters": [{"objectId": "M", "pageProperties": {"pageBackgroundFill": {"solidFill": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}},
                         "pageElements": [placeholder("M_t", "TITLE"), placeholder("M_b", "BODY", y=100)]}],
            "layouts": [layout("LT", "TITLE", [picture("LT_d"), placeholder("LT_t", "CENTERED_TITLE"), placeholder("LT_s", "SUBTITLE")]),
                        layout("LO", "TITLE_ONLY", [picture("LO_d"), placeholder("LO_t", "TITLE")]),
                        layout("LB", "BLANK", [picture("LB_d")])],
            "slides": [{"objectId": f"S{i}", "slideProperties": {"layoutObjectId": "LT" if i == 0 else "LO"},
                        "pageProperties": {"pageBackgroundFill": {"propertyState": "INHERIT"}}, "pageElements": []}
                       for i in range(n_slides)]}


@pytest.fixture(scope="module")
def talk(tmp_path_factory):
    """v1 and retheme of the sync talk, the base's theme as `convert` records it on the made-up
    deck, and the retheme's side."""
    need("v1", "retheme")
    tmp = tmp_path_factory.mktemp("theme")
    v1 = sync.build_ours(SYNC_DECKS / "v1.pdf", tmp / "v1", {"slides": []})
    side1 = theme_sync.ours_side(v1)
    out = Path(v1["out"])
    pres = made_up_deck(len(v1["deck"]["slides"]))
    state = {"scale": v1["plan"].scale, "slides": [{"objectId": f"S{i}"} for i in range(len(v1["deck"]["slides"]))],
             "theme": {"ground": "#ffffff", "master": "#ffffff",
                       "decorations": {g: str(Path(p["path"]).relative_to(out)) for g, p in side1["pictures"].items() if p},
                       "layouts": {str(s["page"]): emit.slide_layout(s)[0] for s in v1["deck"]["slides"]}}}
    rec = theme_sync.record(v1["deck"], out, pres, state)
    slides = copy.deepcopy(v1["slides"])
    for i, s in enumerate(slides):
        s["objectId"], s["layoutObjectId"] = f"S{i}", "LT" if i == 0 else "LO"
    base = {"slides": slides, "theme": rec, "master_background": side1["shared"]}
    retheme = sync.build_ours(SYNC_DECKS / "retheme.pdf", tmp / "retheme", {"slides": slides})
    return {"v1": v1, "side1": side1, "pres": pres, "base": base, "retheme": retheme,
            "side2": theme_sync.ours_side(retheme)}


def plan(talk, pres=None, side=None, ours=None, base=None):
    return theme_sync.plan(base or talk["base"], side or talk["side2"], ours or talk["retheme"],
                           pres or talk["pres"], "1ab", lambda p: f"url:{Path(p).name}", lambda page: f"new_{page}")


def ops(reqs):
    return sorted((next(iter(r)), next(iter(r.values())).get("objectId") or next(iter(r.values())).get("imageObjectId"))
                  for r in reqs)


# ---------------------------------------------------------------- what convert wrote, as data

def test_the_spec_writes_what_style_layout_placeholders_writes(talk):
    """layout_style_spec + layout_placeholder_requests are the same requests, placeholder by
    placeholder, as the pass convert runs (so a sync writes what a fresh conversion would)."""
    v1 = talk["v1"]
    pres = talk["pres"]
    sent = []

    class Call:
        def __init__(self, answer):
            self.answer = answer

        def execute(self):
            return self.answer

    class Presentations:
        def get(self, **_):
            return Call(pres)

        def batchUpdate(self, presentationId, body):
            sent.extend(body["requests"])
            return Call({})

    class Slides:
        def presentations(self):
            return Presentations()

    mp = emit.master_plan(v1["deck"], Path(v1["out"]), theme=None)
    emit.style_layout_placeholders(Slides(), "P", v1["deck"], v1["plan"].scale, v1["plan"].fonts, emit.PPTX_TITLE_DY, mp["ground"])
    spec = emit.layout_style_spec(v1["deck"], v1["plan"].scale, v1["plan"].fonts, emit.PPTX_TITLE_DY, mp["ground"])
    mine = [r for page in pres["masters"] + pres["layouts"] for pe in page["pageElements"]
            if (kind := pe.get("shape", {}).get("placeholder", {}).get("type")) in theme_sync.PLACEHOLDERS and spec.get(kind)
            for r in emit.layout_placeholder_requests(spec[kind], pe)]
    assert sent and json.dumps(mine, sort_keys=True) == json.dumps(sent, sort_keys=True)


def test_convert_records_every_layout_with_its_decoration_and_placeholders(talk):
    rec = talk["base"]["theme"]
    assert rec["fill"] == "color:#ffffff" and rec["shared"] == talk["side1"]["shared"]
    assert {pid: p["group"] for pid, p in rec["pages"].items()} == {"M": "master", "LT": "TITLE", "LO": "*", "LB": "*"}
    assert rec["pages"]["LO"]["decoration"]["oid"] == "LO_d"
    assert theme_sync.same_picture_id(rec["pages"]["LO"]["decoration"]["picture"], talk["side1"]["pictures"]["*"])
    assert set(rec["pages"]["M"]["placeholders"]) == {"M_t", "M_b"}
    assert rec["pages"]["LT"]["placeholders"]["LT_t"]["kind"] == "CENTERED_TITLE"
    json.dumps(rec)  # (it goes into the base: plain data)


# ---------------------------------------------------------------- the merge

def test_the_same_theme_writes_nothing(talk):
    p = plan(talk, side=talk["side1"], ours=talk["v1"])
    assert p["requests"] == [] and p["conflicts"] == [] and p["cleanup"] == [] and p["warnings"] == []


def test_a_retheme_nobody_edited_writes_the_decoration_and_the_title_style(talk):
    p = plan(talk)
    assert p["conflicts"] == [] and p["warnings"] == []
    names = ops(p["requests"])
    # the frames' decoration on every layout of that group, the title page's left alone
    assert [x for x in names if x[0] == "replaceImage"] == [("replaceImage", "LB_d"), ("replaceImage", "LO_d")]
    assert ("updatePageElementAltText", "LO_d") in names
    # the frame title style on the master and the TITLE_ONLY layout; the title page's is the same
    assert {x[1] for x in names if x[0] == "updateTextStyle"} == {"M_t", "LO_t"}
    assert not any(x[1] and x[1].startswith("LT") for x in names)
    style = next(r for r in p["requests"] if "updateTextStyle" in r and r["updateTextStyle"]["objectId"] == "LO_t")
    assert style["updateTextStyle"]["style"]["fontSize"]["magnitude"] > 25  # (\huge)
    assert set(p["stage"]) == {talk["side2"]["pictures"]["*"]["path"]}
    assert {a["slide"] for a in p["applied"]} == {"master", "layout Blank", "layout Title Only"}


def slide_title(oid, kind, parent, runs):
    """A slide placeholder as presentations.get gives it: `runs` [(text, style)], one paragraph."""
    elements, i = [{"endIndex": sum(len(t) for t, _ in runs) + 1, "paragraphMarker": {"style": {}}}], 0
    for text, style in runs + [("\n", {})]:
        elements.append({**({"startIndex": i} if i else {}), "endIndex": i + len(text),
                         "textRun": {"content": text, "style": style}})
        i += len(text)
    return {"objectId": oid, "size": size(600, 60), "transform": at(20, 100),
            "shape": {"placeholder": {"type": kind, "parentObjectId": parent}, "text": {"textElements": elements}}}


def test_a_restyled_master_leaves_the_slides_titles_that_inherited_it_as_they_were(talk):
    """Google's .pptx import drops a run size equal to the inherited one, so the title page's title
    (beamer: the frame title's size) inherits the master's TITLE size. Restyling the master for a
    retheme's bigger frame titles must not grow it: its inherited style, as it rendered, is written
    onto it after the master's (before, Slides would drop it as equal to the inherited one). Only on the converter's slides, and only what the run does not set itself."""
    pres = copy.deepcopy(talk["pres"])
    master = pres["masters"][0]
    master["pageElements"][0]["shape"]["text"]["textElements"][1]["textRun"]["style"] = {
        "fontFamily": "Lato", "fontSize": {"magnitude": 20.7, "unit": "PT"}}
    master["pageElements"][0]["shape"]["text"]["textElements"][0]["paragraphMarker"]["style"] = {"alignment": "START"}
    for lay in pres["layouts"]:
        for pe in lay["pageElements"]:
            if pe.get("shape", {}).get("placeholder", {}).get("type") in ("TITLE", "CENTERED_TITLE"):
                pe["shape"]["placeholder"]["parentObjectId"] = "M_t"
    white = {"foregroundColor": {"opaqueColor": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}
    pres["slides"][0]["pageElements"] = [slide_title("S0_t", "CENTERED_TITLE", "LT_t", [("Keeping in Sync", white)])]
    pres["slides"][1]["pageElements"] = [slide_title("S1_t", "TITLE", "LO_t", [("Motivation", {
        **white, "fontFamily": "Lato", "fontSize": {"magnitude": 20.7, "unit": "PT"}})])]
    pres["slides"].append({"objectId": "MINE", "slideProperties": {"layoutObjectId": "LO"},
                           "pageElements": [slide_title("MINE_t", "TITLE", "LO_t", [("Added in Slides", {})])]})
    base = copy.deepcopy(talk["base"])   # (convert wrote that master style: it is no deck edit)
    base["theme"]["pages"]["M"]["placeholders"]["M_t"]["readback"]["style"] = theme_sync.style_hash(master["pageElements"][0])
    p = plan(talk, pres=pres, base=base)
    pins = [r for r in p["requests"] if next(iter(r.values())).get("objectId", "").startswith(("S0", "S1", "MINE"))]
    # after the layouts change (Slides drops a run property equal to the inherited one)
    assert pins and pins == p["requests"][-len(pins):]
    s0 = [r["updateTextStyle"] for r in pins if "updateTextStyle" in r and r["updateTextStyle"]["objectId"] == "S0_t"]
    assert len(s0) == 1 and s0[0]["style"]["fontSize"] == {"magnitude": 20.7, "unit": "PT"}
    assert "foregroundColor" not in s0[0]["fields"].split(",")                  # (its own white stays its own)
    assert s0[0]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": len("Keeping in Sync")}
    assert not any(r["updateTextStyle"]["objectId"] == "S1_t" and "fontSize" in r["updateTextStyle"]["fields"]
                   for r in pins if "updateTextStyle" in r)                      # (sets its own size)
    assert not any(next(iter(r.values()))["objectId"] == "MINE_t" for r in pins)   # (the person's slide follows the theme)
    assert {"updateParagraphStyle": {"objectId": "S0_t", "fields": "alignment", "style": {"alignment": "START"},
                                     "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 15}}} in pins
    assert set(p["pinned"]) == {"S0_t", "S1_t"}   # (S1's alignment is inherited too)


def test_a_layout_the_person_edited_is_a_conflict_and_is_left_alone(talk):
    pres = copy.deepcopy(talk["pres"])
    lo = next(l for l in pres["layouts"] if l["objectId"] == "LO")
    lo["pageElements"][1] = placeholder("LO_t", "TITLE", colour={"green": 0.5})   # recoloured
    lb = next(l for l in pres["layouts"] if l["objectId"] == "LB")
    lb["pageElements"][0] = picture("LB_d", x=30)                                  # moved
    p = plan(talk, pres=pres)
    names = ops(p["requests"])
    assert ("replaceImage", "LB_d") not in names and ("replaceImage", "LO_d") in names
    assert {x[1] for x in names if x[0] == "updateTextStyle"} == {"M_t"}
    conflicts = {(c["slide"], c["element"], c["field"]): c for c in p["conflicts"]}
    assert set(conflicts) == {("layout Title Only", "LO_t", "title style"), ("layout Blank", "LB_d", "theme decoration")}
    assert conflicts[("layout Title Only", "LO_t", "title style")]["resolution"] == "deck kept"
    assert "moved" in conflicts[("layout Blank", "LB_d", "theme decoration")]["theirs"]
    assert all(c["id"] for c in p["conflicts"])


def test_a_layout_edit_the_source_did_not_touch_is_nobodys_business(talk):
    pres = copy.deepcopy(talk["pres"])
    lt = next(l for l in pres["layouts"] if l["objectId"] == "LT")
    lt["pageElements"][0] = picture("LT_d", x=30)
    lt["pageElements"][1] = placeholder("LT_t", "CENTERED_TITLE", colour={"red": 0.5})
    p = plan(talk, pres=pres)
    assert not any(c["slide"] == "layout Title" for c in p["conflicts"])
    assert not any(x[1] and x[1].startswith("LT") for x in ops(p["requests"]))


def test_a_deleted_decoration_is_a_conflict_and_nothing_is_created_for_it(talk):
    pres = copy.deepcopy(talk["pres"])
    lo = next(l for l in pres["layouts"] if l["objectId"] == "LO")
    lo["pageElements"] = lo["pageElements"][1:]
    p = plan(talk, pres=pres)
    assert ("layout Title Only", "LO_d", "theme decoration") in {(c["slide"], c["element"], c["field"]) for c in p["conflicts"]}
    assert not any("createImage" in r for r in p["requests"])


def test_a_theme_without_decoration_removes_the_pictures_last(talk):
    side = copy.deepcopy(talk["side2"])
    side["pictures"] = {}
    p = plan(talk, side=side)
    assert sorted(p["cleanup"]) == ["LB_d", "LO_d", "LT_d"]
    assert not any("deleteObject" in r for r in p["requests"])


def test_a_new_master_fill_is_written_unless_the_person_changed_it(talk):
    side = copy.deepcopy(talk["side2"])
    side["fill"] = "color:#fff0f0"
    p = plan(talk, side=side)
    fill = [r for r in p["requests"] if "updatePageProperties" in r]
    assert len(fill) == 1 and fill[0]["updatePageProperties"]["objectId"] == "M"
    pres = copy.deepcopy(talk["pres"])
    pres["masters"][0]["pageProperties"]["pageBackgroundFill"]["solidFill"]["color"]["rgbColor"] = {"red": 0.2}
    p = plan(talk, side=side, pres=pres)
    assert not any("updatePageProperties" in r for r in p["requests"])
    assert [c["field"] for c in p["conflicts"]] == ["master background"]


def test_a_layout_serves_the_decoration_most_of_its_slides_have(talk):
    """One slide of TITLE_ONLY that a fresh conversion would put on a copy of the layout: the
    layout keeps the decoration of the others, and the slide is a warning."""
    side = copy.deepcopy(talk["side2"])
    ours = talk["retheme"]
    odd = ours["deck"]["slides"][3]["page"]
    side["groups"][odd] = "*_V1"
    side["pictures"]["*_V1"] = None
    p = plan(talk, side=side)
    assert p["page_group"]["LO"] == "*"
    assert len(p["warnings"]) == 1 and ours["slides"][3]["key"] in p["warnings"][0]


def test_the_new_record_takes_what_was_written_and_keeps_what_was_not(talk):
    pres = copy.deepcopy(talk["pres"])
    lo = next(l for l in pres["layouts"] if l["objectId"] == "LO")
    lo["pageElements"][1] = placeholder("LO_t", "TITLE", colour={"green": 0.5})
    p = plan(talk, pres=pres)
    after = copy.deepcopy(pres)
    for l in after["layouts"]:
        for e in l["pageElements"]:
            if "image" in e and l["objectId"] != "LT":
                e["image"]["contentUrl"] = "https://lh3.example/deco-b=s0"
    rec = theme_sync.new_record(talk["base"]["theme"], talk["side2"], p["written"], after)
    base = talk["base"]["theme"]
    assert rec["pages"]["LO"]["placeholders"]["LO_t"] == base["pages"]["LO"]["placeholders"]["LO_t"]  # (the conflict comes back)
    assert theme_sync.same_spec(rec["pages"]["M"]["placeholders"]["M_t"]["spec"], talk["side2"]["spec"]["TITLE"])
    assert theme_sync.same_picture_id(rec["pages"]["LO"]["decoration"]["picture"], talk["side2"]["pictures"]["*"])
    assert rec["pages"]["LO"]["decoration"]["readback"]["contentHash"] == snapshot.image_hash("https://lh3.example/deco-b=s0")
    assert rec["shared"] == talk["side2"]["shared"]
    # and a sync on that base with the same source writes nothing more, reporting the same conflict
    again = plan(talk, pres=after, base={**talk["base"], "theme": rec})
    assert again["requests"] == [] and [c["element"] for c in again["conflicts"]] == ["LO_t"]
    assert again["conflicts"][0]["id"] == p["conflicts"][0]["id"]


def test_a_placeholder_an_interrupted_sync_wrote_is_its_own(talk):
    pres = copy.deepcopy(talk["pres"])
    lo = next(l for l in pres["layouts"] if l["objectId"] == "LO")
    lo["pageElements"][1] = placeholder("LO_t", "TITLE", colour={"red": 0.5})
    base = {**talk["base"], "pending": {"theme": {"LO_t": talk["side2"]["spec"]["TITLE"]}}}
    p = plan(talk, pres=pres, base=base)
    assert p["conflicts"] == [] and "LO_t" in p["written"]
    assert "LO_t" not in {x[1] for x in ops(p["requests"])}


# ---------------------------------------------------------------- the header and footer words

def footer_box(oid, text, x):
    return {"objectId": oid, "size": size(200, 12), "transform": at(x, 390),
            "shape": {"shapeType": "TEXT_BOX", "text": {"textElements": [
                {"startIndex": 0, "endIndex": len(text) + 1, "paragraphMarker": {"style": {}}},
                {"startIndex": 0, "endIndex": len(text) + 1, "textRun": {"content": text + "\n", "style": {}}}]}}}


def with_footers(pres, says):
    """The deck as convert leaves it: every layout carries the shared words (emit.write_layout_texts)."""
    pres = copy.deepcopy(pres)
    for li, lay in enumerate(pres["layouts"]):
        lay["pageElements"] += [footer_box(f"{emit.LAYOUT_TEXT_PREFIX}{li}_{ti}", s, 240 * ti) for ti, s in enumerate(says)]
    return pres


@pytest.fixture
def footers(talk):
    """The sync talk's footline (author, title, date) on the made-up deck's layouts, recorded as
    convert records it, and a new version whose \\date changed."""
    old = talk["v1"]["deck"]["layout_texts"]
    says = theme_sync.texts_says(old)
    pres = with_footers(talk["pres"], says)
    base = {**talk["base"], "theme": {**talk["base"]["theme"], "texts": theme_sync.texts_entry(old, pres)}}
    new = copy.deepcopy(old)
    date = next(t for t in new if identity.plain_text(t) == "September 2026")
    date["paragraphs"][0]["runs"][0]["text"] = "October 2026"
    side = {**talk["side1"], "texts": new}
    return {"pres": pres, "base": base, "side": side, "says": says}


def footer_plan(talk, f, pres=None, base=None, side=None):
    return plan(talk, pres=pres or f["pres"], base=base or f["base"], side=side or f["side"], ours=talk["v1"])


def test_convert_records_the_layouts_footer_words(talk, footers):
    rec = footers["base"]["theme"]["texts"]
    assert rec["says"] == ["A. Author (Uni)", "Deck sync", "September 2026"]
    assert len(rec["objects"]) == 3 * len(footers["pres"]["layouts"]) and \
        {o["text"] for o in rec["objects"].values()} == {s + "\n" for s in rec["says"]}
    json.dumps(rec)
    # and convert's own record carries them (the made-up deck holds none: recorded as none)
    assert talk["base"]["theme"]["texts"]["says"] == rec["says"] and talk["base"]["theme"]["texts"]["objects"] == {}


def test_a_new_date_nobody_edited_rewrites_the_footer_on_every_layout(talk, footers):
    """The edit hunt's h5a: `\\date` changed, and the footline showed the old date on every slide."""
    p = footer_plan(talk, footers)
    assert p["conflicts"] == [] and p["warnings"] == []
    deleted = {r["deleteObject"]["objectId"] for r in p["requests"] if "deleteObject" in r}
    assert deleted == set(footers["base"]["theme"]["texts"]["objects"])
    created = [r["createShape"] for r in p["requests"] if "createShape" in r]
    assert sorted(c["objectId"] for c in created) == sorted(deleted)   # (convert's ids, one batch)
    assert {c["elementProperties"]["pageObjectId"] for c in created} == {"LT", "LO", "LB"}
    inserted = [r["insertText"]["text"] for r in p["requests"] if "insertText" in r]
    assert inserted.count("October 2026") == 3 and "September 2026" not in inserted
    assert {"slide": "layouts", "element": None, "fields": ["header and footer"]} in p["applied"]
    assert p["pending"]["header and footer"] == theme_sync.texts_digest(footers["side"]["texts"])


def test_the_same_footer_writes_nothing(talk, footers):
    p = footer_plan(talk, footers, side={**footers["side"], "texts": talk["v1"]["deck"]["layout_texts"]})
    assert not any(r.get("deleteObject", {}).get("objectId", "").startswith(emit.LAYOUT_TEXT_PREFIX) for r in p["requests"])
    assert p["applied"] == [] and p["conflicts"] == []


def test_a_footer_the_person_retyped_is_a_conflict_and_stays(talk, footers):
    pres = copy.deepcopy(footers["pres"])
    lo = next(l for l in pres["layouts"] if l["objectId"] == "LO")
    lo["pageElements"][-1] = footer_box(lo["pageElements"][-1]["objectId"], "Draft - do not share", 480)
    p = footer_plan(talk, footers, pres=pres)
    assert not any("deleteObject" in r or "createShape" in r for r in p["requests"])
    [c] = [c for c in p["conflicts"] if c["field"] == "header and footer"]
    assert c["resolution"] == "deck kept" and "Draft - do not share" in json.dumps(c["theirs"])
    assert "October 2026" in json.dumps(c["ours"])


def test_after_the_write_the_next_sync_writes_no_footer(talk, footers):
    p = footer_plan(talk, footers)
    after = with_footers(talk["pres"], theme_sync.texts_says(footers["side"]["texts"]))
    rec = theme_sync.new_record(footers["base"]["theme"], footers["side"], p["written"], after)
    assert rec["texts"]["says"][2] == "October 2026"
    again = footer_plan(talk, footers, pres=after, base={**footers["base"], "theme": rec})
    assert again["conflicts"] == [] and not any("createShape" in r for r in again["requests"])


def test_a_footer_an_interrupted_sync_wrote_is_its_own(talk, footers):
    after = with_footers(talk["pres"], theme_sync.texts_says(footers["side"]["texts"]))
    digest = theme_sync.texts_digest(footers["side"]["texts"])
    base = {**footers["base"], "pending": {"theme": {"header and footer": digest}}}
    p = footer_plan(talk, footers, pres=after, base=base)
    assert p["conflicts"] == [] and "header and footer" in p["written"]
    assert not any("createShape" in r for r in p["requests"])


def test_a_base_older_than_footer_sync_leaves_them_and_says_so(talk, footers):
    theme = {k: v for k, v in footers["base"]["theme"].items() if k != "texts"}
    p = footer_plan(talk, footers, base={**footers["base"], "theme": theme})
    assert not any("createShape" in r for r in p["requests"])
    assert any("header and footer" in w and "'October 2026'" in w for w in p["warnings"])
    same = footer_plan(talk, footers, base={**footers["base"], "theme": theme},
                       side={**footers["side"], "texts": talk["v1"]["deck"]["layout_texts"]})
    assert not any("header and footer" in w for w in same["warnings"])


# ---------------------------------------------------------------- slides and old bases

class FakeSync(sync.Sync):
    def __init__(self, base, side):
        self.base, self.theme_side, self.theme_plan = base, side, None


def test_a_slide_showing_the_new_shared_background_inherits_the_master(talk):
    s = FakeSync(talk["base"], talk["side2"])
    key = talk["side2"]["shared"]
    pres = copy.deepcopy(talk["pres"])
    assert s.background_requests("S2", key, {}, pres) == []   # (it inherits already)
    pres["slides"][2]["pageProperties"] = {"pageBackgroundFill": {"solidFill": {"color": {"rgbColor": {"red": 1}}}}}
    assert s.background_requests("S2", key, {}, pres) == [{"updatePageProperties": {
        "objectId": "S2", "fields": "pageBackgroundFill.propertyState",
        "pageProperties": {"pageBackgroundFill": {"propertyState": "INHERIT"}}}}]
    assert s.background_requests("S9", key, {}, pres, created=True) == []


def test_an_old_base_leaves_the_layouts_alone_and_says_so(talk):
    old = {k: v for k, v in talk["base"].items() if k != "theme"}
    s = FakeSync(old, talk["side2"])
    assert s.master_key() == talk["side1"]["shared"]   # (the old behaviour)
    assert "older than theme sync" in theme_sync.old_base_warning(old, talk["side2"])
    assert theme_sync.old_base_warning(old, talk["side1"]) is None


@pytest.mark.parametrize("name, group", [("TITLE", "TITLE"), ("TITLE_ONLY", "*"), ("BLANK", "*"),
                                         ("TITLE_ONLY_V2", "*_V2"), ("TITLE_V1", "TITLE_V1")])
def test_groups_follow_emits_layout_names(name, group):
    assert theme_sync.group_of(name) == group
