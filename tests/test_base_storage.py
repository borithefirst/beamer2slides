"""Where the sync base comes from when one deck is synced from two checkouts (docs/sync.md).

Drive holds the authoritative base; `<out>/sync/base.json` is only a cache. A checkout that kept
an older generation, or the base of another deck, must never sync against it while Drive has the
real one, and a Drive base that has gone missing must not silently take a stale cache's place
without saying so. No Google calls: the Drive client is faked.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from googleapiclient.errors import HttpError

from beamer2slides import snapshot
from beamer2slides.google_types import (AffineTransform, BatchUpdateResponse, DriveFile, Files, Page, PageElement,
                                        Presentation, Request, Size, SlidesRequest, all_elements, as_json, object_id)
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.typing_compat import override

from .fake_google import Answer, Fetcher, Later, NoDrive, NoFiles, NoPresentations, NoSlides
from .json_reads import jat, jobj

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides.google_types import CreateFile, FileId, GetFile, GetPresentation, UpdateFile, UpdatePresentation

PID = "deck-1"


def http_error(status: int) -> HttpError:
    return HttpError(resp=type("Resp", (), {"status": status, "reason": "no"})(), content=b"{}")


def base(pid: str, generation: int) -> JsonObject:
    return {"version": snapshot.VERSION, "generation": generation, "presentationId": pid, "slides": []}


class FakeDrive(NoFiles, NoDrive):
    """Just enough of the Drive client for snapshot's base storage: `props` (file id ->
    appProperties), `blobs` (file id -> bytes); `update_fails`: the recorded base file is gone."""

    def __init__(self, props: dict[str, dict[str, str]], blobs: dict[str, bytes], update_fails: bool) -> None:
        self.props = props
        self.blobs = blobs
        self.update_fails = update_fails
        self.created: list[Mapping[str, object]] = []
        self.pointed: list[tuple[str, Mapping[str, str | None]]] = []
        self.written: list[tuple[str, bytes]] = []

    # the client's shape: drive.files().get(...).execute()
    @override
    def files(self) -> Files:
        return self

    @override
    def get(self, **kw: Unpack[GetFile]) -> Request[DriveFile]:
        fid = kw["fileId"]
        if fid not in self.props and fid not in self.blobs:
            raise http_error(404)
        return Answer(DriveFile(name="A talk", parents=["folder-1"], appProperties=dict(self.props.get(fid, {}))))

    @override
    def get_media(self, **kw: Unpack[FileId]) -> Request[bytes]:
        fid = kw["fileId"]
        if fid not in self.blobs:
            raise http_error(404)
        return Answer(self.blobs[fid])

    @override
    def update(self, **kw: Unpack[UpdateFile]) -> Request[DriveFile]:
        fid, body, media_body = kw["fileId"], kw.get("body"), kw.get("media_body")
        if media_body is not None:
            if self.update_fails:
                raise http_error(404)
            self.blobs[fid] = media_body.getbytes(0, media_body.size())
            self.written.append((fid, self.blobs[fid]))
        if body and "appProperties" in body:
            props = self.props.setdefault(fid, {})
            for k, v in body["appProperties"].items():
                if v is None:
                    props.pop(k, None)
                else:
                    props[k] = v
            self.pointed.append((fid, body["appProperties"]))
        return Answer(DriveFile(id=fid))

    @override
    def create(self, **kw: Unpack[CreateFile]) -> Request[DriveFile]:
        body, media_body = kw["body"], kw.get("media_body")
        assert media_body is not None, "the base goes up with its content"
        fid = f"base-{len(self.created) + 1}"
        self.created.append(body)
        self.props[fid] = {k: v for k, v in body.get("appProperties", {}).items() if v is not None}
        self.blobs[fid] = media_body.getbytes(0, media_body.size())
        return Answer(DriveFile(id=fid))


def no_drive_base() -> FakeDrive:
    """Drive with the deck and no base recorded on it."""
    return FakeDrive({PID: {}}, {}, False)


def drive_with_base(stored: JsonObject, pid: str) -> FakeDrive:
    return FakeDrive({pid: {snapshot.BASE_PROPERTY: "base-0"}}, {"base-0": json.dumps(stored).encode("utf-8")}, False)


def loaded(got: tuple[JsonObject | None, str]) -> tuple[str, JsonObject]:
    """Where `load_base` found a base, and the base (which it must have found)."""
    b, where = got
    assert b is not None, f"no base ({where})"
    return where, b


# ---------------------------------------------------------------- load


def test_drive_wins_over_an_older_local_cache(tmp_path: Path) -> None:
    """The other checkout synced and stored generation 2; ours still caches generation 0."""
    snapshot.save_local(base(PID, 0), tmp_path)
    where, got = loaded(snapshot.load_base(PID, tmp_path, drive_with_base(base(PID, 2), PID), None, None))
    assert (where, got["generation"]) == ("drive", 2)


def test_the_local_cache_is_used_when_drive_has_no_base(tmp_path: Path) -> None:
    snapshot.save_local(base(PID, 3), tmp_path)
    where, got = loaded(snapshot.load_base(PID, tmp_path, no_drive_base(), None, None))
    assert (where, got["generation"]) == ("local", 3)


def test_a_deleted_drive_base_falls_back_to_the_cache(tmp_path: Path) -> None:
    """The pointer survives a Drive cleanup that removed the file itself."""
    drive = FakeDrive({PID: {snapshot.BASE_PROPERTY: "base-0"}}, {}, False)  # (no blob)
    snapshot.save_local(base(PID, 1), tmp_path)
    where, got = loaded(snapshot.load_base(PID, tmp_path, drive, None, None))
    assert (where, got["generation"]) == ("local", 1)


def test_a_cache_belonging_to_another_deck_is_refused(tmp_path: Path) -> None:
    """A folder reused for a different deck: syncing against that base would rewrite this one."""
    snapshot.save_local(base("other-deck", 0), tmp_path)
    assert snapshot.load_base(PID, tmp_path, no_drive_base(), None, None) == (None, "none")


def test_a_drive_base_naming_another_deck_is_refused(tmp_path: Path) -> None:
    snapshot.save_local(base(PID, 5), tmp_path)
    where, got = loaded(snapshot.load_base(PID, tmp_path, drive_with_base(base("other-deck", 9), PID), None, None))
    assert (where, got["generation"]) == ("local", 5)


def test_a_drive_base_that_is_no_json_object_is_reported_not_read_into(tmp_path: Path) -> None:
    """The file the deck names holds JSON, but not an object (a list): it was swept for a
    `cleanup` list before it was judged, and `.get` on a list crashed the load (fixed
    2026-09-29, when the load was typed). It is ignored and said so, and the cache stands in."""
    drive = FakeDrive({PID: {snapshot.BASE_PROPERTY: "base-0", snapshot.CLEANED_PROPERTY: "0"}},
                      {"base-0": b"[1, 2]"}, False)
    snapshot.save_local(base(PID, 2), tmp_path)
    problems: list[str] = []
    where, got = loaded(snapshot.load_base(PID, tmp_path, drive, problems, None))
    assert (where, got["generation"]) == ("local", 2)
    assert problems == ["the base stored in Drive was ignored: not a JSON object"]


def test_no_base_anywhere(tmp_path: Path) -> None:
    assert snapshot.load_base(PID, tmp_path, no_drive_base(), None, None) == (None, "none")


def test_without_a_drive_client_the_cache_still_works(tmp_path: Path) -> None:
    snapshot.save_local(base(PID, 7), tmp_path)
    where, got = loaded(snapshot.load_base(PID, tmp_path, None, None, None))
    assert (where, got["generation"]) == ("local", 7)


# ------------------------------------------------- the cleanup a sync has carried out

def cleaning(generation: int) -> JsonObject:
    return {**base(PID, generation), "cleanup": ["b2s_old1", "b2s_old2"]}


def test_a_cleanup_the_deck_says_is_done_is_not_read_again(tmp_path: Path) -> None:
    """The last thing a sync does is delete the objects it replaced, and the only news afterwards
    is that they are gone. That is one field of the deck's own appProperties, not the whole base
    again (`mark_cleaned`): a media update costs about 1.8 s whatever it carries."""
    drive = drive_with_base(cleaning(4), PID)
    drive.props[PID][snapshot.CLEANED_PROPERTY] = "4"
    where, got = loaded(snapshot.load_base(PID, tmp_path, drive, None, None))
    assert (where, "cleanup" in got) == ("drive", False)


def test_a_cleanup_of_another_generation_still_names_its_leftovers(tmp_path: Path) -> None:
    """The flag is about one generation: a sync that died after this one left real leftovers."""
    drive = drive_with_base(cleaning(5), PID)
    drive.props[PID][snapshot.CLEANED_PROPERTY] = "4"
    _, got = loaded(snapshot.load_base(PID, tmp_path, drive, None, None))
    assert got["cleanup"] == ["b2s_old1", "b2s_old2"]


def test_the_cache_is_read_by_the_same_flag(tmp_path: Path) -> None:
    snapshot.save_local(cleaning(2), tmp_path)
    drive = FakeDrive({PID: {snapshot.CLEANED_PROPERTY: "2"}}, {}, False)
    where, got = loaded(snapshot.load_base(PID, tmp_path, drive, None, None))
    assert (where, "cleanup" in got) == ("local", False)


def test_marking_the_cleanup_done_writes_no_base(tmp_path: Path) -> None:
    drive = drive_with_base(cleaning(4), PID)
    info = snapshot.deck_info(drive, PID)
    assert snapshot.mark_cleaned(drive, PID, 4, info) is None
    assert drive.written == []                                   # no media update at all
    assert drive.props[PID][snapshot.CLEANED_PROPERTY] == "4"
    assert info.get("appProperties", {})[snapshot.CLEANED_PROPERTY] == "4"  # (the caller's facts follow)


def test_a_flag_drive_will_not_take_is_said_so_the_base_can_go_up_instead(tmp_path: Path) -> None:
    class Refuses(FakeDrive):
        @override
        def update(self, **kw: Unpack[UpdateFile]) -> Request[DriveFile]:
            body = kw.get("body")
            if body and "appProperties" in body:
                raise http_error(403)
            return super().update(**kw)

    drive = Refuses({PID: {snapshot.BASE_PROPERTY: "base-0"}}, {"base-0": json.dumps(cleaning(4)).encode("utf-8")},
                    False)
    why = snapshot.mark_cleaned(drive, PID, 4, None)
    assert why and "HttpError" in why
    assert snapshot.mark_cleaned(None, PID, 4, None) == "no Drive service"


# ---------------------------------------------------------------- save


def test_saving_updates_the_file_the_deck_points_at(tmp_path: Path) -> None:
    drive = drive_with_base(base(PID, 1), PID)
    fid = snapshot.save_drive(drive, base(PID, 2), None, None)
    assert (fid, drive.created) == ("base-0", [])
    assert json.loads(gzip.decompress(drive.blobs["base-0"]))["generation"] == 2
    assert loaded(snapshot.load_base(PID, None, drive, None, None))[1]["generation"] == 2


# ------------------------------------------------- the two forms a Drive base comes in
#
# Until 2026-10-04 the base went up as plain JSON; since, gzip-compressed (`snapshot.stored_base`:
# the 48-slide ambiguous deck's 689 kB of JSON is 54 kB, the last upload of a conversion). A deck
# converted before keeps its plain file until its next store, so every read takes both.


def big_base(generation: int) -> JsonObject:
    """A base with enough in it for compression to show (its slides repeat, as a deck's do)."""
    slides: list[Json] = [{"objectId": f"b2s_s{i:03}", "elements": [{"key": f"t{j}", "text": "the same words " * 8}
                                                                     for j in range(6)]} for i in range(40)]
    return {**base(PID, generation), "slides": slides, "words": "é→∑ beyond ASCII"}


def test_a_plain_json_base_from_before_is_read() -> None:
    drive = drive_with_base(big_base(3), PID)                     # (the old form: plain UTF-8 JSON)
    _, got = loaded(snapshot.load_base(PID, None, drive, None, None))
    assert got == big_base(3)


def test_a_gzip_base_is_read() -> None:
    drive = FakeDrive({PID: {snapshot.BASE_PROPERTY: "base-0"}}, {"base-0": snapshot.stored_base(big_base(4))}, False)
    _, got = loaded(snapshot.load_base(PID, None, drive, None, None))
    assert got == big_base(4)


def test_what_goes_up_is_gzip_of_the_same_json_and_far_smaller() -> None:
    drive = drive_with_base(big_base(1), PID)
    snapshot.save_drive(drive, big_base(2), None, None)
    up = drive.written[-1][1]
    assert up.startswith(snapshot.GZIP_MAGIC)
    plain = json.dumps(big_base(2), ensure_ascii=False).encode("utf-8")
    assert gzip.decompress(up) == plain                    # the very JSON the plain form held
    assert len(up) * 10 < len(plain)
    assert snapshot.stored_base(big_base(2)) == up         # (the same base, the same bytes: mtime 0)


def test_an_old_plain_file_is_overwritten_in_place_and_read_back() -> None:
    """The deck keeps naming the same file: a plain one is replaced by the gzip form, not orphaned."""
    drive = drive_with_base(big_base(5), PID)
    assert snapshot.save_drive(drive, big_base(6), None, None) == "base-0" and drive.created == []
    assert loaded(snapshot.load_base(PID, None, drive, None, None))[1] == big_base(6)


def test_a_new_base_file_says_what_it_holds() -> None:
    drive = no_drive_base()
    snapshot.save_drive(drive, base(PID, 1), "Talk", None)
    assert drive.created[0]["mimeType"] == snapshot.BASE_MIME == "application/gzip"
    assert str(drive.created[0]["name"]).endswith("sync base.json.gz")


def test_a_broken_gzip_base_is_no_base_and_the_cache_stands_in(tmp_path: Path) -> None:
    """Cut short in transit or storage: unreadable, as a truncated plain file always was."""
    cut = snapshot.stored_base(big_base(9))[:40]
    drive = FakeDrive({PID: {snapshot.BASE_PROPERTY: "base-0"}}, {"base-0": cut}, False)
    snapshot.save_local(base(PID, 2), tmp_path)
    where, got = loaded(snapshot.load_base(PID, tmp_path, drive, None, None))
    assert (where, got["generation"]) == ("local", 2)
    with pytest.raises(ValueError, match="broken gzip"):
        snapshot.read_stored_base(cut)
    with pytest.raises(ValueError):
        snapshot.read_stored_base(b"\x1f\x8bnot gzip at all")


def test_a_base_given_as_text_is_still_read() -> None:
    """A client library that answers a download as str (the reader always took it)."""
    assert snapshot.read_stored_base(json.dumps(big_base(1))) == big_base(1)


def test_saving_creates_the_file_and_points_the_deck_at_it(tmp_path: Path) -> None:
    drive = no_drive_base()
    fid = snapshot.save_drive(drive, base(PID, 1), None, None)
    assert drive.created and drive.created[0]["parents"] == ["folder-1"]  # (beside the presentation)
    assert drive.props[PID][snapshot.BASE_PROPERTY] == fid
    assert loaded(snapshot.load_base(PID, None, drive, None, None))[1]["generation"] == 1


def test_a_vanished_base_file_is_replaced_not_lost(tmp_path: Path) -> None:
    """The recorded file was deleted: the save makes a new one and repoints the presentation."""
    drive = FakeDrive({PID: {snapshot.BASE_PROPERTY: "base-gone"}}, {}, True)
    fid = snapshot.save_drive(drive, base(PID, 4), None, None)
    assert fid != "base-gone" and drive.props[PID][snapshot.BASE_PROPERTY] == fid
    assert loaded(snapshot.load_base(PID, None, drive, None, None))[1]["generation"] == 4


def test_the_local_copy_round_trips(tmp_path: Path) -> None:
    path = snapshot.save_local(base(PID, 8), tmp_path)
    assert path == tmp_path / "sync" / "base.json"
    assert json.loads(path.read_text(encoding="utf-8"))["generation"] == 8


# ---------------------------------------------------------------- the overlay steps the deck holds


def test_sync_keeps_the_overlay_steps_the_deck_was_made_from() -> None:
    """`convert --overlays all` then a plain `sync` used to drop every step in between."""
    from beamer2slides.sync import overlay_mode
    assert overlay_mode(None, "all") == ("all", None)
    assert overlay_mode(None, "last") == ("last", None)


def test_a_base_without_the_field_means_the_default() -> None:
    """Bases recorded before this was written down."""
    from beamer2slides.sync import overlay_mode
    assert overlay_mode(None, None) == ("last", None)


def test_asking_for_the_other_steps_is_allowed_but_said_out_loud() -> None:
    from beamer2slides.sync import overlay_mode
    mode, warning = overlay_mode("last", "all")
    assert mode == "last" and warning is not None and "read as slides the source dropped" in warning


def test_asking_for_the_steps_the_deck_has_is_quiet() -> None:
    from beamer2slides.sync import overlay_mode
    assert overlay_mode("all", "all") == ("all", None)


def test_convert_records_the_mode_in_the_base(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """build_base writes it, so the next sync can read it."""
    from beamer2slides import snapshot as snap
    from beamer2slides.sync_model import DeckRead

    def read_presentation_of(pres: Presentation) -> DeckRead:
        return DeckRead(presentation_id=PID, revision_id="r1", page_size=(720, 405), layouts={},
                        master_background=None, slides=())
    monkeypatch.setattr(snap, "read_presentation_of", read_presentation_of)
    built = snap.build_base({"slides": []}, tmp_path, Presentation(presentationId=PID), {"slides": []},
                            tmp_path / "talk.pdf", 0, False, "all", None)
    assert built["overlays"] == "all"


# ---------------------------------------------------------------- the warning about a stale cache


def test_a_base_file_that_vanished_is_worth_a_warning(tmp_path: Path) -> None:
    """The pointer is still there, the file is not: another checkout may have synced since."""
    drive = FakeDrive({PID: {snapshot.BASE_PROPERTY: "base-gone"}}, {}, False)
    said = snapshot.stale_base_warning("local", drive, PID, None)
    assert said is not None and "may be older than the deck" in said


def test_a_deck_that_never_had_a_drive_base_is_no_warning(tmp_path: Path) -> None:
    assert snapshot.stale_base_warning("local", no_drive_base(), PID, None) is None


def test_no_warning_when_the_base_came_from_drive(tmp_path: Path) -> None:
    assert snapshot.stale_base_warning("drive", drive_with_base(base(PID, 0), PID), PID, None) is None


class Broken(FakeDrive):
    """A Drive that refuses to read the deck, with `status`."""

    def __init__(self, status: int) -> None:
        super().__init__({}, {}, False)
        self.status = status

    @override
    def get(self, **kw: Unpack[GetFile]) -> Request[DriveFile]:
        raise http_error(self.status)


def test_no_warning_when_the_deck_itself_cannot_be_read(tmp_path: Path) -> None:
    """Not our business here: the sync will fail on its own, and guessing would be noise."""
    assert snapshot.stale_base_warning("local", Broken(403), PID, None) is None


@pytest.mark.parametrize("status", [403, 404, 500])
def test_a_drive_that_refuses_everything_leaves_the_cache_in_charge(tmp_path: Path, status: int) -> None:
    snapshot.save_local(base(PID, 2), tmp_path)
    where, got = loaded(snapshot.load_base(PID, tmp_path, Broken(status), None, None))
    assert (where, got["generation"]) == ("local", 2)


# ---------------------------------------------------------------- the last step of a conversion
#
# `snapshot_after_convert` reads the deck, tags its objects and records the base. Two things it
# used to do it no longer does: read the deck a second time to learn the titles it had just
# written, and download the pictures after the tags instead of while they were being written.


def size() -> Size:
    return {"width": {"magnitude": 100, "unit": "PT"}, "height": {"magnitude": 20, "unit": "PT"}}


def transform() -> AffineTransform:
    return {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "PT"}


def element(oid: str) -> PageElement:
    return PageElement(objectId=oid, size=size(), transform=transform(),
                       shape={"shapeType": "TEXT_BOX", "text": {"textElements": []}})


def alt_text(oid: str, title: str) -> SlidesRequest:
    return {"updatePageElementAltText": {"objectId": oid, "title": title}}


def read_deck() -> Presentation:
    group = PageElement(objectId="g", elementGroup={"children": [as_json(element("b"), "b")]}, size=size(),
                        transform=transform())
    return Presentation(presentationId=PID, revisionId="r1",
                        slides=[Page(objectId="s1", pageElements=[element("a"), group])])


def titles_of(pres: Presentation) -> dict[str, str | None]:
    return {object_id(e): e.get("title") for e in all_elements(pres.get("slides", [])[0].get("pageElements", []), "test")}


def test_the_titles_a_tag_batch_wrote_are_put_in_rather_than_read_back() -> None:
    """What a second presentations.get would say, said locally - measured against the real thing
    on the 48-slide ambiguous deck (181 tags): the two bases are equal field for field."""
    pres = read_deck()
    after = snapshot.tagged(pres, [alt_text("a", "b2s:one/text/0"), alt_text("b", "b2s:one/image/0")], "r2")
    assert titles_of(after) == {"a": "b2s:one/text/0", "g": None, "b": "b2s:one/image/0"}
    assert after.get("revisionId") == "r2"                       # the batch's own answer says it
    assert titles_of(pres) == {"a": None, "g": None, "b": None}   # the read it was made from is left alone


def test_a_revision_the_batch_did_not_answer_with_leaves_the_read_as_it_was() -> None:
    after = snapshot.tagged(read_deck(), [alt_text("a", "b2s:one/text/0")], None)
    assert after.get("revisionId") == "r1"


def tagged_id(request: Mapping[str, object]) -> object:
    """The object an alt-text request tags."""
    alt = request.get("updatePageElementAltText")
    return alt.get("objectId") if isinstance(alt, Mapping) else None


class TaggingSlides(NoPresentations, NoSlides):
    """A Slides client that refuses whole batches, and one request inside them (`refuses`)."""

    def __init__(self, refuses: str | None) -> None:
        self.refuses, self.revision = refuses, 1
        self.batches: list[int] = []

    @override
    def presentations(self) -> TaggingSlides:
        return self

    @override
    def batchUpdate(self, **kw: Unpack[UpdatePresentation]) -> Request[BatchUpdateResponse]:
        reqs = kw["body"]["requests"]
        self.batches.append(len(reqs))
        if len(reqs) > 1 and self.refuses:
            raise http_error(400)
        if any(tagged_id(r) == self.refuses for r in reqs):
            raise http_error(400)
        self.revision += 1
        return Answer(BatchUpdateResponse(writeControl={"requiredRevisionId": f"r{self.revision}"}))


def test_only_the_tags_that_landed_are_put_into_the_read() -> None:
    """The API refuses an alt text on some placeholders; those titles are not in the deck, so they
    must not be in the base either."""
    slides = TaggingSlides("b")
    landed, revision = snapshot.write_tags(slides, PID, [alt_text("a", "b2s:one/text/0"),
                                                         alt_text("b", "b2s:one/image/0")])
    assert [tagged_id(r) for r in landed] == ["a"]
    assert titles_of(snapshot.tagged(read_deck(), landed, revision)) == {"a": "b2s:one/text/0", "g": None, "b": None}
    assert slides.batches == [2, 1, 1]                        # the batch, then one request at a time


def test_nothing_to_tag_costs_no_call() -> None:
    slides = TaggingSlides(None)
    assert snapshot.write_tags(slides, PID, []) == ([], None) and slides.batches == []


def picture_deck() -> tuple[JsonObject, Presentation]:
    read: JsonObject = {"slides": [{"objectId": "s1", "background": {"picture": "h0"},
                                    "objects": {"a": {"image": {"contentHash": "h1"}}}}]}
    pres = Presentation(slides=[Page(objectId="s1",
                                     pageProperties={"pageBackgroundFill": {"stretchedPictureFill": {"contentUrl": "u-bg"}}},
                                     pageElements=[PageElement(objectId="a", image={"contentUrl": "u-a"})])])
    return read, pres


def downloaded_again(url: str) -> bytes:
    pytest.fail(f"downloaded {url} again")


def test_pictures_signed_while_the_tags_were_written_are_not_fetched_again(fetcher: Fetcher) -> None:
    read, pres = picture_deck()
    fetcher(downloaded_again)
    assert snapshot.sign_pictures(read, pres, None, None, 8, {"a": "sig-a", "s1": "sig-bg"}, None, None, None, None) == 2
    assert jat(read, "slides", 0, "objects", "a", "image", "signature") == "sig-a"
    assert jat(read, "slides", 0, "background", "signature") == "sig-bg"


def test_a_picture_the_download_missed_is_simply_unsigned(fetcher: Fetcher) -> None:
    """`ready` comes from another thread; a picture it could not fetch leaves no signature, which
    is what a failed download has always left (sync then compares the contentHash alone)."""
    read, pres = picture_deck()
    fetcher(downloaded_again)
    snapshot.sign_pictures(read, pres, None, None, 8, {"a": "sig-a"}, None, None, None, None)
    assert "signature" not in jobj(read, "slides", 0, "background")


def url_signature(data: bytes) -> str:
    """A signature that says which URL the bytes came from (the fetchers here answer the URL)."""
    return f"sig({data.decode()})"


def test_the_signatures_are_by_the_id_that_owns_the_picture(monkeypatch: pytest.MonkeyPatch, fetcher: Fetcher) -> None:
    _, pres = picture_deck()
    fetcher(str.encode)
    monkeypatch.setattr(snapshot, "signature", url_signature)
    assert snapshot.picture_signatures(pres, 8, None, (), None) == {"a": "sig(u-a)", "s1": "sig(u-bg)"}


def test_the_installed_fetcher_reaches_the_worker_threads(monkeypatch: pytest.MonkeyPatch, fetcher: Fetcher) -> None:
    """Eight workers download the pictures, and a worker inherits no context: the fetcher is
    resolved on the calling thread and handed down, so urllib is never reached."""
    import threading

    from beamer2slides import net

    def urllib_fetch(url: str) -> bytes:
        pytest.fail(f"urllib fetched {url}")
    monkeypatch.setattr(net, "urllib_fetch", urllib_fetch)
    threads: set[str] = set()

    def fetch(url: str) -> bytes:
        threads.add(threading.current_thread().name)
        return url.encode()

    fetcher(fetch)
    monkeypatch.setattr(snapshot, "signature", url_signature)
    _, pres = picture_deck()
    assert snapshot.picture_signatures(pres, 8, None, (), None) == {"a": "sig(u-a)", "s1": "sig(u-bg)"}
    assert threading.current_thread().name not in threads, "the downloads ran on the pool"
    read, pres = picture_deck()
    assert snapshot.sign_pictures(read, pres, None, None, 8, None, None, None, None, None) == 2
    assert jat(read, "slides", 0, "objects", "a", "image", "signature") == "sig(u-a)"


def test_a_fetcher_that_raises_leaves_the_pictures_unsigned_and_the_base_whole(monkeypatch: pytest.MonkeyPatch,
                                                                               fetcher: Fetcher) -> None:
    """A harness's client raises its own types; none of them may escape the signing. A
    PermissionError - "not allowed" - is asked once, not three times with a sleep between."""
    from beamer2slides import net

    def no_sleep(seconds: float) -> None:
        pass
    monkeypatch.setattr(net.time, "sleep", no_sleep)
    asked: list[str] = []

    class Refused(Exception):
        pass

    def refuse(url: str) -> bytes:
        asked.append(url)
        raise Refused(url)

    fetcher(refuse)
    _, pres = picture_deck()
    assert snapshot.picture_signatures(pres, 8, None, (), None) == {}
    assert sorted(asked) == ["u-a", "u-a", "u-a", "u-bg", "u-bg", "u-bg"]  # (three tries each)

    asked.clear()

    def forbid(url: str) -> bytes:
        asked.append(url)
        raise PermissionError(url)

    fetcher(forbid)
    read, pres = picture_deck()
    assert snapshot.sign_pictures(read, pres, None, None, 8, None, None, None, None, None) == 2
    assert "signature" not in jobj(read, "slides", 0, "objects", "a", "image")
    assert sorted(asked) == ["u-a", "u-bg"]


class ReadingSlides(NoPresentations, NoSlides):
    """A Slides client whose presentations.get answers what `read` makes."""

    def __init__(self, read: Callable[[], Presentation]) -> None:
        self.read = read

    @override
    def presentations(self) -> ReadingSlides:
        return self

    @override
    def get(self, **kw: Unpack[GetPresentation]) -> Request[Presentation]:
        return Later(self.read)


def after_convert_without_writes(monkeypatch: pytest.MonkeyPatch, read: Callable[[], Presentation],
                                 pid: str) -> ReadingSlides:
    """`snapshot_after_convert` with its writes switched off: no tags, no theme record, and
    `converted_base` an empty base of deck `pid`. The Slides client it reads the deck with, which
    answers what `read` makes."""
    from beamer2slides import theme_sync

    def converted_base(*args: object) -> JsonObject:
        return {"slides": [], "generation": 0, "presentationId": pid}

    def write_tags(*args: object) -> tuple[list[JsonObject], str | None]:
        return [], None

    def record(*args: object) -> None:
        return None
    monkeypatch.setattr(snapshot, "converted_base", converted_base)
    monkeypatch.setattr(snapshot, "write_tags", write_tags)
    monkeypatch.setattr(theme_sync, "record", record)
    return ReadingSlides(read)


def test_a_failed_drive_save_of_the_base_is_said_not_only_printed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    """Convert's last step keeps the base locally when Drive refuses it, and says so through
    `problems` - where the agent layer turns it into a warning - instead of only printing."""
    from beamer2slides import google_auth
    from beamer2slides.emit_state import EmitState

    class Refusing(FakeDrive):
        @override
        def files(self) -> Files:
            raise http_error(403)

    slides = after_convert_without_writes(monkeypatch, read_deck, PID)
    state = EmitState(presentation_id=PID, url="u", scale=1.0, slides=(), contained=None, theme=None, previous=None)
    with google_auth.use_services({"slides": slides, "drive": Refusing({}, {}, False)}):
        problems: list[str] = []
        snapshot.snapshot_after_convert({"slides": []}, tmp_path, state, {"pdf": "x", "sha1": None}, "last", problems)
        assert len(problems) == 1 and "could not store the sync base in Drive" in problems[0]
        assert "no_base" in problems[0] and snapshot.local_path(tmp_path).exists()
        assert capsys.readouterr().out == ""
        snapshot.snapshot_after_convert({"slides": []}, tmp_path, state, {"pdf": "x", "sha1": None}, "last", None)
        assert "warning: could not store the sync base in Drive" in capsys.readouterr().out
