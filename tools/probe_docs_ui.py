"""Do named-range anchors survive edits made by a *human* in the Docs UI?

`tools/probe_docs_api.py` measured the API path. The editor is a different code
path, and Google's one documented warning — that content copied within a document
does not carry its range — lives there. This makes a doc to drive by hand (or by
browser automation) and reads the anchors back between edits.

    python tools\\probe_docs_ui.py create     # makes the doc, prints its URL
    python tools\\probe_docs_ui.py read       # anchors + text, after an edit
    python tools\\probe_docs_ui.py delete     # clean up

The document id is remembered in out/docs-probe/ui-doc.txt.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from googleapiclient.http import MediaIoBaseUpload

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from beamer2slides.google_auth import credentials, docs_service, drive_service  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "docs-probe"
STATE = OUT / "ui-doc.txt"
DOC_MIME = "application/vnd.google-apps.document"

# Each paragraph gets an anchor, and says what is to be done to it.
PARAGRAPHS = [
    ("keep", "KEEP this paragraph is the control and must not be touched"),
    ("type", "TYPE click in the middle of this one and type a word"),
    ("cut", "CUT select this whole line, Ctrl+X, then Ctrl+V at the end"),
    ("copy", "COPY select this whole line and Ctrl+C, paste it at the end"),
    ("plain", "PLAIN copy this line and paste it back with Ctrl+Shift+V"),
    ("undo", "UNDO delete this whole line, then press Ctrl+Z"),
    ("suggest", "SUGGEST switch to Suggesting mode and delete this line"),
]

SOURCE = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
          "<h1>Anchor probe: edits by hand</h1>"
          + "".join(f"<p>{text}</p>" for _, text in PARAGRAPHS)
          + "<p>PASTE ZONE — paste below this line.</p></body></html>")


def services():
    creds = credentials()
    return drive_service(creds), docs_service(creds)


def paragraphs_of(doc: dict):
    """(startIndex, endIndex, text) per body paragraph."""
    for el in doc["body"]["content"]:
        para = el.get("paragraph")
        if not para:
            continue
        text = "".join(r.get("textRun", {}).get("content", "") for r in para["elements"])
        if text.strip():
            yield el["startIndex"], el["endIndex"], text.rstrip("\n")


def create() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    drive, docs = services()
    media = MediaIoBaseUpload(io.BytesIO(SOURCE.encode("utf-8")), mimetype="text/html")
    doc_id = drive.files().create(
        body={"name": "b2s anchor probe (edit me)", "mimeType": DOC_MIME},
        media_body=media, fields="id").execute()["id"]
    doc = docs.documents().get(documentId=doc_id).execute()

    wanted = {name: text.split(" ", 1)[0] for name, text in PARAGRAPHS}
    requests = []
    for start, end, text in paragraphs_of(doc):
        for name, marker in wanted.items():
            if text.startswith(marker):
                requests.append({"createNamedRange": {
                    "name": f"b2s:{name}",
                    "range": {"startIndex": start, "endIndex": end - 1}}})
    docs.documents().batchUpdate(documentId=doc_id,
                                 body={"requests": requests}).execute()
    STATE.write_text(doc_id, encoding="utf-8")
    print(f"{len(requests)} anchors planted")
    print(f"https://docs.google.com/document/d/{doc_id}/edit")


def read() -> None:
    doc_id = STATE.read_text(encoding="utf-8").strip()
    mode = sys.argv[2] if len(sys.argv) > 2 else "DEFAULT_FOR_CURRENT_ACCESS"
    _, docs = services()
    doc = docs.documents().get(documentId=doc_id, suggestionsViewMode=mode).execute()
    named = doc.get("namedRanges", {})
    print(f"revision {doc['revisionId'][:20]}…  {len(named)} anchors")
    for name in sorted(f"b2s:{n}" for n, _ in PARAGRAPHS):
        entry = named.get(name)
        if not entry:
            print(f"  {name:14s} GONE")
            continue
        spans = [(r["startIndex"], r["endIndex"]) for r in entry["namedRanges"][0]["ranges"]]
        body = "".join(r.get("textRun", {}).get("content", "")
                       for el in doc["body"]["content"]
                       for r in el.get("paragraph", {}).get("elements", []))
        # The body string starts at index 1, so slice with a 1 offset.
        shown = " / ".join(body[a - 1:b - 1][:38].replace("\n", "⏎") for a, b in spans)
        print(f"  {name:14s} {len(spans)} span(s) {spans} {shown!r}")
    # The ids sit on the textRun, not on the ParagraphElement wrapping it, and only
    # SUGGESTIONS_INLINE shows them: the default view reads as if every pending
    # suggestion had been rejected.
    pending = set()
    for el in doc["body"]["content"]:
        for run in el.get("paragraph", {}).get("elements", []):
            for key in ("suggestedInsertionIds", "suggestedDeletionIds"):
                for sid in run.get("textRun", {}).get(key, []):
                    pending.add((key, sid))
    print(f"  pending suggestions (this view): {sorted(pending) or 'none'}")

    print("\n  paragraphs now in the document:")
    for _, _, text in paragraphs_of(doc):
        print(f"    {text[:70]}")


def delete() -> None:
    doc_id = STATE.read_text(encoding="utf-8").strip()
    drive, _ = services()
    drive.files().delete(fileId=doc_id).execute()
    STATE.unlink()
    print("deleted")


if __name__ == "__main__":
    {"create": create, "read": read, "delete": delete}[
        sys.argv[1] if len(sys.argv) > 1 else "read"]()
