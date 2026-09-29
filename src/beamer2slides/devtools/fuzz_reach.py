"""What a live sync-fuzz step *reached*: the preconditions of the layout defects, read from the step.

A layout defect of a synced deck needs both sides on one slide: the person changed something there
and the source re-laid something there, and only then can the result overlap, overflow or strand.
Whether the sync then got it right is the layout oracle's question (devtools/layout_oracle.py); this
one is cheaper and comes first - did the campaign even set the stage? A campaign that sets it once
in 200 steps proves little about layout however long it runs, and the counts here are how one sees
that, and how one sees a change of the generator move it.

Each precondition is judged from the files every live step already writes (base, before, after,
report, edits), so it runs over an old archive as well as over a new round:

  text_into_relaid     a text unit the source re-laid (text/size/position) is recreated, and the
                       person's own wording that goes into it is longer than the base's: their
                       words in a box sized for the source's.
  hole_reworded        a text unit with an inline picture (formula hole) is recreated with the
                       person's text, and the person changed words *before* a hole: what places the
                       picture moved.
  moved_vs_reflow      a slide where the person moved/resized an element or added one of their own,
                       and the source re-laid another unit (text/size/position).
  moved_overlapped     ... and after the sync the person's box and the re-laid unit's box overlap.
  recreated_in_group   a unit is recreated while it sits in a group the person made, or in a group
                       (converter's or theirs) the person moved or resized.
  table_grows          a table the source changed, on a slide where the person edited that table or
                       has something of their own (moved, added) the table can grow into.

  python tools/fuzz_reach.py <archive> [--json]    per edit kind, per variant, per precondition
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from beamer2slides.json_types import Json, JsonObject, as_str

PRECONDITIONS = ("text_into_relaid", "hole_reworded", "moved_vs_reflow", "moved_overlapped",
                 "recreated_in_group", "table_grows")
RELAID = {"text", "size", "position"}
HOLE_ROLES = ("math", "figure", "inline")   # anchored pictures that sit in a hole (not number balls)
NBSP = chr(0xA0)   # no-break space: the runs a formula hole is typed with
OVERLAP_MARGIN = 0.5   # pt two boxes must share before they count as overlapping

# Which deck-side field an edit kind can make, so a precondition that needs the person's text (or
# geometry, or grouping) is credited to the edits that can have made it and not to their neighbours.
EDIT_FIELDS = {
    "replace_word": {"text"}, "append_sentence": {"text"}, "delete_paragraph": {"text"},
    "add_paragraph": {"text"}, "insert_before_hole": {"text"},
    "insert_table_row": {"text"}, "insert_table_column": {"text"},
    "bold": {"text_style"}, "recolour": {"text_style"}, "resize_font": {"text_style"},
    "move": {"geometry"}, "resize": {"geometry"}, "delete_element": {"deleted"}, "delete_group": {"deleted"},
    "add_text_box": {"user"}, "add_shape": {"user"}, "add_image": {"user"}, "duplicate": {"user"},
    "group": {"group"}, "ungroup": {"group"},
}
NEEDS = {"text_into_relaid": {"text"}, "hole_reworded": {"text"},
         "moved_vs_reflow": {"geometry", "user"}, "moved_overlapped": {"geometry", "user"},
         "recreated_in_group": {"group", "geometry"}, "table_grows": {"text", "geometry", "user"}}

Box = tuple[float, ...]


@dataclass(frozen=True, kw_only=True)
class Reached:
    """Where a step reached a precondition: the slide, and the unit when one unit did."""
    slide: str
    unit: str | None


def reached_json(r: Reached) -> JsonObject:
    """As round.json has always said it: {"slide", "unit"}, or {"slide"} alone."""
    return {"slide": r.slide} if r.unit is None else {"slide": r.slide, "unit": r.unit}


Reach = dict[str, list[Reached]]


# An archive is read leniently: a step may lack a file or a part of one, and reads as having none.

def _obj(v: Json) -> JsonObject:
    return v if isinstance(v, dict) else {}


def _objs(v: Json) -> list[JsonObject]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _strs(v: Json) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _text(v: Json) -> str:
    return v if isinstance(v, str) else ""


def _box(v: Json) -> Box | None:
    return tuple(float(x) for x in v if isinstance(x, (int, float))) if isinstance(v, list) else None


def _units(report: JsonObject) -> Iterator[tuple[str, JsonObject]]:
    for a in _objs(report.get("actions")):
        for u in _objs(a.get("units")):
            yield as_str(a.get("slide"), "report.actions[].slide"), u


def _overlap(a: Box, b: Box) -> bool:
    m = OVERLAP_MARGIN
    return a[0] < b[2] - m and b[0] < a[2] - m and a[1] < b[3] - m and b[1] < a[3] - m


def _first_change(a: str, b: str) -> int:
    n = min(len(a), len(b))
    return next((i for i in range(n) if a[i] != b[i]), n if len(a) != len(b) else -1)


def _tagged(read_slide: JsonObject | None) -> dict[str, list[Box]]:
    """unit key -> boxes of the objects a read-back tags with it (`b2s:<slide>/<key>`)."""
    out: defaultdict[str, list[Box]] = defaultdict(list)
    for v in _obj(_obj(read_slide).get("objects")).values():
        rb = _obj(v)
        t = _text(rb.get("title"))
        box = _box(rb.get("box"))
        if t.startswith("b2s:") and "/" in t and box:
            out[t.split("/", 1)[1]].append(box)
    return out


def step_reach(base: JsonObject, before: JsonObject | None, after: JsonObject | None, report: JsonObject) -> Reach:
    """precondition -> where it was reached, for one sync step."""
    found: Reach = {p: [] for p in PRECONDITIONS}
    bslides = {_text(s.get("key")): s for s in _objs(base.get("slides"))}
    live = {_text(s.get("objectId")): s for s in _objs(_obj(before).get("slides"))}
    done = {_text(s.get("objectId")): s for s in _objs(_obj(after).get("slides"))}
    per_slide: defaultdict[str, list[JsonObject]] = defaultdict(list)
    for skey, u in _units(report):
        per_slide[skey].append(u)
    user_slides = Counter(s for o in _objs(report.get("user_objects")) if isinstance(s := o.get("slide"), str))
    for skey, units in per_slide.items():
        b = bslides.get(skey)
        sid = _obj(b).get("objectId")
        read = live.get(sid) if isinstance(sid, str) else None
        objects = _obj(_obj(read).get("objects"))
        els = _objs(_obj(b).get("elements"))
        groups_ours = set(_strs(_obj(b).get("groups")))
        mine: dict[str, list[Box | None]] = {}   # the person's side of this slide: unit key -> boxes they moved
        for u in units:
            key = as_str(u.get("key"), "report unit key")
            deck, source = set(_strs(u.get("deck"))), set(_strs(u.get("source")))
            members = [e for e in els if e.get("key") == key or e.get("anchor") == key]
            main = next((e for e in members if e.get("key") == key), None)
            objs = [o for e in members for o in _strs(e.get("objects"))]
            recreated = u.get("action") == "recreate"
            if main and read:
                mid = _text(main.get("main"))
                was = _text(_obj(_obj(main.get("readback")).get(mid)).get("text"))
                now = _text(_obj(objects.get(mid)).get("text"))
            else:
                was = now = ""
            if recreated and "text" in deck and source & RELAID and key.startswith("text/") \
                    and len(now.strip()) > len(was.strip()):
                found["text_into_relaid"].append(Reached(slide=skey, unit=key))
            holes = [e for e in members if e is not main and e.get("kind") == "image" and e.get("role") in HOLE_ROLES]
            if recreated and "text" in deck and holes and NBSP in was:
                cut = _first_change(was, now)
                if 0 <= cut <= was.rfind(NBSP):
                    found["hole_reworded"].append(Reached(slide=skey, unit=key))
            parents = {p for o in objs if isinstance(p := _obj(objects.get(o)).get("parent_group"), str)}
            theirs_group = any(g not in groups_ours and not g.startswith("b2s_") for g in parents)
            if recreated and ("group" in deck or theirs_group or ("geometry" in deck and parents)):
                found["recreated_in_group"].append(Reached(slide=skey, unit=key))
            if "geometry" in deck and "deleted" not in deck:
                mine[key] = [_box(_obj(objects.get(o)).get("box")) for o in objs]
        # (a footer is re-laid whenever a slide comes or goes: the frame counter, not a reflow)
        relaid = [(as_str(u.get("key"), "report unit key"), u) for u in units
                  if u.get("action") in ("recreate", "create") and set(_strs(u.get("source"))) & RELAID
                  and not as_str(u.get("key"), "report unit key").startswith("text/footer/")]
        person = bool(mine) or bool(user_slides.get(skey))
        if person and any(k not in mine or len(mine) > 1 for k, _ in relaid):
            found["moved_vs_reflow"].append(Reached(slide=skey, unit=relaid[0][0] if relaid else None))
            got_id = _obj(b).get("objectId")
            got = done.get(got_id) if isinstance(got_id, str) else None
            tags = _tagged(got)
            theirs_boxes: list[Box | None] = [bx for k in mine for bx in tags.get(k, [])]
            ids = {o.get("objectId") for o in _objs(report.get("user_objects")) if o.get("slide") == skey}
            theirs_boxes += [bx for oid, rb in _obj(_obj(got).get("objects")).items()
                             if oid in ids and (bx := _box(_obj(rb).get("box")))]
            if any(_overlap(x, y) for k, _ in relaid if k not in mine for y in tags.get(k, [])
                   for x in theirs_boxes if x):
                found["moved_overlapped"].append(Reached(slide=skey, unit=None))
        for u in units:
            # (recreated, or refilled in place - `sync.table_refill` keeps the table object)
            key = as_str(u.get("key"), "report unit key")
            source = set(_strs(u.get("source")))
            if key.startswith("table/") and u.get("action") != "delete" and source & RELAID and (
                    set(_strs(u.get("deck"))) & {"text", "geometry"} or ("size" in source and person)):
                found["table_grows"].append(Reached(slide=skey, unit=key))
    return found


def reach_json(reach: Reach) -> JsonObject:
    """The preconditions reached, as a step record in round.json says them."""
    out: JsonObject = {}
    for p, where in reach.items():
        if where:
            out[p] = [reached_json(w) for w in where]
    return out


def _slide_key(sel: Json, base: JsonObject, before: JsonObject | None) -> str | None:
    """The base slide key an edit's slide selector named (a title, or an index into `before`)."""
    if isinstance(sel, str):
        sel = {"title": sel}
    if not isinstance(sel, dict):
        return None
    slides_of_base = _objs(base.get("slides"))
    if "index" in sel:
        slides = _objs(_obj(before).get("slides"))
        index = sel["index"]
        if isinstance(index, int) and 0 <= index < len(slides):
            oid = slides[index].get("objectId")
            return next((_text(s.get("key")) for s in slides_of_base if s.get("objectId") == oid), None)
        return None
    title = " ".join(_text(sel.get("title")).split())
    return next((_text(s.get("key")) for s in slides_of_base if " ".join(_text(s.get("title")).split()) == title), None)


def edit_label(spec: JsonObject) -> str:
    """An edit's kind for counting: its `aim` when a focused generator drew it for one."""
    edit, aim = as_str(spec.get("edit"), "edit"), spec.get("aim")
    return f"{edit}:{aim}" if aim else edit


Credit = list[tuple[str, set[str]]]


def credit(edits: Sequence[JsonObject], reach: Reach, base: JsonObject, before: JsonObject | None) -> Credit:
    """(edit label, the preconditions it is credited with) per edit of the step."""
    out: Credit = []
    for spec in edits:
        args = _obj(spec.get("args"))
        skey = _slide_key(args.get("slide", args.get("after")), base, before)
        can = EDIT_FIELDS.get(as_str(spec.get("edit"), "edit"), set())
        got = {p for p, where in reach.items() if skey and any(w.slide == skey for w in where) and can & NEEDS[p]}
        out.append((edit_label(spec), got))
    return out


def _load(path: Path) -> JsonObject | None:
    try:
        loaded: Json = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


Row = tuple[Path, JsonObject, Reach, Credit]


def archive_steps(root: Path) -> Iterator[Row]:
    """(round folder, step record, reach, credit) for every step of every round under `root`."""
    for rj in sorted(root.glob("r*/round.json")):
        record = _load(rj) or {}
        for s in _objs(record.get("steps")):
            folder = rj.parent / f"step{s['step']}"
            base, before, report = (_load(folder / f"{n}.json") for n in ("base", "before", "report"))
            if not (base and before and report):
                continue
            after = _load(folder / "after.json")
            reach = step_reach(base, before, after, report)
            yield rj.parent, s, reach, credit(_objs(s.get("edits")), reach, base, before)


@dataclass(frozen=True, kw_only=True)
class Summary:
    """Counts for the tables: per variant and per edit label, how many steps / edits reached what."""
    steps: dict[str, int]
    variants: dict[str, int]
    by_variant: dict[str, dict[str, int]]
    edits: dict[str, int]
    by_edit: dict[str, dict[str, int]]
    edits_per_step: float


def _counts_json(c: Mapping[str, int]) -> JsonObject:
    out: JsonObject = {}
    for k, v in c.items():
        out[k] = v
    return out


def summary_json(s: Summary) -> JsonObject:
    """In the order `--json` has always printed it."""
    return {"steps": _counts_json(s.steps), "variants": _counts_json(s.variants),
            "by_variant": {k: _counts_json(v) for k, v in s.by_variant.items()},
            "edits": _counts_json(s.edits), "by_edit": {k: _counts_json(v) for k, v in s.by_edit.items()},
            "edits_per_step": s.edits_per_step}


def summarise(rows: Iterable[Row]) -> Summary:
    steps: Counter[str] = Counter()
    by_variant: defaultdict[str, Counter[str]] = defaultdict(Counter)
    variants: Counter[str] = Counter()
    by_edit: defaultdict[str, Counter[str]] = defaultdict(Counter)
    edits: Counter[str] = Counter()
    per_step_edits: list[int] = []
    for _, s, reach, cred in rows:
        hit = {p for p, w in reach.items() if w}
        steps["steps"] += 1
        steps.update(hit)
        variant = as_str(s.get("variant"), "step variant")
        variants[variant] += 1
        by_variant[variant].update(hit)
        per_step_edits.append(len(cred))
        for label, got in cred:
            edits[label] += 1
            by_edit[label].update(got)
    return Summary(steps=dict(steps), variants=dict(variants), by_variant={k: dict(v) for k, v in by_variant.items()},
                   edits=dict(edits), by_edit={k: dict(v) for k, v in by_edit.items()},
                   edits_per_step=sum(per_step_edits) / max(1, len(per_step_edits)))


def _rate(hits: int, n: int) -> str:
    if not n:
        return "-"
    return f"{hits / n:5.1%} ({1 / (hits / n):5.1f})" if hits else f"  0   (>{n})"


def table(summary: Summary) -> str:
    """Rates and expected tries to the first hit (1/p, in brackets), per precondition."""
    cols = PRECONDITIONS
    head = f"{'':28}" + "".join(f"{c[:18]:>20}" for c in cols)
    lines = [f"{summary.steps.get('steps', 0)} steps, {summary.edits_per_step:.1f} edits per step",
             "per step (expected steps to the first hit)", head,
             f"{'all steps':28}" + "".join(f"{_rate(summary.steps.get(c, 0), summary.steps.get('steps', 0)):>20}"
                                           for c in cols)]
    for v, n in sorted(summary.variants.items(), key=lambda kv: -kv[1]):
        lines.append(f"{v + f' ({n})':28}" + "".join(f"{_rate(summary.by_variant[v].get(c, 0), n):>20}" for c in cols))
    lines += ["", "per edit (expected edits of that kind to the first hit)", head]
    for k, n in sorted(summary.edits.items(), key=lambda kv: -kv[1]):
        lines.append(f"{k + f' ({n})':28}" + "".join(f"{_rate(summary.by_edit[k].get(c, 0), n):>20}" for c in cols))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", type=Path, help="a live fuzz_sync --out folder (r*/round.json, r*/step*/...)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    archive: Path = args.archive
    summary = summarise(archive_steps(archive))
    print(json.dumps(summary_json(summary), indent=1) if args.json else table(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
