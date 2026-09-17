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
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
MASTER = HERE / "talk.tex"

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
    "notes": "change the speaker notes of two frames",
    "numbers": "insert a numbered step and reword the last one",
    "untitled": "rename the unlabelled frame's title",
}
MIXED = ["reword", "addbullet", "formula", "figure", "addframe", "reorder", "tablecell", "notes"]
VARIANTS = {"v1": [], **{f: [f] for f in FLAGS}, "mixed": MIXED,
            "chain": MIXED + ["blockedit", "deleteframe", "untitled", "numbers", "retitle", "removebullet"],
            # sources of the scenarios in tests/test_sync_live.py
            "disjoint": ["tablecell", "blockedit", "figure", "notes", "numbers"],
            "same-element": ["reword", "blockedit", "figure", "formula", "tablecell"],
            "conflict": ["reword", "blockedit"],
            "deletions": ["deleteframe", "tablecell", "removebullet"],
            "slides": ["addframe", "reorder", "untitled"]}

MOTIVATION = {"contains": "Later the source changes again"}  # (no scenario edits these words)
CHECKS = {
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
    "notes": [{"check": "notes", "slide": MOTIVATION, "text": "Start with a show of hands: who edited a converted deck?"},
              {"check": "notes", "slide": {"title": "Finding the same slide"},
               "text": "Labels are the stable key. Titles and page numbers are only fallbacks."}],
    "numbers": [{"check": "text", "slide": {"title": "The sync algorithm"}, "text": "Match slides by their frame labels",
                 "count": 1},
                {"check": "text", "slide": {"title": "The sync algorithm"}, "text": "Merge and write the changes", "count": 0},
                {"check": "fresh", "slide": {"title": "The sync algorithm"}}],
    "untitled": [{"check": "slide_count", "slide": {"title": "Takeaways"}, "count": 1},
                 {"check": "slide_count", "slide": {"title": "Conclusions"}, "count": 0}],
}


WHY, ALGO = "Why decks and sources diverge", "The sync algorithm"
INTENDED = {  # classification_diff(v1, variant) items per flag
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
    "notes": [f"{WHY}: notes -> Start with a show of hands: who edited a converted deck?",
              "Finding the same slide: notes -> Labels are the stable key. Titles and page numbers are only fallbacks."],
    "numbers": [f"{ALGO}: text- Merge and write the changes", f"{ALGO}: text+ Match slides by their frame labels",
                f"{ALGO}: text+ Merge the changes and write them back", f"{ALGO}: pictures 4 -> 5 changed"],
    "untitled": ["Conclusions: title -> Takeaways"],
}


def intended_diff(flags: list[str]) -> set[str]:
    """The classification differences from v1 this variant is meant to make."""
    out = {d for f in flags for d in INTENDED[f]}
    if "reorder" in flags:
        out.add("order " + " / ".join(t for t in titles(flags) if t != "Pulling edits back"))
    return out


def checks(flags: list[str]) -> list[dict]:
    """What a deck synced to this source must show (source side only)."""
    return [c for f in flags for c in CHECKS[f]]


def titles(flags: list[str]) -> list[str]:
    """Slide titles of the variant in order."""
    order = ["Keeping Slides and Source in Sync",
             "Why decks drift away from their source" if "retitle" in flags else "Why decks and sources diverge",
             "The sync algorithm", "Merging text", "Convergence"]
    order += ["Results", "Merge policy"] if "reorder" in flags else ["Merge policy", "Results"]
    order += ["Pulling edits back"] * ("addframe" in flags) + ["Three versions"] * ("deleteframe" not in flags)
    return order + ["Finding the same slide", "Takeaways" if "untitled" in flags else "Conclusions"]


def _runs_text(runs: list[dict]) -> str:
    return "".join(r.get("text", "") for r in runs)


def _drawings_hash(raw_page: dict, ids: list[str], origin: list[float]) -> str:
    by_id = {d["id"]: d for d in raw_page["drawings"]}
    x, y = origin[0], origin[1]

    def rel(v):
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, list) and len(v) == 2 and all(isinstance(c, (int, float)) for c in v):
            return [round((v[0] - x) * 2) / 2, round((v[1] - y) * 2) / 2]
        return [rel(c) for c in v] if isinstance(v, list) else v

    items = [{k: rel(v) for k, v in by_id[i].items() if k not in ("id", "bbox")} for i in ids if i in by_id]
    return hashlib.sha1(json.dumps(items, sort_keys=True).encode()).hexdigest()[:10]


def summary(folder: Path) -> list[dict]:
    """What a classified folder (deck.json, raw.json) says per slide: title, texts (paragraphs,
    table cells, diagram labels), pictures (role and a position-free content hash), notes."""
    deck = json.loads((folder / "deck.json").read_text(encoding="utf-8"))
    raw = {p["index"]: p for p in json.loads((folder / "raw.json").read_text(encoding="utf-8"))["pages"]}
    slides = []
    for s in deck["slides"]:
        title, texts, pictures = None, [], []
        for e in s["elements"]:
            if e["kind"] == "text" and e.get("role") == "title" and title is None:
                title = _runs_text(e["paragraphs"][0]["runs"]).strip()
            elif e["kind"] == "text" and e.get("role") != "footer":
                texts += [_runs_text(p["runs"]).strip() for p in e["paragraphs"]]
            elif e["kind"] == "table":
                texts += [_runs_text(c).strip() for row in e["cells"] for c in row]
            elif e["kind"] == "diagram":
                texts += [_runs_text(p).strip() for n in e["nodes"] for p in n.get("paragraphs") or []]
            elif e["kind"] == "image":
                page = raw[s["page"]]
                spans = {sp["id"]: sp for sp in page["spans"]}
                x0, y0, x1, y1 = e["bbox"]
                inside = e.get("drawings") or [d["id"] for d in page["drawings"] if d["bbox"][0] >= x0 - 1 and
                                               d["bbox"][1] >= y0 - 1 and d["bbox"][2] <= x1 + 1 and d["bbox"][3] <= y1 + 1]
                content = _drawings_hash(page, inside, e["bbox"]) + "|" + \
                    "".join(spans[i]["text"] + spans[i]["font"] for i in e.get("spans", []) if i in spans)
                pictures.append(f"{e.get('role')}:{content}")
        slides.append({"title": title, "texts": [" ".join(t.split()) for t in texts if t.strip()], "pictures": pictures,
                       "notes": s.get("notes") or ""})
    return slides


def classification_diff(a: list[dict], b: list[dict]) -> list[str]:
    """Differences from summary `a` to summary `b`, slides named by their title in `a`."""
    def sim(x, y):
        wx, wy = set(" ".join(x["texts"]).split()), set(" ".join(y["texts"]).split())
        return len(wx & wy) / max(1, len(wx | wy))

    match: dict[int, int] = {}
    for i, x in enumerate(a):
        same = [j for j, y in enumerate(b) if y["title"] == x["title"] and j not in match.values()]
        if len(same) == 1:
            match[i] = same[0]
    for i, x in enumerate(a):
        if i not in match:
            free = [(sim(x, y), j) for j, y in enumerate(b) if j not in match.values()]
            if free and max(free)[0] > 0.5:
                match[i] = max(free)[1]
    out = [f"slide- {x['title']}" for i, x in enumerate(a) if i not in match]
    out += [f"slide+ {y['title']}" for j, y in enumerate(b) if j not in match.values()]
    kept = [i for i in range(len(a)) if i in match]
    if [match[i] for i in kept] != sorted(match[i] for i in kept):
        out.append("order " + " / ".join(b[j]["title"] for j in sorted(match.values())))
    for i in kept:
        x, y, name = a[i], b[match[i]], a[i]["title"]
        if x["title"] != y["title"]:
            out.append(f"{name}: title -> {y['title']}")
        out += [f"{name}: text- {t}" for t in x["texts"] if t not in y["texts"]]
        out += [f"{name}: text+ {t}" for t in y["texts"] if t not in x["texts"]]
        if sorted(x["pictures"]) != sorted(y["pictures"]):
            out.append(f"{name}: pictures {len(x['pictures'])} -> {len(y['pictures'])} changed")
        if x["notes"] != y["notes"]:
            out.append(f"{name}: notes -> {y['notes']}")
    return out


GUARD = re.compile(r"^\s*%<(\*|/)?(!?)(\w+)>(.*)$")


def render(flags: list[str], master: Path = MASTER) -> str:
    """talk.tex with the guards resolved for these flags."""
    unknown = set(flags) - set(FLAGS)
    if unknown:
        raise ValueError(f"unknown flags {sorted(unknown)}")
    lines = master.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(r"\documentclass"))
    out, blocks = [f"% Sync test talk, flags: {', '.join(flags) or 'none'}"], []  # blocks: (guard, included)
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


def compile_tex(tex: Path, runs: int = 2) -> Path:
    """pdflatex with SyncTeX in the file's folder (two passes); the PDF."""
    cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-synctex=1", tex.name]
    for _ in range(runs):
        done = subprocess.run(cmd, cwd=tex.parent, capture_output=True, text=True, errors="replace")
        if done.returncode:
            log = tex.with_suffix(".log")
            tail = log.read_text(errors="replace")[-3000:] if log.exists() else done.stdout[-3000:]
            raise RuntimeError(f"{tex} failed:\n{tail}")
    return tex.with_suffix(".pdf")


def build(variant: str, force: bool = False) -> Path:
    """out/<variant>.pdf, compiled again only when talk.tex changed since."""
    OUT.mkdir(exist_ok=True)
    tex, pdf = OUT / f"{variant}.tex", OUT / f"{variant}.pdf"
    text = render(VARIANTS[variant])
    if not force and pdf.exists() and tex.exists() and tex.read_text(encoding="utf-8") == text:
        return pdf
    tex.write_text(text, encoding="utf-8")
    return compile_tex(tex)


def main(names: list[str]) -> int:
    failed = 0
    for name in names or VARIANTS:
        try:
            print(f"OK   {build(name, force=True).name}")
        except (RuntimeError, KeyError) as e:
            failed += 1
            print(f"FAIL {name}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
