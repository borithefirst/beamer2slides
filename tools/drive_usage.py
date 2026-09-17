"""Report what beamer2slides has put in Google Drive: decks, and picture assets left by
versions that uploaded pictures separately.

Read-only. Usage: python tools/drive_usage.py
"""

from beamer2slides.google_auth import drive_service
from beamer2slides.gslides import execute


def list_all(drive, q: str, fields: str) -> list[dict]:
    out, token = [], None
    while True:
        r = execute(drive.files().list(q=q, fields=f"nextPageToken,files({fields})", pageSize=1000, pageToken=token))
        out += r["files"]
        token = r.get("nextPageToken")
        if not token:
            return out


def main() -> None:
    drive = drive_service()
    folders = list_all(drive, "name = 'beamer2slides assets' and mimeType = 'application/vnd.google-apps.folder' "
                              "and trashed = false", "id")
    for folder in folders:
        files = list_all(drive, f"'{folder['id']}' in parents and trashed = false", "size")
        print(f"assets folder: {len(files)} files, {sum(int(f.get('size', 0)) for f in files) / 1e6:.1f} MB")
    decks = list_all(drive, "mimeType = 'application/vnd.google-apps.presentation' and trashed = false",
                     "id,name,createdTime")
    print(f"decks created by the app: {len(decks)}")
    for d in sorted(decks, key=lambda d: d["createdTime"]):
        print(f"  {d['createdTime'][:16]}  {d['name']}")


if __name__ == "__main__":
    main()
