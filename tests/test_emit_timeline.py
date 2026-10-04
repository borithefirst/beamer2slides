"""When `emit.build_deck` reads and writes what, against a fake Slides that records each call and
the thread it came from (no Google).

Three things a faster conversion must not change: the layout pass's batches land before the first
content batch (a layout batch landing after one takes every title's box away: the race of
`tools/probe_layout_race.py`), they are the batches its two reads of their own used to make, and
measure_places' scratch slides are deleted only once their places are measured, a refused delete
still failing the conversion. What may change: the layout pass reads nothing (it works off the
import's own read), the copies' read is in the air while measure_places works, and the scratch
slides go beside the content rather than after it."""

from __future__ import annotations

import copy
import io
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

import pytest
from googleapiclient.errors import HttpError
from PIL import Image

from beamer2slides import emit, ir
from beamer2slides.emit_metrics import PPTX_TITLE_DY, FontMapper
from beamer2slides.emit_model import JsonMap, Place, objects_of
from beamer2slides.emit_theme import LAYOUT_TEXT_PREFIX, master_plan_of, style_layout_placeholders, write_layout_texts
from beamer2slides.google_auth import use_services
from beamer2slides.google_types import (BatchUpdateResponse, DocsService, DriveFile, DriveService, Files, Presentation,
                                        Presentations, Request, SlidesRequest, SlidesService, presentation,
                                        slides_json)
from beamer2slides.gslides import EMU_PER_PT
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.typing_compat import override

from .fake_google import Answer, Later, NoDrive, NoFiles, NoPresentations, NoSlides
from .ir_sources import source_slides
from .json_reads import jobjs, jstr
from .test_ir import small_typed

if TYPE_CHECKING:
    from google.auth.credentials import Credentials
    from typing_extensions import Unpack

    from beamer2slides.google_types import GetPresentation, UpdateFile, UpdatePresentation

PID = "deck-1"
SCRATCH = ("b2s_m000", "b2s_m001")
SIZE = (362.83, 272.13)

What = Literal["get", "batch", "measured", "refused"]


@dataclass(frozen=True, kw_only=True)
class Call:
    """One call the fake Slides answered (or refused), in the order they reached it."""
    what: What
    thread: str
    fields: str | None
    requests: tuple[SlidesRequest, ...]


def emu(v: float) -> JsonObject:
    return {"magnitude": round(v * EMU_PER_PT), "unit": "EMU"}


def placeholder(oid: str, kind: str) -> JsonObject:
    """A master or layout placeholder as the import brings it: a "\\n" of text, a size, a place."""
    return {"objectId": oid, "size": {"width": emu(300.0), "height": emu(40.0)},
            "transform": {"scaleX": 1.0, "scaleY": 1.0, "translateX": 0.0, "translateY": 0.0, "unit": "EMU"},
            "shape": {"shapeType": "TEXT_BOX", "placeholder": {"type": kind},
                      "text": {"textElements": [{"endIndex": 1, "paragraphMarker": {}},
                                                {"endIndex": 1, "textRun": {"content": "\n"}}]}}}


def is_scratch_delete(r: SlidesRequest) -> bool:
    gone = r.get("deleteObject")
    return gone is not None and gone["objectId"].startswith("b2s_m")


def http_error(status: int) -> HttpError:
    return HttpError(resp=type("Resp", (), {"status": status, "reason": "no"})(), content=b"{}")


class World:
    """One presentation every client of the run reads and writes: its slides, master and layouts,
    and the calls made on it. `refuse_scratch`: the scratch slides' delete is refused."""

    def __init__(self, refuse_scratch: bool) -> None:
        self.lock = threading.Lock()
        self.calls: list[Call] = []
        self.slides: list[JsonObject] = []
        self.masters: list[JsonObject] = [
            {"objectId": "m0", "pageElements": [placeholder("m0_title", "TITLE"), placeholder("m0_body", "BODY")]}]
        self.layouts: list[JsonObject] = [
            {"objectId": f"l{i}", "layoutProperties": {"name": name},
             "pageElements": [placeholder(f"l{i}_title", "TITLE"), placeholder(f"l{i}_body", "BODY")]}
            for i, name in enumerate(("TITLE_AND_BODY", "TITLE_ONLY", "BLANK"))]
        self.refuse_scratch = refuse_scratch
        self.measuring = threading.Event()
        self.copies_read = threading.Event()
        self.read_while_measuring = False
        self.overlapped = False

    def note(self, what: What, fields: str | None, requests: Sequence[SlidesRequest]) -> None:
        with self.lock:
            self.calls.append(Call(what=what, thread=threading.current_thread().name, fields=fields,
                                   requests=tuple(requests)))

    def imported(self, pptx: bytes) -> None:
        with self.lock:
            self.slides = source_slides(io.BytesIO(pptx))

    def get(self, fields: str | None) -> Presentation:
        self.note("get", fields, [])
        with self.lock:
            if fields is not None and fields.startswith("slides("):
                answer: JsonObject = {"slides": [{
                    "objectId": s["objectId"],
                    "pageElements": [{"objectId": e["objectId"], "size": e["size"]} for e in jobjs(s, "pageElements")],
                    "slideProperties": {"notesPage": {"notesProperties": {
                        "speakerNotesObjectId": f"{jstr(s, 'objectId')}_notes"}}}} for s in self.slides]}
            else:   # the import's read, and any other: everything (a field mask asks for less)
                answer = {"presentationId": PID, "pageSize": {"width": emu(SIZE[0]), "height": emu(SIZE[1])},
                          "slides": [copy.deepcopy(s) for s in self.slides],
                          "masters": [copy.deepcopy(p) for p in self.masters],
                          "layouts": [copy.deepcopy(p) for p in self.layouts]}
        if fields is not None and fields.startswith("slides("):
            # (answered only once measure_places is at work, or after a while: `measuring`)
            self.read_while_measuring = self.measuring.wait(10)
            self.copies_read.set()
        return presentation(answer, "the world")

    def update(self, requests: Sequence[SlidesRequest]) -> BatchUpdateResponse:
        if requests and all(is_scratch_delete(r) for r in requests) and self.refuse_scratch:
            self.note("refused", None, requests)
            raise http_error(400)
        self.note("batch", None, requests)
        with self.lock:
            for r in requests:
                self.apply(r)
        return BatchUpdateResponse(presentationId=PID, replies=[{} for _ in requests])

    def apply(self, r: SlidesRequest) -> None:
        """What the layout pass and phase 1 change, as Slides would: copies, deletes, layout boxes."""
        dup = r.get("duplicateObject")
        if dup is not None:
            ids = dup.get("objectIds", {})
            source = next(s for s in self.slides if s["objectId"] == dup["objectId"])
            elements: list[Json] = [{"objectId": ids.get(jstr(e, "objectId"), f"{jstr(e, 'objectId')}_copy"),
                                     "size": e["size"]} for e in jobjs(source, "pageElements")]
            self.slides.append({"objectId": ids.get(dup["objectId"], f"{dup['objectId']}_copy"),
                                "pageElements": elements})
        gone = r.get("deleteObject")
        if gone is not None:
            oid = gone["objectId"]
            self.slides = [s for s in self.slides if s["objectId"] != oid]
            for page in self.slides + self.masters + self.layouts:
                kept: list[Json] = [e for e in jobjs(page, "pageElements") if e["objectId"] != oid]
                page["pageElements"] = kept
        made = r.get("createShape")
        props = None if made is None else made.get("elementProperties")
        if made is not None and props is not None:
            for page in self.layouts:
                if page["objectId"] == props["pageObjectId"]:
                    grown: list[Json] = [*jobjs(page, "pageElements"), {"objectId": made.get("objectId", "")}]
                    page["pageElements"] = grown


class WorldSlides(NoSlides, NoPresentations):
    """A Slides client over the world (a fresh one per thread, as the library builds them)."""

    def __init__(self, world: World) -> None:
        self.world = world

    @override
    def presentations(self) -> Presentations:
        return self

    @override
    def get(self, **kw: Unpack[GetPresentation]) -> Request[Presentation]:
        fields = kw.get("fields")
        return Later(lambda: self.world.get(fields))

    @override
    def batchUpdate(self, **kw: Unpack[UpdatePresentation]) -> Request[BatchUpdateResponse]:
        requests = kw["body"]["requests"]
        return Later(lambda: self.world.update(requests))


class WorldDrive(NoFiles, NoDrive):
    """Drive as far as an in-place import: the .pptx uploaded becomes the world's slides."""

    def __init__(self, world: World) -> None:
        self.world = world

    @override
    def files(self) -> Files:
        return self

    @override
    def update(self, **kw: Unpack[UpdateFile]) -> Request[DriveFile]:
        media = kw.get("media_body")
        assert media is not None, "the deck is rebuilt from a .pptx"
        self.world.imported(media.getbytes(0, media.size()))
        return Answer(DriveFile(id=kw["fileId"]))


class WorldBuilder:
    """`use_services`' builder: a new Slides client per call (so build_deck runs threaded)."""

    def __init__(self, world: World) -> None:
        self.world = world

    @overload
    def __call__(self, api: Literal["slides"], version: str, creds: Credentials | None) -> SlidesService | None: ...
    @overload
    def __call__(self, api: Literal["drive"], version: str, creds: Credentials | None) -> DriveService | None: ...
    @overload
    def __call__(self, api: Literal["docs"], version: str, creds: Credentials | None) -> DocsService | None: ...

    def __call__(self, api: str, version: str,
                 creds: Credentials | None) -> SlidesService | DriveService | DocsService | None:
        return WorldSlides(self.world) if api == "slides" else None


def footer() -> ir.ThemeText:
    run: ir.Run = {"text": "Footer", "font": "CMSS10", "family": "sans", "size": 8.0, "bold": False, "italic": False,
                   "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False,
                   "strike": False, "highlight": None}
    para: ir.Paragraph = {"align": "left", "level": 0, "bullet": None, "size": 8.0, "text_x0": 20.0, "tab_x0": None,
                          "lines": [{"baseline": 260.0, "x0": 20.0, "x1": 50.0}], "wrap_limit": None, "runs": [run]}
    return {"kind": "text", "role": "layout", "bbox": [20.0, 252.0, 50.0, 262.0], "panel": None, "code": False,
            "key": ("Footer", 20, 260, "#000000"), "chars": 6, "paragraphs": [para], "spans": []}


def deck_in(out: Path) -> JsonObject:
    """A one-slide rendered deck with a layout text, its background picture written under `out`."""
    typed = small_typed()
    typed["layout_texts"] = [footer()]
    deck = ir.deck_json(typed)
    for s in jobjs(deck, "slides"):
        s.update(background="backgrounds/bg-001.png", background_color=None)
    (out / "backgrounds").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (363, 272), "white").save(out / "backgrounds" / "bg-001.png")
    return deck


MeasurePlaces = Callable[[SlidesService, str, JsonMap, float, FontMapper, Callable[[JsonObject, int], JsonObject],
                          Mapping[int, str], Path, float], "tuple[dict[str, Place], list[str]]"]


def measuring(world: World, wait: bool) -> MeasurePlaces:
    """measure_places as far as the timeline goes: it leaves scratch slides, and where `wait`, it
    waits (a while) for the copies' read to be answered - which the world does only while this
    is at work, so both waits end at once only when that read is in the air beside it."""
    def measure(slides: SlidesService, pid: str, deck: JsonMap, scale: float, fonts: FontMapper,
                placed: Callable[[JsonObject, int], JsonObject], page_slide: Mapping[int, str], out: Path,
                page_width: float) -> tuple[dict[str, Place], list[str]]:
        world.measuring.set()
        world.overlapped = wait and world.copies_read.wait(10) and world.read_while_measuring
        world.note("measured", None, [])
        return {}, list(SCRATCH)
    return measure


Kind = Literal["get", "measured", "refused", "copy", "scratch", "layout", "content"]


def targets(value: Json) -> set[str]:
    """Every object a request names (its `objectId`s and `pageObjectId`s, at any depth)."""
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("objectId", "pageObjectId") and isinstance(v, str):
                found.add(v)
            else:
                found |= targets(v)
    elif isinstance(value, list):
        for v in value:
            found |= targets(v)
    return found


def on_layouts(oid: str) -> bool:
    """A master's or a layout's own object (the world's ids), or a layout text the pass makes."""
    return oid.startswith((LAYOUT_TEXT_PREFIX, "m0", "l0", "l1", "l2"))


def kind_of(c: Call) -> Kind:
    if c.what != "batch":
        return c.what
    if c.requests and all(is_scratch_delete(r) for r in c.requests):
        return "scratch"
    if any("duplicateObject" in r for r in c.requests):
        return "copy"
    if all(on_layouts(oid) for r in c.requests for oid in targets(slides_json(r))):
        return "layout"
    return "content"


def kinds(world: World) -> list[Kind]:
    return [kind_of(c) for c in world.calls]


def converted(world: World, out: Path, threaded: bool) -> None:
    deck = deck_in(out)
    if threaded:
        with use_services(WorldBuilder(world)):
            emit.build_deck(WorldSlides(world), WorldDrive(world), deck, out, "T", PID, True)
    else:
        one = WorldSlides(world)
        with use_services({"slides": one}):
            emit.build_deck(one, WorldDrive(world), deck, out, "T", PID, True)


def two_read_layout_batches(out: Path) -> list[tuple[SlidesRequest, ...]]:
    """What the layout pass sent when it read the masters and layouts itself (once for the texts,
    once more for the placeholders after the texts' batch), against a world as the import left it."""
    world = World(False)
    plan = emit.upload_plan(deck_in(out), out)
    ground = master_plan_of(plan.deck, out, "plan").ground
    slides = WorldSlides(world)
    write_layout_texts(slides, PID, objects_of(plan.deck.get("layout_texts", []), "layout_texts"), plan.scale, plan.fonts)
    style_layout_placeholders(slides, PID, plan.deck, plan.scale, plan.fonts, PPTX_TITLE_DY, ground)
    assert [c.what for c in world.calls] == ["get", "batch", "get", "batch"], "two reads, two batches"
    return [c.requests for c in world.calls if c.what == "batch"]


def test_the_layout_pass_reads_nothing_and_writes_what_its_two_reads_wrote(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch):
    world = World(False)
    monkeypatch.setattr(emit, "measure_places", measuring(world, True))
    converted(world, tmp_path / "new", True)
    asked = [c.fields for c in world.calls if c.what == "get"]
    assert not [f for f in asked if f is not None and ("layouts(" in f or "masters(" in f)], asked
    assert len(asked) == 2, f"the import's read and the copies' read, nothing else: {asked}"
    layout = [c.requests for c in world.calls if kind_of(c) == "layout"]
    assert layout == two_read_layout_batches(tmp_path / "old")
    # (both batches did something: the footer onto every layout, the body look into every BODY)
    assert any("createShape" in r for r in layout[0]) and any("updateTextStyle" in r for r in layout[1])


def test_every_layout_batch_lands_before_the_first_content_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    world = World(False)
    monkeypatch.setattr(emit, "measure_places", measuring(world, True))
    converted(world, tmp_path, True)
    seen = kinds(world)
    assert "content" in seen and seen.count("layout") == 2, seen
    assert max(i for i, k in enumerate(seen) if k == "layout") < seen.index("content"), seen
    assert seen.index("copy") < min(i for i, k in enumerate(seen) if k == "layout"), seen


def test_the_copies_are_read_while_the_places_are_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    world = World(False)
    monkeypatch.setattr(emit, "measure_places", measuring(world, True))
    converted(world, tmp_path, True)
    assert world.overlapped, "the copies' read was not answered while measure_places worked"


def test_the_scratch_slides_go_once_measured_and_behind_the_layout_pass(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch):
    world = World(False)
    monkeypatch.setattr(emit, "measure_places", measuring(world, True))
    converted(world, tmp_path, True)
    seen = kinds(world)
    assert seen.count("scratch") == 1, seen
    at = seen.index("scratch")
    assert seen.index("measured") < at, seen
    assert max(i for i, k in enumerate(seen) if k == "layout") < at, seen
    gone = [r.get("deleteObject") for r in world.calls[at].requests]
    assert [g["objectId"] for g in gone if g is not None] == list(SCRATCH)


def test_a_refused_scratch_delete_still_fails_the_conversion_after_the_content(tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch):
    world = World(True)
    monkeypatch.setattr(emit, "measure_places", measuring(world, True))
    with pytest.raises(HttpError):
        converted(world, tmp_path, True)
    seen = kinds(world)
    assert "refused" in seen and "content" in seen, seen


def test_one_lent_client_keeps_the_serial_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A caller's own client (use_services with a mapping): one call at a time, layouts first,
    the scratch slides last, all on the caller's thread."""
    world = World(False)
    monkeypatch.setattr(emit, "measure_places", measuring(world, False))
    converted(world, tmp_path, False)
    assert {c.thread for c in world.calls} == {threading.current_thread().name}
    seen = [k for k in kinds(world) if k != "get"]
    assert seen == ["copy", "layout", "layout", "measured", "content", "scratch"], seen
