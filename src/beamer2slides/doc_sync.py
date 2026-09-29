"""Push a canonical HTML file to a Google Doc, and keep the two in step ever after.

The Docs twin of `sync.py`, and the same bargain (docs/google-docs.md): the file in
git is what the source says, the document is what the reader says, and where both
moved the document wins. What is different is written down in `doc_merge`: a chip is
never rewritten, a list's ordered-ness comes from the file because no read can report
it, and a push after the first can never be a re-import — `files.update` destroys
every named range, and the named ranges are what tell the merge which block is which.

Three commands:

    beamer2slides docs push doc.html      creates the document and plants the anchors
    beamer2slides docs sync doc.html      merges both ways and writes
    beamer2slides docs adopt --doc <id>   writes the file a document nobody pushed never had

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
import copy
import hashlib
import io
import json
import mimetypes
import os
import random
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, TypeVar

from . import doc_ir, doc_merge, google_auth
from .doc_ir import U16, Block, Ir, Run
from .gapi import HttpError, is_transient, media_upload, status_of
from .google_auth import (credentials, credentials_for_threads, docs_service, drive_service,
                          shared_service)
from .google_types import (DocsBatchUpdateBody, DocsBatchUpdateResponse, DocsRequest,
                           DocsService, DocsTabProperties, Document, DriveFile, DriveService, FileBody,
                           Request)
from .json_types import Json, JsonObject, JsonShapeError, as_object, as_str
from .typing_compat import assert_never

if TYPE_CHECKING:
    from typing_extensions import Required

_T = TypeVar("_T")

DOC_MIME = "application/vnd.google-apps.document"
JSON_MIME = "application/json"
STATE_DIR = ".b2s"
ATTEMPTS = 3  # how often a write may be re-planned when the document moved under it
DOC_ID = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")
# The document's own `appProperties` key holding the id of its base file in Drive.
BASE_PROPERTY = "b2sBase"
# What the *cache* beside the file remembers of that id, so the next sync can fetch the
# base without asking the document where it is (`load_drive`'s `hint`). The cache only:
# the copy in Drive is read by a checkout that had to look the file up to read it at all.
BASE_FID = "base_fid"
# Above this many requests one `batchUpdate` is cut into several (`send`).
CHUNK = 500

# Where a sync's base came from (`load_base`).
Where = Literal["drive", "local", "none"]


class StoredBase(Ir, total=False):
    """A base as it is stored, in Drive and beside the file: the document's IR as the
    last settle read it, and the two things only the store says - how many syncs it
    has seen (`store_base`) and, in the cache alone, where Drive keeps it (`BASE_FID`)."""
    generation: int
    base_fid: str


class Written(TypedDict):
    """One tab, planned and written (`_sync_part`), or only planned (a dry run)."""
    stamp: str | None               # the tab's id; None for the first tab, "new" for one to add
    label: str | None               # its title in the report; None for the first tab
    result: doc_merge.Plan
    shaped: list[doc_merge.Told]    # what the structural batches did
    attempts: int                   # how often it was planned again after a refused write


class TabConflict(doc_merge.Conflict, total=False):
    tab: str                        # the tab it is on, past the first


class SyncReport(TypedDict, total=False):
    """What `sync` did, the JSON of `.b2s/<stem>.sync-report.json` (`write_report`) and
    what `agent/doc_tools.py` reads."""
    document: Required[str]
    url: Required[str]
    dry_run: Required[bool]
    requests: Required[int]
    conflicts: Required[list[TabConflict]]
    notes: Required[list[str]]
    applied: Required[list[str]]
    kept: Required[list[str]]
    comments: Required[list[str]]
    removed: Required[list[str]]
    replanned: int
    plan: list[DocsRequest]         # a dry run's requests, as they would be sent
    base: Where
    backup: str
    blocks: int
    report: str


class PushReport(TypedDict):
    """What `push` made."""
    document: str
    url: str
    blocks: int
    anchored: int
    tabs: int
    notes: list[str]


class AdoptReport(PushReport):
    """What `adopt` wrote."""
    file: str


def _read(request: Request[_T], tries: int = 4) -> _T:
    """One read, made again through a transient failure.

    A read can be made twice for the price of a round trip and nothing else, so every
    read a sync makes goes through here. An SSL EOF or a 429 in the middle of one ends
    the run otherwise - after the write batch that leaves the document written and the
    file and the base not settled, and in `load_base` it leaves a sync that cannot read
    the base, which is the `--assume-base` dialog where both answers throw work away.

    A *write* never comes here: a batch whose answer was lost may well have been
    applied, and sending it again would apply it twice. What makes a write safe to
    repeat is `send`'s own `requiredRevisionId`, and that is the sync's business, not
    a retry loop's.
    """
    for attempt in range(tries):
        try:
            return request.execute()
        except HttpError as err:
            if not is_transient(err) or attempt == tries - 1:
                raise
        except OSError:      # an SSL EOF or a reset connection, seen on `documents.get`
            if attempt == tries - 1:
                raise
        time.sleep(min(8.0, 2 ** attempt) + random.random())
    raise ValueError(f"a read given {tries} tries is never made")


# ---------------------------------------------------------------- state: Drive first

def state_dir(path: Path) -> Path:
    return path.parent / STATE_DIR


def base_path(path: Path) -> Path:
    """Where the *cache* of the base lives, beside the canonical file.

    The base itself — what both sides agreed on at the end of the last sync, and
    what a three-way merge needs to tell a source change from a document change —
    is stored in Drive (`save_drive`), because that is the only place every
    checkout can see it. `.b2s/` is git-ignorable scratch state: a fresh clone, a
    colleague's machine or a second checkout has none, and before the base went to
    Drive that dropped `sync` into the `--assume-base` dialog, where both answers
    throw somebody's work away.
    """
    return state_dir(path) / f"{path.stem}.base.json"


def base_problem(data: Json, document: str | None) -> str | None:
    """Why `data` cannot be used as the base of `document` (None: it can)."""
    parsed = parse_base(data, document)
    return parsed if isinstance(parsed, str) else None


def parse_base(data: Json, document: str | None) -> StoredBase | str:
    """A base as JSON gave it, parsed (`doc_ir.parse_ir`), or why it is no base of
    `document` (any document's, when that is None): not an object, no blocks, another
    document's, or a value of another shape than its key holds."""
    if not isinstance(data, dict):
        return "not a JSON object"
    if not isinstance(data.get("blocks"), list):
        return "no blocks"
    if document and data.get("document") != document:
        return f"it belongs to document {data.get('document')}"
    try:
        base: StoredBase = {"blocks": doc_ir.parse_blocks(data.get("blocks"), "the base.blocks")}
        doc_ir.read_ir_fields(base, data, "the base")
        if (generation := data.get("generation")) is not None:
            base["generation"] = _whole(generation, "the base.generation")
        if isinstance(fid := data.get(BASE_FID), str):
            base["base_fid"] = fid
    except JsonShapeError as err:
        return str(err)
    return base


def _whole(value: Json, where: str) -> int:
    """A count as JSON wrote it: `int()` took a float or a numeral here before bases
    were parsed, and a base written that way is still somebody's base."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise JsonShapeError(f"{where}: a count was expected, found {value!r}")
    try:
        return int(value)
    except ValueError as err:
        raise JsonShapeError(f"{where}: a count was expected, found {value!r}") from err


def read_local(path: Path, document: str | None) -> tuple[StoredBase | None, str | None]:
    """(the cached base, why not): a missing, truncated or foreign file is no base."""
    file = base_path(path)
    if not file.exists():
        return None, None
    try:
        data: Json = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return None, f"{file} could not be read ({type(err).__name__}: {err})"
    parsed = parse_base(data, document)
    return (None, f"{file}: {parsed}") if isinstance(parsed, str) else (parsed, None)


def load_local(path: Path, document: str) -> StoredBase | None:
    """The cache alone, with no Drive call. `load_base` is what a sync uses."""
    return read_local(path, document)[0]


def save_base(path: Path, base: Mapping[str, object]) -> Path:
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


def base_file_id(drive: DriveService, document: str) -> str | None:
    """The id of the document's base file in Drive, off its own appProperties."""
    try:
        info = _read(drive.files().get(fileId=document, fields="appProperties"))
    except HttpError:
        return None
    return _base_property(info)


def _base_property(info: DriveFile) -> str | None:
    """`appProperties.b2sBase` of a `files.get` answer, where it says one."""
    return info.get("appProperties", {}).get(BASE_PROPERTY) or None


def load_drive(drive: DriveService, document: str, found: dict[str, str] | None = None,
               hint: str | None = None) -> StoredBase | None:
    """The base Drive holds for this document, or None (no base, or unreadable).

    `found` is filled with the base file's id where there is one: the document's
    `appProperties` said so, and `save_drive` would otherwise ask for them again at
    the end of the same run - a round trip for a fact this call already has.

    `hint` is the base file the copy beside this file remembers (`store_base`). The
    document's base file is the same file for the document's life — `save_drive`
    makes a new one only where the old one is gone, and then writes its id into the
    document — so a file that comes back under that id and says it belongs to this
    document *is* the document's base, and the lookup that would have said so is a
    round trip saved at the one end of a sync where nothing can overlap it. A hint
    that does not answer, or answers with another document's base, is no worse than
    none: the lookup happens after all.
    """
    parsed = _load_drive(drive, document, found, hint)
    return None if parsed is None or isinstance(parsed, str) else parsed


def _load_drive(drive: DriveService, document: str, found: dict[str, str] | None,
                hint: str | None) -> StoredBase | str | None:
    """`load_drive`, and why a base Drive holds is none (a file that is not JSON is no
    base at all, as before: None)."""
    if hint:
        said = _read_base_file(drive, hint)
        if said is not None and not isinstance(checked := parse_base(said, document), str):
            if found is not None:
                found["fid"] = hint
            return checked
    try:
        fid = base_file_id(drive, document)
    except HttpError:
        return None
    if not fid:
        return None
    data = _read_base_file(drive, fid)
    if data is None:
        return None
    if found is not None:
        found["fid"] = fid
    # Any document's here: whose it is, `load_base` says out loud.
    return parse_base(data, None)


def _read_base_file(drive: DriveService, fid: str) -> Json | None:
    """The JSON one Drive file holds, or None where it cannot be read as JSON at all."""
    try:
        data = _read(drive.files().get_media(fileId=fid))
        said: Json = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        return said
    except (HttpError, ValueError, OSError):
        return None


def save_drive(drive: DriveService, document: str, base: Mapping[str, object],
               title: str | None = None, known_fid: str | None = None) -> str:
    """The base as a JSON file in the document's own folder, its id in the
    document's `appProperties.b2sBase`. Returns the file id.

    `drive.file` reaches both: the document because this tool created it (or was
    given it), the base file because this tool created it. Nothing here asks for a
    wider scope, and no link is ever made public.

    `known_fid` is the base file this run already found (`load_drive`), which is the
    common case and saves the lookup below - a whole round trip at the end of a sync,
    where there is nothing left to overlap it with. A stale one costs nothing: the
    update is refused, and the lookup happens after all.
    """
    data = json.dumps(base, ensure_ascii=False).encode("utf-8")
    if known_fid:
        try:
            drive.files().update(fileId=known_fid, fields="id", media_body=media_upload(
                io.BytesIO(data), JSON_MIME)).execute()
            return known_fid
        except HttpError:
            pass  # deleted, or somebody else's now: ask the document below
    info = _read(drive.files().get(fileId=document, fields="name,parents,appProperties"))
    fid = _base_property(info)
    if fid:
        try:
            drive.files().update(fileId=fid, fields="id", media_body=media_upload(
                io.BytesIO(data), JSON_MIME)).execute()
        except HttpError:
            fid = None  # deleted, or somebody else's now: a new one is made below
    if not fid:
        name = info.get("name")
        body: FileBody = {"name": f"{title or (name if isinstance(name, str) else document)}"
                                  f" - beamer2slides docs base.json",
                          "mimeType": JSON_MIME, "appProperties": {"b2sBaseOf": document}}
        parents = info.get("parents")
        from .drive_folder import place
        place(body, drive, [p for p in parents if isinstance(p, str)]
              if isinstance(parents, list) else None)
        from .google_types import file_id
        fid = file_id(drive.files().create(body=body, fields="id", media_body=media_upload(
            io.BytesIO(data), JSON_MIME)).execute(), "the base file")
        drive.files().update(fileId=document, fields="id",
                             body={"appProperties": {BASE_PROPERTY: fid}}).execute()
    return fid


def rename_document(drive: DriveService, document: str, name: str,
                    problems: list[str]) -> str | None:
    """Rename the document, which is a Drive call and not a request.

    A Google Doc's title is its name in Drive: `documents.get` reports it and no
    `batchUpdate` request writes it, so the file's `<title>` reaches the document
    only this way (`doc_merge.document_title` decides whether it should). Returns
    the name written, or None — a rename Drive refuses fails nothing and is said out
    loud, as a base it refuses is.
    """
    try:
        drive.files().update(fileId=document, fields="id", body={"name": name}).execute()
        return name
    except HttpError as err:
        problems.append(f"the document could not be renamed {name!r} ({status_of(err)}); "
                        f"it keeps the name it has")
        return None


def stale_base_warning(where: Where, drive: DriveService | None, document: str) -> str | None:
    """The document names a base in Drive that we cannot read (deleted, or owned by
    somebody else) while we sync against the copy beside the file: another checkout
    may have synced this document since, so the copy can be older than the document.
    Nothing is lost when it is — the document wins where both moved, and the changes
    that checkout made read as the document's — but the person should hear about it.
    """
    if where != "local" or drive is None:
        return None
    fid = base_file_id(drive, document)
    if not fid:
        return None
    try:
        _read(drive.files().get_media(fileId=fid))
    except HttpError:
        return (f"the document names a sync base in Drive that cannot be read; syncing against "
                f"the copy in {STATE_DIR}/ beside the file, which may be older than the "
                f"document (docs/google-docs.md, \"The base is Drive-first\")")
    return None


def load_base(path: Path, document: str, drive: DriveService | None = None,
              problems: list[str] | None = None,
              found: dict[str, str] | None = None) -> tuple[StoredBase | None, Where]:
    """(the base, where it came from: `drive`, `local` or `none`).

    Drive is authoritative and the copy beside the file is a cache — except when
    the cache is the newer of the two, which is what a sync whose Drive upload
    failed leaves behind (`generation` counts the syncs). A base that is truncated
    or belongs to another document is not used at all; why goes into `problems`,
    which the caller reports, because a base ignored in silence would make the next
    sync treat every difference as somebody's change.

    `found` is filled with the id of the base file in Drive (`load_drive`), so the
    sync that ends by storing the base again need not ask the document for it twice.
    """
    problems = problems if problems is not None else []
    local, why = read_local(path, document)
    if why:
        problems.append(f"the base beside the file was ignored: {why}")
    # The cache is read first for its `base_fid` alone, which saves Drive's own lookup.
    said = (_load_drive(drive, document, found, local.get("base_fid") if local is not None else None)
            if drive is not None else None)
    remote: StoredBase | None = None
    if isinstance(said, str):
        problems.append(f"the base stored in Drive was ignored: {said}")
    elif said is not None:
        if document and said.get("document") != document:
            problems.append(f"the base stored in Drive was ignored: "
                            f"it belongs to document {said.get('document')}")
        else:
            remote = said
    if remote is not None and local is not None:
        here, there = local.get("generation", 0), remote.get("generation", 0)
        if here > there:
            problems.append(f"the base in Drive is older than the copy beside the file "
                            f"(generation {there} vs {here}): syncing from the copy and "
                            f"storing it in Drive again")
            return local, "local"
        if here < there:
            problems.append(f"the copy of the base beside the file is older than Drive's "
                            f"(generation {here} vs {there}): another checkout has synced "
                            f"this document since, and Drive's base is the one used")
    if remote is not None:
        return remote, "drive"
    if local is not None:
        if warning := stale_base_warning("local", drive, document):
            problems.append(warning)
        else:
            problems.append("the base came from the copy beside the file; Drive has none "
                            "for this document yet, and this sync stores it there")
        return local, "local"
    return None, "none"


def store_base(path: Path, base: Ir, drive: DriveService | None = None,
               document: str | None = None, previous: int = 0,
               base_fid: str | None = None) -> str | None:
    """Store the base where the next sync will look for it: beside the file first
    (atomically), then in Drive, which is where it is looked for first.

    Returns why Drive could not take it (None: it did). A Drive write that fails
    never fails the sync — the cache is still there, and the next run says out loud
    that the base came from it.
    """
    cached, _ = read_local(path, None)
    stamped: dict[str, object] = dict(base)
    # The count only has to rise, and it is what tells a cache another checkout has
    # overtaken from one whose Drive write failed. The cache beside the file counts
    # too: an `--assume-base` run has no base to take the number from.
    stamped["generation"] = max(previous, cached.get("generation", 0) if cached is not None else 0) + 1
    stamped.pop(BASE_FID, None)   # what goes to Drive is the base and nothing of ours
    # The cache keeps what it believed, or the sync would drop the hint it was just given
    # and the run after this one would have to look the base up again.
    known = base_fid or (cached.get("base_fid") if cached is not None else None)
    save_base(path, stamped | ({BASE_FID: known} if known else {}))
    if drive is None:
        return "no Drive service"
    document = document or base.get("document")
    if not document:
        return "the base does not say which document it belongs to"
    try:
        fid = save_drive(drive, document, _without(stamped, "uri"), known_fid=base_fid)
    except (HttpError, OSError) as err:
        return f"{type(err).__name__}: {err}"
    if fid and fid != known:
        # Where the base landed, for the next sync to fetch it without a lookup. Written
        # after the Drive call, because only its answer says where that was; a local write,
        # so a base is never left half-saved by it.
        save_base(path, stamped | {BASE_FID: fid})
    return None


def _without(value: Mapping[str, object], key: str) -> dict[str, object]:
    """`value` with `key` taken out of every object in it, however deep."""
    return {k: _without_in(v, key) for k, v in value.items() if k != key}


def _without_in(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return {str(k): _without_in(v, key) for k, v in value.items() if k != key}
    if isinstance(value, list):
        return [_without_in(v, key) for v in value]
    return value
def document_id(text: str) -> str:
    """The document a URL, an id or a canonical file names."""
    found = DOC_ID.search(text)
    return found.group(1) if found else text.strip()


def url(ident: str) -> str:
    return f"https://docs.google.com/document/d/{ident}/edit"


# ---------------------------------------------------------------- reading both sides

def read_file(path: Path) -> Ir:
    if not path.is_file():
        raise SystemExit(f"{path}: no such file (this command reads the canonical HTML)")
    ir = doc_ir.key_blocks(doc_ir.from_html(path.read_text(encoding="utf-8")))
    if stray := ir.get("stray"):
        raise SystemExit(stray_refusal(path, stray))
    for run in _pictures(ir):
        src = run.get("src", "")
        local = picture_file(path, src)
        if local is None:
            continue
        if local.is_file():
            run["sha"] = digest(local.read_bytes())
        else:
            run["missing"] = True
            ir.setdefault("unsupported", []).append(
                f"<img src={src!r}>: no such file beside {path.name} — "
                f"the picture is left as the document has it")
    return ir


STRAY_SAID = 3          # how many stray passages to quote before counting the rest
STRAY_LONG = 60         # how much of one to quote


def stray_refusal(path: Path, stray: list[str]) -> str:
    """Words in the file that no block carries, and why they stop a sync.

    A block is a `<p>`, an `<h1>`-`<h6>`, an `<li>` or a `<table>`; text outside one is
    read by nothing, so it would reach the document through no request at all - and then
    the settle writes the file again from the document, taking those words out of the
    file too. One mistyped tag (`<it>` for `<li>`, a `<p>` never closed) and somebody's
    sentence is dropped twice over, while the run says "0 requests, the two sides already
    say the same thing" - which is true of everything the reader *could* read. Refusing
    is the only honest answer, and the fix is one character away.
    """
    said = ", ".join(repr(_short(text)) for text in stray[:STRAY_SAID])
    rest = f" and {len(stray) - STRAY_SAID} more" if len(stray) > STRAY_SAID else ""
    return (f"{path.name} says {said}{rest} outside any block, so nothing would carry "
            f"those words into the document.\n"
            f"  Every block is a <p>, <h1>-<h6>, <li> or <table>: look at the tag around "
            f"that text (a mistyped <it> for <li>, a tag left open).\n"
            f"  Nothing was read and nothing written.")


def _short(text: str) -> str:
    return text if len(text) <= STRAY_LONG else text[:STRAY_LONG - 1] + "…"


def _pictures(ir: Ir) -> list[Run]:
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


def embedded(path: Path, ir: Ir) -> Ir:
    """The file as the importer should see it: every picture's bytes inside it.

    Measured: Drive's HTML import embeds a `data:` URI and keeps `alt` and `title` as
    the picture's description and title. A relative `src` would mean nothing to it.
    """
    out = copy.deepcopy(ir)
    for run in _pictures(out):
        local = picture_file(path, run.get("src", ""))
        if local is not None and local.is_file():
            run["src"] = data_uri(local)
    return out


def write_file(path: Path, ir: Ir, document: str) -> None:
    named = ir.copy()
    named["document"] = document
    path.write_text(doc_ir.to_html(named), encoding="utf-8")


def read_document(docs: DocsService, ident: str, *sources: Ir) -> tuple[Document, Ir]:
    """The document, and its IR — every tab — with what a read cannot say filled in.

    Keys come from the named ranges; a list's ordered-ness comes from `sources` — the
    canonical file and the base — because an imported list never reports its own
    (`doc_merge.restore_unreadable`).
    """
    doc = _get(docs, ident)
    ours = sources[0] if sources else None
    base = sources[1] if len(sources) > 1 else None
    return doc, document_ir(doc, ident, ours, base)


def _get(docs: DocsService, ident: str) -> Document:
    return _read(docs.documents().get(documentId=ident, includeTabsContent=True))


def document_ir(doc: Document, ident: str, ours: Ir | None = None,
                base: Ir | None = None) -> Ir:
    """The first tab is the IR; the others go under `tabs` (`doc_ir.parts`), each
    filled in from the file's and the base's tab with the same id."""
    ir = _part_of(doc, None, ours, base)
    extra = [_part_of(doc, tab.get("tabProperties", {}).get("tabId"), ours, base)
             for tab in doc_ir.tabs_of(doc)[1:]]
    if extra:
        ir["tabs"] = extra
    ir["document"] = ident
    return ir


def _part_of(doc: Document, tab: str | None, ours: Ir | None, base: Ir | None) -> Ir:
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
        if props.get("tabId") != (tab or part.get("tab")):
            continue
        if not tab:
            # The first tab keeps the *document's* name as its `title` (that is what the
            # file's `<title>` is); its own tab title is `tab_title`.
            part["tab_title"] = props.get("title", "")
        else:
            part["title"] = props.get("title", "")
            if parent := props.get("parentTabId"):
                part["parent"] = parent
    return part


def open_comments(drive: DriveService, ident: str) -> list[str]:
    """The comments on the document nobody has resolved, for the report.

    A comment is a question somebody asked about a passage, and a sync that rewrites
    that passage answers it by accident — the merge has no idea one is there, because
    a comment lives in Drive and not in the document's content at all. So they are
    read (`drive.file` reaches the documents this tool made) and said out loud.
    Nothing here writes or resolves one: that is the reader's to do, in the browser.
    """
    try:
        found = _read(drive.comments().list(
            fileId=ident, includeDeleted=False, pageSize=100,
            fields="comments(content,resolved,author/displayName,"
                   "quotedFileContent/value,replies/content)")).get("comments", [])
    except HttpError as err:
        return [f"the document's comments could not be read ({status_of(err)})"]
    out: list[str] = []
    for comment in found:
        if comment.get("resolved"):
            continue
        about = (comment.get("quotedFileContent") or {}).get("value", "")
        who = comment.get("author", {}).get("displayName", "somebody")
        replies = len(comment.get("replies", []))
        out.append(f"{who} on {_said(about)[:40]!r}: {comment.get('content', '')[:80]!r}"
                   + (f", and {replies} repl{'y' if replies == 1 else 'ies'}" if replies else ""))
    return out


def _said(value: Json) -> str:
    """A string an answer gave, or nothing where it gave none."""
    return value if isinstance(value, str) else ""


def lent_clients() -> bool:
    """True where the clients this module answers with are somebody else's.

    A service object carries one connection and is not thread-safe, so a client that was
    handed over rather than built here stays on the thread it was handed to. Two ways of
    handing one over: `google_auth.use_services`, and putting one in place of this module's
    own `docs_service` / `drive_service` — which is what the Docs benchmark's world and the
    live tests do, and a fake world driven from two threads is no more thread-safe than a
    real client.
    """
    return (shared_service("docs", "v1") or shared_service("drive", "v3")
                or docs_service is not google_auth.docs_service
                or drive_service is not google_auth.drive_service)


def in_background(fn: Callable[[DocsService, DriveService], _T], name: str) -> Future[_T] | None:
    """`fn(docs, drive)` on a thread of its own with clients of its own, or None where it
    has to be run here (`lent_clients`).

    `sync.Sync.in_background`'s rule, one API over: a worker builds its own clients from
    credentials resolved *here*, a thread inheriting no context (`google_auth._Hook`).
    Every future this returns is collected by its caller; the pool is left to the
    interpreter.
    """
    if lent_clients():
        return None
    creds = credentials_for_threads()   # resolved here: a worker inherits no context
    pool = ThreadPoolExecutor(1, thread_name_prefix=name)
    try:
        return pool.submit(lambda: fn(docs_service(creds), drive_service(creds)))
    finally:
        pool.shutdown(wait=False)


def collect(future: Future[_T] | None, otherwise: Callable[[], _T]) -> _T:
    """What a background read answered, or what it answers when there was no thread."""
    return future.result() if future is not None else otherwise()


def limits(ours: Ir) -> list[str]:
    """What this sync cannot carry, said out loud rather than dropped in silence."""
    return list(ours.get("unsupported", []))


def unmodelled_notes(doc: Document, full: bool = False) -> list[str]:
    """What the live document carries that the dialect never reads.

    `doc_ir.unmodelled` walks the raw `documents.get` answer against the reader's own
    map, and this says what it found. It is the other half of the convergence check:
    "a second sync writes 0 requests" is measured on the IR, so it proves the IR
    round-trips and says nothing at all about a property the IR never looked at. Such
    a property survives an ordinary edit — a request names the fields it writes, and
    a block is restyled, not rebuilt — and goes when the block holding it is written
    again from nothing.

    `full` names every one, and then names the blocks that carry them
    (`block_risk_notes`), which is what `adopt` owes whoever hands us a document
    somebody else wrote. A sync says the count and the commonest few instead, or
    every report would carry fifteen lines that never change.
    """
    found = doc_ir.unmodelled(doc)
    if not found:
        return []
    order = sorted(found, key=lambda path: (-found[path]["count"], path))
    if full:
        return [f"the document has {found[path]['count']} × {path} "
                f"(e.g. {found[path]['example']}), which the canonical file cannot say"
                for path in order] + block_risk_notes(doc)
    rest = f" and {len(order) - 3} more" if len(order) > 3 else ""
    # The one number a sync can act on: not how many kinds there are but how many
    # blocks a rewrite would cost something. Naming them is `adopt`'s job, once.
    risky = len(doc_ir.unread_blocks(doc))
    carried = f"{risky} block(s) carry one, and " if risky else ""
    return [f"{len(order)} kinds of document property this file cannot say "
            f"({', '.join(order[:3])}{rest}); {carried}they survive an edit and go "
            f"with a block written again from nothing"]


RISKY_BLOCKS = 8        # how many to name before saying how many more there are
PROPERTIES = 4          # how many of one block's properties to name on its line
#: The tail of a `rewrite_losses` line. A report's notes are prose, and this is the
#: one note that is a loss rather than a caution, so a reader of the notes — the
#: agent journey above all — needs to be able to tell it from the rest. Named here,
#: where the line is written, rather than guessed at by whoever reads it.
LOSS_MARK = "in nothing the file can say"


def block_risk_notes(doc: Document, limit: int = RISKY_BLOCKS) -> list[str]:
    """Which blocks would lose something if the source rewrote them.

    The counts above are the document's; these are addresses. A property the dialect
    never reads is not carried by the file, so it survives every ordinary edit — a
    request names the fields it writes — and goes the moment the block holding it is
    written again from nothing. Whoever is about to edit an adopted document through
    its file is entitled to know *which* paragraph that is, in the words they can see
    in the document, and to leave that one alone.
    """
    risky = doc_ir.unread_blocks(doc)
    out: list[str] = []
    for block in risky[:limit]:
        # The last two segments of the path: the struct and the field, which is what
        # names the property. The nodes above them say where the walker was, not what
        # the person lost, and a whole path per property makes a line nobody finishes.
        named = [".".join(path.split(".")[-2:])
                 + (f" ×{entry['count']}" if entry["count"] > 1 else "")
                 for path, entry in block["unread"].items()]
        what = ", ".join(named[:PROPERTIES]) + (
            f" and {len(named) - PROPERTIES} more" if len(named) > PROPERTIES else "")
        where = f"[{block['tab']}] " if block["tab"] else ""
        words = f"{block['words'][:48]!r}" if block["words"] else f"at {block['span'][0]}"
        out.append(f"{where}the {block['kind']} {words} carries {what}; rewriting that "
                   f"block through the file would drop {'it' if len(named) == 1 else 'them'}")
    if len(risky) > limit:
        out.append(f"and {len(risky) - limit} more blocks carry something the file "
                   f"cannot say (`doc_ir.unread_blocks` names them all)")
    return out


def rewrite_losses(doc: Document, planned: Sequence[Written]) -> list[str]:
    """What this sync actually costs, as opposed to what it risks.

    `block_risk_notes` names every block that carries something the dialect never
    read, which is what somebody deciding *how* to edit a document needs. This is the
    other end of the same question, asked once the plan exists: of those blocks, which
    is this run about to write again from nothing? Nearly always none, and then the
    report says nothing at all; when it is one, it is the one line in the report that
    is a loss rather than a caution, and it says so before the write, not after.

    A block merely **restyled** is safe and is left out: every request names the
    fields it writes, and no field the merge owns is a field nobody reads. A block
    **rewritten** (the file's runs go in where the document's were) or **moved** (a
    move is a delete and a write, and the write says only what the file says) is not.
    A block the source **deleted** is left out too — its words are going on purpose,
    and a property going with them is not news.
    """
    risky = {(block["tab"], tuple(block["span"])): block
             for block in doc_ir.unread_blocks(doc)}
    if not risky:
        return []
    out: list[str] = []
    for each in planned:
        for block in each["result"].get("blocks", []):
            if not (span := block.get("span")) or not (block.get("rewrite") or block.get("moved")):
                continue
            hit = risky.get((each["stamp"], tuple(span)))
            if hit is None:
                continue
            named = ", ".join(".".join(path.split(".")[-2:]) for path in hit["unread"])
            why = "moved" if block.get("moved") else "rewritten from the file"
            where = f"[{each['label']}] " if each.get("label") else ""
            words = f"{hit['words'][:48]!r}" if hit["words"] else f"at {hit['span'][0]}"
            out.append(f"{where}the {hit['kind']} {words} is being {why}, which drops "
                       f"{named} — the document's, and {LOSS_MARK}")
    return out


def stamp_of(ir: Ir, part: Ir) -> str | None:
    """The `tabId` a tab's requests carry: none for the first tab, which is where a
    request without one goes (`doc_merge.on_tab`)."""
    return None if part is ir else part.get("tab")


# ---------------------------------------------------------------- writing

def send(docs: DocsService, ident: str, requests: Sequence[DocsRequest],
         revision: str | None = None,
         notes: list[str] | None = None) -> DocsBatchUpdateResponse:
    """One `batchUpdate`, or — past `CHUNK` requests — several, in order.

    Docs applies a batch's requests in the order they are given, and the plan is
    already in that order, so cutting it into consecutive batches writes the same
    document as one batch would: nothing is reordered and no request crosses a
    boundary. What it is *not* is atomic. A single batch either lands whole or
    lands not at all; a chunked write that fails on its third batch leaves the
    first two in the document. That is the price of not having one oversized batch
    refused whole, and it is said out loud (`notes`) whenever it happened.

    `requiredRevisionId` can only guard the first batch — after it the document has
    a new revision, which is ours. The answer carries it (`writeControl`), so each
    batch requires the revision the one before it produced: somebody typing
    half-way through the run is still refused, rather than writing over their words.
    Unmeasured against the live API: whether `BatchUpdateDocumentResponse` always
    answers with a `writeControl`. If it does not, the chain simply stops guarding
    (the later batches go unrequired), which is what the single-batch path did all
    along; no batch is ever sent with a revision that is not the previous answer's.
    """
    if len(requests) <= CHUNK:
        return _batch(docs, ident, requests, revision)
    cuts = [requests[at:at + CHUNK] for at in range(0, len(requests), CHUNK)]
    if notes is not None:
        notes.append(f"{len(requests)} requests were written in {len(cuts)} batches of up to "
                     f"{CHUNK}: unlike one batch, a run that fails part-way leaves the "
                     f"batches before it in the document")
    replies: list[JsonObject] = []
    answer: DocsBatchUpdateResponse = {}
    for cut in cuts:
        answer = _batch(docs, ident, cut, revision)
        replies += answer.get("replies", [])
        revision = (answer.get("writeControl") or {}).get("requiredRevisionId")
    whole = answer.copy()
    whole["replies"] = replies
    return whole


def _batch(docs: DocsService, ident: str, requests: Sequence[DocsRequest],
           revision: str | None) -> DocsBatchUpdateResponse:
    body: DocsBatchUpdateBody = {"requests": requests}
    if revision:
        # The plan is indices into the document as it was read. Anyone who typed since
        # has moved them, so the write is refused rather than landing in the wrong place.
        body["writeControl"] = {"requiredRevisionId": revision}
    return docs.documents().batchUpdate(documentId=ident, body=body).execute() or {}


def moved_on(error: HttpError) -> bool:
    """Whether a refused write means the document changed under the plan."""
    return status_of(error) in (400, 409) and "revision" in str(error).lower()


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

    def __init__(self, drive: DriveService, docs: DocsService, path: Path) -> None:
        self.drive, self.docs, self.path = drive, docs, path
        self.urls: dict[str, str] = {}
        self.files: list[str] = []

    def resolve(self, requests: Sequence[DocsRequest]) -> list[DocsRequest]:
        """The requests with every staged picture's URL filled in."""
        wanted = [image["uri"][len(doc_merge.STAGE):] for r in requests
                  if (image := r.get("insertInlineImage"))
                  and image["uri"].startswith(doc_merge.STAGE)]
        needed = sorted(set(wanted) - self.urls.keys())
        if needed:
            self._stage(needed)
        out: list[DocsRequest] = []
        for request in requests:
            image = request.get("insertInlineImage")
            if image and image["uri"].startswith(doc_merge.STAGE):
                staged = image.copy()
                staged["uri"] = self.urls[image["uri"][len(doc_merge.STAGE):]]
                fixed: DocsRequest = {"insertInlineImage": staged}
                out.append(fixed)
                continue
            out.append(request)
        return out

    def _stage(self, sources: list[str]) -> None:
        # Numbered paragraphs, so a picture the import could not take is missed by
        # name rather than shifting every URL after it onto the wrong picture.
        body = "".join(f'<p>{n}:<img src="{data_uri(self.path.parent / src)}"></p>'
                       for n, src in enumerate(sources))
        from .drive_folder import place
        from .google_types import file_id
        ident = file_id(self.drive.files().create(
            body=place({"name": self.NAME, "mimeType": DOC_MIME,
                        "appProperties": {"b2sStaging": "docs"}}, self.drive),
            media_body=media_upload(io.BytesIO(f"<html><body>{body}</body></html>".encode()),
                                    "text/html"), fields="id").execute(), "the staging document")
        self.files.append(ident)
        staged = doc_ir.from_document(_get(self.docs, ident), None)
        for block in staged["blocks"]:
            runs = block.get("runs", [])
            label = doc_ir.runs_text(runs).split(":")[0]
            uris = [r.get("uri") for r in runs if r.get("chip") == "image"]
            if label.isdigit() and int(label) < len(sources) and uris and (uri := uris[0]):
                self.urls[sources[int(label)]] = uri
        missing = [s for s in sources if s not in self.urls]
        if missing:
            raise RuntimeError(f"the staging document brought no picture for {missing[:3]}")

    def close(self) -> None:
        for ident in self.files:
            try:
                self.drive.files().delete(fileId=ident).execute()
            except HttpError as err:
                print(f"  the staging document {ident} could not be deleted ({status_of(err)})")
        self.files = []


def fetch_pictures(path: Path, live: Ir, drive: DriveService | None = None,
                   ident: str | None = None) -> int:
    """Put the pictures a reader inserted into the document beside the canonical file.

    Their `contentUri` lasts about half an hour and names nothing of ours, so a file
    that pointed there would be broken by tomorrow. Each one is saved once, under its
    object id, in `<stem>.media/`, and from then on the file carries it like any other
    picture — which is what makes a reader's picture something git can keep.

    A `contentUri` is a googleusercontent URL, which a caller may have no way to fetch
    (downloads switched off, a harness with no public network). With `drive`, what no
    download brought comes out of the document's own zip export (`exported_pictures`).
    """
    from . import net

    wanted = [(value, uri, run) for run in _pictures(live)
              if not run.get("src") and (uri := run.get("uri")) and (value := run.get("value"))]
    got: dict[str, bytes] = {}
    failed: dict[str, object] = {}
    for value, uri, _ in wanted:
        if net.downloads_off():
            failed[value] = "downloads are switched off"
            continue
        try:
            got[value] = net.download(uri)   # through the caller's fetcher
        except Exception as err:  # noqa: BLE001 - a harness's fetcher raises its own types
            failed[value] = err
    if failed and drive is not None and ident:
        exported = exported_pictures(drive, ident, _pictures(live))
        got |= {oid: exported[oid] for oid in failed if oid in exported}
    done = 0
    for value, _, run in wanted:
        data = got.get(value)
        if data is None:
            print(f"  the picture {value} could not be fetched: {failed.get(value)}")
            continue
        # A fetcher hands over bytes and nothing else, so the picture names its own type.
        suffix = net.picture_suffix(data)
        folder = path.parent / f"{path.stem}.media"
        folder.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", value) + suffix
        (folder / name).write_bytes(data)
        run["src"], run["sha"] = f"{folder.name}/{name}", digest(data)
        done += 1
    return done


# An exported `<img>` and a run are the same picture when their sizes agree this well
# (CSS px: the export writes 13.33px for a run the IR rounds to 13).
EXPORT_SIZE_SLACK = 1.5


def exported_pictures(drive: DriveService, ident: str, runs: Sequence[Run]) -> dict[str, bytes]:
    """The pictures of `runs` (every picture run of the document, in document order) as
    Drive's zip export of the document carries them, by object id.

    The export is one HTML file of every tab and an `images/` folder; its `<img>` tags
    come in document order, one per picture (a picture used twice may share one file).
    Nothing in them names an object, so they are paired by order, and only when the
    count and every size agree: a mismatch saves nothing rather than the wrong picture.
    Measured live: tabs in order, a picture inserted later sits where it stands.
    """
    import zipfile

    try:
        data = _read(drive.files().export(fileId=ident, mimeType="application/zip"))
    except HttpError as err:
        print(f"  no pictures from the document's export: it was refused ({status_of(err)})")
        return {}
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        page = next(n for n in archive.namelist() if n.endswith(".html"))
        html = archive.read(page).decode("utf-8")
    except (zipfile.BadZipFile, StopIteration, UnicodeDecodeError) as err:
        print(f"  no pictures from the document's export: it could not be read ({err})")
        return {}
    tags = re.findall(r"<img\b[^>]*>", html)
    if len(tags) != len(runs):
        print(f"  no pictures from the document's export: it holds {len(tags)} and the "
              f"document {len(runs)}, so they cannot be paired")
        return {}
    folder = page.rpartition("/")[0]
    out: dict[str, bytes] = {}
    for tag, run in zip(tags, runs):
        src = re.search(r'\bsrc="([^"]+)"', tag)
        size = [re.search(rf"\b{side}:\s*([\d.]+)px", tag) for side in ("width", "height")]
        said = [float(m.group(1)) for m in size if m is not None]
        if (want := run.get("size")) and len(said) == len(size) and any(
                abs(side - wanted) > EXPORT_SIZE_SLACK for side, wanted in zip(said, want)):
            print(f"  no pictures from the document's export: the picture {run.get('value')} "
                  f"is not the size its place in the export says")
            return {}
        name = f"{folder}/{src.group(1)}" if src and folder else (src.group(1) if src else None)
        if (value := run.get("value")) and name in archive.namelist():
            out[value] = archive.read(name)
    return out


def plant_ranges(docs: DocsService, ident: str, ir: Ir, tab: str | None) -> int:
    """Name every keyed block of one tab the document does not name yet.

    One batch, and on a refusal one request at a time, so a range the API will not
    take names itself in the output instead of costing the rest their anchors.

    The blocks take what the batch answered (`adopt_replies`), so `settle` need not
    read the document again to learn where the anchors it has just planted are.
    """
    requests = doc_merge.on_tab(doc_ir.name_requests(ir), tab)
    if not requests:
        return 0
    try:
        adopt_replies(ir, requests, send(docs, ident, requests))
        return len(requests)
    except HttpError as err:
        print(f"  the batch of {len(requests)} named ranges was refused ({status_of(err)}); "
              f"trying them one at a time")
    done = 0
    for request in requests:
        try:
            adopt_replies(ir, [request], send(docs, ident, [request]))
            done += 1
        except HttpError as err:
            print(f"  no anchor for {_range_named(request)}: {status_of(err)}")
    return done


def _range_named(request: DocsRequest) -> str | None:
    """The named range a `createNamedRange` or `deleteNamedRange` is about."""
    if (made := request.get("createNamedRange")) is not None:
        return made["name"]
    if (gone := request.get("deleteNamedRange")) is not None:
        return gone.get("name") or gone.get("namedRangeId")
    return None


def adopt_replies(ir: Ir, requests: Sequence[DocsRequest], answer: DocsBatchUpdateResponse) -> int:
    """Give each block the id of the named range just planted on it.

    A `createNamedRange` answers with the id of the range it made, and the request
    says which block that was for - its name *is* the block's key - and the span it
    went on. So the read that would learn those two things is a read this batch has
    already paid for: the last round trip of a sync, with nothing left to overlap it
    with (the Slides tail's lesson, docs/project-notes.md "the **tail**"). A range this batch
    deleted is gone from the document, so the orphans it names are no longer orphans.

    Only the ids move. Everything else in `ir` came from the read this batch was
    planned against, and a named range moves no text, so nothing else can have
    changed - which is what `test_the_anchors_a_batch_answers_with_are_what_a_read_
    would_say` proves against the live API.
    """
    replies = answer.get("replies") or []
    by_key = {key: b for b in ir["blocks"] if (key := b.get("key"))}
    done = 0
    for n, request in enumerate(requests):
        ask = request.get("createNamedRange")
        made = (replies[n] if n < len(replies) else {}) or {}
        created = made.get("createNamedRange")
        planted = created.get("namedRangeId") if isinstance(created, dict) else None
        if not ask or not planted or not isinstance(planted, str):
            continue
        block = by_key.get(ask["name"][len(doc_ir.KEY_PREFIX):])
        if block is None:
            continue
        block["rangeId"] = planted
        block["range"] = [U16(ask["range"]["startIndex"]), U16(ask["range"]["endIndex"])]
        done += 1
    if any(r.get("deleteNamedRange") for r in requests):
        ir.pop("orphans", None)
    return done


def settle(docs: DocsService, ident: str, path: Path, ours: Ir | None, base: Ir | None,
           planned: Mapping[str | None, list[Block]] | None = None,
           drive: DriveService | None = None, problems: list[str] | None = None,
           name_unmodelled: bool = False, renamed: str | None = None,
           base_fid: str | None = None, read: tuple[Document, Ir] | None = None) -> Ir:
    """After a write: read the document, anchor what is new, and let that read be both
    the new base and the new canonical file. File, document and base agree from here.

    `planned` is what each tab was written as, by its stamp (None: the first tab).
    With `drive`, the equations get their LaTeX (`equation_latex`) and the base goes
    to Drive as well as to the cache beside the file; a Drive write that fails is
    said out loud (`problems`) and fails nothing. So is what the document carries and
    the dialect does not (`unmodelled_notes`); `name_unmodelled` names every one of
    those, which is what `adopt` and `push` want and a sync does not. `renamed` is a
    name Drive has just been given for the document, which `documents.get` need not
    have caught up with — the file and the base must say the name that was written,
    or the next read would put the old one back and the rename would be undone.

    `read` is a read of the document still current, for a caller that has just made
    one and written nothing since (`adopt`): the document cannot have moved, so
    reading it twice in a row is a round trip for nothing. `base_fid` is the base
    file in Drive this run already found (`load_base`)."""
    planned = planned or {}
    doc, live = read if read is not None else _read_both(docs, ident, ours, base)
    for line in unmodelled_notes(doc, name_unmodelled):
        print(f"  {line}")
        if problems is not None:
            problems.append(line)
    tidy: list[DocsRequest] = []
    named = 0
    doc_merge.settle_keys(live, planned, base)
    for part in doc_ir.parts(live):
        tidy += doc_merge.on_tab(doc_merge.tidy_requests(part), stamp_of(live, part))
    if tidy:
        send(docs, ident, tidy)
    for part in doc_ir.parts(live):
        named += plant_ranges(docs, ident, part, stamp_of(live, part))
    if tidy:
        # What the anchors did, the batch itself answered (`adopt_replies`); what
        # `tidy_requests` did - a named style, a bullet, a list's glyph - it did not,
        # and the block it left behind is what the file and the base must say.
        doc, live = _read_both(docs, ident, ours, base)
    for part in doc_ir.parts(live):
        if blocks := planned.get(stamp_of(live, part)):
            doc_merge.place_pictures(part, blocks)
    if drive is not None:
        equation_latex(drive, ident, doc, live)
    if renamed:
        live["title"] = renamed
    fetch_pictures(path, live, drive, ident)
    write_file(path, live, ident)
    refused = store_base(path, live, drive, ident, _generation(base), base_fid=base_fid)
    if refused and drive is not None:
        line = (f"the base could not be stored in Drive ({refused}); the copy in {STATE_DIR}/ "
                f"beside the file is the only one, so another checkout has no base to sync from")
        print(f"  {line}")
        if problems is not None:
            problems.append(line)
    return live


def _read_both(docs: DocsService, ident: str, ours: Ir | None,
               base: Ir | None) -> tuple[Document, Ir]:
    """`read_document` with the file and the base, either of which may be missing."""
    doc = _get(docs, ident)
    return doc, document_ir(doc, ident, ours, base)


def _generation(base: Mapping[str, object] | None) -> int:
    """How many syncs `base` has seen, where it is a base that was stored."""
    if base is None:
        return 0
    said = base.get("generation", 0)
    return int(said) if isinstance(said, (int, float, str)) else 0


def equation_latex(drive: DriveService, ident: str, doc: Document, live: Ir) -> int:
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
        markdown = _read(drive.files().export(fileId=ident, mimeType="text/markdown"))
    except HttpError as err:
        print(f"  no LaTeX for the equations: the Markdown export was refused ({status_of(err)})")
        return 0
    text = markdown.decode("utf-8") if isinstance(markdown, bytes) else str(markdown)
    return doc_ir.attach_latex(live, doc_ir.latex_of(spots, text))


# ---------------------------------------------------------------- the report

BASE_FROM: dict[Where, str] = {
    "drive": "the base file in Drive, which every checkout sees",
    "local": f"the cache in {STATE_DIR}/ beside the file (Drive has none, or "
             f"could not be read)",
    "none": "none — there was no base, and --assume-base decided"}


def write_report(path: Path, info: SyncReport) -> Path:
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
    if base := info.get("base"):
        lines += [f"Base: {BASE_FROM.get(base, base)}", ""]
    if backup := info.get("backup"):
        lines += [f"The document was exported to `{backup}` before being written over "
                  f"(`--assume-base source-wins` has no base to merge against).", ""]
    for title, items in ((f"Deleted from the document{'' if info['dry_run'] else ' (no way back)'}",
                          info.get("removed", [])),
                         ("Conflicts (the document won)",
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


def _words(block: Block) -> str:
    """The block, short enough to read in a report. A table has no words of its own,
    so its cells stand in for it."""
    if block.get("kind") == "table":
        return " | ".join(doc_merge.block_text(inner) for row in block.get("rows", [])
                          for cell in row for inner in cell)[:60]
    return doc_merge.block_text(block)[:60]


def _summary(result: doc_merge.Plan) -> tuple[list[str], list[str], list[str]]:
    applied: list[str] = []
    kept: list[str] = []
    gone: list[str] = []
    for block in result.get("removed", []):
        # Its own list and its own heading, above everything else the report says: it
        # is the one change here that no second sync can bring back
        # (`doc_merge.deleted_blocks`).
        gone.append(f"`{block.get('key', '(unkeyed)')}`: {_words(block)!r}")
    for block in result["blocks"]:
        key = block.get("key", "(unkeyed)")
        words = _words(block)
        if block.get("moved"):
            applied.append(f"`{key}` moved to where the source has it: {words!r}")
            continue
        match block.get("origin"):
            case "added by the source":
                applied.append(f"`{key}` added: {words!r}")
            case "merged":
                applied.append(f"`{key}` rewritten: {words!r}")
            case "added in the document" | "unknown to the base":
                kept.append(f"`{key}` is the document's own: {words!r}")
            case "kept from the document":
                kept.append(f"`{key}` says what the document says: {words!r}")
            case "kept over a source delete":
                kept.append(f"`{key}` was deleted in the source but edited here: {words!r}")
            case "frozen content differs" | "table grid differs" | "the grid the source has" | None:
                pass
            case unexpected:
                assert_never(unexpected)
    return applied, kept, gone


# ---------------------------------------------------------------- commands

def push(path: Path, name: str | None = None, new_doc: bool = False) -> PushReport:
    """Create the document from the canonical file and plant one anchor per block."""
    source = read_file(path)
    if (named := source.get("document")) and not new_doc:
        raise SystemExit(f"{path} already names document {named}\n"
                         f"  {url(named)}\n"
                         f"  Use `docs sync` to write to it, or --new-doc for a second one.")
    creds = credentials()
    drive, docs = drive_service(creds), docs_service(creds)
    first = embedded(path, source)
    first.pop("document", None)
    first.pop("tabs", None)  # the importer makes one tab of whatever it is given
    html = doc_ir.to_html(first)
    from .drive_folder import place
    from .google_types import file_id
    ident = file_id(drive.files().create(
        body=place({"name": name or source.get("title") or path.stem, "mimeType": DOC_MIME}, drive),
        media_body=media_upload(io.BytesIO(html.encode("utf-8")), "text/html"),
        fields="id").execute(), f"the document imported from {path.name}")

    doc, live = read_document(docs, ident)
    # The importer builds what HTML can say; give what came back the file's keys, and
    # the ordered-ness the import threw away.
    doc_merge.inherit_keys(source, live)
    doc_merge.restore_unreadable(live, source)
    plant_ranges(docs, ident, live, None)
    # The other tabs are written the way a sync writes a tab the source added.
    tabs = doc_merge.pair_tabs({"blocks": []}, source, live)
    written = _write_tabs(drive, docs, ident, path, source, {"blocks": []}, live, tabs,
                          False, None, None)
    planned: dict[str | None, list[Block]] = {None: source["blocks"]}
    planned.update({w["stamp"]: w["result"]["blocks"] for w in written})
    notes = limits(source)
    live = settle(docs, ident, path, source, source, planned, drive, notes, True)
    blocks = [b for part in doc_ir.parts(live) for b in part["blocks"]]
    return {"document": ident, "url": url(ident), "blocks": len(blocks),
            "anchored": sum(1 for b in blocks if b.get("rangeId")),
            "tabs": len(doc_ir.parts(live)), "notes": notes}


def adopt(document: str, path: Path | None = None, force: bool = False,
          folder: Path | None = None) -> AdoptReport:
    """Write the canonical file a document nobody pushed never had.

    The Docs twin of `beamer2slides adopt`. `push` goes file → document and refuses
    a file that already names one; there was no way the other way round, and a team
    that has been writing a Google Doc for a year has nothing else to offer. The
    machinery was all there — `settle` reads a document, keys its blocks, plants one
    named range each, writes the file and stores the base — it was simply not
    reachable from the command line.

    Idempotent: a second run finds every block already named by its range, keys
    nothing new, plants nothing and writes the same file. A document Drive's HTML
    importer built cannot say whether its lists are numbered (`doc_ir._ordered`) and
    there is no file yet to say for it, so `settle` gives those lists bullets of the
    document's own — from then on they read back as what they are.

    `folder` is where a `path` of None lands. The name comes from the document's title,
    which is only known once it has been read, so without this the file resolves against
    whatever folder the process happens to be in — right for the command line, where that
    is the folder the person typed in, and wrong for a caller that has one workspace the
    file must stay inside (`agent/doc_tools.py`).
    """
    ident = document_id(document)
    creds = credentials()
    docs, drive = docs_service(creds), drive_service(creds)
    doc, live = read_document(docs, ident)
    if path is None:
        path = Path(folder or ".") / f"{doc_ir.slug(doc.get('title') or ident)}.html"
    if path.exists():
        named = doc_ir.from_html(path.read_text(encoding="utf-8")).get("document")
        if named != ident and not force:
            raise SystemExit(
                f"{path} is already there and " +
                (f"names document {named}\n  {url(named)}\n"
                 if named else "names no document\n") +
                f"  Adopting {ident} would write the document's words over it.\n"
                f"  Give another path, or --force to overwrite this one.")
    path.parent.mkdir(parents=True, exist_ok=True)
    notes = limits(live)
    # Nothing has been written since that read, so the document cannot have moved:
    # `settle` reading it again would be a round trip for the same answer.
    live = settle(docs, ident, path, None, None, None, drive, notes, True, read=(doc, live))
    blocks = [b for part in doc_ir.parts(live) for b in part["blocks"]]
    return {"document": ident, "url": url(ident), "file": str(path), "blocks": len(blocks),
            "anchored": sum(1 for b in blocks if b.get("rangeId")),
            "tabs": len(doc_ir.parts(live)), "notes": notes}


def sync(path: Path, document: str | None = None, dry_run: bool = False,
         assume_base: str | None = None, backup: bool = True) -> SyncReport:
    """Merge the file and the document three ways, write, and rewrite the file."""
    ours = read_file(path)
    named = document_id(document) if document else ours.get("document")
    if not named:
        raise SystemExit(f"{path} does not say which document it belongs to.\n"
                         f"  Pass --doc <url or id>, or `docs push {path.name}` to make one.")
    ident: str = named
    creds = credentials()
    docs, drive = docs_service(creds), drive_service(creds)
    troubles: list[str] = []
    found: dict[str, str] = {}
    # The three reads a sync opens with need nothing of each other: the base, the
    # document, and the comments — which nothing before the report wants at all, so that
    # one is waited for after the write rather than here. The base stays on this thread,
    # being the one that reads a file and appends to `troubles`.
    reading = in_background(lambda d, _: _get(d, ident), "b2s-doc")
    asking = in_background(lambda _, d: open_comments(d, ident), "b2s-comments")
    stored, where = load_base(path, ident, drive, troubles, found)
    doc = collect(reading, lambda: _get(docs, ident))
    for trouble in troubles:
        print(f"  {trouble}")
    theirs = document_ir(doc, ident, ours, stored or {"blocks": []})
    kept: Path | None = None
    base: Ir
    if stored is None:
        base, kept = _no_base(path, ours, theirs, assume_base, drive, ident, backup,
                              dry_run)
        if kept:
            print(f"  the document was exported to {kept} before being written over")
        elif dry_run and backup and assume_mode(assume_base) == "source-wins":
            print("  a real run would export the document to "
                  f"{STATE_DIR}/backups/ before writing over it")
    else:
        base = stored
    tabs = doc_merge.pair_tabs(base, ours, theirs)

    if dry_run:
        planned = [_dry(None, None, doc_merge.plan(base, ours, theirs))]
        for tab, mine, was in tabs["pairs"]:
            planned.append(_dry(tab, mine.get("title", ""),
                                doc_merge.plan(was, mine, doc_ir.tab_part(theirs, tab) or {"blocks": []})))
        for mine in tabs["create"]:
            # What a tab just added reads as: nothing, and the paragraph it keeps.
            planned.append(_dry("new", mine.get("title", ""), doc_merge.plan(
                {"blocks": []}, mine, {"blocks": [], "trailer": [U16(1), U16(2)]})))
        info = _report(ident, True, ours, tabs, planned,
                       collect(asking, lambda: open_comments(drive, ident)))
        info["plan"] = tabs["requests"] + [
            r for each in planned for r in doc_merge.on_tab(
                each["result"]["structure"] + each["result"]["requests"], each["stamp"])]
        info["requests"] = len(info["plan"]) + len(tabs["create"])
        info["base"] = where
        # A dry run never reaches `settle`, which is where this is normally said —
        # and it is the run where being told what a write would cost is worth most.
        info["notes"] = (troubles + info["notes"] + unmodelled_notes(doc)
                         + rewrite_losses(doc, planned))
        info["report"] = str(write_report(path, info))
        return info

    hook = os.environ.pop("B2S_DOCS_BEFORE_WRITE", None)  # (tests: someone types now)
    if hook:
        subprocess.run(hook, shell=True, check=False)
    batched: list[str] = []
    written = _write_tabs(drive, docs, ident, path, ours, base, theirs, tabs, True, doc,
                          batched)
    renamed = (rename_document(drive, ident, rename, batched)
               if (rename := tabs["rename"]) else None)
    # The comments are the report's, and nothing before this point wanted them: the read
    # has been running beside the planning and the write it is only now waited for.
    info = _report(ident, False, ours, tabs, written,
                   collect(asking, lambda: open_comments(drive, ident)))
    info["base"] = where
    if kept:
        info["backup"] = str(kept)
    info["notes"] = troubles + info["notes"] + batched + rewrite_losses(doc, written)
    live = settle(docs, ident, path, ours, base,
                  {each["stamp"]: each["result"]["blocks"] for each in written}, drive,
                  info["notes"], renamed=renamed, base_fid=found.get("fid"))
    info["blocks"] = sum(len(part["blocks"]) for part in doc_ir.parts(live))
    info["report"] = str(write_report(path, info))
    return info


def _dry(stamp: str | None, label: str | None, result: doc_merge.Plan) -> Written:
    """A tab a dry run planned and did not write."""
    return {"stamp": stamp, "label": label, "result": result, "shaped": result["shaped"],
            "attempts": 0}


def _said_in(label: str | None, line: str) -> str:
    """A line of the report, with the tab it is about in front past the first tab."""
    return f"[{label}] {line}" if label is not None else line


def _in_tab(conflict: doc_merge.Conflict, label: str | None) -> TabConflict:
    tagged: TabConflict = {"base": conflict["base"], "ours": conflict["ours"],
                           "theirs": conflict["theirs"], "key": conflict["key"]}
    if label is not None:
        tagged["tab"] = label
    return tagged


def _report(ident: str, dry_run: bool, ours: Ir, tabs: doc_merge.TabPlan,
            written: Sequence[Written], asked: list[str]) -> SyncReport:
    """The report of a sync, every tab in it; what happened past the first tab is
    said with the tab's title in front."""
    info: SyncReport = {
        "document": ident, "url": url(ident), "dry_run": dry_run, "requests": 0,
        "conflicts": [], "notes": limits(ours) + tabs["notes"],
        "applied": list(tabs["applied"]), "kept": [], "comments": asked, "removed": []}
    for each in written:
        result, label = each["result"], each["label"]
        info["requests"] += len(result["requests"])
        info["conflicts"] += [_in_tab(c, label) for c in result["conflicts"]]
        info["notes"] += [_said_in(label, n) for n in result["notes"]]
        applied, kept, gone = _summary(result)
        info["applied"] += ([_said_in(label, note) for t in each["shaped"]
                             if (note := t.get("note")) is not None]
                            + [_said_in(label, a) for a in applied])
        info["kept"] += [_said_in(label, k) for k in kept]
        info["removed"] += [_said_in(label, g) for g in gone]
        if each["attempts"]:
            info["replanned"] = max(info.get("replanned", 0), each["attempts"])
    return info


def _write_tabs(drive: DriveService, docs: DocsService, ident: str, path: Path, ours: Ir,
                base: Ir, theirs: Ir, tabs: doc_merge.TabPlan, first: bool,
                doc: Document | None, notes: list[str] | None) -> list[Written]:
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
    known = {tab for p in doc_ir.parts(theirs) if (tab := p.get("tab")) is not None}
    siblings = doc_merge.tab_siblings(theirs)
    made: dict[str, str] = {}
    for part in tabs["create"]:
        # One at a time: a child tab needs the id its parent was just given, and a
        # tab placed among its siblings needs the ids of the ones already made.
        if (parent := part.get("parent")) is not None and parent in made:
            part["parent"] = made[parent]
        request = doc_merge.add_tab_request(part, known | set(made.values()),
                                            ours, siblings)
        reply = send(docs, ident, [request])
        tab = _new_tab_id(reply)
        if mine := part.get("tab"):
            made[mine] = tab
        part["tab"] = tab
        adding = request.get("addDocumentTab")
        props: DocsTabProperties = adding["tabProperties"] if adding is not None else {}
        if (index := props.get("index")) is None:
            raise ValueError("an addDocumentTab request that names no index")
        siblings.setdefault(props.get("parentTabId"), []).insert(index, tab)
        pairs.append((tab, part, {"blocks": []}))
    stager = Stager(drive, docs, path)
    written: list[Written] = []
    try:
        start: list[tuple[str | None, Ir, Ir]] = [(None, ours, base)] if first else []
        for tab, mine, was in start + pairs:
            done = _sync_part(docs, ident, path, stager, tab, ours, base, mine, was, doc, notes)
            written.append(done)
            if done["result"]["requests"] or done["shaped"] or done["attempts"]:
                doc = None  # the document has moved on from that read
    finally:
        # The pictures are in the document now, copied: the staging file can go.
        stager.close()
    return written


def _new_tab_id(answer: DocsBatchUpdateResponse) -> str:
    """The id Docs gave the tab one `addDocumentTab` made."""
    replies = answer.get("replies") or []
    added = as_object(replies[0].get("addDocumentTab"), "the addDocumentTab reply")
    props = as_object(added.get("tabProperties"), "the addDocumentTab reply's tabProperties")
    return as_str(props.get("tabId"), "the addDocumentTab reply's tabId")


def _sync_part(docs: DocsService, ident: str, path: Path, stager: Stager, tab: str | None,
               ours: Ir, base: Ir, mine: Ir, was: Ir, doc: Document | None,
               notes: list[str] | None) -> Written:
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
                     revision, notes)
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


def read_part(docs: DocsService, ident: str, tab: str | None, ours: Ir | None,
              base: Ir | None) -> tuple[Document, Ir]:
    """The document, and the IR of one tab of it (None: the first)."""
    doc = _get(docs, ident)
    return doc, _part_of(doc, tab, ours, base)


def _write_structure(docs: DocsService, ident: str, tab: str | None, ours: Ir, base: Ir,
                     mine: Ir, was: Ir, doc: Document, theirs: Ir, result: doc_merge.Plan
                     ) -> tuple[Document, Ir, Ir, doc_merge.Plan, list[doc_merge.Told]]:
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
    shaped: list[doc_merge.Told] = []
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
        # A table the *reader* beheaded in the browser carries no range either
        # (`doc_merge.recover_tables`), and this read is the one place that had
        # nobody to recover it: `anchor_tables` then saw a table with no key where
        # it was looking for one of ours whose range this very batch destroyed, and
        # gave it that key. Recovered first, it is not free to be taken, and
        # `plant_ranges` puts its own range back in the same breath.
        found = doc_merge.recover_tables(
            was, theirs, {t["key"] for t in result["shaped"] if t.get("key")})
        anchored = doc_merge.anchor_tables(theirs, result["shaped"])
        # And the empty paragraph a new table's swallow took the name off, which the
        # plan below would read as a block the reader had deleted. After the
        # anchoring: it is found by the table it stands in front of.
        anchored += doc_merge.recover_swallowed(theirs, result["shaped"])
        # And the empty paragraph the delete of the body's last table ate the mark
        # of, which the trailer now stands in for.
        anchored += doc_merge.recover_eaten(theirs, result["shaped"])
        if anchored or found:
            plant_ranges(docs, ident, theirs, tab)
            doc, theirs = read_part(docs, ident, tab, ours, base)
        was = doc_merge.rebase_tables(was, theirs, result["shaped"])
        result = doc_merge.plan(was, mine, theirs)
    return doc, theirs, was, result, shaped


AssumeMode = Literal["document-wins", "source-wins"]
ASSUME_MODES: tuple[AssumeMode, ...] = ("document-wins", "source-wins")
# The names this option had first. They read backwards: they named the side the base
# would be *taken from*, which is the side whose changes are thereby thrown away.
ASSUME_ALIASES: dict[str, AssumeMode] = {"file": "document-wins", "document": "source-wins"}
ASSUME_MEANS: dict[AssumeMode, str] = {
    "document-wins": "the document is right where they differ: nothing is written to it, "
                     "and the file is rewritten from the document — every edit made to the "
                     "source since the last sync is discarded",
    "source-wins": "the file is right where they differ: the file is written over the live "
                   "document — every edit a reader made there since the last sync is discarded",
}


def assume_mode(value: str | None) -> AssumeMode | None:
    """`--assume-base`, with the old spellings mapped and named for what they do."""
    match value:
        case None:
            return None
        case "document-wins" | "source-wins":
            return value
    mode = ASSUME_ALIASES.get(value)
    if mode is None:
        raise SystemExit(f"--assume-base {value}: expected one of "
                         f"{', '.join(ASSUME_MODES + tuple(ASSUME_ALIASES))}")
    print(f"  --assume-base {value} is the old name for --assume-base {mode}, and it reads "
          f"backwards: {ASSUME_MEANS[mode]}")
    return mode


def backup_document(drive: DriveService, document: str, path: Path) -> Path:
    """Export the document to `.b2s/backups/<stem>-<when>.html` before it is
    overwritten whole.

    `--assume-base source-wins` writes the file over a live document other people
    may be in, with no base to say what they changed, so there has to be a way
    back. The Slides side's rule (`guard.demand_way_back`): a write with no way
    back is something one asks for — `--no-backup` — and never something that
    happens because an export failed. So a refused export stops the sync.

    The export is Drive's HTML, which is a lossy read-back (docs/google-docs.md,
    "The round trip does not close on its own"): it is a copy of the words to
    recover from, not a file this tool could push back unchanged.
    """
    folder = state_dir(path) / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.html"
    try:
        data = _read(drive.files().export(fileId=document, mimeType="text/html"))
    except HttpError as err:
        raise SystemExit(
            f"the document could not be exported as a backup ({status_of(err)}), and\n"
            f"  --assume-base source-wins writes the file over it with no base to merge\n"
            f"  against. Nothing was written. Pass --no-backup to ask for that anyway.") from err
    out.write_bytes(data if isinstance(data, bytes) else str(data).encode("utf-8"))
    return out


def _no_base(path: Path, ours: Ir, theirs: Ir, assume: str | None,
             drive: DriveService | None = None, document: str | None = None,
             backup: bool = True, dry_run: bool = False) -> tuple[Ir, Path | None]:
    """What to do when the last sync's base is nowhere — neither in Drive nor beside
    the file. (The base, the backup taken before a destructive answer.)

    Without a base there is no way to tell a source change from a document change,
    and either guess throws somebody's work away — so the answer is the person's to
    give, and the message says whose work each answer costs.
    """
    mode = assume_mode(assume)
    if mode == "document-wins":
        return ours, None     # every difference is the document's: nothing is written
    if mode == "source-wins":
        # The destructive direction: the file goes over a live document, and nothing
        # read from it survives except by merge luck. Keep a copy first — and if a
        # copy cannot even be attempted, say so rather than write without one.
        # A dry run writes nothing, so it takes no backup either: `--dry-run` is how
        # one looks at this answer before giving it, and it used to leave a .docx in
        # `.b2s/backups/` for the look alone.
        kept = None
        if backup and not dry_run:
            if drive is None or not document:
                raise SystemExit(
                    "--assume-base source-wins writes the file over the live document, and\n"
                    "  there is no Drive service here to export a backup first. Nothing was\n"
                    "  written. --no-backup is how one asks for a write with no way back.")
            kept = backup_document(drive, document, path)
        return theirs, kept   # every difference is the source's: the file is written out
    raise SystemExit(
        f"no base for this document — neither in Drive nor at {base_path(path)}\n"
        f"  A three-way merge needs to know what both sides agreed on last time.\n"
        f"  --assume-base document-wins  {ASSUME_MEANS['document-wins']}\n"
        f"  --assume-base source-wins    {ASSUME_MEANS['source-wins']}\n"
        f"                               (the document is exported to {STATE_DIR}/backups/ "
        f"first; --no-backup skips that)")
