"""Source versions of the stress test talk (talk.tex) and what each changes.

Same machinery as tests/decks/sync/build.py (guards `%<flag>`, `%<*flag>`…`%</flag>`, VARIANTS,
CHECKS, titles, INTENDED) for a deck built to be ambiguous: repeated titles, repeated paragraphs,
repeated pictures, repeated cell values, repeated notes, untitled frames, and text that no naive
character index survives (astral characters, combining marks, NBSP, soft hyphens, RTL, CJK).
`summary` and the helpers it needs are imported from the sync build module; `render` and
`compile_tex` are this deck's own, because the flags differ and the unicode frames need a system
font, so the engine is **lualatex**.

The variants change the source drastically and ambiguously: two identical frames swapped, a label
moved to another frame, every title renamed at once, the first and last frame deleted, ten frames
reordered, every bullet of one frame rewritten, whitespace only, a cell in every row, one of two
identical pictures replaced, a frame inserted between two near-identical ones, and `kitchen`
combining most of them.

Usage: python tests/decks/stress/build.py [variant ...]    (default: all) -> out/<variant>.pdf
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
FIGURES = OUT / "figures"
MASTER = HERE / "talk.tex"
ENGINE = "lualatex"  # fontspec: the unicode frames load Cambria Math, YaHei, Segoe UI Emoji

_spec = importlib.util.spec_from_file_location("sync_build", HERE.parent / "sync" / "build.py")
sync_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_build)

GUARD = sync_build.GUARD


def summary(folder: Path) -> list[dict]:
    """What a classified folder says per slide: title, texts, pictures, notes (sync/build.py),
    with each picture's box size added - two `\\includegraphics` of different files hold no
    drawings and no spans, so nothing else tells them apart."""
    import json
    slides = sync_build.summary(folder)
    deck = json.loads((folder / "deck.json").read_text(encoding="utf-8"))
    for s, ir in zip(slides, deck["slides"]):
        sizes = [f"{round(e['bbox'][2] - e['bbox'][0])}x{round(e['bbox'][3] - e['bbox'][1])}"
                 for e in ir["elements"] if e["kind"] == "image"]
        s["pictures"] = [f"{p}@{z}" for p, z in zip(s["pictures"], sizes)]
    return slides

FLAGS = {
    "swaptwins": "swap the two frames that differ by one word",
    "insertframe": "add a frame between the two near-identical ones",
    "movelabel": "move a label to the next frame",
    "nolabel": "a frame loses its label",
    "retitleall": "rename every title at once",
    "dropends": "delete the first and the last frame",
    "reorder10": "reorder ten frames (reversed)",
    "rewritebullets": "rewrite every bullet of one frame",
    "rewriteblock": "rewrite the block title and body",
    "whitespace": "change only whitespace in the source",
    "everyrow": "change a cell in every row of the twenty-row table",
    "swappicture": "replace one of two identical pictures",
    "notesedit": "change the notes one of two identical-notes frames",
    "fourthree": "build the deck 4:3 instead of 16:9",
}

# The ten frames `reorder10` reverses, in source order (results-a first).
REORDERED = ["results-a", "results-b", None, "notes-a", "notes-b", "diagram", "displaymath",
             "code", "links", "numbers"]

KITCHEN = ["swaptwins", "insertframe", "movelabel", "retitleall", "reorder10", "rewritebullets",
           "everyrow", "swappicture", "notesedit"]
VARIANTS = {"v1": [], **{f: [f] for f in FLAGS}, "kitchen": KITCHEN,
            # sources of the scenarios in tests/test_stress_live.py
            "ambiguous": ["swaptwins", "insertframe", "everyrow"],
            "identity": ["movelabel", "nolabel", "retitleall"],
            "churn": ["reorder10", "dropends", "rewritebullets", "rewriteblock"],
            "pictures": ["swappicture", "notesedit"]}

# ---------------------------------------------------------------- the frames of v1

TWENTY = {"contains": "Twenty rows, one verdict"}   # the table itself is a picture, see NOTES below
REPEAT = {"contains": "Geometry"}
TWINPICS = {"contains": "The same file twice"}
NOTES_A = {"contains": "A slide whose speaker notes are shared"}
BLOCKCOL = {"contains": "A block that lives in a column"}
AGENDA = {"contains": "Characters that break naive indexing"}
SUMMARY = {"contains": "Labels decide identity"}

FRAMES = [  # (label in v1, title in v1); None = an untitled frame
    ("title", "A Deck Built to Break the Merge"),
    ("agenda", "Agenda"),
    ("twin-a", "Twin slides"),
    ("twin-b", "Twin slides"),
    ("echo-one", "Echo, first time"),
    ("echo-two", "Echo, second time"),
    ("echo-three", "Echo, third time"),
    ("astral", "Characters outside the BMP"),
    ("scripts", "Other scripts and invisible characters"),
    ("longline", "One very long line"),
    ("gaps", "Gaps and ragged ends"),
    ("bigtable", "Twenty rows"),
    ("widetable", "Ten columns"),
    ("merged", "Merged cells"),
    ("columns", "Two columns of text"),
    ("blockcol", "A block inside a column"),
    ("boxes", "Twelve text boxes"),
    ("formulas", "Formulas side by side"),
    ("steps", "Steps that appear one by one"),
    ("alternatives", "Alternatives on one frame"),
    ("figcaption", "A figure with a caption"),
    ("twinpics", "Two identical pictures"),
    ("picagain", "The same picture again"),
    ("fullpage", None),
    ("onlypic", None),
    ("results-a", "Results"),
    ("results-b", "Results"),
    (None, "Results"),
    ("notes-a", "Notes, one"),
    ("notes-b", "Notes, two"),
    ("diagram", "A small diagram"),
    ("displaymath", "Display mathematics"),
    ("code", "A code block"),
    ("links", "Links in the text"),
    ("numbers", "Numbered steps"),
    ("description", "Description list"),
    ("quote", "A quotation"),
    ("emphasis", "Emphasis of every kind"),
    ("deep", "Deep nesting again"),
    ("mobile", "Moving labels"),
    ("arriving", "Arriving labels"),
    ("vanishing", "Disappearing label"),
    ("footnotes", "Footnotes and small print"),
    ("repeatcells", "Cells that repeat a value"),
    ("whitespace", "Whitespace only"),
    ("quote-again", "The quotation, again"),
    ("backup", "Backup material"),
    ("summary", "Takeaways"),
]
V1 = [(lab or f"#{i}", lab, t) for i, (lab, t) in enumerate(FRAMES)]  # (name, label, title)
INSERTED = ("between", "between", "Inserted between the twins")


def frames(flags: list[str]) -> list[tuple[str, str | None, str | None]]:
    """(name, label, title) of every frame of the variant, in order. The name is the frame's v1
    label (else #index in v1): what `classification_diff` calls it, whatever the variant does."""
    out = list(V1)
    a = next(i for i, f in enumerate(out) if f[0] == "twin-a")
    b = next(i for i, f in enumerate(out) if f[0] == "twin-b")
    if "swaptwins" in flags:
        out[a], out[b] = out[b], out[a]
    if "insertframe" in flags:  # the inserted frame sits where twin-a was, before twin-b
        out.insert(a if "swaptwins" in flags else a + 1, INSERTED)
    if "reorder10" in flags:
        i = next(k for k, f in enumerate(out) if f[1] == "results-a")
        out[i:i + 10] = out[i:i + 10][::-1]
    if "dropends" in flags:
        out = out[1:-1]
    if "movelabel" in flags:
        out = [(n, None if lab == "mobile" else "mobile" if lab == "arriving" else lab, t) for n, lab, t in out]
    if "nolabel" in flags:
        out = [(n, None if lab == "vanishing" else lab, t) for n, lab, t in out]
    if "retitleall" in flags:
        out = [(n, lab, t and f"{t} v2") for n, lab, t in out]
    return out


def titles(flags: list[str]) -> list[str | None]:
    return [t for _, _, t in frames(flags)]


def names(flags: list[str]) -> list[str]:
    return [n for n, _, _ in frames(flags)]


# ---------------------------------------------------------------- what a synced deck must show

CHECKS = {
    "swaptwins": [],      # an order change only (titles())
    "movelabel": [],      # identity only: nothing visible changes
    "nolabel": [],
    "reorder10": [],
    "fourthree": [],
    "whitespace": [{"check": "text", "slide": None, "count": 1,
                    "text": "A paragraph whose words never change, only the spaces between them in the source."}],
    "insertframe": [{"check": "text", "slide": None, "count": 1,
                     "text": "A frame added exactly where identity is hardest"}],
    "retitleall": [{"check": "title", "slide": AGENDA, "text": "Agenda v2"},
                   {"check": "title", "slide": TWENTY, "text": "Twenty rows v2"}],
    "dropends": [{"check": "slide_count", "slide": SUMMARY, "count": 0},
                 {"check": "text", "slide": None, "text": "Laboratory of Hard Cases", "count": 0}],
    "rewritebullets": [{"check": "text", "slide": None, "count": 1,
                        "text": "Every timing below comes from this deck and no other"},
                       {"check": "text", "slide": None, "count": 1,
                        "text": "None of it is shown while the talk is running"},
                       {"check": "text", "slide": None, "count": 1,
                        "text": "Seconds are rounded, milliseconds are dropped"},
                       {"check": "text", "slide": None, "count": 0,
                        "text": "Timings are measured on the stress deck itself"}],
    "rewriteblock": [{"check": "text", "slide": BLOCKCOL, "count": 1,
                      "text": "The deck always wins, and the report explains why."},
                     {"check": "text", "slide": BLOCKCOL, "count": 0, "text": "Deck edits win"}],
    "everyrow": [{"check": "text", "slide": REPEAT, "text": "moved", "count": 6},
                 {"check": "text", "slide": REPEAT, "text": "same", "count": 12},
                 {"check": "image", "slide": TWENTY, "count": 1},
                 {"check": "fresh", "slide": TWENTY}],
    "swappicture": [{"check": "image", "slide": TWINPICS, "count": 2},
                    {"check": "fresh", "slide": TWINPICS}],
    "notesedit": [{"check": "notes", "slide": NOTES_A,
                   "text": "Slow right down here; give the audience a moment to catch up."}],
}


def checks(flags: list[str]) -> list[dict]:
    """What a deck synced to this source must show (source side only)."""
    return [c for f in flags for c in CHECKS[f]]


# ---------------------------------------------------------------- intended classification diffs

BACKUP_BULLETS = [("Timings are measured on the stress deck itself", "Every timing below comes from this deck and no other"),
                  ("Nothing here is shown in the talk", "None of it is shown while the talk is running"),
                  ("The numbers are rounded to whole seconds", "Seconds are rounded, milliseconds are dropped")]

INTENDED = {  # classification_diff(v1, variant) items per flag
    "swaptwins": ["order twin-b / twin-a"],
    "insertframe": ["slide+ between"],
    "movelabel": [],
    "nolabel": [],
    "fourthree": [],
    "whitespace": [],
    "reorder10": ["order " + " / ".join(["numbers", "links", "code", "displaymath", "diagram",
                                         "notes-b", "notes-a", "#27", "results-b", "results-a"])],
    "dropends": ["slide- title", "slide- summary"],
    "retitleall": [f"{lab or '#' + str(i)}: title -> {t} v2"
                   for i, (lab, t) in enumerate(FRAMES) if t],
    "rewritebullets": [f"backup: text- {old}" for old, _ in BACKUP_BULLETS] +
                      [f"backup: text+ {new}" for _, new in BACKUP_BULLETS],
    "rewriteblock": ["blockcol: text- Rule", "blockcol: text- Deck edits win, and the source is told so in the report.",
                     "blockcol: text+ Rule, restated", "blockcol: text+ The deck always wins, and the report explains why."],
    "everyrow": ["bigtable: pictures 1 -> 1 changed"] +
                [f"repeatcells: text- {v}" for v in []] + ["repeatcells: text+ moved"],
    "swappicture": ["twinpics: pictures 2 -> 2 changed"],
    "notesedit": ["notes-a: notes -> Slow right down here; give the audience a moment to catch up."],
    "fourthree": [],
}
# 4:3 is a page size, not an edit: every paragraph rewraps, so its classification diff is not
# something to pin down - the variant only has to build and convert.
DIFF_EXEMPT = {"fourthree"}


def intended_diff(flags: list[str]) -> set[str]:
    """The classification differences from v1 this variant is meant to make."""
    out = {d for f in flags for d in INTENDED[f]}
    kept = set(names(flags))  # a per-slide item of a frame the variant deletes never shows up
    out = {d for d in out if ":" not in d or d.split(":")[0] in kept}
    if {"swaptwins", "reorder10"} & set(flags):
        out = {d for d in out if not d.startswith("order ")}
        out.add("order " + " / ".join(n for n in names(flags) if n != "between"))
    return out


# ---------------------------------------------------------------- classification diff

def classification_diff(a: list[dict], b: list[dict], a_names: list[str] | None = None,
                        b_names: list[str] | None = None) -> list[str]:
    """Differences from summary `a` to summary `b`, slides named by their frame label.

    Like the sync deck's, but titles repeat and some frames have none here, so slides are named
    by `a_names` (the labels of v1) and paired by text similarity in order, never by title.
    """
    a_names = a_names or [f"#{i}" for i in range(len(a))]
    b_names = b_names or [f"#{i}" for i in range(len(b))]

    def sim(x, y):
        wx, wy = set(" ".join(x["texts"]).split()), set(" ".join(y["texts"]).split())
        if not wx and not wy:
            return 1.0 if x["title"] == y["title"] else 0.0
        # The notes carry most of the weight: a frame whose every bullet is rewritten still has
        # them, while titles and words repeat all over this deck.
        return (len(wx & wy) / max(1, len(wx | wy)) + 0.05 * (x["title"] == y["title"])
                + 0.4 * bool(x["notes"] and x["notes"] == y["notes"]))

    match: dict[int, int] = {}
    pairs = sorted(((sim(x, y), i, j) for i, x in enumerate(a) for j, y in enumerate(b)), reverse=True)
    taken: set[int] = set()
    for s, i, j in pairs:
        if s > 0.5 and i not in match and j not in taken:
            match[i], _ = j, taken.add(j)
    out = [f"slide- {a_names[i]}" for i in range(len(a)) if i not in match]
    out += [f"slide+ {b_names[j]}" for j in range(len(b)) if j not in taken]
    kept = sorted(match)
    if [match[i] for i in kept] != sorted(match[i] for i in kept):
        out.append("order " + " / ".join(b_names[j] for j in sorted(match.values())))
    for i in kept:
        x, y, name = a[i], b[match[i]], a_names[i]
        if x["title"] != y["title"]:
            out.append(f"{name}: title -> {y['title']}")
        out += [f"{name}: text- {t}" for t in x["texts"] if t not in y["texts"]]
        out += [f"{name}: text+ {t}" for t in y["texts"] if t not in x["texts"]]
        if sorted(x["pictures"]) != sorted(y["pictures"]):
            out.append(f"{name}: pictures {len(x['pictures'])} -> {len(y['pictures'])} changed")
        if x["notes"] != y["notes"]:
            out.append(f"{name}: notes -> {y['notes']}")
    return out


# ---------------------------------------------------------------- rendering the source

FRAME_START = re.compile(r"^\s*\\begin\{frame\}")
FRAME_END = re.compile(r"^\s*\\end\{frame\}")


def _reorder_frames(lines: list[str], first_label: str, count: int) -> list[str]:
    """Reverse `count` frames starting at the frame labelled `first_label`.

    Ten frames swapped around is a source change no guard can express readably (it would mean a
    second copy of every one of them), so it is a transformation of the rendered file instead.
    """
    spans, i = [], 0
    while i < len(lines):
        if FRAME_START.match(lines[i]):
            j = i
            while j < len(lines) and not FRAME_END.match(lines[j]):
                j += 1
            spans.append((i, j))
            i = j + 1
        else:
            i += 1
    start = next((k for k, (a, _) in enumerate(spans) if f"label={first_label}" in lines[a]), None)
    if start is None:
        raise ValueError(f"no frame labelled {first_label}")
    chosen = spans[start:start + count]
    blocks = [lines[a:b + 1] for a, b in chosen][::-1]
    out = list(lines)
    for (a, b), block in reversed(list(zip(chosen, blocks))):  # back to front: the slots move
        out[a:b + 1] = block
    return out


def render(flags: list[str], master: Path = MASTER) -> str:
    """talk.tex with the guards resolved for these flags."""
    unknown = set(flags) - set(FLAGS)
    if unknown:
        raise ValueError(f"unknown flags {sorted(unknown)}")
    lines = master.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if "\\documentclass" in l)
    out, blocks = [], []  # blocks: (guard, included)
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
    if "reorder10" in flags:
        out = _reorder_frames(out, "results-a", 10)
    header = [f"% Stress test talk, flags: {', '.join(flags) or 'none'}", f"% !TEX program = {ENGINE}"]
    return "\n".join(header + out) + "\n"


# ---------------------------------------------------------------- pictures

def write_figures(folder: Path = FIGURES) -> None:
    """The pictures the talk includes: one used twice on a slide and again on another, one that
    replaces it in a variant, and one full-page background. Written once, byte for byte the same."""
    from PIL import Image, ImageDraw
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "dot.png").exists():
        img = Image.new("RGB", (240, 240), "white")
        d = ImageDraw.Draw(img)
        d.ellipse((30, 30, 210, 210), fill=(40, 80, 200), outline=(10, 20, 60), width=6)
        img.save(folder / "dot.png")
    if not (folder / "wave.png").exists():
        import math
        img = Image.new("RGB", (240, 180), "white")  # another aspect than dot.png, so a replaced
        d = ImageDraw.Draw(img)                      # picture is visible in the element's box
        d.line([(x, 90 - int(60 * math.sin(x / 38))) for x in range(10, 231)], fill=(200, 30, 30), width=6)
        img.save(folder / "wave.png")
    if not (folder / "wide.png").exists():
        img = Image.new("RGB", (960, 540))
        px = img.load()
        for y in range(540):
            for x in range(0, 960, 8):
                c = (200 - y // 6, 210 - y // 8, 240)
                for k in range(8):
                    px[x + k, y] = c
        img.save(folder / "wide.png")


# ---------------------------------------------------------------- compiling

def compile_tex(tex: Path, runs: int = 3) -> Path:
    """lualatex with SyncTeX in the file's folder; the PDF."""
    cmd = [ENGINE, "-interaction=nonstopmode", "-halt-on-error", "-synctex=1", tex.name]
    for _ in range(runs):
        done = subprocess.run(cmd, cwd=tex.parent, capture_output=True, text=True, errors="replace")
        if done.returncode:
            log = tex.with_suffix(".log")
            tail = log.read_text(errors="replace")[-4000:] if log.exists() else done.stdout[-4000:]
            raise RuntimeError(f"{tex} failed:\n{tail}")
        if "Rerun" not in done.stdout:
            break
    return tex.with_suffix(".pdf")


def build(variant: str, force: bool = False) -> Path:
    """out/<variant>.pdf, compiled again only when talk.tex changed since."""
    OUT.mkdir(exist_ok=True)
    write_figures()
    tex, pdf = OUT / f"{variant}.tex", OUT / f"{variant}.pdf"
    text = render(VARIANTS[variant])
    if not force and pdf.exists() and tex.exists() and tex.read_text(encoding="utf-8") == text:
        return pdf
    tex.write_text(text, encoding="utf-8")
    return compile_tex(tex)


def main(names_: list[str]) -> int:
    failed = 0
    for name in names_ or VARIANTS:
        try:
            print(f"OK   {build(name, force=True).name}")
        except (RuntimeError, KeyError, ValueError) as e:
            failed += 1
            print(f"FAIL {name}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
