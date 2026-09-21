"""Probe: a table of somebody's deck, read back by the converter - can its cells be found again?

`adopt` writes a person's table as a tikz grid (`adopt.table_block`) and the converter then reads
the compiled page back the way it reads any page. This asks what happens to the tables of the adopt
corpus, in two steps:

  read back   for every table object of the deck, how many converted elements stand inside it and
              whether one of them is a `table` - and whether the object pairs at all
  rebuilt     given the scatter, can the words be put back into their cells *and proved right*?
              The deck knows its own grid (`deck_ir.table_element` reads `rows`, `col_widths`,
              `row_heights`), so each cell of the reconstruction is checked against what that cell
              of the deck says. Anything short of every cell is a refusal, with the reason.

Findings (docs/sync.md, "A table of somebody's deck, read back as a scatter"): of 42 tables, 9 pair,
1 comes back as a `table` element and 32 as loose cell texts plus the thin rule images between them;
**none of the 42 rebuilds correctly**. Merged cells, empty cells, a wrapped cell and a number
right-aligned under a left-aligned heading each shift a column or a row by one, and a grid wrong by
one writes one cell's words into the cell beside it. So a cell of the deck's own table is *named*
(`adopt_sync.inside_tables`, `merge.plan_unit`'s `in_table`), not rebuilt.

No Google call and no Drive write: it compiles the source tree a benchmark run left in the corpus
cache (`$B2S_ADOPT_CORPUS`, else `out/adopt-corpus`) and reads the deck out of the cached
`presentation.json`, building the target with the current reader rather than trusting the cached
`target.json`. One compile per deck, so it is slow.

Usage: python tools/probe_deck_tables.py [deck ...] [--tag T] [--detail]
"""

import json
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from beamer2slides import adopt_sync, snapshot
from beamer2slides.devtools.adopt_bench import CORPUS, build_target


def bands(held, conv):
    """The held elements grouped into rows: a new band where an element starts below every element
    of the one before it."""
    out = []
    for i in sorted(held, key=lambda i: (round(conv[i]["bbox"][1], 1), conv[i]["bbox"][0])):
        box = conv[i]["bbox"]
        if out and box[1] < max(conv[j]["bbox"][3] for j in out[-1]):
            out[-1].append(i)
        else:
            out.append([i])
    return [sorted(b, key=lambda i: conv[i]["bbox"][0]) for b in out]


def columns(obj):
    xs, x = [], obj["bbox"][0]
    for w in obj["col_widths"]:
        xs.append((x, x + w))
        x += w
    return xs


def alike(a: str, b: str) -> bool:
    a, b = " ".join(a.split()), " ".join(b.split())
    if a == b:
        return True
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.9


def rebuild(obj, held, conv):
    """(cells, why not) - the table the held elements make, or the first thing that stopped it."""
    rows = obj.get("rows") or []
    if not rows or not obj.get("col_widths"):
        return None, "no grid"
    if any(c.get("rowspan", 1) > 1 or c.get("colspan", 1) > 1 for c in obj.get("table_cells") or []):
        return None, "merged cells"
    if any(conv[i]["kind"] != "text" for i in held):
        return None, "something in it is not words"
    lines = bands(held, conv)
    full = [r for r in range(len(rows)) if any(c.strip() for c in rows[r])]
    if len(lines) != len(full):
        return None, f"{len(lines)} bands for {len(full)} rows with words"
    cols = columns(obj)
    out = [["" for _ in rows[r]] for r in range(len(rows))]
    for r, band in zip(full, lines):
        for i in band:
            box = conv[i]["bbox"]
            mid = (box[0] + box[2]) / 2
            c = next((k for k, (x0, x1) in enumerate(cols) if x0 <= mid < x1), None)
            if c is None or c >= len(out[r]):
                return None, "a word stands outside every column"
            out[r][c] = (out[r][c] + " " + conv[i]["text"]).strip()
    for r, row in enumerate(rows):
        for c, said in enumerate(row):
            if not alike(out[r][c], said):
                return None, f"cell {r},{c}: {out[r][c][:30]!r} for {said[:30]!r}"
    return out, None


def has_table(target: dict) -> bool:
    return any(e["kind"] == "table"
               for s in target.get("slides") or [] for e in s.get("elements") or [])


def look(name: str, tag: str, work: Path, tally: Counter, detail: bool) -> None:
    cache = CORPUS / name
    tex = cache / "runs" / tag / "tree" / "main.tex"
    if not tex.exists() or not (cache / "presentation.json").exists():
        return
    target = build_target(cache)
    if not has_table(target):
        return
    pres = json.loads((cache / "presentation.json").read_text(encoding="utf-8"))
    page_width = float(snapshot.page_size(pres)[0])
    folds = adopt_sync.deck_folds(target)
    made, err = adopt_sync.convert_source(tex, work / name, page_width=page_width, folds=folds)
    if made is None:
        print(f"{name}: does not compile, skipped\n{err}")
        return
    conv_deck = made["deck"]
    problem = adopt_sync.labels_match(conv_deck, target)
    if problem:
        print(f"{name}: {problem}, skipped")
        return
    read = snapshot.read_presentation(pres)
    by_id = {s["objectId"]: s for s in read["slides"]}
    for conv_slide, tgt in zip(conv_deck["slides"], target["slides"]):
        live = by_id.get(tgt.get("objectId"))
        objs = [e for e in adopt_sync.deck_objects(tgt) if live and e["object"] in live["objects"]]
        conv = conv_slide["elements"]
        pairs, _why = adopt_sync.pair_elements(conv, objs)
        tied = set(pairs.values())
        for k, obj in enumerate(objs):
            if obj["kind"] != "table":
                continue
            tally["deck tables"] += 1
            if k in tied:
                tally["paired"] += 1
                continue
            held = [i for i, e in enumerate(conv) if adopt_sync._holds(e["bbox"], obj["bbox"])]
            kinds = sorted({conv[i]["kind"] for i in held})
            tally[f"read back as {'a table' if 'table' in kinds else 'no table'}"] += 1
            cells, why = rebuild(obj, held, conv)
            tally["rebuilt" if cells else "refused"] += 1
            tally[f"  {why.split(':')[0]}" if why else "  ok"] += 1
            if detail:
                print(f"{name:18} {len(held):3} elements ({'+'.join(kinds) or '-':22}) {why}")


def main(argv: list[str]) -> None:
    detail = "--detail" in argv
    argv = [a for a in argv if a != "--detail"]
    tag = "sh-a"
    if "--tag" in argv:
        i = argv.index("--tag")
        tag, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    names = argv or sorted(p.name for p in CORPUS.iterdir()
                           if p.is_dir() and (p / "presentation.json").exists())
    tally: Counter = Counter()
    work = Path("out") / "probe-deck-tables"
    for name in names:
        look(name, tag, work, tally, detail)
    print()
    for k, v in tally.most_common():
        print(f"  {k:44} {v}")


if __name__ == "__main__":
    main(sys.argv[1:])
