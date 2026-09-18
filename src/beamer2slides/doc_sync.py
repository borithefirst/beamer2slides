"""Push a canonical HTML file to a Google Doc, and keep the two in step ever after.

The Docs twin of `sync.py`, and the same bargain (docs/google-docs.md): the file in
git is what the source says, the document is what the reader says, and where both
moved the document wins. What is different is written down in `doc_merge`: a chip is
never rewritten, a list's ordered-ness comes from the file because no read can report
it, and a push after the first can never be a re-import — `files.update` destroys
every named range, and the named ranges are what tell the merge which block is which.

Two commands:

    beamer2slides docs push doc.html      creates the document and plants the anchors
    beamer2slides docs sync doc.html      merges both ways and writes

Test hook: `B2S_DOCS_BEFORE_WRITE` (a shell command) runs once after planning and
before the first write — that is how the re-plan is exercised on a live document.

`push` writes the file back with a key on every block and a `<meta>` naming the
document, so the file alone says where it lives. `sync` ends by **regenerating the
file from the document it just wrote**: file, document and base then say the same
thing, and the next sync writes nothing. That is the property to check after any
change here.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import time
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from . import doc_ir, doc_merge
from .google_auth import credentials, docs_service, drive_service

DOC_MIME = "application/vnd.google-apps.document"
STATE_DIR = ".b2s"
ATTEMPTS = 3  # how often a write may be re-planned when the document moved under it
DOC_ID = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")


# ---------------------------------------------------------------- state beside the file

def state_dir(path: Path) -> Path:
    return path.parent / STATE_DIR


def base_path(path: Path) -> Path:
    """The base of the last sync: what both sides agreed on, and what a three-way
    merge needs to tell a source change from a document change."""
    return state_dir(path) / f"{path.stem}.base.json"


def load_base(path: Path, document: str) -> dict | None:
    file = base_path(path)
    if not file.exists():
        return None
    try:
        base = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return base if base.get("document") == document else None


def save_base(path: Path, base: dict) -> Path:
    """Written to a temporary file and moved into place: a run killed here leaves the
    previous base, never half of one (half a base is no base at all)."""
    file = base_path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(file.name + ".writing")
    tmp.write_text(json.dumps(base, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, file)
    return file


def document_id(text: str) -> str:
    """The document a URL, an id or a canonical file names."""
    found = DOC_ID.search(text)
    return found.group(1) if found else text.strip()


def url(ident: str) -> str:
    return f"https://docs.google.com/document/d/{ident}/edit"


# ---------------------------------------------------------------- reading both sides

def read_file(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path}: no such file (this command reads the canonical HTML)")
    return doc_ir.key_blocks(doc_ir.from_html(path.read_text(encoding="utf-8")))


def write_file(path: Path, ir: dict, document: str) -> None:
    path.write_text(doc_ir.to_html(dict(ir) | {"document": document}), encoding="utf-8")


def read_document(docs, ident: str, *sources: dict) -> tuple[dict, dict]:
    """The document, and the IR of its first tab, with what a read cannot say filled in.

    Keys come from the named ranges; a list's ordered-ness comes from `sources` — the
    canonical file and the base — because an imported list never reports its own
    (`doc_merge.restore_unreadable`).
    """
    doc = docs.documents().get(documentId=ident, includeTabsContent=True).execute()
    ir = doc_ir.from_document(doc)
    doc_ir.apply_keys(ir, doc_ir.named_ranges_of(doc, ir.get("tab")))
    doc_merge.restore_unreadable(ir, *sources)
    ir["document"] = ident
    return doc, ir


def open_comments(drive, ident: str) -> list[str]:
    """The comments on the document nobody has resolved, for the report.

    A comment is a question somebody asked about a passage, and a sync that rewrites
    that passage answers it by accident — the merge has no idea one is there, because
    a comment lives in Drive and not in the document's content at all. So they are
    read (`drive.file` reaches the documents this tool made) and said out loud.
    Nothing here writes or resolves one: that is the reader's to do, in the browser.
    """
    try:
        found = drive.comments().list(
            fileId=ident, includeDeleted=False, pageSize=100,
            fields="comments(content,resolved,author/displayName,"
                   "quotedFileContent/value,replies/content)").execute().get("comments", [])
    except HttpError as err:
        return [f"the document's comments could not be read ({err.resp.status})"]
    out = []
    for comment in found:
        if comment.get("resolved"):
            continue
        about = (comment.get("quotedFileContent") or {}).get("value", "")
        replies = len(comment.get("replies", []))
        out.append(f"{comment.get('author', {}).get('displayName', 'somebody')} "
                   f"on {about[:40]!r}: {comment.get('content', '')[:80]!r}"
                   + (f", and {replies} repl{'y' if replies == 1 else 'ies'}" if replies else ""))
    return out


def limits(ours: dict, doc: dict) -> list[str]:
    """What this sync cannot carry, said out loud rather than dropped in silence."""
    out = list(ours.get("unsupported", []))
    tabs = doc_ir.tabs_of(doc)
    if len(tabs) > 1:
        names = ", ".join(t.get("tabProperties", {}).get("title", "?") for t in tabs[1:])
        out.append(f"the document has {len(tabs)} tabs; only the first one is synced "
                   f"(left alone: {names})")
    return out


# ---------------------------------------------------------------- writing

def send(docs, ident: str, requests: list[dict], revision: str | None = None) -> None:
    body: dict = {"requests": requests}
    if revision:
        # The plan is indices into the document as it was read. Anyone who typed since
        # has moved them, so the write is refused rather than landing in the wrong place.
        body["writeControl"] = {"requiredRevisionId": revision}
    docs.documents().batchUpdate(documentId=ident, body=body).execute()


def moved_on(error: HttpError) -> bool:
    """Whether a refused write means the document changed under the plan."""
    return error.resp.status in (400, 409) and "revision" in str(error).lower()


def plant_ranges(docs, ident: str, ir: dict) -> int:
    """Name every keyed block the document does not name yet.

    One batch, and on a refusal one request at a time, so a range the API will not
    take names itself in the output instead of costing the rest their anchors.
    """
    requests = doc_ir.name_requests(ir)
    if not requests:
        return 0
    try:
        send(docs, ident, requests)
        return len(requests)
    except HttpError as err:
        print(f"  the batch of {len(requests)} named ranges was refused ({err.resp.status}); "
              f"trying them one at a time")
    done = 0
    for request in requests:
        try:
            send(docs, ident, [request])
            done += 1
        except HttpError as err:
            print(f"  no anchor for {request['createNamedRange']['name']}: {err.resp.status}")
    return done


def settle(docs, ident: str, path: Path, ours: dict, base: dict,
           planned: list[dict] | None = None) -> dict:
    """After a write: read the document, anchor what is new, and let that read be both
    the new base and the new canonical file. File, document and base agree from here."""
    _, live = read_document(docs, ident, ours, base)
    if planned:
        doc_merge.adopt_keys(live, planned)
    doc_ir.key_blocks(live)
    tidy = doc_merge.tidy_requests(live)
    if tidy:
        send(docs, ident, tidy)
    if plant_ranges(docs, ident, live) or tidy:
        _, live = read_document(docs, ident, ours, base)
    write_file(path, live, ident)
    save_base(path, live)
    return live


# ---------------------------------------------------------------- the report

def write_report(path: Path, info: dict) -> Path:
    state_dir(path).mkdir(parents=True, exist_ok=True)
    # Not `with_suffix`: `table.sync-report` already looks suffixed, and the report
    # would land in `table.md` next to a file called table.html.
    stem = state_dir(path) / f"{path.stem}.sync-report"
    json_file = stem.with_name(stem.name + ".json")
    md_file = stem.with_name(stem.name + ".md")
    json_file.write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = [f"# {path.name} → {info['url']}", "",
             f"{time.strftime('%Y-%m-%d %H:%M:%S')} — {info['requests']} request(s) "
             f"{'planned' if info['dry_run'] else 'written'}", ""]
    for title, items in (("Conflicts (the document won)",
                          [f"`{c['key']}`: the source said {c['ours']!r}, "
                           f"the document says {c['theirs']!r}" for c in info["conflicts"]]),
                         ("Left alone", info["notes"]),
                         ("Open comments in the document", info.get("comments", [])),
                         ("Written from the source", info["applied"]),
                         ("Kept from the document", info["kept"])):
        if items:
            lines += [f"## {title}", ""] + [f"- {line}" for line in items] + [""]
    md_file.write_text("\n".join(lines), encoding="utf-8")
    return md_file


def _words(block: dict) -> str:
    """The block, short enough to read in a report. A table has no words of its own,
    so its cells stand in for it."""
    if block.get("kind") == "table":
        return " | ".join(doc_merge.block_text(inner) for row in block.get("rows", [])
                          for cell in row for inner in cell)[:60]
    return doc_merge.block_text(block)[:60]


def _summary(result: dict) -> tuple[list[str], list[str]]:
    applied, kept = [], []
    for block in result["blocks"]:
        origin, key = block.get("origin"), block.get("key", "(unkeyed)")
        words = _words(block)
        if block.get("moved"):
            applied.append(f"`{key}` moved to where the source has it: {words!r}")
        elif origin == "added by the source":
            applied.append(f"`{key}` added: {words!r}")
        elif origin == "merged":
            applied.append(f"`{key}` rewritten: {words!r}")
        elif origin in ("added in the document", "unknown to the base"):
            kept.append(f"`{key}` is the document's own: {words!r}")
        elif origin == "kept from the document":
            kept.append(f"`{key}` says what the document says: {words!r}")
        elif origin == "kept over a source delete":
            kept.append(f"`{key}` was deleted in the source but edited here: {words!r}")
    return applied, kept


# ---------------------------------------------------------------- commands

def push(path: Path, name: str | None = None, new_doc: bool = False) -> dict:
    """Create the document from the canonical file and plant one anchor per block."""
    source = read_file(path)
    if source.get("document") and not new_doc:
        raise SystemExit(f"{path} already names document {source['document']}\n"
                         f"  {url(source['document'])}\n"
                         f"  Use `docs sync` to write to it, or --new-doc for a second one.")
    creds = credentials()
    drive, docs = drive_service(creds), docs_service(creds)
    html = doc_ir.to_html(dict(source) | {"document": None})
    ident = drive.files().create(
        body={"name": name or source.get("title") or path.stem, "mimeType": DOC_MIME},
        media_body=MediaIoBaseUpload(io.BytesIO(html.encode("utf-8")), mimetype="text/html"),
        fields="id").execute()["id"]

    doc, live = read_document(docs, ident)
    # The importer builds what HTML can say; give what came back the file's keys, and
    # the ordered-ness the import threw away.
    doc_merge.inherit_keys(source, live)
    doc_merge.restore_unreadable(live, source)
    plant_ranges(docs, ident, live)
    live = settle(docs, ident, path, source, source)
    return {"document": ident, "url": url(ident), "blocks": len(live["blocks"]),
            "anchored": sum(1 for b in live["blocks"] if b.get("rangeId")),
            "notes": limits(source, doc)}


def sync(path: Path, document: str | None = None, dry_run: bool = False,
         assume_base: str | None = None) -> dict:
    """Merge the file and the document three ways, write, and rewrite the file."""
    ours = read_file(path)
    ident = document_id(document) if document else ours.get("document")
    if not ident:
        raise SystemExit(f"{path} does not say which document it belongs to.\n"
                         f"  Pass --doc <url or id>, or `docs push {path.name}` to make one.")
    creds = credentials()
    docs, drive = docs_service(creds), drive_service(creds)
    base = load_base(path, ident)
    doc, theirs = read_document(docs, ident, ours, base or {"blocks": []})
    if base is None:
        base = _no_base(path, ours, theirs, assume_base)

    result = doc_merge.plan(base, ours, theirs)

    def report(extra: dict | None = None) -> dict:
        applied, kept = _summary(result)
        return {"document": ident, "url": url(ident), "dry_run": dry_run,
                "requests": len(result["requests"]), "conflicts": result["conflicts"],
                "notes": limits(ours, doc) + result["notes"],
                "applied": [t["note"] for t in shaped] + applied, "kept": kept,
                "comments": asked} | (extra or {})

    shaped: list[dict] = []
    asked = open_comments(drive, ident)
    if dry_run:
        info = report({"plan": result["structure"] + result["requests"],
                       "requests": len(result["structure"]) + len(result["requests"])})
        info["applied"] = [t["note"] for t in result["shaped"]] + info["applied"]
        info["report"] = str(write_report(path, info))
        return info

    hook = os.environ.pop("B2S_DOCS_BEFORE_WRITE", None)  # (tests: someone types now)
    if hook:
        subprocess.run(hook, shell=True, check=False)
    doc, theirs, base, result, shaped = _write_structure(
        docs, ident, path, ours, base, doc, theirs, result)
    info = report()
    attempt, revision = 0, doc.get("revisionId")
    while True:
        try:
            if result["requests"]:
                send(docs, ident, result["requests"], revision)
            break
        except HttpError as err:
            attempt += 1
            if not moved_on(err) or attempt >= ATTEMPTS:
                raise
            # Somebody typed between the read and the write. Read again and re-plan:
            # their words are now part of `theirs`, so the merge keeps them.
            print(f"  the document changed while this sync was planned; reading it again "
                  f"({attempt}/{ATTEMPTS - 1})")
            doc, theirs = read_document(docs, ident, ours, base)
            ours = read_file(path)  # (the file may have been committed to in the meantime)
            result = doc_merge.plan(base, ours, theirs)
            doc, theirs, base, result, more = _write_structure(
                docs, ident, path, ours, base, doc, theirs, result)
            shaped += more
            revision = doc.get("revisionId")
            info = report({"replanned": attempt})

    live = settle(docs, ident, path, ours, base, result["blocks"])
    info["blocks"] = len(live["blocks"])
    info["report"] = str(write_report(path, info))
    return info


def _write_structure(docs, ident: str, path: Path, ours: dict, base: dict,
                     doc: dict, theirs: dict, result: dict) -> tuple:
    """Write what the grid needs before the words, and plan the words again.

    A table the source added and rows or columns it changed cannot go in the batch
    that writes the text: `insertTable` and its kin move every index below them, and
    the cells they create do not exist until they have been sent. So they go first,
    on their own; the document is read again; a table that was just built is given the
    file's key and anchored, or the next plan would not recognise it and would build
    it a second time; the base takes those tables as the document now reports them —
    that grid is no longer a difference between the sides — and the words are planned
    against what the document says now.

    Returns the document, its IR, the base, the new plan and what was written, in
    words for the report. A batch that leaves work over (two tables added at one
    index, which one send cannot place) comes round again.
    """
    shaped: list[dict] = []
    for _ in range(ATTEMPTS):
        if not result["structure"]:
            break
        try:
            send(docs, ident, result["structure"], doc.get("revisionId"))
        except HttpError as err:
            if not moved_on(err):
                raise
            print("  the document changed while the table edits were planned; reading it again")
            doc, theirs = read_document(docs, ident, ours, base)
            result = doc_merge.plan(base, ours, theirs)
            continue
        shaped += result["shaped"]
        doc, theirs = read_document(docs, ident, ours, base)
        if doc_merge.anchor_tables(theirs, result["shaped"]):
            plant_ranges(docs, ident, theirs)
            doc, theirs = read_document(docs, ident, ours, base)
        base = doc_merge.rebase_tables(base, theirs, result["shaped"])
        result = doc_merge.plan(base, ours, theirs)
    return doc, theirs, base, result, shaped


def _no_base(path: Path, ours: dict, theirs: dict, assume: str | None) -> dict:
    """What to do when the last sync's base is not there.

    Without it there is no way to tell a source change from a document change, and
    guessing either way can throw work away. The answer is the person's to give.
    """
    where = base_path(path)
    if assume == "file":
        return ours     # every difference is the document's: nothing is written
    if assume == "document":
        return theirs   # every difference is the source's: the file is written out
    raise SystemExit(
        f"no base for this document at {where}\n"
        f"  A three-way merge needs to know what both sides agreed on last time.\n"
        f"  --assume-base file      the document is right where they differ (writes nothing,\n"
        f"                          then rewrites {path.name} from the document)\n"
        f"  --assume-base document  the file is right where they differ (writes the file out)")
