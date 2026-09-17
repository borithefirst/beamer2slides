"""Push a canonical HTML file to a Google Doc, edit it by hand, sync both ways.

The whole Docs loop on one small document, end to end, against the live API:

    python tools\\docs_spike.py push [file.html]  # convert, plant named ranges, save base
    python tools\\docs_spike.py read              # what the document says now
    python tools\\docs_spike.py sync [--dry-run]  # three-way merge, write, rewrite the file
    python tools\\docs_spike.py delete

`push` writes the canonical file back with a `b2s:` id on every block, creates the
document through Drive conversion and names one range per block. `sync` reads the
document, merges it with the file against the base of the last sync, sends the
edits with `requiredRevisionId`, and then **regenerates the file from the document
it just wrote**, so file and document say the same thing and the next sync writes
nothing. That last property is the point of the spike: `sync` prints the request
count, and a second `sync` must print 0.

Everything lives in out/docs-spike/: `doc.txt` (the id), `source.html` (the
canonical file unless one is named), `base.json` (the IR of the last read).
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

from beamer2slides import doc_ir, doc_merge  # noqa: E402
from beamer2slides.google_auth import credentials, docs_service, drive_service  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "docs-spike"
STATE = OUT / "doc.txt"
SOURCE = OUT / "source.html"
WHICH = OUT / "source-path.txt"   # which file `push` was given, so `sync` uses the same one
BASE = OUT / "base.json"
DOC_MIME = "application/vnd.google-apps.document"

SAMPLE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>b2s docs spike</title>
</head>
<body>
<h1>The spike document</h1>
<p>This paragraph came from the <b>canonical file</b>, and so did
<a href="https://example.com/">this link</a>.</p>
<h2>What to do with it</h2>
<ul>
 <li>Edit a few words here, in the document.</li>
 <li>Insert a smart chip on the line below.</li>
</ul>
<p>Owner: </p>
<ol>
 <li>Then run sync.</li>
 <li>Then run sync again: it must write nothing.</li>
</ol>
<p style="text-align:center">The end.</p>
</body>
</html>
"""


def services():
    creds = credentials()
    return drive_service(creds), docs_service(creds)


def doc_id() -> str:
    return STATE.read_text(encoding="utf-8").strip()


def url(ident: str) -> str:
    return f"https://docs.google.com/document/d/{ident}/edit"


def read_document(docs, ident: str, *sources: dict) -> tuple[dict, dict]:
    """The document, and the IR of its first tab with keys from the named ranges.

    `sources` are the files that know what the read cannot report — a list's
    ordered-ness, once Drive's importer has built it (doc_merge.restore_unreadable).
    """
    doc = docs.documents().get(documentId=ident, includeTabsContent=True).execute()
    ir = doc_ir.from_document(doc)
    doc_ir.apply_keys(ir, doc_ir.named_ranges_of(doc, ir.get("tab")))
    doc_merge.restore_unreadable(ir, *sources)
    return doc, ir


def send(docs, ident: str, requests: list[dict], revision: str | None = None) -> None:
    body: dict = {"requests": requests}
    if revision:
        body["writeControl"] = {"requiredRevisionId": revision}
    docs.documents().batchUpdate(documentId=ident, body=body).execute()


def plant_ranges(docs, ident: str, ir: dict) -> int:
    """Name every keyed block the document does not name yet.

    One batch, and on a refusal one request at a time, so a range the API will not
    take (a table's span, say) is named in the output instead of losing the rest.
    """
    requests = doc_ir.name_requests(ir)
    if not requests:
        return 0
    try:
        send(docs, ident, requests)
        return len(requests)
    except HttpError as err:
        print(f"  the batch of {len(requests)} named ranges was refused: {err.resp.status}")
    done = 0
    for request in requests:
        try:
            send(docs, ident, [request])
            done += 1
        except HttpError as err:
            print(f"  refused: {request['createNamedRange']['name']} "
                  f"{request['createNamedRange']['range']} ({err.resp.status})")
    return done


def save_base(ir: dict) -> None:
    BASE.write_text(json.dumps(ir, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- commands

def push(args) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = Path(args.file) if args.file else SOURCE
    if not path.exists():
        path.write_text(SAMPLE, encoding="utf-8")
        print(f"wrote a sample canonical file to {path}")
    source = doc_ir.key_blocks(doc_ir.from_html(path.read_text(encoding="utf-8")))
    path.write_text(doc_ir.to_html(source), encoding="utf-8")  # keys are part of the file
    print(f"{path}: {len(source['blocks'])} blocks, each with a key")

    drive, docs = services()
    media = MediaIoBaseUpload(io.BytesIO(doc_ir.to_html(source).encode("utf-8")),
                              mimetype="text/html")
    ident = drive.files().create(
        body={"name": source["title"] or "b2s docs spike", "mimeType": DOC_MIME},
        media_body=media, fields="id").execute()["id"]
    STATE.write_text(ident, encoding="utf-8")
    WHICH.write_text(str(path.resolve()), encoding="utf-8")

    _, live = read_document(docs, ident)
    # The importer builds what HTML can say; give what came back the file's keys,
    # and the ordered-ness the import threw away.
    doc_merge.inherit_keys(source, live)
    doc_merge.restore_unreadable(live, source)
    named = plant_ranges(docs, ident, live)
    _, live = read_document(docs, ident, source)
    save_base(live)
    keyed = sum(1 for b in live["blocks"] if b.get("key"))
    print(f"{named} named ranges planted; {keyed}/{len(live['blocks'])} blocks keyed")
    print(url(ident))


def read(args) -> None:
    _, docs = services()
    doc, ir = read_document(docs, doc_id())
    print(f"{doc.get('title')!r}  revision {doc.get('revisionId')}  tab {ir.get('tab')}")
    for block in ir["blocks"]:
        frozen = "".join(f" [{r['chip']}:{r.get('text') or r.get('value', '')}]"
                         for r in block.get("runs", []) if r.get("frozen"))
        print(f"  {str(block.get('span')):12s} {block['kind']:9s} "
              f"{block.get('key', '(unkeyed)'):34s} {doc_ir.runs_text(block.get('runs', []))[:48]!r}"
              f"{frozen}")
    (OUT / "live.html").write_text(doc_ir.to_html(ir), encoding="utf-8")
    print(f"-> {OUT / 'live.html'}")


def sync(args) -> None:
    path = Path(args.file) if args.file else _source_path()
    ours = doc_ir.key_blocks(doc_ir.from_html(path.read_text(encoding="utf-8")))
    base = json.loads(BASE.read_text(encoding="utf-8"))
    _, docs = services()
    ident = doc_id()
    doc, theirs = read_document(docs, ident, ours, base)

    result = doc_merge.plan(base, ours, theirs)
    for clash in result["conflicts"]:
        print(f"  conflict {clash['key']}: source {clash['ours']!r} "
              f"vs document {clash['theirs']!r} — the document wins")
    for note in result["notes"]:
        print(f"  note {note}")
    print(f"requests: {len(result['requests'])}")
    if args.dry_run:
        (OUT / "requests.json").write_text(json.dumps(result["requests"], indent=1),
                                           encoding="utf-8")
        return
    if result["requests"]:
        send(docs, ident, result["requests"], doc.get("revisionId"))

    # Read what the document became, name the blocks it does not name yet, and let
    # that read be both the new base and the new canonical file.
    merged = {"blocks": result["blocks"]}
    _, live = read_document(docs, ident, merged, ours, base)
    doc_ir.key_blocks(live)
    if plant_ranges(docs, ident, live):
        _, live = read_document(docs, ident, merged, ours, base)
    path.write_text(doc_ir.to_html(live), encoding="utf-8")
    save_base(live)
    print(f"{path} rewritten from the document; base saved")


def _source_path() -> Path:
    return Path(WHICH.read_text(encoding="utf-8").strip()) if WHICH.exists() else SOURCE


def delete(args) -> None:
    drive, _ = services()
    drive.files().delete(fileId=doc_id()).execute()
    STATE.unlink()
    print("deleted")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, function, takes_file in (("push", push, True), ("read", read, False),
                                       ("sync", sync, True), ("delete", delete, False)):
        command = sub.add_parser(name)
        command.set_defaults(run=function, file=None, dry_run=False)
        if takes_file:
            command.add_argument("file", nargs="?")
        if name == "sync":
            command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
