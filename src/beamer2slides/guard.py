"""Guarding a live deck against an accidental rebuild (docs/sync.md, "Never lose deck edits").

`convert` on an output folder that already has a deck replaces that deck's whole content
(`files.update`). If someone edited it in Slides, the edits are gone. This module asks the same
question `sync` asks - what did the person change since the converter wrote this deck? - against
the sync base (`<out>/sync/base.json`, or Drive `appProperties.b2sBase`), and refuses the rebuild
when the answer isn't "nothing".

What counts as an edit is `merge.deck_edits` / `merge.user_objects` / `merge.slide_touched`, i.e.
exactly what sync would keep:
  - a new `revisionId` is not an edit (Google bumps it on its own, e.g. when a deck is opened),
  - a new `contentUrl` for the same picture is not an edit (pixel signatures decide, `snapshot`),
  - a thumbnail export is not an edit,
  - `measure_places`' scratch slides (`b2s_mNNN`) left by an interrupted run are not an edit.

Before a destructive write the deck's `revisionId` is recorded (`<out>/backups/backups.json`,
`emit.json` "previous") and a backup can be kept: an exported .pptx next to the output folder
and/or a Drive copy of the presentation.
"""

import io
import json
import re
import time
from pathlib import Path

from googleapiclient.errors import HttpError

from . import merge, snapshot
from .gslides import execute

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
    "group": "group taken apart",
    "deleted": "deleted",
    "part_deleted": "partly deleted",
}
EXAMPLES = 3


class RebuildRefused(Exception):
    """Raised instead of replacing a deck that must not be replaced. `survey` is the finding."""

    def __init__(self, message: str, survey: dict):
        super().__init__(message)
        self.survey = survey


def deck_url(pid: str) -> str:
    return f"https://docs.google.com/presentation/d/{pid}/edit"


# ---------------------------------------------------------------- the previous deck


def previous_deck(drive, out: Path) -> dict | None:
    """What the output folder's emit.json points at: {"presentationId", "state", "name",
    "modifiedTime"}. state: "live", "trashed", "gone" (deleted or not ours any more),
    "other" (not a presentation)."""
    state_file = out / "emit.json"
    if not state_file.exists():
        return None
    try:
        pid = json.loads(state_file.read_text(encoding="utf-8"))["presentationId"]
    except (ValueError, KeyError):
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


def sign_changed(base: dict, theirs: dict, pres: dict) -> None:
    """Pixel signatures for the live pictures whose contentUrl differs from the base's. Google
    issues new URLs for pictures nobody touched, so only the pixels tell a replaced one apart
    (sync.Sync.sign_changed does the same before planning)."""
    images = {oid: rb["image"] for s in base["slides"] for e in s["elements"]
              for oid, rb in e.get("readback", {}).items() if "image" in rb}
    backgrounds = {s.get("objectId"): s.get("background_readback") or {} for s in base["slides"]}
    objects, slides = set(), set()
    for s in theirs["slides"]:
        for oid, rb in s["objects"].items():
            if "image" in rb and oid in images and images[oid].get("contentHash") != rb["image"].get("contentHash"):
                objects.add(oid)
        bg, old = s.get("background") or {}, backgrounds.get(s["objectId"], {})
        if "picture" in bg and "picture" in old and old["picture"] != bg["picture"]:
            slides.add(s["objectId"])
    if objects or slides:
        snapshot.sign_pictures(theirs, pres, objects, slides)


def _snippet(text: str | None, length: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[:length - 1] + "…"


def survey(base: dict, pres: dict, sign: bool = True) -> dict:
    """What the person changed in the live deck since the base was recorded.

    {"edited": bool, "revisionId", "counts": {field/kind: n}, "slides": [{"slide", "why", "edits"}],
     "slides_added", "slides_deleted", "reordered", "examples": [readable lines]}"""
    pres = {**pres, "slides": [s for s in pres.get("slides", []) if not SCRATCH.fullmatch(s["objectId"])]}
    theirs = snapshot.read_presentation(pres)
    if sign:
        sign_changed(base, theirs, pres)
    live = {s["objectId"]: s for s in theirs["slides"]}
    titles = {s.get("objectId"): (s.get("title") or s.get("key")) for s in base["slides"]}
    counts: dict[str, int] = {}
    slides, examples = [], []
    for n, b in enumerate(base["slides"], start=1):
        read = live.get(b.get("objectId"))
        if read is None:
            counts["slides_deleted"] = counts.get("slides_deleted", 0) + 1
            slides.append({"slide": b["key"], "page": n, "why": ["slide deleted"], "edits": []})
            examples.append(f"slide {n} \"{_snippet(titles.get(b.get('objectId')), 30)}\": deleted in Slides")
            continue
        entry = {"slide": b["key"], "page": n, "why": [], "edits": []}
        for el in b["elements"]:
            for field, oids in merge.deck_edits(el, read).items():
                counts[field] = counts.get(field, 0) + 1
                entry["edits"].append({"element": el["key"], "field": field, "objects": oids})
                if len(examples) < EXAMPLES:
                    what = FIELD_WORDS.get(field, field)
                    shown = read["objects"].get(oids[0], {}).get("text") if field == "text" else None
                    examples.append(f"slide {n} \"{_snippet(titles.get(b.get('objectId')), 30)}\": {what}"
                                    f" ({el['key']}{f': “{_snippet(shown)}”' if shown else ''})")
        added = merge.user_objects(b, read)
        if added:
            counts["objects_added"] = counts.get("objects_added", 0) + len(added)
            entry["why"].append(f"{len(added)} object(s) added")
            if len(examples) < EXAMPLES:
                examples.append(f"slide {n} \"{_snippet(titles.get(b.get('objectId')), 30)}\": "
                                f"{len(added)} object(s) added in Slides")
        if b.get("notes_readback", "") != read.get("notes", ""):
            counts["notes"] = counts.get("notes", 0) + 1
            entry["why"].append("notes edited")
            if len(examples) < EXAMPLES:
                examples.append(f"slide {n} \"{_snippet(titles.get(b.get('objectId')), 30)}\": speaker notes edited")
        if merge.background_edited(b, read):
            counts["background"] = counts.get("background", 0) + 1
            entry["why"].append("background changed")
            if len(examples) < EXAMPLES:
                examples.append(f"slide {n} \"{_snippet(titles.get(b.get('objectId')), 30)}\": background changed")
        if entry["edits"]:
            entry["why"].insert(0, f"{len(entry['edits'])} element edit(s)")
        if entry["why"]:
            slides.append(entry)
    base_ids = {b.get("objectId") for b in base["slides"]}
    extra = [s["objectId"] for s in theirs["slides"] if s["objectId"] not in base_ids]
    if extra:
        counts["slides_added"] = len(extra)
        if len(examples) < EXAMPLES:
            examples.append(f"{len(extra)} slide(s) added in Slides")
    # Reorder: the base slides that are still there, in the live order.
    live_order = [s["objectId"] for s in theirs["slides"] if s["objectId"] in base_ids]
    base_order = [b["objectId"] for b in base["slides"] if b.get("objectId") in set(live_order)]
    reordered = live_order != base_order
    if reordered:
        counts["slides_reordered"] = 1
        if len(examples) < EXAMPLES:
            examples.append("the slides were reordered in Slides")
    return {"edited": bool(slides or extra or reordered), "revisionId": theirs.get("revisionId"),
            "counts": counts, "slides": slides, "slides_added": len(extra),
            "slides_deleted": counts.get("slides_deleted", 0), "reordered": reordered, "examples": examples}


SUMMARY_WORDS = {"text": "text edit", "text_style": "style change", "geometry": "move or resize",
                 "shape_style": "fill or outline change", "image": "picture replaced", "group": "group taken apart",
                 "deleted": "element deleted", "part_deleted": "element partly deleted", "notes": "notes edit",
                 "background": "background change", "objects_added": "object added in Slides",
                 "slides_added": "slide added", "slides_deleted": "slide deleted"}


def summary_line(found: dict) -> str:
    """"3 slides edited: 2 text edits, 1 object added in Slides, slides reordered"."""
    parts = [f"{n} {SUMMARY_WORDS[k]}{'s' if n > 1 and not SUMMARY_WORDS[k].endswith('ed') else ''}"
             for k, n in found["counts"].items() if k in SUMMARY_WORDS and n]
    if found["reordered"]:
        parts.append("slides reordered")
    slides = len(found["slides"])
    head = f"{slides} slide{'s' if slides != 1 else ''} edited" if slides else "the deck was edited"
    return f"{head}: {', '.join(parts)}" if parts else head


# ---------------------------------------------------------------- the check


def command_line(command: str, pdf: Path | str | None, out: Path, extra: str = "") -> str:
    pdf = str(pdf) if pdf else "<pdf>"
    return f"python -m beamer2slides {command} {pdf} {'--deck' if command == 'sync' else '--out'} {out}{extra}"


def refusal_message(pid: str, out: Path, pdf: Path | str | None, found: dict, reason: str) -> str:
    lines = []
    if reason == "edited":
        lines.append("refusing to rebuild: this deck was edited in Google Slides after beamer2slides wrote it.")
        lines.append(f"  {deck_url(pid)}")
        lines.append(f"  {summary_line(found)}")
        lines += [f"    - {e}" for e in found["examples"]]
    elif reason == "no-base":
        lines.append("refusing to rebuild: there is no sync base for this deck, so whether someone "
                     "edited it cannot be checked.")
        lines.append(f"  {deck_url(pid)}")
        lines.append("  (the base is written by convert; an older deck, or a convert whose base "
                     "recording failed, has none)")
    elif reason == "other-source":
        lines.append(f"refusing to rebuild: the deck in {out} was converted from "
                     f"{found.get('base_source')}, not from {pdf}.")
        lines.append(f"  {deck_url(pid)}")
        lines.append("  rebuilding it here would replace that deck with this PDF's slides.")
    lines.append("  A rebuild replaces the whole deck. What to do instead:")
    if reason == "edited":
        lines.append(f"    merge the PDF into the deck, keeping the edits:  {command_line('sync', pdf, out)}")
    lines.append(f"    leave that deck alone and make a new one:        {command_line('convert', pdf, out, ' --new-deck')}")
    lines.append(f"    rebuild anyway (the deck's content is replaced): {command_line('convert', pdf, out, ' --force-rebuild')}")
    if found.get("revisionId"):
        lines.append(f"  The deck is at revision {found['revisionId']}; a forced rebuild keeps a backup "
                     "first (--backup, docs/sync.md).")
    return "\n".join(lines)


def check_rebuild(slides, drive, pid: str, out: Path, pdf: Path | str | None = None, force: bool = False,
                  base: dict | None = None) -> dict:
    """Look at the live deck before replacing its content. Returns the finding
    ({"reason", "revisionId", ...}); raises RebuildRefused unless `force`."""
    if base is None:
        base, where = snapshot.load_base(pid, out, drive)
    else:
        where = "given"
    pres = execute(slides.presentations().get(presentationId=pid))
    found = {"presentationId": pid, "revisionId": pres.get("revisionId"), "base_from": where,
             "checked": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": "", "edited": False, "examples": [],
             "counts": {}, "slides": [], "slides_added": 0, "slides_deleted": 0, "reordered": False}
    if base is None:
        found["reason"] = "no-base"
    else:
        found.update(survey(base, pres))
        found["base_generation"] = base.get("generation", 0)
        found["base_source"] = Path(base.get("source", {}).get("pdf") or "").name or None
        if found["edited"]:
            found["reason"] = "edited"
        elif pdf is not None and found["base_source"] and Path(pdf).name != found["base_source"]:
            found["reason"] = "other-source"
    if found["reason"] and not force:
        raise RebuildRefused(refusal_message(pid, out, pdf, found, found["reason"]), found)
    return found


# ---------------------------------------------------------------- backups


def backup_dir(out: Path) -> Path:
    return out / "backups"


def export_pptx(drive, pid: str, path: Path) -> int:
    """The live deck as a .pptx next to the output folder. Drive refuses files.export over 10 MB."""
    data = execute(drive.files().export_media(fileId=pid, mimeType=PPTX_MIME))
    data = data.getvalue() if isinstance(data, io.BytesIO) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def copy_in_drive(drive, pid: str, name: str | None = None) -> dict:
    """A Drive copy of the presentation, which stays a full deck with its own URL."""
    info = execute(drive.files().get(fileId=pid, fields="name,parents"))
    body = {"name": name or f"{info.get('name', 'deck')} (beamer2slides backup "
                            f"{time.strftime('%Y-%m-%d %H:%M')})",
            "appProperties": {"b2sBackupOf": pid}}
    if info.get("parents"):
        body["parents"] = info["parents"]
    copy = execute(drive.files().copy(fileId=pid, body=body, fields="id,name"))
    return {"presentationId": copy["id"], "name": copy.get("name"), "url": deck_url(copy["id"])}


def backup_deck(drive, pid: str, out: Path, mode: str, note: str = "", fallback: bool = True) -> dict:
    """Keep a way back before a destructive write. mode: none | file | drive | both.
    `fallback`: a refused .pptx export (the 10 MB limit) is answered with a Drive copy - what a
    rebuild wants, while a sync, which only ever rewrites parts, settles for the warning.
    Returns {"file", "drive", "warnings"} (paths/ids as strings)."""
    result: dict = {"mode": mode, "warnings": []}
    if mode in ("none", None):
        return result
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if mode in ("file", "both"):
        path = backup_dir(out) / f"{stamp}-{pid[:12]}.pptx"
        try:
            size = export_pptx(drive, pid, path)
            result["file"] = str(path)
            result["bytes"] = size
        except HttpError as e:
            result["warnings"].append(f"could not export the deck as .pptx ({api_message(e)}; {EXPORT_LIMIT_NOTE})")
            if mode == "file" and fallback:
                mode = "drive"  # never replace a deck's content without any way back
    if mode in ("drive", "both"):
        try:
            result["drive"] = copy_in_drive(drive, pid)
        except HttpError as e:
            result["warnings"].append(f"could not copy the deck in Drive ({api_message(e)})")
    if note:
        result["note"] = note
    return result


def api_message(e: HttpError) -> str:
    try:
        return json.loads(e.content)["error"]["message"][:200]
    except (ValueError, KeyError, TypeError):
        return str(e)[:200]


def record(out: Path, entry: dict) -> Path:
    """Append one line to <out>/backups/backups.json: what the deck was before this write."""
    path = backup_dir(out) / "backups.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    log = []
    if path.exists():
        try:
            log = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log = []
    log.append(entry)
    path.write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def restore_hint(entry: dict, what: str = "rebuild") -> list[str]:
    """What to print (and to put in a report) so the person can get the old deck back.

    The .pptx backup is the way back that was measured to work: Drive keeps a revision row per
    editing session of a converted deck, but every revision's export gives the file's *current*
    content, so `files.update` leaves nothing the API can fetch (tools/probe_revision_history.py,
    docs/sync.md). Version history in the Slides UI is worth a try, never a promise."""
    lines = []
    rev, pid = entry.get("revisionId"), entry.get("presentationId")
    backup = entry.get("backup") or {}
    if rev:
        lines.append(f"  the deck before this {what} was revision {rev}"
                     f"{' (Drive revision at ' + entry['modifiedTime'] + ')' if entry.get('modifiedTime') else ''}")
    if backup.get("file"):
        lines.append(f"  backup: {backup['file']}")
        lines.append(f"    put it back with: python tools/deck_backup.py restore --deck {entry.get('out', '<out folder>')} "
                     f"--from \"{backup['file']}\"")
    if backup.get("drive"):
        lines.append(f"  backup copy in Drive: {backup['drive']['url']}")
    for w in backup.get("warnings", []):
        lines.append(f"  warning: {w}")
    if pid and not backup.get("file") and not backup.get("drive"):
        lines.append("  no backup file was kept (--backup file|drive|both keeps one); Drive's version history "
                     "cannot be read back through the API (docs/sync.md)")
    return lines
