"""Checks on a synced Slides deck, read through presentations.get.

A check is a small JSON record (tools/deck_edits.py returns them as the expectations of its
edits, tests/decks/sync/build.py as what a source change must show):
  {"check": "text", "slide": SEL|null, "text": "...", "count": n}      occurrences in shapes and cells
  {"check": "title", "slide": SEL, "text": "..."}                        title placeholder, first paragraph
  {"check": "style", "slide": SEL, "text": "...", "context": "...", "bold"|"italic"|"color"|"size": ...}
  {"check": "box", "slide": SEL, "target": TARGET, "origin": [x, y], "size": [w, h], "tol": pt}
  {"check": "image", "slide": SEL, "near": [cx, cy], "colour": "#rrggbb", "count": n}   valid pictures
  {"check": "shape", "slide": SEL, "shape_type": "STAR_5", "near": [cx, cy], "color": "#rrggbb", "count": n}
  {"check": "grouped", "slide": SEL, "members": [TARGET, ...], "grouped": bool}
  {"check": "slides", "order": [SEL, ...], "adjacent": bool}             each once, in this order
  {"check": "slide_count", "slide": SEL, "count": n}
  {"check": "notes", "slide": SEL, "text": "..."}
  {"check": "background", "slide": SEL, "color": "#rrggbb"}
  {"check": "fresh", "slide": SEL}                      the slide matches a fresh conversion (compare_fresh)
SEL: {"title": "..."}, {"contains": "..."} (any text on the slide) or {"index": i}; a string is a title;
null in text checks: the whole deck.
TARGET: {"text": "...", "near": [cx, cy]?}, {"image_near": [cx, cy]}, {"image": "largest"} or {"id": objectId}.
Positions are absolute slide pt (720 pt wide), text is compared with whitespace normalised.

On top: `integrity` (duplicates, orphans, groups intact), `check_report` (sync-report sections),
`compare_fresh` (untouched slides vs a fresh conversion: elements, notes, background, thumbnails)
and `alignment_compare` (tools/alignment.py on the synced deck vs the fresh conversion).

  python tools/sync_check.py <presentation id|url|out folder> checks.json [--report sync-report.json]
"""

import argparse
import io
import json
import re
import shutil
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

EMU_PER_PT = 12700
TOL = 1.5


class CheckError(Exception):
    pass


def norm(s: str | None) -> str:
    return " ".join((s or "").replace("\xa0", " ").replace("\x0b", " ").split())


def hex_color(c: dict | None) -> str | None:
    rgb = (c or {}).get("rgbColor")
    if rgb is None:
        return None
    return "#" + "".join(f"{round(rgb.get(k, 0.0) * 255):02x}" for k in ("red", "green", "blue"))


def _affine(t: dict | None) -> np.ndarray:
    t = t or {"scaleX": 1, "scaleY": 1}
    unit = EMU_PER_PT if t.get("unit", "EMU") == "EMU" else 1.0
    return np.array([[t.get("scaleX", 0.0), t.get("shearX", 0.0), t.get("translateX", 0.0) / unit],
                     [t.get("shearY", 0.0), t.get("scaleY", 0.0), t.get("translateY", 0.0) / unit], [0, 0, 1]])


def text_elements(obj: dict) -> list[dict]:
    return obj.get("text", {}).get("textElements", [])


def raw_text(text_els: list[dict]) -> str:
    return "".join(t.get("textRun", {}).get("content", "") or t.get("autoText", {}).get("content", "") for t in text_els)


@dataclass
class Element:
    obj: dict
    kind: str                 # shape, image, table, line, group, other
    box: list[float]          # absolute, slide pt
    groups: tuple[str, ...]   # enclosing group objectIds, outermost first
    texts: list[tuple[str, dict | None]] = field(default_factory=list)  # (raw text, cellLocation) per container

    @property
    def id(self) -> str:
        return self.obj["objectId"]

    @property
    def parent(self) -> str | None:
        return self.groups[-1] if self.groups else None

    @property
    def top(self) -> str:
        return self.groups[0] if self.groups else self.id

    @property
    def text(self) -> str:
        return norm(" | ".join(t for t, _ in self.texts))

    @property
    def center(self) -> tuple[float, float]:
        return (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2

    def descriptor(self) -> str:
        if self.kind == "shape":
            return f"shape:{self.obj['shape'].get('shapeType')}:{self.text[:80]}"
        if self.kind == "table":
            return f"table:{self.text[:80]}"
        return self.kind


def flatten(page_elements: list[dict], parent_m=None, groups: tuple[str, ...] = ()) -> list[Element]:
    parent_m = np.eye(3) if parent_m is None else parent_m
    out = []
    for e in page_elements:
        m = parent_m @ _affine(e.get("transform"))
        kind = next((k for k in ("shape", "image", "table", "line") if k in e), "group" if "elementGroup" in e else "other")
        box = [0.0, 0.0, 0.0, 0.0]
        if "size" in e:
            w, h = (e["size"][k]["magnitude"] / (EMU_PER_PT if e["size"][k].get("unit", "EMU") == "EMU" else 1)
                    for k in ("width", "height"))
            pts = m @ np.array([[0, w, 0, w], [0, 0, h, h], [1, 1, 1, 1]])
            box = [float(pts[0].min()), float(pts[1].min()), float(pts[0].max()), float(pts[1].max())]
        el = Element(e, kind, box, groups)
        if kind == "shape":
            el.texts = [(raw_text(text_elements(e["shape"])), None)]
        elif kind == "table":
            el.texts = [(raw_text(text_elements(cell)), {"rowIndex": r, "columnIndex": c})
                        for r, row in enumerate(e["table"].get("tableRows", []))
                        for c, cell in enumerate(row.get("tableCells", []))]
        children = flatten(e["elementGroup"].get("children", []), m, groups + (e["objectId"],)) if kind == "group" else []
        if children:
            xs = [c.box for c in children]
            el.box = [min(b[0] for b in xs), min(b[1] for b in xs), max(b[2] for b in xs), max(b[3] for b in xs)]
        out.append(el)
        out += children
    return out


@dataclass
class Slide:
    obj: dict
    index: int
    elements: list[Element]

    @property
    def id(self) -> str:
        return self.obj["objectId"]

    @property
    def title(self) -> str | None:
        for e in self.elements:
            if e.kind == "shape" and e.obj["shape"].get("placeholder", {}).get("type") in ("TITLE", "CENTERED_TITLE"):
                return norm(e.texts[0][0].split("\n")[0])
        return None

    @property
    def all_text(self) -> str:
        return norm(" ".join(e.text for e in self.elements if e.texts))

    def notes_shape(self) -> dict | None:
        page = self.obj.get("slideProperties", {}).get("notesPage", {})
        nid = page.get("notesProperties", {}).get("speakerNotesObjectId")
        return next((e for e in page.get("pageElements", []) if e["objectId"] == nid), None)

    @property
    def notes(self) -> str:
        shape = self.notes_shape()
        return norm(raw_text(text_elements(shape["shape"]))) if shape and "shape" in shape else ""

    @property
    def background(self) -> str | None:
        fill = self.obj.get("pageProperties", {}).get("pageBackgroundFill", {})
        return hex_color(fill.get("solidFill", {}).get("color")) if fill.get("propertyState", "RENDERED") == "RENDERED" else None


class Model:
    """A presentations.get result with lookups by content."""

    def __init__(self, pres: dict):
        self.pres = pres
        self.slides = [Slide(s, i, flatten(s.get("pageElements", []))) for i, s in enumerate(pres.get("slides", []))]

    @property
    def revision(self) -> str | None:
        return self.pres.get("revisionId")

    def find(self, sel) -> list[Slide]:
        if sel is None:
            return self.slides
        if isinstance(sel, str):
            sel = {"title": sel}
        if "index" in sel:
            return self.slides[sel["index"]:sel["index"] + 1]
        if "title" in sel:
            return [s for s in self.slides if s.title == norm(sel["title"])]
        return [s for s in self.slides if norm(sel["contains"]) in s.all_text]

    def one(self, sel) -> Slide:
        found = self.find(sel)
        if len(found) != 1:
            raise CheckError(f"slide {json.dumps(sel, ensure_ascii=False)}: {len(found)} slides match")
        return found[0]

    def element(self, slide: Slide, target: dict) -> Element:
        if "id" in target:
            found = [e for e in slide.elements if e.id == target["id"]]
        elif "text" in target:
            phrase = norm(target["text"])
            found = [e for e in slide.elements if e.kind in ("shape", "table") and phrase in e.text]
            if "near" in target:
                found = [e for e in found if max(abs(e.center[0] - target["near"][0]),
                                                 abs(e.center[1] - target["near"][1])) <= target.get("tol", 4)]
        elif "image_near" in target:
            cx, cy = target["image_near"]
            found = [e for e in slide.elements if e.kind == "image" and abs(e.center[0] - cx) <= 4 and abs(e.center[1] - cy) <= 4]
        elif target.get("image") == "largest":
            images = sorted((e for e in slide.elements if e.kind == "image"),
                            key=lambda e: (e.box[2] - e.box[0]) * (e.box[3] - e.box[1]), reverse=True)
            found = images[:1]
        else:
            raise CheckError(f"unknown target {target}")
        if len(found) != 1:
            raise CheckError(f"slide {slide.index + 1} ({slide.title}): {len(found)} elements match {target}")
        return found[0]


def phrase_span(raw: str, phrase: str) -> tuple[int, int] | None:
    """(start, end) of a phrase in raw text, whitespace-insensitive; code point indices."""
    words = norm(phrase).split(" ")
    m = re.search(r"[\s\xa0\x0b]+".join(map(re.escape, words)), raw)
    return (m.start(), m.end()) if m else None


def utf16(s: str, i: int) -> int:
    """Slides text indices count UTF-16 code units."""
    return len(s[:i].encode("utf-16-le")) // 2


def style_runs(text_els: list[dict], raw: str, start: int, end: int) -> list[dict]:
    """Styles of the runs overlapping [start, end) (code point indices of `raw`)."""
    a, b = utf16(raw, start), utf16(raw, end)
    return [t["textRun"].get("style", {}) for t in text_els
            if "textRun" in t and t.get("startIndex", 0) < b and t["endIndex"] > a and t["textRun"]["content"].strip()]


def _download(url: str) -> Image.Image:
    with urllib.request.urlopen(url, timeout=60) as r:
        return Image.open(io.BytesIO(r.read())).convert("RGBA")


def image_problem(el: Element, colour: str | None) -> str | None:
    """None if the picture downloads, decodes, isn't empty and (optionally) shows the colour."""
    url = el.obj["image"].get("contentUrl")
    if not url:
        return f"picture {el.id} has no contentUrl"
    try:
        img = np.asarray(_download(url)).astype(np.int16)
    except Exception as e:  # noqa: BLE001
        return f"picture {el.id} doesn't load: {e}"
    if img.shape[0] < 2 or img.shape[1] < 2:
        return f"picture {el.id} is {img.shape[1]}x{img.shape[0]} px"
    opaque = img[..., 3] > 32
    rgb = img[..., :3]
    if not opaque.any() or (opaque.all() and np.ptp(rgb.reshape(-1, 3), axis=0).max() < 8):
        return f"picture {el.id} is blank"
    if colour:
        want = np.array([int(colour[i:i + 2], 16) for i in (1, 3, 5)])
        close = (np.abs(rgb - want).max(axis=2) < 70) & opaque
        if close.mean() < 0.001:
            return f"picture {el.id} shows no {colour}"
    return None


def evaluate(model: Model, check: dict) -> str | None:
    """None if the check holds, else what is wrong."""
    try:
        return _evaluate(model, check)
    except CheckError as e:
        return str(e)


def _evaluate(model: Model, c: dict) -> str | None:
    kind = c["check"]
    what = json.dumps(c, ensure_ascii=False)
    if kind == "fresh":
        return None  # compare_fresh
    if kind == "slides":
        idx = [model.one(sel).index for sel in c["order"]]
        ok = idx == sorted(set(idx)) and (not c.get("adjacent") or idx == list(range(idx[0], idx[0] + len(idx))))
        return None if ok else f"slides at {idx}: {what}"
    if kind == "slide_count":
        n = len(model.find(c["slide"]))
        return None if n == c["count"] else f"{n} slides match: {what}"
    if kind == "text":
        phrase = norm(c["text"])
        n = sum(norm(t).count(phrase) for s in model.find(c.get("slide")) for e in s.elements for t, _ in e.texts)
        return None if n == c.get("count", 1) else f"text found {n} times: {what}"
    slide = model.one(c["slide"])
    if kind == "title":
        return None if slide.title == norm(c["text"]) else f"title is {slide.title!r}: {what}"
    if kind == "notes":
        return None if slide.notes == norm(c["text"]) else f"notes are {slide.notes!r}: {what}"
    if kind == "background":
        return None if slide.background == c["color"].lower() else f"background is {slide.background}: {what}"
    if kind == "style":
        el = model.element(slide, {"text": c.get("context", c["text"])})
        for raw, cell in el.texts:
            ctx = phrase_span(raw, c.get("context", c["text"]))
            if not ctx:
                continue
            span = phrase_span(raw[ctx[0]:ctx[1]], c["text"])
            if not span:
                continue
            styles = style_runs(el.obj["shape"]["text"]["textElements"] if cell is None else
                                el.obj["table"]["tableRows"][cell["rowIndex"]]["tableCells"][cell["columnIndex"]]["text"]["textElements"],
                                raw, ctx[0] + span[0], ctx[0] + span[1])
            bad = []
            for st in styles:
                for key in ("bold", "italic", "underline", "strikethrough"):
                    if key in c and bool(st.get(key)) != c[key]:
                        bad.append(f"{key}={st.get(key)}")
                if "color" in c and hex_color(st.get("foregroundColor", {}).get("opaqueColor")) != c["color"].lower():
                    bad.append(f"color={hex_color(st.get('foregroundColor', {}).get('opaqueColor'))}")
                if "size" in c and abs(st.get("fontSize", {}).get("magnitude", -1) - c["size"]) > 0.01:
                    bad.append(f"size={st.get('fontSize', {}).get('magnitude')}")
            return f"style {sorted(set(bad))}: {what}" if bad or not styles else None
        return f"text not found for style: {what}"
    if kind == "box":
        el = model.element(slide, c["target"])
        tol = c.get("tol", TOL)
        bad = []
        if "origin" in c and max(abs(el.box[0] - c["origin"][0]), abs(el.box[1] - c["origin"][1])) > tol:
            bad.append(f"origin {[round(v, 1) for v in el.box[:2]]}")
        size = [el.box[2] - el.box[0], el.box[3] - el.box[1]]
        if "size" in c and max(abs(size[0] - c["size"][0]), abs(size[1] - c["size"][1])) > tol:
            bad.append(f"size {[round(v, 1) for v in size]}")
        return f"{', '.join(bad)}: {what}" if bad else None
    if kind in ("image", "shape"):
        found = [e for e in slide.elements if e.kind == kind]
        if kind == "shape":
            found = [e for e in found if e.obj["shape"].get("shapeType") == c["shape_type"]]
            if "color" in c:
                found = [e for e in found if hex_color(e.obj["shape"].get("shapeProperties", {}).get("shapeBackgroundFill", {})
                                                       .get("solidFill", {}).get("color")) == c["color"].lower()]
        if "near" in c:
            found = [e for e in found if max(abs(e.center[0] - c["near"][0]), abs(e.center[1] - c["near"][1])) <= c.get("tol", 4)]
        if kind == "image":
            problems = [(e, image_problem(e, c.get("colour"))) for e in found]
            if c.get("colour"):
                found = [e for e, p in problems if p is None]
            elif any(p for _, p in problems):
                return "; ".join(p for _, p in problems if p) + f": {what}"
        n = len(found)
        return None if n == c.get("count", 1) else f"{n} {kind}s match: {what}"
    if kind == "grouped":
        members = [model.element(slide, t) for t in c["members"]]
        together = bool(set.intersection(*(set(m.groups) for m in members)))
        return None if together == c["grouped"] else f"groups {[m.groups for m in members]}: {what}"
    raise CheckError(f"unknown check {kind}")


def check_all(model: Model, checks: list[dict]) -> list[str]:
    return [p for c in checks if (p := evaluate(model, c))]


# ---------------------------------------------------------------- integrity

def groups(model: Model) -> dict[str, list[tuple[str, frozenset]]]:
    """slide title -> [(group id, member descriptors)] of every group."""
    out = {}
    for s in model.slides:
        out[s.title or f"#{s.index}"] = [
            (e.id, frozenset(c.descriptor() for c in s.elements if c.parent == e.id))
            for e in s.elements if e.kind == "group"]
    return out


def integrity(model: Model, before: Model | None = None, base_ids: set[str] | None = None,
              allow_groups_changed: set[str] = frozenset()) -> list[str]:
    """Duplicates (same kind, text and box twice on a slide), orphans (sync objects the base
    doesn't know, empty text boxes of ours, formula pictures out of their text's group, empty
    groups) and groups of `before` whose members all survived but no longer form a group."""
    problems = []
    for s in model.slides:
        name = f"slide {s.index + 1} ({s.title})"
        seen = {}
        for e in s.elements:
            if e.kind == "group":
                if not any(c.parent == e.id for c in s.elements):
                    problems.append(f"{name}: empty group {e.id}")
                continue
            key = (e.descriptor(), e.obj.get("description", ""), tuple(round(v * 2) for v in e.box))
            if key in seen:
                problems.append(f"{name}: duplicate {e.descriptor()} {e.id} and {seen[key]}")
            seen[key] = e.id
            if base_ids is not None and e.id.startswith("b2s_") and e.id not in base_ids:
                problems.append(f"{name}: orphan {e.descriptor()} {e.id} (not in the base)")
            if e.kind == "shape" and e.id.startswith("b2s_") and not e.text and \
                    e.obj["shape"].get("shapeType") == "TEXT_BOX" and "placeholder" not in e.obj["shape"]:
                problems.append(f"{name}: empty text box {e.id}")
            tag = e.obj.get("title") or ""
            if e.kind == "image" and (tag in ("Formula", "Icon") or re.search(r"/image/(math|icon)/", tag)):
                if e.parent is None or not any(c.parent == e.parent and c.kind == "shape" for c in s.elements):
                    problems.append(f"{name}: formula picture {e.id} is not grouped with its text")
    if before is not None:
        now = groups(model)
        members_now = {t: {c.descriptor() for c in s.elements} for s in model.slides for t in [s.title or f"#{s.index}"]}
        for title, gs in groups(before).items():
            if title in allow_groups_changed or title not in now:
                continue
            for gid, members in gs:
                if members <= members_now[title] and not any(members <= m for _, m in now[title]):
                    problems.append(f"slide {title}: group {gid} of {sorted(members)} came apart")
    return problems


def ids_in(data) -> set[str]:
    """Every string in a JSON value (the object ids a base snapshot knows, whatever its layout)."""
    if isinstance(data, str):
        return {data}
    if isinstance(data, dict):
        return set().union(*(ids_in(k) | ids_in(v) for k, v in data.items())) if data else set()
    if isinstance(data, list):
        return set().union(*map(ids_in, data)) if data else set()
    return set()


# ---------------------------------------------------------------- report

REPORT_SECTIONS = {
    "conflicts": ("conflicts",),
    "converged": ("converged", "converged_overrides"),
    "overrides": ("overrides", "deck_overrides", "deck_edits_kept", "kept"),
    "applied": ("applied", "source_changes", "changes_applied"),
    "slides": ("slides_created", "slides_deleted", "slides_moved", "created", "deleted", "moved"),
}


def section(report: dict, name: str) -> list:
    out = []
    for key in REPORT_SECTIONS[name]:
        value = report.get(key)
        if isinstance(value, list):
            out += value
        elif isinstance(value, dict):
            out += [value] if name != "slides" else [v for vs in value.values() if isinstance(vs, list) for v in vs]
    return out


def changes(report: dict) -> int:
    """How many writes a report lists (applied source changes and slide operations)."""
    return len(section(report, "applied")) + len(section(report, "slides"))


def check_report(report: dict, conflicts: list[list[str]] = (), converged: list[list[str]] = (),
                 overrides: list[list[str]] = (), no_conflicts: bool = False) -> list[str]:
    """Each expected entry is a list of strings one report entry of that section must all contain
    (e.g. both versions of a conflicting text)."""
    problems = []
    for name, expected in (("conflicts", conflicts), ("converged", converged), ("overrides", overrides)):
        entries = [json.dumps(e, ensure_ascii=False) for e in section(report, name)]
        for words in expected:
            if not any(all(norm(w) in norm(e) for w in words) for e in entries):
                problems.append(f"report: no {name} entry with {words}")
    if no_conflicts and section(report, "conflicts"):
        problems.append(f"report: unexpected conflicts {section(report, 'conflicts')}")
    return problems


# ---------------------------------------------------------------- fresh conversion

def compare_fresh(synced: Model, ref: Model, titles: list[str], tol: float = 2.0) -> list[str]:
    """Slides (by title) of the synced deck against a fresh conversion of the same source: the
    same elements (kind, text, box), groups, notes and background."""
    problems = []
    for title in titles:
        try:
            s, r = synced.one(title), ref.one(title)
        except CheckError as e:
            problems.append(f"fresh: {e}")
            continue
        free = [e for e in s.elements if e.kind != "group"]
        for e in (e for e in r.elements if e.kind != "group"):
            match = [x for x in free if x.descriptor() == e.descriptor() and
                     max(abs(a - b) for a, b in zip(x.box, e.box)) <= tol]
            if match:
                free.remove(min(match, key=lambda x: max(abs(a - b) for a, b in zip(x.box, e.box))))
            else:
                problems.append(f"fresh {title}: missing {e.descriptor()} at {[round(v) for v in e.box]}")
        problems += [f"fresh {title}: extra {x.descriptor()} {x.id} at {[round(v) for v in x.box]}" for x in free]
        gs = sorted(sorted(m) for _, m in groups(synced)[title])
        gr = sorted(sorted(m) for _, m in groups(ref)[title])
        if gs != gr:
            problems.append(f"fresh {title}: groups {gs} != {gr}")
        if s.notes != r.notes:
            problems.append(f"fresh {title}: notes {s.notes!r} != {r.notes!r}")
        if s.background != r.background:
            problems.append(f"fresh {title}: background {s.background} != {r.background}")
    return problems


def thumbnail_diff(slides_api, a: tuple[str, str], b: tuple[str, str], path: Path, limit: float = 0.002) -> str | None:
    """Google's thumbnails of two slides ((presentation id, page id)) differ in more than `limit`
    of their pixels: the problem, with a diff PNG (red: only in a, blue: only in b)."""
    from beamer2slides.gslides import save_thumbnail
    path.parent.mkdir(parents=True, exist_ok=True)
    pa, pb = path.with_name(path.stem + "-synced.png"), path.with_name(path.stem + "-fresh.png")
    save_thumbnail(slides_api, a[0], a[1], pa)
    save_thumbnail(slides_api, b[0], b[1], pb)
    ia = np.asarray(Image.open(pa).convert("RGB")).astype(np.int16)
    ib = np.asarray(Image.open(pb).convert("RGB")).astype(np.int16)
    if ia.shape != ib.shape:
        return f"thumbnails {pa.name} and {pb.name} differ in size"
    differ = np.abs(ia - ib).max(axis=2) > 60
    share = float(differ.mean())
    if share <= limit:
        return None
    diff = np.full(ia.shape, 255, dtype=np.uint8)
    diff[differ & (ia.sum(axis=2) < ib.sum(axis=2))] = (220, 40, 40)
    diff[differ & (ia.sum(axis=2) >= ib.sum(axis=2))] = (30, 110, 230)
    Image.fromarray(diff).save(path)
    return f"thumbnail differs in {share:.2%} of pixels ({path})"


def alignment_compare(ref_out: Path, synced: Model, ref: Model, pid: str, titles: list[str], work: Path,
                      growth: float = 0.75) -> list[str]:
    """tools/alignment.py on the synced deck's slides `titles`, against the same measurement of
    the fresh conversion in `ref_out` (holes, numbers, bullets, overlays). The synced objects are
    found by kind, text and box, so the measurement can use the fresh conversion's deck.json."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import alignment
    from beamer2slides.google_auth import slides_service
    from beamer2slides.gslides import save_thumbnail

    deck = json.loads((ref_out / "deck.json").read_text(encoding="utf-8"))
    state = json.loads((ref_out / "emit.json").read_text(encoding="utf-8"))
    # The measurement folders refer to the fresh conversion's PDF and pictures (alignment keeps the PDF open).
    pdf = ref_out / "slides.pdf" if (ref_out / "slides.pdf").exists() else Path(deck["source"]["pdf"])
    deck = {**deck, "source": {**deck["source"], "pdf": str(pdf)}, "slides": [
        {**s, "elements": [{**e, "file": str(ref_out / e["file"])} if e.get("file") else e for e in s["elements"]]}
        for s in deck["slides"]]}
    api = slides_service()
    reports = {}
    for name, model, presentation in (("fresh", ref, state["presentationId"]), ("synced", synced, pid)):
        folder = work / name
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "fidelity").mkdir(parents=True, exist_ok=True)
        for old in (folder / "fidelity").glob("*.png"):
            old.unlink()
        slides, emitted = [], []
        for dslide, eslide in zip(deck["slides"], state["slides"]):
            r = next((s for s in ref.slides if s.id == eslide["objectId"]), None)
            if r is None or r.title not in titles:
                continue
            target = model.one(r.title)
            ids = []
            for oid in eslide["elements"]:
                re_el = next((e for e in r.elements if e.id == oid), None)
                cands = [e for e in target.elements if re_el and e.descriptor() == re_el.descriptor()]
                best = min(cands, key=lambda e: max(abs(a - b) for a, b in zip(e.box, re_el.box)), default=None)
                ids.append(best.id if best else f"missing_{oid}")
            slides.append(dslide)
            emitted.append({"page": eslide["page"], "objectId": target.id, "elements": ids})
            save_thumbnail(api, presentation, target.id, folder / "fidelity" / f"slides-{dslide['page'] + 1:03}.png")
        (folder / "deck.json").write_text(json.dumps({**deck, "slides": slides}, ensure_ascii=False), encoding="utf-8")
        (folder / "emit.json").write_text(json.dumps({**state, "presentationId": presentation, "slides": emitted}),
                                          encoding="utf-8")
        try:
            reports[name] = alignment.measure(folder, refresh=True)
        except Exception as e:  # noqa: BLE001 (a missing object, say)
            return [f"alignment of the {name} deck failed: {e!r}"]
    fresh = {key: alignment.metrics(kind, row) for kind, key, row in alignment.items(reports["fresh"])}
    fresh_fail = {key for kind, key, _ in alignment.failures(reports["fresh"])}
    problems = []
    for kind, key, row in alignment.items(reports["synced"]):
        for msg in alignment.row_failures(kind, row):
            if key not in fresh_fail:
                problems.append(f"alignment {kind} {key}: {msg} (fresh conversion passes)")
        for metric, value in alignment.metrics(kind, row).items():
            old = fresh.get(key, {}).get(metric)
            if old is not None and value > old + growth:
                problems.append(f"alignment {kind} {key}: {metric} {value} vs {old} in the fresh conversion")
    return problems


# ---------------------------------------------------------------- command line

def presentation_id(deck: str) -> str:
    path = Path(deck)
    if (path / "emit.json").exists():
        return json.loads((path / "emit.json").read_text(encoding="utf-8"))["presentationId"]
    m = re.search(r"/presentation/d/([\w-]+)", deck)
    return m.group(1) if m else deck


def read(pid: str, slides_api=None) -> Model:
    from beamer2slides.google_auth import slides_service
    from beamer2slides.gslides import execute
    return Model(execute((slides_api or slides_service()).presentations().get(presentationId=pid)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck", help="presentation id, URL or converted out folder")
    ap.add_argument("checks", type=Path, help="JSON list of checks or of expectations (with checks)")
    ap.add_argument("--report", type=Path, help="sync-report.json: its conflicts are listed")
    args = ap.parse_args()
    items = json.loads(args.checks.read_text(encoding="utf-8-sig"))
    checks = [c for item in items for c in (item["checks"] if "checks" in item else [item])]
    model = read(presentation_id(args.deck))
    problems = check_all(model, checks) + integrity(model)
    if args.report:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        for c in section(report, "conflicts"):
            print("conflict:", json.dumps(c, ensure_ascii=False))
    print("\n".join(problems) or f"all {len(checks)} checks hold")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
