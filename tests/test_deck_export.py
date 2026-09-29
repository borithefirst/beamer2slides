"""A deck Drive will not export whole comes out in parts (`deck_export`): Drive copies cut down to
some of the slides, each export paired with its own slides of the read, every copy deleted, and
only a refusal about size - never one about permission - worth a copy. No Google calls: a fake
Drive/Slides pair whose export of more than K slides is refused as too large."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from beamer2slides import deck_export, google_auth, guard
from beamer2slides.deck_pictures import LivePictures
from beamer2slides.gapi import HttpError

from .test_deck_pictures import image, page, pic, png, pptx
from .test_guard import Request


def api_error(status: int, reason: str, message: str) -> HttpError:
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}
    return HttpError(SimpleNamespace(status=status, reason=message), json.dumps(body).encode())


def too_large():
    return api_error(403, "exportSizeLimitExceeded", "This file is too large to be exported.")


def not_allowed():
    return api_error(403, "insufficientFilePermissions", "The user does not have sufficient permissions for this file.")


class World:
    """Drive files as lists of slide ids, one picture per slide. `limit`: an export of more slides
    than that is too large; `huge`: a slide whose export is too large whatever else is with it;
    `broken`: copies whose export raises this; `renumber`: copies get ids of their own."""

    def __init__(self, n: int = 5, limit: int = 2, huge=(), renumber: bool = False):
        self.lock = threading.Lock()
        self.decks = {"P": [f"S{k}" for k in range(1, n + 1)]}
        self.pictures = {f"S{k}": png(mark=(k, 1, k + 9, 9)) for k in range(1, n + 1)}
        self.limit, self.huge, self.renumber = limit, set(huge), renumber
        self.broken: BaseException | None = None
        self.calls: list[tuple] = []
        self.bodies: list[dict] = []
        self.running = self.most = 0
        self.delay = 0.0

    def pres(self) -> dict:
        return {"presentationId": "P", "slides": [page(s, [image(f"i{s}")]) for s in self.decks["P"]]}

    def call(self, *what):
        with self.lock:
            self.calls.append(what)

    def files(self):          # (the world is its own Drive and Slides client, for one thread)
        return self

    def presentations(self):
        return self

    # ---- Drive
    def copy(self, fileId, body=None, fields=None):
        with self.lock:
            new = f"C{sum(1 for c in self.calls if c[0] == 'copy') + 1}"
            self.calls.append(("copy", fileId, new))
            self.bodies.append(body)
            self.decks[new] = [f"{s}x" if self.renumber else s for s in self.decks[fileId]]
        return Request({"id": new})

    def export_media(self, fileId, mimeType):
        return _Export(self, fileId)

    def delete(self, fileId):
        with self.lock:
            self.calls.append(("delete", fileId))
            self.decks.pop(fileId)
        return Request({})

    # ---- Slides
    def batchUpdate(self, presentationId, body):
        doomed = [r["deleteObject"]["objectId"] for r in body["requests"]]
        with self.lock:
            self.calls.append(("batch", presentationId, len(doomed)))
            if any(d not in self.decks[presentationId] for d in doomed):
                return Request(api_error(400, "badRequest", "Invalid requests[0].deleteObject: no object"))
            self.decks[presentationId] = [s for s in self.decks[presentationId] if s not in doomed]
        return Request({})

    def get(self, presentationId, fields=None):
        self.call("get", presentationId)
        return Request({"presentationId": presentationId,
                        "slides": [{"objectId": s} for s in self.decks[presentationId]]})

    def copies(self):
        return [c[2] for c in self.calls if c[0] == "copy"]

    def left(self):
        return sorted(f for f in self.decks if f != "P")


class _Export:
    def __init__(self, world: World, fid: str):
        self.world, self.fid = world, fid

    def execute(self):
        w = world = self.world
        with w.lock:
            w.calls.append(("export", self.fid))
            w.running += 1
            w.most = max(w.most, w.running)
            slides = [s.rstrip("x") for s in world.decks[self.fid]]
        try:
            if self.fid != "P" and w.broken is not None:
                raise w.broken
            time.sleep(w.delay)
            if len(slides) > w.limit or w.huge & set(slides):
                raise too_large()
            return pptx([(pic(rid="r1"), {"r1": w.pictures[s]}, None) for s in slides])
        finally:
            with w.lock:
                w.running -= 1


def refuse(url):
    raise PermissionError(url)


def every_picture(world):
    return {f"i{s}": world.pictures[s] for s in world.decks["P"]}


# ---------------------------------------------------------------- pictures

def test_a_deck_too_large_to_export_whole_gives_every_picture_from_its_parts():
    """Halves, then halves of what is still too large: each part's pictures go to its own slides'
    ids, and every copy is gone afterwards."""
    world = World(n=5, limit=2)
    with google_auth.use_services({"drive": world, "slides": world}):
        live = LivePictures(world.pres(), world, refuse, 8, None, world)
        assert live.get([f"iS{k}" for k in range(1, 6)]) == every_picture(world)
    # whole (5) refused; (1-3) refused, (4-5) out; (1-2) and (3) out
    assert live.copies == 4 and live.parts == 3 and live.exports == 5 and live.unexported == []
    assert world.left() == [] and sorted(c[1] for c in world.calls if c[0] == "delete") == world.copies()
    assert all(b["appProperties"] == {"b2sStaging": "P"} for b in world.bodies)


def test_parts_are_exported_on_threads_three_at_a_time():
    world = World(n=9, limit=1)
    world.delay = 0.05
    made = []
    done = deck_export.export_deck(world, world, world.pres(), per_part=1, workers=3,
                                   clients=lambda: made.append(threading.get_ident()) or (world, world))
    assert 1 < len(made) <= 3, "a client pair per thread"
    assert done.complete and [p.first for p in done.parts] == list(range(9))
    assert done.pictures(world.pres(), None) == every_picture(world)
    assert 1 < world.most <= 3 and world.left() == []


def test_a_timeout_is_a_size_too():
    world = World(n=2, limit=1)
    real = world.export_media

    def slow(fileId, mimeType):
        return Request(TimeoutError("timed out")) if fileId == "P" else real(fileId, mimeType)
    world.export_media = slow
    with google_auth.use_services({"drive": world, "slides": world}):
        done = deck_export.export_deck(world, world, world.pres())
    assert done.complete and len(done.parts) == 2 and done.refused is None


def test_a_permission_refusal_makes_no_copy():
    world = World(n=4)
    world.export_media = lambda fileId, mimeType: Request(not_allowed())
    with google_auth.use_services({"drive": world, "slides": world}):
        live = LivePictures(world.pres(), world, refuse, 8, None, world)
        assert live.get(["iS1"]) == {}
    assert world.copies() == [] and live.exports == 1 and live.copies == 0
    assert live.unexported == ["S1", "S2", "S3", "S4"]
    done = deck_export.export_deck(world, world, world.pres())
    assert "sufficient permissions" in done.refused and not done.parts


def test_a_slide_too_large_alone_is_reported_and_the_rest_come_out():
    world = World(n=4, limit=4, huge={"S3"})
    with google_auth.use_services({"drive": world, "slides": world}):
        done = deck_export.export_deck(world, world, world.pres())
    assert [(p.first, p.end) for p in done.parts] == [(0, 2), (3, 4)]
    assert done.missing == [{"slides": [3, 3], "ids": ["S3"], "reason": "HTTP 403: This file is too large to be exported."}]
    got = done.pictures(world.pres(), None)
    assert got == {k: v for k, v in every_picture(world).items() if k != "iS3"}
    assert world.left() == [] and done.deleted == done.copies == 4   # (1-2, 3-4, 3, 4)


def test_copies_are_deleted_even_when_their_export_raises():
    world = World(n=4, limit=2)
    world.broken = api_error(400, "badRequest", "no")
    with google_auth.use_services({"drive": world, "slides": world}):
        done = deck_export.export_deck(world, world, world.pres())
    assert not done.parts and [m["slides"] for m in done.missing] == [[1, 2], [3, 4]]
    assert world.left() == [] and done.copies == done.deleted == 2
    # whatever it raises: the copy goes, and what it raised is the caller's to hear
    world.broken = RuntimeError("a bug")
    with google_auth.use_services({"drive": world, "slides": world}), pytest.raises(RuntimeError):
        deck_export.export_deck(world, world, world.pres())
    assert world.left() == []


def test_no_slides_client_is_the_whole_export_or_nothing():
    world = World(n=3, limit=2)
    live = LivePictures(world.pres(), world, refuse, 8, None, None)
    assert live.get(["iS1"]) == {} and world.copies() == [] and live.exports == 1
    # a function that cannot make one is no client either
    done = deck_export.export_deck(world, lambda: 1 / 0, world.pres())
    assert not done.parts and world.copies() == []


def test_a_copy_with_ids_of_its_own_is_cut_by_place():
    world = World(n=3, limit=2, renumber=True)
    with google_auth.use_services({"drive": world, "slides": world}):
        done = deck_export.export_deck(world, world, world.pres())
    assert done.complete and done.pictures(world.pres(), None) == every_picture(world)
    assert world.left() == []


def test_too_large_tells_size_from_permission():
    assert deck_export.too_large(too_large()) and deck_export.too_large(TimeoutError())
    assert not deck_export.too_large(not_allowed())
    assert not deck_export.too_large(api_error(404, "notFound", "File not found"))
    assert deck_export.too_large(api_error(403, "", "exportSizeLimitExceeded"))  # (the message alone)


# ---------------------------------------------------------------- a backup in parts

def test_a_backup_too_large_to_export_whole_is_kept_in_parts(tmp_path, monkeypatch):
    world = World(n=3, limit=2)
    out = tmp_path / "talk"
    with google_auth.use_services({"drive": world, "slides": world}):
        # a rebuild's (`fallback`): parts, and still the Drive copy - the deck whole, where parts come
        # back as presentations of their own
        copied = []
        monkeypatch.setattr(guard, "copy_in_drive", lambda d, pid, name: copied.append(pid) or {"url": "u"})
        rebuild = guard.backup_deck(world, "P", out, "file", "", True, world)
        assert copied == ["P"] and rebuild["drive"] == {"url": "u"} and len(rebuild["parts"]) == 2
        result = guard.backup_deck(world, "P", out, "file", "", False, world)   # a sync's
    assert "file" not in result and "drive" not in result
    names = [p["file"].rsplit("-", 3)[-3:] for p in result["parts"]]
    assert names == [["slides", "001", "002.pptx"], ["slides", "003", "003.pptx"]]
    assert [p["slides"] for p in result["parts"]] == [[1, 2], [3, 3]]
    assert guard.way_back_kept(result) and world.left() == []
    assert any("2 .pptx parts" in w for w in result["warnings"])
    entry = {"presentationId": "P", "backup": result}
    assert any("slides 3-3" in line for line in guard.restore_hint(entry, "rebuild"))
    guard.record(out, entry)
    assert len(guard.backup_files(guard.read_log(out))) == 2
    assert guard.prune_backups(out, 1, None, False)["doomed"] == [], "a backup in parts is one backup"


def test_a_backup_missing_a_slide_is_no_way_back(tmp_path):
    world = World(n=3, limit=3, huge={"S2"})
    world.export_media = (lambda real: lambda fileId, mimeType:
                          Request(too_large()) if fileId == "P" else real(fileId, mimeType))(world.export_media)
    with google_auth.use_services({"drive": world, "slides": world}):
        result = guard.backup_deck(world, "P", tmp_path / "talk", "file", "", False, world)
    assert result["parts_missing"] == [[2, 2]] and not guard.way_back_kept(result)
    assert any("slides 2-2 are in no part" in w for w in result["warnings"])
