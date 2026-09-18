"""Report what beamer2slides has put in Google Drive: converted decks, backup copies, and the
staging decks a killed sync left behind.

    python tools/drive_usage.py                      # read-only report
    python tools/drive_usage.py --delete-staging     # delete only the leftover staging decks
    python tools/drive_usage.py --delete-orphans     # decks no folder here can reach any more

A staging deck is a temporary file sync imports pictures through; the live deck copies the
pictures, so the staging file is useless once the sync is past that point and is deleted at the
end of every run that reaches it. A run that was killed leaves it in Drive, and no sync ever
deletes it on its own: an id read from a file could name anything (docs/sync.md, "If a sync or a
pull dies"). `--delete-staging` is a person saying yes to that, and it only ever touches files
that carry the `b2sStaging` marker this program writes **and** the staging name, and that are
older than `--older-than-hours` (12 by default, so a sync running right now is never touched).

Backup copies (`--backup drive|both`, `appProperties.b2sBackupOf`) are a way back to a deck and
are never deleted here - they are only listed, so they can be recognised among the decks.

`--delete-orphans` deletes converted decks **no folder under `--root` names any more** (default
`out`): a test deck whose folder was thrown away, and nothing this checkout can still open. It is
deliberately over-eager about what counts as a reference - every id-shaped word in every .json,
.md or .txt under the roots, not just emit.json - because being wrong that way keeps a deck, and
being wrong the other way loses one. It also spares a deck a backup copy points at, and anything
younger than `--older-than-hours` (a conversion running right now has not written its emit.json
yet). Nothing happens without `--yes`, and what it then does is **move them to the Drive trash**,
where they can be restored for 30 days - a wrong answer here should cost a restore, not a deck.
Decks are the only thing this touches: the assets folder is a shared store and is only measured.

Read-only unless --delete-staging or --delete-orphans --yes is given.
"""

import argparse
import calendar
import re
import time
from pathlib import Path

from beamer2slides.google_auth import drive_service
from beamer2slides.gslides import execute

STAGING_NAME = "beamer2slides sync staging (temporary)"
ID_WORD = re.compile(r"[A-Za-z0-9_-]{20,}")
READABLE = (".json", ".md", ".txt", ".html", ".csv")


def list_all(drive, q: str, fields: str) -> list[dict]:
    out, token = [], None
    while True:
        r = execute(drive.files().list(q=q, fields=f"nextPageToken,files({fields})", pageSize=1000, pageToken=token))
        out += r["files"]
        token = r.get("nextPageToken")
        if not token:
            return out


def hours_old(created: str) -> float:
    """Drive's createdTime is UTC (`...Z`), and so is `timegm` of it."""
    return (time.time() - calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))) / 3600


def is_staging(d: dict) -> bool:
    """A staging deck goes by the name sync gives it. Only files this app created are visible at
    all (the `drive.file` scope), so a file of this app with exactly that name is one of them;
    syncs from now on also write an `appProperties` marker, shown as `marked` in the report."""
    return d.get("name") == STAGING_NAME or bool((d.get("appProperties") or {}).get("b2sStaging"))


def named_anywhere(roots: list[Path]) -> tuple[set[str], list[Path]]:
    """Every id-shaped word written in a file under `roots`, and the files that could not be read.
    A deck is reachable when its id is in here: emit.json and sync/base.json are what
    `--deck <folder>` reads, but a sync report, a backups.json or a note someone left is a way to
    the deck too, and this is the side to err on."""
    words: set[str] = set()
    unreadable: list[Path] = []
    for root in roots:
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in READABLE:
                continue
            try:
                words |= set(ID_WORD.findall(p.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                unreadable.append(p)
    return words, unreadable


def orphans(decks: list[dict], backups: list[dict], roots: list[Path], hours: float) -> tuple[list[dict], list[str]]:
    """The decks nothing under `roots` names, and a line for each one spared and why."""
    named, unreadable = named_anywhere(roots)
    if unreadable:  # a file we cannot read may be the one naming a deck: judge nothing
        return [], [f"{len(unreadable)} file(s) under the roots could not be read "
                    f"(first: {unreadable[0]}): nothing is deleted"]
    kept_for = {(d.get("appProperties") or {}).get("b2sBackupOf") for d in backups}
    out, why = [], []
    for d in sorted(decks, key=lambda d: d["createdTime"]):
        if d["id"] in named:
            continue
        if d["id"] in kept_for:
            why.append(f"{d['id'][:12]} spared: a backup copy in Drive points at it")
        elif hours_old(d["createdTime"]) < hours:
            why.append(f"{d['id'][:12]} spared: only {hours_old(d['createdTime']):.1f} h old")
        else:
            out.append(d)
    return out, why


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delete-staging", action="store_true", help="delete the leftover staging decks")
    ap.add_argument("--delete-orphans", action="store_true", help="delete decks no folder under --root names")
    ap.add_argument("--root", type=Path, action="append", default=None,
                    help="where to look for references to a deck (default: out); repeatable")
    ap.add_argument("--yes", action="store_true", help="--delete-orphans: actually delete (without it, a dry run)")
    ap.add_argument("--older-than-hours", type=float, default=12.0,
                    help="only files older than this (default 12), so a run in progress is never touched")
    args = ap.parse_args()
    roots = [r for r in (args.root or [Path("out")]) if r.is_dir()]
    drive = drive_service()
    folders = list_all(drive, "name = 'beamer2slides assets' and mimeType = 'application/vnd.google-apps.folder' "
                              "and trashed = false", "id")
    for folder in folders:
        files = list_all(drive, f"'{folder['id']}' in parents and trashed = false", "size")
        print(f"assets folder: {len(files)} files, {sum(int(f.get('size', 0)) for f in files) / 1e6:.1f} MB")
    decks = list_all(drive, "mimeType = 'application/vnd.google-apps.presentation' and trashed = false",
                     "id,name,createdTime,appProperties")
    staging = [d for d in decks if is_staging(d)]
    backups = [d for d in decks if (d.get("appProperties") or {}).get("b2sBackupOf")]
    aside = {d["id"] for d in staging} | {d["id"] for d in backups}
    own = [d for d in decks if d["id"] not in aside]
    print(f"decks created by the app: {len(decks)} ({len(own)} decks, {len(backups)} backup copies, "
          f"{len(staging)} staging leftovers)")
    for d in sorted(own, key=lambda d: d["createdTime"]):
        print(f"  {d['createdTime'][:16]}  {d['name']}")
    for d in sorted(backups, key=lambda d: d["createdTime"]):
        print(f"  {d['createdTime'][:16]}  [backup of {d['appProperties']['b2sBackupOf'][:12]}] {d['name']}")
    if staging:
        print(f"\n{len(staging)} staging deck(s) of a sync that was interrupted (safe to delete: the live decks "
              f"hold their own copies of the pictures):")
        for d in sorted(staging, key=lambda d: d["createdTime"]):
            marker = (d.get("appProperties") or {}).get("b2sStaging")
            print(f"  {d['createdTime'][:16]}  {d['id']}"
                  f"{f'  marked, for deck {marker[:12]}' if marker else ''}")
        old = [d for d in staging if hours_old(d["createdTime"]) >= args.older_than_hours]
        if not args.delete_staging:
            print(f"  delete them with: python tools/drive_usage.py --delete-staging "
                  f"({len(old)} of them are over {args.older_than_hours:g} h old)")
        else:
            for d in old:
                execute(drive.files().delete(fileId=d["id"]))
                print(f"  deleted {d['id']}")
            print(f"deleted {len(old)} staging deck(s); {len(staging) - len(old)} left "
                  f"(younger than {args.older_than_hours:g} h: a sync may be using them)")
    if not args.delete_orphans:
        return
    if not roots:
        raise SystemExit(f"--delete-orphans: no folder to read references from "
                         f"({', '.join(str(r) for r in (args.root or [Path('out')]))} is not a directory)")
    doomed, spared = orphans(own, backups, roots, args.older_than_hours)
    print(f"\nreferences read from: {', '.join(str(r) for r in roots)}")
    for line in spared:
        print(f"  {line}")
    if not doomed:
        print(f"no orphaned decks: every one of the {len(own)} is named by a file under the roots")
        return
    for d in doomed:
        print(f"  {'trashed' if args.yes else 'would trash'}  {d['createdTime'][:16]}  {d['id'][:12]}  {d['name']}")
        if args.yes:  # trashed, not deleted: Drive keeps it restorable for 30 days
            execute(drive.files().update(fileId=d["id"], body={"trashed": True}))
    if args.yes:
        print(f"moved {len(doomed)} deck(s) nothing here named to the Drive trash (restorable for 30 days); "
              f"{len(own) - len(doomed)} kept")
    else:
        print(f"{len(doomed)} deck(s) nothing here names. Add --yes to move them to the Drive trash.")


if __name__ == "__main__":
    main()
