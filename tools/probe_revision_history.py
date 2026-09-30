"""What Drive keeps when a presentation's content is replaced by `files.update` (what `convert`
does when it rebuilds a deck in place), and what of it can be restored through the API.

    python tools/probe_revision_history.py [--wait 30] [--keep]

The probe works on decks it creates itself and deletes again (nothing of yours is touched):

  1. a .pptx is uploaded as a Google Slides deck        (marker ALPHA)
  2. a text box is added through the Slides API         (marker EDIT - "the person edits the deck")
  3. the file's content is replaced with another .pptx  (marker BETA - "convert rebuilds the deck")
  4. `revisions.list` is read and every revision exported as .pptx: which markers does it hold?
  5. `revisions.update(keepForever=True)` is tried on the revision that holds the edit
  6. the revision holding the edit is uploaded again as a new presentation: is the edit back?

Findings go to docs/sync.md. Run it twice with different --wait to see how Drive groups changes
into revisions.
"""

import argparse
import io
import json
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Protocol, TypedDict, runtime_checkable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beamer2slides.gapi import HttpError, media_upload  # noqa: E402
from beamer2slides.google_auth import drive_service, slides_service  # noqa: E402
from beamer2slides.google_types import Request, file_id, json_object, object_id, part  # noqa: E402
from beamer2slides.gslides import execute  # noqa: E402
from beamer2slides.json_types import Json, JsonObject, as_str  # noqa: E402
from tools.deck_backup import download, revisions  # noqa: E402

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
SLIDES_MIME = "application/vnd.google-apps.presentation"


# Drive's `revisions` resource, which google_types' DriveService does not list yet: described
# here, after a runtime check, until it does.
class Revision(TypedDict, total=False):
    id: str
    modifiedTime: str
    keepForever: bool
    exportLinks: dict[str, str]


class RevisionBody(TypedDict, total=False):
    keepForever: bool


class Revisions(Protocol):
    def get(self, *, fileId: str, revisionId: str, fields: str) -> Request[Revision]: ...
    def update(self, *, fileId: str, revisionId: str, body: RevisionBody, fields: str) -> Request[Revision]: ...


@runtime_checkable
class DriveRevisions(Protocol):
    """The Drive client, as far as reading and keeping a revision calls it."""

    def revisions(self) -> Revisions: ...


def with_revisions(drive: object) -> DriveRevisions:
    if not isinstance(drive, DriveRevisions):
        raise TypeError(f"{type(drive).__name__} is no Drive client")
    return drive


def pptx_with(text: str) -> io.BytesIO:
    from pptx import Presentation
    from pptx.util import Pt
    pres = Presentation()
    slide = pres.slides.add_slide(pres.slide_layouts[6])
    box = slide.shapes.add_textbox(Pt(50), Pt(50), Pt(400), Pt(60))
    box.text_frame.text = text
    buf = io.BytesIO()
    pres.save(buf)
    buf.seek(0)
    return buf


def markers_in(data: bytes, markers: dict[str, str]) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        blob = b"".join(z.read(n) for n in z.namelist() if n.endswith(".xml"))
    return [name for name, text in markers.items() if text.encode() in blob]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=30, help="seconds between the steps")
    ap.add_argument("--keep", action="store_true", help="leave the probe decks in Drive")
    args = ap.parse_args()
    wait: float = args.wait
    keep: bool = args.keep
    drive, slides = drive_service(None), slides_service(None)
    tag = uuid.uuid4().hex[:8].upper()
    markers = {"ALPHA": f"ALPHA-{tag}", "EDIT": f"EDIT-{tag}", "BETA": f"BETA-{tag}"}
    steps: list[Json] = []
    report: JsonObject = {"tag": tag, "wait": wait, "steps": steps}
    made: list[str] = []

    pid = file_id(execute(drive.files().create(body={"name": f"b2s revision probe {tag}", "mimeType": SLIDES_MIME},
                                               media_body=media_upload(pptx_with(markers["ALPHA"]), PPTX_MIME),
                                               fields="id")), "the probe deck")
    made.append(pid)
    report["presentationId"] = pid
    steps.append({"step": "upload", "at": time.strftime("%H:%M:%S"), "marker": "ALPHA"})
    try:
        time.sleep(wait)
        page = object_id(execute(slides.presentations().get(presentationId=pid)).get("slides", [])[0])
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [
            {"createShape": {"objectId": f"probe{tag}", "shapeType": "TEXT_BOX", "elementProperties": {
                "pageObjectId": page, "size": {"width": {"magnitude": 300, "unit": "PT"},
                                               "height": {"magnitude": 40, "unit": "PT"}},
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": 60, "translateY": 200, "unit": "PT"}}}},
            {"insertText": {"objectId": f"probe{tag}", "text": markers["EDIT"]}}]}))
        edited_at = execute(drive.files().get(fileId=pid, fields="modifiedTime")).get("modifiedTime")
        if edited_at is None:
            raise ValueError("Drive answered no modifiedTime for the probe deck")
        steps.append({"step": "api edit", "at": time.strftime("%H:%M:%S"), "marker": "EDIT",
                      "modifiedTime": edited_at})
        time.sleep(wait)

        execute(drive.files().update(fileId=pid, media_body=media_upload(pptx_with(markers["BETA"]), PPTX_MIME),
                                     fields="id"))
        steps.append({"step": "files.update (the rebuild)", "at": time.strftime("%H:%M:%S"), "marker": "BETA"})
        time.sleep(wait)

        revs = revisions(drive, pid)
        entries: list[Json] = []
        report["revisions"] = entries
        holds_edit: list[str] = []          # the ids of the revisions whose export holds the edit
        for r in revs:
            rev = json_object(r, "a revision")
            rev_id = as_str(rev.get("id"), "a revision's id")
            links = part(rev.get("exportLinks"), "a revision's exportLinks")
            entry: JsonObject = {"id": rev_id,
                                 "modifiedTime": as_str(rev.get("modifiedTime"), "a revision's modifiedTime"),
                                 "keepForever": rev.get("keepForever"), "pptx_export": PPTX_MIME in links}
            if PPTX_MIME in links:
                data = download(as_str(links[PPTX_MIME], "a revision's .pptx export link"))
                found = markers_in(data, markers)
                held: list[Json] = [*found]
                entry["markers"] = held
                entry["bytes"] = len(data)
                if "EDIT" in found:
                    holds_edit.append(rev_id)
            entries.append(entry)
            time.sleep(1.5)  # the export endpoint rate-limits

        report["edit_survives_the_rebuild"] = bool(holds_edit)
        if holds_edit:
            rid = holds_edit[-1]
            drive_revisions = with_revisions(drive).revisions()
            try:
                kept = execute(drive_revisions.update(fileId=pid, revisionId=rid, body={"keepForever": True},
                                                      fields="id,keepForever"))
                report["keepForever"] = {"accepted": True, "result": json_object(kept, "the kept revision")}
            except HttpError as e:
                report["keepForever"] = {"accepted": False, "error": str(e)[:200]}
            data = download((execute(drive_revisions.get(fileId=pid, revisionId=rid, fields="exportLinks"))
                             .get("exportLinks") or {})[PPTX_MIME])
            back = file_id(execute(drive.files().create(
                body={"name": f"b2s revision probe {tag} restored", "mimeType": SLIDES_MIME},
                media_body=media_upload(io.BytesIO(data), PPTX_MIME), fields="id")), "the restored deck")
            made.append(back)
            text = execute(slides.presentations().get(presentationId=back))
            report["restored"] = {"presentationId": back, "revision": rid,
                                  "holds_the_edit": markers["EDIT"] in json.dumps(text)}
    finally:
        if not keep:
            for fid in made:
                try:
                    execute(drive.files().delete(fileId=fid))
                except HttpError as e:
                    print(f"could not delete the probe deck {fid}: {e}")
            cleaned: list[Json] = [*made]
            report["cleaned_up"] = cleaned
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
