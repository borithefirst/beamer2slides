"""What do smart chips and the other Docs-native objects look like from outside?

The HTML importer cannot create any of them (`insertDate`, `insertPerson`,
`insertRichLink`, `insertTableOfContents` are not in the public v1 discovery —
`tools/probe_docs_features.py`, phase two). But a real document will be full of
them, so an IR that means to round-trip has to *read* them faithfully. This makes
a document with one labelled line per object, waits for a human (or a browser
agent) to insert them, and then shows what each one becomes in

  * `documents.get` — the "DOM" of the doc, and
  * every Drive export format that could carry it.

    python tools\\probe_docs_chips.py create   # makes the doc, prints its URL
    python tools\\probe_docs_chips.py read     # after inserting the chips by hand
    python tools\\probe_docs_chips.py delete

The document id is remembered in out/docs-probe/chips-doc.txt.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from beamer2slides.google_auth import credentials, docs_service, drive_service  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "docs-probe"
STATE = OUT / "chips-doc.txt"
DOC_MIME = "application/vnd.google-apps.document"

# One line per object. The marker is what `read` looks for; the rest says what to
# do to that line in the editor.
CASES = [
    ("DATE", "type @ then a date, e.g. @today, and pick the chip:"),
    ("PERSON", "type @ then your own name or e-mail and pick the person chip:"),
    ("FILE", "type @ then a Drive file name and pick the file chip:"),
    ("LINK", "paste a https URL and choose the chip form when offered:"),
    ("DROPDOWN", "type @dropdown and insert one (any preset):"),
    ("PLACEHOLDER", "type @placeholder / @variable if the menu offers it:"),
    ("STOPWATCH", "type @stopwatch or @timer if the menu offers it:"),
    ("EMOJI", "type @emoji and pick one, or use Insert > Emoji:"),
    ("FOOTNOTE", "put the caret at the end and Insert > Footnote:"),
    ("EQUATION", "put the caret at the end and Insert > Equation, type x^2:"),
    ("WATERMARK", "Insert > Watermark > Text (a document-wide object):"),
    ("COMMENT", "select this whole line and add a comment on it:"),
]

# Objects that do not live on a line of their own.
NOTES = [
    "Also, if you can: Insert > Table of contents (any style) at the very end,",
    "and add a SECOND DOCUMENT TAB (the left sidebar) with a word of text in it.",
]

SOURCE = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
          "<h1>Smart-chip probe</h1>"
          + "".join(f"<p>{marker}: {what} </p>" for marker, what in CASES)
          + "".join(f"<p>{line}</p>" for line in NOTES)
          + "</body></html>")

# Everything a ParagraphElement can be, minus the plain textRun. `dateElement` is
# not in the reference's ParagraphElement union but is what a date chip reads as.
ELEMENT_KINDS = ("person", "richLink", "dateElement", "footnoteReference", "equation",
                 "inlineObjectElement", "horizontalRule", "pageBreak", "columnBreak", "autoText")

# Docs marks "an object sits here" inside ordinary text with this private-use
# character: a placeholder chip, and the watermark inside the default header.
OBJECT_SENTINEL = ""

EXPORTS = {"html": "text/html", "txt": "text/plain", "md": "text/markdown",
           "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def services():
    creds = credentials()
    return drive_service(creds), docs_service(creds)


def create() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    drive, _ = services()
    media = MediaIoBaseUpload(io.BytesIO(SOURCE.encode("utf-8")), mimetype="text/html")
    doc_id = drive.files().create(
        body={"name": "b2s smart-chip probe (edit me)", "mimeType": DOC_MIME},
        media_body=media, fields="id").execute()["id"]
    STATE.write_text(doc_id, encoding="utf-8")
    print(f"https://docs.google.com/document/d/{doc_id}/edit")
    for marker, what in CASES:
        print(f"  {marker:12s} {what}")
    for line in NOTES:
        print(f"  {line}")


def body_of(tab_or_doc: dict) -> list:
    return tab_or_doc.get("body", {}).get("content", [])


def line_text(para: dict) -> str:
    return "".join(el.get("textRun", {}).get("content", "") for el in para["elements"])


def describe(el: dict) -> str | None:
    """A one-line account of a non-text paragraph element."""
    for kind in ELEMENT_KINDS:
        if kind in el:
            return f"{kind} {json.dumps(el[kind], sort_keys=True)}"
    return None


def report_structure(doc: dict, label: str) -> None:
    print(f"\n--- {label}: paragraph elements that are not plain text ---")
    for entry in body_of(doc):
        para = entry.get("paragraph")
        if para is None:
            for other in ("table", "tableOfContents", "sectionBreak"):
                if other in entry:
                    keys = sorted(entry[other].keys())
                    print(f"  [structural] {other}: keys {keys}")
            continue
        text = line_text(para).strip()
        marker = text.split(":", 1)[0] if text else ""
        for el in para["elements"]:
            what = describe(el)
            if what:
                print(f"  {marker[:12]:12s} {what[:400]}")
            elif "textRun" not in el:
                # A dropdown chip is exactly this: an element with a span and no type.
                print(f"  {marker[:12]:12s} ANONYMOUS element, no content key, "
                      f"span {el.get('startIndex')}-{el.get('endIndex')}")
        # A chip may also hide in a textRun's style, and comments/suggestions tag runs.
        for el in para["elements"]:
            run = el.get("textRun")
            if not run:
                continue
            extra = {k: v for k, v in run.items() if k not in ("content", "textStyle")}
            style = run.get("textStyle", {})
            if OBJECT_SENTINEL in run.get("content", ""):
                print(f"  {marker[:12]:12s} object sentinel U+E907 inside a plain textRun")
            if extra:
                print(f"  {marker[:12]:12s} textRun extras {json.dumps(extra)[:300]}")
            if "link" in style:
                print(f"  {marker[:12]:12s} link {json.dumps(style['link'])[:200]}")

    for top in ("footnotes", "inlineObjects", "positionedObjects", "namedRanges",
                "headers", "footers", "lists", "suggestedDocumentStyleChanges"):
        if doc.get(top):
            print(f"  [top-level] {top}: {len(doc[top])} entr(ies) {sorted(doc[top])[:4]}")
    style = doc.get("documentStyle", {})
    for key in sorted(style):
        if "ackground" in key or "atermark" in key or "Header" in key or "Footer" in key:
            print(f"  [documentStyle] {key} = {json.dumps(style[key])[:200]}")


def read() -> None:
    doc_id = STATE.read_text(encoding="utf-8").strip()
    drive, docs = services()

    doc = docs.documents().get(documentId=doc_id).execute()
    (OUT / "chips-document.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    report_structure(doc, "documents.get (no tabs)")

    # Document tabs are opt-in: without this flag a multi-tab doc reports only its first.
    tabbed = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    (OUT / "chips-tabs.json").write_text(json.dumps(tabbed, indent=1), encoding="utf-8")
    tabs = tabbed.get("tabs", [])
    print(f"\n--- tabs: {len(tabs)} top-level ---")
    for tab in tabs:
        props = tab.get("tabProperties", {})
        child = tab.get("childTabs", [])
        print(f"  {props.get('title')!r} id={props.get('tabId')} index={props.get('index')} "
              f"children={len(child)}")
        for sub in child:
            print(f"    child {sub.get('tabProperties', {}).get('title')!r}")
    if tabs:
        report_structure(tabs[-1].get("documentTab", {}), "last tab body")

    for ext, mime in EXPORTS.items():
        try:
            blob = drive.files().export(fileId=doc_id, mimeType=mime).execute()
        except HttpError as err:
            print(f"\n--- export {ext}: refused {err.resp.status} ---")
            continue
        path = OUT / f"chips.{ext}"
        path.write_bytes(blob)
        if ext == "docx":
            print(f"\n--- export {ext}: {len(blob)} bytes -> {path.name} ---")
            continue
        text = blob.decode("utf-8", "replace")
        print(f"\n--- export {ext}: what follows each marker ---")
        for marker, _ in CASES:
            # The HTML export is one long line, so take a window, not a line.
            at = text.find(marker + ":")
            if at < 0:
                print(f"  {marker:12s} (not found)")
                continue
            end = text.find("\n", at) if ext != "html" else at + 320
            print(f"  {marker:12s} {text[at:end if end > 0 else len(text)][:320]}")
        for what, needle in (("the watermark", "DRAFT"), ("the comment", "PROBE COMMENT"),
                             ("tab 2's text", "SECOND TAB BODY")):
            print(f"  [{what} in this export: {needle in text}]")


def delete() -> None:
    doc_id = STATE.read_text(encoding="utf-8").strip()
    drive, _ = services()
    drive.files().delete(fileId=doc_id).execute()
    STATE.unlink()
    print("deleted")


if __name__ == "__main__":
    {"create": create, "read": read, "delete": delete}[
        sys.argv[1] if len(sys.argv) > 1 else "read"]()
