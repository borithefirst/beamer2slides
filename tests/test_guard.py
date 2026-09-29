"""Offline tests of the rebuild guard (src/beamer2slides/guard.py, docs/sync.md): what counts as
a deck edit, the refusal `convert` raises before replacing a deck's content, the backups it keeps,
and the proof that sync's staging deck can never be the user's own deck. No Google calls."""

from __future__ import annotations

import copy
import io
import json
import os
import re
import threading
import time
from collections.abc import Callable, Collection
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, NoReturn

import pytest
from googleapiclient.errors import HttpError

from beamer2slides import google_types, guard, snapshot, sync_model
from beamer2slides.google_types import (DriveFile, Empty, FileList, Files, Presentation, Presentations,
                                        SlidesService, DriveService)
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.typing_compat import override

from . import made_bases, sync_work
from .fake_google import Answer, NoDrive, NoFiles, NoPresentations, NoSlides
from .json_reads import jarr, jat, jnum, jobj, jobjs, jstr

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides.google_types import (CopyFile, CreateFile, ExportFile, FileId, GetFile, GetPresentation,
                                            ListFiles, UpdateFile)

SRC = Path(guard.__file__).parent  # the package as imported, whatever layout it was staged in

Box = tuple[float, float, float, float]


# ---------------------------------------------------------------- a presentation to look at
# (built as the JSON presentations.get answers, changed as JSON, and read as the `Presentation` it
# is where the guard takes one: `as_presentation`)

def as_presentation(pres: JsonObject) -> Presentation:
    return google_types.presentation(pres, "the test deck")


def pt(v: float) -> JsonObject:
    return {"magnitude": v, "unit": "PT"}


def shape(oid: str, box: Box, text: str | None, title: str | None) -> JsonObject:
    x0, y0, x1, y1 = box
    e: JsonObject = {"objectId": oid, "size": {"width": pt(x1 - x0), "height": pt(y1 - y0)},
                     "transform": {"scaleX": 1, "scaleY": 1, "translateX": x0, "translateY": y0, "unit": "PT"}}
    if title is not None:     # (an object with no alt text has no title key: Google answers no null)
        e["title"] = title
    no_text: JsonObject = {}
    e["shape"] = {"shapeType": "TEXT_BOX", "text": {"textElements": [
        {"paragraphMarker": {"style": {"alignment": "START"}}},
        {"textRun": {"content": text + "\n", "style": {"fontFamily": "Lato", "fontSize": pt(18)}}}]}} if text else no_text
    return e


def picture(oid: str, box: Box, url: str) -> JsonObject:
    x0, y0, x1, y1 = box
    return {"objectId": oid, "size":{"width": pt(x1 - x0), "height": pt(y1 - y0)},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x0, "translateY": y0, "unit": "PT"},
            "image": {"contentUrl": url}}


def slide(sid: str, elements: list[Json], notes: str) -> JsonObject:
    return {"objectId": sid, "pageElements": elements,
            "pageProperties": {"pageBackgroundFill": {"propertyState": "INHERIT"}},
            "slideProperties": {"layoutObjectId": "L", "notesPage": {
                "notesProperties": {"speakerNotesObjectId": f"{sid}_notes"},
                "pageElements": [shape(f"{sid}_notes", (0, 0, 100, 50), notes or None, None)]}}}


def presentation() -> JsonObject:
    """Two converted slides: a title, a body and a picture each."""
    slides: list[Json] = []
    for n, name in enumerate(("Intro", "Results")):
        sid = f"b2s_s{n:03}"
        slides.append(slide(sid, [
            shape(f"{sid}_t0", (10, 10, 300, 40), name, f"b2s:{name.lower()}/text/title/0"),
            shape(f"{sid}_t1", (20, 60, 300, 100), f"First point of {name}", f"b2s:{name.lower()}/text/body/0"),
            picture(f"{sid}_f0", (320, 60, 460, 160), f"https://lh3.google.com/{name}=s0")],
            f"say something about {name}"))
    return {"presentationId": "P1", "revisionId": "rev1", "pageSize": {"width": pt(720), "height": pt(405)},
            "layouts": [{"objectId": "L", "layoutProperties": {"name": "TITLE_ONLY"}}],
            "masters": [{"objectId": "M", "pageProperties": {"pageBackgroundFill": {"solidFill": {
                "color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}}}],
            "slides": slides}


def made_element(e: JsonObject) -> made_bases.Made:
    """The element convert wrote `e` from, under the key its alt-text title names (a picture is
    untitled: image/figure/0)."""
    x, y = jnum(e, "transform", "translateX"), jnum(e, "transform", "translateY")
    box = (x, y, x + jnum(e, "size", "width", "magnitude"), y + jnum(e, "size", "height", "magnitude"))
    oid = jstr(e, "objectId")
    if "image" in e:
        return made_bases.Made(key="image/figure/0", object_id=oid,
                               element=made_bases.picture(oid, box, f"figures/{oid}.png"))
    key = jstr(e, "title").removeprefix("b2s:").split("/", 1)[1]
    words = jstr(e, "shape", "text", "textElements", 1, "textRun", "content").rstrip("\n")
    return made_bases.Made(key=key, object_id=oid, element=made_bases.text(oid, key.split("/")[1], box, words))


def base_for(pres: JsonObject, pdf: str) -> JsonObject:
    """The sync base as `convert` records it after writing `pres` from `pdf` (tests/made_bases.py):
    slide keys slide000, slide001, ..., element keys from the objects' alt-text titles."""
    live = as_presentation(pres)
    slides = [made_bases.MadeSlide(key=jstr(s, "objectId").replace("b2s_s", "slide"), object_id=jstr(s, "objectId"),
                                   notes=jstr(r, "notes"), background_color="#ffffff",
                                   elements=[made_element(e) for e in jobjs(s, "pageElements")])
              for s, r in zip(jobjs(pres, "slides"), jobjs(snapshot.read_presentation(live), "slides"))]
    return made_bases.made_base(live, slides, f"C:/talks/{pdf}", 0)


def base_of(pres: JsonObject) -> JsonObject:
    """`base_for` the deck converted from talk.pdf."""
    return base_for(pres, "talk.pdf")


def surveyed(base: JsonObject, pres: JsonObject, sign: bool, drive: DriveService | None) -> JsonObject:
    """`guard.survey` of a base as base.json holds it: parsed first, as `check_rebuild` does."""
    return guard.survey(sync_model.base(base), as_presentation(pres), sign, drive)


def edited(change: Callable[[JsonObject], object]) -> tuple[JsonObject, JsonObject]:
    """(base, live presentation) where `change` was applied to a copy of the live deck."""
    pres = presentation()
    base = base_of(pres)
    live = copy.deepcopy(pres)
    live["revisionId"] = "rev2"  # a write always bumps the revision
    change(live)
    return base, live


def find(pres: JsonObject, oid: str) -> JsonObject:
    return next(e for s in jobjs(pres, "slides") for e in jobjs(s, "pageElements") if e["objectId"] == oid)


def set_text(e: JsonObject, text: str) -> None:
    jobj(e, "shape", "text", "textElements", 1, "textRun")["content"] = text + "\n"


def base_readbacks(base: JsonObject) -> list[JsonObject]:
    """Every object's read-back the base holds."""
    return [jobj(el, "readback", oid) for s in jobjs(base, "slides") for el in jobjs(s, "elements")
            for oid in jobj(el, "readback")]


def reissue_picture_urls(pres: JsonObject) -> None:
    """A URL Google reissued for every picture: another path, the same pixels."""
    for s in jobjs(pres, "slides"):
        for e in jobjs(s, "pageElements"):
            if "image" in e:
                jobj(e, "image")["contentUrl"] = f"https://lh3.google.com/reissued-{jstr(e, 'objectId')}=s0"


# ---------------------------------------------------------------- what is not an edit

def test_untouched_deck_is_not_edited() -> None:
    pres = presentation()
    found = surveyed(base_of(pres), pres, True, None)
    assert found == {"edited": False, "revisionId": "rev1", "counts": {}, "slides": [], "slides_added": 0,
                     "slides_deleted": 0, "reordered": False, "examples": []}


def test_a_new_revision_alone_is_not_an_edit() -> None:
    """Google bumps revisionId on its own (opening a deck, thumbnails): only content counts."""
    pres = presentation()
    base = base_of(pres)
    later: JsonObject = {**copy.deepcopy(pres), "revisionId": "rev999"}
    assert surveyed(base, later, True, None)["edited"] is False


def test_scratch_slides_of_an_interrupted_run_are_not_an_edit() -> None:
    """measure_places' scratch slides (b2s_mNNN) are the converter's own leftovers, not the person's."""
    base, live = edited(lambda p: jarr(p, "slides").append(
        slide("b2s_m003", [shape("x", (0, 0, 10, 10), "scratch", None)], "")))
    assert surveyed(base, live, True, None)["edited"] is False


def png(colour: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def test_a_new_content_url_for_the_same_picture_is_not_an_edit(fetcher: Callable[[Callable[[str], bytes]], None]
                                                              ) -> None:
    """Google issues new contentUrls for pictures nobody touched: the pixels decide."""
    pres = presentation()
    base = base_of(pres)
    same, other = png((240, 240, 240), (8, 8)), png((20, 20, 20), (8, 8))
    for rb in base_readbacks(base):
        if "image" in rb:
            jobj(rb, "image")["signature"] = snapshot.signature(same)
    live = copy.deepcopy(pres)
    reissue_picture_urls(live)
    assert jat(snapshot.read_presentation(as_presentation(live)), "slides", 0, "objects", "b2s_s000_f0", "image",
               "contentHash") != \
        jat(base, "slides", 0, "elements", 2, "readback", "b2s_s000_f0", "image", "contentHash")

    fetcher(lambda url: same)
    assert surveyed(base, live, True, None)["edited"] is False
    fetcher(lambda url: other)
    found = surveyed(base, live, True, None)
    assert found["edited"] and jat(found, "counts", "image") == 2


def test_a_base_older_than_picture_signatures_cannot_clear_a_new_url_but_says_so(
        fetcher: Callable[[Callable[[str], bytes]], None]) -> None:
    """Bases written before pixel signatures (2026-09-17) hold only the URL hash, and Google
    reissues URLs for pictures nobody touched: the rule does not bend - still refused - but the
    finding is a picture that cannot be compared, and the message says what that means."""
    pres = presentation()
    base = base_of(pres)
    for rb in base_readbacks(base):
        if "image" in rb:
            jobj(rb, "image").pop("signature", None)
    live = copy.deepcopy(pres)
    reissue_picture_urls(live)
    fetcher(lambda url: png((240, 240, 240), (8, 8)))
    found = surveyed(base, live, True, None)
    assert found["edited"] and found["counts"] == {"image_unverified": 2}
    message = guard.refusal_message(jstr(pres, "presentationId"), Path("out/x"), "x.pdf", found, "edited")
    assert message.startswith("refusing to rebuild: this deck's sync base was written before beamer2slides "
                              "recorded picture signatures")
    assert "2 uncomparable pictures; nothing else differs" in message
    assert "picture replaced" not in message


def test_a_group_emit_named_and_slides_never_made_is_not_a_deletion() -> None:
    """A diagram of one node gets no group (Slides groups two objects or more), yet bases named
    that group as the element's main object: every rebuild of such a deck was refused as edited
    (19_labels_on_graphics, slide 11). A main object the base did have and the deck lost still is."""
    pres = presentation()
    base = base_of(pres)
    el = next(e for s in jobjs(base, "slides") for e in jobjs(s, "elements") if e.get("readback"))
    el["objects"] = ["never_made_g", *jarr(el, "objects")]
    el["main"] = "never_made_g"
    assert surveyed(base, pres, True, None)["edited"] is False
    real = next(iter(jobj(el, "readback")))
    el["main"] = real
    live = copy.deepcopy(pres)
    for s in jobjs(live, "slides"):
        kept: list[Json] = [e for e in jobjs(s, "pageElements") if e["objectId"] != real]
        s["pageElements"] = kept
    assert surveyed(base, live, True, None)["edited"] is True


# ---------------------------------------------------------------- what is an edit

def test_reworded_text_is_an_edit_with_a_readable_example() -> None:
    base, live = edited(lambda p: set_text(find(p, "b2s_s001_t1"), "Rewritten by hand"))
    found = surveyed(base, live, True, None)
    assert found["edited"] and found["counts"] == {"text": 1}
    assert [s["slide"] for s in jobjs(found, "slides")] == ["slide001"]
    assert jat(found, "slides", 0, "edits") == [{"element": "text/body/0", "field": "text", "objects": ["b2s_s001_t1"]}]
    assert found["examples"] == ['slide 2 "Results": text edited (text/body/0: “Rewritten by hand”)']
    assert guard.summary_line(found) == "1 slide edited: 1 text edit"


def test_moved_and_restyled_objects_are_edits() -> None:
    def change(p: JsonObject) -> None:
        e = find(p, "b2s_s000_t1")
        jobj(e, "transform")["translateY"] = 200
        jobj(find(p, "b2s_s001_t0"), "shape", "text", "textElements", 1, "textRun", "style")["bold"] = True
    base, live = edited(change)
    found = surveyed(base, live, True, None)
    assert found["counts"] == {"geometry": 1, "text_style": 1}
    assert guard.summary_line(found) == "2 slides edited: 1 move or resize, 1 style change"


def test_added_object_deleted_object_notes_and_background() -> None:
    def change(p: JsonObject) -> None:
        jarr(p, "slides", 0, "pageElements").append(shape("theirs1", (400, 300, 500, 340), "a note of my own", None))
        second = jobj(p, "slides", 1)
        kept: list[Json] = [e for e in jobjs(second, "pageElements") if e["objectId"] != "b2s_s001_f0"]
        second["pageElements"] = kept
        jobj(second, "slideProperties", "notesPage", "pageElements", 0, "shape", "text", "textElements", 1,
             "textRun")["content"] = "new notes\n"
        jobj(p, "slides", 0, "pageProperties")["pageBackgroundFill"] = {"solidFill": {"color": {"rgbColor": {"red": 1}}}}
    base, live = edited(change)
    found = surveyed(base, live, True, None)
    assert found["counts"] == {"objects_added": 1, "background": 1, "deleted": 1, "notes": 1}
    assert "1 object added in Slides" in guard.summary_line(found)


def test_slides_added_deleted_and_reordered() -> None:
    base, live = edited(lambda p: jarr(p, "slides").append(
        slide("mine", [shape("m0", (0, 0, 10, 10), "a slide I added", None)], "")))
    assert surveyed(base, live, True, None)["slides_added"] == 1
    base, live = edited(lambda p: jarr(p, "slides").pop(0))
    found = surveyed(base, live, True, None)
    assert found["slides_deleted"] == 1 and "deleted in Slides" in jstr(found, "examples", 0)
    base, live = edited(lambda p: jarr(p, "slides").reverse())
    assert surveyed(base, live, True, None)["reordered"] is True


# ---------------------------------------------------------------- the refusal

class Request(Answer[object]):
    """A call answering `result`, or raising it when it is an exception: what the fakes of other
    test modules answer (they import it by this name). The fakes here answer an `Answer` of the
    type their call's Protocol says."""

    def __init__(self, result: object) -> None:
        super().__init__(result)


def http_error(status: int, message: str) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason=message),
                     json.dumps({"error": {"message": message}}).encode())


def talk(trashed: bool) -> DriveFile:
    """The deck's Drive file, in the trash or not."""
    return DriveFile(id="P1", name="Talk", trashed=trashed, mimeType="application/vnd.google-apps.presentation")


class FakeFiles(NoFiles):
    def __init__(self, drive: FakeDrive) -> None:
        self.drive = drive

    @override
    def get(self, **kw: Unpack[GetFile]) -> google_types.Request[DriveFile]:
        fid = kw["fileId"]
        self.drive.calls.append(("get", fid))
        return Answer(self.drive.file if fid == self.drive.file.get("id") else http_error(404, "File not found"))

    @override
    def export_media(self, **kw: Unpack[ExportFile]) -> google_types.Request[bytes]:
        self.drive.calls.append(("export", kw["fileId"]))
        return Answer(self.drive.export)

    @override
    def copy(self, **kw: Unpack[CopyFile]) -> google_types.Request[DriveFile]:
        self.drive.calls.append(("copy", kw["fileId"]))
        if self.drive.copy_error is not None:
            return Answer(self.drive.copy_error)
        name = (kw.get("body") or {}).get("name")
        assert name is not None, "a copy is named"
        return Answer(DriveFile(id="COPY1", name=name))

    @override
    def create(self, **kw: Unpack[CreateFile]) -> google_types.Request[DriveFile]:
        self.drive.calls.append(("create", kw["body"].get("name")))
        return Answer(DriveFile(id="NEW1"))

    @override
    def update(self, **kw: Unpack[UpdateFile]) -> google_types.Request[DriveFile]:
        self.drive.calls.append(("update", kw["fileId"]))
        return Answer(DriveFile(id=kw["fileId"]))

    @override
    def delete(self, **kw: Unpack[FileId]) -> google_types.Request[Empty]:
        self.drive.calls.append(("delete", kw["fileId"]))
        return Answer(Empty())


class FakeDrive(NoDrive):
    """Drive holding `file` (the deck), whose export answers `export` and whose copy fails with
    `copy_error` (None: works); `calls` what was asked of it, as (call, file id or name)."""

    def __init__(self, file: DriveFile, export: bytes | HttpError, copy_error: HttpError | None) -> None:
        self.file = file
        self.export, self.copy_error = export, copy_error
        self.calls: list[tuple[str, str | None]] = []

    @override
    def files(self) -> Files:
        return FakeFiles(self)


def a_drive() -> FakeDrive:
    """Drive with the live deck, exporting and copying it as asked."""
    return FakeDrive(talk(False), b"PPTX", None)


class FakeSlides(NoPresentations, NoSlides):
    """A Slides client whose presentations.get answers `pres`."""

    def __init__(self, pres: JsonObject) -> None:
        self.pres = pres

    @override
    def presentations(self) -> Presentations:
        return self

    @override
    def get(self, **kw: Unpack[GetPresentation]) -> google_types.Request[Presentation]:
        return Answer(as_presentation(self.pres))


def out_with_base(tmp_path: Path, base: JsonObject | None) -> Path:
    out = tmp_path / "talk"
    out.mkdir()
    (out / "emit.json").write_text(json.dumps({"presentationId": "P1", "url": guard.deck_url("P1")}), encoding="utf-8")
    if base is not None:
        snapshot.save_local(base, out)
    return out


def test_check_rebuild_refuses_an_edited_deck_and_says_what_to_do(tmp_path: Path) -> None:
    base, live = edited(lambda p: set_text(find(p, "b2s_s001_t1"), "reworded in Slides"))
    out = out_with_base(tmp_path, base)
    with pytest.raises(guard.RebuildRefused) as refused:
        guard.check_rebuild(FakeSlides(live), a_drive(), "P1", out, Path("talk.pdf"), False)
    message = str(refused.value)
    assert "refusing to rebuild" in message and "edited in Google Slides" in message
    assert "1 slide edited: 1 text edit" in message
    assert f"sync talk.pdf --deck {out}" in message and "--force-rebuild" in message and "--new-deck" in message
    assert "revision rev2" in message
    assert refused.value.survey["reason"] == "edited"


def test_check_rebuild_lets_an_untouched_deck_through(tmp_path: Path) -> None:
    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    found = guard.check_rebuild(FakeSlides(pres), a_drive(), "P1", out, Path("talk.pdf"), False)
    assert found["reason"] == "" and found["edited"] is False and found["revisionId"] == "rev1"


def test_force_returns_the_finding_instead_of_raising(tmp_path: Path) -> None:
    base, live = edited(lambda p: jarr(p, "slides").pop())
    out = out_with_base(tmp_path, base)
    found = guard.check_rebuild(FakeSlides(live), a_drive(), "P1", out, Path("talk.pdf"), True)
    assert found["reason"] == "edited" and found["slides_deleted"] == 1


def test_a_deck_without_a_base_is_never_silently_rebuilt(tmp_path: Path) -> None:
    """Without a base nobody can say whether the deck was edited, so it isn't replaced by default."""
    out = out_with_base(tmp_path, None)
    with pytest.raises(guard.RebuildRefused) as refused:
        guard.check_rebuild(FakeSlides(presentation()), a_drive(), "P1", out, Path("talk.pdf"), False)
    assert refused.value.survey["reason"] == "no-base"
    assert "no sync base" in str(refused.value)


def test_a_folder_holding_another_pdfs_deck_is_not_rebuilt(tmp_path: Path) -> None:
    """Stale folder reuse: --out points at the deck of a different talk."""
    pres = presentation()
    out = out_with_base(tmp_path, base_for(pres, "other-talk.pdf"))
    with pytest.raises(guard.RebuildRefused) as refused:
        guard.check_rebuild(FakeSlides(pres), a_drive(), "P1", out, Path("talk.pdf"), False)
    assert refused.value.survey["reason"] == "other-source"
    assert "other-talk.pdf" in str(refused.value)
    # the same PDF under a longer path is the same source
    assert guard.check_rebuild(FakeSlides(pres), a_drive(), "P1", out,
                               Path("C:/elsewhere/other-talk.pdf"), False)["reason"] == ""


def test_a_base_that_does_not_parse_is_refused_never_rebuilt_over(tmp_path: Path) -> None:
    """A base.json that still says it is a base (version, slides, the deck's id: `load_base` takes
    it) but that `sync_model.base` refuses - an older form, a damaged file - answers nothing about
    the deck, and no sync could merge against it either. So it is refused as no base is, with the
    reason it could not be read; only the person's --force-rebuild goes past it."""
    pres = presentation()
    base = base_of(pres)
    del jobj(base, "slides", 1, "elements", 0, "fields")["text"]
    out = out_with_base(tmp_path, base)
    with pytest.raises(guard.RebuildRefused) as refused:
        guard.check_rebuild(FakeSlides(pres), a_drive(), "P1", out, Path("talk.pdf"), False)
    assert refused.value.survey["reason"] == "unreadable-base"
    message = str(refused.value)
    assert message.startswith("refusing to rebuild: this deck's sync base cannot be read")
    assert "slide slide001" in message and "'text' is missing" in message
    assert "--new-deck" in message and "--force-rebuild" in message and "beamer2slides sync" not in message
    found = guard.check_rebuild(FakeSlides(pres), a_drive(), "P1", out, Path("talk.pdf"), True)
    assert found["reason"] == "unreadable-base" and found["edited"] is False and found["slides"] == []


def test_the_bases_here_are_ones_convert_could_write() -> None:
    """base_of goes through snapshot's own steps (tests/made_bases.py): the base parses, and is the
    same JSON once read and written back - so what these tests show holds for a real base."""
    base = base_of(presentation())
    assert sync_model.base_json(sync_model.base(base)) == base
    assert sync_model.unread(base) == []


def test_slides_the_deck_never_had_are_each_named_by_their_own_title() -> None:
    """Two base slides with no objectId (a conversion whose deck lost them) are both deleted slides.
    The examples named each by the title of whichever such slide came last: the titles were looked
    up by objectId, and both had none."""
    pres = presentation()
    base = base_of(pres)
    for s in jobjs(base, "slides"):
        s["objectId"] = None
    found = surveyed(base, pres, True, None)
    assert found["slides_deleted"] == 2
    assert jarr(found, "examples")[:2] == ['slide 1 "Intro": deleted in Slides',
                                           'slide 2 "Results": deleted in Slides']


# ---------------------------------------------------------------- the second ask
#
# `convert` asks the guard twice: once while the PDF is being converted and once immediately
# before the write, because the deck may be edited in between. The second ask is about that
# in-between and nothing else, so where the deck is still at the revision the first one read, the
# first one's finding stands (guard.recheck) and the deck and the base are not read again.


class CountingSlides(NoPresentations, NoSlides):
    """A Slides client that remembers what was asked of it: the deck is at `revision` (an error:
    the read fails with it)."""

    def __init__(self, revision: str | HttpError) -> None:
        self.revision = revision
        self.asked: list[str | None] = []

    @override
    def presentations(self) -> Presentations:
        return self

    @override
    def get(self, **kw: Unpack[GetPresentation]) -> google_types.Request[Presentation]:
        self.asked.append(kw.get("fields"))
        if isinstance(self.revision, HttpError):
            return Answer(self.revision)
        return Answer(Presentation(revisionId=self.revision))


def a_finding(tmp_path: Path, change: JsonObject) -> JsonObject:
    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    return {**guard.check_rebuild(FakeSlides(pres), a_drive(), "P1", out, Path("talk.pdf"), False), **change}


def test_a_deck_still_at_its_revision_is_not_surveyed_again(tmp_path: Path) -> None:
    first = a_finding(tmp_path, {})
    slides = CountingSlides("rev1")
    again = guard.recheck(slides, "P1", first)
    assert again is not None
    assert again["reason"] == "" and again["revisionId"] == "rev1" and again["rechecked"] == "revision unchanged"
    assert slides.asked == ["revisionId"], "one field of one read, and no base"
    assert jstr(again, "checked") >= jstr(first, "checked")


def test_a_deck_edited_since_the_first_ask_is_asked_the_whole_question_again(tmp_path: Path) -> None:
    assert guard.recheck(CountingSlides("rev2"), "P1", a_finding(tmp_path, {})) is None


def test_a_finding_about_another_deck_is_never_reused(tmp_path: Path) -> None:
    assert guard.recheck(CountingSlides("rev1"), "P2", a_finding(tmp_path, {})) is None


def test_a_finding_with_a_reason_is_never_reused(tmp_path: Path) -> None:
    """One with a reason raised where it was made (or was forced); it says nothing about now."""
    assert guard.recheck(CountingSlides("rev1"), "P1", a_finding(tmp_path, {"reason": "edited"})) is None
    assert guard.recheck(CountingSlides("rev1"), "P1", None) is None


def test_a_read_that_fails_asks_the_whole_question_again(tmp_path: Path) -> None:
    assert guard.recheck(CountingSlides(http_error(404, "not found")), "P1", a_finding(tmp_path, {})) is None


def no_credentials() -> None:
    return None


def test_the_preflight_hands_its_finding_to_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """emit.preflight_rebuild -> emit.plan_rebuild: the journey `convert` and `deck_convert` make."""
    from beamer2slides import emit

    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    checked = emit.preflight_rebuild(out, Path("talk.pdf"), False, False, FakeSlides(pres), a_drive())
    assert checked is not None and checked.presentation_id == "P1" and checked.found["revisionId"] == "rev1"

    slides = CountingSlides("rev1")

    def slides_service(*creds: object) -> SlidesService:
        return slides

    def asked_again(*args: object, **kw: object) -> NoReturn:
        pytest.fail("asked the whole question again")
    monkeypatch.setattr(emit, "credentials_for_threads", no_credentials)
    monkeypatch.setattr(emit, "slides_service", slides_service)
    monkeypatch.setattr(guard, "check_rebuild", asked_again)
    pid, entry = emit.plan_rebuild(slides, a_drive(), out, False, False, "auto", Path("talk.pdf"), checked)
    assert entry is not None
    assert pid == "P1" and entry["reason"] == "no deck edits" and entry["revisionId"] == "rev1"
    assert slides.asked == ["revisionId"]


def test_a_deck_that_left_the_folder_between_the_two_asks_is_looked_at_properly(tmp_path: Path,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """The folder's deck is in the trash now: the preflight's finding is about a deck this run is
    no longer replacing, so it is dropped and the question asked again."""
    from beamer2slides import emit

    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    checked = emit.preflight_rebuild(out, Path("talk.pdf"), False, False, FakeSlides(pres), a_drive())

    def slides_service(*creds: object) -> SlidesService:
        return CountingSlides("rev1")
    monkeypatch.setattr(emit, "credentials_for_threads", no_credentials)
    monkeypatch.setattr(emit, "slides_service", slides_service)
    drive = FakeDrive(talk(True), b"PPTX", None)
    previous, found = emit.look_again(CountingSlides("rev1"), drive, out, checked)
    assert previous is not None
    assert previous["state"] == "trashed" and found is None


def test_with_no_preflight_the_second_ask_reads_drive_alone(tmp_path: Path) -> None:
    """No preflight finding: where the folder points, and no Slides read. The finding is a record
    now (`emit.Preflight`), so a deck id always comes with the finding `guard.recheck` is given;
    the dict it was could have had the one without the other."""
    from beamer2slides import emit

    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    slides = CountingSlides("rev1")
    previous, found = emit.look_again(slides, a_drive(), out, None)
    assert previous is not None and previous["presentationId"] == "P1" and previous["state"] == "live"
    assert found is None and slides.asked == []


# ---------------------------------------------------------------- backups and records

def test_backup_modes(tmp_path: Path) -> None:
    out = tmp_path / "talk"
    drive = a_drive()
    assert guard.backup_deck(drive, "P1", out, "none", "", True, None) == {"mode": "none", "warnings": []}
    assert drive.calls == []
    result = guard.backup_deck(drive, "P1", out, "file", "", True, None)
    assert Path(jstr(result, "file")).read_bytes() == b"PPTX" and result["bytes"] == 4
    result = guard.backup_deck(drive, "P1", out, "both", "", True, None)
    assert jstr(result, "drive", "url").endswith("COPY1/edit") and Path(jstr(result, "file")).exists()
    assert ("copy", "P1") in drive.calls


def test_a_refused_export_falls_back_to_a_drive_copy(tmp_path: Path) -> None:
    """A deck over Drive's 10 MB export limit must still leave a way back."""
    drive = FakeDrive(talk(False), http_error(403, "exportSizeLimitExceeded"), None)
    result = guard.backup_deck(drive, "P1", tmp_path / "talk", "file", "", True, None)
    assert "file" not in result and jat(result, "drive", "presentationId") == "COPY1"
    assert result["warnings"] and "10 MB" in jstr(result, "warnings", 0)


def a_backup(out: Path, name: str, age_days: float) -> JsonObject:
    path = guard.backup_dir(out) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"P" * 1000)
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))
    return {"presentationId": "P1", "action": "synced", "revisionId": name, "checked": name,
            "backup": {"mode": "file", "file": str(path), "warnings": []}}


def doomed_names(pruned: JsonObject) -> list[str]:
    return [Path(jstr(d, "file")).name for d in jobjs(pruned, "doomed")]


def test_prune_keeps_the_newest_backups_and_only_touches_its_own_files(tmp_path: Path) -> None:
    out = tmp_path / "talk"
    log = [a_backup(out, f"{n:02}.pptx", 0.0) for n in range(5)]
    stranger = guard.backup_dir(out) / "someone-elses.pptx"
    stranger.write_bytes(b"not ours")
    (guard.backup_dir(out) / "backups.json").write_text(json.dumps(log), encoding="utf-8")
    dry = guard.prune_backups(out, 2, None, False)
    assert dry["files"] == 5 and doomed_names(dry) == ["00.pptx", "01.pptx", "02.pptx"]
    assert dry["deleted"] is False and all(p.exists() for p in guard.backup_dir(out).glob("*.pptx"))
    done = guard.prune_backups(out, 2, None, True)
    assert done["deleted"] and done["freed"] == 3000
    assert sorted(p.name for p in guard.backup_dir(out).glob("*.pptx")) == \
        ["03.pptx", "04.pptx", "someone-elses.pptx"], "a file this program did not write is never deleted"
    kept: Json = json.loads((guard.backup_dir(out) / "backups.json").read_text(encoding="utf-8"))
    assert len(jobjs(kept)) == 5, "the record of what the deck was stays, even when its file is gone"
    assert [bool(jobj(e, "backup").get("deleted")) for e in jobjs(kept)] == [True, True, True, False, False]
    assert guard.prune_backups(out, 2, None, False)["doomed"] == []  # nothing left to prune


def test_prune_can_be_asked_to_keep_everything_recent(tmp_path: Path) -> None:
    out = tmp_path / "talk"
    log = [a_backup(out, "old.pptx", 30), a_backup(out, "new.pptx", 1)]
    (guard.backup_dir(out) / "backups.json").write_text(json.dumps(log), encoding="utf-8")
    r = guard.prune_backups(out, 0, 7, False)
    assert doomed_names(r) == ["old.pptx"]


def test_a_backup_neither_kind_of_which_worked_is_no_way_back(tmp_path: Path) -> None:
    """Both refused: the .pptx export (over 10 MB) and the Drive copy (a full Drive)."""
    drive = FakeDrive(talk(False), http_error(403, "exportSizeLimitExceeded"), http_error(403, "storageQuotaExceeded"))
    result = guard.backup_deck(drive, "P1", tmp_path / "talk", "file", "", True, None)
    assert "file" not in result and "drive" not in result and len(jarr(result, "warnings")) == 2
    assert not guard.way_back_kept(result)


def test_way_back_kept_needs_a_file_with_something_in_it(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pptx"
    empty.write_bytes(b"")
    assert not guard.way_back_kept({"warnings": []})
    assert not guard.way_back_kept({"file": str(tmp_path / "never-written.pptx")})
    assert not guard.way_back_kept({"file": str(empty)}), "an empty file restores nothing"
    empty.write_bytes(b"PPTX")
    assert guard.way_back_kept({"file": str(empty)})
    assert guard.way_back_kept({"drive": {"presentationId": "COPY1"}})


def a_way_back(monkeypatch: pytest.MonkeyPatch, lent: bool) -> None:
    """google_auth's clients as `WayBack` makes them: a caller's own client if `lent`, credentials
    "CREDS", and each client the (service, credentials) it was made with."""
    from beamer2slides import google_auth

    def shared_service(*args: object) -> bool:
        return lent

    def credentials_for_threads() -> str:
        return "CREDS"

    def slides_service(*creds: object) -> tuple[str, object]:
        return ("slides", creds[0] if creds else None)

    def drive_service(*creds: object) -> tuple[str, object]:
        return ("drive", creds[0] if creds else None)
    monkeypatch.setattr(google_auth, "shared_service", shared_service)
    monkeypatch.setattr(google_auth, "credentials_for_threads", credentials_for_threads)
    monkeypatch.setattr(google_auth, "slides_service", slides_service)
    monkeypatch.setattr(google_auth, "drive_service", drive_service)


def test_the_way_back_is_made_on_a_thread_with_clients_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """`record_sync_point` is three round trips that need nothing of the sync's planning and must
    be finished before its first write. They go on a thread, with credentials resolved on the
    calling thread (a worker inherits no context) and a client per thread."""
    a_way_back(monkeypatch, lent=False)
    made: list[tuple[object, object, str]] = []

    def make(slides: SlidesService, drive: DriveService) -> JsonObject:
        made.append((slides, drive, threading.current_thread().name))
        return {"entry": {"backup": {"file": "x.pptx"}}}

    point = guard.WayBack(make, "b2s-back")
    assert point.backup() == {"file": "x.pptx"}
    assert point.result() is point.result(), "made once and remembered"
    assert len(made) == 1 and made[0][:2] == (("slides", "CREDS"), ("drive", "CREDS"))
    assert made[0][2].startswith("b2s-back")


def test_a_way_back_nobody_asked_for_is_not_waited_for(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A sync that wrote nothing never asks: its caller reads `kept()` without waiting, and the
    exit does not join the thread (a daemon) - so what the thread writes goes down whole."""
    a_way_back(monkeypatch, lent=False)
    release = threading.Event()
    threads: list[threading.Thread] = []

    def make(slides: SlidesService, drive: DriveService) -> JsonObject:
        threads.append(threading.current_thread())
        release.wait(10)
        return {"entry": {}}

    point = guard.WayBack(make, "b2s-back")
    assert point.kept() is None and not point.asked, "not asked, not waited for"
    release.set()
    assert point.result() == {"entry": {}} and point.kept() == {"entry": {}}
    assert threads[0].daemon

    guard.write_whole(tmp_path / "b" / "backups.json", b"[]")
    assert [p.name for p in (tmp_path / "b").iterdir()] == ["backups.json"], "no .part left"


def test_a_lent_client_makes_the_way_back_on_the_asking_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service object a caller handed over is that caller's, used on one thread at a time
    (`emit.measure_places`' rule): no thread, and the work happens on the first ask."""
    a_way_back(monkeypatch, lent=True)
    made: list[str] = []

    def make(slides: SlidesService, drive: DriveService) -> JsonObject:
        made.append(threading.current_thread().name)
        return {}
    point = guard.WayBack(make, "b2s-back")
    assert made == [], "nothing is done until somebody is about to write"
    point.result()
    assert made == [threading.current_thread().name]


def test_a_way_back_that_could_not_be_made_is_no_reason_not_to_sync(monkeypatch: pytest.MonkeyPatch,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    a_way_back(monkeypatch, lent=True)

    def boom(slides: SlidesService, drive: DriveService) -> JsonObject:
        raise RuntimeError("Drive said no")

    point = guard.WayBack(boom, "b2s-back")
    assert point.result() is None and point.backup() == {}
    assert "Drive said no" in capsys.readouterr().out


class TaggedCopies(NoFiles, NoDrive):
    """A Drive holding the copies `copy_in_drive` tagged, listed over two pages; trashing one of
    `refuse` is refused."""

    def __init__(self, copies: list[DriveFile], refuse: Collection[str]) -> None:
        self.copies, self.refuse = copies, set(refuse)
        self.queries: list[str] = []
        self.trashed: list[str] = []

    @override
    def files(self) -> Files:
        return self

    @override
    def list(self, **kw: Unpack[ListFiles]) -> google_types.Request[FileList]:
        self.queries.append(kw.get("q", ""))
        half = len(self.copies) // 2
        if kw.get("pageToken") is None:
            return Answer(FileList(files=self.copies[:half], nextPageToken="p2"))
        return Answer(FileList(files=self.copies[half:]))

    @override
    def update(self, **kw: Unpack[UpdateFile]) -> google_types.Request[DriveFile]:
        fid = kw["fileId"]
        assert kw.get("body") == {"trashed": True}, "a prune only ever moves to the trash"
        if fid in self.refuse:
            return Answer(http_error(403, "insufficientFilePermissions"))
        self.trashed.append(fid)
        return Answer(DriveFile(id=fid))

    @override
    def delete(self, **kw: Unpack[FileId]) -> google_types.Request[Empty]:
        pytest.fail("a prune never deletes for good")


def test_a_prune_of_drive_backups_keeps_the_newest_and_only_ever_trashes() -> None:
    copies = [DriveFile(id=f"C{i}", name=f"backup {i}", createdTime=f"2026-09-{10 + i:02d}T00:00:00Z")
              for i in (4, 1, 3, 0, 2)]
    drive = TaggedCopies(copies, refuse={"C1"})
    looked = guard.prune_drive_backups(drive, "P1", 2, None, False)
    assert "key='b2sBackupOf' and value='P1'" in drive.queries[0] and "trashed = false" in drive.queries[0]
    assert [c["id"] for c in jobjs(looked, "doomed")] == ["C0", "C1", "C2"], "oldest first, the newest two kept"
    assert looked["copies"] == 5 and not looked["trashed"] and drive.trashed == [], "looking trashes nothing"

    done = guard.prune_drive_backups(drive, "P1", 2, None, True)
    assert drive.trashed == ["C0", "C2"] and done["trashed"]
    assert len(jarr(done, "warnings")) == 1 and "backup 1" in jstr(done, "warnings", 0)

    young = guard.prune_drive_backups(TaggedCopies(copies, ()), "P1", 0, 365 * 100, False)
    assert young["doomed"] == [], "nothing is older than a century"


def test_record_appends_and_restore_hint_reads(tmp_path: Path) -> None:
    out = tmp_path / "talk"
    entry: JsonObject = {"presentationId": "P1", "revisionId": "rev7", "action": "rebuilt in place",
                         "backup": {"file": str(out / "backups" / "x.pptx"), "warnings": []}}
    guard.record(out, entry)
    guard.record(out, {**entry, "revisionId": "rev8"})
    log: Json = json.loads((out / "backups" / "backups.json").read_text(encoding="utf-8"))
    assert [e["revisionId"] for e in jobjs(log)] == ["rev7", "rev8"]
    hint = "\n".join(guard.restore_hint(entry, "rebuild"))
    # The .pptx backup is the way back, and the hint says exactly how (Drive's version history
    # gives the deck's current content back for every revision, docs/sync.md).
    assert "rev7" in hint and "x.pptx" in hint and "deck_backup.py restore" in hint
    bare = "\n".join(guard.restore_hint({"presentationId": "P1", "revisionId": "rev7"}, "rebuild"))
    assert "no backup file was kept" in bare and "version history" in bare


# ---------------------------------------------------------------- emit's decision

def test_plan_rebuild_rebuilds_an_untouched_deck(tmp_path: Path) -> None:
    from beamer2slides.emit import plan_rebuild
    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    drive = a_drive()
    pid, entry = plan_rebuild(FakeSlides(pres), drive, out, False, False, "auto", Path("talk.pdf"), None)
    assert entry is not None
    assert pid == "P1" and entry["action"] == "rebuilt in place" and entry["revisionId"] == "rev1"
    assert entry["backup"] == {"mode": "none", "warnings": []}  # nothing to lose, nothing exported
    logged: Json = json.loads((out / "backups" / "backups.json").read_text(encoding="utf-8"))
    assert jat(logged, 0, "reason") == "no deck edits"


def test_plan_rebuild_backs_up_before_a_forced_rebuild(tmp_path: Path) -> None:
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    drive = a_drive()
    pid, entry = plan_rebuild(FakeSlides(live), drive, out, False, True, "auto", Path("talk.pdf"), None)
    assert entry is not None
    assert pid == "P1" and entry["reason"] == "edited"
    assert Path(jstr(entry, "backup", "file")).exists() and ("export", "P1") in drive.calls
    assert entry["revisionId"] == "rev2" and entry["examples"]


def test_plan_rebuild_refuses_without_force(tmp_path: Path) -> None:
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    drive = a_drive()
    with pytest.raises(guard.RebuildRefused):
        plan_rebuild(FakeSlides(live), drive, out, False, False, "auto", Path("talk.pdf"), None)
    assert ("export", "P1") not in drive.calls and ("update", "P1") not in drive.calls


def test_a_forced_rebuild_whose_backup_failed_is_refused(tmp_path: Path) -> None:
    """The offer that makes `--force-rebuild` acceptable is the backup. When Drive refuses both
    the export and the copy, the deck must stay as it is: nothing else can bring it back."""
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    drive = FakeDrive(talk(False), http_error(403, "exportSizeLimitExceeded"), http_error(403, "storageQuotaExceeded"))
    with pytest.raises(guard.RebuildRefused) as refused:
        plan_rebuild(FakeSlides(live), drive, out, False, True, "auto", Path("talk.pdf"), None)
    message = str(refused.value)
    assert "refusing to rebuild" in message and "backup" in message
    assert "storageQuotaExceeded" in message and "--backup none" in message
    assert refused.value.survey["reason"] == "backup-failed"
    assert ("update", "P1") not in drive.calls
    # the failed attempt is in the log: what was tried, and that the deck is still the old one
    log: Json = json.loads((out / "backups" / "backups.json").read_text(encoding="utf-8"))
    logged = jobjs(log)[-1]
    assert logged["revisionId"] == "rev2" and jat(logged, "backup", "warnings")


def test_a_drive_copy_is_way_back_enough_for_a_forced_rebuild(tmp_path: Path) -> None:
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    drive = FakeDrive(talk(False), http_error(403, "exportSizeLimitExceeded"), None)
    pid, entry = plan_rebuild(FakeSlides(live), drive, out, False, True, "auto", Path("talk.pdf"), None)
    assert entry is not None
    assert pid == "P1" and jat(entry, "backup", "drive", "presentationId") == "COPY1"


def test_backup_none_says_out_loud_that_the_deck_may_go(tmp_path: Path) -> None:
    """`--backup none` is the way to ask for a rebuild without a way back, so it is not refused."""
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    drive = FakeDrive(talk(False), http_error(403, "exportSizeLimitExceeded"), http_error(403, "storageQuotaExceeded"))
    pid, entry = plan_rebuild(FakeSlides(live), drive, out, False, True, "none", Path("talk.pdf"), None)
    assert entry is not None
    assert pid == "P1" and entry["backup"] == {"mode": "none", "warnings": []}


def test_new_deck_leaves_the_old_one_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    pid, entry = plan_rebuild(FakeSlides(live), a_drive(), out, True, False, "auto", Path("talk.pdf"), None)
    assert entry is not None
    assert pid is None and entry["action"] == "new deck" and entry["state"] == "kept"
    assert "left as it is" in capsys.readouterr().out


def test_a_trashed_deck_is_not_resurrected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from beamer2slides.emit import plan_rebuild
    out = out_with_base(tmp_path, base_of(presentation()))
    drive = FakeDrive(talk(True), b"PPTX", None)
    pid, entry = plan_rebuild(FakeSlides(presentation()), drive, out, False, False, "auto", Path("talk.pdf"), None)
    assert entry is not None
    assert pid is None and entry["state"] == "trashed"
    assert "trash" in capsys.readouterr().out
    # a deck deleted in Drive: a new one, and no attempt to write to the old id
    drive = FakeDrive(DriveFile(id="gone"), b"PPTX", None)
    pid, entry = plan_rebuild(FakeSlides(presentation()), drive, out, False, False, "auto", Path("talk.pdf"), None)
    assert entry is not None
    assert pid is None and entry["state"] == "gone"
    assert [c for c in drive.calls if c[0] != "get"] == []


def test_the_backup_modes_of_the_command_line_are_the_guards() -> None:
    from beamer2slides.__main__ import BACKUP_MODES
    assert BACKUP_MODES == guard.BACKUP_MODES


# ---------------------------------------------------------------- sync's staging deck

def test_sync_deletes_only_the_staging_deck_it_just_created(tmp_path: Path) -> None:
    """The staging deck's id comes from the files.create two lines above, and only that id is
    ever passed to files.delete: sync can never delete the user's own deck."""
    from beamer2slides.emit import DeckPlan
    picture_file = tmp_path / "fig.png"
    picture_file.write_bytes(png((10, 10, 10), (40, 30)))
    staged: JsonObject = {"slides": [{"objectId": "s", "pageElements": [
        {"objectId": "i", "description": "b2s-stage:0", "image": {"contentUrl": "https://staged"}}]}]}
    drive = a_drive()
    me = sync_work.bare_sync()
    me.drive, me.slides, me.pid = drive, FakeSlides(staged), "P1"
    me.plan = DeckPlan.__new__(DeckPlan)   # (only its slides' size is read: the staging deck's page)
    me.plan.deck = {"slides": [{"size": [720, 405]}]}
    fid = me.stage(sync_work.staged([], (), {str(picture_file): "figure"}), None, None)
    assert fid == "NEW1" != me.pid
    assert me.urls == {str(picture_file): "https://staged"}

    source = (SRC / "sync.py").read_text(encoding="utf-8")
    deletes = set(re.findall(r"files\(\)\.delete\(fileId=([\w.]+)\)", source))
    assert deletes == {"fid"}  # never self.pid, and never an id read from a file
    # ... and `delete_file`, the one other way to it, is only ever handed that same id
    assert set(re.findall(r"delete_file\([\w.]+, ([\w.]+)\)", source)) == {"fid"}
    assert re.search(r"fid = google_types\.file_id\(execute\(drive\.files\(\)\.create\(", source)
    assert re.search(r"staging = self\.stage\(work, None, None\)", source)
    # Staged on a thread of its own, with clients of its own: the id still comes from that create
    # and nowhere else, and the future is collected whatever happens (run's own `finally`).
    assert re.search(r"self\.in_background\(lambda slides, drive: self\.stage\(work, drive, slides\), \"b2s-stage\"\)",
                     source)
    assert re.search(r"staging = staged\.result\(\)", source)
    # Sent away on a thread at the end of the write, and `drop_staging`'s only caller hands it the
    # id that create returned; nobody leaves before the file is really gone.
    assert set(re.findall(r"self\.drop_staging\((\w+)\)", source)) == {"staging"}
    assert re.search(r"def drop_staging\(self, fid: str\)", source)
    assert re.search(r"s\.await_deletes\(\)", source)
