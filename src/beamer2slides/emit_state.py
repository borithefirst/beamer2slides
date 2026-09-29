"""emit.json: what `emit.build_deck` made of a deck, as a record (`EmitState`).

`write` is the one place emit.json is written, and `read` / `emit_state` read it back; the record
writes the JSON it was read from key for key (`emit_state_json`), so a file read and written again
is the same bytes. What a reader needs of a file that may be older than a key is None here: slides
of old conversions carry no `objects`/`groups`, and no `table_margins` before the .pptx brought
tables."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_optional_str, as_str

FILE = "emit.json"

Margins = tuple[tuple[float, ...], ...]
"""A table's cell margins as `emit.pptx_table` gave them, row by row."""


@dataclass(frozen=True, kw_only=True)
class SlideState:
    """One slide emit wrote: its PDF page and objectId, each element's main object (`elements`,
    in `DeckPlan.slide_parts` order), every object made for each element (`objects`, main first)
    and the slide's other groups; the cell margins of each table the .pptx brought, by the
    element's index as text (`table_margins`, which a sync refills in place)."""
    page: int
    object_id: str
    elements: tuple[str, ...]
    objects: tuple[tuple[str, ...], ...] | None
    groups: tuple[str, ...] | None
    table_margins: dict[str, Margins] | None


@dataclass(frozen=True, kw_only=True)
class Contained:
    """An element `DeckPlan.contain` made the picture of its region: why emit could not plan it."""
    page: int
    id: str
    kind: str | None
    error: str


@dataclass(frozen=True, kw_only=True)
class ThemeState:
    """What emit put on the master and the layouts (`emit.plan_theme`): the page ground, the
    master's colour, each group's decoration file (relative to the out folder) and each page's
    layout (keyed by the page as text). theme_sync reads it (`theme_sync.emitted_theme`)."""
    ground: str
    master: str | None
    decorations: dict[str, str | None]
    layouts: dict[str, str]


@dataclass(frozen=True, kw_only=True)
class EmitState:
    """emit.json. `previous`: what this run replaced and how to get it back (`emit.plan_rebuild`'s
    entry, the one `guard.record` logs), None when it replaced nothing."""
    presentation_id: str
    url: str
    scale: float
    slides: tuple[SlideState, ...]
    contained: tuple[Contained, ...] | None
    theme: ThemeState | None
    previous: JsonObject | None


@dataclass(frozen=True, kw_only=True)
class Emitted:
    """What `emit.emit` built: emit.json's state, and the deck as it was written (`DeckPlan.deck`:
    blocks merged, what emit could not plan made pictures), which the sync base is made from."""
    state: EmitState
    deck: JsonObject


# ---------------------------------------------------------------- reading

def _num(v: Json, where: str) -> float:
    """A number as it came: an int stays an int, so a file written again is the same bytes."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected")


def _strs(v: Json, where: str) -> tuple[str, ...]:
    return tuple(as_str(x, where) for x in as_array(v, where))


def _margins(v: Json, where: str) -> Margins:
    return tuple(tuple(_num(x, where) for x in as_array(row, where)) for row in as_array(v, where))


def slide_state(v: Json, where: str) -> SlideState:
    m = as_object(v, where)
    objects, groups, margins = m.get("objects"), m.get("groups"), m.get("table_margins")
    return SlideState(
        page=as_int(m.get("page"), f"{where}.page"), object_id=as_str(m.get("objectId"), f"{where}.objectId"),
        elements=_strs(m.get("elements"), f"{where}.elements"),
        objects=None if objects is None else tuple(_strs(o, f"{where}.objects")
                                                   for o in as_array(objects, f"{where}.objects")),
        groups=None if groups is None else _strs(groups, f"{where}.groups"),
        table_margins=None if margins is None else {k: _margins(t, f"{where}.table_margins")
                                                    for k, t in as_object(margins, f"{where}.table_margins").items()})


def contained(v: Json, where: str) -> Contained:
    m = as_object(v, where)
    return Contained(page=as_int(m.get("page"), f"{where}.page"), id=as_str(m.get("id"), f"{where}.id"),
                     kind=as_optional_str(m.get("kind"), f"{where}.kind"), error=as_str(m.get("error"), f"{where}.error"))


def theme_state(v: Json, where: str) -> ThemeState:
    m = as_object(v, where)
    return ThemeState(ground=as_str(m.get("ground"), f"{where}.ground"),
                      master=as_optional_str(m.get("master"), f"{where}.master"),
                      decorations={k: as_optional_str(p, f"{where}.decorations")
                                   for k, p in as_object(m.get("decorations"), f"{where}.decorations").items()},
                      layouts={k: as_str(n, f"{where}.layouts")
                               for k, n in as_object(m.get("layouts"), f"{where}.layouts").items()})


def emit_state(v: Mapping[str, Json], where: str) -> EmitState:
    contained_, theme, previous = v.get("contained"), v.get("theme"), v.get("previous")
    return EmitState(
        presentation_id=as_str(v.get("presentationId"), f"{where}: presentationId"),
        url=as_str(v.get("url"), f"{where}: url"), scale=_num(v.get("scale"), f"{where}: scale"),
        slides=tuple(slide_state(s, f"{where}: slides[{i}]") for i, s in enumerate(as_array(v.get("slides"), f"{where}: slides"))),
        contained=None if contained_ is None else tuple(contained(c, f"{where}: contained")
                                                        for c in as_array(contained_, f"{where}: contained")),
        theme=None if theme is None else theme_state(theme, f"{where}: theme"),
        previous=None if previous is None else as_object(previous, f"{where}: previous"))


def read(out: Path) -> EmitState:
    """The output folder's emit.json (a missing or foreign file raises)."""
    path = out / FILE
    return emit_state(as_object(json.loads(path.read_text(encoding="utf-8")), str(path)), str(path))


# ---------------------------------------------------------------- writing

def _json_strs(xs: Sequence[str]) -> list[Json]:
    return [x for x in xs]


def _json_margins(m: Margins) -> list[Json]:
    return [[x for x in row] for row in m]


def slide_state_json(s: SlideState) -> JsonObject:
    out: JsonObject = {"page": s.page, "objectId": s.object_id, "elements": _json_strs(s.elements)}
    if s.objects is not None:
        out["objects"] = [_json_strs(o) for o in s.objects]
    if s.groups is not None:
        out["groups"] = _json_strs(s.groups)
    if s.table_margins is not None:
        out["table_margins"] = {k: _json_margins(m) for k, m in s.table_margins.items()}
    return out


def contained_json(c: Contained) -> JsonObject:
    return {"page": c.page, "id": c.id, "kind": c.kind, "error": c.error}


def theme_state_json(t: ThemeState) -> JsonObject:
    decorations: JsonObject = {k: p for k, p in t.decorations.items()}
    layouts: JsonObject = {k: n for k, n in t.layouts.items()}
    return {"ground": t.ground, "master": t.master, "decorations": decorations, "layouts": layouts}


def emit_state_json(s: EmitState) -> JsonObject:
    """In the order `emit.build_deck` has always written them."""
    out: JsonObject = {"presentationId": s.presentation_id, "url": s.url, "scale": s.scale,
                       "slides": [slide_state_json(x) for x in s.slides]}
    if s.contained is not None:
        out["contained"] = [contained_json(c) for c in s.contained]
    if s.theme is not None:
        out["theme"] = theme_state_json(s.theme)
    if s.previous is not None:
        out["previous"] = s.previous
    return out


def write(out: Path, state: EmitState) -> Path:
    """emit.json: written here and nowhere else."""
    path = out / FILE
    path.write_text(json.dumps(emit_state_json(state), indent=1), encoding="utf-8")
    return path
