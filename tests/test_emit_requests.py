"""Offline checks of the requests emit sends to Google (emit.plan_offline) over every built deck,
replayed on a model of the presentation, and unit tests of emit's placement helpers (no Google API).

The PDFs come from `python tests/decks/build.py` and `python tests/themes/sweep.py --build`."""

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest
from pptx.presentation import Presentation as PptxPresentation

from beamer2slides import emit, emit_holes
from beamer2slides.classify import HOLE_PAD
from beamer2slides.emit import (EMU_PER_PT, HOLE_FONT, HOLE_SPACE_EM, SLIDE_W, FontMapper, find_marks, fit_holes,
                                formula_shifts, hole_offset, hole_run, mark_alpha, number_box_requests,
                                measure_jobs, overlay_boxes, pick_gap, slide_holes, space_shift)
from beamer2slides.emit_metrics import unspaced
from beamer2slides.emit_model import Place, PptxText, set_text, table_of, text_of
from beamer2slides.emit_pptx import SHELL_CHAR
from beamer2slides.emit_tables import pptx_table_of, table_requests_of
from beamer2slides.emit_widths import ADDED_SPACE, HOLE_BREAKS
from beamer2slides.emit_text import text_box_requests_of, words_right
from beamer2slides.fonts import font_info
from beamer2slides.google_types import SlidesRequest, part_json, slides_json, slides_request_kind
from beamer2slides.ir_types import Mark, TextElement
from beamer2slides.json_types import Json, JsonObject, as_str

from . import built_decks
from .json_reads import jarr, jat, jbool, jint, jnum, jnums, jobj, jobjs, jstr, jstrs

TESTS = Path(__file__).resolve().parent
NBSP = "\xa0"
# slide pt a predicted picture may move from its PDF place: where Lato sets a long line's end.
# demo p3's hole after 'for x ∈ ℝ and n ≥ 2, the mean' is 16.7 (∈ ≥ in STIX Two Math's measured
# advances since 2026-10-04), and live it sits 0.34 pt from its words' (tools/alignment.py)
MAX_SHIFT = 18.0

Box4 = tuple[float, float, float, float]


def pdfs() -> list[Path]:
    out = sorted((TESTS / "decks" / "out").glob("*.pdf")) + sorted((TESTS / "decks" / "out" / "notes").glob("*.pdf"))
    out += sorted((TESTS / "themes" / "out").glob("*/talk.pdf"))
    demo = TESTS.parent / "examples" / "demo" / "demo.pdf"
    return out + ([demo] if demo.exists() else [])


# ---------------------------------------------------------------- replaying a deck's requests

def box_of(transform: JsonObject, w: float, h: float) -> Box4:
    """Page box (pt) of an element of size w × h under an affine transform."""
    def part(key: str) -> float:  # (a part the transform leaves out is 0)
        return jnum(transform, key) if key in transform else 0.0

    k = 1 / EMU_PER_PT if transform.get("unit") == "EMU" else 1.0
    xs: list[float] = []
    ys: list[float] = []
    for x, y in ((0.0, 0.0), (w, 0.0), (0.0, h), (w, h)):
        xs.append(part("scaleX") * x + part("shearX") * y + part("translateX") * k)
        ys.append(part("shearY") * x + part("scaleY") * y + part("translateY") * k)
    return min(xs), min(ys), max(xs), max(ys)


def pt_of(dim: Json) -> float:
    return jnum(dim, "magnitude") / EMU_PER_PT if jstr(dim, "unit") == "EMU" else jnum(dim, "magnitude")


def collapse(text: str) -> str:
    # (emit.HOLE_BREAK; the spaces classify added, kept or not by emit_text.within_budget)
    return re.sub(NBSP + "+", NBSP, ADDED_SPACE.sub("", "".join(ch for ch in text if ch not in HOLE_BREAKS)))


def run_text(runs: Sequence[Mapping[str, Json]]) -> str:
    return "".join(as_str(r["text"], "text") for r in runs)


def text_requests(el: JsonObject, scale: float) -> list[JsonObject]:
    """The requests of a text element's box, planned from its record (`text_box_requests_of`)."""
    return [slides_json(r) for r in
            text_box_requests_of(text_of(el), "b2s_s001", "b2s_s001_t0", scale, FONTS, None, None, None, None, None)]


def table_requests(el: JsonObject, slide_id: str, object_id: str, scale: float, imported: bool) -> list[JsonObject]:
    """The requests of a table element on a deck SLIDE_W wide, planned from its record (`table_requests_of`)."""
    return [slides_json(r) for r in table_requests_of(table_of(el), slide_id, object_id, scale, FONTS, imported,
                                                      SLIDE_W / scale)]


def slides_w(runs: Sequence[JsonObject], scale: float) -> float:
    """What Slides sets `runs` at (pt): only runs whose advances were measured."""
    width = emit.slides_width(runs, scale, FONTS)
    assert width is not None, "runs Slides was never measured on"
    return width


class Emitted:
    """One deck's requests in the order emit sends them, replayed on a model of the presentation:
    objects and their pages, groups, text boxes' text. Violations go to `problems[invariant]`."""

    def __init__(self, name: str, deck: JsonObject) -> None:
        self.name = name
        self.result = emit.plan_offline(deck)
        self.plan = self.result["plan"]
        self.slides: dict[int, JsonObject] = {jint(s, "page"): s for s in self.plan.slides()}
        w, h = jnums(self.plan.slides()[0], "size")
        self.page_h = SLIDE_W * h / w
        self.problems: dict[str, list[str]] = defaultdict(list)
        self.page_of: dict[str, str | None] = {}  # object id -> page id (None: a page)
        self.parent: dict[str, str] = {}  # object id -> group id
        self.groups: dict[str, list[str]] = {}
        self.texts: dict[str, str] = {}
        self.font_max: dict[str, float] = {}
        self.sizes: dict[str, tuple[float, float]] = {}
        self.boxes: dict[str, Box4] = {}  # object id -> page box (pt) where its creation gives one
        self.element: JsonObject | None = None  # the element whose requests are being replayed
        self.owner: dict[str, JsonObject] = {}  # object id -> the element whose requests made it
        # text shells the .pptx carries (bullets no preset draws): object id -> the shell
        self.shells: dict[str, PptxText] = {
            f"b2s_s{jint(s, 'page'):03}_t{i}": shell for s in self.plan.slides()
            for i, shell in zip(self.plan.shell_indices(s), self.plan.shells(s))}
        self.replay()

    def flag(self, invariant: str, where: str, message: str) -> None:
        self.problems[invariant].append(f"{self.name} {where}: {message}")

    def create(self, oid: str, page: str | None, where: str) -> None:
        if not 5 <= len(oid) <= 50:
            self.flag("ids", where, f"object id {oid!r} is {len(oid)} characters")
        if oid in self.page_of:
            self.flag("ids", where, f"object id {oid} created twice")
        if page is not None and self.page_of.get(page, "") is not None:
            self.flag("ids", where, f"{oid} created on {page}, which is no page")
        self.page_of[oid] = page
        if self.element is not None:
            self.owner[oid] = self.element

    def need(self, oid: str, where: str, what: str) -> bool:
        if oid not in self.page_of:
            self.flag("ids", where, f"{what} refers to {oid}, which does not exist (yet)")
            return False
        return True

    def replay(self) -> None:
        for slide_id, elements in self.result["page_elements"].items():
            self.create(slide_id, None, "import")
            for e in elements:
                oid = jstr(e, "objectId")
                self.create(oid, slide_id, "import")
                self.sizes[oid] = (pt_of(jat(e, "size", "width")), pt_of(jat(e, "size", "height")))
                shell = self.shells.get(oid)  # (a shell holds one placeholder character per paragraph)
                self.texts[oid] = "" if shell is None else "\n".join(SHELL_CHAR for _ in shell.paragraphs)
            self.create(self.result["speaker_notes"][slide_id], slide_id, "import")
        for r in self.result["measure"]:
            self.apply(r, "measure_places")
        for slide_id, page, parts, _ in self.result["slides"]:
            for el, reqs in parts:
                where = f"{slide_id} {el['id'] if el else ''}".strip()
                self.element = el
                for r in reqs:
                    self.apply(r, where)
        self.element = None
        # what the .pptx brought at its own place: pictures, empty tables, text shells (moved later)
        for slide_id, page, _, _ in self.result["slides"]:
            slide = self.slides[page]
            elements = jobjs(slide, "elements")
            for i, (e, box) in zip([i for i, e in enumerate(elements) if e["kind"] == "image"],
                                   self.result["pictures"][page]):
                self.owner[f"{slide_id}_f{i}"] = e
                self.boxes.setdefault(f"{slide_id}_f{i}", (box[0], box[1], box[2], box[3]))
            tables = [i for i, e in enumerate(elements) if e["kind"] == "table"]
            for i, t in zip(tables, self.plan.tables(slide)):
                self.owner[f"{slide_id}_tab{i}"] = elements[i]
                self.boxes.setdefault(f"{slide_id}_tab{i}", (t.x, t.y, t.x + sum(t.widths), t.y + sum(t.heights)))
            for i, e in enumerate(elements):  # (shells and placeholders: imported, then placed)
                self.owner.setdefault(f"{slide_id}_t{i}", e)

    def frame(self, oid: str) -> Box4 | None:
        """An object's frame on its page (pt), a group's the union of its children's; None for one
        whose place nothing here gives (an imported placeholder never moved)."""
        if oid in self.groups:
            kids = [b for b in (self.frame(c) for c in self.groups[oid] if c in self.page_of) if b is not None]
            if not kids:
                return None
            return (min(b[0] for b in kids), min(b[1] for b in kids), max(b[2] for b in kids), max(b[3] for b in kids))
        return self.boxes.get(oid)

    def owner_of(self, oid: str) -> JsonObject | None:
        """The element an object (or a group: its first child's) was emitted for."""
        if oid in self.owner:
            return self.owner[oid]
        for c in self.groups.get(oid, []):
            got = self.owner_of(c)
            if got is not None:
                return got
        return None

    def apply(self, request: SlidesRequest, where: str) -> None:
        kind = slides_request_kind(request)
        body = jobj(slides_json(request), kind)
        self.check_values(body, where)
        if kind == "createSlide":
            self.create(jstr(body, "objectId"), None, where)
        elif kind in ("createShape", "createTable", "createLine"):
            props = jobj(body, "elementProperties")
            oid, page = jstr(body, "objectId"), jstr(props, "pageObjectId")
            self.need(page, where, kind)
            self.create(oid, page, where)
            w, h = pt_of(jat(props, "size", "width")), pt_of(jat(props, "size", "height"))
            self.sizes[oid] = (w, h)
            self.boxes[oid] = box_of(jobj(props, "transform"), w, h)
            if kind == "createShape":
                self.texts[oid] = ""
        elif kind == "duplicateObject":
            source = jstr(body, "objectId")
            if self.need(source, where, kind):
                for new in jobj(body, "objectIds").values():
                    copy_id = as_str(new, "objectIds")
                    self.create(copy_id, self.page_of[source], where)
                    self.sizes[copy_id] = self.sizes.get(source, (0.0, 0.0))
                    self.texts[copy_id] = ""
        elif kind == "deleteObject":
            oid = jstr(body, "objectId")
            if self.need(oid, where, kind):
                del self.page_of[oid]
        elif kind == "groupObjects":
            children, group = jstrs(body, "childrenObjectIds"), jstr(body, "groupObjectId")
            if len(children) < 2 or len(set(children)) != len(children):
                self.flag("groups", where, f"group {group} of {children}")
            pages = {self.page_of.get(c) for c in children if self.need(c, where, kind)}
            for c in children:
                if c in self.parent:
                    self.flag("groups", where, f"{c} is in {self.parent[c]} and {group}")
                self.parent[c] = group
            if len(pages) > 1:
                self.flag("groups", where, f"group {group} spans pages {pages}")
            self.create(group, pages.pop() if pages else None, where)
            self.groups[group] = children
        elif kind == "updatePageElementsZOrder":
            for oid in jstrs(body, "pageElementObjectIds"):
                self.need(oid, where, kind)
        elif kind == "updatePageElementTransform":
            oid = jstr(body, "objectId")
            if self.need(oid, where, kind) and body["applyMode"] == "ABSOLUTE":
                self.boxes[oid] = box_of(jobj(body, "transform"), *self.sizes.get(oid, (0.0, 0.0)))
        else:
            oid = jstr(body, "objectId")
            if not self.need(oid, where, kind):
                return
            if kind == "updateLineProperties":
                line = jobj(body, "lineProperties")
                for end in ("startConnection", "endConnection"):
                    if end in line:
                        self.need(jstr(line, end, "connectedObjectId"), where, end)
            style: JsonObject = jobj(body, "style") if "style" in body else {}
            link: JsonObject = jobj(style, "link") if "link" in style else {}
            if "pageObjectId" in link:
                self.need(jstr(link, "pageObjectId"), where, "link")
            if "fontSize" in style:
                self.font_max[oid] = max(self.font_max.get(oid, 0.0), jnum(style, "fontSize", "magnitude"))
            if "cellLocation" in body or oid not in self.texts:
                return
            self.edit_text(kind, body, where)

    def edit_text(self, kind: str, body: JsonObject, where: str) -> None:
        # Slides counts text indices in UTF-16 code units (an astral 𝔼 is two): so does the model.
        oid = jstr(body, "objectId")
        text = self.texts[oid]
        units = text.encode("utf-16-le", "surrogatepass")
        n = len(units) // 2
        rng: JsonObject = jobj(body, "textRange") if "textRange" in body else {}
        if rng.get("type") == "FIXED_RANGE":
            start, end = jint(rng, "startIndex"), jint(rng, "endIndex")
            if not 0 <= start < end <= n + 1:  # (+1: the implicit newline ending the text)
                self.flag("ranges", where, f"{kind} range {start}..{end} outside {oid}'s {n} UTF-16 units")
            for i in (start, end):  # an index between the halves of a surrogate pair
                if 0 < i < n and 0xDC00 <= int.from_bytes(units[2 * i:2 * i + 2], "little") <= 0xDFFF:
                    self.flag("ranges", where, f"{kind} range {start}..{end} splits a surrogate pair in {oid}")
        if kind == "insertText":
            i = jint(body, "insertionIndex") if "insertionIndex" in body else 0
            units = units[:2 * i] + jstr(body, "text").encode("utf-16-le", "surrogatepass") + units[2 * i:]
        elif kind == "deleteText":
            units = units[:2 * jint(rng, "startIndex")] + units[2 * jint(rng, "endIndex"):]
        elif kind == "createParagraphBullets":
            out: list[str] = []
            pos = 0
            for para in text.split("\n"):  # (paragraphs in the range lose their leading tabs)
                size = len(para.encode("utf-16-le", "surrogatepass")) // 2
                inside = pos < jint(rng, "endIndex") and pos + size + 1 > jint(rng, "startIndex")
                out.append(para.lstrip("\t") if inside else para)
                pos += size + 1
            units = "\n".join(out).encode("utf-16-le", "surrogatepass")
        self.texts[oid] = units.decode("utf-16-le", "surrogatepass")

    def check_values(self, body: Json, where: str) -> None:
        """Font sizes, weights and element sizes positive; colours and alphas in [0, 1]."""
        stack: list[Json] = [body]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack += node
            elif isinstance(node, dict):
                for key, value in node.items():
                    if key in ("fontSize", "weight") and isinstance(value, dict) and not jnum(value, "magnitude") > 0:
                        self.flag("values", where, f"{key} {value}")
                    elif key == "rgbColor" and (set(jobj(value)) - {"red", "green", "blue"} or
                                                not all(0 <= jnum(v) <= 1 for v in jobj(value).values())):
                        self.flag("values", where, f"colour {value}")
                    elif key == "alpha" and not 0 <= jnum(value) <= 1:
                        self.flag("values", where, f"alpha {value}")
                    elif key == "size" and isinstance(value, dict) and "width" in value and \
                            (jnum(value, "width", "magnitude") < 0 or jnum(value, "height", "magnitude") < 0):
                        self.flag("values", where, f"size {value}")
                    elif isinstance(value, (dict, list)):
                        stack.append(value)

    # helpers for the invariants

    def top(self, oid: str) -> str | None:
        return self.parent.get(oid)

    def part_requests(self, slide_id: str, element_id: str) -> list[JsonObject]:
        _, _, parts, _ = next(s for s in self.result["slides"] if s[0] == slide_id)
        return [slides_json(r) for el, reqs in parts if el and el["id"] == element_id for r in reqs]

    def title_oids(self, slide: JsonObject) -> set[str]:
        slide_id = f"b2s_s{jint(slide, 'page'):03}"
        title = emit.title_element(slide)
        if title is None:
            return set()
        sub = emit.subtitle_element(slide, title)
        return {f"{slide_id}_t{title}"} | ({f"{slide_id}_t{sub}"} if sub is not None else set())


@lru_cache(maxsize=None)
def emitted() -> tuple[Emitted, ...]:
    found = pdfs()  # (classified once per run: built_decks)
    return tuple(Emitted(str(pdf.relative_to(TESTS.parent)).replace("\\", "/"), deck)
                 for pdf, deck in zip(found, built_decks.classified_decks(found)))


@pytest.fixture(scope="module")
def decks() -> tuple[Emitted, ...]:
    found = emitted()
    if not found:
        pytest.skip("no PDFs built")
    return found


def problems(decks: Sequence[Emitted], invariant: str) -> list[str]:
    return [p for d in decks for p in d.problems[invariant]]


def report(found: list[str]) -> str:
    return f"{len(found)} problem(s):\n" + "\n".join(found[:40])


@dataclass(frozen=True, kw_only=True)
class OffPage:
    """An object whose frame reaches past its slide's page by more than the tolerance."""
    slide: str
    oid: str
    element: str
    family: str
    """The element's kind/role and the object: `text/footer shape`, `diagram/figure group`..."""
    sides: str
    """Which edges it crosses, of L, T, R, B."""
    over: float
    """How far past the furthest of them (pt)."""
    box: Box4


def off_page(d: Emitted, tolerance: float) -> list[OffPage]:
    """Every object on a deck slide - a group by its children's union, as Slides frames it - whose
    frame reaches past the page by more than `tolerance` pt (CLAUDE.md: nothing the converter
    writes sticks out into the editor's canvas)."""
    out: list[OffPage] = []
    for oid, page in d.page_of.items():
        if page is None or not page.startswith("b2s_s") or "scratch" in page:
            continue
        box = d.frame(oid)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        past = (("L", -x0), ("T", -y0), ("R", x1 - SLIDE_W), ("B", y1 - d.page_h))
        sides = "".join(side for side, by in past if by > tolerance)
        if not sides:
            continue
        el = d.owner_of(oid)
        what = "group" if oid in d.groups else "picture" if re.search(r"_f\d+$", oid) else \
            "table" if "_tab" in oid else "shape"
        family = (f"{el['kind']}/{el.get('role') or ''} " if el is not None else "? ") + what
        out.append(OffPage(slide=page, oid=oid, element=jstr(el, "id") if el is not None else "", family=family,
                           sides=sides, over=max(by for _, by in past), box=box))
    return out


def element_parts(parts: Sequence[emit.Part], element_ids: Sequence[str]) -> list[tuple[JsonObject, list[JsonObject], str]]:
    """(element, its requests, its object id) of a slide's parts: the first part is the slide's own
    (deletions), the element parts follow in element order."""
    out: list[tuple[JsonObject, list[JsonObject], str]] = []
    for (el, reqs), oid in zip(parts[1:], element_ids):
        assert el is not None, f"{oid}: an element's part names no element"
        out.append((el, [slides_json(r) for r in reqs], oid))
    return out


# ---------------------------------------------------------------- invariants over every deck

def test_object_ids_exist_are_unique_and_well_formed(decks: tuple[Emitted, ...]) -> None:
    assert not (found := problems(decks, "ids")), report(found)


def test_text_ranges_lie_within_the_text(decks: tuple[Emitted, ...]) -> None:
    assert not (found := problems(decks, "ranges")), report(found)


def test_sizes_and_colours_are_valid(decks: tuple[Emitted, ...]) -> None:
    assert not (found := problems(decks, "values")), report(found)


def test_groups_hold_existing_objects_once(decks: tuple[Emitted, ...]) -> None:
    assert not (found := problems(decks, "groups")), report(found)


def test_every_element_is_emitted_once(decks: tuple[Emitted, ...]) -> None:
    found: list[str] = []
    for d in decks:
        for slide_id, page, parts, element_ids in d.result["slides"]:
            elements = jobjs(d.slides[page], "elements")
            ids = [el["id"] for el, _ in parts if el]
            if ids != [e["id"] for e in elements] or len(set(element_ids)) != len(element_ids):
                found.append(f"{d.name} {slide_id}: elements {ids} emitted as {element_ids}")
            spans: dict[str, Json] = {}
            for e in elements:
                if e["kind"] in ("text", "table", "diagram") or e.get("overlay"):
                    for s in jstrs(e, "spans") if "spans" in e else []:
                        if s in spans:
                            found.append(f"{d.name} {slide_id}: span {s} in both {spans[s]} and {e['id']}")
                        spans[s] = e["id"]
    assert not found, report(found)


def test_every_text_reaches_its_text_box(decks: tuple[Emitted, ...]) -> None:
    found: list[str] = []
    for d in decks:
        for slide_id, page, parts, element_ids in d.result["slides"]:
            for el, oid in zip(jobjs(d.slides[page], "elements"), element_ids):
                if el["kind"] == "text":
                    want = "\n".join("".join(NBSP if r.get("hole") else jstr(r, "text") for r in jobjs(p, "runs"))
                                     for p in jobjs(el, "paragraphs"))
                    got = d.texts.get(oid)
                    if got is None or collapse(got) != collapse(want):
                        found.append(f"{d.name} {slide_id} {el['id']}: {got!r} != {want!r}")
                elif el["kind"] == "table":
                    cells = {(jint(r, "insertText", "cellLocation", "rowIndex"),
                              jint(r, "insertText", "cellLocation", "columnIndex")): jstr(r, "insertText", "text")
                             for r in d.part_requests(slide_id, jstr(el, "id")) if "insertText" in r}
                    for i, row in enumerate(jarr(el, "cells")):
                        for j, runs in enumerate(jarr(row)):
                            words = run_text(jobjs(runs))
                            if words.strip() and cells.get((i, j), "").strip() != words.strip():
                                found.append(f"{d.name} {slide_id} {el['id']} cell {i},{j}: {words!r}")
                elif el["kind"] == "diagram":
                    labels = "\n".join(jstr(r, "insertText", "text")
                                       for r in d.part_requests(slide_id, jstr(el, "id")) if "insertText" in r)
                    for node in jobjs(el, "nodes"):
                        for runs in jarr(node, "paragraphs"):
                            if run_text(jobjs(runs)).strip() not in labels:
                                found.append(f"{d.name} {slide_id} {el['id']}: label {run_text(jobjs(runs))!r}")
    assert not found, report(found)


def test_anchored_pictures_share_a_group_with_their_text(decks: tuple[Emitted, ...]) -> None:
    found: list[str] = []
    for d in decks:
        for slide_id, page, parts, element_ids in d.result["slides"]:
            slide = d.slides[page]
            elements = jobjs(slide, "elements")
            oids = {jstr(e, "id"): oid for e, oid in zip(elements, element_ids)}
            for e, oid in zip(elements, element_ids):
                if not e.get("anchor"):
                    continue
                anchor = jstr(e, "anchor")
                if anchor not in oids:
                    found.append(f"{d.name} {slide_id} {e['id']}: anchor {anchor} is not on the slide")
                    continue
                text = oids[anchor]
                if text in d.title_oids(slide):
                    continue  # (placeholders can't be grouped)
                for member in [oid] + ([f"{oid}n"] if e.get("number") else []):
                    if d.top(member) is None or d.top(member) != d.top(text):
                        found.append(f"{d.name} {slide_id} {e['id']}: {member} in group {d.top(member)}, "
                                     f"its text {text} in {d.top(text)}")
    assert not found, report(found)


def test_numbers_are_centred_on_their_ball(decks: tuple[Emitted, ...]) -> None:
    found: list[str] = []
    for d in decks:
        for slide_id, page, parts, element_ids in d.result["slides"]:
            pictures = {jstr(e, "id"): box for e, box in d.result["pictures"][page]}
            for e, oid in zip(jobjs(d.slides[page], "elements"), element_ids):
                if not e.get("number"):
                    continue
                eid = jstr(e, "id")
                reqs = [r for r in d.part_requests(slide_id, eid) if jobj(next(iter(r.values()))).get("objectId") == f"{oid}n"]
                x0, y0, x1, y1 = d.boxes[f"{oid}n"]
                bx0, by0, bx1, by1 = pictures[eid]
                if abs((x0 + x1 - bx0 - bx1) / 2) > 0.1 or abs((y0 + y1 - by0 - by1) / 2) > 0.1:
                    found.append(f"{d.name} {slide_id} {eid}: number box centre "
                                 f"({(x0 + x1) / 2:.2f}, {(y0 + y1) / 2:.2f}) != ball's ({(bx0 + bx1) / 2:.2f}, {(by0 + by1) / 2:.2f})")
                shape = [jobj(r, "updateShapeProperties", "shapeProperties") for r in reqs if "updateShapeProperties" in r]
                para = [jobj(r, "updateParagraphStyle", "style") for r in reqs if "updateParagraphStyle" in r]
                if [s.get("contentAlignment") for s in shape] != ["MIDDLE"] or [s.get("alignment") for s in para] != ["CENTER"]:
                    found.append(f"{d.name} {slide_id} {eid}: number not centred ({shape}, {para})")
                number = jstr(e, "number", "text")
                if d.texts.get(f"{oid}n") != number:
                    found.append(f"{d.name} {slide_id} {eid}: number text {d.texts.get(f'{oid}n')!r} != {number!r}")
    assert not found, report(found)


def test_holes_are_no_break_spaces_as_wide_as_the_hole(decks: tuple[Emitted, ...]) -> None:
    found: list[str] = []
    fonts = FontMapper()
    for d in decks:
        scale = d.plan.scale
        for slide_id, page, parts, element_ids in d.result["slides"]:
            slide = d.slides[page]
            pictures = {id(run): pic for _, _, run, pic in slide_holes(slide)}
            for el, reqs, oid in element_parts(parts, element_ids):
                if el["kind"] != "text":
                    continue
                holes = [r for p in jobjs(el, "paragraphs") for r in jobjs(p, "runs") if r.get("hole")]
                text = d.texts[oid]
                styled = [jobj(r, "updateTextStyle") for r in reqs if "updateTextStyle" in r
                          and jobj(r, "updateTextStyle", "style").get("fontFamily") == HOLE_FONT
                          and jat(r, "updateTextStyle", "textRange", "type") == "FIXED_RANGE"]
                gaps = [(jint(s, "textRange", "endIndex") - jint(s, "textRange", "startIndex"),
                         jnum(s, "style", "fontSize", "magnitude"))
                        for s in styled
                        if set(text[jint(s, "textRange", "startIndex"):jint(s, "textRange", "endIndex")]) == {NBSP}]
                if len(gaps) != len(holes):
                    found.append(f"{d.name} {slide_id} {el['id']}: {len(holes)} holes, {len(gaps)} runs of no-break spaces")
                    continue
                for run, (n, size) in zip(holes, gaps):
                    hole = jnum(run, "hole")
                    width, z = hole * scale, fonts(run, scale)[1]
                    if abs(n * HOLE_SPACE_EM * size - width) > 0.005 * n + 0.01 or size > z + 0.01:
                        found.append(f"{d.name} {slide_id} {el['id']}: {n} spaces of {size} pt for a hole of "
                                     f"{width:.2f} pt at {z} pt")
                    pic = pictures.get(id(run))
                    if pic and hole < jnum(pic, "bbox", 2) - jnum(pic, "bbox", 0) - 0.01:
                        found.append(f"{d.name} {slide_id} {pic['id']}: hole {hole:.2f} narrower than its picture "
                                     f"{jnum(pic, 'bbox', 2) - jnum(pic, 'bbox', 0):.2f}")
    assert not found, report(found)


def test_predicted_pictures_stay_near_their_pdf_place(decks: tuple[Emitted, ...]) -> None:
    found: list[str] = []
    for d in decks:
        scale = d.plan.scale
        for page, slide in d.slides.items():
            for pid, dx in d.plan.shifts[page].items():
                if abs(dx * scale) >= MAX_SHIFT:
                    found.append(f"{d.name} page {page + 1} {pid}: formula shift {dx * scale:.1f} pt")
            boxes = {jstr(e, "id"): jnums(e, "bbox") for e in jobjs(slide, "elements")}
            for pid, (x0, x1) in d.plan.overlays[page].items():
                bx0, _, bx1, _ = boxes[pid]
                if abs((x0 - bx0) * scale) >= MAX_SHIFT or abs((x1 - x0) / (bx1 - bx0) - 1) > emit.OVERLAY_STRETCH + 1e-6:
                    found.append(f"{d.name} page {page + 1} {pid}: overlay {bx0:.1f}..{bx1:.1f} -> {x0:.1f}..{x1:.1f}")
    assert not found, report(found)


def test_overlay_marks_highlight_their_words_on_the_scratch_slide(decks: tuple[Emitted, ...]) -> None:
    """Every mark of an overlay lies on a word found in its anchor's text box (measure_places
    highlights it): a right edge's word is the last word before it."""
    found: list[str] = []
    for d in decks:
        plan = d.plan
        _, jobs = measure_jobs(plan.deck, plan.scale, plan.fonts, plan.placed, plan.page_slide)
        for job in jobs:
            page = job.page
            texts = {jstr(e, "id"): e for e in jobjs(d.slides[page], "elements") if e["kind"] == "text"}
            for o in job.overlays:
                pic = o.picture
                assert pic.anchor is not None, f"{pic.id}: an overlay with no anchor"
                text = "\n".join(emit.slides_texts(texts[pic.anchor], plan.scale, plan.fonts))
                where = f"{d.name} page {page + 1} {pic.id}"
                marks = [i for w in o.words for i, _ in w.marks]
                if sorted(marks) != list(range(len(pic.marks))):
                    found.append(f"{where}: marks {sorted(marks)} of {len(pic.marks)} on words")
                for w in o.words:
                    for i, side in w.marks:
                        if not side:
                            continue
                        word = pic.marks[i].before[-1][5]
                        got = text[w.range[0]:w.range[1]]
                        if " ".join(word.split()) != " ".join(got.split()):
                            found.append(f"{where}: mark {i} ends {word!r}, highlighted {got!r}")
    assert not found, report(found)


def test_bullets_are_styled_before_they_are_created(decks: tuple[Emitted, ...]) -> None:
    """A bullet keeps the style its paragraph had when it was created (CLAUDE.md pitfall): family,
    size and colour first, bullets next, then runs in parts that never cover a whole item."""
    found: list[str] = []
    for d in decks:
        for slide_id, page, parts, element_ids in d.result["slides"]:
            for el, reqs, oid in element_parts(parts, element_ids):
                if el["kind"] != "text":
                    continue
                paragraphs = jobjs(el, "paragraphs")
                if not any(p["bullet"] for p in paragraphs) or oid in d.shells:  # (shells: the next test)
                    continue
                where = f"{d.name} {slide_id} {el['id']}"
                inserted = next(jstr(r, "insertText", "text") for r in reqs if "insertText" in r)
                dummies = {jint(r, "deleteText", "textRange", "startIndex") for r in reqs if "deleteText" in r}
                spans: list[tuple[int, int, str]] = []  # (start, end) of each paragraph in the inserted text, dummies left out
                pos = 0
                for para in inserted.split("\n"):
                    if not (pos in dummies and para == "-"):
                        spans.append((pos, pos + len(para), para))
                    pos += len(para) + 1
                if len(spans) != len(paragraphs):
                    found.append(f"{where}: {len(spans)} paragraphs inserted for {len(paragraphs)}")
                    continue
                kinds = [next(iter(r)) for r in reqs]
                covered: set[int] = set()
                for k, r in enumerate(reqs):
                    if "createParagraphBullets" not in r:
                        continue
                    bullets = jobj(r, "createParagraphBullets")
                    for (start, end, para), p in zip(spans, paragraphs):
                        if not (start < jint(bullets, "textRange", "endIndex") and
                                end + 1 > jint(bullets, "textRange", "startIndex")):
                            continue
                        covered.add(start)
                        if not p["bullet"]:
                            found.append(f"{where}: bullets over the plain paragraph {para!r}")
                            continue
                        bullet = jobj(p, "bullet")
                        if bullets["bulletPreset"] != emit.bullet_preset(bullet) or \
                                len(para) - len(para.lstrip("\t")) != emit.bullet_level(bullet, jint(p, "level")):
                            found.append(f"{where}: {para!r} got {bullets['bulletPreset']} at "
                                         f"{len(para) - len(para.lstrip(chr(9)))} tabs")
                        before = [jobj(q, "updateTextStyle") for q in reqs[:k] if "updateTextStyle" in q
                                  and jint(q, "updateTextStyle", "textRange", "startIndex") <= start
                                  and jint(q, "updateTextStyle", "textRange", "endIndex") >= end]
                        need = {"fontFamily", "fontSize"}
                        if bullet.get("color"):
                            need.add("foregroundColor")
                        if not any(need <= set(jstr(q, "fields").split(",")) for q in before):
                            found.append(f"{where}: {para!r} has no {sorted(need)} before its bullet")
                for (start, _, para), p in zip(spans, paragraphs):
                    if p["bullet"] and start not in covered:
                        found.append(f"{where}: {para!r} has no bullet")
                # After the bullets exist (final indices, no tabs): no style request covers a whole item.
                last = max(i for i, kind in enumerate(kinds) if kind in ("createParagraphBullets", "deleteText"))
                pos = 0
                items: list[tuple[int, int]] = []
                for p in paragraphs:
                    n = len(run_text(emit.hole_runs(jobjs(p, "runs"), d.plan.scale, d.plan.fonts)))
                    if p["bullet"] and n > 1:
                        items.append((pos, pos + n))
                    pos += n + 1
                for q in reqs[last + 1:]:
                    if "updateTextStyle" in q:
                        t = jobj(q, "updateTextStyle", "textRange")
                        if (t["startIndex"], t["endIndex"]) in items:
                            found.append(f"{where}: one style request over the whole item {t}")
    assert not found, report(found)


def test_triangle_bullets_come_with_the_pptx(decks: tuple[Emitted, ...]) -> None:
    """A box whose bullets are all beamer's ▶ (no preset draws it: createParagraphBullets wrote ➢)
    comes in the .pptx as a text shell whose bullets are `a:buChar` ► (▶'s shape, larger in Slides; emit_text.text_shell_of): it
    is never created here nor given preset bullets; each paragraph's words go in front of its
    placeholder character, deleted right after, so no paragraph is ever empty; and no style request
    covers a whole item (it would restyle the item's bullet)."""
    found: list[str] = []
    shells = 0
    for d in decks:
        for slide_id, page, parts, element_ids in d.result["slides"]:
            synced: list[tuple[JsonObject, list[JsonObject], str]] | None = None
            for el, reqs, oid in element_parts(parts, element_ids):
                shell = d.shells.get(oid)
                if shell is None:
                    continue
                shells += 1
                if synced is None:  # what sync writes of the slide (slide_emission): every box created
                    emission = emit.slide_emission(d.slides[page], d.plan.scale, d.plan.fonts)
                    synced = element_parts(emission["parts"], emission["element_ids"])
                sync_kinds = [next(iter(r)) for e, rs, o in synced if o == oid for r in rs]
                if "createShape" not in sync_kinds or "createParagraphBullets" not in sync_kinds:
                    found.append(f"{d.name} {slide_id} {el['id']}: sync's emission of a shell box is {sync_kinds}")
                where = f"{d.name} {slide_id} {el['id']}"
                paragraphs = jobjs(el, "paragraphs")
                kinds = [next(iter(r)) for r in reqs]
                if "createShape" in kinds or "createParagraphBullets" in kinds:
                    found.append(f"{where}: a shell created or given preset bullets ({kinds})")
                if not kinds or kinds[0] != "updatePageElementTransform" or \
                        jstr(reqs[0], "updatePageElementTransform", "applyMode") != "ABSOLUTE":
                    found.append(f"{where}: a shell not placed first ({kinds[:2]})")
                if len(shell.paragraphs) != len(paragraphs):
                    found.append(f"{where}: {len(shell.paragraphs)} shell paragraphs for {len(paragraphs)}")
                    continue
                for p, sp in zip(paragraphs, shell.paragraphs):
                    if (sp.char is None) != (not p["bullet"]) or (sp.char is not None and sp.char != "►"):
                        found.append(f"{where}: a paragraph's bullet {p['bullet']} carried as {sp.char!r}")
                # the fill: last paragraph first, words in front of the character, then the character out
                fills = [(jint(r, "insertText", "insertionIndex"), jstr(r, "insertText", "text"),
                          jint(reqs[k + 1], "deleteText", "textRange", "startIndex") if k + 1 < len(reqs)
                          and "deleteText" in reqs[k + 1] else -1)
                         for k, r in enumerate(reqs) if "insertText" in r]
                want = [(2 * i, t, 2 * i + len(t.encode("utf-16-le", "surrogatepass")) // 2)
                        for i, t in reversed(list(enumerate(d.texts[oid].split("\n"))))]
                if [(a, c) for a, _, c in fills] != [(a, c) for a, _, c in want] or not all(t for _, t, _ in fills):
                    found.append(f"{where}: filled as {fills}, expected {want}")
                last = max(i for i, kind in enumerate(kinds) if kind == "deleteText")
                pos = 0
                items: list[tuple[int, int]] = []
                for p in paragraphs:
                    n = len(run_text(emit.hole_runs(jobjs(p, "runs"), d.plan.scale, d.plan.fonts)))
                    if p["bullet"] and n > 1:
                        items.append((pos, pos + n))
                    pos += n + 1
                for q in reqs[last + 1:]:
                    if "updateTextStyle" in q:
                        t = jobj(q, "updateTextStyle", "textRange")
                        if (t["startIndex"], t["endIndex"]) in items:
                            found.append(f"{where}: one style request over the whole item {t}")
    assert shells, "no deck has a box of ▶ bullets: the text shells went untested"
    assert not found, report(found)


OFF_PAGE_TOLERANCE = 1.0  # pt an object's frame may reach past the page (rounding, a hairline's half)
# Objects allowed past the page: "deck slide object-id". Empty, and it stays so: an object the
# converter cannot bring in is explained by `words_at_the_edge`, or it is a defect.
OFF_PAGE_ALLOWED: frozenset[str] = frozenset()


def words_at_the_edge(d: Emitted, f: OffPage) -> bool:
    """Whether an object reaches past the right edge only as far as a text box must for its words,
    as Slides sets them, not to wrap (`emit_text.words_right`: the widest line, LINE_MARGIN and the
    inset), when they come that close to the edge - a group by such boxes alone. What the box
    shows is its words; `on_page` brought in every point of room past them."""
    if f.sides != "R":
        return False
    if f.oid in d.groups:
        for c in d.groups[f.oid]:
            b = d.frame(c)
            if b is not None and b[2] > SLIDE_W + OFF_PAGE_TOLERANCE and not words_at_the_edge(d, OffPage(
                    slide=f.slide, oid=c, element="", family="", sides="R", over=b[2] - SLIDE_W, box=b)):
                return False
        return True
    el = d.owner.get(f.oid)
    if el is None or el["kind"] != "text" or not re.search(r"_t\d+$", f.oid):
        return False
    slide = d.slides[int(f.slide[5:])]
    scale = SLIDE_W / jnums(slide, "size")[0]
    typed = emit.parse_slide_element(el, "background" in slide, f.slide)
    if not isinstance(typed, TextElement):
        return False
    need = words_right(set_text(typed), scale, FONTS)
    return need is not None and f.box[2] <= max(SLIDE_W, need) + OFF_PAGE_TOLERANCE


def test_elements_lie_within_the_page(decks: tuple[Emitted, ...]) -> None:
    """Every object emit writes - pictures, shapes, tables, lines, text boxes, groups by their
    children - lies within its page to `OFF_PAGE_TOLERANCE`: Slides frames it there in the editor,
    past the slide's edge, wherever its ink is (live: 15,000 such frames over 1,169 bases, footers'
    room past their words, single lines' slack, number boxes, an arc's whole circle). The one
    exception is a text box whose own words come within its inset and LINE_MARGIN of the edge
    (`words_at_the_edge`)."""
    found = [f"{d.name} {f.slide} {f.oid} ({f.family}, {f.element}): {f.sides} +{f.over:.1f} pt "
             f"{tuple(round(v, 1) for v in f.box)}"
             for d in decks for f in off_page(d, OFF_PAGE_TOLERANCE)
             if f"{d.name} {f.slide} {f.oid}" not in OFF_PAGE_ALLOWED and not words_at_the_edge(d, f)]
    assert not found, report(found)


def test_the_off_page_allowlist_stays_empty() -> None:
    assert not OFF_PAGE_ALLOWED, "bring the object in, or explain it as words_at_the_edge does"


def stacking(page_elements: Sequence[JsonObject], parts: Sequence[emit.Part]) -> list[str]:
    """A slide's objects bottom to top once emit's `parts` have run, groups opened (a group's
    children keep their order among themselves), as Slides stacks them: the imported objects in
    their order, a created or duplicated object on top, a group where its topmost child was, and
    z-order requests moving top-level objects only (several keep their order)."""
    top: list[str] = [jstr(e, "objectId") for e in page_elements]
    kids: dict[str, list[str]] = {}
    for _, reqs in parts:
        for r in reqs:
            kind = slides_request_kind(r)
            body = jobj(slides_json(r), kind)
            if kind in ("createShape", "createLine", "createTable", "createImage"):
                top.append(jstr(body, "objectId"))
            elif kind == "duplicateObject":
                top += [as_str(v, "objectIds") for v in jobj(body, "objectIds").values()]
            elif kind == "deleteObject":
                oid = jstr(body, "objectId")
                top = [o for o in top if o != oid]
                kids = {g: [o for o in k if o != oid] for g, k in kids.items()}
            elif kind == "groupObjects":
                group = jstr(body, "groupObjectId")
                children = sorted(jstrs(body, "childrenObjectIds"), key=top.index)  # (a child not on top raises)
                top[top.index(children[-1])] = group
                top = [o for o in top if o not in children]
                kids[group] = children
            elif kind == "updatePageElementsZOrder":
                moved = sorted(jstrs(body, "pageElementObjectIds"), key=top.index)
                rest = [o for o in top if o not in moved]
                op = jstr(body, "operation")
                assert op in ("BRING_TO_FRONT", "SEND_TO_BACK"), f"no model of {op}"
                top = rest + moved if op == "BRING_TO_FRONT" else moved + rest

    def opened(oids: Sequence[str]) -> list[str]:
        return [x for o in oids for x in ([o] if o not in kids else [o, *opened(kids[o])])]
    return opened(top)


def stacking_problems(name: str, result: emit.OfflinePlan) -> list[str]:
    """Where emit stacks two overlapping elements of a slide otherwise than deck.json orders them
    (its element order is z-order), the lower one an opaque shape: a panel over words, a picture or
    another panel the PDF draws on it hides them in Slides."""
    found: list[str] = []
    slides = {jint(s, "page"): s for s in result["plan"].slides()}
    for slide_id, page, parts, element_ids in result["slides"]:
        flat = stacking(result["page_elements"][slide_id], parts)
        at = {o: k for k, o in enumerate(flat)}
        elements = jobjs(slides[page], "elements")
        for i, (low, low_id) in enumerate(zip(elements, element_ids)):
            if low["kind"] != "shape" or "opacity" in low or low.get("fill") is None or low_id not in at:
                continue
            lx0, ly0, lx1, ly1 = jnums(low, "bbox")
            for high, high_id in zip(elements[i + 1:], element_ids[i + 1:]):
                hx0, hy0, hx1, hy1 = jnums(high, "bbox")
                if high_id in at and at[high_id] < at[low_id] and \
                        min(lx1, hx1) - max(lx0, hx0) > 0.5 and min(ly1, hy1) - max(ly0, hy0) > 0.5:
                    found.append(f"{name} {slide_id}: {jstr(high, 'id')} ({jstr(high, 'kind')}) is under the "
                                 f"panel {jstr(low, 'id')} the PDF draws it on")
    return found


def test_nothing_lies_under_a_panel_the_pdf_draws_it_on(decks: tuple[Emitted, ...]) -> None:
    """Element order is z-order: the groups emit makes (a block's, a text's with its pictures) and
    its z-order requests keep it wherever a panel and what lies on it overlap (a listing's framed
    panel inside a block was left above the block's group with the code text in it)."""
    found = [p for d in decks for p in stacking_problems(d.name, d.result)]
    assert not found, report(found)


# ---------------------------------------------------------------- the imported .pptx

WHITE: dict[str, str] = {"color": "#ffffff"}


def emu(v: object) -> int:
    """A placeholder's position or size (EMU): every one here has them."""
    assert isinstance(v, int), v
    return v


def placeholders(prs: PptxPresentation) -> list[tuple[str, str, int, int, int, int]]:
    return [(page.name, s.name, emu(s.left), emu(s.top), emu(s.width), emu(s.height))
            for page in [prs.slide_master, *prs.slide_layouts] for s in page.placeholders]


def test_wide_pptx_rescales_every_placeholder_once() -> None:
    """16:9 decks: layout placeholders that inherit the master's position used to be scaled twice,
    with x and width written as 0."""
    from pptx import Presentation
    square = placeholders(Presentation(emit.build_pptx(720.0, 540.0, [], [], WHITE, None)))
    wide = placeholders(Presentation(emit.build_pptx(720.0, 405.0, [], [], WHITE, None)))
    for (layout, name, x, y, w, h), (_, _, wx, wy, ww, wh) in zip(square, wide):
        assert (wx, ww) == (x, w) and ww > 0, (layout, name)
        assert abs(wy - 0.75 * y) <= 1 and abs(wh - 0.75 * h) <= 1, (layout, name)


def test_pptx_pictures_sit_at_their_boxes(tmp_path: Path) -> None:
    from PIL import Image
    from pptx import Presentation
    Image.new("RGB", (4, 3), "red").save(tmp_path / "f.png")
    box = [101.37, 57.2, 180.05, 96.93]
    page: dict[str, object] = {"layout": "BLANK", "fill": None, "templates": False,
                               "pictures": [{"file": tmp_path / "f.png", "bbox": box, "alt": "x^2", "title": "Formula"}]}
    pic, = Presentation(emit.build_pptx(453.54, 255.12, [], [page], WHITE, None)).slides[0].shapes
    got = [pic.left, pic.top, pic.left + pic.width, pic.top + pic.height]
    assert all(abs(g - v * EMU_PER_PT) <= 1 for g, v in zip(got, box))
    assert pic._element._nvXxPr.cNvPr.get("descr") == "x^2"


def test_theme_decoration_keeps_what_backgrounds_share() -> None:
    """A header bar on every frame (one frame with a highlighted word in it, one without the bar):
    the decoration is the bar without the pixels where frames differ; the bare frame doesn't show it."""
    from beamer2slides.render import page_ground, theme_decoration
    white = np.array([255, 255, 255])
    frame = np.full((60, 80, 3), 255, np.uint8)
    frame[:10] = (51, 51, 179)
    highlight = frame.copy()
    highlight[4:6, 10:20] = (255, 200, 0)
    bare = np.full((60, 80, 3), 255, np.uint8)
    assert (page_ground(frame) == white).all()
    picture, inside, exact = theme_decoration([frame, highlight, bare], white)
    assert picture is not None
    assert inside == [True, True, False] and not exact
    assert (picture[:10, :, 3] == 255).sum() == 800 - 20 and (picture[10:, :, 3] == 0).all()
    assert (picture[4:6, 10:20, 3] == 0).all() and tuple(picture[0, 0]) == (51, 51, 179, 255)
    assert theme_decoration([frame, frame], white)[2], "a frame that is ground plus decoration can inherit"
    assert theme_decoration([bare, bare], white)[0] is None


def test_theme_decoration_is_not_every_slides_words_cut_out() -> None:
    """A decoration over most of the page (a shaded canvas) on backgrounds that each keep their own
    words on it (a page border read as a figure left them there): cut out of it, they showed
    etched into every slide made in Slides, the master's ground through each. What one background
    keeps on the ground (a formula), or a highlight small as a mini frame's, is cut out as before."""
    from beamer2slides.render import theme_decoration
    white = np.array([255, 255, 255])
    shaded = np.full((60, 80, 3), 255, np.uint8)
    shaded[:40] = (200, 180, 140)
    formula = shaded.copy()
    formula[45:50, 20:60] = (0, 0, 0)  # (on the ground: 4% of the page)

    def words(at: int, size: int) -> np.ndarray:
        img = shaded.copy()
        img[5:5 + size, at:at + 2 * size] = (40, 30, 20)
        return img
    picture, inside, _ = theme_decoration([formula, shaded, words(10, 1)], white)
    assert picture is not None and inside == [True, True, True]
    assert (picture[45:50, 20:60, 3] == 0).all() and (picture[:40, :, 3] == 255).sum() == 40 * 80 - 2
    etched = [words(at, 5) for at in (5, 30, 55)]  # (each 1% of the page, all on the decoration)
    picture, inside, exact = theme_decoration(etched, white)
    assert picture is None and inside == [False, False, False] and not exact


def test_pptx_layouts_carry_the_theme(tmp_path: Path) -> None:
    """Decorations at the bottom of the layouts; a _V1 page layout is a copy with its own decoration."""
    from PIL import Image
    from pptx import Presentation
    for name in ("main", "title", "variant"):
        Image.new("RGBA", (8, 6), (0, 0, 255, 255)).save(tmp_path / f"{name}.png")
    pages: list[dict[str, object]] = [{"layout": "TITLE_ONLY", "fill": None, "templates": False, "pictures": []},
                                      {"layout": "TITLE_ONLY_V1", "fill": None, "templates": False, "pictures": []},
                                      {"layout": "BLANK_V1", "fill": None, "templates": False, "pictures": []}]
    decorations = {"*": tmp_path / "main.png", "TITLE": tmp_path / "title.png", "*_V1": tmp_path / "variant.png"}
    prs = Presentation(emit.build_pptx(720.0, 405.0, [], pages, WHITE, decorations))
    pictures = {l.name: [s for s in l.shapes if s.shape_type == 13] for l in prs.slide_layouts}
    assert len(prs.slide_layouts) == 13 and all(len(p) == 1 for p in pictures.values())
    assert [s.slide_layout.name for s in prs.slides] == ["Title Only", "Title Only (theme 2)", "Blank (theme 2)"]
    layout = prs.slides[1].slide_layout
    assert layout.shapes[0].shape_type == 13 and any("Title" in p.name for p in layout.placeholders), "decoration first, title placeholder kept"
    assert (layout.shapes[0].width, layout.shapes[0].height) == (prs.slide_width, prs.slide_height)


# ---------------------------------------------------------------- numeric helpers

SCALE = SLIDE_W / 453.54
FONTS = FontMapper()
BODY = 10.91  # the size of a run of text in the test decks (CMSS10 at 11 pt)


def text_run(text: str, size: float, **extra: Json) -> JsonObject:
    """A run as classify writes it: CMSS10, black, plain, and `extra`'s fields."""
    run: JsonObject = {"text": text, "font": "CMSS10", "family": "sans", "size": size, "bold": False, "italic": False,
                       "smallcaps": False, "color": "#000000", "link": None}
    run.update(extra)
    return run


@pytest.mark.parametrize("text", ["7", "12", "123"])
def test_number_box_is_centred_on_the_ball(text: str) -> None:
    number: JsonObject = {**text_run(text, 8.0), "center": [40.3, 120.7], "height": 9.5, "baseline": 123.4, "x0": 37.0}
    reqs = [slides_json(r) for r in number_box_requests(number, "b2s_s001", "b2s_s001_f3n", SCALE, FONTS)]
    props = jobj(reqs[0], "createShape", "elementProperties")
    w, h = pt_of(jat(props, "size", "width")), pt_of(jat(props, "size", "height"))
    x0, y0, x1, y1 = box_of(jobj(props, "transform"), w, h)
    assert abs((x0 + x1) / 2 - 40.3 * SCALE) < 0.01 and abs((y0 + y1) / 2 - 120.7 * SCALE) < 0.01
    assert w - 2 * emit.PAD_X >= len(text) * FONTS(number, SCALE)[1], "room for every digit: no wrap"
    assert jat(reqs[1], "updateShapeProperties", "shapeProperties", "contentAlignment") == "MIDDLE"
    assert jat(reqs[2], "insertText", "text") == text and jat(reqs[4], "updateParagraphStyle", "style", "alignment") == "CENTER"


@pytest.mark.parametrize("hole", [0.4, 3.0, 17.67, 40.71, 120.0])
def test_hole_run_is_exactly_as_wide_as_the_hole(hole: float) -> None:
    run = text_run(NBSP, BODY, hole=hole)
    z = FONTS(run, SCALE)[1]
    out = hole_run(run, SCALE, FONTS)
    spaces, size = jstr(out, "text"), jnum(out, "hole_size")
    n, width = len(spaces), hole * SCALE
    assert set(spaces) == {NBSP} and out["hole"] == hole
    assert abs(n * HOLE_SPACE_EM * size - width) <= 0.005 * n
    assert size <= z + 0.005, "never taller than the line"
    assert n == 1 or (n - 1) * HOLE_SPACE_EM * z < width, "as few spaces as fit"


def three_holes_json() -> JsonObject:
    """A line 'A [f] and [g] or [h] end.' with TeX's word spaces (3.63 pt) around three formulas."""
    words = [("A", 10.91, 7.26), ("and", 40.43, 17.0), ("or", 93.69, 10.3), ("end.", 132.25, 20.0)]
    holes = [(21.8, 16.0), (61.06, 30.0), (107.62, 22.0)]  # (hole_x0, picture width)
    runs: list[JsonObject] = [text_run("A ", BODY)]
    elements: list[Json] = []
    for k, (x, w) in enumerate(holes):
        before: list[Json] = [[ww, "CMSS10", "sans", False, False, t, x0] for t, x0, ww in words[:k + 1]]
        runs += [text_run(NBSP, BODY, hole=w, hole_x0=x, before=before, next_x0=words[k + 1][1]),
                 text_run(words[k + 1][0] + " ", BODY)]
        elements.append({"id": f"p0h{k}", "kind": "image", "anchor": "p0t0", "bbox": [x - HOLE_PAD, 92.0, x - HOLE_PAD + w, 106.0]})
    runs[-1]["text"] = "end."
    para: JsonObject = {"align": "left", "runs": [r for r in runs], "lines": [{"baseline": 104.0, "x0": 10.91, "x1": 152.25}]}
    elements.append({"id": "p0t0", "kind": "text", "paragraphs": [para]})
    return {"page": 0, "size": [453.54, 255.12], "elements": elements}


def text_runs_of(slide: JsonObject) -> list[JsonObject]:
    """The runs of the one paragraph of the last element (the text) of a synthetic slide."""
    return jobjs(slide, "elements", -1, "paragraphs", 0, "runs")


def test_several_holes_on_a_line_keep_their_pictures_in_place() -> None:
    slide = fit_holes(three_holes_json(), SCALE, FONTS)
    shifts = formula_shifts(slide, SCALE, FONTS)
    assert all(abs(shifts.get(f"p0h{k}", 0.0)) < 3 for k in range(3)), shifts
    run = text_runs_of(slide)[5]
    em = FONTS(run, SCALE)[1] / SCALE
    assert space_shift(run, em, ()) < -30, "without the earlier holes both gaps look like stretched spaces"


def test_fit_holes_reaches_the_next_word_but_never_below_the_picture() -> None:
    slide = three_holes_json()
    tight = text_runs_of(slide)[3]
    tight["next_x0"] = jnum(tight, "hole_x0") + 10  # the next word starts inside the picture
    fitted = fit_holes(slide, SCALE, FONTS)
    holes = [jnum(r, "hole") for r in text_runs_of(fitted) if r.get("hole")]
    space = emit.SYMBOL_ADVANCE_EM[" "] * FONTS(tight, SCALE)[1] / SCALE
    assert holes[0] == pytest.approx(40.43 - (10.91 + 7.26 + space), abs=0.01)
    assert holes[1] == 30.0, "as wide as the picture"
    followed = three_holes_json()
    text_runs_of(followed)[2]["text"] = " and "  # a space after the hole: no reach
    assert [jnum(r, "hole") for r in text_runs_of(fit_holes(followed, SCALE, FONTS)) if r.get("hole")][0] == 16.0


def test_hole_offset_shares_the_spaces_like_the_pdf() -> None:
    slide = fit_holes(three_holes_json(), SCALE, FONTS)
    _, p, run, pic = slide_holes(slide)[1]
    assert pic is not None, "the hole's picture is on the slide"
    z = FONTS(run, SCALE)[1]
    o = hole_offset(p, run, pic, SCALE, z)
    space = emit.SYMBOL_ADVANCE_EM[" "] * z
    px0, px1 = jnum(pic, "bbox", 0), jnum(pic, "bbox", 2)
    left, right = space + o, (jnum(run, "hole") - (px1 - px0)) * SCALE - o
    pdf_left, pdf_right = px0 - 57.43, 93.69 - px1
    assert left / right == pytest.approx(pdf_left / pdf_right, rel=0.01)
    glued = three_holes_json()
    text_runs_of(glued)[0]["text"] = "A"  # no word space before the formula
    _, p, run, pic = slide_holes(glued)[0]
    assert pic is not None, "the hole's picture is on the slide"
    assert hole_offset(p, run, pic, SCALE, z) == 0.0


def overlay(marks_x: list[float], bbox: Box4) -> JsonObject:
    marks: list[Json] = [{"x": x, "hole_x0": x, "font": "CMSS10", "family": "sans", "size": 10.91, "bold": False,
                          "italic": False, "before": []} for x in marks_x]
    return {"elements": [{"id": "p0o0", "kind": "image", "overlay": True, "anchor": "p0t0", "bbox": [v for v in bbox],
                          "marks": marks}]}


@pytest.mark.parametrize("drifts, expected", [
    ({100.0: 5.0, 200.0: 5.0}, (105.0, 205.0)),     # both ends drift alike: moved, not stretched
    ({100.0: 0.0, 200.0: 30.0}, (110.0, 220.0)),    # slope 0.3 clamped to OVERLAY_STRETCH
    ({100.0: 0.0, 200.0: -4.0}, (100.0, 196.0)),    # narrower by the slope (-0.04)
    ({150.0: 2.0, 155.0: 12.0}, (107.0, 207.0)),    # marks closer than 10 pt: no stretch
])
def test_overlay_boxes_drift_and_clamped_stretch(monkeypatch: pytest.MonkeyPatch, drifts: dict[float, float],
                                                 expected: tuple[float, float]) -> None:
    def fake(m: Mark, scale: float, fonts: FontMapper) -> float:  # the drift of the mark at its x
        return drifts[m.hole_x0]
    monkeypatch.setattr(emit_holes, "mark_drift", fake)
    x0, x1 = overlay_boxes(overlay(list(drifts), (100.0, 50.0, 200.0, 60.0)), SCALE, FONTS)["p0o0"]
    assert (x0, x1) == pytest.approx(expected, abs=1e-6)


def test_coloured_text_is_no_mark() -> None:
    img = np.full((900, 1600, 3), 255, dtype=np.uint8)
    img[100:140, 200:300] = (255, 0, 0)  # a red link
    img[100:140, 400:500] = (0, 0, 255)  # a blue one
    img[200:240, 200:300] = (255, 0, 255)  # the mark
    marks = find_marks(mark_alpha(img, "#ff00ff"), 1600 / SLIDE_W)
    assert len(marks) == 1 and marks[0][1] == pytest.approx(200 * SLIDE_W / 1600, abs=0.5)


def unplaced(el: JsonObject, n: int) -> JsonObject:
    """DeckPlan.placed for a slide whose pictures stay where the PDF has them."""
    return el


@pytest.mark.parametrize("label", ["below", "above"])
def test_a_braced_formula_is_measured_on_its_text_line(label: str) -> None:
    """An \\underbrace label below (or \\overbrace above) makes the picture taller than its line: the
    gap is looked for on the text line, not at the picture's middle, which lies a line away."""
    size = 10.91
    pic_box = [60.0, 88.0, 100.0, 124.0] if label == "below" else [60.0, 70.0, 100.0, 104.0]
    baselines = [100.0, 128.0, 141.0, 154.0] if label == "below" else [72.0, 100.0, 113.0, 126.0]
    line_at = 100.0
    hole = text_run(NBSP, size, hole=40.0, hole_x0=61.0, before=[[7.26, "CMSS10", "sans", False, False, "A", 10.91]],
                    next_x0=104.0)
    runs: list[Json] = [text_run("some text " * 3, BODY), text_run("A ", BODY), hole, text_run("words", BODY)]
    para: JsonObject = {"align": "left", "bullet": None, "level": 0, "size": size, "text_x0": 10.91, "tab_x0": None,
                        "wrap_limit": 300.0, "runs": runs,
                        "lines": [{"baseline": b, "x0": 10.91, "x1": 200.0} for b in baselines]}
    slide: JsonObject = {"page": 0, "size": [453.54, 255.12], "elements": [
        {"id": "p0h0", "kind": "image", "anchor": "p0t0", "bbox": [v for v in pic_box]},
        {"id": "p0t0", "kind": "text", "role": "body", "bbox": [10.91, 60.0, 200.0, 160.0], "paragraphs": [para]}]}
    _, jobs = measure_jobs({"slides": [slide]}, SCALE, FONTS, unplaced, {0: "b2s_s000"})
    (found,), = [job.gaps for job in jobs]
    assert found.cy == pytest.approx((line_at - 0.35 * size) * SCALE)
    middle = (pic_box[1] + pic_box[3]) / 2 * SCALE
    assert abs(round((middle - found.cy) / found.pitch)) == 1, "the picture's middle is a line away"
    mark = (found.x0 + 2.0, found.cy - 6.0, found.x0 + 2.0 + found.width, found.cy + 6.0)
    assert pick_gap([mark], found.x0, found.cy, found.width, found.pitch) == pytest.approx((2.0, 0.0))


def braced_phrase() -> JsonObject:
    """'Gradient descent updates every parameter after each batch.' with a brace under
    'updates every parameter' (marks at both edges of 'updates' and 'parameter', as classify.mark writes them)."""
    words = [("Gradient", 10.91, 38.9), ("descent", 53.4, 33.91), ("updates", 91.03, 35.39), ("every", 130.05, 23.47),
             ("parameter", 157.16, 45.52), ("after", 206.3, 22.0)]
    marks: list[Json] = []
    for k in (2, 4):
        before: list[Json] = [[w, "CMSS10", "sans", False, False, t, x0] for t, x0, w in words[:k + 1]]
        for x, b in ((words[k][1], before[:-1]), (words[k][1] + words[k][2], before)):
            marks.append({"x": x, "hole_x0": x, "pads": 0.0, "font": "CMSS10", "family": "sans", "size": 10.91,
                          "bold": False, "italic": False, "before": b})
    para: JsonObject = {"align": "left", "bullet": None, "level": 0, "size": 10.91, "text_x0": 10.91, "tab_x0": None,
                        "wrap_limit": 300.0,
                        "runs": [text_run(" ".join(t for t, _, _ in words) + " each batch.", BODY)],
                        "lines": [{"baseline": 106.13, "x0": 10.91, "x1": 283.62}]}
    return {"page": 0, "size": [453.54, 255.12], "elements": [
        {"id": "p0t0", "kind": "text", "role": "body", "bbox": [10.91, 98.0, 283.62, 109.0], "paragraphs": [para]},
        {"id": "p0f0", "kind": "image", "overlay": True, "anchor": "p0t0", "bbox": [89.83, 110.44, 203.99, 129.32], "marks": marks}]}


def test_overlay_words_are_highlighted_and_the_brace_fits_them() -> None:
    slide = braced_phrase()
    planned, jobs = measure_jobs({"slides": [slide]}, SCALE, FONTS, unplaced, {0: "b2s_s000"})
    reqs = [slides_json(r) for r in planned]
    (job,) = jobs
    (o,) = job.overlays
    text = emit.slides_texts(jobj(slide, "elements", 0), SCALE, FONTS)[0]
    assert [text[slice(*w.range)] for w in o.words] == ["updates", "parameter"]
    assert len({w.colour for w in o.words}) == 2, "words on one line get different colours"
    highlights = [jobj(r, "updateTextStyle") for r in reqs
                  if "updateTextStyle" in r and "backgroundColor" in jobj(r, "updateTextStyle", "style")]
    assert [(jint(h, "textRange", "startIndex"), jint(h, "textRange", "endIndex")) for h in highlights] == \
        [w.range for w in o.words]
    # Slides sets 'updates' 3 pt (PDF) further right and 3 pt wider, 'parameter' 16 pt further: stretched 12%.
    drift = {91.03: 3.0, 126.42: 6.0, 157.16: 12.0, 202.68: 16.0}

    def x(v: float) -> float:
        return (v + drift[v]) * SCALE

    marks = {w.colour: [(x(a), 160.0, x(b), 172.0), (x(a) + 200, 160.0, x(b) + 200, 172.0)]
             for w, (a, b) in zip(o.words, [(91.03, 126.42), (157.16, 202.68)])}
    move, table = emit.overlay_move(o, marks, SCALE)
    bbox = jnums(slide, "elements", 1, "bbox")
    b0, b1 = emit.fit_overlay(bbox, list(drift.items()), emit.OVERLAY_STRETCH_MEASURED)
    x0, _, x1, _ = bbox
    assert move is not None and move.sx is not None, "measured words move the brace"
    assert move.dx == pytest.approx((b0 - x0) * SCALE) and move.dy == 0.0
    assert move.sx == pytest.approx((b1 - b0) / (x1 - x0))
    assert move.sx > 1 + emit.OVERLAY_STRETCH, "measured words stretch a brace further than predicted ones may"
    assert [m[2] for m in table] == pytest.approx(list(drift.values()), abs=0.01)
    unmeasured: dict[str, list[Box4]] = {c: [] for c in marks}
    assert emit.overlay_move(o, unmeasured, SCALE)[0] is None, "no word found: the prediction stays"


def test_measured_overlay_move_keeps_the_left_edge_under_a_relative_scale(decks: tuple[Emitted, ...]) -> None:
    """measure_places' (dx, dy, scaleX): the RELATIVE transform scales about the page origin, so
    its translation makes the picture's left edge move by dx and its width scale by scaleX."""
    d = next((d for d in decks if any(e.get("marks") for s in d.slides.values() for e in jobjs(s, "elements"))), None)
    if d is None:
        pytest.skip("no deck with overlays built")
    slide = next(s for s in d.plan.slides() if any(e.get("marks") for e in jobjs(s, "elements")))
    i, pic = next((i, e) for i, e in enumerate(jobjs(slide, "elements")) if e.get("marks"))
    x0, _, x1, _ = next(box for e, box in d.result["pictures"][jint(slide, "page")] if e["id"] == pic["id"])
    templates = [(100.0, 100.0)] * len(d.plan.keys)
    parts, _ = d.plan.slide_parts(slide, d.result["page_elements"], d.result["speaker_notes"],
                                  {jstr(pic, "id"): Place(dx=4.0, dy=0.0, sx=1.2)}, templates, None)
    oid = f"b2s_s{jint(slide, 'page'):03}_f{i}"
    t, = [part_json(move, "updatePageElementTransform") for _, reqs in parts for r in reqs
          if (move := r.get("updatePageElementTransform")) is not None and move["objectId"] == oid]
    assert t["applyMode"] == "RELATIVE"
    moved = [jnum(t, "transform", "scaleX") * v + jnum(t, "transform", "translateX") / EMU_PER_PT for v in (x0, x1)]
    assert moved == pytest.approx([x0 + 4.0, x0 + 4.0 + 1.2 * (x1 - x0)], abs=0.01)

# ---------------------------------------------------------------- right to left

# A Hebrew paragraph is a left-to-right paragraph to Slides unless it is told otherwise, and
# then its full stop lands at the wrong end and the cursor walks the wrong way. Synthetic, as
# `tests/test_bidi.py` is: the characters, not a font.
ALEF, BET, GIMEL = "א", "ב", "ג"
ONE_LINE = ((100.0, 200.0),)  # (x0, x1) of each line of a paragraph


def hebrew_element(align: str, lines: Sequence[tuple[float, float]], bullet: JsonObject | None,
                   direction: str | None, text: str | None) -> JsonObject:
    para: JsonObject = {"align": align, "level": 0, "bullet": bullet, "size": 10.91, "text_x0": lines[0][0],
                        "tab_x0": None, "wrap_limit": None,
                        "runs": [text_run(f"{ALEF}{BET} {GIMEL}" if text is None else text, BODY)],
                        "lines": [{"baseline": 60.0 + 12 * i, "x0": x0, "x1": x1} for i, (x0, x1) in enumerate(lines)]}
    if direction:
        para["direction"] = direction
    return {"id": "p0t0", "kind": "text", "role": "body", "code": False,
            "bbox": [min(l[0] for l in lines), 50.0, max(l[1] for l in lines), 50.0 + 12 * len(lines)],
            "paragraphs": [para]}


def paragraph_style(el: JsonObject) -> JsonObject:
    return next(jobj(r, "updateParagraphStyle") for r in text_requests(el, SCALE) if "updateParagraphStyle" in r)


def test_a_hebrew_paragraph_is_told_which_way_it_reads() -> None:
    style = paragraph_style(hebrew_element("left", ONE_LINE, None, "rtl", None))
    assert jat(style, "style", "direction") == "RIGHT_TO_LEFT" and "direction" in jstr(style, "fields").split(",")


def test_a_left_to_right_paragraph_is_told_nothing_it_was_not_told_before() -> None:
    style = paragraph_style(hebrew_element("left", ONE_LINE, None, None, "one two"))
    assert "direction" not in jobj(style, "style") and "direction" not in jstr(style, "fields")
    assert style["fields"] == "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine"


@pytest.mark.parametrize("align, lines, alignment", [
    ("right", ((120.0, 200.0), (100.0, 200.0)), "START"),   # flush right: where it starts
    ("left", ((100.0, 200.0),), "START"),                   # one line: nothing was measured
    ("left", ((100.0, 200.0), (100.0, 200.0)), "START"),    # justified: it starts at the right too
    ("left", ((100.0, 200.0), (100.0, 170.0)), "END"),      # really ragged right: it ends there
    ("center", ((110.0, 190.0), (100.0, 200.0)), "CENTER"),
])
def test_a_hebrew_paragraph_keeps_the_edge_it_is_drawn_against(align: str, lines: tuple[tuple[float, float], ...],
                                                               alignment: str) -> None:
    # START and END are the reading direction's own ends, so the page's left and right have to
    # be mirrored into them (emit.hugs), or every Hebrew paragraph moves across its box.
    assert jat(paragraph_style(hebrew_element(align, lines, None, "rtl", None)), "style", "alignment") == alignment


def test_a_hebrew_paragraph_is_indented_from_the_right() -> None:
    # The second paragraph starts 10 pt short of the right margin the first one reaches.
    el = hebrew_element("left", ONE_LINE, None, "rtl", None)
    short = jobj(hebrew_element("left", ((100.0, 190.0),), None, "rtl", None), "paragraphs", 0)
    jarr(el, "paragraphs").append(short)
    styles = [jobj(r, "updateParagraphStyle", "style") for r in text_requests(el, SCALE) if "updateParagraphStyle" in r]
    assert pt_of(styles[0]["indentStart"]) == 0
    assert pt_of(styles[1]["indentStart"]) == pytest.approx(10 * SCALE, abs=0.01)


def test_a_hebrew_bullet_hangs_on_the_right_of_its_item() -> None:
    bullet: JsonObject = {"kind": "glyph", "text": "●", "bbox": [204.0, 52.0, 210.0, 58.0], "color": "#000000"}
    style = jobj(paragraph_style(hebrew_element("left", ONE_LINE, bullet, "rtl", None)), "style")
    # indentFirstLine is measured from the paragraph's own edge, which is the bullet's right.
    assert pt_of(style["indentFirstLine"]) == pytest.approx((210.0 - 204.0) * SCALE + emit.BULLET_GAP, abs=0.01)
    assert style["direction"] == "RIGHT_TO_LEFT"


def hebrew_table() -> JsonObject:
    return {"id": "p0b0", "kind": "table", "frame": [100.0, 50.0, 200.0, 70.0], "size": 10.91,
            "columns": [{"x0": 100.0, "x1": 140.0, "align": "left"},
                        {"x0": 160.0, "x1": 200.0, "align": "left"}],
            "cells": [[[text_run(f"{ALEF}{BET}", BODY)], [text_run("one", BODY)]]],
            "row_baselines": [60.0], "row_heights": [20.0], "rules": []}


def column_widths(reqs: Sequence[JsonObject]) -> list[float]:
    return [jnum(r, "updateTableColumnProperties", "tableColumnProperties", "columnWidth", "magnitude") / EMU_PER_PT
            for r in reqs if "updateTableColumnProperties" in r]


def test_slides_width_reads_measured_advances() -> None:
    size = FONTS(text_run("12,000", BODY), SCALE)[1]  # a number's own size (test_a_number_is_set_at_its_pdf_width)
    lato = emit.ADVANCES["Lato"]["regular"]
    assert emit.slides_width([text_run("12,000", BODY)], SCALE, FONTS) == pytest.approx((5 * lato["0"] + lato[","]) * size)
    # Lato's digits are tabular and wider than Computer Modern's 0.5 em: a number is no sentence.
    assert lato["0"] > 0.55
    assert emit.slides_width([text_run("x", BODY, font="FiraSans-Regular")], SCALE, FONTS) is None  # not measured


# Advance widths (em) from CMCSC10.afm, and TeX's interword space in it: what the PDF measures.
CMCSC10 = {"S": 0.611, "m": 0.746, "a": 0.613, "l": 0.513, "c": 0.591, "p": 0.557, "s": 0.457, ":": 0.319,
           "M": 0.988, "o": 0.635, "n": 0.613, "i": 0.302, "t": 0.591, "r": 0.602, "W": 1.105, "k": 0.635,
           "f": 0.535, "e": 0.557, " ": 0.3333}


def test_small_caps_split_the_difference_between_width_and_height() -> None:
    # 27_text_fit, frame `faces`: Slides draws a small capital at 0.70 of its capital where CMCSC
    # draws it at 0.755, and CMCSC is an extended face - the line came out 0.777 of the PDF's width.
    # Full width (1.28x) made the capitals 30% too tall; the compromise leaves it ~11% narrow.
    text = "Small caps: Monitor Workstation Mainframe"
    run = text_run(text, BODY, font="CMCSC10", family="serif", smallcaps=True)
    pdf = sum(CMCSC10[c] for c in text) * 10.91 * SCALE
    assert emit.slides_width([run], SCALE, FONTS) == pytest.approx(0.89 * pdf, rel=0.03)
    plain = FONTS(text_run(text, BODY, font="CMR10", family="serif"), SCALE)[1]
    assert FONTS(run, SCALE)[1] == pytest.approx(plain * emit.SMALL_CAPS_WIDTH["serif"], abs=0.1)


def test_a_small_caps_run_lays_its_line_out_at_the_size_slides_does() -> None:
    # tools/probe_text_fit_fonts.py: lowercase letters only are drawn in the small font; a space,
    # a capital or a comma in the run and the line takes the run's full size.
    def sc(text: str) -> JsonObject:
        return text_run(text, BODY, font="CMCSC10", family="serif", smallcaps=True)

    assert emit.line_size(sc("caps"), 20.0) == pytest.approx(20.0 * emit.SMALL_CAPS_SIZE)
    assert emit.line_size(sc("small caps"), 20.0) == 20.0
    assert emit.line_size(sc("Caps"), 20.0) == 20.0
    assert emit.line_size(text_run("caps", BODY), 20.0) == 20.0


def test_a_number_is_set_at_its_pdf_width() -> None:
    # 27_text_fit, frame `table-tight`: Lato's tabular digits are 0.577 em where Computer Modern's
    # are 0.5, so a number column came out 8-14% wider than the PDF's (and its digits taller).
    cmss = {False: {**dict.fromkeys("0123456789", 0.5), ",": 0.277, ".": 0.277},  # CMSS10.afm, CMSSBX10.afm
            True: {**dict.fromkeys("0123456789", 0.55), ",": 0.305, ".": 0.305}}
    for text, font, size, bold in (("1,281,167", "CMSS9", 8.97, False), ("1000", "CMSS9", 8.97, False),
                                   ("0,000", "CMSS12", 11.96, False), ("27.3", "CMSS10", 10.91, False),
                                   ("2026", "CMSSBX10", 10.91, True)):
        design = int(re.sub(r"\D", "", font))
        pdf = sum(cmss[bold][c] for c in text) * emit.design_width(emit.DESIGN_WIDTH["sans"], design) * size * SCALE
        run = text_run(text, size, font=font, bold=bold)
        assert emit.slides_width([run], SCALE, FONTS) == pytest.approx(pdf, rel=0.012), text
    # A sentence keeps its size, numbers in it or not, and so do a unit, a script and a word.
    prose = FONTS(text_run("Monitor", BODY), SCALE)[1]
    for run in (text_run("Monitor 12,345.67", BODY), text_run("12h", BODY), text_run("x86", BODY),
                text_run("2", BODY, script="super"), text_run("Classes", BODY)):
        assert FONTS(run, SCALE)[1] == prose, run["text"]
    assert FONTS(text_run("1,281,167", BODY), SCALE)[1] < prose


def test_a_font_with_no_design_size_has_no_pdf_width() -> None:
    # emit_widths.pdf_width_of read `font_info(font).design_size` and handed it to design_width,
    # which needs a number; a font that is no TeX optical cut has None. cm_face_of refused those
    # first, so nothing failed, but the width read on that alone. Each now says None for itself.
    for font, family in (("Helvetica", "sans"), ("Palatino-Roman", "serif"), ("DejaVuSans", "sans")):
        run = text_run("Coral reefs", BODY, font=font, family=family)
        assert font_info(font).design_size is None, font
        assert emit.pdf_width([run]) is None, font
    assert emit.pdf_width([text_run("Coral reefs", BODY)]) is not None  # (CMSS10: a design size of 10)


def test_a_block_body_with_no_flip_merges_under_its_title_bar() -> None:
    # merge_blocks read a body's `flip` with `and`, so a null flip made the key (True, None) and
    # the shape table raised KeyError. A body with no flip is one whose bottom is not round.
    head: JsonObject = {"kind": "shape", "block": 3, "shape": "ROUND_RECTANGLE", "flip": False, "radius": 4.0,
                        "bbox": [10.0, 10.0, 200.0, 30.0]}
    body: JsonObject = {"kind": "shape", "block": 3, "shape": "ROUND_2_SAME_RECTANGLE", "flip": None, "radius": 2.0,
                        "bbox": [10.0, 30.0, 200.0, 90.0], "title_bar": [10.0, 10.0, 200.0, 30.0]}
    merged = emit.merge_blocks([head, body])
    assert merged[0]["shape"] == "ROUND_2_SAME_RECTANGLE" and merged[0]["flip"] is False
    assert merged[0]["bbox"] == [10.0, 10.0, 200.0, 90.0] and merged[0]["radius"] == 4.0
    assert merged[1] is head and body["flip"] is None  # the caller's dicts stay as they were


def test_deck_ir_reads_a_number_and_small_caps_back_at_their_pdf_size() -> None:
    from beamer2slides.deck_ir import pdf_size
    for run in (text_run("1,281,167", 8.97, font="CMSS9"),
                text_run("Small caps", BODY, font="CMCSC10", family="serif", smallcaps=True)):
        family, z = FONTS(run, SCALE)
        size, _ = pdf_size(FONTS, family, z, False, False, SCALE, None, jstr(run, "text"), jbool(run, "smallcaps"), False)
        assert size == pytest.approx(jnum(run, "size"), rel=0.02), run["text"]


def shape_of(run: JsonObject) -> float:
    """FontMapper's width correction for a run's letters (numbers aside, factor-independent)."""
    design = emit.font_info(jstr(run, "font")).design_size or 10
    return FONTS.shape_ratio(run, emit.FONT_FOR_FAMILY[jstr(run, "family")], 1.0, design)


def test_computer_modern_advances_give_the_pdf_its_own_line_widths(decks: tuple[Emitted, ...]) -> None:
    # The PDF side of the prediction (CM AFM advances and kerns, TFM interword and sentence
    # spaces) against the width of every one-line paragraph of plain Computer Modern text the
    # test PDFs hold: within 0.3% but for lines TeX spread (\and's quads, a justified line).
    ratios: list[float] = []
    for d in decks:
        for slide in d.plan.slides():
            for el in jobjs(slide, "elements"):
                if el["kind"] != "text":
                    continue
                for p in jobjs(el, "paragraphs") if "paragraphs" in el else []:
                    every = jobjs(p, "runs")
                    runs = [r for r in every if jstr(r, "text").strip()]
                    lines = jobjs(p, "lines")
                    if len(lines) != 1 or p.get("bullet") or not runs or len({jnum(r, "size") for r in runs}) > 1 \
                            or any(r.get("script") or r.get("hole") for r in every) \
                            or any(emit.cm_face(r) is None or r.get("smallcaps") or "\t" in jstr(r, "text") for r in runs) \
                            or sum(c.isalpha() for r in runs for c in jstr(r, "text")) < 8:
                        continue
                    em = 0.0
                    for r in every:
                        face = emit.cm_face(r)
                        assert face is not None, f"{el['id']}: {r['text']!r} is set in no Computer Modern face"
                        s, pdf, counted, skipped = emit.advance_widths(
                            jstr(r, "text"), emit.CM_ADVANCES[face],
                            emit.ADVANCES[emit.FONT_FOR_FAMILY[jstr(r, "family")]]["regular"])
                        if skipped:
                            break
                        em += pdf
                    else:
                        design = emit.font_info(jstr(runs[0], "font")).design_size
                        assert design is not None, f"{el['id']}: a Computer Modern face with no design size"
                        dw = emit.design_width(emit.DESIGN_WIDTH[jstr(runs[0], "family")], design)
                        ratios.append(em * dw * jnum(runs[0], "size") / (jnum(lines[0], "x1") - jnum(lines[0], "x0")))
    found = np.array(ratios)
    assert len(found) > 400
    assert np.median(found) == pytest.approx(1.0, abs=0.001)
    assert (np.abs(found - 1) < 0.003).mean() > 0.95


def test_slides_advances_predict_the_calibrated_widths() -> None:
    # tools/calibrate.py measured each row's width in Slides against the PDF; the prediction from
    # the advances alone (Slides' and CM's) must say the same, relative to the running text rows.
    rows = {"pangram": "The quick brown fox jumps over the lazy dog",
            "lorem": "Lorem ipsum dolor sit amet, consectetur adipiscing elit",
            "caps": "SPEAKER NOTES AND CAPITALS: WHY NOT?"}
    for family, face, cal in (("Lato", "cmss10", "fonts.json"), ("PT Serif", "cmr10", "fonts_serif.json")):
        calibration: Json = json.loads((emit.CALIBRATION_DIR / cal).read_text(encoding="utf-8"))
        measured = jobj(calibration, "fonts", family, "width_ratio")
        slides = emit.ADVANCES[family]["regular"]
        ref = FONTS.reference_ratio(face, slides, False)
        for key, text in rows.items():
            s, pdf, _, _ = emit.advance_widths(text, emit.CM_ADVANCES[face], slides)
            assert s / pdf / ref == pytest.approx(jnum(measured, "by_row", key) / jnum(measured, "text_mean"), abs=0.006), \
                (family, key)
        for style, key, bold, italic in (("bold", "bold", True, False), ("italic", "italic", False, True)):
            face2 = emit.CM_FACE[("sans" if family == "Lato" else "serif", bold, italic)]
            s, pdf, _, _ = emit.advance_widths(rows["pangram"], emit.CM_ADVANCES[face2], emit.ADVANCES[family][style])
            assert s / pdf / ref == pytest.approx(jnum(measured, "by_row", key) / jnum(measured, "text_mean"), abs=0.006), \
                (family, key)


def test_a_run_whose_letters_are_unlike_a_sentence_is_set_at_its_pdf_width() -> None:
    # PT Serif's capitals are 11% narrower than CMR's against a sentence (calibration: 0.891):
    # a line of serif capitals is sized up to the PDF's width; so is bold, and a line of w.
    def pdf_width(run: JsonObject) -> float:
        slides = emit.ADVANCES[emit.FONT_FOR_FAMILY[jstr(run, "family")]][
            emit.STYLE_KEY[(jbool(run, "bold"), jbool(run, "italic"))]]
        face = emit.cm_face(run)
        assert face is not None, f"{run['text']!r} is set in no Computer Modern face"
        em = emit.advance_widths(jstr(run, "text"), emit.CM_ADVANCES[face], slides)[1]
        return em * emit.design_width(emit.DESIGN_WIDTH[jstr(run, "family")], 10) * jnum(run, "size") * SCALE

    for run in (text_run("SPEAKER NOTES AND CAPITALS: WHY NOT?", BODY, font="CMR10", family="serif"),
                text_run("THE QUICK BROWN FOX JUMPS OVER", BODY, font="CMBX10", family="serif", bold=True)):
        assert abs(shape_of(run) - 1) > emit.SHAPE_TOL, run["text"]
        # its letters as wide against the PDF as the letters of the sentences the size factor was
        # calibrated on (bold keeps its half correction); its spaces are Slides' own
        letters: JsonObject = {**run, "text": unspaced(jstr(run, "text"))}
        assert shape_of(letters) == shape_of(run), run["text"]
        sentences: JsonObject = {**run, "text": unspaced(" ".join(emit.SHAPE_REFERENCE))}
        ordinary = slides_w([sentences], SCALE) / pdf_width(sentences)
        assert slides_w([letters], SCALE) / pdf_width(letters) == pytest.approx(ordinary, rel=0.01), run["text"]


def test_a_run_is_sized_by_its_letters_never_its_spaces() -> None:
    # real_linear-attention-a s39: half of it TeX's wide spaces after each '?', it was sized 20%
    # up; its letters (capitals, '?', quotes) are 12% narrower in Lato, and that is all it takes.
    query = text_run("Query: “A ? C ? F ? E ? B ?”", 7.97, font="CMSS8")
    slides = emit.ADVANCES["Lato"]["regular"]
    face = emit.CM_ADVANCES["cmss10"]
    letters = emit.advance_widths(unspaced(jstr(query, "text")), face, slides)
    sentence = emit.advance_widths(unspaced(" ".join(emit.SHAPE_REFERENCE)), face, slides)
    assert shape_of(query) == pytest.approx(letters[0] / letters[1] / (sentence[0] / sentence[1]))
    assert 0.85 < shape_of(query) < 0.9
    # spaces alone never size a run: TeX's after a sentence are wider than Lato's
    assert shape_of(text_run("Done. Next. Then? Yes! Fine. Good. Over.", BODY)) == 1.0


def test_ordinary_text_keeps_the_size_factor() -> None:
    # Within tolerance, too short to judge, or not Computer Modern: the deck-wide size.
    prose = FONTS(text_run("Monitor", BODY), SCALE)[1]
    for run in (text_run("SPEAKER NOTES AND CAPITALS: WHY NOT?", BODY),  # Lato's capitals: 0.945, ordinary
                text_run("WWWW MMMM", BODY), text_run("wwwwwwww", BODY),  # short: a word's own spread
                # Lato's w is 3% wider than its sentence letters: this line was 8% off only by
                # having fewer spaces than a sentence, and Lato's spaces are narrower than CM's
                text_run("wwwwwwww wwwwwwww wwwwwwww", BODY),
                text_run("Wide letters: WWWWWWWW MMMMMMMM mmmmmmmm wwwwwwww", BODY),  # 1.024 over the paragraph
                text_run("The quick brown fox jumps over the lazy dog, twice", BODY),
                text_run("SPEAKER NOTES AND CAPITALS", BODY, script="super")):
        assert shape_of(run) == 1.0, run["text"]
        if not run.get("script"):
            assert FONTS(run, SCALE)[1] == prose, run["text"]
    # real_linear-attention-a s39: half of it TeX's wide spaces after each '?', its letters alike;
    # it came out 9% larger than its neighbours.
    query = text_run('Query: "A ? C ? F ? E ? B ?"', BODY)
    assert shape_of(query) == 1.0 and FONTS(query, SCALE)[1] == prose
    fira =text_run("SPEAKER NOTES AND CAPITALS: WHY NOT?", BODY, font="FiraSans-Regular")
    assert FONTS(fira, SCALE)[1] == round(10.91 * SCALE, 1)


def test_no_run_of_the_test_decks_leaves_the_size_factor(decks: tuple[Emitted, ...]) -> None:
    # The tolerance sits outside the spread of every run of text in the test PDFs (the widest
    # of 15 characters or more is 6.3% off): a converted deck changes only where a run is an
    # outlier, and none of these is. Numbers keep their own correction.
    moved: list[tuple[str, Json, str]] = []
    for d in decks:
        for slide in d.plan.slides():
            for el in jobjs(slide, "elements"):
                paragraphs: list[list[JsonObject]]
                if el["kind"] == "text":
                    paragraphs = [jobjs(p, "runs") for p in (jobjs(el, "paragraphs") if "paragraphs" in el else [])]
                elif el["kind"] == "table":
                    paragraphs = [jobjs(c) for row in jarr(el, "cells") for c in jarr(row) if c]
                elif el["kind"] == "diagram":
                    paragraphs = [jobjs(runs) for n in jobjs(el, "nodes")
                                  for runs in (jarr(n, "paragraphs") if n.get("paragraphs") else [])]
                else:
                    paragraphs = []
                for runs in paragraphs:
                    for r in runs:
                        text = jstr(r, "text")
                        number = "".join(text.split())
                        if any(c in emit.DIGITS for c in number) and all(c in emit.NUMBER_CHARS for c in number):
                            continue
                        if emit.cm_face(r) and not r.get("smallcaps") and shape_of(r) != 1.0:
                            moved.append((d.name, el["id"], text[:40]))
    assert not moved


def test_deck_ir_reads_an_outlier_run_back_at_its_pdf_size() -> None:
    from beamer2slides.deck_ir import pdf_size
    for run in (text_run("SPEAKER NOTES AND CAPITALS: WHY NOT?", BODY, font="CMR10", family="serif"),
                text_run("SPEAKER NOTES AND CAPITALS: WHY NOT?", 8.97, font="CMR9", family="serif"),
                text_run("THE QUICK BROWN FOX JUMPS OVER", BODY, font="CMBX10", family="serif", bold=True)):
        family, z = FONTS(run, SCALE)
        assert z != FONTS({**run, "text": "The quick brown fox jumps over"}, SCALE)[1]
        size, _ = pdf_size(FONTS, family, z, jbool(run, "bold"), False, SCALE, None, jstr(run, "text"), False, False)
        assert size == pytest.approx(jnum(run, "size"), rel=0.01), run["text"]


INLINE_MATH_LINES = ((117.98, 10.91, 349.03), (131.53, 10.91, 277.31))  # (baseline, x0, x1) of its two lines


def inline_math(lines: Sequence[tuple[float, float, float]]) -> JsonObject:
    """27_text_fit, frame `inline-math`: `and $\\alpha_{t+1} = x^2$ in the same line, ...` wrapped
    in two lines. A script run carries its text's size and a small optical cut (CMSSI8)."""
    runs: list[Json] = [
        text_run("With and ", BODY), text_run("α", BODY, font="CMMI10", italic=True),
        text_run("t", BODY, font="CMSSI8", italic=True, script="sub"), text_run("+1", BODY, font="CMSS8", script="sub"),
        text_run(" = x", BODY), text_run("2", BODY, font="CMSS8", script="super"),
        text_run(" in the same line, the scripts and symbols are set in other fonts, and the line is still full.", BODY)]
    para: JsonObject = {"align": "left", "level": 0, "bullet": None, "size": 10.91, "text_x0": 10.91, "tab_x0": None,
                        "wrap_limit": 368.25, "runs": runs,
                        "lines": [{"baseline": b, "x0": x0, "x1": x1} for b, x0, x1 in lines]}
    return {"id": "p16t1", "kind": "text", "role": "body", "code": False,
            "bbox": [10.91, lines[0][0] - 10, 349.03, lines[-1][0] + 3], "paragraphs": [para]}


def run_styles(el: JsonObject) -> dict[str, JsonObject]:
    reqs = text_requests(el, SCALE)
    text = next(jstr(r, "insertText", "text") for r in reqs if "insertText" in r)
    out: dict[str, JsonObject] = {}
    for r in reqs:
        if "updateTextStyle" not in r:
            continue
        st = jobj(r, "updateTextStyle")
        if st and "baselineOffset" in jobj(st, "style"):
            out[text[jint(st, "textRange", "startIndex"):jint(st, "textRange", "endIndex")]] = jobj(st, "style")
    return out


def test_a_subscript_is_no_larger_than_its_text() -> None:
    # Slides lowers a SUBSCRIPT run 0.371 em of its own size (TeX: 0.15 em), and FontMapper gave
    # the subscript more than the text (the text's size in a small optical cut): 23.4 pt against
    # 21.2 at the torture's scale, so it hung into the next line (text_fit `crowded`). At the
    # text's size it is what a person typing a subscript gets, and still a SUBSCRIPT.
    el = inline_math(INLINE_MATH_LINES)
    styles = run_styles(el)
    runs = {jstr(r, "text"): r for r in jobjs(el, "paragraphs", 0, "runs")}
    body = FONTS(runs["With and "], SCALE)[1]
    for text in ("t", "+1"):
        assert FONTS(runs[text], SCALE)[1] > body  # what it was set at
        assert styles[text]["baselineOffset"] == "SUBSCRIPT"
        assert pt_of(styles[text]["fontSize"]) == body, text
    # a superscript already stands where TeX's does: its size is FontMapper's
    assert styles["2"]["baselineOffset"] == "SUPERSCRIPT"
    assert pt_of(styles["2"]["fontSize"]) == FONTS(runs["2"], SCALE)[1]
    # a subscript already smaller than its text keeps its own size
    small = text_run("i", 6.0, font="CMSS6", script="sub")
    sizes = emit.run_sizes([text_run("w ", BODY), small], SCALE, FONTS)
    assert sizes[1] == FONTS(small, SCALE)[1] < body


def test_a_run_lies_on_the_line_it_is_drawn_on() -> None:
    # The formula (the italic α and the superscript, larger Slides sizes than the text) is on the
    # first line: only that line is laid out at their size.
    el = inline_math(INLINE_MATH_LINES)
    p = jobj(el, "paragraphs", 0)
    runs = jobjs(p, "runs")
    sizes = emit.run_sizes(runs, SCALE, FONTS)
    alpha, body = FONTS(runs[1], SCALE)[1], FONTS(runs[0], SCALE)[1]
    assert alpha > body
    assert emit.line_sizes(p, sizes, SCALE, FONTS) == [max(sizes[:6]), body]
    # moved to the end of the paragraph, it is on the last line
    p2: JsonObject = {**p, "runs": [text_run("With and the same line, the scripts and symbols are set in other fonts, "
                                             "and the line is still full ", BODY), runs[1]]}
    assert emit.line_sizes(p2, emit.run_sizes(jobjs(p2, "runs"), SCALE, FONTS), SCALE, FONTS) == [body, alpha]
    # one size throughout: every line has it
    p3: JsonObject = {**p, "runs": [text_run("plain words " * 12, BODY)]}
    assert emit.line_sizes(p3, [body], SCALE, FONTS) == [body, body]


def test_lines_of_different_sizes_keep_the_pdf_pitch() -> None:
    # Slides' pitch from one wrapped line to the next is the first one's descent and the next
    # one's ascent (tools/probe_subscripts.py `crowding`): with one size for every line (the
    # largest run's), a line without that run came 2.2 pt too close to the one above it.
    paras: list[JsonObject] = [{"bullet": None}]
    (r,), _ = emit.vertical_layout(paras, [[100.0, 126.9]], [[22.1, 21.2]])
    assert emit.inner_pitch(22.1, r, 21.2) == pytest.approx(26.9, abs=0.01)
    (old,), _ = emit.vertical_layout(paras, [[100.0, 126.9]], [22.1])
    assert emit.inner_pitch(22.1, old, 21.2) < 26.9 - 0.8
    # one size on every line is laid out as before
    for sizes in ([[14.0, 14.0, 14.0]], [14.0]):
        assert emit.vertical_layout(paras, [[100.0, 117.0, 134.0]], sizes) == ([round(17 / 16.8, 4)], [0.0])
    # a bulleted item followed by another: the item's own lines at their pitch, the gap to the next
    # its spaceAbove (written with LIST_SPACING, tests/test_text_pitch.py)
    items: list[JsonObject] = [{"bullet": {"kind": "glyph"}}, {"bullet": {"kind": "glyph"}}]
    (r1, _), (_, above) = emit.vertical_layout(items, [[100.0, 126.9], [155.0]], [[22.1, 21.2], [21.2]])
    assert emit.inner_pitch(22.1, r1, 21.2) == pytest.approx(26.9, abs=0.01)
    first_to_next = emit.line_pitch(22.1, r1, 21.2) + emit.pitch_between(21.2, r1, 21.2, 1.0, above)
    assert first_to_next == pytest.approx(55.0, abs=emit.PX_PT / 2)


def number_table(x1: float) -> JsonObject:
    """A PDF table whose right-aligned number column ends at x1: TeX's 'Compute | 12,000'."""
    return {"id": "p0b0", "kind": "table", "frame": [100.0, 50.0, x1 + 6, 90.0], "size": 10.91,
            "columns": [{"x0": 106.0, "x1": 146.0, "align": "left"},
                        {"x0": x1 - 30.0, "x1": x1, "align": "right"}],
            "cells": [[[text_run("Compute", BODY)], [text_run("12,000", BODY)]],
                      [[text_run("Storage", BODY)], [text_run("900", BODY)]]],
            "row_baselines": [60.0, 75.0], "row_heights": [15.0, 15.0], "rules": []}


def test_a_number_column_is_as_wide_as_its_widest_number_in_slides() -> None:
    # A cell that wraps in Slides doubles its row and pushes the table over the caption below.
    reqs = table_requests(number_table(188.0), "b2s_s001", "b2s_s001_b0", SCALE, False)
    need = slides_w([text_run("12,000", BODY)], SCALE)
    widths = column_widths(reqs)
    assert widths[1] >= need + 2 * emit.TABLE_CELL_PAD + emit.WRAP_MARGIN
    assert widths[0] >= slides_w([text_run("Compute", BODY)], SCALE) + 2 * emit.TABLE_CELL_PAD
    # The right alignment indent never eats into the room the number needs.
    for r in reqs:
        style: JsonObject = jobj(r, "updateParagraphStyle") if "updateParagraphStyle" in r else {}
        if style and style["cellLocation"] == {"rowIndex": 0, "columnIndex": 1}:
            inner = jobj(style, "style")
            end = jnum(inner, "indentEnd", "magnitude") if "indentEnd" in inner else 0
            assert widths[1] - 2 * emit.TABLE_CELL_PAD - end >= need


def test_a_crowded_column_pushes_the_columns_after_it_along() -> None:
    # Only 12 pt between the columns: they cannot both fit where TeX put them, so the table grows.
    reqs = table_requests(number_table(176.0), "b2s_s001", "b2s_s001_b0", SCALE, False)
    need = [slides_w([text_run(t, BODY)], SCALE) for t in ("Compute", "12,000")]
    assert all(w >= n + 2 * emit.TABLE_CELL_PAD for w, n in zip(column_widths(reqs), need))


def test_a_cell_spanning_columns_is_as_wide_as_its_words() -> None:
    table = number_table(188.0)
    heading: list[Json] = [[text_run("Monthly running costs in francs", BODY)], []]
    jarr(table, "cells").insert(0, heading)
    table["merges"] = [{"row": 0, "col": 0, "rows": 1, "cols": 2, "align": "center"}]
    jarr(table, "row_baselines").insert(0, 45.0)
    jarr(table, "row_heights").insert(0, 15.0)
    widths = column_widths(table_requests(table, "b2s_s001", "b2s_s001_b0", SCALE, False))
    words = slides_w([text_run("Monthly running costs in francs", BODY)], SCALE)
    assert sum(widths) >= words + 2 * emit.TABLE_CELL_PAD + emit.WRAP_MARGIN


TIGHT_SCALE = SLIDE_W / 364.19  # 27_text_fit is a 4:3 beamer page


def tight_table() -> JsonObject:
    """27_text_fit, frame `table-tight`: a \\footnotesize booktabs table, rows 10.96 pt apart."""
    rows = [["Dataset", "Split", "Images", "Classes"], ["ImageNet-1k", "train", "1,281,167", "1000"],
            ["ImageNet-1k", "val", "50,000", "1000"], ["Places365", "train", "1,803,460", "365"],
            ["Monitor", "Workstation", "12,345,678", "99"]]

    def rule(row: int, position: str, weight: float, y: float) -> JsonObject:
        return {"row": row, "position": position, "color": "#000000", "weight": weight, "y": y}

    return {"id": "p9tab0", "kind": "table", "frame": [80.99, 89.16, 281.84, 155.55], "size": 8.97,
            "row_baselines": [100.42, 117.01, 127.97, 138.93, 149.89], "row_heights": [16.59, 10.96, 10.96, 10.96, 10.96],
            "columns": [{"x0": 80.99, "x1": 130.08, "align": "left"}, {"x0": 142.03, "x1": 188.67, "align": "left"},
                        {"x0": 200.63, "x1": 242.62, "align": "right"}, {"x0": 254.57, "x1": 281.76, "align": "right"}],
            "bounds": [80.99, 136.06, 194.65, 248.59, 281.84], "merges": [], "borders": [], "fills": [],
            "rules": [rule(0, "TOP", 0.87, 89.16), rule(1, "TOP", 0.55, 105.91), rule(4, "BOTTOM", 0.87, 155.55)],
            "cells": [[[text_run(t, 8.97, font="CMSS9")] for t in row] for row in rows]}


def test_a_table_from_the_pptx_keeps_the_pdf_row_pitch() -> None:
    # A createTable table has 7.2 pt of padding above and below every row, which the API cannot
    # set: a \footnotesize booktabs table came out 4 pt taller than the PDF's and ran towards its
    # caption. The .pptx brings the table with margins of its own (tools/probe_pptx_table_margins.py).
    el, scale = tight_table(), TIGHT_SCALE
    lay = emit.table_layout(el, scale, FONTS, imported=True, page_w=SLIDE_W / scale)
    assert lay.y == pytest.approx(89.16 * scale) and lay.y + sum(lay.heights) == pytest.approx(155.55 * scale, abs=0.05)
    assert lay.ratios == pytest.approx([1.0] * 5)  # the text keeps its own line spacing
    # Every baseline where the PDF has it: the top inset takes booktabs' space under a rule.
    y = lay.y
    for b, h, inset in zip(jnums(el, "row_baselines"), lay.heights, lay.insets):
        assert inset >= 0 and y + inset + emit.TABLE_TEXT_TOP + emit.ASCENT_EM * lay.z == pytest.approx(b * scale, abs=0.05)
        y += h
    assert lay.insets[1] > 1.0 and lay.insets[2] == pytest.approx(0.0, abs=1e-6)  # under \midrule / no rule
    api = emit.table_layout(el, scale, FONTS, imported=False, page_w=SLIDE_W / scale)
    assert sum(api.heights) > sum(lay.heights) + 3  # what an API-made table grows by


def test_a_table_from_the_pptx_is_filled_not_created() -> None:
    el, scale = tight_table(), TIGHT_SCALE
    reqs = table_requests(el, "b2s_s009", "b2s_s009_tab3", scale, True)
    assert not any("createTable" in r for r in reqs)
    assert reqs[0] == {"updatePageElementsZOrder": {"pageElementObjectIds": ["b2s_s009_tab3"], "operation": "BRING_TO_FRONT"}}
    assert any("createTable" in r for r in table_requests(el, "b2s_s009", "b2s_s009_tab3", scale, False))  # sync's way
    table = emit.pptx_table(el, scale, FONTS, SLIDE_W / scale)
    lay = emit.table_layout(el, scale, FONTS, imported=True, page_w=SLIDE_W / scale)
    assert table["margins"] == [[emit.TABLE_CELL_PAD, round(t, 2), emit.TABLE_CELL_PAD, 0.0] for t in lay.insets]
    heights = [jnum(r, "updateTableRowProperties", "tableRowProperties", "minRowHeight", "magnitude") / EMU_PER_PT
               for r in reqs if "updateTableRowProperties" in r]
    assert heights == pytest.approx(table["heights"], abs=0.01)


def test_the_pptx_carries_each_table_empty_with_its_margins() -> None:
    from pptx import Presentation
    from pptx.shapes.graphfrm import GraphicFrame
    el, scale = tight_table(), TIGHT_SCALE
    table = emit.pptx_table(el, scale, FONTS, SLIDE_W / scale)
    typed = pptx_table_of(table_of(el), scale, FONTS, emit.SLIDE_W / scale)
    page: dict[str, object] = {"layout": "BLANK", "fill": None, "pictures": [], "tables": [typed], "templates": False}
    prs = Presentation(emit.build_pptx(364.19, 273.14, [], [page], WHITE, None))
    frames = [s for s in prs.slides[0].shapes if isinstance(s, GraphicFrame) and s.has_table]
    assert len(frames) == 1
    grid = frames[0].table
    assert (len(grid.rows), len(grid.columns)) == (5, 4)
    assert [round(c.width / EMU_PER_PT, 2) for c in grid.columns] == pytest.approx(table["widths"], abs=0.01)
    assert [round(r.height / EMU_PER_PT, 2) for r in grid.rows] == pytest.approx(table["heights"], abs=0.01)
    for r, (left, top, right, bottom) in enumerate(table["margins"]):
        cell = grid.cell(r, 3)
        assert (cell.margin_left, cell.margin_top, cell.margin_right, cell.margin_bottom) == \
            tuple(round(v * EMU_PER_PT) for v in (left, top, right, bottom))
        assert cell.text == ""
    tbl = frames[0]._element.graphic.graphicData.tbl
    assert tbl is not None
    pr = tbl.tblPr
    assert pr is not None
    assert pr.get("firstRow") is None and pr.get("bandRow") is None  # no header look, no bands
    style_id = pr.find(f"{{{emit.NS_A}}}tableStyleId")
    assert style_id is not None
    assert style_id.text == emit.NO_TABLE_STYLE


def test_a_wrapped_cell_wraps_in_its_column() -> None:
    # 27_text_fit, frame `table-merged`: a p{3.2cm} cell is one cell of three lines, and its row
    # holds them. Its column holds each of the PDF's lines, a hyphenated word whole ("A cell set
    # in a para-" became "A cell set in a" in Slides, and the cell four lines): not the whole
    # paragraph on one line.
    el = tight_table()
    long = "A cell set in a paragraph column, which wraps in the PDF too"
    jarr(el, "cells", 2)[1] = [text_run(long, 8.97, font="CMSS9")]
    starts: list[Json] = [long.index("graph"), long.index("wraps")]
    el["wrapped"] = [[2, 1, starts]]
    el["row_lines"] = [1, 1, 3, 1, 1]
    el["row_baselines"] = [100.42, 117.01, 127.97, 160.85, 171.81]
    el["row_heights"] = [16.59, 10.96, 32.88, 10.96, 10.96]
    jarr(el, "frame")[3] = 177.47
    jobj(el, "rules", 2)["y"] = 177.47
    lay = emit.table_layout(el, TIGHT_SCALE, FONTS, imported=True, page_w=SLIDE_W / TIGHT_SCALE)
    # (a cell's run is set at the table's size, not shaped: `cell`, FontMapper.shape_ratio; its
    # numbers, as wide as Lato sets them, shrink this tight table a little)
    widest = slides_w([{**text_run("A cell set in a paragraph", 8.97 * lay.shrink, font="CMSS9"), "cell": True}],
                      TIGHT_SCALE)
    assert lay.cell_width[(2, 1)] == pytest.approx(widest)
    assert widest + 2 * emit.TABLE_CELL_PAD <= lay.widths[1] < slides_w(jobjs(el, "cells", 2, 1), TIGHT_SCALE)
    assert lay.heights[2] >= 3 * emit.LINE_EM * lay.z - 1 and lay.ratios[2] == pytest.approx(1.0)
    assert lay.y + sum(lay.heights) == pytest.approx(177.47 * TIGHT_SCALE, abs=0.05)


def test_a_subscript_in_a_cell_is_no_larger_than_its_text() -> None:
    # 16_colored_table, `Math in cells`: lambda_max's subscript hung 3 pt under the table's last row.
    table = number_table(188.0)
    math_runs = jat(inline_math(INLINE_MATH_LINES), "paragraphs", 0, "runs")
    jarr(table, "cells", 0)[0] = math_runs
    runs = jobjs(math_runs)
    reqs = table_requests(table, "b2s_s001", "b2s_s001_b0", SCALE, True)
    text = "".join(jstr(r, "text") for r in runs).strip()
    sizes: dict[str, tuple[Json, float]] = {}
    for r in reqs:
        if "updateTextStyle" not in r:
            continue
        st = jobj(r, "updateTextStyle")
        style = jobj(st, "style")
        if st.get("cellLocation") == {"rowIndex": 0, "columnIndex": 0} and "baselineOffset" in style:
            sizes[text[jint(st, "textRange", "startIndex"):jint(st, "textRange", "endIndex")]] = \
                (style["baselineOffset"], pt_of(style["fontSize"]))
    body = FONTS(runs[0], SCALE)[1]
    for piece in ("t", "+1"):
        assert sizes[piece] == ("SUBSCRIPT", body)


def test_a_hebrew_table_cell_reads_right_to_left_and_stays_where_it_is_drawn() -> None:
    reqs = table_requests(hebrew_table(), "b2s_s001", "b2s_s001_b0", SCALE, False)
    styles = {jint(r, "updateParagraphStyle", "cellLocation", "columnIndex"): jobj(r, "updateParagraphStyle", "style")
              for r in reqs if "updateParagraphStyle" in r}
    assert styles[0]["direction"] == "RIGHT_TO_LEFT" and styles[0]["alignment"] == "END"
    assert "direction" not in styles[1] and styles[1]["alignment"] == "START"
