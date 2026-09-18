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

import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import subprocess
import time
import urllib.request
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
    # A picture's `uri` is a URL that dies within the hour: no use to the next sync.
    tmp.write_text(json.dumps(_without(base, "uri"), indent=1, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, file)
    return file


def _without(value, key: str):
    """`value` with `key` taken out of every dict in it, however deep."""
    if isinstance(value, dict):
        return {k: _without(v, key) for k, v in value.items() if k != key}
    if isinstance(value, list):
        return [_without(v, key) for v in value]
    return value


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
    ir = doc_ir.key_blocks(doc_ir.from_html(path.read_text(encoding="utf-8")))
    for run in _pictures(ir):
        local = picture_file(path, run.get("src", ""))
        if local is None:
            continue
        if local.is_file():
            run["sha"] = digest(local.read_bytes())
        else:
            run["missing"] = True
            ir.setdefault("unsupported", []).append(
                f"<img src={run['src']!r}>: no such file beside {path.name} — "
                f"the picture is left as the document has it")
    return ir


def _pictures(ir: dict) -> list[dict]:
    """Every picture run of every tab."""
    return [run for part in doc_ir.parts(ir) for run in doc_merge._image_runs(part["blocks"])]


def picture_file(path: Path, src: str) -> Path | None:
    """The file an `<img src>` names, beside the canonical file; None for a URL."""
    if not src or re.match(r"^[a-z][a-z0-9+.-]*:", src, re.I):
        return None
    return path.parent / src


def digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


def data_uri(file: Path) -> str:
    mime = mimetypes.guess_type(file.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(file.read_bytes()).decode()}"


def embedded(path: Path, ir: dict) -> dict:
    """The file as the importer should see it: every picture's bytes inside it.

    Measured: Drive's HTML import embeds a `data:` URI and keeps `alt` and `title` as
    the picture's description and title. A relative `src` would mean nothing to it.
    """
    out = json.loads(json.dumps(ir))
    for run in _pictures(out):
        local = picture_file(path, run.get("src", ""))
        if local is not None and local.is_file():
            run["src"] = data_uri(local)
    return out


def write_file(path: Path, ir: dict, document: str) -> None:
    path.write_text(doc_ir.to_html(dict(ir) | {"document": document}), encoding="utf-8")


def read_document(docs, ident: str, *sources: dict) -> tuple[dict, dict]:
    """The document, and its IR — every tab — with what a read cannot say filled in.

    Keys come from the named ranges; a list's ordered-ness comes from `sources` — the
    canonical file and the base — because an imported list never reports its own
    (`doc_merge.restore_unreadable`).
    """
    doc = _get(docs, ident)
    ours, base = (list(sources) + [None, None])[:2]
    return doc, document_ir(doc, ident, ours, base)


def _get(docs, ident: str) -> dict:
    return docs.documents().get(documentId=ident, includeTabsContent=True).execute()


def document_ir(doc: dict, ident: str, ours: dict | None = None,
                base: dict | None = None) -> dict:
    """The first tab is the IR; the others go under `tabs` (`doc_ir.parts`), each
    filled in from the file's and the base's tab with the same id."""
    ir = _part_of(doc, None, ours, base)
    extra = [_part_of(doc, tab.get("tabProperties", {}).get("tabId"), ours, base)
             for tab in doc_ir.tabs_of(doc)[1:]]
    if extra:
        ir["tabs"] = extra
    ir["document"] = ident
    return ir


def _part_of(doc: dict, tab: str | None, ours: dict | None, base: dict | None) -> dict:
    """One tab's IR (None: the first). `ours` and `base` are whole IRs: the tab of
    theirs with the same id is what fills this one in."""
    if tab:
        ours, base = doc_ir.tab_part(ours, tab), doc_ir.tab_part(base, tab)
    part = doc_ir.from_document(doc, tab)
    doc_ir.apply_keys(part, doc_ir.named_ranges_of(doc, part.get("tab")))
    doc_merge.restore_unreadable(part, *[s for s in (ours, base) if s])
    doc_merge.restore_pictures(part, base, ours)
    if tab:
        part.pop("title", None)
        for each in doc_ir.tabs_of(doc):
            props = each.get("tabProperties", {})
            if props.get("tabId") == tab:
                part["title"] = props.get("title", "")
                if props.get("parentTabId"):
                    part["parent"] = props["parentTabId"]
    return part


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


def limits(ours: dict) -> list[str]:
    """What this sync cannot carry, said out loud rather than dropped in silence."""
    return list(ours.get("unsupported", []))


def stamp_of(ir: dict, part: dict) -> str | None:
    """The `tabId` a tab's requests carry: none for the first tab, which is where a
    request without one goes (`doc_merge.on_tab`)."""
    return None if part is ir else part.get("tab")


# ---------------------------------------------------------------- writing

def send(docs, ident: str, requests: list[dict], revision: str | None = None) -> dict:
    body: dict = {"requests": requests}
    if revision:
        # The plan is indices into the document as it was read. Anyone who typed since
        # has moved them, so the write is refused rather than landing in the wrong place.
        body["writeControl"] = {"requiredRevisionId": revision}
    return docs.documents().batchUpdate(documentId=ident, body=body).execute() or {}


def moved_on(error: HttpError) -> bool:
    """Whether a refused write means the document changed under the plan."""
    return error.resp.status in (400, 409) and "revision" in str(error).lower()


class Stager:
    """Pictures for `insertInlineImage`, which takes a URL and never bytes.

    The Slides sync's staging deck, one dimension smaller: the pictures a batch needs
    are imported as a document of their own (`data:` URIs, which Drive's HTML import
    embeds), the `contentUri` Docs then gives each one is what the batch inserts, and
    the staging document is deleted once the batch is in. Measured: a picture inserted
    that way is copied into the document and still loads after the staging file is
    gone. No link is ever made public, which is the rule on the Slides side too.
    """

    NAME = "beamer2slides docs staging (temporary)"

    def __init__(self, drive, docs, path: Path):
        self.drive, self.docs, self.path = drive, docs, path
        self.urls: dict[str, str] = {}
        self.files: list[str] = []

    def resolve(self, requests: list[dict]) -> list[dict]:
        """The requests with every staged picture's URL filled in."""
        wanted = [r["insertInlineImage"]["uri"][len(doc_merge.STAGE):] for r in requests
                  if r.get("insertInlineImage", {}).get("uri", "").startswith(doc_merge.STAGE)]
        needed = sorted(set(wanted) - self.urls.keys())
        if needed:
            self._stage(needed)
        out = []
        for request in requests:
            image = request.get("insertInlineImage")
            if image and image["uri"].startswith(doc_merge.STAGE):
                request = {"insertInlineImage": image | {
                    "uri": self.urls[image["uri"][len(doc_merge.STAGE):]]}}
            out.append(request)
        return out

    def _stage(self, sources: list[str]) -> None:
        # Numbered paragraphs, so a picture the import could not take is missed by
        # name rather than shifting every URL after it onto the wrong picture.
        body = "".join(f'<p>{n}:<img src="{data_uri(self.path.parent / src)}"></p>'
                       for n, src in enumerate(sources))
        ident = self.drive.files().create(
            body={"name": self.NAME, "mimeType": DOC_MIME, "appProperties": {"b2sStaging": "docs"}},
            media_body=MediaIoBaseUpload(io.BytesIO(f"<html><body>{body}</body></html>".encode()),
                                         mimetype="text/html"), fields="id").execute()["id"]
        self.files.append(ident)
        staged = doc_ir.from_document(
            self.docs.documents().get(documentId=ident, includeTabsContent=True).execute())
        for block in staged["blocks"]:
            label = doc_ir.runs_text(block["runs"]).split(":")[0]
            uris = [r.get("uri") for r in block["runs"] if r.get("chip") == "image"]
            if label.isdigit() and int(label) < len(sources) and uris and uris[0]:
                self.urls[sources[int(label)]] = uris[0]
        missing = [s for s in sources if s not in self.urls]
        if missing:
            raise RuntimeError(f"the staging document brought no picture for {missing[:3]}")

    def close(self) -> None:
        for ident in self.files:
            try:
                self.drive.files().delete(fileId=ident).execute()
            except HttpError as err:
                print(f"  the staging document {ident} could not be deleted ({err.resp.status})")
        self.files = []


def fetch_pictures(path: Path, live: dict) -> int:
    """Put the pictures a reader inserted into the document beside the canonical file.

    Their `contentUri` lasts about half an hour and names nothing of ours, so a file
    that pointed there would be broken by tomorrow. Each one is saved once, under its
    object id, in `<stem>.media/`, and from then on the file carries it like any other
    picture — which is what makes a reader's picture something git can keep.
    """
    done = 0
    for run in _pictures(live):
        if run.get("src") or not run.get("uri") or not run.get("value"):
            continue
        try:
            with urllib.request.urlopen(run["uri"], timeout=60) as reply:
                data, mime = reply.read(), reply.headers.get_content_type()
        except OSError as err:
            print(f"  the picture {run['value']} could not be fetched: {err}")
            continue
        suffix = mimetypes.guess_extension(mime) or ".png"
        suffix = ".jpg" if suffix in (".jpe", ".jpeg") else suffix
        folder = path.parent / f"{path.stem}.media"
        folder.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", run["value"]) + suffix
        (folder / name).write_bytes(data)
        run["src"], run["sha"] = f"{folder.name}/{name}", digest(data)
        done += 1
    return done


def plant_ranges(docs, ident: str, ir: dict, tab: str | None = None) -> int:
    """Name every keyed block of one tab the document does not name yet.

    One batch, and on a refusal one request at a time, so a range the API will not
    take names itself in the output instead of costing the rest their anchors.
    """
    requests = doc_merge.on_tab(doc_ir.name_requests(ir), tab)
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
           planned: dict | None = None, drive=None) -> dict:
    """After a write: read the document, anchor what is new, and let that read be both
    the new base and the new canonical file. File, document and base agree from here.

    `planned` is what each tab was written as, by its stamp (None: the first tab).
    With `drive`, the equations get their LaTeX (`equation_latex`)."""
    planned = planned or {}
    doc, live = read_document(docs, ident, ours, base)
    tidy, named = [], 0
    for part in doc_ir.parts(live):
        stamp = stamp_of(live, part)
        if planned.get(stamp):
            doc_merge.adopt_keys(part, planned[stamp])
        doc_ir.key_blocks(part)
        tidy += doc_merge.on_tab(doc_merge.tidy_requests(part), stamp)
    if tidy:
        send(docs, ident, tidy)
    for part in doc_ir.parts(live):
        named += plant_ranges(docs, ident, part, stamp_of(live, part))
    if named or tidy:
        doc, live = read_document(docs, ident, ours, base)
    for part in doc_ir.parts(live):
        if planned.get(stamp_of(live, part)):
            doc_merge.place_pictures(part, planned[stamp_of(live, part)])
    if drive is not None:
        equation_latex(drive, ident, doc, live)
    fetch_pictures(path, live)
    write_file(path, live, ident)
    save_base(path, live)
    return live


def equation_latex(drive, ident: str, doc: dict, live: dict) -> int:
    """Give each equation of `live` its LaTeX, which `documents.get` does not say at all
    (an equation reads as `{}`) and the Markdown export does (`doc_ir.latex_of`).

    Only on the read that becomes the file and the base: the planning reads leave it
    out, and the merge does not mind, since a frozen run is compared with the base's
    and both carry the same LaTeX. An export refused costs the file its LaTeX, no more.
    """
    spots = doc_ir.equation_spots(doc)
    if not spots:
        return 0
    try:
        markdown = drive.files().export(fileId=ident, mimeType="text/markdown").execute()
    except HttpError as err:
        print(f"  no LaTeX for the equations: the Markdown export was refused ({err.resp.status})")
        return 0
    if isinstance(markdown, bytes):
        markdown = markdown.decode("utf-8")
    return doc_ir.attach_latex(live, doc_ir.latex_of(spots, markdown))


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
                          [(f"[{c['tab']}] " if c.get("tab") is not None else "")
                           + f"`{c['key']}`: the source said {c['ours']!r}, "
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
    first = embedded(path, source) | {"document": None}
    first.pop("tabs", None)  # the importer makes one tab of whatever it is given
    html = doc_ir.to_html(first)
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
    # The other tabs are written the way a sync writes a tab the source added.
    tabs = doc_merge.pair_tabs({"blocks": []}, source, live)
    written = _write_tabs(drive, docs, ident, path, source, {"blocks": []}, live, tabs,
                          first=False)
    planned = {None: source["blocks"]} | {w["stamp"]: w["result"]["blocks"] for w in written}
    live = settle(docs, ident, path, source, source, planned, drive)
    blocks = [b for part in doc_ir.parts(live) for b in part["blocks"]]
    return {"document": ident, "url": url(ident), "blocks": len(blocks),
            "anchored": sum(1 for b in blocks if b.get("rangeId")),
            "tabs": len(doc_ir.parts(live)), "notes": limits(source)}


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
    tabs = doc_merge.pair_tabs(base, ours, theirs)
    asked = open_comments(drive, ident)

    if dry_run:
        planned = [{"stamp": None, "label": None, "result": doc_merge.plan(base, ours, theirs)}]
        for tab, mine, was in tabs["pairs"]:
            planned.append({"stamp": tab, "label": mine.get("title", ""),
                            "result": doc_merge.plan(was, mine, doc_ir.tab_part(theirs, tab))})
        for mine in tabs["create"]:
            # What a tab just added reads as: nothing, and the paragraph it keeps.
            planned.append({"stamp": "new", "label": mine.get("title", ""), "result":
                            doc_merge.plan({"blocks": []}, mine, {"blocks": [], "trailer": [1, 2]})})
        for each in planned:
            each["shaped"] = each["result"]["shaped"]
        info = _report(ident, True, ours, tabs, planned, asked)
        info["plan"] = tabs["requests"] + [
            r for each in planned for r in doc_merge.on_tab(
                each["result"]["structure"] + each["result"]["requests"], each["stamp"])]
        info["requests"] = len(info["plan"]) + len(tabs["create"])
        info["report"] = str(write_report(path, info))
        return info

    hook = os.environ.pop("B2S_DOCS_BEFORE_WRITE", None)  # (tests: someone types now)
    if hook:
        subprocess.run(hook, shell=True, check=False)
    written = _write_tabs(drive, docs, ident, path, ours, base, theirs, tabs, doc=doc)
    info = _report(ident, False, ours, tabs, written, asked)
    live = settle(docs, ident, path, ours, base,
                  {each["stamp"]: each["result"]["blocks"] for each in written}, drive)
    info["blocks"] = sum(len(part["blocks"]) for part in doc_ir.parts(live))
    info["report"] = str(write_report(path, info))
    return info


def _report(ident: str, dry_run: bool, ours: dict, tabs: dict, written: list[dict],
            asked: list[str]) -> dict:
    """The report of a sync, every tab in it; what happened past the first tab is
    said with the tab's title in front."""
    info = {"document": ident, "url": url(ident), "dry_run": dry_run, "requests": 0,
            "conflicts": [], "notes": limits(ours) + tabs["notes"],
            "applied": list(tabs["applied"]), "kept": [], "comments": asked}
    for each in written:
        result, label = each["result"], each["label"]
        say = (lambda line, label=label: f"[{label}] {line}") if label is not None else str
        info["requests"] += len(result["requests"])
        info["conflicts"] += [c | {"tab": label} if label is not None else c
                              for c in result["conflicts"]]
        info["notes"] += [say(n) for n in result["notes"]]
        applied, kept = _summary(result)
        info["applied"] += [say(t["note"]) for t in each["shaped"]] + [say(a) for a in applied]
        info["kept"] += [say(k) for k in kept]
        if each.get("attempts"):
            info["replanned"] = max(info.get("replanned", 0), each["attempts"])
    return info


def _write_tabs(drive, docs, ident: str, path: Path, ours: dict, base: dict, theirs: dict,
                tabs: dict, first: bool = True, doc: dict | None = None) -> list[dict]:
    """Write every tab: the tab edits (`doc_merge.pair_tabs`) first, then each tab's
    words, the first tab first. A tab is its own plan and its own batch — its indices
    are its own — so the others wait for nothing it does.

    `doc` is the read the sync began with: tabs are planned against it until something
    has been written, so a reader who typed since is caught by the revision check and
    the tab is planned again, rather than slipping in unnoticed between two reads."""
    if tabs["requests"] or tabs["create"]:
        doc = None
    if tabs["requests"]:
        send(docs, ident, tabs["requests"])
    pairs = list(tabs["pairs"])
    known = {p.get("tab") for p in doc_ir.parts(theirs)} - {None}
    made: dict = {}
    for part in tabs["create"]:
        # One at a time: a child tab needs the id its parent was just given.
        parent = made.get(part.get("parent"), part.get("parent"))
        reply = send(docs, ident, [doc_merge.add_tab_request(
            part | {"parent": parent}, known | set(made.values()))])
        tab = reply["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
        if part.get("tab"):
            made[part["tab"]] = tab
        part["tab"] = tab
        if part.get("parent") in made:
            part["parent"] = made[part["parent"]]
        pairs.append((tab, part, {"blocks": []}))
    stager = Stager(drive, docs, path)
    written = []
    try:
        for tab, mine, was in ([(None, ours, base)] if first else []) + pairs:
            done = _sync_part(docs, ident, path, stager, tab, ours, base, mine, was, doc)
            written.append(done)
            if done["result"]["requests"] or done["shaped"] or done["attempts"]:
                doc = None  # the document has moved on from that read
    finally:
        # The pictures are in the document now, copied: the staging file can go.
        stager.close()
    return written


def _sync_part(docs, ident: str, path: Path, stager: Stager, tab: str | None,
               ours: dict, base: dict, mine: dict, was: dict, doc: dict | None = None) -> dict:
    """Plan one tab against the document and write it.

    `tab` is None for the first tab, whose requests go without a `tabId`; `mine` and
    `was` are the file's and the base's version of the tab, `ours` and `base` the
    whole of them (what a read fills the tab in from). `doc` is a read still current,
    if there is one; otherwise the document is read now.
    """
    if doc is None:
        doc, theirs = read_part(docs, ident, tab, ours, base)
    else:
        theirs = _part_of(doc, tab, ours, base)
    result = doc_merge.plan(was, mine, theirs)
    doc, theirs, was, result, shaped = _write_structure(
        docs, ident, tab, ours, base, mine, was, doc, theirs, result)
    attempt, revision = 0, doc.get("revisionId")
    while True:
        try:
            if result["requests"]:
                send(docs, ident, stager.resolve(doc_merge.on_tab(result["requests"], tab)),
                     revision)
            break
        except HttpError as err:
            attempt += 1
            if not moved_on(err) or attempt >= ATTEMPTS:
                raise
            # Somebody typed between the read and the write. Read again and re-plan:
            # their words are now part of `theirs`, so the merge keeps them.
            print(f"  the document changed while this sync was planned; reading it again "
                  f"({attempt}/{ATTEMPTS - 1})")
            if tab is None:
                mine = read_file(path)  # (the file may have been committed to meanwhile)
            doc, theirs = read_part(docs, ident, tab, ours, base)
            result = doc_merge.plan(was, mine, theirs)
            doc, theirs, was, result, more = _write_structure(
                docs, ident, tab, ours, base, mine, was, doc, theirs, result)
            shaped += more
            revision = doc.get("revisionId")
    return {"stamp": tab, "label": mine.get("title", "") if tab else None,
            "result": result, "shaped": shaped, "attempts": attempt}


def read_part(docs, ident: str, tab: str | None, ours: dict | None,
              base: dict | None) -> tuple[dict, dict]:
    """The document, and the IR of one tab of it (None: the first)."""
    doc = _get(docs, ident)
    return doc, _part_of(doc, tab, ours, base)


def _write_structure(docs, ident: str, tab: str | None, ours: dict, base: dict,
                     mine: dict, was: dict, doc: dict, theirs: dict, result: dict) -> tuple:
    """Write what the grid needs before the words, and plan the words again.

    A table the source added and rows or columns it changed cannot go in the batch
    that writes the text: `insertTable` and its kin move every index below them, and
    the cells they create do not exist until they have been sent. So they go first,
    on their own; the tab is read again; a table that was just built is given the
    file's key and anchored, or the next plan would not recognise it and would build
    it a second time; the base takes those tables as the document now reports them —
    that grid is no longer a difference between the sides — and the words are planned
    against what the document says now.

    Returns the document, the tab's IR, its base, the new plan and what was written,
    in words for the report. A batch that leaves work over (two tables added at one
    index, which one send cannot place) comes round again.
    """
    shaped: list[dict] = []
    for _ in range(ATTEMPTS):
        if not result["structure"]:
            break
        try:
            send(docs, ident, doc_merge.on_tab(result["structure"], tab), doc.get("revisionId"))
        except HttpError as err:
            if not moved_on(err):
                raise
            print("  the document changed while the table edits were planned; reading it again")
            doc, theirs = read_part(docs, ident, tab, ours, base)
            result = doc_merge.plan(was, mine, theirs)
            continue
        shaped += result["shaped"]
        doc, theirs = read_part(docs, ident, tab, ours, base)
        if doc_merge.anchor_tables(theirs, result["shaped"]):
            plant_ranges(docs, ident, theirs, tab)
            doc, theirs = read_part(docs, ident, tab, ours, base)
        was = doc_merge.rebase_tables(was, theirs, result["shaped"])
        result = doc_merge.plan(was, mine, theirs)
    return doc, theirs, was, result, shaped


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
