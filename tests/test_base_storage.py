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


@pytest.mark.parametrize("status", [403, 404, 500])
def test_a_drive_that_refuses_everything_leaves_the_cache_in_charge(tmp_path, status):
    class Broken(FakeDrive):
        def get(self, fileId, fields=None):
            raise http_error(status)

    snapshot.save_local(base(generation=2), tmp_path)
    got, where = snapshot.load_base(PID, tmp_path, Broken())
    assert (where, got["generation"]) == ("local", 2)
