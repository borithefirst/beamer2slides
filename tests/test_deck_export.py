"""A deck Drive will not export whole comes out in parts (`deck_export`): Drive copies cut down to
some of the slides, each export paired with its own slides of the read, every copy deleted, and
only a refusal about size - never one about permission - worth a copy. No Google calls: a fake
Drive/Slides pair whose export of more than K slides is refused as too large."""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Generic, NoReturn, TypeVar

import pytest

from beamer2slides import deck_export, google_auth, guard
from beamer2slides.deck_pictures import LivePictures
from beamer2slides.gapi import HttpError
from beamer2slides.google_auth import Services
from beamer2slides.google_types import (AboutResource, BatchUpdateResponse, Comments, DriveFile, DriveService, Empty,
                                        Pages, Permissions, Presentation, Presentations, Revisions, SlidesService,
                                        presentation)
from beamer2slides.json_types import Json, JsonObject, as_array, as_objects, as_str

from .test_deck_pictures import pptx

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides.google_types import (CopyFile, CreateFile, CreatePresentation, ExecuteOptions, ExportFile,
                                            FileId, FileList, GetFile, GetPresentation, ListFiles, UpdateFile,
                                            UpdatePresentation)

T_co = TypeVar("T_co", covariant=True)
EMU = 12700
# one picture on a slide, as a .pptx export writes it: its blip is relationship r1
PIC = '<p:pic><p:nvPicPr><p:cNvPr id="2" name="x"/></p:nvPicPr><p:blipFill><a:blip r:embed="r1"/></p:blipFill></p:pic>'


def api_error(status: int, reason: str, message: str) -> HttpError:
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}
    return HttpError(SimpleNamespace(status=status, reason=message), json.dumps(body).encode())


def too_large() -> HttpError:
    return api_error(403, "exportSizeLimitExceeded", "This file is too large to be exported.")


def not_allowed() -> HttpError:
    return api_error(403, "insufficientFilePermissions", "The user does not have sufficient permissions for this file.")


def png(k: int) -> bytes:
    """Slide k's picture: a mark of its own, so that each part's pictures are told apart."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (40, 20), "white")
    ImageDraw.Draw(img).rectangle((k, 1, k + 9, 9), fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def fail(error: BaseException) -> NoReturn:
    raise error


def unused(call: str) -> NoReturn:
    raise AssertionError(f"deck_export makes no {call}")


@dataclass(frozen=True, kw_only=True)
class Answer(Generic[T_co]):
    """One call, made when executed (`google_types.Request`): `run` answers it or raises."""
    run: Callable[[], T_co]

    def execute(self, **options: Unpack[ExecuteOptions]) -> T_co:
        return self.run()


def answer(value: T_co) -> Answer[T_co]:
    return Answer(run=lambda: value)


def refused(error: BaseException) -> Answer[NoReturn]:
    return Answer(run=lambda: fail(error))


class World:
    """Drive files as lists of slide ids, one picture per slide. `limit`: an export of more slides
    than that is too large; `huge`: a slide whose export is too large whatever else is with it;
    `broken`: copies whose export raises this; `renumber`: copies get ids of their own;
    `refusing`: what an export of a file raises before any of that (None: nothing); `bare`: slides
    with no picture at all."""

    def __init__(self, n: int, limit: int, huge: set[str], renumber: bool) -> None:
        self.lock = threading.Lock()
        self.decks = {"P": [f"S{k}" for k in range(1, n + 1)]}
        self.pictures = {f"S{k}": png(k) for k in range(1, n + 1)}
        self.limit, self.huge, self.renumber = limit, huge, renumber
        self.broken: BaseException | None = None
        self.bare: set[str] = set()
        self.refusing: Callable[[str], BaseException | None] = lambda fid: None
        self.calls: list[tuple[str, ...]] = []
        self.bodies: list[Mapping[str, object]] = []
        self.running = self.most = 0
        self.delay = 0.0
        self.drive, self.slides = FakeDrive(self), FakeSlides(self)

    def pres(self) -> Presentation:
        slides: list[Json] = [{"objectId": s, "pageElements": [] if s in self.bare else [
            {"objectId": f"i{s}", "image": {"contentUrl": f"u-i{s}"},
             "size": {"width": {"magnitude": 40 * EMU, "unit": "EMU"},
                      "height": {"magnitude": 20 * EMU, "unit": "EMU"}}}]}
            for s in self.decks["P"]]
        return presentation({"presentationId": "P", "slides": slides}, "the world's deck")

    def services(self) -> Services:
        return {"drive": self.drive, "slides": self.slides}

    def clients(self) -> tuple[DriveService, SlidesService]:
        return self.drive, self.slides

    def export(self, fid: str) -> bytes:
        with self.lock:
            self.calls.append(("export", fid))
            self.running += 1
            self.most = max(self.most, self.running)
            slides = [s.rstrip("x") for s in self.decks[fid]]
        try:
            if fid != "P" and self.broken is not None:
                raise self.broken
            time.sleep(self.delay)
            if len(slides) > self.limit or self.huge & set(slides):
                raise too_large()
            return pptx([("", {}, None) if s in self.bare else (PIC, {"r1": self.pictures[s]}, None) for s in slides])
        finally:
            with self.lock:
                self.running -= 1

    def copies(self) -> list[str]:
        return [c[2] for c in self.calls if c[0] == "copy"]

    def left(self) -> list[str]:
        return sorted(f for f in self.decks if f != "P")


class FakeFiles:
    """Drive's `files()`: copies, exports and deletes of the world's decks."""

    def __init__(self, world: World) -> None:
        self.world = world

    def copy(self, **kw: Unpack[CopyFile]) -> Answer[DriveFile]:
        w, fid = self.world, kw["fileId"]
        with w.lock:
            new = f"C{sum(1 for c in w.calls if c[0] == 'copy') + 1}"
            w.calls.append(("copy", fid, new))
            w.bodies.append(dict(kw.get("body", {})))
            w.decks[new] = [f"{s}x" if w.renumber else s for s in w.decks[fid]]
        made: DriveFile = {"id": new}
        return answer(made)

    def export_media(self, **kw: Unpack[ExportFile]) -> Answer[bytes]:
        fid = kw["fileId"]
        error = self.world.refusing(fid)
        if error is not None:
            return refused(error)
        return Answer(run=lambda: self.world.export(fid))

    def delete(self, **kw: Unpack[FileId]) -> Answer[Empty]:
        w = self.world
        with w.lock:
            w.calls.append(("delete", kw["fileId"]))
            w.decks.pop(kw["fileId"])
        nothing: Empty = {}
        return answer(nothing)

    def list(self, **kw: Unpack[ListFiles]) -> Answer[FileList]:
        unused("files().list")

    def get(self, **kw: Unpack[GetFile]) -> Answer[DriveFile]:
        unused("files().get")

    def create(self, **kw: Unpack[CreateFile]) -> Answer[DriveFile]:
        unused("files().create")

    def update(self, **kw: Unpack[UpdateFile]) -> Answer[DriveFile]:
        unused("files().update")

    def get_media(self, **kw: Unpack[FileId]) -> Answer[bytes]:
        unused("files().get_media")

    def export(self, **kw: Unpack[ExportFile]) -> Answer[bytes]:
        unused("files().export")


class FakeDrive:
    def __init__(self, world: World) -> None:
        self.world = world

    def files(self) -> FakeFiles:     # (one per call: the world holds the state)
        return FakeFiles(self.world)

    def permissions(self) -> Permissions:
        unused("permissions()")

    def comments(self) -> Comments:
        unused("comments()")

    def revisions(self) -> Revisions:
        unused("revisions()")

    def about(self) -> AboutResource:
        unused("about()")


def doomed(requests: Sequence[Mapping[str, object]]) -> list[str]:
    """The slides a batch of `deleteObject` requests deletes."""
    out: list[str] = []
    for r in requests:
        d = r.get("deleteObject")
        oid = d.get("objectId") if isinstance(d, dict) else None
        assert isinstance(oid, str), f"not a deleteObject: {r}"
        out.append(oid)
    return out


class FakePresentations:
    """Slides' `presentations()`: a copy's slides deleted, and its slides read."""

    def __init__(self, world: World) -> None:
        self.world = world

    def batchUpdate(self, **kw: Unpack[UpdatePresentation]) -> Answer[BatchUpdateResponse]:
        w, pid = self.world, kw["presentationId"]
        gone = doomed(kw["body"]["requests"])
        with w.lock:
            w.calls.append(("batch", pid, str(len(gone))))
            if any(d not in w.decks[pid] for d in gone):
                return refused(api_error(400, "badRequest", "Invalid requests[0].deleteObject: no object"))
            w.decks[pid] = [s for s in w.decks[pid] if s not in gone]
        replies: BatchUpdateResponse = {}
        return answer(replies)

    def get(self, **kw: Unpack[GetPresentation]) -> Answer[Presentation]:
        w, pid = self.world, kw["presentationId"]
        with w.lock:
            w.calls.append(("get", pid))
        slides: list[Json] = [{"objectId": s} for s in w.decks[pid]]
        return answer(presentation({"presentationId": pid, "slides": slides}, pid))

    def create(self, **kw: Unpack[CreatePresentation]) -> Answer[Presentation]:
        unused("presentations().create")

    def pages(self) -> Pages:
        unused("presentations().pages()")


class FakeSlides:
    def __init__(self, world: World) -> None:
        self.world = world

    def presentations(self) -> Presentations:
        return FakePresentations(self.world)


def refuse(url: str) -> bytes:
    raise PermissionError(url)


def every_picture(world: World) -> dict[str, bytes]:
    return {f"i{s}": world.pictures[s] for s in world.decks["P"] if s not in world.bare}


def texts(v: Json, where: str) -> list[str]:
    return [as_str(x, where) for x in as_array(v, where)]


# ---------------------------------------------------------------- pictures

def test_a_deck_too_large_to_export_whole_gives_every_picture_from_its_parts() -> None:
    """Halves, then halves of what is still too large: each part's pictures go to its own slides'
    ids, and every copy is gone afterwards."""
    world = World(n=5, limit=2, huge=set(), renumber=False)
    with google_auth.use_services(world.services()):
        live = LivePictures(world.pres(), world.drive, refuse, 8, None, world.slides)
        assert live.get([f"iS{k}" for k in range(1, 6)]) == every_picture(world)
    # whole (5) refused; (1-3) refused, (4-5) out; (1-2) and (3) out
    assert live.copies == 4 and live.parts == 3 and live.exports == 5 and live.unexported == []
    assert world.left() == [] and sorted(c[1] for c in world.calls if c[0] == "delete") == world.copies()
    assert all(b["appProperties"] == {"b2sStaging": "P"} for b in world.bodies)


def test_parts_hold_only_the_slides_owning_a_picture() -> None:
    """Slides 2, 3 and 5 own no picture: no part holds them, so the halves are of 1, 4 and 6 alone,
    each part's pictures still go to their own slides' ids, and a slide too large alone is
    reported by its own place and id."""
    world = World(n=6, limit=3, huge={"S4"}, renumber=False)
    world.bare = {"S2", "S3", "S5"}
    with google_auth.use_services(world.services()):
        live = LivePictures(world.pres(), world.drive, refuse, 8, None, world.slides)
        assert live.get(["iS1", "iS4", "iS6"]) == {k: v for k, v in every_picture(world).items() if k != "iS4"}
    # whole (6) refused; (1, 4) refused, (6) out; (1) out, (4) refused alone
    assert live.copies == 4 and live.parts == 2 and live.unexported == ["S4"]
    assert world.left() == []
    with google_auth.use_services(world.services()):
        done = deck_export.export_deck(world.drive, world.slides, world.pres(), per_part=None,
                                       workers=deck_export.WORKERS, clients=None, only=["S1", "S4", "S6"])
    assert [p.slides for p in done.parts] == [(0,), (5,)]
    assert done.missing == [{"slides": [4, 4], "ids": ["S4"], "reason": "HTTP 403: This file is too large to be exported."}]
    assert done.summary()["parts"] == [[1], [6]]


def test_a_deck_with_no_slide_owning_a_picture_exports_one_slide_for_its_layouts() -> None:
    world = World(n=3, limit=1, huge=set(), renumber=False)
    world.bare = {"S1", "S2", "S3"}
    with google_auth.use_services(world.services()):
        done = deck_export.export_deck(world.drive, world.slides, world.pres(), per_part=None,
                                       workers=deck_export.WORKERS, clients=None, only=[])
    assert done.complete and [p.slides for p in done.parts] == [(0,)] and world.copies() == ["C1"]


def test_parts_are_exported_on_threads_three_at_a_time() -> None:
    world = World(n=9, limit=1, huge=set(), renumber=False)
    world.delay = 0.05
    made: list[int] = []

    def clients() -> tuple[DriveService, SlidesService]:
        made.append(threading.get_ident())
        return world.clients()
    done = deck_export.export_deck(world.drive, world.slides, world.pres(), per_part=1, workers=3,
                                   clients=clients, only=None)
    assert 1 < len(made) <= 3, "a client pair per thread"
    assert done.complete and [p.first for p in done.parts] == list(range(9))
    assert done.pictures(world.pres(), None) == every_picture(world)
    assert 1 < world.most <= 3 and world.left() == []


def test_a_timeout_is_a_size_too() -> None:
    world = World(n=2, limit=1, huge=set(), renumber=False)
    world.refusing = lambda fid: TimeoutError("timed out") if fid == "P" else None
    with google_auth.use_services(world.services()):
        done = deck_export.export_deck(world.drive, world.slides, world.pres(), per_part=None, workers=deck_export.WORKERS, clients=None, only=None)
    assert done.complete and len(done.parts) == 2 and done.refused is None


def test_a_permission_refusal_makes_no_copy() -> None:
    world = World(n=4, limit=2, huge=set(), renumber=False)
    world.refusing = lambda fid: not_allowed()
    with google_auth.use_services(world.services()):
        live = LivePictures(world.pres(), world.drive, refuse, 8, None, world.slides)
        assert live.get(["iS1"]) == {}
    assert world.copies() == [] and live.exports == 1 and live.copies == 0
    assert live.unexported == ["S1", "S2", "S3", "S4"]
    done = deck_export.export_deck(world.drive, world.slides, world.pres(), per_part=None, workers=deck_export.WORKERS, clients=None, only=None)
    assert done.refused is not None and "sufficient permissions" in done.refused and not done.parts


def test_a_slide_too_large_alone_is_reported_and_the_rest_come_out() -> None:
    world = World(n=4, limit=4, huge={"S3"}, renumber=False)
    with google_auth.use_services(world.services()):
        done = deck_export.export_deck(world.drive, world.slides, world.pres(), per_part=None, workers=deck_export.WORKERS, clients=None, only=None)
    assert [(p.first, p.end) for p in done.parts] == [(0, 2), (3, 4)]
    assert done.missing == [{"slides": [3, 3], "ids": ["S3"], "reason": "HTTP 403: This file is too large to be exported."}]
    got = done.pictures(world.pres(), None)
    assert got == {k: v for k, v in every_picture(world).items() if k != "iS3"}
    assert world.left() == [] and done.deleted == done.copies == 4   # (1-2, 3-4, 3, 4)


def test_copies_are_deleted_even_when_their_export_raises() -> None:
    world = World(n=4, limit=2, huge=set(), renumber=False)
    world.broken = api_error(400, "badRequest", "no")
    with google_auth.use_services(world.services()):
        done = deck_export.export_deck(world.drive, world.slides, world.pres(), per_part=None, workers=deck_export.WORKERS, clients=None, only=None)
    assert not done.parts and [m["slides"] for m in done.missing] == [[1, 2], [3, 4]]
    assert world.left() == [] and done.copies == done.deleted == 2
    # whatever it raises: the copy goes, and what it raised is the caller's to hear
    world.broken = RuntimeError("a bug")
    with google_auth.use_services(world.services()), pytest.raises(RuntimeError):
        deck_export.export_deck(world.drive, world.slides, world.pres(), per_part=None, workers=deck_export.WORKERS, clients=None, only=None)
    assert world.left() == []


def test_no_slides_client_is_the_whole_export_or_nothing() -> None:
    world = World(n=3, limit=2, huge=set(), renumber=False)
    live = LivePictures(world.pres(), world.drive, refuse, 8, None, None)
    assert live.get(["iS1"]) == {} and world.copies() == [] and live.exports == 1

    # a function that cannot make one is no client either
    def no_client() -> SlidesService:
        raise RuntimeError("no credentials on this thread")
    done = deck_export.export_deck(world.drive, no_client, world.pres(), per_part=None, workers=deck_export.WORKERS, clients=None, only=None)
    assert not done.parts and world.copies() == []


def test_a_copy_with_ids_of_its_own_is_cut_by_place() -> None:
    world = World(n=3, limit=2, huge=set(), renumber=True)
    with google_auth.use_services(world.services()):
        done = deck_export.export_deck(world.drive, world.slides, world.pres(), per_part=None, workers=deck_export.WORKERS, clients=None, only=None)
    assert done.complete and done.pictures(world.pres(), None) == every_picture(world)
    assert world.left() == []


def test_too_large_tells_size_from_permission() -> None:
    assert deck_export.too_large(too_large()) and deck_export.too_large(TimeoutError())
    assert not deck_export.too_large(not_allowed())
    assert not deck_export.too_large(api_error(404, "notFound", "File not found"))
    assert deck_export.too_large(api_error(403, "", "exportSizeLimitExceeded"))  # (the message alone)


# ---------------------------------------------------------------- a backup in parts

def test_a_backup_too_large_to_export_whole_is_kept_in_parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(n=3, limit=2, huge=set(), renumber=False)
    out = tmp_path / "talk"
    with google_auth.use_services(world.services()):
        # a rebuild's (`fallback`): parts, and still the Drive copy - the deck whole, where parts come
        # back as presentations of their own
        copied: list[str] = []

        def copy_in_drive(drive: DriveService, pid: str, name: str | None) -> JsonObject:
            copied.append(pid)
            return {"url": "u"}

        def trim_drive_backups(drive: DriveService, pid: str, made: str) -> guard.Trashed:
            return guard.Trashed(moved=[], refused=[])
        monkeypatch.setattr(guard, "copy_in_drive", copy_in_drive)
        monkeypatch.setattr(guard, "trim_drive_backups", trim_drive_backups)
        rebuild = guard.backup_deck(world.drive, "P", out, "file", "", True, world.slides)
        assert copied == ["P"] and rebuild["drive"] == {"url": "u"} and len(as_array(rebuild["parts"], "parts")) == 2
        result = guard.backup_deck(world.drive, "P", out, "file", "", False, world.slides)   # a sync's
    assert "file" not in result and "drive" not in result
    parts = as_objects(result["parts"], "parts")
    names = [as_str(p["file"], "file").rsplit("-", 3)[-3:] for p in parts]
    assert names == [["slides", "001", "002.pptx"], ["slides", "003", "003.pptx"]]
    assert [p["slides"] for p in parts] == [[1, 2], [3, 3]]
    assert guard.way_back_kept(result) and world.left() == []
    assert any("2 .pptx parts" in w for w in texts(result["warnings"], "warnings"))
    entry: JsonObject = {"presentationId": "P", "backup": result}
    assert any("slides 3-3" in line for line in guard.restore_hint(entry, "rebuild"))
    guard.record(out, entry)
    assert len(guard.backup_files(guard.read_log(out))) == 2
    assert guard.prune_backups(out, 1, None, False)["doomed"] == [], "a backup in parts is one backup"


def test_a_backup_missing_a_slide_is_no_way_back(tmp_path: Path) -> None:
    world = World(n=3, limit=3, huge={"S2"}, renumber=False)
    world.refusing = lambda fid: too_large() if fid == "P" else None
    with google_auth.use_services(world.services()):
        result = guard.backup_deck(world.drive, "P", tmp_path / "talk", "file", "", False, world.slides)
    assert result["parts_missing"] == [[2, 2]] and not guard.way_back_kept(result)
    assert any("slides 2-2 are in no part" in w for w in texts(result["warnings"], "warnings"))
