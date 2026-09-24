"""`drive_folder`: where the files beamer2slides creates in Drive go - the app's own folder by
default (`auto`), the old places for `none`, a folder by id when asked, and a folder the app
cannot use refused before anything is created. No Google calls."""

import pytest

from beamer2slides import drive_folder, guard, snapshot
from beamer2slides.drive_folder import FOLDER_MIME, HOME_PROPERTY

from .test_guard import Request, http_error


class Drive:
    """Files by id; records every create and copy body."""

    def __init__(self, files=None):
        self.files_ = dict(files or {})
        self.created, self.n = [], 0

    def files(self):
        return self

    def list(self, q, spaces, fields, pageSize):
        assert f"key='{HOME_PROPERTY}'" in q and "trashed=false" in q
        return Request({"files": [{"id": i} for i, f in self.files_.items()
                                  if (f.get("appProperties") or {}).get(HOME_PROPERTY) == "1" and not f.get("trashed")]})

    def get(self, fileId, fields=None):
        if fileId not in self.files_:
            return Request(http_error(404, "File not found"))
        return Request({"id": fileId, **self.files_[fileId]})

    def create(self, body=None, fields=None, media_body=None):
        self.n += 1
        fid = f"F{self.n}"
        self.files_[fid] = dict(body)
        self.created.append(body)
        return Request({"id": fid})

    def copy(self, fileId, body=None, fields=None):
        self.created.append(body)
        return Request({"id": "COPY", "name": body["name"]})

    def update(self, fileId, body=None, media_body=None, fields=None):
        return Request({"id": fileId})


@pytest.fixture(autouse=True)
def nothing_set(monkeypatch):
    monkeypatch.delenv(drive_folder.FOLDER_ENV, raising=False)   # (conftest sets `none`)


def test_none_is_what_it_always_did(monkeypatch):
    drive = Drive()
    with drive_folder.use_folder("none"):
        assert drive_folder.parents(drive) is None and drive_folder.parents(drive, ["P0"]) == ["P0"]
    monkeypatch.setenv(drive_folder.FOLDER_ENV, "none")
    assert drive_folder.parents(drive) is None
    assert drive.created == []


def test_the_default_makes_the_apps_folder_once_and_finds_it_again():
    drive = Drive()
    assert drive_folder.spec() == "auto"
    assert drive_folder.parents(drive, ["P0"]) == ["F1"]
    assert drive_folder.parents(drive) == ["F1"]
    assert [b["mimeType"] for b in drive.created] == [FOLDER_MIME]
    drive.files_["F1"]["name"] = "renamed by the person"
    assert drive_folder.parents(drive) == ["F1"] and len(drive.created) == 1


def test_a_drive_that_will_not_make_the_folder_costs_no_conversion(capsys):
    """`auto` is nobody's explicit request: when Drive refuses the list or the folder, the file
    goes where `none` puts it, and says so."""
    class Refusing(Drive):
        def list(self, **kw):
            return Request(http_error(403, "insufficient scope"))

    drive = Refusing()
    assert drive_folder.parents(drive, ["P0"]) == ["P0"] and drive_folder.parents(drive) is None
    assert "no 'beamer2slides' folder" in capsys.readouterr().out


def test_a_folder_the_app_cannot_see_is_refused_by_name():
    drive = Drive({"FOLD": {"mimeType": FOLDER_MIME}, "DOC": {"mimeType": "application/pdf"}})
    with drive_folder.use_folder("FOLD"):
        assert drive_folder.parents(drive, ["P0"]) == ["FOLD"]
    with drive_folder.use_folder("HIDDEN"), pytest.raises(SystemExit, match="only sees folders it created"):
        drive_folder.parents(drive)
    with drive_folder.use_folder("DOC"), pytest.raises(SystemExit, match="not a folder"):
        drive_folder.parents(drive)
    assert drive.created == []


def test_the_base_and_a_backup_copy_go_into_the_folder_asked_for():
    drive = Drive({"DECK": {"name": "Talk", "parents": ["P0"]}, "FOLD": {"mimeType": FOLDER_MIME}})
    with drive_folder.use_folder("none"):
        snapshot.save_drive(drive, {"presentationId": "DECK"}, info={"name": "Talk", "parents": ["P0"]})
    assert drive.created[-1]["parents"] == ["P0"]                     # beside the deck, as before
    with drive_folder.use_folder("FOLD"):
        snapshot.save_drive(drive, {"presentationId": "DECK"}, info={"name": "Talk", "parents": ["P0"]})
        assert drive.created[-1]["parents"] == ["FOLD"]
        guard.copy_in_drive(drive, "DECK")
        assert drive.created[-1]["parents"] == ["FOLD"] and "backup" in drive.created[-1]["name"]


def test_a_new_deck_goes_into_the_folder_asked_for():
    from beamer2slides import emit
    drive = Drive({"FOLD": {"mimeType": FOLDER_MIME}})
    page = {"width": {"magnitude": 100}, "height": {"magnitude": 75}}
    slides = type("S", (), {"presentations": lambda self: self,
                            "get": lambda self, presentationId: Request({"pageSize": page})})()
    import io
    with drive_folder.use_folder("FOLD"):
        emit.import_presentation(slides, drive, "Talk", 400, 300, io.BytesIO(b"pptx"), None)
    assert drive.created[-1]["parents"] == ["FOLD"] and drive.created[-1]["name"] == "Talk"
    with drive_folder.use_folder("none"):
        emit.import_presentation(slides, drive, "Talk", 400, 300, io.BytesIO(b"pptx"), None)
    assert "parents" not in drive.created[-1]


def test_the_agent_context_carries_the_folder():
    from beamer2slides.agent.context import AgentContext
    assert AgentContext.__dataclass_fields__["drive_folder"].default is None
