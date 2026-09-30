"""Source versions of the sync test talk (talk.tex) and what each changes.

A variant is a set of flags; `render` resolves talk.tex's guards into a plain .tex (what an AI
author's edited file looks like) and `compile_tex` builds it with SyncTeX. `CHECKS` says what
a synced deck must show for each flag (tools/sync_check.py format), `titles` the slide titles
in order.

Usage: python tests/decks/sync/build.py [variant ...]    (default: all) -> out/<variant>.pdf
"""

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from beamer2slides.json_types import Json, JsonObject, as_array, as_int, as_object, as_objects, as_str
from beamer2slides.raw_types import RawPage, parse_raw

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
MASTER = HERE / "talk.tex"
RUNS = 2  # pdflatex passes (`compile_tex`)

FLAGS = {
    "reword": "reword a bullet",
    "addbullet": "add a bullet",
    "removebullet": "remove a nested bullet",
    "formula": "change the inline formula",
    "figure": "change the TikZ figure (curve and colour)",
    "addframe": "add a frame (with a TikZ picture) after Results",
    "deleteframe": "delete the diagram frame",
    "reorder": "swap the block and table frames",
    "retitle": "rename a labelled frame's title",
    "blockedit": "edit the block title and body",
    "tablecell": "change a table cell",
    "tablemove": "add a line above the table (which moves it)",
    "tablerow": "add a row at the end of the table",
    "notes": "change the speaker notes of two frames",
    "numbers": "insert a numbered step and reword the last one",
    "untitled": "rename the unlabelled frame's title",
    # layout probes (tests/test_sync_live.py, the `layout-*` scenarios): three frames at the end,
    # and the source changes made to them (which only mean something together with `probes`)
    "probes": "add the layout probe frames (Room to grow, Two boxes, Display math)",
    "probereword": "reword the paragraph of Room to grow",
    "probepush": "push each probe frame's content down (a line added on top; Room to grow: vertical space)",
    "retheme": "a different frame title bar (larger title font, another colour)",
    "subtitle": "add a long line under the title page, which takes the subtitle placeholder from the authors",
}
PROBE_EDITS = ("probereword", "probepush")  # (no variant of their own: they edit the probe frames)
MIXED = ["reword", "addbullet", "formula", "figure", "addframe", "reorder", "tablecell", "notes"]
VARIANTS = {"v1": [], **{f: [f] for f in FLAGS if f not in PROBE_EDITS}, "mixed": MIXED,
            "chain": MIXED + ["blockedit", "deleteframe", "untitled", "numbers", "retitle", "removebullet"],
            # sources of the scenarios in tests/test_sync_live.py
            "disjoint": ["tablecell", "blockedit", "figure", "notes", "numbers"],
            "same-element": ["reword", "blockedit", "figure", "formula", "tablecell"],
            "conflict": ["reword", "blockedit"],
            "deletions": ["deleteframe", "tablecell", "removebullet"],
            "slides": ["addframe", "reorder", "untitled"],
            "converged": ["reword", "tablecell"],
            "table-moved": ["tablemove", "tablecell"],
            "table-row": ["tablerow", "tablecell"],
            "many-edits": ["retitle", "reword", "addbullet", "removebullet", "notes"],
            "groups": ["figure", "blockedit", "numbers"],
            # the layout probes: `probes` is their base, converted first
            "probes-reword": ["probes", "probereword"],
            "probes-push": ["probes", "probereword", "probepush"]}

MOTIVATION: JsonObject = {"contains": "Later the source changes again"}  # (no scenario edits these words)
TITLE_PAGE: JsonObject = {"title": "Keeping Slides and Source in Sync"}
CHECKS: dict[str, list[JsonObject]] = {
    "reword": [{"check": "text", "slide": MOTIVATION, "text": "by an AI assistant and converted once", "count": 1},
               {"check": "text", "slide": MOTIVATION, "text": "by an author and converted once", "count": 0}],
    "addbullet": [{"check": "text", "slide": MOTIVATION, "text": "Converting again would throw away every manual edit",
                   "count": 1}],
    "removebullet": [{"check": "text", "slide": MOTIVATION, "text": "moving pictures around", "count": 0}],
    "formula": [{"check": "fresh", "slide": {"title": "Merging text"}}],
    "figure": [{"check": "image", "slide": {"title": "Convergence"}, "colour": "#cc0000", "count": 1},
               {"check": "fresh", "slide": {"title": "Convergence"}}],
    "addframe": [{"check": "text", "slide": {"title": "Pulling edits back"},
                  "text": "Plain wording edits are patched into the source", "count": 1},
                 {"check": "image", "slide": {"title": "Pulling edits back"}, "count": 1},
                 {"check": "fresh", "slide": {"title": "Pulling edits back"}}],
    "deleteframe": [{"check": "slide_count", "slide": {"title": "Three versions"}, "count": 0},
                    {"check": "text", "slide": None, "text": "Merged", "count": 0}],
    "reorder": [],
    "retitle": [{"check": "title", "slide": MOTIVATION, "text": "Why decks drift away from their source"}],
    "blockedit": [{"check": "text", "slide": {"title": "Merge policy"}, "text": "Deck edits come first", "count": 1},
                  {"check": "text", "slide": {"title": "Merge policy"}, "text": "Deck edits win", "count": 0},
                  {"check": "text", "slide": {"title": "Merge policy"}, "text": "kept aside and listed as a conflict",
                   "count": 1}],
    "tablecell": [{"check": "text", "slide": {"title": "Results"}, "text": "4.7 s", "count": 1},
                  {"check": "text", "slide": {"title": "Results"}, "text": "3.9 s", "count": 0}],
    "tablemove": [{"check": "text", "slide": {"title": "Results"}, "text": "Measured on the sync test talk.", "count": 1}],
    "tablerow": [{"check": "text", "slide": {"title": "Results"}, "text": "4.2 s", "count": 1}],
    "notes": [{"check": "notes", "slide": MOTIVATION, "text": "Start with a show of hands: who edited a converted deck?"},
              {"check": "notes", "slide": {"title": "Finding the same slide"},
               "text": "Labels are the stable key. Titles and page numbers are only fallbacks."}],
    "numbers": [{"check": "text", "slide": {"title": "The sync algorithm"}, "text": "Match slides by their frame labels",
                 "count": 1},
                {"check": "text", "slide": {"title": "The sync algorithm"}, "text": "Merge and write the changes", "count": 0},
                {"check": "fresh", "slide": {"title": "The sync algorithm"}}],
    "untitled": [{"check": "slide_count", "slide": {"title": "Takeaways"}, "count": 1},
                 {"check": "slide_count", "slide": {"title": "Conclusions"}, "count": 0}],
    "probes": [{"check": "slide_count", "slide": {"title": t}, "count": 1}
               for t in ("Room to grow", "Two boxes", "Display math")],
    "probereword": [{"check": "text", "slide": {"title": "Room to grow"}, "text": "The person types a longer version",
                     "count": 1}],
    "probepush": [{"check": "text", "slide": {"title": "Two boxes"}, "text": "The source adds this line above both boxes.",
                   "count": 1}],
    "retheme": [],
    "subtitle": [{"check": "text", "slide": TITLE_PAGE, "text": "how one sync reconciles all three", "count": 1},
                 {"check": "text", "slide": TITLE_PAGE, "text": "University of Examples", "count": 1}],
}


WHY, ALGO = "Why decks and sources diverge", "The sync algorithm"
INTENDED: dict[str, list[str]] = {  # classification_diff(v1, variant) items per flag
    "reword": [f"{WHY}: text- The source is written in by an author and converted once",
               f"{WHY}: text+ The source is written in by an AI assistant and converted once"],
    "addbullet": [f"{WHY}: text+ Converting again would throw away every manual edit"],
    "removebullet": [f"{WHY}: text- moving pictures around"],
    "formula": ["Merging text: pictures 2 -> 2 changed"],
    "figure": ["Convergence: pictures 1 -> 1 changed"],
    "addframe": ["slide+ Pulling edits back"],
    "deleteframe": ["slide- Three versions"],
    "reorder": [],  # the order item, from titles()
    "retitle": [f"{WHY}: title -> Why decks drift away from their source"],
    "blockedit": ["Merge policy: text- Deck edits win",
                  "Merge policy: text- A source change to a field the deck also edited is reported as a conflict.",
                  "Merge policy: text+ Deck edits come first",
                  "Merge policy: text+ A source change to a field the deck also edited is kept aside and listed as a conflict."],
    "tablecell": ["Results: text- 3.9 s", "Results: text+ 4.7 s"],
    "tablemove": ["Results: text+ Measured on the sync test talk."],
    "tablerow": ["Results: text+ Deletions", "Results: text+ 4.2 s"],
    "notes": [f"{WHY}: notes -> Start with a show of hands: who edited a converted deck?",
              "Finding the same slide: notes -> Labels are the stable key. Titles and page numbers are only fallbacks."],
    "numbers": [f"{ALGO}: text- Merge and write the changes", f"{ALGO}: text+ Match slides by their frame labels",
                f"{ALGO}: text+ Merge the changes and write them back", f"{ALGO}: pictures 4 -> 5 changed"],
    "untitled": ["Conclusions: title -> Takeaways"],
    "probes": ["slide+ Room to grow", "slide+ Two boxes", "slide+ Display math"],
    # (against v1 the probe frames are simply added: what the probe edits change is in PROBE_INTENDED,
    #  against the `probes` variant)
    "probereword": [],
    "probepush": [],
    "retheme": [],
    "subtitle": ["Keeping Slides and Source in Sync: text+ Slides, source and base: what each one says, and how one "
                 "sync reconciles all three"],
}
PROBE_INTENDED = {  # classification_diff(probes, variant) items per probe edit
    "probereword": ["Room to grow: text- The person writes a longer version of this paragraph than the converter "
                    "made room for, so its box has to grow downwards.",
                    "Room to grow: text+ The person types a longer version of this paragraph than the converter "
                    "made room for, so its box has to grow downwards."],
    "probepush": ["Two boxes: text+ The source adds this line above both boxes.",
                  "Display math: text+ The source adds this line above the equation."],
}


def intended_diff(flags: list[str]) -> set[str]:
    """The classification differences from v1 this variant is meant to make."""
    out = {d for f in flags for d in INTENDED[f]}
    if "reorder" in flags:
        out.add("order " + " / ".join(t for t in titles(flags) if t != "Pulling edits back"))
    return out


def checks(flags: list[str]) -> list[JsonObject]:
    """What a deck synced to this source must show (source side only)."""
    return [c for f in flags for c in CHECKS[f]]


def titles(flags: list[str]) -> list[str]:
    """Slide titles of the variant in order."""
    order = ["Keeping Slides and Source in Sync",
             "Why decks drift away from their source" if "retitle" in flags else "Why decks and sources diverge",
             "The sync algorithm", "Merging text", "Convergence"]
    order += ["Results", "Merge policy"] if "reorder" in flags else ["Merge policy", "Results"]
    order += ["Pulling edits back"] * ("addframe" in flags) + ["Three versions"] * ("deleteframe" not in flags)
    order += ["Finding the same slide", "Takeaways" if "untitled" in flags else "Conclusions"]
    return order + ["Room to grow", "Two boxes", "Display math"] * ("probes" in flags)


@dataclass(frozen=True, kw_only=True)
class Summary:
    """What a classified folder says of one slide (`summary`): its title, its texts (paragraphs,
    table cells, diagram labels, whitespace collapsed), its pictures (role and a content hash) and
    its speaker notes."""
    title: str | None
    texts: tuple[str, ...]
    pictures: tuple[str, ...]
    notes: str


def _runs_text(runs: Json, where: str) -> str:
    return "".join(as_str(r.get("text", ""), f"{where}.text") for r in as_objects(runs, where))


def _strs(value: Json, where: str) -> list[str]:
    return [as_str(v, f"{where}[{i}]") for i, v in enumerate(as_array(value, where))]


def numbers(value: Json, where: str) -> list[float]:
    """A JSON array of numbers (a box, a point)."""
    out: list[float] = []
    for i, v in enumerate(as_array(value, where)):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{where}[{i}]: a number was expected, found {v!r}")
        out.append(v)
    return out


def _drawings_hash(raw_page: RawPage, ids: Sequence[str]) -> str:
    """The drawings `ids` of a page, hashed without their ids and boxes."""
    by_id = {d["id"]: d for d in raw_page["drawings"]}
    items = [{k: v for k, v in by_id[i].items() if k not in ("id", "bbox")} for i in ids if i in by_id]
    return hashlib.sha1(json.dumps(items, sort_keys=True).encode()).hexdigest()[:10]


def summary(folder: Path) -> list[Summary]:
    """What a classified folder (deck.json, raw.json) says per slide (`Summary`)."""
    deck = as_object(json.loads((folder / "deck.json").read_text(encoding="utf-8")), "deck.json")
    raw_json = folder / "raw.json"  # (raw.json becomes pages only through its parser)
    raw = {p["index"]: p for p in parse_raw(json.loads(raw_json.read_text(encoding="utf-8")), str(raw_json))["pages"]}
    slides: list[Summary] = []
    for n, s in enumerate(as_objects(deck["slides"], "deck.json.slides")):
        title: str | None = None
        texts: list[str] = []
        pictures: list[str] = []
        for k, e in enumerate(as_objects(s["elements"], f"slides[{n}].elements")):
            where = f"slides[{n}].elements[{k}]"
            if e["kind"] == "text" and e.get("role") == "title" and title is None:
                first = as_objects(e["paragraphs"], f"{where}.paragraphs")[0]
                title = _runs_text(first["runs"], f"{where}.paragraphs[0].runs").strip()
            elif e["kind"] == "text" and e.get("role") != "footer":
                texts += [_runs_text(p["runs"], f"{where}.paragraphs.runs").strip()
                          for p in as_objects(e["paragraphs"], f"{where}.paragraphs")]
            elif e["kind"] == "table":
                texts += [_runs_text(c, f"{where}.cells").strip()
                          for row in as_array(e["cells"], f"{where}.cells") for c in as_array(row, f"{where}.cells")]
            elif e["kind"] == "diagram":
                texts += [_runs_text(p, f"{where}.nodes.paragraphs").strip()
                          for node in as_objects(e["nodes"], f"{where}.nodes")
                          for p in as_array(node.get("paragraphs") or [], f"{where}.nodes.paragraphs")]
            elif e["kind"] == "image":
                page = raw[as_int(s["page"], f"slides[{n}].page")]
                spans = {sp["id"]: sp for sp in page["spans"]}
                x0, y0, x1, y1 = numbers(e["bbox"], f"{where}.bbox")
                inside = _strs(e.get("drawings") or [], f"{where}.drawings") or [
                    d["id"] for d in page["drawings"]
                    if d["bbox"][0] >= x0 - 1 and d["bbox"][1] >= y0 - 1 and d["bbox"][2] <= x1 + 1 and d["bbox"][3] <= y1 + 1]
                content = _drawings_hash(page, inside) + "|" + \
                    "".join(spans[i]["text"] + spans[i]["font"] for i in _strs(e.get("spans", []), f"{where}.spans")
                            if i in spans)
                pictures.append(f"{e.get('role')}:{content}")
        notes = s.get("notes") or ""
        slides.append(Summary(title=title, texts=tuple(" ".join(t.split()) for t in texts if t.strip()),
                              pictures=tuple(pictures), notes=as_str(notes, f"slides[{n}].notes")))
    return slides


def classification_diff(a: list[Summary], b: list[Summary]) -> list[str]:
    """Differences from summary `a` to summary `b`, slides named by their title in `a`."""
    def sim(x: Summary, y: Summary) -> float:
        wx, wy = set(" ".join(x.texts).split()), set(" ".join(y.texts).split())
        return len(wx & wy) / max(1, len(wx | wy))

    match: dict[int, int] = {}
    for i, x in enumerate(a):
        same = [j for j, y in enumerate(b) if y.title == x.title and j not in match.values()]
        if len(same) == 1:
            match[i] = same[0]
    for i, x in enumerate(a):
        if i not in match:
            free = [(sim(x, y), j) for j, y in enumerate(b) if j not in match.values()]
            if free and max(free)[0] > 0.5:
                match[i] = max(free)[1]
    out = [f"slide- {x.title}" for i, x in enumerate(a) if i not in match]
    out += [f"slide+ {y.title}" for j, y in enumerate(b) if j not in match.values()]
    kept = [i for i in range(len(a)) if i in match]
    if [match[i] for i in kept] != sorted(match[i] for i in kept):
        out.append("order " + " / ".join(str(b[j].title) for j in sorted(match.values())))
    for i in kept:
        x, y, name = a[i], b[match[i]], a[i].title
        if x.title != y.title:
            out.append(f"{name}: title -> {y.title}")
        out += [f"{name}: text- {t}" for t in x.texts if t not in y.texts]
        out += [f"{name}: text+ {t}" for t in y.texts if t not in x.texts]
        if sorted(x.pictures) != sorted(y.pictures):
            out.append(f"{name}: pictures {len(x.pictures)} -> {len(y.pictures)} changed")
        if x.notes != y.notes:
            out.append(f"{name}: notes -> {y.notes}")
    return out


GUARD = re.compile(r"^\s*%<(\*|/)?(!?)(\w+)>(.*)$")


def render(flags: list[str]) -> str:
    """talk.tex with the guards resolved for these flags."""
    unknown = set(flags) - set(FLAGS)
    if unknown:
        raise ValueError(f"unknown flags {sorted(unknown)}")
    lines = MASTER.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(r"\documentclass"))
    out = [f"% Sync test talk, flags: {', '.join(flags) or 'none'}"]
    blocks: list[tuple[str, bool]] = []  # (guard, included)
    for line in lines[start:]:
        m = GUARD.match(line)
        if not m:
            if all(inc for _, inc in blocks):
                out.append(line)
            continue
        mark, neg, flag, rest = m.groups()
        wanted = (flag in flags) != bool(neg)
        if mark == "*":
            blocks.append((neg + flag, wanted))
        elif mark == "/":
            if not blocks or blocks[-1][0] != neg + flag:
                raise ValueError(f"unbalanced guard {line!r}")
            blocks.pop()
        elif wanted and all(inc for _, inc in blocks):
            out.append(rest)
    if blocks:
        raise ValueError(f"unclosed guards {blocks}")
    return "\n".join(out) + "\n"


def compile_tex(tex: Path) -> Path:
    """pdflatex with SyncTeX in the file's folder (`RUNS` passes); the PDF."""
    cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-synctex=1", tex.name]
    for _ in range(RUNS):
        done = subprocess.run(cmd, cwd=tex.parent, capture_output=True, text=True, errors="replace")
        if done.returncode:
            log = tex.with_suffix(".log")
            tail = log.read_text(errors="replace")[-3000:] if log.exists() else done.stdout[-3000:]
            raise RuntimeError(f"{tex} failed:\n{tail}")
    return tex.with_suffix(".pdf")


def compiled(variant: str, *, force: bool) -> Path:
    """out/<variant>.pdf, compiled again when `force` or when talk.tex changed since."""
    OUT.mkdir(exist_ok=True)
    tex, pdf = OUT / f"{variant}.tex", OUT / f"{variant}.pdf"
    text = render(VARIANTS[variant])
    if not force and pdf.exists() and tex.exists() and tex.read_text(encoding="utf-8") == text:
        return pdf
    tex.write_text(text, encoding="utf-8")
    return compile_tex(tex)


def build(variant: str) -> Path:
    """out/<variant>.pdf, compiled again only when talk.tex changed since."""
    return compiled(variant, force=False)


def main(names: list[str]) -> int:
    failed = 0
    for name in names or VARIANTS:
        try:
            print(f"OK   {compiled(name, force=True).name}")
        except (RuntimeError, KeyError) as e:
            failed += 1
            print(f"FAIL {name}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))