"""Backups and Drive version history of a converted deck (docs/sync.md, "Never lose deck edits").

    python tools/deck_backup.py list    --deck <url|id|out folder>
    python tools/deck_backup.py export  --deck ... [--revision ID] [--to FILE]
    python tools/deck_backup.py restore --deck ... [--revision ID | --from FILE] [--in-place]
    python tools/deck_backup.py prune   --deck ... [--keep 10] [--older-than-days N] [--yes]

`list` shows what Drive keeps of the presentation (its revisions, newest last) and the local
backups `convert --backup` / `sync --backup` wrote, with what they take up on disk. `export` saves
one revision as a .pptx. `restore` uploads a .pptx (a local backup, or a revision exported on the
fly) as a **new** presentation and prints its URL; `--in-place` puts it back into the same file
instead (the same `files.update` a rebuild uses, so the current content becomes a revision of its
own).

`prune` is the only destructive action here: every sync of a deck writes a .pptx of it, so a folder
that is synced often grows without end. It keeps the newest `--keep` backups (and everything newer
than `--older-than-days`, when given) and deletes the rest - but only files `backups.json` says
this program wrote, never anything else in the folder, and never without `--yes`. The log entries
stay, with `deleted` on the ones whose file is gone: what the deck was, and when, is evidence worth
keeping even when the way back is not kept.

Only the app's own files are reachable (drive.file scope): a deck this tool made or opened.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from beamer2slides import guard
from beamer2slides.google_auth import credentials, drive_service
from beamer2slides.guard import PPTX_MIME, backup_dir, deck_url
from beamer2slides.gslides import execute
from beamer2slides.sync import resolve_deck


def revisions(drive, pid: str) -> list[dict]:
    out, token = [], None
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


def revision_pptx(drive, pid: str, revision: dict) -> bytes:
    links = revision.get("exportLinks") or execute(
        drive.revisions().get(fileId=pid, revisionId=revision["id"], fields="exportLinks")).get("exportLinks", {})
    if PPTX_MIME not in links:
        raise SystemExit(f"revision {revision['id']} has no .pptx export link (it has {sorted(links)})")
    return download(links[PPTX_MIME])


def pick(revs: list[dict], wanted: str | None) -> dict:
    """`wanted`: a revision id, 'latest', 'previous', or 'before:<ISO time>' (the newest revision
    not newer than that time - the deck as it was when convert recorded `modifiedTime`)."""
    if not revs:
        raise SystemExit("this file has no revisions in Drive")
    if wanted in (None, "latest"):
        return revs[-1]
    if wanted == "previous":
        return revs[-2] if len(revs) > 1 else revs[-1]
    if wanted.startswith("before:"):
        when = wanted[len("before:"):]
        earlier = [r for r in revs if r["modifiedTime"] <= when]
        if not earlier:
            raise SystemExit(f"no revision at or before {when} (the oldest is {revs[0]['modifiedTime']})")
        return earlier[-1]
    found = next((r for r in revs if r["id"] == wanted), None)
    if not found:
        raise SystemExit(f"no revision {wanted} (there are {[r['id'] for r in revs]})")
    return found


def upload(drive, data: bytes, name: str, pid: str | None = None) -> str:
    import io

    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=PPTX_MIME)
    if pid:
        execute(drive.files().update(fileId=pid, media_body=media, fields="id"))
        return pid
    return execute(drive.files().create(body={"name": name, "mimeType": "application/vnd.google-apps.presentation"},
                                        media_body=media, fields="id"))["id"]


def local_backups(folder: Path | None) -> list[dict]:
    if folder is None:
        return []
    log = backup_dir(folder) / "backups.json"
    if not log.exists():
        return []
    try:
        return json.loads(log.read_text(encoding="utf-8"))
    except ValueError:
        return []


def prune(folder: Path | None, keep: int, older_than_days: float | None, yes: bool) -> None:
    if folder is None:
        raise SystemExit("prune needs --deck to be an output folder (its backups.json says what this program wrote)")
    r = guard.prune_backups(folder, keep, older_than_days, delete=yes)
    print(f"{r['files']} backup file(s) in {backup_dir(folder)}, {r['bytes'] / 1e6:.1f} MB")
    if not r["doomed"]:
        print(f"nothing to prune (the newest {keep} are kept"
              f"{f', and everything under {older_than_days:g} days old' if older_than_days is not None else ''})")
        return
    for d in r["doomed"]:
        e = d["entry"]
        print(f"  {'deleted' if yes else 'would delete'} {Path(d['file']).name} ({d['bytes'] / 1e6:.1f} MB, "
              f"{e.get('action', '?')} at {e.get('checked', '?')}, revision {e.get('revisionId', '?')})")
    if yes:
        print(f"deleted {len(r['doomed'])} file(s), {r['freed'] / 1e6:.1f} MB; "
              f"{r['files'] - len(r['doomed'])} kept")
    else:
        print(f"{len(r['doomed'])} file(s), {r['freed'] / 1e6:.1f} MB. Add --yes to delete them; each one is a "
              f"way back to the deck as it was at that revision.")


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
    args = ap.parse_args()
    pid, folder = resolve_deck(args.deck)
    if args.action == "prune":  # no Google call: this is about files on disk
        return prune(folder, args.keep, args.older_than_days, args.yes)
    drive = drive_service()
    revs = revisions(drive, pid)
    if args.action == "list":
        info = execute(drive.files().get(fileId=pid, fields="name,modifiedTime"))
        print(f"{info['name']}  {deck_url(pid)}")
        print(f"{len(revs)} revision(s) in Drive (oldest first):")
        for r in revs:
            who = (r.get("lastModifyingUser") or {}).get("displayName", "?")
            print(f"  {r['id']:>6}  {r['modifiedTime'][:19].replace('T', ' ')}  {who}"
                  f"{'  keepForever' if r.get('keepForever') else ''}"
                  f"{'  (pptx export)' if PPTX_MIME in (r.get('exportLinks') or {}) else ''}")
        entries = local_backups(folder)
        for b in entries:
            line = f"  {b.get('checked', '?')}  {b.get('action')}  revision {b.get('revisionId')}  {b.get('reason', '')}"
            print(line)
            for k, v in (b.get("backup") or {}).items():
                if k in ("file", "drive"):
                    print(f"      {k}: {v['url'] if isinstance(v, dict) else v}"
                          f"{'  (deleted)' if k == 'file' and not Path(v).exists() else ''}")
        files = guard.backup_files(entries)
        if files:
            size = sum(p.stat().st_size for p, _ in files) / 1e6
            print(f"  {len(files)} backup file(s) on disk, {size:.1f} MB"
                  + (f" - `prune --deck {folder}` offers to delete the older ones" if len(files) > 10 else ""))
        return
    if args.action == "export":
        rev = pick(revs, args.revision)
        data = revision_pptx(drive, pid, rev)
        path = args.to or (backup_dir(folder) if folder else Path(".")) / f"revision-{rev['id']}-{pid[:12]}.pptx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"revision {rev['id']} ({rev['modifiedTime'][:19].replace('T', ' ')}) -> {path} ({len(data) / 1e6:.2f} MB)")
        return
    if args.source:
        data = args.source.read_bytes()
        what = str(args.source)
    else:
        rev = pick(revs, args.revision)
        data = revision_pptx(drive, pid, rev)
        what = f"revision {rev['id']}"
    name = execute(drive.files().get(fileId=pid, fields="name"))["name"]
    if args.in_place:
        upload(drive, data, name, pid)
        print(f"{what} written back into {deck_url(pid)} (the content it had is now a revision of its own)")
    else:
        new = upload(drive, data, f"{name} (restored from {what})")
        print(f"{what} -> a new presentation: {deck_url(new)}")


if __name__ == "__main__":
    sys.exit(main())
