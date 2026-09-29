"""The edit hunt: a person edits a converted deck, the author revises the source, sync merges -
and afterwards the deck looks wrong, lost something, or says something untrue.

The visual hunt (`visual_hunt`) judged conversions and was blind to editing. The fuzzers
(`fuzz_sync`) edit with a fixed vocabulary on one test deck and are judged by code. This is the
third instrument: realistic talks, a person's edits written the way a person makes them in the
Slides editor (the `deck_edits` vocabulary, or raw batchUpdate requests for anything else), a
realistic source revision, the real `sync`, then the deterministic judges *and* pictures for an
eye. A journey is a folder `out/edithunt/j/<journey>/`:

    python -m beamer2slides.devtools.edit_hunt start decks/<j>/v1.tex --journey <j> --slot s1
    python -m beamer2slides.devtools.edit_hunt edit  --journey <j> edits.json
    python -m beamer2slides.devtools.edit_hunt sync  --journey <j> decks/<j>/v2.tex
    (edit / sync again for a second round: the base is the one the last sync wrote)
    python -m beamer2slides.devtools.edit_hunt pull  --journey <j>
    python -m beamer2slides.devtools.edit_hunt dump  --journey <j>

`start` compiles v1, converts it into `out/edithunt/<slot>` (the slot's previous deck is deleted
from Drive first, so a campaign leaves one deck per slot), saves every slide's thumbnail
(`thumbs/v1/NNN.png`) and `deck-v1.md`: each slide's objects with ids, boxes (pt) and text, what
an editor needs to address raw requests. `edits.json` is a list; an item is either a
`deck_edits` spec `{"edit": "replace_word", "args": {...}}` (found by content, returns
expectations) or `{"raw": [request, ...], "why": "..."}`, one batchUpdate. `edit` applies them in
order, keeps what Google refused apart (`rN/refused.json`), and saves `thumbs/rN-edited/` and
`deck-rN-edited.md`.

`sync` compiles the revision, copies it over the slot's PDF (the deck is paired with a PDF by its
name), snapshots the deck, runs the real `sync`, snapshots again, and judges: `loss_oracle`,
`layout_oracle`, `sync_check.integrity`, the `deck_edits` expectations, and a second sync of the
same PDF, which must write nothing (`settle_requests`). It writes `thumbs/rN-synced/`, the new
PDF's pages and per synced slide `cmp/rN-NNN.png`: the deck as the person left it | the new PDF
page | the synced slide. `summary-rN.json` holds the verdicts, the report's conflicts and
warnings and which slide is which. The judges are a pre-screen, never the verdict: a finding is
what an eye confirms on the pictures (or in the report's words).

`pull` pulls the deck back into a copy of the latest source (`pull --out`), and archives the
source diff, `edits.md` and the recompiled pages beside the synced slides (`pull/cmp-NNN.png`).

Google runs share the write quota through `visual_hunt.google_turn` (lock files under
`out/edithunt/locks`, `B2S_HUNT_GOOGLE` at once). Blind spots: thumbnails, not the editor
(selection, alt text, autofit behaviour on typing); edits come through the API, so what the
editor's own gestures do beyond their requests (a paste's styles, a drag's snapping) is not
reproduced."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from ..google_types import part, presentation
from ..json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects, as_str
from .visual_hunt import PANEL, ROOT, compile_tex, google_turn

if TYPE_CHECKING:
    from .sync_check import Element

HUNT = ROOT / "out" / "edithunt"
LOCKS = HUNT / "locks"
EMU = 12700
CLI_TIMEOUT = 3600   # s: a convert or a sync of a hunt deck
EMPTY_PANEL_HEIGHT = 563   # px: a missing picture's panel, as tall as a 16:9 slide's


# ---------------------------------------------------------------- plumbing

def cli(*args: str | Path, timeout: int) -> subprocess.CompletedProcess[str]:
    """One `python -m beamer2slides ...`, never interactive, one Google turn."""
    cmd = [sys.executable, "-m", "beamer2slides.devtools.counted", *map(str, args)]
    with google_turn(LOCKS):
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, errors="replace",
                              stdin=subprocess.DEVNULL, timeout=timeout)


def journey_dir(journey: str) -> Path:
    return HUNT / "j" / journey


def _flag(v: Json, where: str) -> bool:
    if isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: true or false was expected, found {v!r}")


def _jsons(xs: Sequence[JsonObject] | Sequence[str]) -> list[Json]:
    """A list of objects or strings as the JSON array it is written as."""
    return [x for x in xs]


@dataclass(frozen=True, kw_only=True)
class Round:
    """One round of a journey: the person's edits, then (once `synced`) the author's revision."""
    n: int
    edits: tuple[JsonObject, ...]   # the edits.json items Google applied, in order
    synced: bool

    def json(self) -> JsonObject:
        return {"n": self.n, "edits": _jsons(self.edits), "synced": self.synced}


@dataclass(frozen=True, kw_only=True)
class Journey:
    """A journey's state.json: its deck, its slot and the rounds so far."""
    journey: str
    slot: str
    pid: str
    out: str   # the slot's output folder
    pdf: str   # the slot's PDF, which each revision is copied over
    sources: tuple[str, ...]   # the .tex of each version, oldest first
    rounds: tuple[Round, ...]

    def json(self) -> JsonObject:
        return {"journey": self.journey, "slot": self.slot, "pid": self.pid, "out": self.out, "pdf": self.pdf,
                "sources": _jsons(self.sources), "rounds": [r.json() for r in self.rounds]}

    def open_round(self) -> Journey:
        """The journey with a round open for edits: its last one, unless that one was synced."""
        if self.rounds and not self.rounds[-1].synced:
            return self
        return replace(self, rounds=(*self.rounds, Round(n=len(self.rounds) + 1, edits=(), synced=False)))

    def with_last(self, rnd: Round) -> Journey:
        return replace(self, rounds=(*self.rounds[:-1], rnd))


def journey_state(o: JsonObject, where: str) -> Journey:
    rounds = tuple(Round(n=as_int(r.get("n"), f"{where} round n"),
                         edits=tuple(as_objects(r.get("edits"), f"{where} round edits")),
                         synced=_flag(r.get("synced"), f"{where} round synced"))
                   for r in as_objects(o.get("rounds"), f"{where} rounds"))
    return Journey(journey=as_str(o.get("journey"), f"{where} journey"), slot=as_str(o.get("slot"), f"{where} slot"),
                   pid=as_str(o.get("pid"), f"{where} pid"), out=as_str(o.get("out"), f"{where} out"),
                   pdf=as_str(o.get("pdf"), f"{where} pdf"),
                   sources=tuple(as_str(s, f"{where} sources") for s in as_array(o.get("sources"), f"{where} sources")),
                   rounds=rounds)


def read_json(path: Path) -> Json:
    return json.loads(path.read_text(encoding="utf-8"))


def load_state(journey: str) -> Journey:
    path = journey_dir(journey) / "state.json"
    return journey_state(as_object(read_json(path), str(path)), str(path))


def save_state(state: Journey) -> None:
    (journey_dir(state.journey) / "state.json").write_text(json.dumps(state.json(), indent=1, ensure_ascii=False),
                                                           encoding="utf-8")


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


def read_pres(pid: str) -> JsonObject:
    from beamer2slides.google_auth import slides_service
    from beamer2slides.google_types import as_json
    from beamer2slides.gslides import execute
    return as_json(execute(slides_service().presentations().get(presentationId=pid)), pid)


def snapshot(pid: str) -> tuple[JsonObject, JsonObject]:
    from beamer2slides import snapshot as snap
    from beamer2slides.deck_pictures import WORKERS
    pres = read_pres(pid)
    typed = presentation(pres, pid)
    read = snap.read_presentation(typed)
    snap.sign_pictures(read, typed, None, None, WORKERS, None, None, None, None, None)
    return pres, read


def thumbs(pid: str, pres: JsonObject, folder: Path) -> None:
    from beamer2slides.deck_ir import slide_thumbnails
    if folder.exists():
        shutil.rmtree(folder)
    slide_thumbnails(pid, presentation(pres, pid), folder)


def drop_slot(slot: str) -> None:
    """The slot's previous deck (and its Drive base) out of Drive, and its folder off the disk."""
    out = HUNT / slot
    emit = out / "emit.json"
    if emit.exists():
        from beamer2slides import snapshot as snap
        from beamer2slides.google_auth import drive_service
        from beamer2slides.gslides import execute
        drive = drive_service()
        try:
            pid = as_str(as_object(read_json(emit), str(emit)).get("presentationId"), f"{emit} presentationId")
            info = execute(drive.files().get(fileId=pid, fields="appProperties"))
            props = info.get("appProperties")
            fid = props.get(snap.BASE_PROPERTY) if isinstance(props, dict) else None
            if isinstance(fid, str) and fid:
                execute(drive.files().delete(fileId=fid))
            execute(drive.files().delete(fileId=pid))
        except Exception as e:  # noqa: BLE001 (gone already, or never ours)
            print(f"could not delete the slot's old deck: {e}")
    if out.exists():
        shutil.rmtree(out)


# ---------------------------------------------------------------- the deck as an editor reads it

def what_it_is(e: Element) -> str:
    """An element's kind as the dump names it: a shape by its type (and placeholder), a table by
    its grid."""
    if e.kind == "shape":
        what = e.shape_type if e.shape_type is not None else "shape"
        placeholder = part(e.shape.get("placeholder"), "shape.placeholder")
        return what + (f"/{placeholder.get('type')}" if placeholder else "")
    if e.kind == "table":
        rows, columns = e.table_size
        return f"table {rows}x{columns}"
    return e.kind


def dump(pres: JsonObject) -> str:
    """Every slide's objects with ids, absolute boxes in pt and their text: what raw requests
    address. Text is shown with its paragraphs split by ' | ', a table cell by [r,c]."""
    from .sync_check import Model
    size = presentation(pres, "dump").get("pageSize", {})
    w, h = (round(d.get("magnitude", 0) / EMU, 1) for d in (size.get("width", {}), size.get("height", {})))
    lines = [f"# {pres.get('title', '')}  ({pres['presentationId']})",
             f"boxes: [x, y, w, h] pt on a {w} x {h} pt slide (a raw request's EMU = pt x 12700); "
             "ids are what raw requests name. A table's box height is not its rows' height.", ""]
    for s in Model(pres).slides:
        layout = s.obj.get("slideProperties", {}).get("layoutObjectId")
        lines.append(f"## slide {s.index + 1}  id={s.id}  layout={layout}  title={s.title!r}")
        for e in s.elements:
            x0, y0, x1, y1 = (round(v, 1) for v in e.box)
            depth = "  " * len(e.groups)
            title = e.obj.get("title") or ""
            head = f"{depth}- {e.id} {what_it_is(e)} [{x0}, {y0}, {round(x1 - x0, 1)}, {round(y1 - y0, 1)}]" + \
                (f" alt={title}" if title else "")
            texts: list[str] = []
            for text, cell in e.texts:
                t = " | ".join(p for p in text.rstrip("\n").split("\n"))
                if t.strip():
                    where = f"[{cell.row},{cell.column}] " if cell is not None else ""
                    texts.append(where + t)
            if texts:
                body = "; ".join(texts)
                head += ": " + (body[:400] + "..." if len(body) > 400 else body)
            lines.append(head)
        lines.append("")
    return "\n".join(lines)


def save_dump(pres: JsonObject, path: Path) -> None:
    path.write_text(dump(pres), encoding="utf-8")


# ---------------------------------------------------------------- pictures

def panel(image: Image.Image | None, label: str) -> Image.Image:
    if image is None:
        canvas = Image.new("RGB", (PANEL, EMPTY_PANEL_HEIGHT + 28), (90, 90, 90))
        ImageDraw.Draw(canvas).text((8, 6), label, fill=(255, 255, 255))
        return canvas
    image = image.convert("RGB")
    image = image.resize((PANEL, round(image.height * PANEL / image.width)), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (PANEL, image.height + 28), (40, 40, 40))
    canvas.paste(image, (0, 28))
    ImageDraw.Draw(canvas).text((8, 6), label, fill=(255, 255, 255))
    return canvas


def row(panels: list[Image.Image]) -> Image.Image:
    h = max(p.height for p in panels)
    out = Image.new("RGB", (len(panels) * PANEL + 12 * (len(panels) - 1), h), (255, 0, 255))
    for i, p in enumerate(panels):
        out.paste(p, (i * (PANEL + 12), 0))
    return out


def render_pages(pdf: Path, folder: Path) -> list[Path]:
    from beamer2slides.pdf import Document
    folder.mkdir(parents=True, exist_ok=True)
    doc = Document(pdf)
    paths = []
    for n in range(len(doc)):
        page = doc[n]
        path = folder / f"{n + 1:03d}.png"
        Image.fromarray(page.render(1600 / page.width)).convert("RGB").save(path)
        paths.append(path)
    return paths


def _open(path: Path | None) -> Image.Image | None:
    return Image.open(path) if path is not None and path.exists() else None


# ---------------------------------------------------------------- the journey

def start(tex: Path, journey: str, slot: str) -> int:
    j = journey_dir(journey)
    if j.exists():
        shutil.rmtree(j)
    j.mkdir(parents=True)
    t0 = time.time()
    tex = tex.resolve()
    built = compile_tex(tex)
    shutil.copyfile(tex, j / "v1.tex")
    shutil.copyfile(built, j / "v1.pdf")
    drop_slot(slot)
    (HUNT / "pdfs").mkdir(parents=True, exist_ok=True)
    pdf = HUNT / "pdfs" / f"{slot}.pdf"
    shutil.copyfile(built, pdf)
    out = HUNT / slot
    r = cli("convert", pdf, "--out", out, "--title", f"edit hunt {journey}", timeout=CLI_TIMEOUT)
    (j / "convert.log").write_text(r.stdout[-20000:] + "\n--- stderr\n" + r.stderr[-20000:], encoding="utf-8")
    if r.returncode:
        print(f"convert failed ({r.returncode}); see {j / 'convert.log'}\n{r.stderr[-2000:]}")
        return 1
    emit = out / "emit.json"
    pid = as_str(as_object(read_json(emit), str(emit)).get("presentationId"), f"{emit} presentationId")
    pres = read_pres(pid)
    thumbs(pid, pres, j / "thumbs" / "v1")
    render_pages(built, j / "pdf" / "v1")
    save_dump(pres, j / "deck-v1.md")
    save_state(Journey(journey=journey, slot=slot, pid=pid, out=str(out), pdf=str(pdf), sources=(str(tex),), rounds=()))
    print(f"{journey}: {len(as_array(pres.get('slides', []), 'slides'))} slides in {time.time() - t0:.0f}s\n"
          f"https://docs.google.com/presentation/d/{pid}/edit\n"
          f"read {j / 'deck-v1.md'} and {j / 'thumbs' / 'v1'}")
    return 0


def edit(journey: str, edits_path: Path) -> int:
    from beamer2slides.gslides import execute
    from .deck_edits import apply, open_deck
    state = load_state(journey).open_round()
    j = journey_dir(journey)
    rnd = state.rounds[-1]
    n = rnd.n
    items = as_objects(json.loads(edits_path.read_text(encoding="utf-8-sig")), str(edits_path))
    deck = open_deck(state.pid, defer=False)
    applied: list[JsonObject] = []
    refused: list[tuple[JsonObject, str]] = []   # (item, why)
    expectations: list[JsonObject] = []
    with google_turn(LOCKS):
        for item in items:
            try:
                if "raw" in item:
                    execute(deck.api.presentations().batchUpdate(
                        presentationId=state.pid, body={"requests": as_objects(item["raw"], "raw")}))
                    deck.read()
                else:
                    expectations.append(apply(deck, item).json())
                applied.append(item)
            except Exception as e:  # noqa: BLE001 (an edit Google refused or that found nothing)
                refused.append((item, f"{type(e).__name__}: {e}"[:1500]))
                deck.read()
    rnd = replace(rnd, edits=(*rnd.edits, *applied))
    state = state.with_last(rnd)
    folder = j / f"r{n}"
    write_json(folder / "edits.json", list(rnd.edits))
    exp_path = folder / "expectations.json"
    earlier = as_objects(read_json(exp_path), str(exp_path)) if exp_path.exists() else []
    write_json(exp_path, earlier + expectations)
    if refused:
        write_json(folder / "refused.json", [{"item": item, "error": why} for item, why in refused])
    pres = read_pres(state.pid)
    thumbs(state.pid, pres, j / "thumbs" / f"r{n}-edited")
    save_dump(pres, j / f"deck-r{n}-edited.md")
    save_state(state)
    print(f"round {n}: {len(applied)} edits applied, {len(refused)} refused"
          + (f" (see {folder / 'refused.json'})" if refused else "")
          + f"\nread {j / f'deck-r{n}-edited.md'} and {j / 'thumbs' / f'r{n}-edited'}")
    for _, why in refused:
        print("  refused:", why[:300])
    return 0


def requests_of(stdout: str) -> int | None:
    m = re.findall(r"requests (\d+)", stdout)
    return int(m[-1]) if m else None


def sync(journey: str, tex: Path) -> int:
    from beamer2slides import snapshot as snap
    from beamer2slides.emit import SLIDE_W
    from beamer2slides.sync import build_ours
    from . import layout_oracle, loss_oracle
    from . import sync_check as sc

    loaded = load_state(journey)
    j = journey_dir(journey)
    state = loaded.open_round()
    rnd = state.rounds[-1]
    n = rnd.n
    if state is not loaded:   # no edits this round: the deck as it is is how the person left it
        thumbs(state.pid, read_pres(state.pid), j / "thumbs" / f"r{n}-edited")
    folder = j / f"r{n}"
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    tex = tex.resolve()
    built = compile_tex(tex)
    shutil.copyfile(tex, folder / f"v{n + 1}.tex")
    shutil.copyfile(built, folder / f"v{n + 1}.pdf")
    pdf = Path(state.pdf)
    shutil.copyfile(built, pdf)
    out = Path(state.out)
    base_path = out / "sync" / "base.json"
    base = as_object(read_json(base_path), str(base_path))
    write_json(folder / "base.json", base)
    pres_before, before = snapshot(state.pid)
    write_json(folder / "before.json", before)
    r = cli("sync", pdf, "--deck", out, timeout=CLI_TIMEOUT)
    (folder / "sync.log").write_text(r.stdout[-30000:] + "\n--- stderr\n" + r.stderr[-20000:], encoding="utf-8")
    summary: JsonObject = {"journey": journey, "round": n, "sync_code": r.returncode, "requests": requests_of(r.stdout),
                           "url": f"https://docs.google.com/presentation/d/{state.pid}/edit"}
    if r.returncode:
        summary["sync_error"] = (r.stdout[-1500:] + r.stderr[-2500:])
    report: JsonObject = {}
    report_path = out / "sync" / "sync-report.json"
    if report_path.exists() and not r.returncode:
        report = as_object(read_json(report_path), str(report_path))
        write_json(folder / "report.json", report)
        if (out / "sync" / "sync-report.md").exists():
            shutil.copyfile(out / "sync" / "sync-report.md", folder / "sync-report.md")
    pres_after, after = snapshot(state.pid)
    write_json(folder / "after.json", after)
    thumbs(state.pid, pres_after, j / "thumbs" / f"r{n}-synced")
    save_dump(pres_after, j / f"deck-r{n}-synced.md")
    pages = render_pages(built, j / "pdf" / f"v{n + 1}")

    if not r.returncode:
        ours: JsonObject | None
        try:
            ours = build_ours(pdf, folder / "ours", base, "last", SLIDE_W,
                              snap.find_base_pictures(base, snap.picture_folders(out)))
        except Exception as e:  # noqa: BLE001
            ours, summary["ours_error"] = None, f"{type(e).__name__}: {e}"

        def loss() -> str:
            found = loss_oracle.check(base, before, after, report, ours)
            write_json(folder / "loss.json", found)
            return loss_oracle.describe(loss_oracle.failures(found))

        def layout() -> str:
            found = layout_oracle.check(base, before, after, report, ours)
            write_json(folder / "layout.json", found)
            return layout_oracle.describe(layout_oracle.failures(found))

        for name, judge in (("loss", loss), ("layout", layout)):
            try:
                summary[name] = _jsons(judge().strip().splitlines())
            except Exception as e:  # noqa: BLE001 (a pre-screen never fails the round)
                summary[name] = [f"judge crashed: {type(e).__name__}: {e}"]
        new_base = as_object(read_json(base_path), str(base_path))
        try:
            summary["integrity"] = _jsons(sc.integrity(sc.Model(pres_after), before=sc.Model(pres_before),
                                                       base_ids=sc.ids_in(new_base), allow_groups_changed=frozenset(),
                                                       allow_ungrouped=frozenset()))
        except Exception as e:  # noqa: BLE001
            summary["integrity"] = [f"crashed: {type(e).__name__}: {e}"]
        exp_path = folder / "expectations.json"
        if exp_path.exists():
            checks = [c for x in as_objects(read_json(exp_path), str(exp_path))
                      for c in as_objects(x.get("checks", []), f"{exp_path} checks")]
            summary["expectations_failing"] = _jsons(sc.check_all(sc.Model(pres_after), checks))
        # The same PDF again: a sync that settled writes nothing.
        r2 = cli("sync", pdf, "--deck", out, timeout=CLI_TIMEOUT)
        (folder / "settle.log").write_text(r2.stdout[-30000:] + "\n--- stderr\n" + r2.stderr[-20000:], encoding="utf-8")
        settle_requests = requests_of(r2.stdout)
        summary["settle_code"], summary["settle_requests"] = r2.returncode, settle_requests
        if settle_requests:
            shutil.copyfile(report_path, folder / "settle-report.json")
            pres_settled = read_pres(state.pid)
            thumbs(state.pid, pres_settled, j / "thumbs" / f"r{n}-resynced")
        summary["conflicts"] = _jsons([f"{c.get('slide')} / {c.get('element')}: {c.get('field')} ({c.get('resolution')})"
                                       for c in as_objects(report.get("conflicts", []), "report conflicts")])
        summary["warnings"] = report.get("warnings", [])
        summary["counts"] = {k: _count(report, k) for k in ("applied", "overrides", "conflicts", "warnings")}
    else:
        new_base = base

    listing, gone = which_slide_is_which(pres_before, pres_after, new_base, pages, j, n)
    slides_gone: list[Json] = [g for g in gone]
    summary["slides"], summary["slides_gone"] = _jsons(listing), slides_gone
    summary["seconds"] = round(time.time() - t0)
    write_json(j / f"summary-r{n}.json", summary)
    state = replace(state.with_last(replace(rnd, synced=True)), sources=(*state.sources, str(tex)))
    save_state(state)
    print(json.dumps({k: v for k, v in summary.items() if k != "slides"}, indent=1, ensure_ascii=False)[:6000])
    print(f"pictures: {j / 'cmp' / f'r{n}-NNN.png'}")
    return 0


def _count(report: JsonObject, key: str) -> int:
    """How many entries the report lists under `key` (absent or null: none)."""
    v = report.get(key)
    return len(as_array(v, f"report {key}")) if v else 0


def _slide_ids(pres: JsonObject, where: str) -> list[str]:
    return [as_str(s.get("objectId"), f"{where} objectId") for s in as_objects(pres.get("slides", []), f"{where} slides")]


def which_slide_is_which(pres_before: JsonObject, pres_after: JsonObject, new_base: JsonObject, pages: list[Path],
                         j: Path, n: int) -> tuple[list[JsonObject], list[int]]:
    """Per synced slide, the pictures `cmp/rN-NNN.png`: the same slide as the person left it | its
    new PDF page | the synced slide; and the listing of which is which, with the slides the sync
    took away (numbered as the person left them)."""
    edited_idx = {sid: i for i, sid in enumerate(_slide_ids(pres_before, "before"))}
    page_of: dict[str, int | None] = {}
    for s in as_objects(new_base.get("slides", []), "base slides"):
        oid, p = s.get("objectId"), s.get("page")
        if isinstance(oid, str):
            page_of[oid] = None if p is None else as_int(p, "base slide page")
    cmp = j / "cmp"
    cmp.mkdir(exist_ok=True)
    listing: list[JsonObject] = []
    after_ids = _slide_ids(pres_after, "after")
    for i, sid in enumerate(after_ids):
        e = edited_idx.get(sid)
        p = page_of.get(sid)
        edited = _open(j / "thumbs" / f"r{n}-edited" / f"{e + 1:03d}.png") if e is not None else None
        page = _open(pages[p]) if p is not None and p < len(pages) else None
        synced = _open(j / "thumbs" / f"r{n}-synced" / f"{i + 1:03d}.png")
        edited_label = f"AS THE PERSON LEFT IT (slide {e + 1})" if e is not None else "(no such slide before the sync)"
        page_label = f"NEW SOURCE PDF page {p + 1}" if page is not None and p is not None \
            else "(no source page: the person's slide)"
        row([panel(edited, edited_label), panel(page, page_label),
             panel(synced, f"AFTER SYNC slide {i + 1}")]).save(cmp / f"r{n}-{i + 1:03d}.png")
        listing.append({"synced": i + 1, "id": sid, "edited": None if e is None else e + 1,
                        "page": None if p is None else p + 1})
    gone = [i + 1 for sid, i in edited_idx.items() if sid not in set(after_ids)]
    return listing, gone


def pull(journey: str) -> int:
    state = load_state(journey)
    j = journey_dir(journey)
    k = sum(1 for p in j.glob("pull*") if p.is_dir()) + 1
    folder = j / f"pull{k}"
    if folder.exists():
        shutil.rmtree(folder)
    src = Path(state.sources[-1])
    work, edited = folder / "work", folder / "src"
    r = cli("pull", "--deck", state.out, "--tex", src, "--out", edited, "--work", work, timeout=CLI_TIMEOUT)
    (folder / "pull.log").write_text(r.stdout[-30000:] + "\n--- stderr\n" + r.stderr[-20000:], encoding="utf-8")
    result: JsonObject = {"pull_code": r.returncode}
    out_tex = next(iter(edited.rglob(src.name)), None) if edited.exists() else None
    if out_tex is None:
        result["error"] = "pull wrote no source" + (r.stderr[-1500:] if r.returncode else "")
    else:
        diff = difflib.unified_diff(src.read_text(encoding="utf-8").splitlines(), out_tex.read_text(encoding="utf-8").splitlines(),
                                    "before-pull.tex", "after-pull.tex", lineterm="", n=2)
        (folder / "diff.txt").write_text("\n".join(diff), encoding="utf-8")
        for name in ("edits.md",):
            found = next(iter(work.rglob(name)), None) if work.exists() else None
            if found:
                shutil.copyfile(found, folder / name)
        try:
            pages = render_pages(compile_tex(out_tex), folder / "pages")
            pres = read_pres(state.pid)
            thumbs(state.pid, pres, folder / "deck")
            for i in range(max(len(pages), len(as_array(pres.get("slides", []), "slides")))):
                row([panel(_open(folder / "deck" / f"{i + 1:03d}.png"), f"DECK slide {i + 1}"),
                     panel(_open(pages[i]) if i < len(pages) else None, f"PULLED SOURCE page {i + 1}")]
                    ).save(folder / f"cmp-{i + 1:03d}.png")
        except Exception as e:  # noqa: BLE001
            result["compile_error"] = f"{type(e).__name__}: {e}"[:3000]
    write_json(folder / "summary.json", result)
    print(json.dumps(result, indent=1)[:3000], f"\nsee {folder}")
    return 0


def main(argv: list[str] | None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("tex", type=Path)
    s.add_argument("--journey", required=True)
    s.add_argument("--slot", required=True)
    e = sub.add_parser("edit")
    e.add_argument("--journey", required=True)
    e.add_argument("edits", type=Path)
    y = sub.add_parser("sync")
    y.add_argument("--journey", required=True)
    y.add_argument("tex", type=Path)
    u = sub.add_parser("pull")
    u.add_argument("--journey", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--journey", required=True)
    a = p.parse_args(argv)
    cmd: str = a.cmd
    journey: str = a.journey
    if cmd == "start":
        tex: Path = a.tex
        slot: str = a.slot
        return start(tex, journey, slot)
    if cmd == "edit":
        edits: Path = a.edits
        return edit(journey, edits)
    if cmd == "sync":
        revision: Path = a.tex
        return sync(journey, revision)
    if cmd == "pull":
        return pull(journey)
    state = load_state(journey)
    pres = read_pres(state.pid)
    save_dump(pres, journey_dir(journey) / "deck-now.md")
    print(journey_dir(journey) / "deck-now.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(None))
