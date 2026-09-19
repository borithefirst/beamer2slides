"""How readable a beamer source is to the person who will edit it: a cheap proxy, no compile.

`adopt` is scored for fidelity by `adopt_bench`; this scores the other half of its promise - a source
a person can keep. Only the frame bodies count (what one edits slide by slide); the preamble and any
.sty beside it are machinery one reads once, and are reported apart.

Each component is set against sources people wrote (the test decks, the sync talk: `REFERENCE`,
medians printed by `python -m beamer2slides.devtools.readability ref`), as min(1, human / ours) for
what should be small and min(1, ours / human) for what should be large, and the score is their
geometric mean: 1.0 reads like a hand-written source.

- `lines`: lines per frame body.
- `numbers`: numeric literals (lengths, sizes, coordinates) per word of visible text.
- `plumbing`: commands that are not an author's vocabulary (`AUTHOR`), per word.
- `bloat`: source characters per visible character.
- `author`: the share of commands that are an author's vocabulary.
- `repeat`: 1 - the share of body lines found in at least `REPEAT_FRAMES` frames - what a theme or a
  macro should say once (a layout's placeholders, a master's decoration, the same text style).

`construct` charges each body line to what wrote it (text plumbing, style switches, placement,
shapes, pictures, tables, text), so a report ranks where the lines go, as `adopt_bench losses` does
for pixels.
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

AUTHOR = frozenset("""
begin end frame frametitle framesubtitle item itemize enumerate description textbf textit emph underline
texttt includegraphics caption centering column columns block alertblock exampleblock href url tabular
hline toprule midrule bottomrule multicolumn cline textcolor color small footnotesize scriptsize tiny large
Large LARGE huge Huge normalsize vspace hspace newline par pause only uncover onslide visible alert title
subtitle author institute date titlepage maketitle section subsection tableofcontents note label ref cite
vfill hfill quad qquad textsc textsf textrm mathbf frac sqrt sum int left right cdot ldots dots footnote
bibitem raggedright raggedleft flushleft flushright center figure table minipage parbox makebox fbox
colorbox tikz node draw fill path tikzpicture scope usebeamertemplate usebeamercolor usebeamerfont
setbeamertemplate setbeamercolor setbeamerfont
""".split())

CS = re.compile(r"\\([A-Za-z@]+|.)")
NUM = re.compile(r"(?<![A-Za-z0-9])-?\d+(?:\.\d+)?")
FRAME = re.compile(r"\\begin\{frame\}(.*?)\\end\{frame\}", re.S)
REPEAT_FRAMES = 3

# medians over REFERENCE (sources people wrote), `ref` recomputes them
HUMAN = {"lines": 8.0, "numbers": 0.09, "plumbing": 0.03, "bloat": 1.9, "author": 0.87}
REFERENCE = ("tests/decks/*.tex", "tests/decks/sync/talk.tex")

CONSTRUCTS = [
    ("text plumbing", re.compile(r"\\(leftskip|rightskip|prevdepth|baselineskip|vrule|llap|rlap|parfillskip|"
                                 r"slidesbox|vbox|vskip|vss|noindent|strut|hskip|kern|spaceskip)\b")),
    ("table", re.compile(r"\\adopt(cell|row|fix|box|drop|y)\b")),
    ("shape", re.compile(r"\\(path|draw|fill|node|useasboundingbox|shade)\b|tikzpicture")),
    ("picture", re.compile(r"\\includegraphics\b")),
    ("placement", re.compile(r"textblock")),
    ("style switch", re.compile(r"\\(slidesize|color|fontseries|fontshape|selectfont|ttfamily|sffamily|rmfamily|"
                                r"addfontfeature|fontspec|bfseries|itshape|slidesfont\w*)\b")),
    ("link", re.compile(r"\\href\b")),
]


def frames(tex: str) -> list[str]:
    return FRAME.findall(tex.split("\\begin{document}", 1)[-1])


def visible(body: str) -> str:
    """The words a reader sees, roughly: commands, options, lengths and punctuation taken out."""
    s = re.sub(r"(?<!\\)%.*", "", body)
    s = CS.sub(" ", s)
    s = re.sub(r"\[[^\]\n]*\]", " ", s)
    s = NUM.sub(" ", s)
    s = re.sub(r"[{}\[\]()=,;.\-+*/&^_$#~|<>:!?\"']", " ", s)
    return " ".join(w for w in s.split() if len(w) > 1 and re.search(r"[^\W\d_]", w)
                    and w not in ("bp", "pt", "em", "ex", "fil", "plus", "minus", "north", "west", "cycle"))


def construct(line: str) -> str:
    for name, pat in CONSTRUCTS:
        if pat.search(line):
            return name
    s = line.strip()
    if not s or re.fullmatch(r"[{}%\s]*", s):
        return "braces"
    return "text" if visible(s) else "other"


def _key(line: str) -> str | None:
    s = line.strip()
    if len(s) < 12 or s.startswith("\\end{") or not re.search(r"[A-Za-z]", s):
        return None
    return s


def measure(tex: str) -> dict:
    fs = frames(tex)
    if not fs:
        return {}
    words = sum(len(visible(f).split()) for f in fs)
    text_chars = sum(len(visible(f)) for f in fs)
    cmds = Counter(m for f in fs for m in CS.findall(f) if m[:1].isalpha())
    n_cmd = sum(cmds.values())
    author = sum(n for c, n in cmds.items() if c in AUTHOR)
    lines = [ln for f in fs for ln in f.strip("\n").split("\n")]
    seen = defaultdict(set)
    for k, f in enumerate(fs):
        for ln in f.split("\n"):
            key = _key(ln)
            if key:
                seen[key].add(k)
    repeated = sum(1 for ln in lines if (key := _key(ln)) and len(seen[key]) >= REPEAT_FRAMES)
    by = Counter(construct(ln) for ln in lines)
    w = max(words, 1)
    return {"frames": len(fs), "words": words,
            "lines": len(lines) / len(fs),
            "numbers": sum(len(NUM.findall(re.sub(r"(?<!\\)%.*", "", f))) for f in fs) / w,
            "plumbing": (n_cmd - author) / w,
            "bloat": sum(len(f) for f in fs) / max(text_chars, 1),
            "author": author / max(n_cmd, 1),
            "repeat": 1 - repeated / max(len(lines), 1),
            "constructs": dict(by),
            "top_commands": cmds.most_common(10),
            "top_repeated": sorted(((len(v), k) for k, v in seen.items() if len(v) >= REPEAT_FRAMES), reverse=True)[:5]}


def components(m: dict, human: dict = HUMAN) -> dict:
    small = {k: min(1.0, human[k] / m[k]) if m[k] > 0 else 1.0 for k in ("lines", "numbers", "plumbing", "bloat")}
    return {**small, "author": min(1.0, m["author"] / human["author"]), "repeat": m["repeat"]}


def score(m: dict, human: dict = HUMAN) -> float:
    c = components(m, human)
    return math.exp(sum(math.log(max(v, 1e-3)) for v in c.values()) / len(c))


def reference(root: Path) -> dict:
    ms = [measure(p.read_text(encoding="utf-8", errors="replace"))
          for pat in REFERENCE for p in sorted(root.glob(pat))]
    ms = [m for m in ms if m]

    def median(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2]
    return {k: round(median([m[k] for m in ms]), 3) for k in HUMAN}


def tree_source(tree: Path) -> str:
    main = tree / "main.tex"
    return main.read_text(encoding="utf-8", errors="replace") if main.exists() else ""


def report(corpus: Path, tag: str, verbose: bool = False) -> None:
    rows, lines_by = [], Counter()
    for d in sorted(p for p in corpus.iterdir() if (p / "runs" / tag / "tree").is_dir()):
        m = measure(tree_source(d / "runs" / tag / "tree"))
        if not m:
            continue
        rows.append((d.name, m))
        lines_by.update(m["constructs"])
    if not rows:
        print(f"no trees under tag {tag!r}")
        return
    print(f"{'deck':<22} {'score':>5}  {'lines':>6} {'nums/w':>6} {'plumb/w':>7} {'bloat':>5} {'author':>6} {'repeat':>6}")
    for name, m in sorted(rows, key=lambda r: score(r[1])):
        print(f"{name:<22} {score(m):5.3f}  {m['lines']:6.1f} {m['numbers']:6.2f} {m['plumbing']:7.2f} "
              f"{m['bloat']:5.1f} {m['author']:6.2f} {m['repeat']:6.2f}")
        if verbose:
            print("    ", m["top_commands"])
            for n, k in m["top_repeated"]:
                print(f"     x{n}: {k[:100]}")
    mean = sum(score(m) for _, m in rows) / len(rows)
    comp = {k: sum(components(m)[k] for _, m in rows) / len(rows) for k in components(rows[0][1])}
    print(f"\n{len(rows)} decks: readability {mean:.3f}  (" + ", ".join(f"{k} {v:.2f}" for k, v in comp.items()) + ")")
    total = sum(lines_by.values())
    print("\nframe body lines by construct:")
    for k, n in lines_by.most_common():
        print(f"  {100 * n / total:5.1f}%  {n:7}  {k}")


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="score every deck's source under a bench tag")
    r.add_argument("--tag", required=True)
    r.add_argument("-v", "--verbose", action="store_true")
    f = sub.add_parser("file", help="score .tex files")
    f.add_argument("paths", nargs="+")
    sub.add_parser("ref", help="the human medians, from REFERENCE")
    args = ap.parse_args(argv)
    if args.cmd == "report":
        from .adopt_bench import CORPUS
        report(CORPUS, args.tag, args.verbose)
    elif args.cmd == "file":
        for p in args.paths:
            m = measure(Path(p).read_text(encoding="utf-8", errors="replace"))
            print(f"{p}: {score(m):.3f}" if m else f"{p}: no frames")
    else:
        print(reference(Path.cwd()))


if __name__ == "__main__":
    main(sys.argv[1:])
