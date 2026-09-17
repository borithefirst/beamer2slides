"""Can we anchor to a Google Doc the way alt-text titles anchor a Slides deck?

Named ranges are the only id you can plant at an arbitrary text position (there is
no createBookmark, and paragraphs carry no id). Google documents that they track
edits, that they can split, and that copied content does not carry them — but not
whether a range dies with its text, nor any count cap, nor what a Drive copy does.
This measures all of it. See docs/google-docs.md, risk 2.

    .venv\\Scripts\\python.exe tools\\probe_docs_api.py [--cap N] [--keep]

Stage 0 also answers a scope question worth money: whether the narrow `drive.file`
scope already reaches `documents.get` for a doc this app created, so we never have
to ask for the `documents` scope (which reaches every Doc the user owns).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from beamer2slides.google_auth import credentials, docs_service, drive_service  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "docs-probe"
DOC_MIME = "application/vnd.google-apps.document"

SOURCE = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<h1>Anchor probe</h1>
<p>ALPHA the first paragraph of the probe document END</p>
<p>BRAVO the second paragraph of the probe document END</p>
<p>CHARLIE the third paragraph of the probe document END</p>
<p>DELTA the fourth paragraph of the probe document END</p>
</body></html>
"""


def text_of(doc: dict) -> str:
    """The body as one string."""
    out = []
    for el in doc["body"]["content"]:
        for run in el.get("paragraph", {}).get("elements", []):
            out.append(run.get("textRun", {}).get("content", ""))
    return "".join(out)


def find(doc: dict, needle: str) -> tuple[int, int]:
    """Start/end index of `needle`, in the API's UTF-16 index space."""
    for el in doc["body"]["content"]:
        for run in el.get("paragraph", {}).get("elements", []):
            content = run.get("textRun", {}).get("content")
            if content and needle in content:
                at = run["startIndex"] + content.index(needle)
                return at, at + len(needle)
    raise LookupError(needle)


def ranges_of(docs, doc_id: str) -> dict[str, list[tuple[int, int]]]:
    doc = docs.documents().get(documentId=doc_id).execute()
    return {name: [(r["startIndex"], r["endIndex"]) for r in nr["namedRanges"][0]["ranges"]]
            for name, nr in sorted(doc.get("namedRanges", {}).items())}


def batch(docs, doc_id: str, requests: list[dict]) -> dict:
    return docs.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}).execute()


def stage0(drive, docs) -> str | None:
    """Create a doc and see whether drive.file alone reaches documents.get."""
    media = MediaIoBaseUpload(io.BytesIO(SOURCE.encode("utf-8")), mimetype="text/html")
    doc_id = drive.files().create(body={"name": "b2s anchor probe", "mimeType": DOC_MIME},
                                  media_body=media, fields="id").execute()["id"]
    print(f"created {doc_id}")
    try:
        doc = docs.documents().get(documentId=doc_id).execute()
    except HttpError as err:
        detail = err.reason or ""
        print(f"  documents.get refused: {err.resp.status} {detail[:150]}")
        if "has not been used in project" in detail or "is disabled" in detail:
            print("\n  -> The Docs API is still off for this Cloud project. Enable it at")
            print("     https://console.cloud.google.com/apis/library/docs.googleapis.com")
            print("     (project beamer2slides), wait a minute, and run this again.")
        elif "insufficient" in detail.lower() or err.resp.status == 403:
            print("\n  -> drive.file is NOT enough; add the documents scope to")
            print("     google_auth.SCOPES, delete token.json and re-consent.")
        drive.files().delete(fileId=doc_id).execute()
        return None
    print(f"  documents.get works on the narrow drive.file scope — no `documents` scope needed")
    print(f"  revisionId {doc.get('revisionId')!r}, {len(text_of(doc))} chars of body")
    return doc_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=400, help="how many ranges to try planting")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    creds = credentials()
    drive, docs = drive_service(creds), docs_service(creds)
    results: dict[str, object] = {}

    print("=== stage 0: is the API reachable, and with which scope? ===")
    doc_id = stage0(drive, docs)
    if not doc_id:
        return 1
    made = [doc_id]

    try:
        print("\n=== stage 1: plant an anchor on each paragraph ===")
        doc = docs.documents().get(documentId=doc_id).execute()
        reqs = []
        for word in ("ALPHA", "BRAVO", "CHARLIE", "DELTA"):
            start, _ = find(doc, word)
            # Anchor the whole paragraph: from the word to just before its newline.
            para = next(el for el in doc["body"]["content"]
                        if el.get("startIndex", -1) <= start < el.get("endIndex", -1))
            reqs.append({"createNamedRange": {
                "name": f"b2s:{word.lower()}",
                "range": {"startIndex": start, "endIndex": para["endIndex"] - 1}}})
        batch(docs, doc_id, reqs)
        planted = ranges_of(docs, doc_id)
        print(f"  planted {len(planted)}: {json.dumps(planted)}")
        results["planted"] = planted

        print("\n=== stage 2: type inside, before and after a range ===")
        doc = docs.documents().get(documentId=doc_id).execute()
        inside, _ = find(doc, "first")
        batch(docs, doc_id, [{"insertText": {"location": {"index": inside},
                                             "text": "VERY "}}])
        after = ranges_of(docs, doc_id)
        print(f"  after typing inside b2s:alpha -> {after['b2s:alpha']}")
        print(f"     (was {planted['b2s:alpha']}; the range should have grown by 5)")
        results["insert_inside"] = after["b2s:alpha"]

        doc = docs.documents().get(documentId=doc_id).execute()
        start, _ = find(doc, "BRAVO")
        batch(docs, doc_id, [{"insertText": {"location": {"index": start}, "text": "X"}}])
        at_edge = ranges_of(docs, doc_id)
        print(f"  after typing at the very start of b2s:bravo -> {at_edge['b2s:bravo']}")
        print("     (does a range swallow text typed at its left edge?)")
        results["insert_at_start"] = at_edge["b2s:bravo"]

        print("\n=== stage 3: delete every character of a range ===")
        doc = docs.documents().get(documentId=doc_id).execute()
        rng = ranges_of(docs, doc_id)["b2s:charlie"][0]
        batch(docs, doc_id, [{"deleteContentRange": {
            "range": {"startIndex": rng[0], "endIndex": rng[1]}}}])
        left = ranges_of(docs, doc_id)
        gone = "b2s:charlie" not in left
        print(f"  b2s:charlie {'is GONE' if gone else 'SURVIVES as ' + str(left['b2s:charlie'])}")
        print("     -> an anchor whose text the user deletes",
              "disappears; treat a missing anchor as a deletion." if gone else
              "lingers empty; sweep zero-length ranges.")
        results["deleted_all_text"] = None if gone else left["b2s:charlie"]

        print("\n=== stage 4: does a Drive copy carry the anchors? ===")
        copy_id = drive.files().copy(fileId=doc_id, body={"name": "b2s anchor probe copy"},
                                     fields="id").execute()["id"]
        made.append(copy_id)
        copied = ranges_of(docs, copy_id)
        print(f"  copy has {len(copied)} named ranges: {sorted(copied)}")
        print("     -> a duplicated document keeps its anchors."
              if copied else "     -> a copy LOSES the anchors; re-tag after copying.")
        results["survive_drive_copy"] = sorted(copied)

        print(f"\n=== stage 5: how many ranges can one document hold? (trying {args.cap}) ===")
        doc = docs.documents().get(documentId=doc_id).execute()
        start, end = find(doc, "DELTA")
        try:
            for chunk in range(0, args.cap, 100):
                batch(docs, doc_id, [
                    {"createNamedRange": {"name": f"b2s:bulk{i}",
                                          "range": {"startIndex": start, "endIndex": end}}}
                    for i in range(chunk, min(chunk + 100, args.cap))])
            total = len(ranges_of(docs, doc_id))
            print(f"  {total} ranges planted with no complaint (no cap found below {args.cap})")
            results["cap"] = f">= {total}"
        except HttpError as err:
            total = len(ranges_of(docs, doc_id))
            print(f"  refused after {total} ranges: {err.resp.status} {(err.reason or '')[:120]}")
            results["cap"] = total

        print("\n=== stage 6: revision control ===")
        doc = docs.documents().get(documentId=doc_id).execute()
        stale = doc["revisionId"]
        batch(docs, doc_id, [{"insertText": {"location": {"index": 1}, "text": "Z"}}])
        try:
            docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": "Q"}}],
                      "writeControl": {"requiredRevisionId": stale}}).execute()
            print("  a stale requiredRevisionId was ACCEPTED — do not rely on it")
            results["required_revision"] = "accepted"
        except HttpError as err:
            print(f"  a stale requiredRevisionId is rejected: {err.resp.status} "
                  f"{(err.reason or '')[:90]}")
            print("     -> the sync.py plan/send/re-plan pattern ports directly")
            results["required_revision"] = "rejected"
    finally:
        (OUT / "anchors.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(f"\nwrote {OUT / 'anchors.json'}")
        if args.keep:
            print("kept:", ", ".join(made))
        else:
            for fid in made:
                try:
                    drive.files().delete(fileId=fid).execute()
                except HttpError:
                    pass
            print(f"deleted {len(made)} probe files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
