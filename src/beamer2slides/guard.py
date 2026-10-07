"""Guarding a live deck against an accidental rebuild (docs/sync.md, "Never lose deck edits").

`convert` on an output folder that already has a deck replaces that deck's whole content
(`files.update`). If someone edited it in Slides, the edits are gone. This module asks the same
question `sync` asks - what did the person change since the converter wrote this deck? - against
the sync base (`<out>/sync/base.json`, or Drive `appProperties.b2sBase`), and refuses the rebuild
when the answer isn't "nothing".

What counts as an edit is `merge.deck_edits_of` / `merge.user_objects_of` /
`merge.background_edited_of`, read off the parsed base (`sync_model.base`), i.e. exactly what sync
would keep:
  - a new `revisionId` is not an edit (Google bumps it on its own, e.g. when a deck is opened),
  - a new `contentUrl` for the same picture is not an edit (pixel signatures decide, `snapshot`),
  - a thumbnail export is not an edit,
  - `measure_places`' scratch slides (`b2s_mNNN`) left by an interrupted run are not an edit.
A base that does not parse is refused as no base at all is (`Refusal`): nothing can say whether
the deck was edited against it.

Before a destructive write the deck's `revisionId` is recorded (`<out>/backups/backups.json`,
`emit.json` "previous") and a backup can be kept: an exported .pptx next to the output folder
and/or a Drive copy of the presentation - in the app's hidden Drive space when hidden storage is on
(`drive_folder.hidden`: a native copy, `hidden_copy`, or the .pptx, `hidden_pptx`).
"""

import io
import json
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, TypedDict

from . import merge, snapshot, sync_model
from .gapi import HttpError, message_of
from .deck_pictures import WORKERS
from .google_types import (DriveFile, DriveService, FileBody, Presentation, SlidesService, as_json, file_id,
                           object_id)
from .gslides import execute
from .json_types import Json, JsonObject, JsonShapeError
from .merge import Edit
from .sync_model import Base, DeckRead, ElementEntry, ImageRead, ObjectId, SlideEntry, SlideRead
from .typing_compat import assert_never

SCRATCH = re.compile(r"b2s_m\d{3}")  # emit.measure_places' scratch slides
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
EXPORT_LIMIT_NOTE = "Drive refuses files.export above 10 MB"
BACKUP_MODES = ("auto", "none", "file", "drive", "both")

FIELD_WORDS = {
    "text": "text edited",
    "text_style": "text style changed",
    "geometry": "moved or resized",
    "shape_style": "fill or outline changed",
    "image": "picture replaced",
    "image_unverified": "picture cannot be compared",
    "group": "group taken apart",
    "deleted": "deleted",
    "part_deleted": "partly deleted",
}
EXAMPLES = 3


class RebuildRefused(Exception):
    """Raised instead of replacing a deck that must not be replaced. `survey` is the finding."""

    def __init__(self, message: str, survey: JsonObject):
        super().__init__(message)
        self.survey = survey


def deck_url(pid: str) -> str:
    return f"https://docs.google.com/presentation/d/{pid}/edit"


def _object(v: Json) -> JsonObject:
    """A log's or a backup's nested object, absent (or not one) read as empty: these are notes a
    person may have edited, read to say what can be said, never refused."""
    return v if isinstance(v, dict) else {}


def _array(v: Json) -> list[Json]:
    return v if isinstance(v, list) else []


# ---------------------------------------------------------------- the previous deck


def previous_deck(drive: DriveService, out: Path) -> JsonObject | None:
    """What the output folder's emit.json points at: {"presentationId", "state", "name",
    "modifiedTime"}. state: "live", "trashed", "gone" (deleted or not ours any more),
    "other" (not a presentation). An emit.json naming no presentation id points at nothing."""
    state_file = out / "emit.json"
    if not state_file.exists():
        return None
    try:
        written: Json = json.loads(state_file.read_text(encoding="utf-8"))
    except ValueError:
        return None
    pid = _object(written).get("presentationId")
    if not isinstance(pid, str):
        return None
    try:
        f = execute(drive.files().get(fileId=pid, fields="id,name,trashed,mimeType,modifiedTime"))
    except HttpError:
        return {"presentationId": pid, "state": "gone", "name": None, "modifiedTime": None}
    state = "live" if not f.get("trashed") else "trashed"
    if f.get("mimeType") != "application/vnd.google-apps.presentation":
        state = "other"
    return {"presentationId": pid, "state": state, "name": f.get("name"), "modifiedTime": f.get("modifiedTime")}


# ---------------------------------------------------------------- detection


def sign_changed(base: Base, theirs: DeckRead, pres: Presentation, drive: DriveService | None) -> DeckRead:
    """`theirs` with pixel signatures for the live pictures whose contentUrl differs from the
    base's. Google issues new URLs for pictures nobody touched, so only the pixels tell a replaced
    one apart (sync.Sync.sign_changed does the same before planning). A rebuild writes over every
    one of them, so all are read: downloaded, else out of a Drive export through `drive`
    (`deck_pictures.LivePictures`)."""
    images = {oid: rb.image for s in base.slides for e in s.elements for oid, rb in e.readback.items()
              if rb.image is not None}
    backgrounds = {s.object_id: _background_seen(s) for s in base.slides}
    objects: set[str] = set()
    slides: set[str] = set()
    for s in theirs.slides:
        for oid, rb in s.objects.items():
            old_image = images.get(oid)
            if rb.image is not None and old_image is not None and old_image.content_hash != rb.image.content_hash:
                objects.add(oid)
        bg, old = s.background or {}, backgrounds.get(s.object_id, {})
        if "picture" in bg and "picture" in old and old["picture"] != bg["picture"]:
            slides.add(s.object_id)
    if not (objects or slides):
        return theirs
    return snapshot.sign_pictures_of(theirs, pres, objects, slides, WORKERS, None, None, drive, None, None)[0]


def _background_seen(s: SlideEntry) -> JsonObject:
    """The background the base read of its slide ({}: none)."""
    return (None if s.seen is None else s.seen.background_readback) or {}


UNVERIFIABLE = ("image_unverified", "background_unverified")


def _unverifiable(field: Edit, el: ElementEntry, oids: Sequence[ObjectId]) -> str:
    """A picture edit that is only a new URL against a base with no pixel signature: bases
    written before signatures were recorded (2026-09-17 and older) cannot say whether a picture
    Google re-issued is the same one, so it counts - the rule does not bend - but as a picture
    that cannot be compared, not as one somebody replaced."""
    if field != "image":
        return field
    old = [_image_seen(el, oid) for oid in oids]
    return "image_unverified" if old and all(o is not None and not o.signature for o in old) else field


def _image_seen(el: ElementEntry, oid: ObjectId) -> ImageRead | None:
    rb = el.readback.get(oid)
    return None if rb is None else rb.image


def _never_made(el: ElementEntry, oids: Sequence[ObjectId], read: SlideRead) -> bool:
    """A "deleted" main object the base's own read-back never had, while the element's other
    objects stand: a group emit named and Slides never made (a diagram of one node). Bases
    written before `snapshot.attach_readback` left such ids out still name them."""
    readback = el.readback
    return bool(readback) and all(oid not in readback for oid in oids) and \
        all(oid in read.objects for oid in readback)


def _snippet(text: str | None, length: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[:length - 1] + "…"


def _count(counts: dict[str, int], kind: str, n: int) -> None:
    counts[kind] = counts.get(kind, 0) + n


def survey(base: Base, pres: Presentation, sign: bool, drive: DriveService | None) -> JsonObject:
    """What the person changed in the live deck since the base was recorded (`drive`: see
    `sign_changed`).

    {"edited": bool, "revisionId", "counts": {field/kind: n}, "slides": [{"slide", "why", "edits"}],
     "slides_added", "slides_deleted", "reordered", "examples": [readable lines]}"""
    pres = {**pres, "slides": [s for s in pres.get("slides", []) if not SCRATCH.fullmatch(object_id(s))]}
    theirs = snapshot.read_presentation_of(pres)
    if sign:
        theirs = sign_changed(base, theirs, pres, drive)
    live = {s.object_id: s for s in theirs.slides}
    counts: dict[str, int] = {}
    slides: list[Json] = []
    examples: list[str] = []
    for n, b in enumerate(base.slides, start=1):
        # (a slide's own title: two slides the deck never had share no objectId to be named by)
        title = f"slide {n} \"{_snippet(b.title or b.key, 30)}\""
        read = None if b.object_id is None else live.get(b.object_id)
        if read is None:
            _count(counts, "slides_deleted", 1)
            slides.append({"slide": b.key, "page": n, "why": ["slide deleted"], "edits": []})
            examples.append(f"{title}: deleted in Slides")
            continue
        edits: list[Json] = []
        why: list[Json] = []
        for el in b.elements:
            for edit, oids in merge.deck_edits_of(el, read).items():
                if edit == "deleted" and _never_made(el, oids, read):
                    continue
                field = _unverifiable(edit, el, oids)
                _count(counts, field, 1)
                edits.append({"element": el.key, "field": field, "objects": [o for o in oids]})
                if len(examples) < EXAMPLES:
                    what = FIELD_WORDS.get(field, field)
                    looked = read.objects.get(oids[0])
                    shown = looked.text if field == "text" and looked is not None else None
                    examples.append(f"{title}: {what} ({el.key}{f': “{_snippet(shown, 40)}”' if shown else ''})")
        added = merge.user_objects_of(b, read)
        if added:
            _count(counts, "objects_added", len(added))
            why.append(f"{len(added)} object(s) added")
            if len(examples) < EXAMPLES:
                examples.append(f"{title}: {len(added)} object(s) added in Slides")
        if ("" if b.seen is None else b.seen.notes_readback) != read.notes:
            _count(counts, "notes", 1)
            why.append("notes edited")
            if len(examples) < EXAMPLES:
                examples.append(f"{title}: speaker notes edited")
        if merge.background_edited_of(b, read):
            old, new = _background_seen(b), read.background or {}
            unknown = "picture" in old and "picture" in new and not old.get("signature")
            kind = "background_unverified" if unknown else "background"
            words = "background picture cannot be compared" if unknown else "background changed"
            _count(counts, kind, 1)
            why.append(words)
            if len(examples) < EXAMPLES:
                examples.append(f"{title}: {words}")
        if edits:
            why.insert(0, f"{len(edits)} element edit(s)")
        if why:
            slides.append({"slide": b.key, "page": n, "why": why, "edits": edits})
    base_ids = {b.object_id for b in base.slides}
    extra = [s.object_id for s in theirs.slides if s.object_id not in base_ids]
    if extra:
        counts["slides_added"] = len(extra)
        if len(examples) < EXAMPLES:
            examples.append(f"{len(extra)} slide(s) added in Slides")
    # Reorder: the base slides that are still there, in the live order.
    live_order = [s.object_id for s in theirs.slides if s.object_id in base_ids]
    base_order = [b.object_id for b in base.slides if b.object_id in set(live_order)]
    reordered = live_order != base_order
    if reordered:
        counts["slides_reordered"] = 1
        if len(examples) < EXAMPLES:
            examples.append("the slides were reordered in Slides")
    return {"edited": bool(slides or extra or reordered), "revisionId": theirs.revision_id,
            "counts": {k: n for k, n in counts.items()}, "slides": slides, "slides_added": len(extra),
            "slides_deleted": counts.get("slides_deleted", 0), "reordered": reordered,
            "examples": [e for e in examples]}


SUMMARY_WORDS = {"text": "text edit", "text_style": "style change", "geometry": "move or resize",
                 "shape_style": "fill or outline change", "image": "picture replaced", "group": "group taken apart",
                 "deleted": "element deleted", "part_deleted": "element partly deleted", "notes": "notes edit",
                 "background": "background change", "objects_added": "object added in Slides",
                 "image_unverified": "uncomparable picture",
                 "background_unverified": "uncomparable background",
                 "slides_added": "slide added", "slides_deleted": "slide deleted"}


def _counts(found: JsonObject) -> JsonObject:
    counts = found.get("counts")
    return counts if isinstance(counts, dict) else {}


def _lines(found: JsonObject, key: str) -> list[str]:
    said = found.get(key)
    return [e for e in said if isinstance(e, str)] if isinstance(said, list) else []


def summary_line(found: JsonObject) -> str:
    """"3 slides edited: 2 text edits, 1 object added in Slides, slides reordered"."""
    parts = [f"{n} {SUMMARY_WORDS[k]}{'s' if n > 1 and not SUMMARY_WORDS[k].endswith('ed') else ''}"
             for k, n in _counts(found).items() if k in SUMMARY_WORDS and isinstance(n, int) and n]
    if found["reordered"]:
        parts.append("slides reordered")
    edited = found["slides"]
    slides = len(edited) if isinstance(edited, list) else 0
    head = f"{slides} slide{'s' if slides != 1 else ''} edited" if slides else "the deck was edited"
    return f"{head}: {', '.join(parts)}" if parts else head


# ---------------------------------------------------------------- the check


def command_line(command: str, pdf: Path | str | None, out: Path, extra: str) -> str:
    named = str(pdf) if pdf else "<pdf>"
    return f"python -m beamer2slides {command} {named} {'--deck' if command == 'sync' else '--out'} {out}{extra}"


Refusal = Literal["edited", "no-base", "unreadable-base", "other-source"]
"""Why `check_rebuild` will not replace a deck. `unreadable-base`: a base this version cannot
parse (`sync_model.base`: an old or damaged base.json that still says it is one), which no sync
can merge against either - so, like no base at all, nothing can say whether the deck was edited."""


def refusal_message(pid: str, out: Path, pdf: Path | str | None, found: JsonObject, reason: Refusal) -> str:
    lines: list[str] = []
    counts = _counts(found)
    if reason == "edited" and counts and set(counts) <= set(UNVERIFIABLE):
        lines.append("refusing to rebuild: this deck's sync base was written before beamer2slides recorded "
                     "picture signatures, so its pictures cannot be compared with the live deck (Google gives "
                     "unchanged pictures new URLs).")
        lines.append(f"  {deck_url(pid)}")
        lines.append(f"  {summary_line(found)}; nothing else differs.")
        lines += [f"    - {e}" for e in _lines(found, "examples")]
        lines.append("  If nobody replaced a picture or a background in Slides, a forced rebuild loses nothing "
                     "(the base it writes carries signatures, so the next rebuild is checked in full).")
    elif reason == "edited":
        lines.append("refusing to rebuild: this deck was edited in Google Slides after beamer2slides wrote it.")
        lines.append(f"  {deck_url(pid)}")
        lines.append(f"  {summary_line(found)}")
        lines += [f"    - {e}" for e in _lines(found, "examples")]
    elif reason == "no-base":
        lines.append("refusing to rebuild: there is no sync base for this deck, so whether someone "
                     "edited it cannot be checked.")
        lines.append(f"  {deck_url(pid)}")
        lines.append("  (the base is written by convert; an older deck, or a convert whose base "
                     "recording failed, has none)")
    elif reason == "unreadable-base":
        lines.append("refusing to rebuild: this deck's sync base cannot be read by this version of beamer2slides, "
                     "so whether someone edited the deck cannot be checked.")
        lines.append(f"  {deck_url(pid)}")
        lines.append(f"  ({found.get('base_problem')})")
    elif reason == "other-source":
        lines.append(f"refusing to rebuild: the deck in {out} was converted from "
                     f"{found.get('base_source')}, not from {pdf}.")
        lines.append(f"  {deck_url(pid)}")
        lines.append("  rebuilding it here would replace that deck with this PDF's slides.")
    else:
        assert_never(reason)
    lines.append("  A rebuild replaces the whole deck. What to do instead:")
    if reason == "edited":
        lines.append(f"    merge the PDF into the deck, keeping the edits:  {command_line('sync', pdf, out, '')}")
    lines.append(f"    leave that deck alone and make a new one:        {command_line('convert', pdf, out, ' --new-deck')}")
    lines.append(f"    rebuild anyway (the deck's content is replaced): {command_line('convert', pdf, out, ' --force-rebuild')}")
    if found.get("revisionId"):
        lines.append(f"  The deck is at revision {found['revisionId']}; a forced rebuild keeps a backup "
                     "first (--backup, docs/sync.md).")
    return "\n".join(lines)


def _source_name(base: Base) -> str | None:
    """The file name of the PDF the base says the deck came from."""
    pdf = (base.source or {}).get("pdf")
    return (Path(pdf).name or None) if isinstance(pdf, str) and pdf else None


def check_rebuild(slides: SlidesService, drive: DriveService, pid: str, out: Path, pdf: Path | str | None,
                  force: bool) -> JsonObject:
    """Look at the live deck before replacing its content. Returns the finding
    ({"reason", "revisionId", ...}); raises RebuildRefused unless `force`."""
    # Two reads that need nothing but the id, so they are made at once: the base out of Drive on a
    # thread of its own while the live deck comes down here. One client per thread, which is all a
    # service object asks - these two are different services.
    with ThreadPoolExecutor(1, thread_name_prefix="b2s-guard") as pool:
        loading = pool.submit(snapshot.load_base, pid, out, drive, None, None)
        pres = execute(slides.presentations().get(presentationId=pid))
        stored, where = loading.result()
    found: JsonObject = {
        "presentationId": pid, "revisionId": pres.get("revisionId"), "base_from": where,
        "checked": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": "", "edited": False, "examples": [],
        "counts": {}, "slides": [], "slides_added": 0, "slides_deleted": 0, "reordered": False}
    reason: Refusal | None = None
    base: Base | None = None
    if stored is None:
        reason = "no-base"
    else:
        try:
            base = sync_model.base(stored)
        except JsonShapeError as e:
            # Never a rebuild over the deck: a base nobody can read is no answer to "was it edited?"
            reason = "unreadable-base"
            found["base_problem"] = str(e)
    if base is not None:
        found.update(survey(base, pres, True, drive))
        found["base_generation"] = 0 if base.generation is None else base.generation
        source = _source_name(base)
        found["base_source"] = source
        if found["edited"]:
            reason = "edited"
        elif pdf is not None and source and Path(pdf).name != source:
            reason = "other-source"
    if reason is not None:
        found["reason"] = reason
        if not force:
            raise RebuildRefused(refusal_message(pid, out, pdf, found, reason), found)
    return found


def recheck(slides: SlidesService, pid: str, found: JsonObject | None) -> JsonObject | None:
    """`found` again, where the deck is still at the revision it was found at - one field of one
    read instead of the whole deck, the sync base and the survey (None: ask the question again).

    `convert` asks the guard twice: once while the PDF is being converted, and once immediately
    before the write, because the deck may be edited in between. Only what changed in between is
    really in question, and a revisionId that has not moved is a deck nobody has touched - so the
    second ask is a `presentations.get` of that one field. A finding with a reason is never
    reused: that one raised where it was made."""
    if not found or found.get("presentationId") != pid or not found.get("revisionId") or found.get("reason"):
        return None
    try:
        now = execute(slides.presentations().get(presentationId=pid, fields="revisionId"))
    except HttpError:
        return None
    if now.get("revisionId") != found["revisionId"]:
        return None
    return {**found, "checked": time.strftime("%Y-%m-%d %H:%M:%S"), "rechecked": "revision unchanged"}


# ---------------------------------------------------------------- backups


def backup_dir(out: Path) -> Path:
    return out / "backups"


def export_pptx(drive: DriveService, pid: str, path: Path) -> int:
    """The live deck as a .pptx next to the output folder. Drive refuses files.export over 10 MB,
    and takes minutes over a deck in a big embedded face (`gslides.SLOW_EXPORT`); a timeout is
    not tried again (`deck_export.export_bytes`)."""
    from .deck_export import export_bytes
    data = export_bytes(drive, pid)
    write_whole(path, data)
    return len(data)


class PartFile(TypedDict):
    """One .pptx part `export_parts` wrote: its path, its slides [first, last] 1-based, its size."""
    file: str
    slides: list[int]
    bytes: int


class Unexported(TypedDict):
    """Slides [first, last] 1-based that no part holds, and why."""
    slides: list[int]
    reason: str


class _PartsKept(TypedDict):
    parts: list[PartFile]
    missing: list[Unexported]


class Parts(_PartsKept, total=False):
    """What `export_parts` kept; `leftovers`: copies Drive would not delete, where there are any."""
    leftovers: list[str]


def export_parts(drive: DriveService, slides: SlidesService, pid: str, path: Path) -> Parts:
    """The live deck as .pptx parts beside `path` (`<stem>-slides-001-004.pptx`), for a deck Drive
    will not export whole (`deck_export`): halves first, since the whole was just refused, each
    halved again while it is. A slide no part brought is in `missing`, and then the parts are no
    way back to the whole deck (`way_back_kept`)."""
    from .deck_export import WORKERS as EXPORTS, deck_ids, export_deck
    pres = execute(slides.presentations().get(presentationId=pid, fields="presentationId,slides.objectId"))
    n = len(deck_ids(pres)[1])
    if n < 2:
        return {"parts": [], "missing": [{"slides": [1, n], "reason": "a deck of one slide has no parts"}]}
    done = export_deck(drive, slides, pres, per_part=(n + 1) // 2, workers=EXPORTS, clients=None,
                       only=None)
    parts: list[PartFile] = []
    for part in done.parts:
        file = path.with_name(f"{path.stem}-{part.name}.pptx")
        write_whole(file, part.data)
        parts.append({"file": str(file), "slides": [part.first + 1, part.end], "bytes": len(part.data)})
    kept: Parts = {"parts": parts, "missing": [{"slides": m["slides"], "reason": m["reason"]} for m in done.missing]}
    if done.leftovers:
        kept["leftovers"] = done.leftovers
    return kept


def write_whole(path: Path, data: bytes) -> None:
    """Write a file so that it is either all there or not there: through a `.part` beside it.
    A way back no sync asked for is made on a daemon thread (`WayBack`), and the interpreter's
    exit stops one wherever it is - a half-written backups.json would lose the whole log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)


def copy_in_drive(drive: DriveService, pid: str, name: str | None) -> JsonObject:
    """A Drive copy of the presentation, which stays a full deck with its own URL, in the Backups
    folder (`drive_folder.backup_parents`). `name`: the copy's (None: the deck's own, stamped as a
    backup)."""
    info = execute(drive.files().get(fileId=pid, fields="name,parents"))
    body: FileBody = {"name": name or f"{info.get('name', 'deck')} (beamer2slides backup "
                                      f"{time.strftime('%Y-%m-%d %H:%M')})",
                      "appProperties": {"b2sBackupOf": pid}}
    from .drive_folder import backup_parents
    where = backup_parents(drive, info.get("parents"))
    if where:
        body["parents"] = where
    copy = execute(drive.files().copy(fileId=pid, body=body, fields="id,name"))
    cid = file_id(copy, f"its copy of {pid}")
    return {"presentationId": cid, "name": copy.get("name"), "url": deck_url(cid)}


def _backup_name(drive: DriveService, pid: str, suffix: str) -> str:
    info = execute(drive.files().get(fileId=pid, fields="name"))
    return f"{info.get('name', 'deck')} (beamer2slides backup {time.strftime('%Y-%m-%d %H:%M')}){suffix}"


def hidden_copy(drive: DriveService, pid: str) -> JsonObject:
    """A native copy of the presentation in the app's hidden Drive space (`drive_folder.hidden`
    mode `deck`): no export, so no size limit, and every objectId kept - a deck restored from it
    (`tools/deck_backup.py restore --hidden ID`) is the same deck to sync. Raises HttpError where
    Drive will not take it."""
    from .drive_folder import APPDATA
    body: FileBody = {"name": _backup_name(drive, pid, ""), "parents": [APPDATA],
                      "appProperties": {"b2sBackupOf": pid}}
    copy = execute(drive.files().copy(fileId=pid, body=body, fields="id,name"))
    return {"id": file_id(copy, f"its hidden copy of {pid}"), "name": copy.get("name"), "kind": "deck"}


def hidden_pptx(drive: DriveService, pid: str, exported: Path | None) -> JsonObject:
    """The presentation's .pptx in the app's hidden Drive space (mode `pptx`): `exported`, the file
    this backup already wrote, else a new export (Drive's 10 MB limit). A restore from it is an
    import, which gives the deck new objectIds. Raises HttpError or TimeoutError where Drive will
    not export or take it."""
    from .deck_export import export_bytes
    from .drive_folder import APPDATA
    from .gapi import media_upload
    data = exported.read_bytes() if exported is not None else export_bytes(drive, pid)
    body: FileBody = {"name": _backup_name(drive, pid, ".pptx"), "parents": [APPDATA], "mimeType": PPTX_MIME,
                      "appProperties": {"b2sBackupOf": pid}}
    made = execute(drive.files().create(body=body, fields="id,name",
                                        media_body=media_upload(io.BytesIO(data), PPTX_MIME)))
    return {"id": file_id(made, f"its hidden .pptx of {pid}"), "name": made.get("name"), "kind": "pptx",
            "bytes": len(data)}


def hide_backup(drive: DriveService, pid: str, exported: Path | None, warnings: list[Json]) -> JsonObject | None:
    """The Drive backup kept in the app's hidden space, as the context's hidden-storage mode says
    (`drive_folder.hidden`); None with the mode off, or when the hidden space refused - then a
    warning says why, and the caller keeps the visible copy it always made: a way back is never
    lost over where it is kept."""
    from .drive_folder import hidden
    mode = hidden()
    if mode == "off":
        return None
    try:
        if mode == "deck":
            return hidden_copy(drive, pid)
        if mode == "pptx":
            return hidden_pptx(drive, pid, exported)
        assert_never(mode)
    except (HttpError, TimeoutError, OSError) as e:
        why = api_message(e) if isinstance(e, HttpError) else f"{type(e).__name__}: {e}"
        warnings.append(f"could not keep the backup in the app's hidden Drive storage ({why}); "
                        f"a visible copy is kept instead")
        return None


def _part_json(p: PartFile) -> JsonObject:
    slides: list[Json] = [n for n in p["slides"]]
    return {"file": p["file"], "slides": slides, "bytes": p["bytes"]}


def backup_deck(drive: DriveService, pid: str, out: Path, mode: str | None, note: str, fallback: bool,
                slides: SlidesService | None) -> JsonObject:
    """Keep a way back before a destructive write. mode: none | file | drive | both. `note`: why,
    kept in the result when there is one ("": none).
    `slides`: a Slides client, with which a deck Drive will not export whole (its size, a timeout)
    is kept in .pptx parts instead (`export_parts`; `parts` in the result, no `file`).
    `fallback`: a refused .pptx export (the 10 MB limit) that no parts made up for is answered
    with a Drive copy - what a rebuild wants, while a sync, which only ever rewrites parts, settles
    for the warning. Returns {"file" | "parts", "drive", "warnings"} (paths/ids as strings)."""
    from .deck_export import too_large
    warnings: list[Json] = []
    result: JsonObject = {"mode": mode, "warnings": warnings}
    if mode in ("none", None):
        return result
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if mode in ("file", "both"):
        path = backup_dir(out) / f"{stamp}-{pid[:12]}.pptx"
        try:
            size = export_pptx(drive, pid, path)
            result["file"] = str(path)
            result["bytes"] = size
        except (HttpError, TimeoutError) as e:
            why = api_message(e) if isinstance(e, HttpError) else "the export timed out"
            kept: Parts | None = None
            if slides is not None and too_large(e):
                try:
                    kept = export_parts(drive, slides, pid, path)
                except (HttpError, OSError) as err:
                    warnings.append(f"could not export the deck in parts either ({type(err).__name__}: {err})")
            if kept and kept["parts"]:
                result["parts"] = [_part_json(p) for p in kept["parts"]]
                result["bytes"] = sum(p["bytes"] for p in kept["parts"])
                if kept["missing"]:
                    missing: list[Json] = [[n for n in m["slides"]] for m in kept["missing"]]
                    result["parts_missing"] = missing
            if kept and kept["parts"] and not kept["missing"]:
                warnings.append(f"the deck is too large to export whole ({why}); it was kept in "
                                f"{len(kept['parts'])} .pptx parts, each one restorable on its own")
            else:
                warnings.append(f"could not export the deck as .pptx ({why}; {EXPORT_LIMIT_NOTE})")
                for m in kept["missing"] if kept else []:
                    warnings.append(f"slides {m['slides'][0]}-{m['slides'][1]} are in no part ({m['reason']})")
            if mode == "file" and fallback:
                # never replace a deck's content without a way back to it whole: parts come back as
                # separate presentations, a Drive copy as the deck itself
                mode = "drive"
            leftovers = kept.get("leftovers") if kept else None
            if leftovers:
                warnings.append(f"temporary copies Drive would not delete: {', '.join(leftovers)} "
                                f"(tools/drive_usage.py --delete-staging)")
    if mode in ("drive", "both"):
        made: Json = None
        exported = result.get("file")
        kept_hidden = hide_backup(drive, pid, Path(exported) if isinstance(exported, str) else None, warnings)
        if kept_hidden is not None:
            result["hidden"] = kept_hidden
            made = kept_hidden.get("id")
        else:
            try:
                copy = copy_in_drive(drive, pid, None)
            except HttpError as e:
                warnings.append(f"could not copy the deck in Drive ({api_message(e)})")
            else:
                result["drive"] = copy
                made = copy.get("presentationId")
        if made is not None:
            try:
                trimmed = trim_drive_backups(drive, pid, made if isinstance(made, str) else "")
            except HttpError as e:
                warnings.append(f"could not list the deck's older backup copies ({api_message(e)}); "
                                f"none was moved to the trash")
            else:
                if trimmed.moved:
                    result["trashed"] = [n for n in trimmed.moved]
                warnings += [f"could not move {r} to the trash" for r in trimmed.refused]
    if note:
        result["note"] = note
    return result


class WayBack:
    """The way back a sync keeps before its first write, made while the sync reads and plans.

    `__main__.record_sync_point` is three round trips - the deck's revisionId, its modifiedTime and
    a .pptx export - and it used to run before the sync had made a single call of its own, 3.4 s of
    a 20 s sync with nothing else in the air. None of it reads anything the planning writes, and
    all of it has to be finished before the deck is first written to: so it goes on a thread here
    and `sync` collects it at that one point (`Sync.before_write`), which is where the promise is.

    Worth about a second, not the 3.4 s it costs, and that gap is the measurement's whole point:
    what it now runs beside is the source's own conversion and the deck's first read, which want
    this machine and this connection too. Interleaved A/B on a 13-frame talk, nine pairs: 18.97 s
    against 18.00 s over the last six (five of six favouring the thread), 0.60 s over all nine (six
    of nine). Small enough that only the pairs say it at all - the same unchanged sync runs 16.5 to
    21.7 s with the day - and it is kept for the second reason as much as the first: `sync` is the
    one that knows when the first write happens, so handing the note over is also what lets an
    adopted deck's first sync ask whether a way back was kept, instead of being refused although one
    was (`agent.deck_tools.deck_sync` never passed it at all).

    Where a caller lent its own client there is no thread - a service object is that caller's and
    belongs to one thread at a time (`emit.measure_places`' rule) - and the work happens on the
    first ask, which is the order it always ran in. `result` never raises: a missing recovery note
    is no reason not to sync, and it is asked for in places that are about to write.

    A sync that writes nothing never asks (`asked` stays False), and its caller does not wait for
    a way back it did not need (`kept`): on a deck in Noto Sans SC the export alone is two minutes
    (`gslides.SLOW_EXPORT`), which every no-op sync used to spend. So the thread is a daemon, the
    interpreter's exit does not join it, and what it writes goes down whole or not at all
    (`write_whole`)."""

    def __init__(self, fn: Callable[[SlidesService, DriveService], JsonObject | None], name: str):
        """`fn` makes the note ({"out", "entry"}: `__main__.sync_point`); `name`: its thread's."""
        from .google_auth import credentials_for_threads, drive_service, shared_service, slides_service

        self.note: JsonObject | None = None
        self.asked = False
        self.job: Future[JsonObject | None] | None = None
        self.make: Callable[[], JsonObject | None] = lambda: fn(slides_service(None), drive_service(None))
        if shared_service("slides", "v1") or shared_service("drive", "v3"):
            return
        try:
            creds = credentials_for_threads()   # resolved here: a worker inherits no context
        except Exception:  # noqa: BLE001 (no token: the old order, which says so where it fails)
            return
        from .drive_folder import hidden, spec, use_folder, use_hidden
        where, hide = spec(), hidden()   # (so is where a `--backup drive` copy goes)

        def make() -> JsonObject | None:
            with use_folder(where), use_hidden(hide):
                return fn(slides_service(creds), drive_service(creds))

        self.make = make
        job: Future[JsonObject | None] = Future()
        self.job = job

        def run() -> None:
            if job.set_running_or_notify_cancel():
                try:
                    job.set_result(make())
                except BaseException as e:  # noqa: BLE001 (handed to whoever asks, as a pool would)
                    job.set_exception(e)

        threading.Thread(target=run, name=name, daemon=True).start()

    def kept(self) -> JsonObject | None:
        """The recovery note if the sync asked for one - that is, if it wrote - else None, without
        waiting for a way back nobody needed."""
        return self.note if self.asked else None

    def result(self) -> JsonObject | None:
        """The recovery note, made once and remembered."""
        if not self.asked:
            self.asked = True
            try:
                self.note = self.job.result() if self.job is not None else self.make()
            except Exception as e:  # noqa: BLE001 (a missing recovery note is no reason not to sync)
                print(f"warning: could not record the deck's revision before syncing "
                      f"({type(e).__name__}: {e})")
            self.job = None
        return self.note

    def backup(self) -> JsonObject:
        """What was kept, for the refusal that asks whether there is any way back at all."""
        return _object(_object(_object(self.result()).get("entry")).get("backup"))


def way_back_kept(backup: JsonObject) -> bool:
    """Whether this backup can actually be put back: a .pptx file that is there and not empty, or
    .pptx parts that are all there and hold every slide (`export_parts`), or a Drive copy.
    `backup_deck` only warns when Drive refuses the export or the copy. A copy or .pptx in the app's
    hidden space (`hidden`) is one too."""
    if backup.get("drive") or backup.get("hidden"):
        return True

    def there(name: Json) -> bool:
        path = Path(name) if isinstance(name, str) and name else None
        return bool(path and path.exists() and path.stat().st_size > 0)
    parts = _array(backup.get("parts"))
    if parts and not backup.get("parts_missing"):
        return all(there(_object(p).get("file")) for p in parts)
    return there(backup.get("file"))


def demand_way_back(pid: str, out: Path, pdf: Path | str | None, entry: JsonObject, mode: str | None) -> None:
    """Refuse a forced rebuild whose backup did not happen.

    A forced rebuild replaces the content of a deck someone edited, and the offer that makes that
    acceptable is the backup. When it could not be kept - the export refused (over 10 MB), the
    Drive copy refused (quota, a full Drive), no room on disk - the rebuild must not happen either:
    nothing else brings that content back, because every Drive revision of a Slides file exports
    the file's *current* content (docs/sync.md, tools/probe_revision_history.py).
    `--backup none` is how one says out loud that the deck may go."""
    backup = _object(entry.get("backup"))
    if mode in ("none", None) or way_back_kept(backup):
        return
    lines = ["refusing to rebuild: the backup that makes a forced rebuild safe could not be kept, "
             "and a rebuild replaces the whole deck.",
             f"  {deck_url(pid)}"]
    lines += [f"  {w}" for w in _array(backup.get("warnings"))] or ["  no backup file was written"]
    lines.append("  What to do instead:")
    if entry.get("reason") == "edited":
        lines.append(f"    merge the PDF into the deck, keeping the edits:  {command_line('sync', pdf, out, '')}")
    lines.append(f"    leave that deck alone and make a new one:        {command_line('convert', pdf, out, ' --new-deck')}")
    lines.append(f"    try the other backup:                            "
                 f"{command_line('convert', pdf, out, ' --force-rebuild --backup drive')}")
    lines.append(f"    rebuild with no way back (says it out loud):     "
                 f"{command_line('convert', pdf, out, ' --force-rebuild --backup none')}")
    raise RebuildRefused("\n".join(lines), {**entry, "reason": "backup-failed"})


def api_message(e: HttpError) -> str:
    return message_of(e)


def record(out: Path, entry: JsonObject) -> Path:
    """Append one line to <out>/backups/backups.json: what the deck was before this write."""
    path = backup_dir(out) / "backups.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    log: list[Json] = []
    if path.exists():
        try:
            written: Json = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            written = []
        if not isinstance(written, list):
            # (appending to it would lose it: a log that is not one is no log to write over)
            raise ValueError(f"{path} is not a list of backups")
        log = written
    log.append(entry)
    write_whole(path, json.dumps(log, indent=1, ensure_ascii=False).encode("utf-8"))
    return path


def read_log(out: Path) -> list[Json]:
    """backups.json's lines as written (a line that is not an object is left for its reader)."""
    path = backup_dir(out) / "backups.json"
    try:
        log: Json = json.loads(path.read_text(encoding="utf-8"))
        return log if isinstance(log, list) else []
    except (OSError, ValueError):
        return []


def backup_files(entries: Sequence[Json]) -> list[tuple[Path, JsonObject]]:
    """The .pptx files the log says this program wrote, in the order they were written. Nothing
    else in the folder is ever a candidate for deletion: a file someone put there is theirs."""
    found: list[tuple[Path, JsonObject]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        backup = _object(e.get("backup"))
        for name in [backup.get("file")] + [_object(p).get("file") for p in _array(backup.get("parts"))]:
            if isinstance(name, str) and name and Path(name).exists():
                found.append((Path(name), e))
    return found


def prune_backups(out: Path, keep: int, older_than_days: float | None, delete: bool) -> JsonObject:
    """Which backup files of this output folder are past what it keeps, and delete them when asked.

    Every sync writes a .pptx of the deck before its first write, so a folder synced often grows
    without end. The newest `keep` are kept, and with `older_than_days` so is everything younger
    than that. The log entry of a deleted file stays, with `backup.deleted`: what the deck was, and
    when, is worth keeping as evidence even when the way back is not."""
    entries = read_log(out)
    files = backup_files(entries)
    backups = list({id(e): e for _, e in files}.values())   # (a backup in parts is one backup)
    kept_entries = {id(e) for e in (backups[-keep:] if keep else [])}
    doomed = [(p, e) for p, e in files if id(e) not in kept_entries]
    if older_than_days is not None:
        cutoff = time.time() - older_than_days * 86400
        doomed = [(p, e) for p, e in doomed if p.stat().st_mtime < cutoff]
    listed: list[Json] = [{"file": str(p), "bytes": p.stat().st_size, "entry": e} for p, e in doomed]
    result: JsonObject = {"files": len(files), "bytes": sum(p.stat().st_size for p, _ in files), "doomed": listed,
                          "freed": sum(p.stat().st_size for p, _ in doomed), "deleted": False}
    if delete and doomed:
        for p, e in doomed:
            p.unlink()
            backup = e.get("backup")   # (an object: backup_files found the file there)
            if not isinstance(backup, dict):
                backup = {}
                e["backup"] = backup
            backup["deleted"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (backup_dir(out) / "backups.json").write_text(json.dumps(entries, indent=1, ensure_ascii=False),
                                                      encoding="utf-8")
        result["deleted"] = True
    return result


def drive_backups(drive: DriveService, pid: str) -> list[DriveFile]:
    """The Drive copies `copy_in_drive` made of this presentation, oldest first. Found by their
    `b2sBackupOf` tag, so a copy somebody made by hand is never among them (and drive.file only
    lists what this app made anyway). With hidden storage on, those in the app's hidden space too
    (`hidden_copy`, `hidden_pptx`): listing that space needs the scope the mode asks for."""
    from .drive_folder import APPDATA, hidden
    found: list[DriveFile] = []
    token: str | None = None
    query = (f"appProperties has {{ key='b2sBackupOf' and value='{pid}' }} and trashed = false")
    spaces = "drive" if hidden() == "off" else f"drive,{APPDATA}"
    while True:
        r = execute(drive.files().list(q=query, spaces=spaces, pageSize=100, pageToken=token,
                                       fields="nextPageToken,files(id,name,createdTime,mimeType,spaces)"))
        found += r.get("files", [])
        token = r.get("nextPageToken")
        if not token:
            return sorted(found, key=lambda f: f.get("createdTime", ""))


def prune_drive_backups(drive: DriveService, pid: str, keep: int, older_than_days: float | None,
                        trash: bool) -> JsonObject:
    """`prune_backups` for the Drive copies: every sync with `backup="drive"` (a detached agent
    context's `auto`) leaves one, so they accumulate like the .pptx files do. The newest `keep`
    are kept, and with `older_than_days` everything younger; the rest go to Drive's **trash**
    when asked - never a permanent delete, so a copy pruned by mistake comes back from there for
    30 days."""
    copies = drive_backups(drive, pid)
    doomed = copies[:-keep] if keep else list(copies)
    if older_than_days is not None:
        # datetime, not time.gmtime: Windows refuses a negative timestamp (a cutoff before 1970).
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%S")
        doomed = [c for c in doomed if c.get("createdTime", "") < cutoff]
    warnings: list[Json] = []
    listed: list[Json] = [as_json(c, "a Drive backup") for c in doomed]
    result: JsonObject = {"copies": len(copies), "doomed": listed, "trashed": False, "warnings": warnings}
    if trash and doomed:
        warnings += [f"could not move {r} to the trash" for r in trash_copies(drive, doomed).refused]
        result["trashed"] = True
    return result


DRIVE_BACKUPS_KEPT = 3
"""How many Drive copies of one deck a backup leaves: the one it made and the two before it. Every
sync whose way back is a Drive copy (a detached agent's `auto`, `--backup drive`) made one more full
deck, and a person searching Drive for their talk found it once per sync; the older ones now go to
Drive's trash as each new one is made, where they stay restorable for 30 days."""


@dataclass(frozen=True, kw_only=True)
class Trashed:
    """What moving Drive copies to the trash did: the names of those moved, and of those Drive
    refused, each with why."""
    moved: list[str]
    refused: list[str]


def trash_copies(drive: DriveService, copies: Sequence[DriveFile]) -> Trashed:
    """Move `copies` to Drive's trash - never a permanent delete."""
    moved: list[str] = []
    refused: list[str] = []
    for c in copies:
        cid = c.get("id")
        if cid is None:   # (listed with `files(id,...)`: never so)
            continue
        name = c.get("name", cid)
        try:
            execute(drive.files().update(fileId=cid, body={"trashed": True}, fields="id"))
            moved.append(name)
        except HttpError as e:
            refused.append(f"{name} ({api_message(e)})")
    return Trashed(moved=moved, refused=refused)


def trim_drive_backups(drive: DriveService, pid: str, made: str) -> Trashed:
    """After `copy_in_drive` made `made`: the deck's older copies past `DRIVE_BACKUPS_KEPT` go to
    the trash. The new copy is never one of them, even when Drive's listing does not show it yet
    (then one more old copy stays, never one fewer)."""
    older = [c for c in drive_backups(drive, pid) if c.get("id") != made]
    return trash_copies(drive, older[:max(0, len(older) - (DRIVE_BACKUPS_KEPT - 1))])


def restore_hint(entry: JsonObject, what: str) -> list[str]:
    """What to print (and to put in a report) so the person can get the old deck back.

    The .pptx backup is the way back that was measured to work: Drive keeps a revision row per
    editing session of a converted deck, but every revision's export gives the file's *current*
    content, so `files.update` leaves nothing the API can fetch (tools/probe_revision_history.py,
    docs/sync.md). Version history in the Slides UI is worth a try, never a promise."""
    lines: list[str] = []
    rev, pid, modified = entry.get("revisionId"), entry.get("presentationId"), entry.get("modifiedTime")
    backup = _object(entry.get("backup"))
    file, parts, drive = backup.get("file"), _array(backup.get("parts")), backup.get("drive")
    if rev:
        lines.append(f"  the deck before this {what} was revision {rev}"
                     f"{f' (Drive revision at {modified})' if modified else ''}")
    if file:
        lines.append(f"  backup: {file}")
        lines.append(f"    put it back with: python tools/deck_backup.py restore --deck {entry.get('out', '<out folder>')} "
                     f"--from \"{file}\"")
    if parts:
        lines.append(f"  backup in {len(parts)} parts (the deck was too large to export whole):")
        for p in parts:
            first, last = (_array(_object(p).get("slides")) + [None, None])[:2]
            lines.append(f"    slides {first}-{last}: {_object(p).get('file')}")
        lines.append("    each one restores as a presentation of its own (python tools/deck_backup.py restore "
                     "--deck ... --from PART); Slides' File > Import slides puts them together")
    if drive:
        lines.append(f"  backup copy in Drive: {_object(drive).get('url')}")
    hidden = _object(backup.get("hidden"))
    if hidden:
        kind = "a copy of the deck" if hidden.get("kind") == "deck" else "the deck's .pptx"
        lines.append(f"  backup in the app's hidden Drive storage: {kind}, {hidden.get('name')}")
        lines.append(f"    put it back with: python tools/deck_backup.py restore --deck {entry.get('out', '<out folder>')} "
                     f"--hidden {hidden.get('id')}")
    trashed = _array(backup.get("trashed"))
    if trashed:
        lines.append(f"  older backup copies moved to Drive's trash (the newest {DRIVE_BACKUPS_KEPT} are kept; "
                     f"the trash keeps them 30 days): {', '.join(str(t) for t in trashed)}")
    for w in _array(backup.get("warnings")):
        lines.append(f"  warning: {w}")
    if pid and not file and not parts and not drive and not hidden:
        lines.append("  no backup file was kept (--backup file|drive|both keeps one); Drive's version history "
                     "cannot be read back through the API (docs/sync.md)")
    return lines
