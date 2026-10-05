"""Where the files beamer2slides creates in Drive go.

What it creates: the deck a conversion makes (`emit.import_presentation`) and the document
`docs push` makes; the sync base beside each (`snapshot.save_drive`, `doc_sync` - a JSON file whose
id the deck or document keeps in its appProperties); a copy kept before a destructive write
(`guard.backup_deck`, `--backup drive`); and staging files a sync deletes after use (`Sync.stage`,
`doc_sync`'s picture staging). Rebuilding a deck in place writes over the same file, wherever it is.

Every one of them goes into one folder, so a person's Drive gets one "beamer2slides" folder and
nothing else. `use_folder(spec)` (per context), `$B2S_DRIVE_FOLDER`, the CLI's `--drive-folder` or
`AgentContext.drive_folder` say which:
- `auto` (the default, decided 2026-09-24): the app's own folder, "beamer2slides" in My Drive,
  found by its `b2sHome` appProperty (so renaming or moving it keeps it) and created the first
  time. Should Drive refuse to list or make it, the file goes where `none` would put it, with a
  warning: a conversion is not lost over where its deck lands.
- a folder id: that folder. Under the `drive.file` scope the app sees only what it created or was
  opened with, so a folder made in the Drive UI by hand is refused here, by name, before anything
  is created.
- `none`: where files went before there was a folder - new decks and documents in My Drive's
  root, a base or a backup copy beside the file it belongs to.

A deck found by its URL keeps finding its base wherever either is: the base is looked up by the
id the deck carries, never by folder.

Backup copies go one level further down, into a "Backups" folder inside that folder
(`backup_parents`), so the folder a person opens holds their decks and the copies kept of them sit
out of the way. The base files are named for the app, not the deck (`snapshot.base_name`): a person
searching Drive for their talk's name finds the talk.

**Hidden storage** (`use_hidden`, `$B2S_HIDDEN_FILES`, the CLI's `--hidden-files`; off by
default; an agent harness sets the variable - AgentContext's defaulted fields are capped): the base files and the backups go into Drive's
appDataFolder - the app's own space, which the Drive UI and its search never show (Settings >
Manage apps lists only its size, and can delete it) - and only the decks and documents are where
a person looks. It needs one more scope, `drive.appdata` (`APPDATA_SCOPE`, asked for only while
the mode is on: `google_auth.wanted_scopes`). Two modes, to be measured against each other:
- `deck`: a backup is a native Slides copy in appDataFolder (`guard.hidden_copy`): no size limit,
  and a restore from it keeps every objectId, so the restored deck can still be synced;
- `pptx`: a backup is the deck's .pptx uploaded there (`guard.hidden_pptx`): Drive's 10 MB
  export limit applies, and a restore is an import, which renumbers the deck's objects.
Whatever the hidden space refuses (the scope not granted, a file kind it does not take, a deck too
large to export) is kept where it would have gone with the mode off, with a warning: a way back is
never lost over where it is kept. A base is read by the id its deck carries, wherever it is.
"""

from __future__ import annotations

import os
from collections.abc import Generator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, TypeVar, overload

from .google_types import DriveService, FileBody, file_id
from .json_types import as_optional_str

FOLDER_ENV = "B2S_DRIVE_FOLDER"
FOLDER_MIME = "application/vnd.google-apps.folder"
HOME_PROPERTY = "b2sHome"
HOME_NAME = "beamer2slides"
BACKUPS_PROPERTY = "b2sBackups"
"""The appProperty of a folder backup copies go into; its value is the id of the folder it is in."""
BACKUPS_NAME = "Backups"
AUTO = "auto"
NONE = "none"

_spec: ContextVar[str | None] = ContextVar("beamer2slides.drive_folder", default=None)


@contextmanager
def use_folder(spec: str | None) -> Generator[None, None, None]:
    """Create every Drive file inside this block in `spec` (a folder id, `auto` or `none`)."""
    token = _spec.set(spec)
    try:
        yield
    finally:
        _spec.reset(token)


def spec() -> str:
    """The folder asked for in this context, else `$B2S_DRIVE_FOLDER`, else `auto`."""
    return _spec.get() or os.environ.get(FOLDER_ENV) or AUTO


HIDDEN_ENV = "B2S_HIDDEN_FILES"
HiddenMode = Literal["off", "deck", "pptx"]
HIDDEN_MODES: tuple[HiddenMode, ...] = ("off", "deck", "pptx")
APPDATA = "appDataFolder"
"""The parent, and the `spaces` value, of the app's hidden Drive space."""
APPDATA_SCOPE = "https://www.googleapis.com/auth/drive.appdata"

_hidden: ContextVar[HiddenMode | None] = ContextVar("beamer2slides.hidden_files", default=None)


def hidden_mode(text: str) -> HiddenMode:
    """`text` as a hidden-storage mode; anything else is refused by name."""
    for mode in HIDDEN_MODES:
        if mode == text:
            return mode
    raise SystemExit(f"hidden files: {text!r} is not one of {', '.join(HIDDEN_MODES)}")


@contextmanager
def use_hidden(mode: HiddenMode | None) -> Generator[None, None, None]:
    """Keep bases and backups in the app's hidden space inside this block (`mode`; None: as outside)."""
    token = _hidden.set(mode)
    try:
        yield
    finally:
        _hidden.reset(token)


def hidden() -> HiddenMode:
    """The hidden-storage mode asked for in this context, else `$B2S_HIDDEN_FILES`, else `off`."""
    mode = _hidden.get()
    if mode is not None:
        return mode
    return hidden_mode(os.environ.get(HIDDEN_ENV) or "off")


def folder_id(drive: DriveService) -> str | None:
    """The id of the folder new files go into (None: `none`, or the app's folder could not be had).
    Raises SystemExit when a folder named by id cannot take them, before anything was created."""
    from .gapi import HttpError, status_of
    from .gslides import execute
    s = spec()
    if s == NONE:
        return None
    if s == AUTO:
        q = (f"appProperties has {{ key='{HOME_PROPERTY}' and value='1' }} and mimeType='{FOLDER_MIME}' "
             f"and trashed=false")
        try:
            found = execute(drive.files().list(q=q, spaces="drive", fields="files(id)",
                                               pageSize=10)).get("files", [])
            # (`fields` asked for ids: a file without one is no answer, and the next is looked at)
            for folder in found:
                fid = folder.get("id")
                if fid:
                    return fid
            made = execute(drive.files().create(body={"name": HOME_NAME, "mimeType": FOLDER_MIME,
                                                      "appProperties": {HOME_PROPERTY: "1"}}, fields="id"))
            return file_id(made, "the new folder")
        except (HttpError, OSError) as e:
            print(f"warning: no '{HOME_NAME}' folder in Drive ({type(e).__name__}: {e}); the file goes "
                  f"where --drive-folder {NONE} puts it")
            return None
    try:
        info = execute(drive.files().get(fileId=s, fields="id,mimeType,trashed"))
    except HttpError as e:
        raise SystemExit(f"Drive folder {s} cannot be used (HTTP {status_of(e)}): this app "
                         f"only sees folders it created or was opened with. `{AUTO}` makes and reuses a "
                         f"'{HOME_NAME}' folder of its own") from None
    if info.get("mimeType") != FOLDER_MIME or info.get("trashed"):
        raise SystemExit(f"Drive file {s} is not a folder{' (it is in the trash)' if info.get('trashed') else ''}")
    return as_optional_str(info.get("id"), f"Drive file {s}'s id") or s


def parents(drive: DriveService, beside: list[str] | None) -> list[str] | None:
    """What a new file's `parents` should be: the folder (`folder_id`), else `beside` (the parents
    of the file it belongs to, for a base or a backup), else None (My Drive's root)."""
    fid = folder_id(drive)
    if fid:
        return [fid]
    return list(beside) if beside else None


def backup_parents(drive: DriveService, beside: list[str] | None) -> list[str] | None:
    """What a backup copy's `parents` should be: the "Backups" folder inside the folder new files
    go into, found by its `b2sBackups` appProperty (the id of the folder it is in, so it is that
    folder's own) and made the first time. With no folder (`none`, or none could be had) the copy
    goes beside its deck, as it always did; a Backups folder Drive will not list or make leaves the
    copy in the folder itself, with a warning - a backup is never lost over where it lands."""
    from .gapi import HttpError
    from .gslides import execute
    fid = folder_id(drive)
    if not fid:
        return list(beside) if beside else None
    q = (f"appProperties has {{ key='{BACKUPS_PROPERTY}' and value='{fid}' }} and mimeType='{FOLDER_MIME}' "
         f"and trashed=false")
    try:
        found = execute(drive.files().list(q=q, spaces="drive", fields="files(id)", pageSize=10)).get("files", [])
        for folder in found:
            bid = folder.get("id")
            if bid:
                return [bid]
        made = execute(drive.files().create(body={"name": BACKUPS_NAME, "mimeType": FOLDER_MIME, "parents": [fid],
                                                  "appProperties": {BACKUPS_PROPERTY: fid}}, fields="id"))
        return [file_id(made, "the backups folder")]
    except (HttpError, OSError) as e:
        print(f"warning: no '{BACKUPS_NAME}' folder in Drive ({type(e).__name__}: {e}); the backup copy goes "
              f"into the folder itself")
        return [fid]


Body = TypeVar("Body", bound=Mapping[str, object])


@overload
def place(body: FileBody, drive: DriveService, beside: list[str] | None) -> FileBody: ...
@overload
def place(body: Body, drive: DriveService, beside: list[str] | None) -> Body: ...
def place(body: Mapping[str, object], drive: DriveService, beside: list[str] | None) -> Mapping[str, object]:
    """`body` (a files.create / files.copy body) with its `parents` set by `parents`, changed in
    place and returned as the type it came in: a `FileBody` (what `files().create` takes, and what a
    dict written in the call is read as) comes back as one, and a body a caller built as a plain
    dict beforehand (snapshot.store_base, doc_sync's base) as that, until those say `FileBody` too.
    (`beside` is given wherever such a body is: its file's parents.)"""
    where = parents(drive, beside)
    if where and isinstance(body, MutableMapping):
        body["parents"] = where
    return body
