"""Theme sync (theme_sync.py): the master, the layouts' decoration pictures and their placeholders'
styles, merged three ways - offline, on the sync test talk and a made-up deck read."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from beamer2slides.emit import SLIDE_W
from beamer2slides import emit, google_types, identity, snapshot, sync, theme_sync
from beamer2slides.google_types import BatchUpdateResponse, Presentation, Request
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.theme_sync import DECORATION, PictureId, SidePicture, ThemeMerge, ThemeRecord, ThemeSide, Wrote
from beamer2slides.typing_compat import override

from .fake_google import Answer, NoPresentations, NoSlides
from .json_reads import jarr, jat, jint, jnum, jobj, jobjs, jstr, jstrs

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides.google_types import GetPresentation, UpdatePresentation

SYNC_DECKS = Path(__file__).resolve().parent / "decks" / "sync" / "out"
EMU = 12700
DECO_A = "https://lh3.example/deco-a=s0"


def need(*names: str) -> None:
    missing = [n for n in names if not (SYNC_DECKS / f"{n}.pdf").exists()]
    if missing:
        pytest.skip(f"build the sync test talk first (tests/decks/sync/build.py): {missing}")


def as_presentation(pres: JsonObject) -> Presentation:
    """The made-up deck as `presentations.get` answers it (the same object)."""
    return google_types.presentation(pres, "the made-up deck")


# ---------------------------------------------------------------- a made-up deck read

def size(w: float, h: float) -> JsonObject:
    return {"width": {"magnitude": w * EMU, "unit": "EMU"}, "height": {"magnitude": h * EMU, "unit": "EMU"}}


def at(x: float, y: float) -> JsonObject:
    return {"scaleX": 1, "scaleY": 1, "translateX": x * EMU, "translateY": y * EMU, "unit": "EMU"}


def picture(oid: str, url: str, x: float) -> JsonObject:
    return {"objectId": oid, "size": size(720, 405), "transform": at(x, 0), "image": {"contentUrl": url},
            "description": DECORATION}


def placeholder(oid: str, kind: str, colour: JsonObject | None, y: float) -> JsonObject:
    style: JsonObject = {"foregroundColor": {"opaqueColor": {"rgbColor": colour}}} if colour else {}
    no_style: JsonObject = {}
    return {"objectId": oid, "size": size(600, 60), "transform": at(20, y),
            "shape": {"placeholder": {"type": kind}, "text": {"textElements": [
                {"endIndex": 1, "paragraphMarker": {"style": no_style}},
                {"endIndex": 1, "textRun": {"content": "\n", "style": style}}]}}}


def layout(oid: str, name: str, elements: list[Json]) -> JsonObject:
    return {"objectId": oid, "layoutProperties": {"name": name, "displayName": name.title().replace("_", " ")},
            "pageElements": elements}


def made_up_deck(n_slides: int) -> JsonObject:
    """The deck `convert` makes of the sync test talk, as presentations.get gives it: the master,
    three layouts carrying the decoration, a title slide and frames on TITLE_ONLY."""
    no_elements: list[Json] = []
    slides: list[Json] = [{"objectId": f"S{i}", "slideProperties": {"layoutObjectId": "LT" if i == 0 else "LO"},
                           "pageProperties": {"pageBackgroundFill": {"propertyState": "INHERIT"}},
                           "pageElements": no_elements[:]} for i in range(n_slides)]
    return {"presentationId": "P", "revisionId": "r1", "pageSize": size(720, 405),
            "masters": [{"objectId": "M", "pageProperties": {"pageBackgroundFill": {"solidFill": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}},
                         "pageElements": [placeholder("M_t", "TITLE", None, 10.0),
                                          placeholder("M_b", "BODY", None, 100)]}],
            "layouts": [layout("LT", "TITLE", [picture("LT_d", DECO_A, 0.0),
                                               placeholder("LT_t", "CENTERED_TITLE", None, 10.0),
                                               placeholder("LT_s", "SUBTITLE", None, 10.0)]),
                        layout("LO", "TITLE_ONLY", [picture("LO_d", DECO_A, 0.0),
                                                    placeholder("LO_t", "TITLE", None, 10.0)]),
                        layout("LB", "BLANK", [picture("LB_d", DECO_A, 0.0)])],
            "slides": slides}


@dataclass(frozen=True, kw_only=True)
class Talk:
    """v1 and retheme of the sync talk, the base's theme as `convert` records it on the made-up deck
    (`base`, `record`), and each version's side."""
    v1: sync.Built
    side1: ThemeSide
    pres: JsonObject
    base: JsonObject
    retheme: sync.Built
    record: ThemeRecord
    side2: ThemeSide


@pytest.fixture(scope="module")
def talk(tmp_path_factory: pytest.TempPathFactory) -> Talk:
    """v1 and retheme of the sync talk, the base's theme as `convert` records it on the made-up
    deck, and the retheme's side."""
    need("v1", "retheme")
    tmp = tmp_path_factory.mktemp("theme")
    v1 = sync.build_ours_of(SYNC_DECKS / "v1.pdf", tmp / "v1", {"slides": []}, "last", SLIDE_W, snapshot.NO_PICTURES)
    side1 = theme_sync.ours_side(sync.theme_ours_of(v1))
    out = v1.out
    n = len(jarr(v1.deck, "slides"))
    pres = made_up_deck(n)
    decorations: JsonObject = {g: str(Path(p.path).relative_to(out)) for g, p in side1.pictures.items() if p}
    layouts: JsonObject = {str(s["page"]): emit.slide_layout(s)[0] for s in jobjs(v1.deck, "slides")}
    state: JsonObject = {"scale": v1.plan.scale, "slides": [{"objectId": f"S{i}"} for i in range(n)],
                         "theme": {"ground": "#ffffff", "master": "#ffffff", "decorations": decorations,
                                   "layouts": layouts}}
    record = theme_sync.record(v1.deck, out, as_presentation(pres), state)
    assert record is not None
    rec = theme_sync.theme_json(record)
    slides = copy.deepcopy(v1.slides)
    for i, s in enumerate(slides):
        s["objectId"] = f"S{i}"
        s["layoutObjectId"] = "LT" if i == 0 else "LO"
    base: JsonObject = {"slides": [*slides], "theme": rec, "master_background": side1.shared}
    retheme = sync.build_ours_of(SYNC_DECKS / "retheme.pdf", tmp / "retheme", {"slides": [*slides]}, "last", SLIDE_W,
                                 snapshot.NO_PICTURES)
    return Talk(v1=v1, side1=side1, pres=pres, base=base, retheme=retheme, record=record,
                side2=theme_sync.ours_side(sync.theme_ours_of(retheme)))


def picture_url(path: str) -> str:
    return f"url:{Path(path).name}"


def new_id(page: str) -> str:
    return f"new_{page}"


def plan(base: JsonObject, side: ThemeSide, ours: sync.Built, pres: JsonObject) -> ThemeMerge:
    return theme_sync.plan(base, side, sync.theme_ours_of(ours), as_presentation(pres), "1ab", picture_url, new_id,
                           None)


def retheme_plan(talk: Talk, pres: JsonObject) -> ThemeMerge:
    """The retheme planned over the talk's base, on `pres`."""
    return plan(talk.base, talk.side2, talk.retheme, pres)


def picture_of(rec_picture: Json) -> PictureId:
    """A base's decoration picture (JSON) as the record reads it."""
    return theme_sync.picture_id_of(rec_picture, "picture")


def side_picture(side: ThemeSide, group: str) -> SidePicture:
    """The decoration picture `side` renders for `group` (it has one)."""
    found = side.pictures[group]
    assert found is not None
    return found


def new_record(theme: Json, side: ThemeSide, written: Mapping[str, Wrote], after: JsonObject) -> JsonObject:
    """`theme_sync.new_record` over the base's JSON, as JSON."""
    return theme_sync.theme_json(theme_sync.new_record(theme_sync.theme_record(theme, "theme"), side, written,
                                                       as_presentation(after)))


def target(r: JsonObject) -> str | None:
    """The object a request is about (its `objectId`, or a replaceImage's `imageObjectId`)."""
    body = jobj(next(iter(r.values())))
    oid = body.get("objectId") or body.get("imageObjectId")
    assert oid is None or isinstance(oid, str)
    return oid


def ops(reqs: Sequence[JsonObject]) -> list[tuple[str, str | None]]:
    return sorted((next(iter(r)), target(r)) for r in reqs)


def placeholder_kind(pe: JsonObject) -> str | None:
    """The placeholder type of a page element (None: no placeholder)."""
    shape = pe.get("shape")
    ph = shape.get("placeholder") if isinstance(shape, dict) else None
    kind = ph.get("type") if isinstance(ph, dict) else None
    return kind if isinstance(kind, str) else None


def layout_page(pres: JsonObject, oid: str) -> JsonObject:
    return next(p for p in jobjs(pres, "layouts") if p["objectId"] == oid)


def where(c: JsonObject) -> tuple[str, str | None, str]:
    """(slide, element, field) of a conflict."""
    element = c.get("element")
    assert element is None or isinstance(element, str)
    return jstr(c, "slide"), element, jstr(c, "field")


# ---------------------------------------------------------------- what convert wrote, as data

def test_the_spec_writes_what_style_layout_placeholders_writes(talk: Talk) -> None:
    """layout_style_spec + layout_placeholder_requests are the same requests, placeholder by
    placeholder, as the pass convert runs (so a sync writes what a fresh conversion would)."""
    v1 = talk.v1
    pres = talk.pres
    sent: list[Mapping[str, object]] = []

    class Presentations(NoPresentations):
        @override
        def get(self, **kw: Unpack[GetPresentation]) -> Request[Presentation]:
            return Answer(as_presentation(pres))

        @override
        def batchUpdate(self, **kw: Unpack[UpdatePresentation]) -> Request[BatchUpdateResponse]:
            sent.extend(kw["body"]["requests"])
            done: BatchUpdateResponse = {}
            return Answer(done)

    class Slides(NoSlides):
        @override
        def presentations(self) -> Presentations:
            return Presentations()

    mp = emit.master_plan(v1.deck, v1.out, None)
    emit.style_layout_placeholders(Slides(), "P", v1.deck, v1.plan.scale, v1.plan.fonts, emit.PPTX_TITLE_DY, mp["ground"])
    spec = emit.layout_style_spec(v1.deck, v1.plan.scale, v1.plan.fonts, emit.PPTX_TITLE_DY, mp["ground"])
    mine = [r for page in jobjs(pres, "masters") + jobjs(pres, "layouts") for pe in jobjs(page, "pageElements")
            if (kind := placeholder_kind(pe)) in theme_sync.PLACEHOLDERS and kind is not None and spec.get(kind)
            for r in emit.layout_placeholder_requests(jobj(spec[kind]), pe)]
    assert sent and json.dumps(mine, sort_keys=True) == json.dumps(sent, sort_keys=True)


def test_convert_records_every_layout_with_its_decoration_and_placeholders(talk: Talk) -> None:
    rec = jobj(talk.base, "theme")
    assert rec["fill"] == "color:#ffffff" and rec["shared"] == talk.side1.shared
    assert {pid: jat(p, "group") for pid, p in jobj(rec, "pages").items()} == \
        {"M": "master", "LT": "TITLE", "LO": "*", "LB": "*"}
    assert jat(rec, "pages", "LO", "decoration", "oid") == "LO_d"
    assert theme_sync.same_picture_id(picture_of(jat(rec, "pages", "LO", "decoration", "picture")),
                                      side_picture(talk.side1, "*").picture)
    assert set(jobj(rec, "pages", "M", "placeholders")) == {"M_t", "M_b"}
    assert jat(rec, "pages", "LT", "placeholders", "LT_t", "kind") == "CENTERED_TITLE"
    json.dumps(rec)  # (it goes into the base: plain data)
    # and it is read back as it was written, the header and footer words too
    assert theme_sync.theme_record(rec, "theme") == talk.record and talk.record.texts is not None
    assert json.dumps(theme_sync.theme_json(theme_sync.theme_record(rec, "theme"))) == json.dumps(rec)


# ---------------------------------------------------------------- the merge

def test_the_same_theme_writes_nothing(talk: Talk) -> None:
    p = plan(talk.base, talk.side1, talk.v1, talk.pres)
    assert p.requests == [] and p.conflicts == [] and p.cleanup == [] and p.warnings == []


def test_a_retheme_nobody_edited_writes_the_decoration_and_the_title_style(talk: Talk) -> None:
    p = retheme_plan(talk, talk.pres)
    assert p.conflicts == [] and p.warnings == []
    names = ops(p.requests)
    # the frames' decoration on every layout of that group, the title page's left alone
    assert [x for x in names if x[0] == "replaceImage"] == [("replaceImage", "LB_d"), ("replaceImage", "LO_d")]
    assert ("updatePageElementAltText", "LO_d") in names
    # the frame title style on the master and the TITLE_ONLY layout; the title page's is the same
    assert {x[1] for x in names if x[0] == "updateTextStyle"} == {"M_t", "LO_t"}
    assert not any(x[1] and x[1].startswith("LT") for x in names)
    style = next(r for r in p.requests if "updateTextStyle" in r and jat(r, "updateTextStyle", "objectId") == "LO_t")
    assert jnum(style, "updateTextStyle", "style", "fontSize", "magnitude") > 25  # (\huge)
    assert set(p.stage) == {side_picture(talk.side2, "*").path}
    assert {jstr(a, "slide") for a in p.applied} == {"master", "layout Blank", "layout Title Only"}


def slide_title(oid: str, kind: str, parent: str, runs: list[tuple[str, JsonObject]]) -> JsonObject:
    """A slide placeholder as presentations.get gives it: `runs` [(text, style)], one paragraph."""
    no_style: JsonObject = {}
    elements: list[Json] = [{"endIndex": sum(len(t) for t, _ in runs) + 1, "paragraphMarker": {"style": {}}}]
    i = 0
    for text, style in [*runs, ("\n", no_style)]:
        run: JsonObject = {"startIndex": i} if i else {}
        elements.append({**run, "endIndex": i + len(text), "textRun": {"content": text, "style": style}})
        i += len(text)
    return {"objectId": oid, "size": size(600, 60), "transform": at(20, 100),
            "shape": {"placeholder": {"type": kind, "parentObjectId": parent}, "text": {"textElements": elements}}}


def test_a_restyled_master_leaves_the_slides_titles_that_inherited_it_as_they_were(talk: Talk) -> None:
    """Google's .pptx import drops a run size equal to the inherited one, so the title page's title
    (beamer: the frame title's size) inherits the master's TITLE size. Restyling the master for a
    retheme's bigger frame titles must not grow it: its inherited style, as it rendered, is written
    onto it after the master's (before, Slides would drop it as equal to the inherited one). Only on the converter's slides, and only what the run does not set itself."""
    pres = copy.deepcopy(talk.pres)
    master = jobj(pres, "masters", 0)
    title_text = jarr(master, "pageElements", 0, "shape", "text", "textElements")
    jobj(title_text, 1, "textRun")["style"] = {"fontFamily": "Lato", "fontSize": {"magnitude": 20.7, "unit": "PT"}}
    jobj(title_text, 0, "paragraphMarker")["style"] = {"alignment": "START"}
    for lay in jobjs(pres, "layouts"):
        for pe in jobjs(lay, "pageElements"):
            if placeholder_kind(pe) in ("TITLE", "CENTERED_TITLE"):
                jobj(pe, "shape", "placeholder")["parentObjectId"] = "M_t"
    white: JsonObject = {"foregroundColor": {"opaqueColor": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}
    jobj(pres, "slides", 0)["pageElements"] = [slide_title("S0_t", "CENTERED_TITLE", "LT_t", [("Keeping in Sync", white)])]
    jobj(pres, "slides", 1)["pageElements"] = [slide_title("S1_t", "TITLE", "LO_t", [("Motivation", {
        **white, "fontFamily": "Lato", "fontSize": {"magnitude": 20.7, "unit": "PT"}})])]
    no_style: JsonObject = {}
    jarr(pres, "slides").append({"objectId": "MINE", "slideProperties": {"layoutObjectId": "LO"},
                                 "pageElements": [slide_title("MINE_t", "TITLE", "LO_t", [("Added in Slides", no_style)])]})
    base = copy.deepcopy(talk.base)   # (convert wrote that master style: it is no deck edit)
    master_title = jobj(master, "pageElements", 0)
    assert google_types.is_page_element(master_title)
    jobj(base, "theme", "pages", "M", "placeholders", "M_t", "readback")["style"] = theme_sync.style_hash(master_title)
    p = plan(base, talk.side2, talk.retheme, pres)
    pins = [r for r in p.requests if (target(r) or "").startswith(("S0", "S1", "MINE"))]
    # after the layouts change (Slides drops a run property equal to the inherited one)
    assert pins and pins == p.requests[-len(pins):]
    s0 = [jobj(r, "updateTextStyle") for r in pins if "updateTextStyle" in r and jat(r, "updateTextStyle", "objectId") == "S0_t"]
    assert len(s0) == 1 and jat(s0[0], "style", "fontSize") == {"magnitude": 20.7, "unit": "PT"}
    assert "foregroundColor" not in jstr(s0[0], "fields").split(",")                  # (its own white stays its own)
    assert s0[0]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": len("Keeping in Sync")}
    assert not any(jat(r, "updateTextStyle", "objectId") == "S1_t" and "fontSize" in jstr(r, "updateTextStyle", "fields")
                   for r in pins if "updateTextStyle" in r)                      # (sets its own size)
    assert not any(target(r) == "MINE_t" for r in pins)   # (the person's slide follows the theme)
    assert {"updateParagraphStyle": {"objectId": "S0_t", "fields": "alignment", "style": {"alignment": "START"},
                                     "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 15}}} in pins
    assert set(p.pinned) == {"S0_t", "S1_t"}   # (S1's alignment is inherited too)


def test_a_layout_the_person_edited_is_a_conflict_and_is_left_alone(talk: Talk) -> None:
    pres = copy.deepcopy(talk.pres)
    jarr(layout_page(pres, "LO"), "pageElements")[1] = placeholder("LO_t", "TITLE", {"green": 0.5}, 10.0)   # recoloured
    jarr(layout_page(pres, "LB"), "pageElements")[0] = picture("LB_d", DECO_A, 30)                        # moved
    p = retheme_plan(talk, pres)
    names = ops(p.requests)
    assert ("replaceImage", "LB_d") not in names and ("replaceImage", "LO_d") in names
    assert {x[1] for x in names if x[0] == "updateTextStyle"} == {"M_t"}
    conflicts = {where(c): c for c in p.conflicts}
    assert set(conflicts) == {("layout Title Only", "LO_t", "title style"), ("layout Blank", "LB_d", "theme decoration")}
    assert conflicts[("layout Title Only", "LO_t", "title style")]["resolution"] == "deck kept"
    assert "moved" in jstr(conflicts[("layout Blank", "LB_d", "theme decoration")], "theirs")
    assert all(c["id"] for c in p.conflicts)


def test_a_layout_edit_the_source_did_not_touch_is_nobodys_business(talk: Talk) -> None:
    pres = copy.deepcopy(talk.pres)
    lt = jarr(layout_page(pres, "LT"), "pageElements")
    lt[0] = picture("LT_d", DECO_A, 30)
    lt[1] = placeholder("LT_t", "CENTERED_TITLE", {"red": 0.5}, 10.0)
    p = retheme_plan(talk, pres)
    assert not any(c["slide"] == "layout Title" for c in p.conflicts)
    assert not any(x[1] and x[1].startswith("LT") for x in ops(p.requests))


def test_a_deleted_decoration_is_a_conflict_and_nothing_is_created_for_it(talk: Talk) -> None:
    pres = copy.deepcopy(talk.pres)
    lo = layout_page(pres, "LO")
    lo["pageElements"] = jarr(lo, "pageElements")[1:]
    p = retheme_plan(talk, pres)
    assert ("layout Title Only", "LO_d", "theme decoration") in {where(c) for c in p.conflicts}
    assert not any("createImage" in r for r in p.requests)


def test_a_theme_without_decoration_removes_the_pictures_last(talk: Talk) -> None:
    side = replace(talk.side2, pictures={})
    p = plan(talk.base, side, talk.retheme, talk.pres)
    assert sorted(p.cleanup) == ["LB_d", "LO_d", "LT_d"]
    assert not any("deleteObject" in r for r in p.requests)


def test_a_new_master_fill_is_written_unless_the_person_changed_it(talk: Talk) -> None:
    side = replace(talk.side2, fill="color:#fff0f0")
    p = plan(talk.base, side, talk.retheme, talk.pres)
    fill = [r for r in p.requests if "updatePageProperties" in r]
    assert len(fill) == 1 and jat(fill[0], "updatePageProperties", "objectId") == "M"
    pres = copy.deepcopy(talk.pres)
    jobj(pres, "masters", 0, "pageProperties", "pageBackgroundFill", "solidFill", "color")["rgbColor"] = {"red": 0.2}
    p = plan(talk.base, side, talk.retheme, pres)
    assert not any("updatePageProperties" in r for r in p.requests)
    assert [c["field"] for c in p.conflicts] == ["master background"]


def test_a_layout_serves_the_decoration_most_of_its_slides_have(talk: Talk) -> None:
    """One slide of TITLE_ONLY that a fresh conversion would put on a copy of the layout: the
    layout keeps the decoration of the others, and the slide is a warning."""
    side = copy.deepcopy(talk.side2)
    ours = talk.retheme
    odd = jint(ours.deck, "slides", 3, "page")
    side.groups[odd] = "*_V1"
    side.pictures["*_V1"] = None
    p = plan(talk.base, side, talk.retheme, talk.pres)
    assert p.page_group["LO"] == "*"
    assert len(p.warnings) == 1 and jstr(ours.slides[3], "key") in p.warnings[0]


def test_the_new_record_takes_what_was_written_and_keeps_what_was_not(talk: Talk) -> None:
    pres = copy.deepcopy(talk.pres)
    jarr(layout_page(pres, "LO"), "pageElements")[1] = placeholder("LO_t", "TITLE", {"green": 0.5}, 10.0)
    p = retheme_plan(talk, pres)
    after = copy.deepcopy(pres)
    for lay in jobjs(after, "layouts"):
        for e in jobjs(lay, "pageElements"):
            if "image" in e and lay["objectId"] != "LT":
                jobj(e, "image")["contentUrl"] = "https://lh3.example/deco-b=s0"
    rec = new_record(talk.base["theme"], talk.side2, p.written, after)
    base = jobj(talk.base, "theme")
    assert jat(rec, "pages", "LO", "placeholders", "LO_t") == \
        jat(base, "pages", "LO", "placeholders", "LO_t")  # (the conflict comes back)
    assert theme_sync.same_spec(jat(rec, "pages", "M", "placeholders", "M_t", "spec"), talk.side2.spec["TITLE"])
    assert theme_sync.same_picture_id(picture_of(jat(rec, "pages", "LO", "decoration", "picture")),
                                      side_picture(talk.side2, "*").picture)
    assert jat(rec, "pages", "LO", "decoration", "readback", "contentHash") == \
        snapshot.image_hash("https://lh3.example/deco-b=s0")
    assert rec["shared"] == talk.side2.shared
    # and a sync on that base with the same source writes nothing more, reporting the same conflict
    again = plan({**talk.base, "theme": rec}, talk.side2, talk.retheme, after)
    assert again.requests == [] and [c["element"] for c in again.conflicts] == ["LO_t"]
    assert again.conflicts[0]["id"] == p.conflicts[0]["id"]


def test_a_placeholder_an_interrupted_sync_wrote_is_its_own(talk: Talk) -> None:
    pres = copy.deepcopy(talk.pres)
    jarr(layout_page(pres, "LO"), "pageElements")[1] = placeholder("LO_t", "TITLE", {"red": 0.5}, 10.0)
    base: JsonObject = {**talk.base, "pending": {"theme": {"LO_t": talk.side2.spec["TITLE"]}}}
    p = plan(base, talk.side2, talk.retheme, pres)
    assert p.conflicts == [] and "LO_t" in p.written
    assert "LO_t" not in {x[1] for x in ops(p.requests)}


# ---------------------------------------------------------------- the header and footer words

def footer_box(oid: str, text: str, x: float) -> JsonObject:
    no_style: JsonObject = {}
    return {"objectId": oid, "size": size(200, 12), "transform": at(x, 390),
            "shape": {"shapeType": "TEXT_BOX", "text": {"textElements": [
                {"startIndex": 0, "endIndex": len(text) + 1, "paragraphMarker": {"style": no_style}},
                {"startIndex": 0, "endIndex": len(text) + 1, "textRun": {"content": text + "\n", "style": {}}}]}}}


def with_footers(pres: JsonObject, says: list[str]) -> JsonObject:
    """The deck as convert leaves it: every layout carries the shared words (emit.write_layout_texts)."""
    pres = copy.deepcopy(pres)
    for li, lay in enumerate(jobjs(pres, "layouts")):
        jarr(lay, "pageElements").extend(footer_box(f"{emit.LAYOUT_TEXT_PREFIX}{li}_{ti}", s, 240 * ti)
                                         for ti, s in enumerate(says))
    return pres


@dataclass(frozen=True, kw_only=True)
class Footers:
    """The made-up deck with the talk's footline (`pres`), its base, the new version's side and what
    the footline says."""
    pres: JsonObject
    base: JsonObject
    side: ThemeSide
    says: list[str]


@pytest.fixture
def footers(talk: Talk) -> Footers:
    """The sync talk's footline (author, title, date) on the made-up deck's layouts, recorded as
    convert records it, and a new version whose \\date changed."""
    old = jobjs(talk.v1.deck, "layout_texts")
    says = theme_sync.texts_says(old)
    pres = with_footers(talk.pres, says)
    texts = theme_sync.texts_json(theme_sync.texts_entry(old, as_presentation(pres)))
    base: JsonObject = {**talk.base, "theme": {**jobj(talk.base, "theme"), "texts": texts}}
    new = copy.deepcopy(old)
    date = next(t for t in new if identity.plain_text(t) == "September 2026")
    jobj(date, "paragraphs", 0, "runs", 0)["text"] = "October 2026"
    side = replace(talk.side1, texts=new)
    return Footers(pres=pres, base=base, side=side, says=says)


def footer_plan(talk: Talk, pres: JsonObject, base: JsonObject, side: ThemeSide) -> ThemeMerge:
    return plan(base, side, talk.v1, pres)


def test_convert_records_the_layouts_footer_words(talk: Talk, footers: Footers) -> None:
    rec = jobj(footers.base, "theme", "texts")
    assert rec["says"] == ["A. Author (Uni)", "Deck sync", "September 2026"]
    assert len(jobj(rec, "objects")) == 3 * len(jarr(footers.pres, "layouts")) and \
        {jstr(o, "text") for o in jobj(rec, "objects").values()} == {s + "\n" for s in jstrs(rec, "says")}
    json.dumps(rec)
    # and convert's own record carries them (the made-up deck holds none: recorded as none)
    assert jat(talk.base, "theme", "texts", "says") == rec["says"] and jat(talk.base, "theme", "texts", "objects") == {}


def test_a_new_date_nobody_edited_rewrites_the_footer_on_every_layout(talk: Talk, footers: Footers) -> None:
    """The edit hunt's h5a: `\\date` changed, and the footline showed the old date on every slide."""
    p = footer_plan(talk, footers.pres, footers.base, footers.side)
    assert p.conflicts == [] and p.warnings == []
    deleted = {jstr(r, "deleteObject", "objectId") for r in p.requests if "deleteObject" in r}
    assert deleted == set(jobj(footers.base, "theme", "texts", "objects"))
    created = [jobj(r, "createShape") for r in p.requests if "createShape" in r]
    assert sorted(jstr(c, "objectId") for c in created) == sorted(deleted)   # (convert's ids, one batch)
    assert {jstr(c, "elementProperties", "pageObjectId") for c in created} == {"LT", "LO", "LB"}
    inserted = [jstr(r, "insertText", "text") for r in p.requests if "insertText" in r]
    assert inserted.count("October 2026") == 3 and "September 2026" not in inserted
    assert {"slide": "layouts", "element": None, "fields": ["header and footer"]} in p.applied
    assert p.pending["header and footer"] == theme_sync.texts_digest(footers.side.texts)


def test_the_same_footer_writes_nothing(talk: Talk, footers: Footers) -> None:
    same = replace(footers.side, texts=jobjs(talk.v1.deck, "layout_texts"))
    p = footer_plan(talk, footers.pres, footers.base, same)
    assert not any("deleteObject" in r and jstr(r, "deleteObject", "objectId").startswith(emit.LAYOUT_TEXT_PREFIX)
                   for r in p.requests)
    assert p.applied == [] and p.conflicts == []


def test_a_footer_the_person_retyped_is_a_conflict_and_stays(talk: Talk, footers: Footers) -> None:
    pres = copy.deepcopy(footers.pres)
    elements = jarr(layout_page(pres, "LO"), "pageElements")
    elements[-1] = footer_box(jstr(elements[-1], "objectId"), "Draft - do not share", 480)
    p = footer_plan(talk, pres, footers.base, footers.side)
    assert not any("deleteObject" in r or "createShape" in r for r in p.requests)
    [c] = [c for c in p.conflicts if c["field"] == "header and footer"]
    assert c["resolution"] == "deck kept" and "Draft - do not share" in json.dumps(c["theirs"])
    assert "October 2026" in json.dumps(c["ours"])


def test_after_the_write_the_next_sync_writes_no_footer(talk: Talk, footers: Footers) -> None:
    p = footer_plan(talk, footers.pres, footers.base, footers.side)
    after = with_footers(talk.pres, theme_sync.texts_says(footers.side.texts))
    rec = new_record(footers.base["theme"], footers.side, p.written, after)
    assert jat(rec, "texts", "says", 2) == "October 2026"
    again = footer_plan(talk, after, {**footers.base, "theme": rec}, footers.side)
    assert again.conflicts == [] and not any("createShape" in r for r in again.requests)


def test_a_footer_an_interrupted_sync_wrote_is_its_own(talk: Talk, footers: Footers) -> None:
    after = with_footers(talk.pres, theme_sync.texts_says(footers.side.texts))
    digest = theme_sync.texts_digest(footers.side.texts)
    base: JsonObject = {**footers.base, "pending": {"theme": {"header and footer": digest}}}
    p = footer_plan(talk, after, base, footers.side)
    assert p.conflicts == [] and "header and footer" in p.written
    assert not any("createShape" in r for r in p.requests)


def test_a_base_older_than_footer_sync_leaves_them_and_says_so(talk: Talk, footers: Footers) -> None:
    theme: JsonObject = {k: v for k, v in jobj(footers.base, "theme").items() if k != "texts"}
    p = footer_plan(talk, footers.pres, {**footers.base, "theme": theme}, footers.side)
    assert not any("createShape" in r for r in p.requests)
    assert any("header and footer" in w and "'October 2026'" in w for w in p.warnings)
    same = footer_plan(talk, footers.pres, {**footers.base, "theme": theme},
                       replace(footers.side, texts=jobjs(talk.v1.deck, "layout_texts")))
    assert not any("header and footer" in w for w in same.warnings)


# ---------------------------------------------------------------- slides and old bases

class FakeSync(sync.Sync):
    def __init__(self, base: JsonObject, side: ThemeSide) -> None:
        self.base, self.theme_side, self.theme_plan = base, side, None


def test_a_slide_showing_the_new_shared_background_inherits_the_master(talk: Talk) -> None:
    s = FakeSync(talk.base, talk.side2)
    key = talk.side2.shared
    assert key is not None
    pres = copy.deepcopy(talk.pres)
    no_slide: JsonObject = {}
    assert s.background_requests("S2", key, no_slide, as_presentation(pres), False) == []   # (it inherits already)
    jobj(pres, "slides", 2)["pageProperties"] = {"pageBackgroundFill": {"solidFill": {"color": {"rgbColor": {"red": 1}}}}}
    assert s.background_requests("S2", key, no_slide, as_presentation(pres), False) == [{"updatePageProperties": {
        "objectId": "S2", "fields": "pageBackgroundFill.propertyState",
        "pageProperties": {"pageBackgroundFill": {"propertyState": "INHERIT"}}}}]
    assert s.background_requests("S9", key, no_slide, as_presentation(pres), True) == []


def test_an_old_base_leaves_the_layouts_alone_and_says_so(talk: Talk) -> None:
    old: JsonObject = {k: v for k, v in talk.base.items() if k != "theme"}
    s = FakeSync(old, talk.side2)
    assert s.master_key() == talk.side1.shared   # (the old behaviour)
    assert "older than theme sync" in (theme_sync.old_base_warning(old, talk.side2) or "")
    assert theme_sync.old_base_warning(old, talk.side1) is None


@pytest.mark.parametrize("name, group", [("TITLE", "TITLE"), ("TITLE_ONLY", "*"), ("BLANK", "*"),
                                         ("TITLE_ONLY_V2", "*_V2"), ("TITLE_V1", "TITLE_V1")])
def test_groups_follow_emits_layout_names(name: str, group: str) -> None:
    assert theme_sync.group_of(name) == group


def placeholder_shape(oid: str, parent: str | None, runs: list[tuple[str, JsonObject]]) -> JsonObject:
    """A placeholder element whose text is `runs` (text, style) with a paragraph marker first."""
    elements: list[Json] = [{"startIndex": 0, "endIndex": 0, "paragraphMarker": {"style": {}}}]
    at_index = 0
    for text, style in runs:
        elements.append({"startIndex": at_index, "endIndex": at_index + len(text),
                         "textRun": {"content": text, "style": style}})
        at_index += len(text)
    ph: JsonObject = {"type": "TITLE"} if parent is None else {"type": "TITLE", "parentObjectId": parent}
    return {"objectId": oid, "shape": {"placeholder": ph, "text": {"textElements": elements}}}


def test_placeholders_whose_parents_name_each_other_end_the_chain() -> None:
    """theme_sync.inherited_pins (placeholder_chain): the old loop stopped on `parent not in chain`,
    an id looked for among element dicts, so it never stopped. A layout and a master placeholder
    naming each other as parent hung the sync forever; each is now taken once."""
    import threading
    no_style: JsonObject = {}
    pres: JsonObject = {"presentationId": "P", "slides": [{"objectId": "S1", "pageElements": [
                            placeholder_shape("s1_title", "l_title", [("Title\n", no_style)])]}],
                        "layouts": [{"objectId": "L1", "pageElements": [
                            placeholder_shape("l_title", "m_title", [("\n", {"fontSize": {"magnitude": 30, "unit": "PT"}})])]}],
                        "masters": [{"objectId": "M1", "pageElements": [
                            placeholder_shape("m_title", "l_title", [("\n", {"fontSize": {"magnitude": 20, "unit": "PT"}})])]}]}
    answer: list[tuple[list[JsonObject], list[str]]] = []

    def pins() -> None:
        answer.append(theme_sync.inherited_pins(as_presentation(pres), {"m_title": {"fields": "fontSize"}}, {"S1"}))
    worker = threading.Thread(target=pins, daemon=True)
    worker.start()
    worker.join(10)
    assert answer, "inherited_pins went round the placeholders' cycle"
    reqs, touched = answer[0]
    assert touched == ["s1_title"]
    # The nearest placeholder's size, as the slide showed it before the sync.
    assert jat(reqs[0], "updateTextStyle", "style") == {"fontSize": {"magnitude": 30, "unit": "PT"}}
