"""Where the files beamer2slides creates in Drive go.

What it creates: the deck a conversion makes (`emit.import_presentation`) and the document
`docs push` makes; the sync base beside each (`snapshot.save_drive`, `doc_sync` - a JSON file whose
id the deck or document keeps in its appProperties); a copy kept before a destructive write
(`guard.backup_deck`, `--backup drive`); and staging files a sync deletes after use (`Sync.stage`,
`doc_sync`'s picture staging). Rebuilding a deck in place writes over the same file, wherever it is.

With nothing said, new decks and documents land in My Drive's root and a base or a backup copy
beside the file it belongs to - which is what it always did. `use_folder(spec)` (per context),
`$B2S_DRIVE_FOLDER`, the CLI's `--drive-folder` or `AgentContext.drive_folder` put every one of them
into one folder instead:
- a folder id: that folder. Under the `drive.file` scope the app sees only what it created or was
  opened with, so a folder made in the Drive UI by hand is refused here, by name, before anything
  is created - `auto` is the one that always works.
- `auto`: the app's own folder, "beamer2slides" in My Drive, found by its `b2sHome` appProperty
  (so renaming or moving it keeps it) and created the first time.

A deck found by its URL keeps finding its base wherever either is: the base is looked up by the
id the deck carries, never by folder.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

FOLDER_ENV = "B2S_DRIVE_FOLDER"
FOLDER_MIME = "application/vnd.google-apps.folder"
HOME_PROPERTY = "b2sHome"
HOME_NAME = "beamer2slides"
AUTO = "auto"

_spec: ContextVar[str | None] = ContextVar("beamer2slides.drive_folder", default=None)


@contextmanager
def use_folder(spec: str | None):
    """Create every Drive file inside this block in `spec` (a folder id, or `auto`)."""
    token = _spec.set(spec)
    try:
        yield
    finally:
        _spec.reset(token)


def spec() -> str | None:
    """The folder asked for in this context, else `$B2S_DRIVE_FOLDER`, else None (the defaults)."""
    return _spec.get() or os.environ.get(FOLDER_ENV) or None


def folder_id(drive) -> str | None:
    """The id of the folder new files go into (None: nobody asked for one). Raises SystemExit
    when the folder named cannot take them, before anything was created."""
    from .gapi import HttpError, status_of
    from .gslides import execute
    s = spec()
    if not s:
        return None
    if s == AUTO:
        q = (f"appProperties has {{ key='{HOME_PROPERTY}' and value='1' }} and mimeType='{FOLDER_MIME}' "
             f"and trashed=false")
        found = execute(drive.files().list(q=q, spaces="drive", fields="files(id)", pageSize=10)).get("files", [])
        if found:
            return found[0]["id"]
        return execute(drive.files().create(body={"name": HOME_NAME, "mimeType": FOLDER_MIME,
                                                  "appProperties": {HOME_PROPERTY: "1"}}, fields="id"))["id"]
    try:
        info = execute(drive.files().get(fileId=s, fields="id,mimeType,trashed"))
    except HttpError as e:
        raise SystemExit(f"Drive folder {s} cannot be used (HTTP {status_of(e)}): this app "
                         f"only sees folders it created or was opened with. `{AUTO}` makes and reuses a "
                         f"'{HOME_NAME}' folder of its own") from None
    if info.get("mimeType") != FOLDER_MIME or info.get("trashed"):
        raise SystemExit(f"Drive file {s} is not a folder{' (it is in the trash)' if info.get('trashed') else ''}")
    return info["id"]


def parents(drive, beside: list[str] | None = None) -> list[str] | None:
    """What a new file's `parents` should be: the folder asked for, else `beside` (the parents of
    the file it belongs to, for a base or a backup), else None (My Drive's root)."""
    fid = folder_id(drive)
    if fid:
        return [fid]
    return list(beside) if beside else None


def place(body: dict, drive, beside: list[str] | None = None) -> dict:
    """`body` (a files.create / files.copy body) with its `parents` set by `parents`."""
    where = parents(drive, beside)
    if where:
        body["parents"] = where
    return body
