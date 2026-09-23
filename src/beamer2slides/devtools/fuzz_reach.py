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
from pathlib import Path

PRECONDITIONS = ("text_into_relaid", "hole_reworded", "moved_vs_reflow", "moved_overlapped",
                 "recreated_in_group", "table_grows")
RELAID = {"text", "size", "position"}
HOLE_ROLES = ("math", "figure", "inline")   # anchored pictures that sit in a hole (not number balls)
NBSP = chr(0xA0)   # no-break space: the runs a formula hole is typed with

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


def _units(report: dict):
    for a in report.get("actions") or []:
        for u in a.get("units") or []:
            yield a["slide"], u


def _overlap(a, b, margin: float = 0.5) -> bool:
    return a[0] < b[2] - margin and b[0] < a[2] - margin and a[1] < b[3] - margin and b[1] < a[3] - margin


def _first_change(a: str, b: str) -> int:
    n = min(len(a), len(b))
    return next((i for i in range(n) if a[i] != b[i]), n if len(a) != len(b) else -1)


def _tagged(read_slide: dict | None) -> dict[str, list]:
    """unit key -> boxes of the objects a read-back tags with it (`b2s:<slide>/<key>`)."""
    out = defaultdict(list)
    for rb in (read_slide or {}).get("objects", {}).values():
        t = rb.get("title") or ""
        if t.startswith("b2s:") and "/" in t and rb.get("box"):
            out[t.split("/", 1)[1]].append(rb["box"])
    return out


def step_reach(base: dict, before: dict, after: dict | None, report: dict) -> dict[str, list[dict]]:
    """precondition -> where it was reached ([{"slide", "unit"}]), for one sync step."""
    found: dict[str, list[dict]] = {p: [] for p in PRECONDITIONS}
    bslides = {s["key"]: s for s in base.get("slides") or []}
    live = {s["objectId"]: s for s in (before or {}).get("slides") or []}
    done = {s["objectId"]: s for s in (after or {}).get("slides") or []}
    per_slide = defaultdict(list)
    for skey, u in _units(report):
        per_slide[skey].append(u)
    user_slides = Counter(o.get("slide") for o in report.get("user_objects") or [])
    for skey, units in per_slide.items():
        b = bslides.get(skey)
        read = live.get((b or {}).get("objectId"))
        els = (b or {}).get("elements") or []
        groups_ours = set((b or {}).get("groups") or [])
        mine = {}   # the person's side of this slide: unit key -> boxes they moved
        for u in units:
            deck, source = set(u.get("deck") or []), set(u.get("source") or [])
            members = [e for e in els if e["key"] == u["key"] or e.get("anchor") == u["key"]]
            main = next((e for e in members if e["key"] == u["key"]), None)
            objs = [o for e in members for o in e.get("objects") or []]
            recreated = u["action"] == "recreate"
            if main and read:
                was = ((main.get("readback") or {}).get(main.get("main")) or {}).get("text") or ""
                now = ((read.get("objects") or {}).get(main.get("main")) or {}).get("text") or ""
            else:
                was = now = ""
            if recreated and "text" in deck and source & RELAID and u["key"].startswith("text/") \
                    and len(now.strip()) > len(was.strip()):
                found["text_into_relaid"].append({"slide": skey, "unit": u["key"]})
            holes = [e for e in members if e is not main and e["kind"] == "image" and e.get("role") in HOLE_ROLES]
            if recreated and "text" in deck and holes and NBSP in was:
                cut = _first_change(was, now)
                if 0 <= cut <= was.rfind(NBSP):
                    found["hole_reworded"].append({"slide": skey, "unit": u["key"]})
            parents = {((read or {}).get("objects", {}).get(o) or {}).get("parent_group") for o in objs} - {None}
            theirs_group = any(g not in groups_ours and not str(g).startswith("b2s_") for g in parents)
            if recreated and ("group" in deck or theirs_group or ("geometry" in deck and parents)):
                found["recreated_in_group"].append({"slide": skey, "unit": u["key"]})
            if "geometry" in deck and "deleted" not in deck:
                mine[u["key"]] = [((read or {}).get("objects", {}).get(o) or {}).get("box") for o in objs]
        # (a footer is re-laid whenever a slide comes or goes: the frame counter, not a reflow)
        relaid = [u for u in units if u["action"] in ("recreate", "create") and set(u.get("source") or []) & RELAID
                  and not u["key"].startswith("text/footer/")]
        person = bool(mine) or user_slides.get(skey)
        if person and any(u["key"] not in mine or len(mine) > 1 for u in relaid):
            found["moved_vs_reflow"].append({"slide": skey, "unit": relaid[0]["key"]} if relaid else {"slide": skey})
            got = done.get((b or {}).get("objectId"))
            tags = _tagged(got)
            theirs_boxes = [bx for k in mine for bx in tags.get(k, [])]
            ids = {o.get("objectId") for o in report.get("user_objects") or [] if o.get("slide") == skey}
            theirs_boxes += [rb["box"] for oid, rb in ((got or {}).get("objects") or {}).items() if oid in ids and rb.get("box")]
            if any(_overlap(x, y) for u in relaid if u["key"] not in mine for y in tags.get(u["key"], [])
                   for x in theirs_boxes if x):
                found["moved_overlapped"].append({"slide": skey})
        for u in units:
            # (recreated, or refilled in place - `sync.table_refill` keeps the table object)
            if u["key"].startswith("table/") and u["action"] != "delete" and set(u.get("source") or []) & RELAID and (
                    set(u.get("deck") or []) & {"text", "geometry"} or ("size" in u.get("source") and person)):
                found["table_grows"].append({"slide": skey, "unit": u["key"]})
    return found


def _slide_key(sel, base: dict, before: dict) -> str | None:
    """The base slide key an edit's slide selector named (a title, or an index into `before`)."""
    if isinstance(sel, str):
        sel = {"title": sel}
    if not isinstance(sel, dict):
        return None
    if "index" in sel:
        slides = (before or {}).get("slides") or []
        if 0 <= sel["index"] < len(slides):
            oid = slides[sel["index"]]["objectId"]
            return next((s["key"] for s in base["slides"] if s.get("objectId") == oid), None)
        return None
    title = " ".join((sel.get("title") or "").split())
    return next((s["key"] for s in base["slides"] if " ".join((s.get("title") or "").split()) == title), None)


def edit_label(spec: dict) -> str:
    """An edit's kind for counting: its `aim` when a focused generator drew it for one."""
    return f"{spec['edit']}:{spec['aim']}" if spec.get("aim") else spec["edit"]


def credit(edits: list[dict], reach: dict, base: dict, before: dict) -> list[tuple[str, set[str]]]:
    """(edit label, the preconditions it is credited with) per edit of the step."""
    out = []
    for spec in edits:
        args = spec.get("args") or {}
        skey = _slide_key(args.get("slide", args.get("after")), base, before)
        can = EDIT_FIELDS.get(spec["edit"], set())
        got = {p for p, where in reach.items() if skey and any(w["slide"] == skey for w in where) and can & NEEDS[p]}
        out.append((edit_label(spec), got))
    return out


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def archive_steps(root: Path):
    """(round folder, step record, reach, credit) for every step of every round under `root`."""
    for rj in sorted(root.glob("r*/round.json")):
        record = _load(rj) or {}
        for s in record.get("steps") or []:
            folder = rj.parent / f"step{s['step']}"
            base, before, report = (_load(folder / f"{n}.json") for n in ("base", "before", "report"))
            if not (base and before and report):
                continue
            after = _load(folder / "after.json")
            reach = step_reach(base, before, after, report)
            yield rj.parent, s, reach, credit(s.get("edits") or [], reach, base, before)


def summarise(rows) -> dict:
    """Counts for the tables: per variant and per edit label, how many steps / edits reached what."""
    steps = Counter()
    by_variant = defaultdict(Counter)
    variants = Counter()
    by_edit = defaultdict(Counter)
    edits = Counter()
    per_step_edits = []
    for _, s, reach, cred in rows:
        hit = {p for p, w in reach.items() if w}
        steps["steps"] += 1
        steps.update(hit)
        variants[s["variant"]] += 1
        by_variant[s["variant"]].update(hit)
        per_step_edits.append(len(cred))
        for label, got in cred:
            edits[label] += 1
            by_edit[label].update(got)
    return {"steps": dict(steps), "variants": dict(variants), "by_variant": {k: dict(v) for k, v in by_variant.items()},
            "edits": dict(edits), "by_edit": {k: dict(v) for k, v in by_edit.items()},
            "edits_per_step": sum(per_step_edits) / max(1, len(per_step_edits))}


def _rate(hits: int, n: int) -> str:
    if not n:
        return "-"
    return f"{hits / n:5.1%} ({1 / (hits / n):5.1f})" if hits else f"  0   (>{n})"


def table(summary: dict) -> str:
    """Rates and expected tries to the first hit (1/p, in brackets), per precondition."""
    cols = PRECONDITIONS
    head = f"{'':28}" + "".join(f"{c[:18]:>20}" for c in cols)
    lines = [f"{summary['steps'].get('steps', 0)} steps, {summary['edits_per_step']:.1f} edits per step",
             "per step (expected steps to the first hit)", head,
             f"{'all steps':28}" + "".join(f"{_rate(summary['steps'].get(c, 0), summary['steps'].get('steps', 0)):>20}"
                                           for c in cols)]
    for v, n in sorted(summary["variants"].items(), key=lambda kv: -kv[1]):
        lines.append(f"{v + f' ({n})':28}" + "".join(f"{_rate(summary['by_variant'][v].get(c, 0), n):>20}" for c in cols))
    lines += ["", "per edit (expected edits of that kind to the first hit)", head]
    for k, n in sorted(summary["edits"].items(), key=lambda kv: -kv[1]):
        lines.append(f"{k + f' ({n})':28}" + "".join(f"{_rate(summary['by_edit'][k].get(c, 0), n):>20}" for c in cols))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", type=Path, help="a live fuzz_sync --out folder (r*/round.json, r*/step*/...)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = summarise(archive_steps(args.archive))
    print(json.dumps(summary, indent=1) if args.json else table(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
