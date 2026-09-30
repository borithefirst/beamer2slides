"""`drive_folder`: where the files beamer2slides creates in Drive go - the app's own folder by
default (`auto`), the old places for `none`, a folder by id when asked, and a folder the app
cannot use refused before anything is created. No Google calls."""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import TYPE_CHECKING

import pytest

from beamer2slides import drive_folder, guard, snapshot
from beamer2slides.drive_folder import FOLDER_MIME, HOME_PROPERTY
from beamer2slides.google_types import DriveFile, FileBody, FileList, Files, Presentation, Presentations
from beamer2slides.typing_compat import override

from .fake_google import Answer, NoDrive, NoFiles, NoPresentations, NoSlides
from .test_guard import http_error

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides import google_types
    from beamer2slides.google_types import CopyFile, CreateFile, GetFile, GetPresentation, ListFiles, UpdateFile


def as_file(fid: str, f: FileBody) -> DriveFile:
    """What Drive answers of a file it holds as `f`: its id, and what was written of it."""
    out = DriveFile(id=fid)
    if "name" in f:
        out["name"] = f["name"]
    if "mimeType" in f:
        out["mimeType"] = f["mimeType"]
    if "parents" in f:
        out["parents"] = list(f["parents"])
    if "trashed" in f:
        out["trashed"] = f["trashed"]
    if "appProperties" in f:
        out["appProperties"] = {k: v for k, v in f["appProperties"].items() if v is not None}
    return out


class Drive(NoFiles, NoDrive):
    """Files by id; records every create and copy body."""

    def __init__(self, files: Mapping[str, FileBody]) -> None:
        self.files_: dict[str, FileBody] = dict(files)
        self.created: list[FileBody] = []
        self.n = 0

    @override
    def files(self) -> Files:
        return self

    @override
    def list(self, **kw: Unpack[ListFiles]) -> google_types.Request[FileList]:
        q = kw.get("q", "")
        assert f"key='{HOME_PROPERTY}'" in q and "trashed=false" in q
        return Answer(FileList(files=[DriveFile(id=i) for i, f in self.files_.items()
                                      if (f.get("appProperties") or {}).get(HOME_PROPERTY) == "1"
                                      and not f.get("trashed")]))

    @override
    def get(self, **kw: Unpack[GetFile]) -> google_types.Request[DriveFile]:
        fid = kw["fileId"]
        if fid not in self.files_:
            return Answer(http_error(404, "File not found"))
        return Answer(as_file(fid, self.files_[fid]))

    @override
    def create(self, **kw: Unpack[CreateFile]) -> google_types.Request[DriveFile]:
        body = kw["body"]
        self.n += 1
        fid = f"F{self.n}"
        self.files_[fid] = body.copy()
        self.created.append(body)
        return Answer(DriveFile(id=fid))

    @override
    def copy(self, **kw: Unpack[CopyFile]) -> google_types.Request[DriveFile]:
        body = kw.get("body")
        assert body is not None, "a copy is given a body"
        self.created.append(body)
        name = body.get("name")
        assert name is not None, "a copy is named"
        return Answer(DriveFile(id="COPY", name=name))

    @override
    def update(self, **kw: Unpack[UpdateFile]) -> google_types.Request[DriveFile]:
        return Answer(DriveFile(id=kw["fileId"]))


@pytest.fixture(autouse=True)
def nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(drive_folder.FOLDER_ENV, raising=False)   # (conftest sets `none`)


def test_none_is_what_it_always_did(monkeypatch: pytest.MonkeyPatch) -> None:
    drive = Drive({})
    with drive_folder.use_folder("none"):
        assert drive_folder.parents(drive, None) is None and drive_folder.parents(drive, ["P0"]) == ["P0"]
    monkeypatch.setenv(drive_folder.FOLDER_ENV, "none")
    assert drive_folder.parents(drive, None) is None
    assert drive.created == []


def test_the_default_makes_the_apps_folder_once_and_finds_it_again() -> None:
    drive = Drive({})
    assert drive_folder.spec() == "auto"
    assert drive_folder.parents(drive, ["P0"]) == ["F1"]
    assert drive_folder.parents(drive, None) == ["F1"]
    assert [b.get("mimeType") for b in drive.created] == [FOLDER_MIME]
    drive.files_["F1"]["name"] = "renamed by the person"
    assert drive_folder.parents(drive, None) == ["F1"] and len(drive.created) == 1


def test_a_drive_that_will_not_make_the_folder_costs_no_conversion(capsys: pytest.CaptureFixture[str]) -> None:
    """`auto` is nobody's explicit request: when Drive refuses the list or the folder, the file
    goes where `none` puts it, and says so."""
    class Refusing(Drive):
        @override
        def list(self, **kw: Unpack[ListFiles]) -> google_types.Request[FileList]:
            return Answer(http_error(403, "insufficient scope"))

    drive = Refusing({})
    assert drive_folder.parents(drive, ["P0"]) == ["P0"] and drive_folder.parents(drive, None) is None
    assert "no 'beamer2slides' folder" in capsys.readouterr().out


def test_a_folder_the_app_cannot_see_is_refused_by_name() -> None:
    drive = Drive({"FOLD": {"mimeType": FOLDER_MIME}, "DOC": {"mimeType": "application/pdf"}})
    with drive_folder.use_folder("FOLD"):
        assert drive_folder.parents(drive, ["P0"]) == ["FOLD"]
    with drive_folder.use_folder("HIDDEN"), pytest.raises(SystemExit, match="only sees folders it created"):
        drive_folder.parents(drive, None)
    with drive_folder.use_folder("DOC"), pytest.raises(SystemExit, match="not a folder"):
        drive_folder.parents(drive, None)
    assert drive.created == []


def test_the_base_and_a_backup_copy_go_into_the_folder_asked_for() -> None:
    drive = Drive({"DECK": {"name": "Talk", "parents": ["P0"]}, "FOLD": {"mimeType": FOLDER_MIME}})
    with drive_folder.use_folder("none"):
        snapshot.save_drive(drive, {"presentationId": "DECK"}, None, {"name": "Talk", "parents": ["P0"]})
    assert drive.created[-1].get("parents") == ["P0"]                 # beside the deck, as before
    with drive_folder.use_folder("FOLD"):
        snapshot.save_drive(drive, {"presentationId": "DECK"}, None, {"name": "Talk", "parents": ["P0"]})
        assert drive.created[-1].get("parents") == ["FOLD"]
        guard.copy_in_drive(drive, "DECK", None)
        assert drive.created[-1].get("parents") == ["FOLD"] and "backup" in drive.created[-1].get("name", "")


class Slides(NoPresentations, NoSlides):
    """A Slides client whose imported deck has a 4:3 page."""

    @override
    def presentations(self) -> Presentations:
        return self

    @override
    def get(self, **kw: Unpack[GetPresentation]) -> google_types.Request[Presentation]:
        return Answer(Presentation(pageSize={"width": {"magnitude": 100}, "height": {"magnitude": 75}}))


def test_a_new_deck_goes_into_the_folder_asked_for() -> None:
    from beamer2slides import emit
    drive = Drive({"FOLD": {"mimeType": FOLDER_MIME}})
    slides = Slides()
    with drive_folder.use_folder("FOLD"):
        emit.import_presentation(slides, drive, "Talk", 400, 300, io.BytesIO(b"pptx"), None)
    assert drive.created[-1].get("parents") == ["FOLD"] and drive.created[-1].get("name") == "Talk"
    with drive_folder.use_folder("none"):
        emit.import_presentation(slides, drive, "Talk", 400, 300, io.BytesIO(b"pptx"), None)
    assert "parents" not in drive.created[-1]


def test_the_agent_context_carries_the_folder() -> None:
    from beamer2slides.agent.context import AgentContext
    assert AgentContext.__dataclass_fields__["drive_folder"].default is None
