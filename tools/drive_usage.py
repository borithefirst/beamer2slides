"""Report what beamer2slides has put in Google Drive: converted decks, backup copies, and the
staging decks a killed sync left behind.

    python tools/drive_usage.py                      # read-only report
    python tools/drive_usage.py --delete-staging     # delete only the leftover staging decks

A staging deck is a temporary file sync imports pictures through; the live deck copies the
pictures, so the staging file is useless once the sync is past that point and is deleted at the
end of every run that reaches it. A run that was killed leaves it in Drive, and no sync ever
deletes it on its own: an id read from a file could name anything (docs/sync.md, "If a sync or a
pull dies"). `--delete-staging` is a person saying yes to that, and it only ever touches files
that carry the `b2sStaging` marker this program writes **and** the staging name, and that are
older than `--older-than-hours` (12 by default, so a sync running right now is never touched).

Backup copies (`--backup drive|both`, `appProperties.b2sBackupOf`) are a way back to a deck and
are never deleted here - they are only listed, so they can be recognised among the decks.

Read-only unless --delete-staging is given.
"""

import argparse
import calendar
import time

from beamer2slides.google_auth import drive_service
from beamer2slides.gslides import execute

STAGING_NAME = "beamer2slides sync staging (temporary)"


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delete-staging", action="store_true", help="delete the leftover staging decks")
    ap.add_argument("--older-than-hours", type=float, default=12.0,
                    help="only staging decks older than this (default 12), so a running sync is never touched")
    args = ap.parse_args()
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


if __name__ == "__main__":
    main()
