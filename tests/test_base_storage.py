"""Where the sync base comes from when one deck is synced from two checkouts (docs/sync.md).

Drive holds the authoritative base; `<out>/sync/base.json` is only a cache. A checkout that kept
an older generation, or the base of another deck, must never sync against it while Drive has the
real one, and a Drive base that has gone missing must not silently take a stale cache's place
without saying so. No Google calls: the Drive client is faked.
"""

import json

import pytest
from googleapiclient.errors import HttpError

from beamer2slides import snapshot

PID = "deck-1"


def http_error(status: int) -> HttpError:
    return HttpError(resp=type("Resp", (), {"status": status, "reason": "no"})(), content=b"{}")


def base(pid: str = PID, generation: int = 0) -> dict:
    return {"version": snapshot.VERSION, "generation": generation, "presentationId": pid, "slides": []}


class FakeDrive:
    """Just enough of the Drive client for snapshot's base storage."""

    def __init__(self, props: dict | None = None, blobs: dict | None = None, update_fails: bool = False):
        self.props = props or {}            # file id -> appProperties
        self.blobs = blobs or {}            # file id -> bytes
        self.update_fails = update_fails    # the recorded base file is gone
        self.created: list[dict] = []
        self.pointed: list[tuple[str, dict]] = []
        self.written: list[tuple[str, bytes]] = []

    # the client's shape: drive.files().get(...).execute()
    def files(self):
        return self

    @staticmethod
    def _request(result):
        return type("Request", (), {"execute": staticmethod(lambda: result)})()

    def get(self, fileId, fields=None):
        if fileId not in self.props and fileId not in self.blobs:
            raise http_error(404)
        return self._request({"name": "A talk", "parents": ["folder-1"], "appProperties": self.props.get(fileId, {})})

    def get_media(self, fileId):
        if fileId not in self.blobs:
            raise http_error(404)
        return self._request(self.blobs[fileId])

    def update(self, fileId, body=None, media_body=None, fields=None):
        if media_body is not None:
            if self.update_fails:
                raise http_error(404)
            self.blobs[fileId] = media_body.getbytes(0, media_body.size())
            self.written.append((fileId, self.blobs[fileId]))
        if body and "appProperties" in body:
            self.props.setdefault(fileId, {}).update(body["appProperties"])
            self.pointed.append((fileId, body["appProperties"]))
        return self._request({"id": fileId})

    def create(self, body=None, fields=None, media_body=None):
        fid = f"base-{len(self.created) + 1}"
        self.created.append(body)
        self.props[fid] = body.get("appProperties", {})
        self.blobs[fid] = media_body.getbytes(0, media_body.size())
        return self._request({"id": fid})


def drive_with_base(stored: dict, pid: str = PID) -> FakeDrive:
    return FakeDrive(props={pid: {snapshot.BASE_PROPERTY: "base-0"}},
                     blobs={"base-0": json.dumps(stored).encode("utf-8")})


# ---------------------------------------------------------------- load


def test_drive_wins_over_an_older_local_cache(tmp_path):
    """The other checkout synced and stored generation 2; ours still caches generation 0."""
    snapshot.save_local(base(generation=0), tmp_path)
    got, where = snapshot.load_base(PID, tmp_path, drive_with_base(base(generation=2)))
    assert (where, got["generation"]) == ("drive", 2)


def test_the_local_cache_is_used_when_drive_has_no_base(tmp_path):
    snapshot.save_local(base(generation=3), tmp_path)
    got, where = snapshot.load_base(PID, tmp_path, FakeDrive(props={PID: {}}))
    assert (where, got["generation"]) == ("local", 3)


def test_a_deleted_drive_base_falls_back_to_the_cache(tmp_path):
    """The pointer survives a Drive cleanup that removed the file itself."""
    drive = FakeDrive(props={PID: {snapshot.BASE_PROPERTY: "base-0"}})  # (no blob)
    snapshot.save_local(base(generation=1), tmp_path)
    got, where = snapshot.load_base(PID, tmp_path, drive)
    assert (where, got["generation"]) == ("local", 1)


def test_a_cache_belonging_to_another_deck_is_refused(tmp_path):
    """A folder reused for a different deck: syncing against that base would rewrite this one."""
    snapshot.save_local(base(pid="other-deck"), tmp_path)
    assert snapshot.load_base(PID, tmp_path, FakeDrive(props={PID: {}})) == (None, "none")


def test_a_drive_base_naming_another_deck_is_refused(tmp_path):
    snapshot.save_local(base(generation=5), tmp_path)
    got, where = snapshot.load_base(PID, tmp_path, drive_with_base(base(pid="other-deck", generation=9)))
    assert (where, got["generation"]) == ("local", 5)


def test_no_base_anywhere(tmp_path):
    assert snapshot.load_base(PID, tmp_path, FakeDrive(props={PID: {}})) == (None, "none")


def test_without_a_drive_client_the_cache_still_works(tmp_path):
    snapshot.save_local(base(generation=7), tmp_path)
    got, where = snapshot.load_base(PID, tmp_path, None)
    assert (where, got["generation"]) == ("local", 7)


# ------------------------------------------------- the cleanup a sync has carried out

def cleaning(generation: int) -> dict:
    return {**base(generation=generation), "cleanup": ["b2s_old1", "b2s_old2"]}


def test_a_cleanup_the_deck_says_is_done_is_not_read_again(tmp_path):
    """The last thing a sync does is delete the objects it replaced, and the only news afterwards
    is that they are gone. That is one field of the deck's own appProperties, not the whole base
    again (`mark_cleaned`): a media update costs about 1.8 s whatever it carries."""
    drive = drive_with_base(cleaning(4))
    drive.props[PID][snapshot.CLEANED_PROPERTY] = "4"
    got, where = snapshot.load_base(PID, tmp_path, drive)
    assert (where, "cleanup" in got) == ("drive", False)


def test_a_cleanup_of_another_generation_still_names_its_leftovers(tmp_path):
    """The flag is about one generation: a sync that died after this one left real leftovers."""
    drive = drive_with_base(cleaning(5))
    drive.props[PID][snapshot.CLEANED_PROPERTY] = "4"
    got, _ = snapshot.load_base(PID, tmp_path, drive)
    assert got["cleanup"] == ["b2s_old1", "b2s_old2"]


def test_the_cache_is_read_by_the_same_flag(tmp_path):
    snapshot.save_local(cleaning(2), tmp_path)
    drive = FakeDrive(props={PID: {snapshot.CLEANED_PROPERTY: "2"}})
    got, where = snapshot.load_base(PID, tmp_path, drive)
    assert (where, "cleanup" in got) == ("local", False)


def test_marking_the_cleanup_done_writes_no_base(tmp_path):
    drive = drive_with_base(cleaning(4))
    info = snapshot.deck_info(drive, PID)
    assert snapshot.mark_cleaned(drive, PID, 4, info) is None
    assert drive.written == []                                   # no media update at all
    assert drive.props[PID][snapshot.CLEANED_PROPERTY] == "4"
    assert info["appProperties"][snapshot.CLEANED_PROPERTY] == "4"  # (the caller's facts follow)


def test_a_flag_drive_will_not_take_is_said_so_the_base_can_go_up_instead(tmp_path):
    class Refuses(FakeDrive):
        def update(self, fileId, body=None, media_body=None, fields=None):
            if body and "appProperties" in body:
                raise http_error(403)
            return super().update(fileId, body, media_body, fields)

    drive = Refuses(props={PID: {snapshot.BASE_PROPERTY: "base-0"}},
                    blobs={"base-0": json.dumps(cleaning(4)).encode("utf-8")})
    why = snapshot.mark_cleaned(drive, PID, 4, None)
    assert why and "HttpError" in why
    assert snapshot.mark_cleaned(None, PID, 4, None) == "no Drive service"


# ---------------------------------------------------------------- save


def test_saving_updates_the_file_the_deck_points_at(tmp_path):
    drive = drive_with_base(base(generation=1))
    fid = snapshot.save_drive(drive, base(generation=2))
    assert (fid, drive.created) == ("base-0", [])
    assert json.loads(drive.blobs["base-0"])["generation"] == 2
    assert snapshot.load_base(PID, None, drive)[0]["generation"] == 2


def test_saving_creates_the_file_and_points_the_deck_at_it(tmp_path):
    drive = FakeDrive(props={PID: {}})
    fid = snapshot.save_drive(drive, base(generation=1))
    assert drive.created and drive.created[0]["parents"] == ["folder-1"]  # (beside the presentation)
    assert drive.props[PID][snapshot.BASE_PROPERTY] == fid
    assert snapshot.load_base(PID, None, drive)[0]["generation"] == 1


def test_a_vanished_base_file_is_replaced_not_lost(tmp_path):
    """The recorded file was deleted: the save makes a new one and repoints the presentation."""
    drive = FakeDrive(props={PID: {snapshot.BASE_PROPERTY: "base-gone"}}, update_fails=True)
    fid = snapshot.save_drive(drive, base(generation=4))
    assert fid != "base-gone" and drive.props[PID][snapshot.BASE_PROPERTY] == fid
    assert snapshot.load_base(PID, None, drive)[0]["generation"] == 4


def test_the_local_copy_round_trips(tmp_path):
    path = snapshot.save_local(base(generation=8), tmp_path)
    assert path == tmp_path / "sync" / "base.json"
    assert json.loads(path.read_text(encoding="utf-8"))["generation"] == 8


# ---------------------------------------------------------------- the overlay steps the deck holds


def test_sync_keeps_the_overlay_steps_the_deck_was_made_from():
    """`convert --overlays all` then a plain `sync` used to drop every step in between."""
    from beamer2slides.sync import overlay_mode
    assert overlay_mode(None, "all") == ("all", None)
    assert overlay_mode(None, "last") == ("last", None)


def test_a_base_without_the_field_means_the_default():
    """Bases recorded before this was written down."""
    from beamer2slides.sync import overlay_mode
    assert overlay_mode(None, None) == ("last", None)


def test_asking_for_the_other_steps_is_allowed_but_said_out_loud():
    from beamer2slides.sync import overlay_mode
    mode, warning = overlay_mode("last", "all")
    assert mode == "last" and "read as slides the source dropped" in warning


def test_asking_for_the_steps_the_deck_has_is_quiet():
    from beamer2slides.sync import overlay_mode
    assert overlay_mode("all", "all") == ("all", None)


def test_convert_records_the_mode_in_the_base(monkeypatch, tmp_path):
    """build_base writes it, so the next sync can read it."""
    from beamer2slides import snapshot as snap
    monkeypatch.setattr(snap, "read_presentation", lambda pres: {"presentationId": PID, "revisionId": "r1",
                                                                 "page_size": [720, 405], "layouts": {},
                                                                 "master_background": None, "slides": []})
    built = snap.build_base({"slides": []}, tmp_path, {"presentationId": PID}, {"slides": []},
                            tmp_path / "talk.pdf", overlays="all")
    assert built["overlays"] == "all"


# ---------------------------------------------------------------- the warning about a stale cache


def test_a_base_file_that_vanished_is_worth_a_warning(tmp_path):
    """The pointer is still there, the file is not: another checkout may have synced since."""
    drive = FakeDrive(props={PID: {snapshot.BASE_PROPERTY: "base-gone"}})
    assert "may be older than the deck" in snapshot.stale_base_warning("local", drive, PID)


def test_a_deck_that_never_had_a_drive_base_is_no_warning(tmp_path):
    assert snapshot.stale_base_warning("local", FakeDrive(props={PID: {}}), PID) is None


def test_no_warning_when_the_base_came_from_drive(tmp_path):
    assert snapshot.stale_base_warning("drive", drive_with_base(base()), PID) is None


def test_no_warning_when_the_deck_itself_cannot_be_read(tmp_path):
    """Not our business here: the sync will fail on its own, and guessing would be noise."""
    class Broken(FakeDrive):
        def get(self, fileId, fields=None):
            raise http_error(403)

    assert snapshot.stale_base_warning("local", Broken(), PID) is None


@pytest.mark.parametrize("status", [403, 404, 500])
def test_a_drive_that_refuses_everything_leaves_the_cache_in_charge(tmp_path, status):
    class Broken(FakeDrive):
        def get(self, fileId, fields=None):
            raise http_error(status)

    snapshot.save_local(base(generation=2), tmp_path)
    got, where = snapshot.load_base(PID, tmp_path, Broken())
    assert (where, got["generation"]) == ("local", 2)


# ---------------------------------------------------------------- the last step of a conversion
#
# `snapshot_after_convert` reads the deck, tags its objects and records the base. Two things it
# used to do it no longer does: read the deck a second time to learn the titles it had just
# written, and download the pictures after the tags instead of while they were being written.


def element(oid: str, title: str | None = None) -> dict:
    return {"objectId": oid, "title": title,
            "size": {"width": {"magnitude": 100, "unit": "PT"}, "height": {"magnitude": 20, "unit": "PT"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "PT"},
            "shape": {"shapeType": "TEXT_BOX", "text": {"textElements": []}}}


def alt_text(oid: str, title: str) -> dict:
    return {"updatePageElementAltText": {"objectId": oid, "title": title}}


def read_deck() -> dict:
    group = {"objectId": "g", "elementGroup": {"children": [element("b")]},
             "size": element("g")["size"], "transform": element("g")["transform"]}
    return {"presentationId": PID, "revisionId": "r1",
            "slides": [{"objectId": "s1", "pageElements": [element("a"), group]}]}


def titles_of(pres: dict) -> dict:
    found = {}
    stack = list(pres["slides"][0]["pageElements"])
    while stack:
        e = stack.pop()
        found[e["objectId"]] = e.get("title")
        stack += e.get("elementGroup", {}).get("children", [])
    return found


def test_the_titles_a_tag_batch_wrote_are_put_in_rather_than_read_back():
    """What a second presentations.get would say, said locally - measured against the real thing
    on the 48-slide ambiguous deck (181 tags): the two bases are equal field for field."""
    pres = read_deck()
    after = snapshot.tagged(pres, [alt_text("a", "b2s:one/text/0"), alt_text("b", "b2s:one/image/0")], "r2")
    assert titles_of(after) == {"a": "b2s:one/text/0", "g": None, "b": "b2s:one/image/0"}
    assert after["revisionId"] == "r2"                       # the batch's own answer says it
    assert titles_of(pres) == {"a": None, "g": None, "b": None}   # the read it was made from is left alone


def test_a_revision_the_batch_did_not_answer_with_leaves_the_read_as_it_was():
    after = snapshot.tagged(read_deck(), [alt_text("a", "b2s:one/text/0")], None)
    assert after["revisionId"] == "r1"


class TaggingSlides:
    """A Slides client that refuses whole batches, and one request inside them."""

    def __init__(self, refuses: str | None = None):
        self.refuses, self.batches, self.revision = refuses, [], 1

    def presentations(self):
        return self

    def batchUpdate(self, presentationId, body):
        reqs = body["requests"]
        self.batches.append(len(reqs))
        if len(reqs) > 1 and self.refuses:
            raise http_error(400)
        if any(r["updatePageElementAltText"]["objectId"] == self.refuses for r in reqs):
            raise http_error(400)
        self.revision += 1
        return FakeDrive._request({"writeControl": {"requiredRevisionId": f"r{self.revision}"}})


def test_only_the_tags_that_landed_are_put_into_the_read():
    """The API refuses an alt text on some placeholders; those titles are not in the deck, so they
    must not be in the base either."""
    slides = TaggingSlides(refuses="b")
    landed, revision = snapshot.write_tags(slides, PID, [alt_text("a", "b2s:one/text/0"),
                                                         alt_text("b", "b2s:one/image/0")])
    assert [r["updatePageElementAltText"]["objectId"] for r in landed] == ["a"]
    assert titles_of(snapshot.tagged(read_deck(), landed, revision)) == {"a": "b2s:one/text/0", "g": None, "b": None}
    assert slides.batches == [2, 1, 1]                        # the batch, then one request at a time


def test_nothing_to_tag_costs_no_call():
    slides = TaggingSlides()
    assert snapshot.write_tags(slides, PID, []) == ([], None) and slides.batches == []


def picture_deck() -> tuple[dict, dict]:
    read = {"slides": [{"objectId": "s1", "background": {"picture": "h0"},
                        "objects": {"a": {"image": {"contentHash": "h1"}}}}]}
    pres = {"slides": [{"objectId": "s1",
                        "pageProperties": {"pageBackgroundFill": {"stretchedPictureFill": {"contentUrl": "u-bg"}}},
                        "pageElements": [{"objectId": "a", "image": {"contentUrl": "u-a"}}]}]}
    return read, pres


def test_pictures_signed_while_the_tags_were_written_are_not_fetched_again(fetcher):
    read, pres = picture_deck()
    fetcher(lambda url: pytest.fail(f"downloaded {url} again"))
    assert snapshot.sign_pictures(read, pres, ready={"a": "sig-a", "s1": "sig-bg"}) == 2
    assert read["slides"][0]["objects"]["a"]["image"]["signature"] == "sig-a"
    assert read["slides"][0]["background"]["signature"] == "sig-bg"


def test_a_picture_the_download_missed_is_simply_unsigned(fetcher):
    """`ready` comes from another thread; a picture it could not fetch leaves no signature, which
    is what a failed download has always left (sync then compares the contentHash alone)."""
    read, pres = picture_deck()
    fetcher(lambda url: pytest.fail(f"downloaded {url} again"))
    snapshot.sign_pictures(read, pres, ready={"a": "sig-a"})
    assert "signature" not in read["slides"][0]["background"]


def test_the_signatures_are_by_the_id_that_owns_the_picture(monkeypatch, fetcher):
    _, pres = picture_deck()
    fetcher(lambda url: url.encode())
    monkeypatch.setattr(snapshot, "signature", lambda data: f"sig({data.decode()})")
    assert snapshot.picture_signatures(pres) == {"a": "sig(u-a)", "s1": "sig(u-bg)"}


def test_the_installed_fetcher_reaches_the_worker_threads(monkeypatch, fetcher):
    """Eight workers download the pictures, and a worker inherits no context: the fetcher is
    resolved on the calling thread and handed down, so urllib is never reached."""
    import threading

    from beamer2slides import net

    monkeypatch.setattr(net, "urllib_fetch", lambda url: pytest.fail(f"urllib fetched {url}"))
    threads = set()

    def fetch(url):
        threads.add(threading.current_thread().name)
        return url.encode()

    fetcher(fetch)
    monkeypatch.setattr(snapshot, "signature", lambda data: f"sig({data.decode()})")
    _, pres = picture_deck()
    assert snapshot.picture_signatures(pres) == {"a": "sig(u-a)", "s1": "sig(u-bg)"}
    assert threading.current_thread().name not in threads, "the downloads ran on the pool"
    read, pres = picture_deck()
    assert snapshot.sign_pictures(read, pres) == 2
    assert read["slides"][0]["objects"]["a"]["image"]["signature"] == "sig(u-a)"


def test_a_fetcher_that_raises_leaves_the_pictures_unsigned_and_the_base_whole(monkeypatch, fetcher):
    """A harness's client raises its own types; none of them may escape the signing. A
    PermissionError - "not allowed" - is asked once, not three times with a sleep between."""
    from beamer2slides import net

    monkeypatch.setattr(net.time, "sleep", lambda s: None)
    asked = []

    class Refused(Exception):
        pass

    def refuse(url):
        asked.append(url)
        raise Refused(url)

    fetcher(refuse)
    _, pres = picture_deck()
    assert snapshot.picture_signatures(pres) == {}
    assert sorted(asked) == ["u-a", "u-a", "u-a", "u-bg", "u-bg", "u-bg"]  # (three tries each)

    asked.clear()

    def forbid(url):
        asked.append(url)
        raise PermissionError(url)

    fetcher(forbid)
    read, pres = picture_deck()
    assert snapshot.sign_pictures(read, pres) == 2
    assert "signature" not in read["slides"][0]["objects"]["a"]["image"]
    assert sorted(asked) == ["u-a", "u-bg"]


def test_a_failed_drive_save_of_the_base_is_said_not_only_printed(monkeypatch, tmp_path, capsys):
    """Convert's last step keeps the base locally when Drive refuses it, and says so through
    `problems` - where the agent layer turns it into a warning - instead of only printing."""
    from beamer2slides import google_auth

    class Refusing(FakeDrive):
        def files(self):
            raise http_error(403)

    monkeypatch.setattr(snapshot, "build_base", lambda *a, **k: {"slides": [], "generation": 0,
                                                                   "presentationId": PID})
    monkeypatch.setattr(snapshot, "write_tags", lambda *a: ([], None))
    monkeypatch.setattr("beamer2slides.theme_sync.record", lambda *a: None)
    slides = type("S", (), {"presentations": lambda self: self,
                            "get": lambda self, presentationId: FakeDrive._request(read_deck())})()
    with google_auth.use_services({"slides": slides, "drive": Refusing()}):
        problems: list[str] = []
        snapshot.snapshot_after_convert({"slides": []}, tmp_path, {"presentationId": PID}, {"pdf": "x",
                                        "sha1": None}, problems=problems)
        assert len(problems) == 1 and "could not store the sync base in Drive" in problems[0]
        assert "no_base" in problems[0] and snapshot.local_path(tmp_path).exists()
        assert capsys.readouterr().out == ""
        snapshot.snapshot_after_convert({"slides": []}, tmp_path, {"presentationId": PID}, {"pdf": "x",
                                        "sha1": None})
        assert "warning: could not store the sync base in Drive" in capsys.readouterr().out
