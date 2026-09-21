"""The Google Docs half of the project, as three journeys an agent can take.

The bargain is the one in docs/google-docs.md: a canonical HTML file in git is what the
source says, the Google Doc is what the reader says, and where both moved the document
wins. `doc_push` starts a pair, `doc_adopt` starts one from the document's end, and
`doc_sync` is every step after that - there is no fourth command, because a `files.update`
rebuild destroys every named range and the named ranges are what tell the merge which
block is which. That is the one rule an agent has to carry across calls.

Three things the command line says by printing, and an agent would otherwise run straight
over, are lifted into structure here:

* **Open comments.** A comment lives in Drive, not in the document's content, so nothing
  the merge reads can see one - and a sync that rewrites the passage a comment hangs on
  answers it by accident. Every open one comes back as a warning and in
  `data["open_comments"]`.
* **The `--assume-base` dialog.** With no base anywhere, both answers throw somebody's
  work away, so the library refuses and asks. That refusal is `base_choice_needed`, with
  both options and what each one costs in `data["options"]`.
* **A chunked write.** Past 500 requests one batch becomes several, and several batches
  are not atomic: one that fails part-way leaves the earlier ones in the document.
* **What a sync took away.** Most notes say what a sync left alone; one says what it
  dropped - a block written again from nothing that carried a property the dialect has no
  spelling for. It comes back under "what is lost", with `data["lost"]` counting them and
  a next step, because an agent reading a column of warnings cannot otherwise tell the one
  that is a loss from the fourteen that are cautions.

The library module is imported as `docs` here, because the tool named `doc_sync` would
otherwise shadow it. Nothing in this file builds a Google service or asks for
credentials: `@tool` has installed the provider by the time a body runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from .. import doc_ir
from .. import doc_sync as docs
from .context import Job, tool
from .types import READS, READS_GOOGLE, WRITES, WRITES_GOOGLE, Refused

#: The note `doc_sync.send` leaves behind when it had to cut one batch into several.
CHUNK_MARK = "batches of up to"
#: What `assume_base` will take: the two answers, and the old names that read backwards.
ASSUME_VALUES = docs.ASSUME_MODES + tuple(docs.ASSUME_ALIASES)


# ---------------------------------------------------------------- the translation layer

def report_diagnostics(j: Job, info: dict) -> dict[str, Any]:
    """Turn one sync report into diagnostics, and say what was in it.

    Written apart from the journey so it can be tested on a report dict alone: the shape
    is `doc_sync._report`'s (`conflicts`, `notes`, `comments`), and every field of it has
    a level. A conflict is a place the document won over the source; a note is something
    the sync left alone or could not carry; a comment is a question in Drive the merge is
    blind to.
    """
    for clash in info.get("conflicts") or []:
        where = str(clash.get("key", "?"))
        if clash.get("tab") is not None:
            where = f"{clash['tab']} / {where}"
        j.conflict(f"the source said {clash.get('ours')!r}, the document says "
                   f"{clash.get('theirs')!r}; the document won", where)
    chunked = False
    lost = 0
    for note in info.get("notes") or []:
        if CHUNK_MARK in note:
            chunked = True
            j.warn(f"the write did not go in as one batch: {note}. Unlike a single batch "
                   f"this is not atomic, so read the document rather than assuming it "
                   f"either all landed or none of it did.", "the write")
        elif docs.LOSS_MARK in note:
            # The one note that is a loss and not a caution: this run really is
            # writing that block again from nothing (`doc_sync.rewrite_losses`).
            lost += 1
            j.warn(f"{note}. Nothing on our side can put it back, since the file has no "
                   f"spelling for it.", "what is lost")
        else:
            j.warn(note)
    if lost:
        j.suggest("set the property again by hand on the block(s) named above, or, next "
                  "time, leave that block to the document and make the change where the "
                  "file can carry it")
    comments = list(info.get("comments") or [])
    for comment in comments:
        j.warn(f"open comment, which no merge can see (it lives in Drive, not in the "
               f"document's content): {comment}", "the document")
    if comments:
        j.suggest("read the open comments before trusting this sync: a rewritten passage "
                  "answers the comment hanging on it by accident")
    if info.get("replanned"):
        j.warn(f"somebody typed in the document while this sync was planned; it was read "
               f"and planned again {info['replanned']} time(s)", "the document")
    if info.get("base") == "none":
        j.warn("there was no base, so assume_base decided the whole merge rather than a "
               "three-way comparison", "the base")
    return {"conflicts": len(info.get("conflicts") or []),
            "notes": len(info.get("notes") or []), "comments": len(comments),
            "chunked": chunked, "lost": lost}


def _state_artifacts(j: Job, path: Path) -> dict[str, str]:
    """Register `.b2s/` beside the canonical file, and say what is in it.

    The Docs state does not live in an out folder: the base cache, the report and any
    backup sit next to the HTML file, because beside the file is where a second checkout
    looks for them.
    """
    out: dict[str, str] = {}
    folder = docs.state_dir(path)
    if folder.is_dir():
        j.artifact(folder, "folder", "the Docs state beside the canonical file: the base "
                                     "cache, the sync report and any backup")
        out["state"] = j.ctx.workspace.ref(folder)
    base = docs.base_path(path)
    if base.is_file():
        j.artifact(base, "json", "the cached sync base (Drive holds the one every checkout "
                                 "sees; this is the copy beside the file)")
        out["base_file"] = j.ctx.workspace.ref(base)
    return out


def _report_artifacts(j: Job, info: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    if not info.get("report"):
        return out
    md = Path(info["report"])
    if md.is_file():
        j.artifact(md, "report", "what this sync wrote, kept and could not carry, in prose")
        out["report"] = j.ctx.workspace.ref(md)
    data = md.with_suffix(".json")
    if data.is_file():
        j.artifact(data, "json", "the same report as data, with the planned requests in it "
                                 "on a dry run")
        out["report_json"] = j.ctx.workspace.ref(data)
    return out


def _options() -> list[dict[str, str]]:
    return [{"value": mode, "costs": docs.ASSUME_MEANS[mode]} for mode in docs.ASSUME_MODES]


def _exit(j: Job, exc: SystemExit, file: str | None = None) -> None:
    """Turn the library's `SystemExit` into the refusal an agent can branch on.

    The library says no by printing a paragraph and exiting; the codes are in
    `types.CODES`. Anything not recognised here goes back up and becomes `refused` with
    the paragraph as the summary, which is honest rather than mislabelled.
    """
    said = str(exc.code) if exc.code not in (0, None) else ""
    if "no base for this document" in said:
        for option in _options():
            j.suggest(f"doc_sync again with assume_base={option['value']!r}: {option['costs']}")
        raise Refused("base_choice_needed",
                      "There is no sync base for this document - neither in Drive nor in "
                      ".b2s/ beside the file - so nothing can tell a source change from a "
                      "reader's change. Say which side to assume; each answer throws the "
                      "other side's work since the last sync away.\n" + said,
                      options=_options(), file=file)
    if "already names document" in said:
        j.suggest("doc_sync to merge into the document it already names")
        raise Refused("already_pushed", said, file=file)
    if "is already there and" in said:
        code = "already_pushed" if "names document" in said else "source_exists"
        raise Refused(code, said, file=file)
    if "does not say which document" in said:
        j.suggest("pass doc= with the document's URL or id",
                  "doc_push to create the document this file belongs to")
        raise Refused("bad_request", said, file=file)
    if "--assume-base" in said:
        raise Refused("bad_request", said, options=_options())
    if "could not be exported as a backup" in said:
        raise Refused("no_way_back", said, file=file)
    if "no such file" in said:
        raise Refused("not_found", said, file=file)
    if "outside any block" in said:
        # The file says something no block carries, so a run that went ahead would write
        # nothing, report that both sides agree, and then take those words out of the file.
        j.suggest("fix the tag around the quoted text (every block is a <p>, <h1>-<h6>, "
                  "<li> or <table>) and run this again")
        raise Refused("bad_request", said, file=file)
    raise exc


# ---------------------------------------------------------------- push

@tool("doc_push", needs=(READS, WRITES, WRITES_GOOGLE))
def doc_push(
    j: Job,
    file: Annotated[str, "Workspace ref of the canonical HTML file to create the document "
                         "from, e.g. 'notes/spec.html'."],
    name: Annotated[str | None, "Title for the new Google Doc; the file's own <title>, or "
                                "its filename, when this is not given."] = None,
    new_doc: Annotated[bool, "Create a second document even though the file already names "
                             "one. Almost never right: it splits one story in two."] = False,
) -> None:
    """Create a Google Doc from a canonical HTML file and anchor every block in it.

    One Drive import plus a few batches - seconds, and it spends nothing but a new
    document. The file is rewritten in place with a key on every block and a `<meta>`
    naming the document, and the first sync base is stored beside it and in Drive.

    Use it once per file. Every step after it is `doc_sync`: a re-import would destroy the
    named ranges, and those are the only thing that says which block is which.
    """
    path = j.path(file, write=True)
    if not path.is_file():
        raise Refused("not_found", f"{file} is not there; doc_push reads the canonical HTML "
                                   f"file that is to become the document.", file=file)
    named = doc_ir.from_html(path.read_text(encoding="utf-8")).get("document")
    if named and not new_doc:
        j.suggest("doc_sync to merge into the document it already names")
        raise Refused("already_pushed",
                      f"{file} already names document {named} ({docs.url(named)}). A second "
                      f"push would make a second document and leave two halves of one "
                      f"story; pass new_doc=True only if that is really wanted.",
                      document=named, url=docs.url(named), file=file)
    try:
        info = docs.push(path, name, new_doc)
    except SystemExit as exc:
        _exit(j, exc, file)
        return

    for note in info.get("notes") or []:
        j.warn(note, file)
    j.artifact(path, "html", "the canonical file, rewritten with a key on every block and a "
                             "<meta> naming the document")
    state = _state_artifacts(j, path)
    j.data.update({"url": info["url"], "document": info["document"],
                   "blocks": info["blocks"], "anchored": info["anchored"],
                   "tabs": info.get("tabs", 1), "file": j.ctx.workspace.ref(path), **state})
    j.suggest("doc_sync after either side changes - never a second doc_push",
              "doc_sync with dry_run=True first to see what a merge would write")
    j.summary = (
        f"Created {info['url']} from {file}: {info['blocks']} block(s), {info['anchored']} "
        f"of them anchored by a named range, across {info.get('tabs', 1)} tab(s). From here "
        f"the file is the source of truth for structure and the document for content, and "
        f"where both moved the document wins. There is no Docs equivalent of a deck "
        f"rebuild: a `files.update` re-import would destroy every named range and with it "
        f"the merge's only way of telling which block is which, so every later step is "
        f"doc_sync.")


# ---------------------------------------------------------------- sync

@tool("doc_sync", needs=(READS, WRITES, READS_GOOGLE))
def doc_sync(
    j: Job,
    file: Annotated[str, "Workspace ref of the canonical HTML file; it normally names the "
                         "document it belongs to."],
    doc: Annotated[str | None, "The document's URL or id, when the file does not name one "
                               "(or to merge into a different one)."] = None,
    dry_run: Annotated[bool, "Plan the merge and write the report, but send nothing to the "
                             "document. Works in a read-only context."] = False,
    assume_base: Annotated[str | None, "Only when there is no base: 'document-wins' "
                                       "discards source edits since the last sync, "
                                       "'source-wins' discards the reader's."] = None,
    backup: Annotated[bool, "Export the document before an assume_base='source-wins' "
                            "write. Turning it off asks for a write with no way back."] = True,
) -> None:
    """Merge the canonical file and the Google Doc three ways, write, and rewrite the file.

    A few reads and a batch or two per tab: seconds on a normal document, and the only
    thing that should ever write to a pushed one. Where both sides moved, the document
    wins; conflicts, what was kept, what could not be carried and the document's open
    comments all come back as diagnostics and in `.b2s/<stem>.sync-report.md`.

    Run it with dry_run=True first when readers are in the document. Afterwards the file,
    the document and the base all say the same thing, so a second sync writes 0 requests -
    which is the property worth checking.
    """
    path = j.path(file, write=True)
    if not path.is_file():
        raise Refused("not_found", f"{file} is not there; doc_sync reads the canonical HTML "
                                   f"file.", file=file)
    if assume_base is not None and assume_base not in ASSUME_VALUES:
        raise Refused("bad_request",
                      f"assume_base={assume_base!r} is not one of {', '.join(ASSUME_VALUES)}.",
                      options=_options())
    if not dry_run:
        # `@tool` declares the least this journey does, so a read-only context can still
        # plan a merge. A real write says so here, before the first request goes out.
        j.require(WRITES_GOOGLE)
    try:
        info = docs.sync(path, doc, dry_run, assume_base, backup)
    except SystemExit as exc:
        _exit(j, exc, file)
        return

    counts = report_diagnostics(j, info)
    refs = _report_artifacts(j, info)
    wrote = bool(not info["dry_run"] and info["requests"])
    if not info["dry_run"]:
        j.artifact(path, "html", "the canonical file, regenerated from the document this "
                                 "sync just wrote")
    if info.get("backup"):
        kept = Path(info["backup"])
        if kept.is_file():
            j.artifact(kept, "html", "the document as it was, exported before the file was "
                                     "written over it")
            refs["backup"] = j.ctx.workspace.ref(kept)
    refs |= _state_artifacts(j, path)
    j.data.update({
        "url": info["url"], "document": info["document"], "dry_run": info["dry_run"],
        "written": wrote, "requests": info["requests"],
        "applied": len(info["applied"]), "kept": len(info["kept"]),
        "conflicts": counts["conflicts"], "chunked": counts["chunked"],
        # Blocks this run wrote again from nothing that carried something the file
        # cannot say. Almost always 0, and a number worth seeing when it is not.
        "lost": counts["lost"],
        "base": info.get("base"), "open_comments": list(info.get("comments") or []),
        "applied_examples": list(info["applied"])[:20],
        "kept_examples": list(info["kept"])[:20],
        "file": j.ctx.workspace.ref(path), **refs})
    if info.get("blocks") is not None:
        j.data["blocks"] = info["blocks"]

    head = (f"{'Planned' if info['dry_run'] else 'Merged'} {file} against {info['url']}: "
            f"{len(info['applied'])} block(s) from the source, {len(info['kept'])} kept from "
            f"the document, {counts['conflicts']} conflict(s) the document won, "
            f"{info['requests']} request(s) {'planned' if info['dry_run'] else 'sent'}.")
    if info["dry_run"]:
        j.suggest("run doc_sync again with dry_run=False" if info["requests"] else
                  "nothing to write: the file and the document already agree")
        tail = ("Nothing was written to the document. "
                + ("Run it again with dry_run=False to send these requests."
                   if info["requests"] else
                   "There is nothing to send: the two sides already say the same thing."))
    else:
        j.suggest("doc_sync again with dry_run=True to confirm it now writes 0 requests")
        tail = ("The file, the document and the base now say the same thing, so a second "
                "sync should write 0 requests - worth confirming with dry_run=True.")
    if counts["comments"]:
        tail += (f" {counts['comments']} open comment(s) hang on passages in the document "
                 f"that nothing in the merge can see; read them before trusting this.")
    j.summary = f"{head} {tail}"


# ---------------------------------------------------------------- adopt

@tool("doc_adopt", needs=(READS, WRITES, READS_GOOGLE))
def doc_adopt(
    j: Job,
    doc: Annotated[str, "The document's URL or id: the Google Doc nobody ever pushed."],
    file: Annotated[str | None, "Workspace ref for the canonical HTML file to write; a slug "
                                "of the document's title when not given."] = None,
    force: Annotated[bool, "Overwrite the target file although it is already there and "
                           "names another document, or none."] = False,
) -> None:
    """Write the canonical HTML file a Google Doc nobody pushed never had.

    One read of every tab plus a batch of named ranges: seconds, and it writes nothing to
    the document but the anchors that make it syncable. Afterwards the pair is an ordinary
    one and `doc_sync` keeps it in step. Idempotent: a second run plants no second set of
    anchors and writes the same file.

    What no HTML import could create does not come back in the file: chips, equations,
    dropdowns and a table of contents are frozen runs, reported by every sync and never
    rewritten by one.

    The warnings also name the individual blocks that carry something the dialect cannot
    say — a paragraph border, a tab stop, a superscript, a table's column widths. Those
    survive every ordinary edit and go the moment the block holding them is written again
    from nothing, so a block named there is one to change in the document rather than in
    the file.
    """
    target = j.path(file, write=True) if file else None
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # A `path` of None makes adopt name the file after the document's title, which it
        # only learns by reading it; `folder` is where that name lands, so the file cannot
        # come out anywhere but the workspace.
        info = docs.adopt(doc, target, force, folder=j.ctx.workspace.root)
    except SystemExit as exc:
        _exit(j, exc, file)
        return

    written = j.path(info["file"]) if target is None else target
    ref = j.ctx.workspace.ref(written)
    for note in info.get("notes") or []:
        j.warn(note, ref)
    j.artifact(written, "html", "the canonical file: what the source now says, keyed block "
                                "by block")
    state = _state_artifacts(j, written)
    j.data.update({"file": ref, "url": info["url"], "document": info["document"],
                   "blocks": info["blocks"], "anchored": info["anchored"],
                   "tabs": info["tabs"], **state})
    j.suggest(f"doc_sync with file={ref!r} from here on",
              "commit the file: it is what git keeps of the document")
    j.summary = (
        f"Wrote {ref} from {info['url']}: {info['blocks']} block(s), {info['anchored']} of "
        f"them anchored by a named range, across {info['tabs']} tab(s). The two are an "
        f"ordinary synced pair from here, and doc_sync is the only thing that should write "
        f"to either. What an HTML import cannot create does not come back in the file - "
        f"chips, equations, dropdowns and a table of contents are frozen runs that every "
        f"sync reports and never rewrites. The warnings name the blocks carrying something "
        f"the file cannot say: change those in the document, not in the file.")


TOOLS = (doc_push, doc_sync, doc_adopt)
