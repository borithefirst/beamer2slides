"""Backups and Drive version history of a converted deck (docs/sync.md, "Never lose deck edits").

    python tools/deck_backup.py list    --deck <url|id|out folder>
    python tools/deck_backup.py export  --deck ... [--revision ID] [--to FILE]
    python tools/deck_backup.py restore --deck ... [--revision ID | --from FILE] [--in-place]
    python tools/deck_backup.py prune   --deck ... [--keep 10] [--older-than-days N] [--yes] [--drive]

`list` shows what Drive keeps of the presentation (its revisions, newest last) and the local
backups `convert --backup` / `sync --backup` wrote, with what they take up on disk. `export` saves
one revision as a .pptx. `restore` uploads a .pptx (a local backup, or a revision exported on the
fly) as a **new** presentation and prints its URL; `--in-place` puts it back into the same file
instead (the same `files.update` a rebuild uses, so the current content becomes a revision of its
own) - which brings the deck's content back at its own URL, links and embeds included, but not its
object ids: Drive's import numbers every page `p1`...`pN` of its own (measured 2026-09-22), so a
sync base older than the restore describes none of the deck and `sync` refuses it. A deck too
large to export whole was backed up in parts (`...-slides-001-004.pptx`, `guard.export_parts`):
each part restores on its own with `--from`, and Slides' File > Import slides joins them;
`tools/deck_export.py` exports any deck that way.

`prune` is the only destructive action here: every sync of a deck writes a .pptx of it, so a folder
that is synced often grows without end. It keeps the newest `--keep` backups (and everything newer
than `--older-than-days`, when given) and deletes the rest - but only files `backups.json` says
this program wrote, never anything else in the folder, and never without `--yes`. The log entries
stay, with `deleted` on the ones whose file is gone: what the deck was, and when, is evidence worth
keeping even when the way back is not kept. `prune --drive` does the same for the Drive copies
`--backup drive` makes (tagged `b2sBackupOf`), moving the older ones to Drive's trash.

Only the app's own files are reachable (drive.file scope): a deck this tool made or opened.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, runtime_checkable

from beamer2slides import guard
from beamer2slides.gapi import media_upload
from beamer2slides.google_auth import credentials, drive_service
from beamer2slides.google_types import DriveFile, DriveService, Request, file_id
from beamer2slides.guard import PPTX_MIME, backup_dir, deck_url
from beamer2slides.gslides import execute
from beamer2slides.json_types import Json, JsonObject, as_array, as_int, as_object, as_objects, as_str
from beamer2slides.sync import resolve_deck

if TYPE_CHECKING:
    from typing_extensions import Required, Unpack


# ---------------------------------------------------------------- Drive's revisions
# `revisions` is a Drive call only this tool makes, so its Protocol is here rather than in
# google_types (which lists what the package calls); `revised` checks a client has it.


class RevisionUser(TypedDict, total=False):
    displayName: str


class Revision(TypedDict, total=False):
    """A revision of a Drive file, as far as `fields=` asked for it."""
    id: str
    modifiedTime: str
    keepForever: bool
    published: bool
    lastModifyingUser: RevisionUser
    exportLinks: dict[str, str]


class RevisionList(TypedDict, total=False):
    revisions: list[Revision]
    nextPageToken: str


class ListRevisions(TypedDict, total=False):
    fileId: Required[str]
    pageSize: int
    pageToken: str | None
    fields: str


class GetRevision(TypedDict, total=False):
    fileId: Required[str]
    revisionId: Required[str]
    fields: str


class Revisions(Protocol):
    def list(self, **kw: Unpack[ListRevisions]) -> Request[RevisionList]: ...
    def get(self, **kw: Unpack[GetRevision]) -> Request[Revision]: ...


@runtime_checkable
class RevisedDrive(DriveService, Protocol):
    """A Drive v3 client with its `revisions` collection."""

    def revisions(self) -> Revisions: ...


def revised(drive: DriveService) -> RevisedDrive:
    if isinstance(drive, RevisedDrive):
        return drive
    raise TypeError("this Drive client has no revisions() collection")


def revision_id(revision: Revision) -> str:
    rid = revision.get("id")
    if rid is None:
        raise SystemExit("Drive answered a revision without its id")
    return rid


def modified_time(revision: Revision) -> str:
    when = revision.get("modifiedTime")
    if when is None:
        raise SystemExit(f"Drive answered revision {revision_id(revision)} without its modifiedTime")
    return when


def file_name(info: DriveFile, pid: str) -> str:
    name = info.get("name")
    if name is None:
        raise SystemExit(f"Drive answered no name for {pid}")
    return name


def revisions(drive: RevisedDrive, pid: str) -> list[Revision]:
    out: list[Revision] = []
    token: str | None = None
    while True:
        r = execute(drive.revisions().list(
            fileId=pid, pageSize=200, pageToken=token,
            fields="nextPageToken,revisions(id,modifiedTime,keepForever,published,lastModifyingUser(displayName),exportLinks)"))
        out += r.get("revisions", [])
        token = r.get("nextPageToken")
        if not token:
            return out


def download(url: str) -> bytes:
    """A revision's export link needs the OAuth token (it is not a public URL)."""
    from google.auth.transport.requests import AuthorizedSession
    r = AuthorizedSession(credentials()).get(url)
    r.raise_for_status()
    return r.content


def revision_pptx(drive: RevisedDrive, pid: str, revision: Revision) -> bytes:
    links = revision.get("exportLinks") or execute(
        drive.revisions().get(fileId=pid, revisionId=revision_id(revision), fields="exportLinks")).get("exportLinks", {})
    if PPTX_MIME not in links:
        raise SystemExit(f"revision {revision_id(revision)} has no .pptx export link (it has {sorted(links)})")
    return download(links[PPTX_MIME])


def pick(revs: list[Revision], wanted: str | None) -> Revision:
    """`wanted`: a revision id, 'latest', 'previous', or 'before:<ISO time>' (the newest revision
    not newer than that time - the deck as it was when convert recorded `modifiedTime`)."""
    if not revs:
        raise SystemExit("this file has no revisions in Drive")
    if wanted is None or wanted == "latest":
        return revs[-1]
    if wanted == "previous":
        return revs[-2] if len(revs) > 1 else revs[-1]
    if wanted.startswith("before:"):
        when = wanted[len("before:"):]
        earlier = [r for r in revs if modified_time(r) <= when]
        if not earlier:
            raise SystemExit(f"no revision at or before {when} (the oldest is {modified_time(revs[0])})")
        return earlier[-1]
    found = next((r for r in revs if revision_id(r) == wanted), None)
    if not found:
        raise SystemExit(f"no revision {wanted} (there are {[revision_id(r) for r in revs]})")
    return found


def upload_into(drive: DriveService, data: bytes, pid: str) -> str:
    """`data` written into the presentation `pid` (its current content becomes a revision)."""
    execute(drive.files().update(fileId=pid, media_body=media_upload(io.BytesIO(data), PPTX_MIME), fields="id"))
    return pid


def upload_new(drive: DriveService, data: bytes, name: str) -> str:
    """`data` imported as a new presentation called `name`: its id."""
    return file_id(execute(drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.presentation"},
        media_body=media_upload(io.BytesIO(data), PPTX_MIME), fields="id")), name)


def local_backups(folder: Path | None) -> list[Json]:
    if folder is None:
        return []
    log = backup_dir(folder) / "backups.json"
    if not log.exists():
        return []
    try:
        written: Json = json.loads(log.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return as_array(written, str(log))


def megabytes(value: Json, where: str) -> float:
    return as_int(value, where) / 1e6


def prune(folder: Path | None, keep: int, older_than_days: float | None, yes: bool) -> None:
    if folder is None:
        raise SystemExit("prune needs --deck to be an output folder (its backups.json says what this program wrote)")
    r = guard.prune_backups(folder, keep, older_than_days, delete=yes)
    files = as_int(r.get("files"), "prune.files")
    doomed = as_objects(r.get("doomed"), "prune.doomed")
    print(f"{files} backup file(s) in {backup_dir(folder)}, {megabytes(r.get('bytes'), 'prune.bytes'):.1f} MB")
    if not doomed:
        print(f"nothing to prune (the newest {keep} are kept"
              f"{f', and everything under {older_than_days:g} days old' if older_than_days is not None else ''})")
        return
    for d in doomed:
        e = as_object(d.get("entry"), "prune.doomed.entry")
        print(f"  {'deleted' if yes else 'would delete'} {Path(as_str(d.get('file'), 'prune.doomed.file')).name} "
              f"({megabytes(d.get('bytes'), 'prune.doomed.bytes'):.1f} MB, "
              f"{e.get('action', '?')} at {e.get('checked', '?')}, revision {e.get('revisionId', '?')})")
    freed = megabytes(r.get("freed"), "prune.freed")
    if yes:
        print(f"deleted {len(doomed)} file(s), {freed:.1f} MB; "
              f"{files - len(doomed)} kept")
    else:
        print(f"{len(doomed)} file(s), {freed:.1f} MB. Add --yes to delete them; each one is a "
              f"way back to the deck as it was at that revision.")


def prune_drive(pid: str, keep: int, older_than_days: float | None, yes: bool) -> None:
    r = guard.prune_drive_backups(drive_service(None),pid, keep, older_than_days, trash=yes)
    doomed = as_objects(r.get("doomed"), "prune.doomed")
    print(f"{r.get('copies')} backup copy(ies) of {deck_url(pid)} in Drive")
    if not doomed:
        print(f"nothing to prune (the newest {keep} are kept)")
        return
    for c in doomed:
        created = as_str(c.get("createdTime", "?"), "prune.doomed.createdTime")
        print(f"  {'moved to the trash' if yes else 'would move to the trash'}: {c.get('name')} "
              f"({created[:19].replace('T', ' ')})  {deck_url(as_str(c.get('id'), 'prune.doomed.id'))}")
    for w in as_array(r.get("warnings"), "prune.warnings"):
        print(f"  warning: {w}")
    if not yes:
        print(f"{len(doomed)} copy(ies). Add --yes to move them to Drive's trash (recoverable there "
              f"for 30 days); each one is a way back to the deck as it was.")


def backup_of(entry: JsonObject) -> JsonObject:
    """An entry's `backup` (`{}` where it has none)."""
    backup = entry.get("backup")
    return as_object(backup, "backups.json backup") if backup else {}


def main() -> None:
    ap = argparse.ArgumentParser(prog="deck_backup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["list", "export", "restore", "prune"])
    ap.add_argument("--deck", required=True, help="presentation URL or id, or a convert output folder")
    ap.add_argument("--revision", help="revision id, 'latest' (default) or 'previous'")
    ap.add_argument("--to", type=Path, help="export: where to write the .pptx")
    ap.add_argument("--from", dest="source", type=Path, help="restore: a .pptx written earlier")
    ap.add_argument("--in-place", action="store_true",
                    help="restore into the same presentation (its current content becomes a revision)")
    ap.add_argument("--keep", type=int, default=10, help="prune: how many of the newest backups to keep (default 10)")
    ap.add_argument("--older-than-days", type=float, help="prune: only delete backups older than this")
    ap.add_argument("--yes", action="store_true", help="prune: actually delete (without it, nothing is touched)")
    ap.add_argument("--drive", action="store_true",
                    help="prune: the tagged Drive copies (--backup drive) instead of the .pptx files; "
                         "they go to Drive's trash, not away")
    args = ap.parse_args()
    action: str = args.action
    deck: str = args.deck
    revision: str | None = args.revision
    to: Path | None = args.to
    source: Path | None = args.source
    in_place: bool = args.in_place
    keep: int = args.keep
    older_than_days: float | None = args.older_than_days
    yes: bool = args.yes
    in_drive: bool = args.drive
    pid, folder = resolve_deck(deck)
    if action == "prune" and in_drive:
        return prune_drive(pid, keep, older_than_days, yes)
    if action == "prune":  # no Google call: this is about files on disk
        return prune(folder, keep, older_than_days, yes)
    drive = revised(drive_service(None))
    revs = revisions(drive, pid)
    if action == "list":
        info = execute(drive.files().get(fileId=pid, fields="name,modifiedTime"))
        print(f"{file_name(info, pid)}  {deck_url(pid)}")
        print(f"{len(revs)} revision(s) in Drive (oldest first):")
        for r in revs:
            who = (r.get("lastModifyingUser") or {}).get("displayName", "?")
            print(f"  {revision_id(r):>6}  {modified_time(r)[:19].replace('T', ' ')}  {who}"
                  f"{'  keepForever' if r.get('keepForever') else ''}"
                  f"{'  (pptx export)' if PPTX_MIME in (r.get('exportLinks') or {}) else ''}")
        entries = local_backups(folder)
        for i, entry in enumerate(entries):
            b = as_object(entry, f"backups.json[{i}]")
            line = f"  {b.get('checked', '?')}  {b.get('action')}  revision {b.get('revisionId')}  {b.get('reason', '')}"
            print(line)
            backup = backup_of(b)
            for k, v in backup.items():
                if k in ("file", "drive"):
                    print(f"      {k}: {v['url'] if isinstance(v, dict) else v}"
                          f"{'  (deleted)' if k == 'file' and not Path(as_str(v, 'backup.file')).exists() else ''}")
            for p in as_objects(backup.get("parts", []), "backup.parts"):   # a deck too large to export whole
                slides = as_array(p.get("slides"), "backup.parts.slides")
                part_file = as_str(p.get("file"), "backup.parts.file")
                print(f"      part, slides {slides[0]}-{slides[1]}: {part_file}"
                      f"{'  (deleted)' if not Path(part_file).exists() else ''}")
        files = guard.backup_files(entries)
        if files:
            size = sum(p.stat().st_size for p, _ in files) / 1e6
            print(f"  {len(files)} backup file(s) on disk, {size:.1f} MB"
                  + (f" - `prune --deck {folder}` offers to delete the older ones" if len(files) > 10 else ""))
        return
    if action == "export":
        rev = pick(revs, revision)
        data = revision_pptx(drive, pid, rev)
        path = to or (backup_dir(folder) if folder else Path(".")) / f"revision-{revision_id(rev)}-{pid[:12]}.pptx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"revision {revision_id(rev)} ({modified_time(rev)[:19].replace('T', ' ')}) -> {path} "
              f"({len(data) / 1e6:.2f} MB)")
        return
    if source:
        data = source.read_bytes()
        what = str(source)
    else:
        rev = pick(revs, revision)
        data = revision_pptx(drive, pid, rev)
        what = f"revision {revision_id(rev)}"
    name = file_name(execute(drive.files().get(fileId=pid, fields="name")), pid)
    if in_place:
        upload_into(drive, data, pid)
        print(f"{what} written back into {deck_url(pid)} (the content it had is now a revision of its own)")
        print("  Drive's import gives every object of the deck a new id, so a sync base recorded before this "
              "restore describes none of it any more: sync refuses such a deck rather than reporting every "
              "element as deleted. The deck is yours to edit in Slides; to convert the PDF again, use "
              "--new-deck, which leaves this one alone.")
    else:
        new = upload_new(drive, data, f"{name} (restored from {what})")
        print(f"{what} -> a new presentation: {deck_url(new)}")


if __name__ == "__main__":
    sys.exit(main())
