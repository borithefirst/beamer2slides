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
from collections.abc import Sequence
from difflib import SequenceMatcher
from pathlib import Path

from beamer2slides import adopt_sync, snapshot
from beamer2slides.devtools.adopt_bench import CORPUS, build_target
from beamer2slides.google_types import presentation
from beamer2slides.json_types import Json, JsonObject, JsonShapeError, as_array, as_object, as_objects, as_str
from beamer2slides.sync_model import SlideRead

Box = tuple[float, float, float, float]


def number(v: Json, where: str) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected, found {type(v).__name__}")


def bbox(e: JsonObject) -> Box:
    """An element's (or a deck object's) box, x0 y0 x1 y1."""
    x0, y0, x1, y1 = (number(v, "bbox") for v in as_array(e["bbox"], "bbox"))
    return x0, y0, x1, y1


def bands(held: Sequence[int], conv: Sequence[JsonObject]) -> list[list[int]]:
    """The held elements grouped into rows: a new band where an element starts below every element
    of the one before it."""
    out: list[list[int]] = []
    for i in sorted(held, key=lambda i: (round(bbox(conv[i])[1], 1), bbox(conv[i])[0])):
        box = bbox(conv[i])
        if out and box[1] < max(bbox(conv[j])[3] for j in out[-1]):
            out[-1].append(i)
        else:
            out.append([i])
    return [sorted(b, key=lambda i: bbox(conv[i])[0]) for b in out]


def columns(obj: JsonObject) -> list[tuple[float, float]]:
    xs: list[tuple[float, float]] = []
    x = bbox(obj)[0]
    for w in as_array(obj["col_widths"], "col_widths"):
        width = number(w, "col_widths")
        xs.append((x, x + width))
        x += width
    return xs


def alike(a: str, b: str) -> bool:
    a, b = " ".join(a.split()), " ".join(b.split())
    if a == b:
        return True
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.9


def rebuild(obj: JsonObject, held: Sequence[int],
            conv: Sequence[JsonObject]) -> tuple[list[list[str]] | None, str | None]:
    """(cells, why not) - the table the held elements make, or the first thing that stopped it."""
    rows = [[as_str(c, "a cell's words") for c in as_array(r, "a row")] for r in as_array(obj.get("rows") or [], "rows")]
    if not rows or not obj.get("col_widths"):
        return None, "no grid"
    if any(number(c.get("rowspan", 1), "rowspan") > 1 or number(c.get("colspan", 1), "colspan") > 1
           for c in as_objects(obj.get("table_cells") or [], "table_cells")):
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
            box = bbox(conv[i])
            mid = (box[0] + box[2]) / 2
            c = next((k for k, (x0, x1) in enumerate(cols) if x0 <= mid < x1), None)
            if c is None or c >= len(out[r]):
                return None, "a word stands outside every column"
            out[r][c] = (out[r][c] + " " + as_str(conv[i]["text"], "an element's text")).strip()
    for r, row in enumerate(rows):
        for c, said in enumerate(row):
            if not alike(out[r][c], said):
                return None, f"cell {r},{c}: {out[r][c][:30]!r} for {said[:30]!r}"
    return out, None


def slides_of(deck: JsonObject, where: str) -> list[JsonObject]:
    return as_objects(deck.get("slides") or [], f"{where}.slides")


def has_table(target: JsonObject) -> bool:
    return any(e["kind"] == "table"
               for s in slides_of(target, "target") for e in as_objects(s.get("elements") or [], "target elements"))


def look(name: str, tag: str, work: Path, tally: Counter[str], detail: bool) -> None:
    cache = CORPUS / name
    tex = cache / "runs" / tag / "tree" / "main.tex"
    if not tex.exists() or not (cache / "presentation.json").exists():
        return
    target = build_target(cache, None, None)
    if not has_table(target):
        return
    where = f"{name}/presentation.json"
    pres = presentation(as_object(json.loads((cache / "presentation.json").read_text(encoding="utf-8")), where), where)
    page_width = float(snapshot.page_size(pres)[0])
    folds = adopt_sync.deck_folds(target)
    made, err = adopt_sync.convert_source_of(tex, work / name, None, page_width, folds)
    if made is None:
        print(f"{name}: does not compile, skipped\n{err}")
        return
    conv_deck = made.deck
    problem = adopt_sync.labels_match(conv_deck, target)
    if problem:
        print(f"{name}: {problem}, skipped")
        return
    read = snapshot.read_presentation_of(pres)
    by_id: dict[str, SlideRead] = {s.object_id: s for s in read.slides}
    for conv_slide, tgt in zip(slides_of(conv_deck, "conversion"), slides_of(target, "target")):
        tgt_id = tgt.get("objectId")
        live = by_id.get(tgt_id) if isinstance(tgt_id, str) else None
        objs = [e for e in adopt_sync.deck_objects(tgt) if live and e["object"] in live.objects]
        conv = as_objects(conv_slide["elements"], "conversion elements")
        pairs, _why = adopt_sync.pair_elements(conv, objs)
        tied = set(pairs.values())
        for k, obj in enumerate(objs):
            if obj["kind"] != "table":
                continue
            tally["deck tables"] += 1
            if k in tied:
                tally["paired"] += 1
                continue
            held = [i for i, e in enumerate(conv) if adopt_sync._holds(bbox(e), bbox(obj))]
            kinds = sorted({as_str(conv[i]["kind"], "an element's kind") for i in held})
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
    tally: Counter[str] = Counter()
    work = Path("out") / "probe-deck-tables"
    for name in names:
        look(name, tag, work, tally, detail)
    print()
    for k, v in tally.most_common():
        print(f"  {k:44} {v}")


if __name__ == "__main__":
    main(sys.argv[1:])
