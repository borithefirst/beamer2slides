"""Offline tests of the rebuild guard (src/beamer2slides/guard.py, docs/sync.md): what counts as
a deck edit, the refusal `convert` raises before replacing a deck's content, the backups it keeps,
and the proof that sync's staging deck can never be the user's own deck. No Google calls."""

import copy
import io
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from beamer2slides import guard, snapshot

SRC = Path(guard.__file__).parent  # the package as imported, whatever layout it was staged in


# ---------------------------------------------------------------- a presentation to look at

def pt(v: float) -> dict:
    return {"magnitude": v, "unit": "PT"}


def shape(oid: str, box, text: str | None = None, title: str | None = None) -> dict:
    x0, y0, x1, y1 = box
    e = {"objectId": oid, "title": title, "description": None,
         "size": {"width": pt(x1 - x0), "height": pt(y1 - y0)},
         "transform": {"scaleX": 1, "scaleY": 1, "translateX": x0, "translateY": y0, "unit": "PT"}}
    e["shape"] = {"shapeType": "TEXT_BOX", "text": {"textElements": [
        {"paragraphMarker": {"style": {"alignment": "START"}}},
        {"textRun": {"content": text + "\n", "style": {"fontFamily": "Lato", "fontSize": pt(18)}}}]} if text else {}}
    return e


def picture(oid: str, box, url: str) -> dict:
    x0, y0, x1, y1 = box
    return {"objectId": oid, "title": None, "description": None,
            "size": {"width": pt(x1 - x0), "height": pt(y1 - y0)},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x0, "translateY": y0, "unit": "PT"},
            "image": {"contentUrl": url}}


def slide(sid: str, elements: list[dict], notes: str = "", background: dict | None = None) -> dict:
    return {"objectId": sid, "pageElements": elements,
            "pageProperties": {"pageBackgroundFill": background or {"propertyState": "INHERIT"}},
            "slideProperties": {"layoutObjectId": "L", "notesPage": {
                "notesProperties": {"speakerNotesObjectId": f"{sid}_notes"},
                "pageElements": [shape(f"{sid}_notes", (0, 0, 100, 50), notes or None)]}}}


def presentation(revision: str = "rev1") -> dict:
    """Two converted slides: a title, a body and a picture each."""
    slides = []
    for n, name in enumerate(("Intro", "Results")):
        sid = f"b2s_s{n:03}"
        slides.append(slide(sid, [
            shape(f"{sid}_t0", (10, 10, 300, 40), name, title=f"b2s:{name.lower()}/text/title/0"),
            shape(f"{sid}_t1", (20, 60, 300, 100), f"First point of {name}", title=f"b2s:{name.lower()}/text/body/0"),
            picture(f"{sid}_f0", (320, 60, 460, 160), f"https://lh3.google.com/{name}=s0")],
            notes=f"say something about {name}"))
    return {"presentationId": "P1", "revisionId": revision, "pageSize": {"width": pt(720), "height": pt(405)},
            "layouts": [{"objectId": "L", "layoutProperties": {"name": "TITLE_ONLY"}}],
            "masters": [{"objectId": "M", "pageProperties": {"pageBackgroundFill": {"solidFill": {
                "color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}}}],
            "slides": slides}


def base_of(pres: dict, pdf: str = "talk.pdf") -> dict:
    """The sync base as `convert` records it: IR keys plus Google's read-back (snapshot.build_base
    does the same around the real IR)."""
    read = snapshot.read_presentation(pres)
    slides = []
    for s, r in zip(pres["slides"], read["slides"]):
        elements = []
        for e in s["pageElements"]:
            key = (e.get("title") or "").removeprefix("b2s:").split("/", 1)[-1] or "image/figure/0"
            elements.append({"key": key, "id": e["objectId"], "kind": "image" if "image" in e else "text",
                             "role": key.split("/")[-2] if "/" in key else "figure", "ir_hash": "h", "fields": {},
                             "fingerprint": {}, "anchor": None, "ir": {},
                             "objects": [e["objectId"]], "main": e["objectId"],
                             "readback": {e["objectId"]: copy.deepcopy(r["objects"][e["objectId"]])}})
        slides.append({"key": s["objectId"].replace("b2s_s", "slide"), "label": None,
                       "title": elements[0]["readback"][elements[0]["main"]]["text"].strip(), "page": len(slides),
                       "text": "", "layout": "TITLE_ONLY", "background": "color:#ffffff", "notes": r["notes"],
                       "objectId": s["objectId"], "layoutObjectId": "L", "background_readback": r["background"],
                       "notes_readback": r["notes"], "groups": [], "order": list(r["order"]), "elements": elements})
    return {"version": 1, "generation": 0, "presentationId": pres["presentationId"], "revisionId": pres["revisionId"],
            "source": {"pdf": f"C:/talks/{pdf}", "sha1": "abc"}, "scale": 1.0, "page_size": [720, 405],
            "deck_page_size": [720, 405], "master_background": "color:#ffffff",
            "master_readback": read["master_background"], "slides": slides}


def edited(change) -> tuple[dict, dict]:
    """(base, live presentation) where `change` was applied to a copy of the live deck."""
    pres = presentation()
    base = base_of(pres)
    live = copy.deepcopy(pres)
    live["revisionId"] = "rev2"  # a write always bumps the revision
    change(live)
    return base, live


def find(pres: dict, oid: str) -> dict:
    return next(e for s in pres["slides"] for e in s["pageElements"] if e["objectId"] == oid)


def set_text(e: dict, text: str) -> None:
    e["shape"]["text"]["textElements"][1]["textRun"]["content"] = text + "\n"


# ---------------------------------------------------------------- what is not an edit

def test_untouched_deck_is_not_edited():
    pres = presentation()
    found = guard.survey(base_of(pres), pres)
    assert found == {"edited": False, "revisionId": "rev1", "counts": {}, "slides": [], "slides_added": 0,
                     "slides_deleted": 0, "reordered": False, "examples": []}


def test_a_new_revision_alone_is_not_an_edit():
    """Google bumps revisionId on its own (opening a deck, thumbnails): only content counts."""
    pres = presentation()
    base = base_of(pres)
    later = {**copy.deepcopy(pres), "revisionId": "rev999"}
    assert guard.survey(base, later)["edited"] is False


def test_scratch_slides_of_an_interrupted_run_are_not_an_edit():
    """measure_places' scratch slides (b2s_mNNN) are the converter's own leftovers, not the person's."""
    base, live = edited(lambda p: p["slides"].append(slide("b2s_m003", [shape("x", (0, 0, 10, 10), "scratch")])))
    assert guard.survey(base, live)["edited"] is False


def png(colour: tuple[int, int, int], size=(8, 8)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def test_a_new_content_url_for_the_same_picture_is_not_an_edit(monkeypatch):
    """Google issues new contentUrls for pictures nobody touched: the pixels decide."""
    pres = presentation()
    base = base_of(pres)
    same, other = png((240, 240, 240)), png((20, 20, 20))
    for s in base["slides"]:
        for el in s["elements"]:
            for rb in el["readback"].values():
                if "image" in rb:
                    rb["image"]["signature"] = snapshot.signature(same)
    live = copy.deepcopy(pres)
    for s in live["slides"]:
        for e in s["pageElements"]:
            if "image" in e:  # a URL Google reissued: another path, the same pixels
                e["image"]["contentUrl"] = f"https://lh3.google.com/reissued-{e['objectId']}=s0"
    assert snapshot.read_presentation(live)["slides"][0]["objects"]["b2s_s000_f0"]["image"]["contentHash"] != \
        base["slides"][0]["elements"][2]["readback"]["b2s_s000_f0"]["image"]["contentHash"]

    monkeypatch.setattr(snapshot, "_download", lambda url: same)
    assert guard.survey(base, live)["edited"] is False
    monkeypatch.setattr(snapshot, "_download", lambda url: other)
    found = guard.survey(base, live)
    assert found["edited"] and found["counts"]["image"] == 2


# ---------------------------------------------------------------- what is an edit

def test_reworded_text_is_an_edit_with_a_readable_example():
    base, live = edited(lambda p: set_text(find(p, "b2s_s001_t1"), "Rewritten by hand"))
    found = guard.survey(base, live)
    assert found["edited"] and found["counts"] == {"text": 1}
    assert [s["slide"] for s in found["slides"]] == ["slide001"]
    assert found["slides"][0]["edits"] == [{"element": "text/body/0", "field": "text", "objects": ["b2s_s001_t1"]}]
    assert found["examples"] == ['slide 2 "Results": text edited (text/body/0: “Rewritten by hand”)']
    assert guard.summary_line(found) == "1 slide edited: 1 text edit"


def test_moved_and_restyled_objects_are_edits():
    def change(p):
        e = find(p, "b2s_s000_t1")
        e["transform"]["translateY"] = 200
        find(p, "b2s_s001_t0")["shape"]["text"]["textElements"][1]["textRun"]["style"]["bold"] = True
    base, live = edited(change)
    found = guard.survey(base, live)
    assert found["counts"] == {"geometry": 1, "text_style": 1}
    assert guard.summary_line(found) == "2 slides edited: 1 move or resize, 1 style change"


def test_added_object_deleted_object_notes_and_background():
    def change(p):
        p["slides"][0]["pageElements"].append(shape("theirs1", (400, 300, 500, 340), "a note of my own"))
        p["slides"][1]["pageElements"] = [e for e in p["slides"][1]["pageElements"] if e["objectId"] != "b2s_s001_f0"]
        p["slides"][1]["slideProperties"]["notesPage"]["pageElements"][0]["shape"]["text"]["textElements"][1][
            "textRun"]["content"] = "new notes\n"
        p["slides"][0]["pageProperties"]["pageBackgroundFill"] = {"solidFill": {"color": {"rgbColor": {"red": 1}}}}
    base, live = edited(change)
    found = guard.survey(base, live)
    assert found["counts"] == {"objects_added": 1, "background": 1, "deleted": 1, "notes": 1}
    assert "1 object added in Slides" in guard.summary_line(found)


def test_slides_added_deleted_and_reordered():
    base, live = edited(lambda p: p["slides"].append(slide("mine", [shape("m0", (0, 0, 10, 10), "a slide I added")])))
    assert guard.survey(base, live)["slides_added"] == 1
    base, live = edited(lambda p: p["slides"].pop(0))
    found = guard.survey(base, live)
    assert found["slides_deleted"] == 1 and "deleted in Slides" in found["examples"][0]
    base, live = edited(lambda p: p["slides"].reverse())
    assert guard.survey(base, live)["reordered"] is True


# ---------------------------------------------------------------- the refusal

class Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def http_error(status: int, message: str) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason=message),
                     json.dumps({"error": {"message": message}}).encode())


class FakeFiles:
    def __init__(self, drive):
        self.drive = drive

    def get(self, fileId, fields=None):
        self.drive.calls.append(("get", fileId))
        return Request(self.drive.file if fileId == self.drive.file["id"] else http_error(404, "File not found"))

    def export_media(self, fileId, mimeType):
        self.drive.calls.append(("export", fileId))
        return Request(self.drive.export)

    def copy(self, fileId, body=None, fields=None):
        self.drive.calls.append(("copy", fileId))
        return Request(self.drive.copy_error or {"id": "COPY1", "name": body["name"]})

    def create(self, body=None, media_body=None, fields=None):
        self.drive.calls.append(("create", body.get("name")))
        return Request({"id": "NEW1"})

    def update(self, fileId, media_body=None, body=None, fields=None):
        self.drive.calls.append(("update", fileId))
        return Request({"id": fileId})

    def delete(self, fileId):
        self.drive.calls.append(("delete", fileId))
        return Request({})


class FakeDrive:
    def __init__(self, file=None, export=b"PPTX", copy_error=None):
        self.file = file or {"id": "P1", "name": "Talk", "trashed": False,
                             "mimeType": "application/vnd.google-apps.presentation"}
        self.export, self.copy_error, self.calls = export, copy_error, []

    def files(self):
        return FakeFiles(self)


class FakeSlides:
    def __init__(self, pres):
        self.pres = pres

    def presentations(self):
        return SimpleNamespace(get=lambda presentationId, fields=None: Request(self.pres))


def out_with_base(tmp_path: Path, base: dict | None, pid: str = "P1") -> Path:
    out = tmp_path / "talk"
    out.mkdir()
    (out / "emit.json").write_text(json.dumps({"presentationId": pid, "url": guard.deck_url(pid)}), encoding="utf-8")
    if base is not None:
        snapshot.save_local(base, out)
    return out


def test_check_rebuild_refuses_an_edited_deck_and_says_what_to_do(tmp_path):
    base, live = edited(lambda p: set_text(find(p, "b2s_s001_t1"), "reworded in Slides"))
    out = out_with_base(tmp_path, base)
    with pytest.raises(guard.RebuildRefused) as refused:
        guard.check_rebuild(FakeSlides(live), FakeDrive(), "P1", out, Path("talk.pdf"))
    message = str(refused.value)
    assert "refusing to rebuild" in message and "edited in Google Slides" in message
    assert "1 slide edited: 1 text edit" in message
    assert f"sync talk.pdf --deck {out}" in message and "--force-rebuild" in message and "--new-deck" in message
    assert "revision rev2" in message
    assert refused.value.survey["reason"] == "edited"


def test_check_rebuild_lets_an_untouched_deck_through(tmp_path):
    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    found = guard.check_rebuild(FakeSlides(pres), FakeDrive(), "P1", out, Path("talk.pdf"))
    assert found["reason"] == "" and found["edited"] is False and found["revisionId"] == "rev1"


def test_force_returns_the_finding_instead_of_raising(tmp_path):
    base, live = edited(lambda p: p["slides"].pop())
    out = out_with_base(tmp_path, base)
    found = guard.check_rebuild(FakeSlides(live), FakeDrive(), "P1", out, Path("talk.pdf"), force=True)
    assert found["reason"] == "edited" and found["slides_deleted"] == 1


def test_a_deck_without_a_base_is_never_silently_rebuilt(tmp_path):
    """Without a base nobody can say whether the deck was edited, so it isn't replaced by default."""
    out = out_with_base(tmp_path, None)
    with pytest.raises(guard.RebuildRefused) as refused:
        guard.check_rebuild(FakeSlides(presentation()), FakeDrive(), "P1", out, Path("talk.pdf"))
    assert refused.value.survey["reason"] == "no-base"
    assert "no sync base" in str(refused.value)


def test_a_folder_holding_another_pdfs_deck_is_not_rebuilt(tmp_path):
    """Stale folder reuse: --out points at the deck of a different talk."""
    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres, pdf="other-talk.pdf"))
    with pytest.raises(guard.RebuildRefused) as refused:
        guard.check_rebuild(FakeSlides(pres), FakeDrive(), "P1", out, Path("talk.pdf"))
    assert refused.value.survey["reason"] == "other-source"
    assert "other-talk.pdf" in str(refused.value)
    # the same PDF under a longer path is the same source
    assert guard.check_rebuild(FakeSlides(pres), FakeDrive(), "P1", out,
                               Path("C:/elsewhere/other-talk.pdf"))["reason"] == ""


# ---------------------------------------------------------------- the second ask
#
# `convert` asks the guard twice: once while the PDF is being converted and once immediately
# before the write, because the deck may be edited in between. The second ask is about that
# in-between and nothing else, so where the deck is still at the revision the first one read, the
# first one's finding stands (guard.recheck) and the deck and the base are not read again.


class CountingSlides:
    """A Slides client that remembers what was asked of it."""

    def __init__(self, revision: str | dict = "rev1"):
        self.revision, self.asked = revision, []

    def presentations(self):
        return SimpleNamespace(get=self._get)

    def _get(self, presentationId, fields=None):
        self.asked.append(fields)
        return Request(self.revision if isinstance(self.revision, Exception) else {"revisionId": self.revision})


def a_finding(tmp_path: Path, **change) -> dict:
    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    return {**guard.check_rebuild(FakeSlides(pres), FakeDrive(), "P1", out, Path("talk.pdf")), **change}


def test_a_deck_still_at_its_revision_is_not_surveyed_again(tmp_path):
    first = a_finding(tmp_path)
    slides = CountingSlides("rev1")
    again = guard.recheck(slides, "P1", first)
    assert again["reason"] == "" and again["revisionId"] == "rev1" and again["rechecked"] == "revision unchanged"
    assert slides.asked == ["revisionId"], "one field of one read, and no base"
    assert again["checked"] >= first["checked"]


def test_a_deck_edited_since_the_first_ask_is_asked_the_whole_question_again(tmp_path):
    assert guard.recheck(CountingSlides("rev2"), "P1", a_finding(tmp_path)) is None


def test_a_finding_about_another_deck_is_never_reused(tmp_path):
    assert guard.recheck(CountingSlides("rev1"), "P2", a_finding(tmp_path)) is None


def test_a_finding_with_a_reason_is_never_reused(tmp_path):
    """One with a reason raised where it was made (or was forced); it says nothing about now."""
    assert guard.recheck(CountingSlides("rev1"), "P1", a_finding(tmp_path, reason="edited")) is None
    assert guard.recheck(CountingSlides("rev1"), "P1", None) is None


def test_a_read_that_fails_asks_the_whole_question_again(tmp_path):
    assert guard.recheck(CountingSlides(http_error(404, "not found")), "P1", a_finding(tmp_path)) is None


def test_the_preflight_hands_its_finding_to_the_write(tmp_path, monkeypatch):
    """emit.preflight_rebuild -> emit.plan_rebuild: the journey `convert` and `deck_convert` make."""
    from beamer2slides import emit

    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    checked = emit.preflight_rebuild(out, Path("talk.pdf"), slides=FakeSlides(pres), drive=FakeDrive())
    assert checked["presentationId"] == "P1" and checked["found"]["revisionId"] == "rev1"

    slides = CountingSlides("rev1")
    monkeypatch.setattr(emit, "credentials_for_threads", lambda: None)
    monkeypatch.setattr(emit, "slides_service", lambda creds=None: slides)
    monkeypatch.setattr(guard, "check_rebuild", lambda *a, **kw: pytest.fail("asked the whole question again"))
    pid, entry = emit.plan_rebuild(slides, FakeDrive(), out, False, False, "auto", Path("talk.pdf"), checked)
    assert pid == "P1" and entry["reason"] == "no deck edits" and entry["revisionId"] == "rev1"
    assert slides.asked == ["revisionId"]


def test_a_deck_that_left_the_folder_between_the_two_asks_is_looked_at_properly(tmp_path, monkeypatch):
    """The folder's deck is in the trash now: the preflight's finding is about a deck this run is
    no longer replacing, so it is dropped and the question asked again."""
    from beamer2slides import emit

    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    checked = emit.preflight_rebuild(out, Path("talk.pdf"), slides=FakeSlides(pres), drive=FakeDrive())
    monkeypatch.setattr(emit, "credentials_for_threads", lambda: None)
    monkeypatch.setattr(emit, "slides_service", lambda creds=None: CountingSlides("rev1"))
    drive = FakeDrive(file={"id": "P1", "name": "Talk", "trashed": True,
                            "mimeType": "application/vnd.google-apps.presentation"})
    previous, found = emit.look_again(CountingSlides("rev1"), drive, out, checked)
    assert previous["state"] == "trashed" and found is None


# ---------------------------------------------------------------- backups and records

def test_backup_modes(tmp_path):
    out = tmp_path / "talk"
    drive = FakeDrive()
    assert guard.backup_deck(drive, "P1", out, "none") == {"mode": "none", "warnings": []}
    assert drive.calls == []
    result = guard.backup_deck(drive, "P1", out, "file")
    assert Path(result["file"]).read_bytes() == b"PPTX" and result["bytes"] == 4
    result = guard.backup_deck(drive, "P1", out, "both")
    assert result["drive"]["url"].endswith("COPY1/edit") and Path(result["file"]).exists()
    assert ("copy", "P1") in drive.calls


def test_a_refused_export_falls_back_to_a_drive_copy(tmp_path):
    """A deck over Drive's 10 MB export limit must still leave a way back."""
    drive = FakeDrive(export=http_error(403, "exportSizeLimitExceeded"))
    result = guard.backup_deck(drive, "P1", tmp_path / "talk", "file")
    assert "file" not in result and result["drive"]["presentationId"] == "COPY1"
    assert result["warnings"] and "10 MB" in result["warnings"][0]


def a_backup(out: Path, name: str, age_days: float = 0.0, size: int = 1000) -> dict:
    path = guard.backup_dir(out) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"P" * size)
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))
    return {"presentationId": "P1", "action": "synced", "revisionId": name, "checked": name,
            "backup": {"mode": "file", "file": str(path), "warnings": []}}


def test_prune_keeps_the_newest_backups_and_only_touches_its_own_files(tmp_path):
    out = tmp_path / "talk"
    log = [a_backup(out, f"{n:02}.pptx") for n in range(5)]
    stranger = guard.backup_dir(out) / "someone-elses.pptx"
    stranger.write_bytes(b"not ours")
    (guard.backup_dir(out) / "backups.json").write_text(json.dumps(log), encoding="utf-8")
    dry = guard.prune_backups(out, keep=2)
    assert dry["files"] == 5 and [Path(d["file"]).name for d in dry["doomed"]] == ["00.pptx", "01.pptx", "02.pptx"]
    assert dry["deleted"] is False and all(p.exists() for p in guard.backup_dir(out).glob("*.pptx"))
    done = guard.prune_backups(out, keep=2, delete=True)
    assert done["deleted"] and done["freed"] == 3000
    assert sorted(p.name for p in guard.backup_dir(out).glob("*.pptx")) == \
        ["03.pptx", "04.pptx", "someone-elses.pptx"], "a file this program did not write is never deleted"
    kept = json.loads((guard.backup_dir(out) / "backups.json").read_text(encoding="utf-8"))
    assert len(kept) == 5, "the record of what the deck was stays, even when its file is gone"
    assert [bool(e["backup"].get("deleted")) for e in kept] == [True, True, True, False, False]
    assert guard.prune_backups(out, keep=2)["doomed"] == []  # nothing left to prune


def test_prune_can_be_asked_to_keep_everything_recent(tmp_path):
    out = tmp_path / "talk"
    log = [a_backup(out, "old.pptx", age_days=30), a_backup(out, "new.pptx", age_days=1)]
    (guard.backup_dir(out) / "backups.json").write_text(json.dumps(log), encoding="utf-8")
    r = guard.prune_backups(out, keep=0, older_than_days=7)
    assert [Path(d["file"]).name for d in r["doomed"]] == ["old.pptx"]


def test_a_backup_neither_kind_of_which_worked_is_no_way_back(tmp_path):
    """Both refused: the .pptx export (over 10 MB) and the Drive copy (a full Drive)."""
    drive = FakeDrive(export=http_error(403, "exportSizeLimitExceeded"),
                      copy_error=http_error(403, "storageQuotaExceeded"))
    result = guard.backup_deck(drive, "P1", tmp_path / "talk", "file")
    assert "file" not in result and "drive" not in result and len(result["warnings"]) == 2
    assert not guard.way_back_kept(result)


def test_way_back_kept_needs_a_file_with_something_in_it(tmp_path):
    empty = tmp_path / "empty.pptx"
    empty.write_bytes(b"")
    assert not guard.way_back_kept({"warnings": []})
    assert not guard.way_back_kept({"file": str(tmp_path / "never-written.pptx")})
    assert not guard.way_back_kept({"file": str(empty)}), "an empty file restores nothing"
    empty.write_bytes(b"PPTX")
    assert guard.way_back_kept({"file": str(empty)})
    assert guard.way_back_kept({"drive": {"presentationId": "COPY1"}})


def test_record_appends_and_restore_hint_reads(tmp_path):
    out = tmp_path / "talk"
    entry = {"presentationId": "P1", "revisionId": "rev7", "action": "rebuilt in place",
             "backup": {"file": str(out / "backups" / "x.pptx"), "warnings": []}}
    guard.record(out, entry)
    guard.record(out, {**entry, "revisionId": "rev8"})
    log = json.loads((out / "backups" / "backups.json").read_text(encoding="utf-8"))
    assert [e["revisionId"] for e in log] == ["rev7", "rev8"]
    hint = "\n".join(guard.restore_hint(entry))
    # The .pptx backup is the way back, and the hint says exactly how (Drive's version history
    # gives the deck's current content back for every revision, docs/sync.md).
    assert "rev7" in hint and "x.pptx" in hint and "deck_backup.py restore" in hint
    bare = "\n".join(guard.restore_hint({"presentationId": "P1", "revisionId": "rev7"}))
    assert "no backup file was kept" in bare and "version history" in bare


# ---------------------------------------------------------------- emit's decision

def fake_services(monkeypatch, drive, slides):
    import beamer2slides.google_auth as auth
    monkeypatch.setattr(auth, "drive_service", lambda *a, **k: drive)
    monkeypatch.setattr(auth, "slides_service", lambda *a, **k: slides)


def test_plan_rebuild_rebuilds_an_untouched_deck(tmp_path):
    from beamer2slides.emit import plan_rebuild
    pres = presentation()
    out = out_with_base(tmp_path, base_of(pres))
    drive = FakeDrive()
    pid, entry = plan_rebuild(FakeSlides(pres), drive, out, False, False, "auto", Path("talk.pdf"))
    assert pid == "P1" and entry["action"] == "rebuilt in place" and entry["revisionId"] == "rev1"
    assert entry["backup"] == {"mode": "none", "warnings": []}  # nothing to lose, nothing exported
    assert json.loads((out / "backups" / "backups.json").read_text(encoding="utf-8"))[0]["reason"] == "no deck edits"


def test_plan_rebuild_backs_up_before_a_forced_rebuild(tmp_path):
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    drive = FakeDrive()
    pid, entry = plan_rebuild(FakeSlides(live), drive, out, False, True, "auto", Path("talk.pdf"))
    assert pid == "P1" and entry["reason"] == "edited"
    assert Path(entry["backup"]["file"]).exists() and ("export", "P1") in drive.calls
    assert entry["revisionId"] == "rev2" and entry["examples"]


def test_plan_rebuild_refuses_without_force(tmp_path):
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    drive = FakeDrive()
    with pytest.raises(guard.RebuildRefused):
        plan_rebuild(FakeSlides(live), drive, out, False, False, "auto", Path("talk.pdf"))
    assert ("export", "P1") not in drive.calls and ("update", "P1") not in drive.calls


def test_a_forced_rebuild_whose_backup_failed_is_refused(tmp_path):
    """The offer that makes `--force-rebuild` acceptable is the backup. When Drive refuses both
    the export and the copy, the deck must stay as it is: nothing else can bring it back."""
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    drive = FakeDrive(export=http_error(403, "exportSizeLimitExceeded"),
                      copy_error=http_error(403, "storageQuotaExceeded"))
    with pytest.raises(guard.RebuildRefused) as refused:
        plan_rebuild(FakeSlides(live), drive, out, False, True, "auto", Path("talk.pdf"))
    message = str(refused.value)
    assert "refusing to rebuild" in message and "backup" in message
    assert "storageQuotaExceeded" in message and "--backup none" in message
    assert refused.value.survey["reason"] == "backup-failed"
    assert ("update", "P1") not in drive.calls
    # the failed attempt is in the log: what was tried, and that the deck is still the old one
    logged = json.loads((out / "backups" / "backups.json").read_text(encoding="utf-8"))[-1]
    assert logged["revisionId"] == "rev2" and logged["backup"]["warnings"]


def test_a_drive_copy_is_way_back_enough_for_a_forced_rebuild(tmp_path):
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    drive = FakeDrive(export=http_error(403, "exportSizeLimitExceeded"))
    pid, entry = plan_rebuild(FakeSlides(live), drive, out, False, True, "auto", Path("talk.pdf"))
    assert pid == "P1" and entry["backup"]["drive"]["presentationId"] == "COPY1"


def test_backup_none_says_out_loud_that_the_deck_may_go(tmp_path):
    """`--backup none` is the way to ask for a rebuild without a way back, so it is not refused."""
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    drive = FakeDrive(export=http_error(403, "exportSizeLimitExceeded"),
                      copy_error=http_error(403, "storageQuotaExceeded"))
    pid, entry = plan_rebuild(FakeSlides(live), drive, out, False, True, "none", Path("talk.pdf"))
    assert pid == "P1" and entry["backup"] == {"mode": "none", "warnings": []}


def test_new_deck_leaves_the_old_one_alone(tmp_path, capsys):
    from beamer2slides.emit import plan_rebuild
    base, live = edited(lambda p: set_text(find(p, "b2s_s000_t1"), "my own words"))
    out = out_with_base(tmp_path, base)
    pid, entry = plan_rebuild(FakeSlides(live), FakeDrive(), out, True, False, "auto", Path("talk.pdf"))
    assert pid is None and entry["action"] == "new deck" and entry["state"] == "kept"
    assert "left as it is" in capsys.readouterr().out


def test_a_trashed_deck_is_not_resurrected(tmp_path, capsys):
    from beamer2slides.emit import plan_rebuild
    out = out_with_base(tmp_path, base_of(presentation()))
    drive = FakeDrive({"id": "P1", "name": "Talk", "trashed": True,
                       "mimeType": "application/vnd.google-apps.presentation"})
    pid, entry = plan_rebuild(FakeSlides(presentation()), drive, out, False, False, "auto", Path("talk.pdf"))
    assert pid is None and entry["state"] == "trashed"
    assert "trash" in capsys.readouterr().out
    # a deck deleted in Drive: a new one, and no attempt to write to the old id
    drive = FakeDrive({"id": "gone"})
    pid, entry = plan_rebuild(FakeSlides(presentation()), drive, out, False, False, "auto", Path("talk.pdf"))
    assert pid is None and entry["state"] == "gone"
    assert [c for c in drive.calls if c[0] != "get"] == []


def test_the_backup_modes_of_the_command_line_are_the_guards(tmp_path):
    from beamer2slides.__main__ import BACKUP_MODES
    assert BACKUP_MODES == guard.BACKUP_MODES


# ---------------------------------------------------------------- sync's staging deck

def test_sync_deletes_only_the_staging_deck_it_just_created(tmp_path):
    """The staging deck's id comes from the files.create two lines above, and only that id is
    ever passed to files.delete: sync can never delete the user's own deck."""
    from beamer2slides.sync import Sync
    picture_file = tmp_path / "fig.png"
    picture_file.write_bytes(png((10, 10, 10), (40, 30)))
    staged = {"slides": [{"objectId": "s", "pageElements": [
        {"objectId": "i", "description": "b2s-stage:0", "image": {"contentUrl": "https://staged"}}]}]}
    drive = FakeDrive()
    me = SimpleNamespace(drive=drive, slides=FakeSlides(staged), urls={}, pid="P1", _fit=lambda f: [40.0, 30.0],
                         plan=SimpleNamespace(deck={"slides": [{"size": [720, 405]}]}))
    fid = Sync.stage(me, {"pictures": {str(picture_file): "figure"}})
    assert fid == "NEW1" != me.pid
    assert me.urls == {str(picture_file): "https://staged"}

    source = (SRC / "sync.py").read_text(encoding="utf-8")
    deletes = set(re.findall(r"files\(\)\.delete\(fileId=([\w.]+)\)", source))
    assert deletes == {"fid"}  # never self.pid, and never an id read from a file
    assert re.search(r"fid = execute\(drive\.files\(\)\.create\(", source)
    assert re.search(r"staging = self\.stage\(work\)", source)
    # Staged on a thread of its own, with clients of its own: the id still comes from that create
    # and nowhere else, and the future is collected whatever happens (run's own `finally`).
    assert re.search(r"self\.in_background\(lambda slides, drive: self\.stage\(work, drive, slides\), \"b2s-stage\"\)",
                     source)
    assert re.search(r"staging = staged\.result\(\)", source)
    # Sent away on a thread at the end of the write, and `drop_staging`'s only caller hands it the
    # id that create returned; nobody leaves before the file is really gone.
    assert set(re.findall(r"self\.drop_staging\((\w+)\)", source)) == {"staging"}
    assert re.search(r"def drop_staging\(self, fid: str\)", source)
    assert re.search(r"s\.await_deletes\(\)", source)
