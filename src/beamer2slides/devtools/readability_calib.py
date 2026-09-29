"""Does `readability.py` measure what a person means by "a source I can keep"? Blind judges decide.

The readability proxy steers `adopt`'s work, so it has to be checked against human judgement. This is
the harness for that check; it makes the sample, writes the prompts, takes the verdicts back and
prints the agreement. **It calls no model**: the judging is done outside it, by fresh readers who see
nothing but the prompt (`prompts/batch-NN.md`), and their answers come back as JSON.

- `sample`: 40 slides drawn from the corpus, deterministically (sha256 of seed, deck and slide index -
  no RNG whose stream may change), stratified over the six kinds a frame can be (title, list, table,
  shape, picture, prose - `frame_kind`, decided on the newer form's body), at most `PER_DECK` of a
  kind from one deck so no deck speaks for a stratum.
- `pairs`: each sampled slide as a `form` pair (the same slide written both ways, sides shuffled by
  hash), and half of them again as a `human` pair against a frame a person wrote (`tests/decks`,
  matched by kind where one is free). What a judge is asked is an editing task - reword a line, move a
  box 20 pt, restyle a phrase - not an aesthetic one, and the free text is the point: it names what
  stands in the way.
- `prompts`: batches of `BATCH` pairs, each with the vocabulary of every form in it (a reader reads
  the .sty once too), scrubbed of anything that says which form is which or who wrote it.
- `report`: the proxy's ordering of a pair against the judges' majority, Spearman between the proxy's
  margin and the judges', and the inter-judge agreement, which is the ceiling for what agreement with
  the proxy can mean.

The proxy score of one frame is `readability.score` over a document holding that frame alone, so
`repeat` (a line found in three frames) is 1.0 on both sides of every pair and never decides one.

The sample, the pairs and the verdicts are TypedDicts: they are written to and read from JSON
(`sample.json`, the judges' answers), and a pair is a `FormPair` or a `HumanPair` by its `kind`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TypedDict, Union

from beamer2slides.json_types import Json, as_objects, as_optional_str, as_str
from beamer2slides.paths import CHECKOUT

from . import readability

CORPUS = Path(os.environ.get("B2S_ADOPT_CORPUS") or CHECKOUT / "out" / "adopt-corpus")
DATA = CHECKOUT / "tests" / "decks" / "foreign" / "readability_calib"
SEED = "readability-calibration-1"
TAGS = ("m6-a", "ls-a")
Kind = Literal["title", "table", "picture", "shape", "list", "prose"]
KINDS: tuple[Kind, ...] = ("title", "table", "picture", "shape", "list", "prose")
# 40 slides: five of every kind so each is represented, the rest to the kinds the corpus is made of
# (of its 909 slides, 424 are picture, 187 prose, 174 list, 43 title, 43 shape, 38 table)
QUOTA: dict[Kind, int] = {"title": 5, "table": 5, "picture": 10, "shape": 6, "list": 7, "prose": 7}  # 40
PER_DECK = 2          # of one kind, from one deck
HUMAN_PAIRS = 20      # of the sampled slides, also set against a frame a person wrote
BATCH = 15
HUMAN_SOURCES = ("tests/decks/*.tex", "tests/decks/sync/talk.tex")
# v2a/b/c are the same talk one edit apart: one of them stands for all (they share every frame)
SKIP_SOURCE = re.compile(r"^sync_smoke_v2")
MAX_LINES = 110       # a frame longer than this is elided, with the count of what was left out

Hand = Literal["left", "right"]
Letter = Literal["A", "B"]
HANDS: tuple[tuple[Hand, Letter], ...] = (("left", "A"), ("right", "B"))


class Wrapped(TypedDict):
    """A frame's body (what the proxy scores) and what wraps it in its file."""
    body: str
    lead: str
    tail: str


class Side(Wrapped):
    """One side of a pair: a wrapped frame and which form wrote it (a tag, or "human")."""
    what: str


class Slide(TypedDict):
    """A corpus slide both tags wrote: its kind and its frame in each."""
    deck: str
    index: int
    kind: Kind
    bodies: dict[str, str]
    frames: dict[str, Wrapped]


class HumanFrame(TypedDict):
    """A frame out of a source a person wrote."""
    source: str
    index: int
    kind: Kind
    body: str


class FormPair(TypedDict):
    """The same slide written by both forms."""
    id: str
    kind: Literal["form"]
    slide_kind: Kind
    deck: str
    index: int
    left: Side
    right: Side


class HumanPair(TypedDict):
    """A slide the newer form wrote against a frame a person wrote."""
    id: str
    kind: Literal["human"]
    slide_kind: Kind
    deck: str
    index: int
    human_source: str
    human_index: int
    left: Side
    right: Side


Pair = Union[FormPair, HumanPair]


class Verdict(TypedDict):
    """What `report` reads of a judge's answer on one pair: "A", "B" or "tie", and why."""
    prefer: str
    reason: str


# judge -> pair id -> verdict
Verdicts = dict[str, dict[str, Verdict]]


class ProxyScore(TypedDict):
    """The proxy's two scores of a pair and the side it prefers."""
    A: float
    B: float
    prefer: str
    margin: float


class Row(TypedDict):
    """One pair the judges voted on, set against the proxy."""
    id: str
    kind: str
    slide_kind: str
    votes: list[str]
    majority: str
    unanimous: bool
    proxy: str
    margin: float
    judge_margin: float
    agree: bool | None


class KindAgreement(TypedDict):
    pairs: int
    agreement: float
    no_majority: int
    by_slide_kind: dict[str, tuple[int, int]]


class Judges(TypedDict):
    n: int
    pairs: int
    unanimous: float
    with_majority: dict[str, float]
    pairwise: float


class Ranking(TypedDict):
    n: int
    spearman: float
    by_form: dict[str, float]
    score_by_form: dict[str, float]
    rate: dict[str, float]
    score: dict[str, float]


class Agreement(TypedDict):
    form: KindAgreement
    human: KindAgreement
    judges: Judges
    spearman_margin: float
    spearman_margin_form: float
    frames: Ranking
    rows: list[Row]


# --------------------------------------------------------------------------------- reading the trees

def _hash(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()


def tree(deck: str, tag: str, corpus: Path) -> Path:
    return corpus / deck / "runs" / tag / "tree"


def decks(corpus: Path, tags: tuple[str, ...]) -> list[str]:
    if not corpus.is_dir():
        return []
    return sorted(p.name for p in corpus.iterdir()
                  if all((tree(p.name, t, corpus) / "main.tex").exists() for t in tags))


LIST_RE = re.compile(r"\\begin\{(itemize|enumerate|description)\}|\\item\b|\\slidebullet\b|\\slidelabel\b")
TITLE_RE = re.compile(r"\\titlepage\b|\\maketitle\b|layout=[\w-]*(title-slide|cover|section)")


def frame_kind(body: str) -> Kind:
    """What a frame is mostly made of, in a vocabulary both forms and a hand-written source share."""
    lines = [ln for ln in body.split("\n") if ln.strip()]
    by: dict[str, int] = {}
    for ln in lines:
        k = readability.construct(ln)
        by[k] = by.get(k, 0) + 1
    n = max(len(lines), 1)
    if by.get("table"):
        return "table"
    if TITLE_RE.search(body) and not by.get("picture") and by.get("shape", 0) <= 1:
        return "title"
    if by.get("picture", 0) >= 2 or (by.get("picture") and by.get("text", 0) <= 3):
        return "picture"
    if by.get("shape", 0) >= max(3, 0.4 * n):
        return "shape"
    if LIST_RE.search(body):
        return "list"
    return "prose"


def frames_with_wrapper(text: str) -> list[Wrapped]:
    """Each frame's body (what the proxy scores) and what wraps it - the lines between the frames,
    which is where one form keeps the page colour the other says in a frame option."""
    body = text.split("\\begin{document}", 1)[-1]
    out: list[Wrapped] = []
    prev = 0
    for m in re.finditer(r"\\begin\{frame\}(.*?)\\end\{frame\}", body, re.S):
        lead = "\n".join(ln for ln in body[prev:m.start()].split("\n")
                         if ln.strip() and not re.fullmatch(r"\s*%.*", ln))
        depth = lead.count("{") - lead.count("}")
        out.append({"body": m.group(1), "lead": lead, "tail": "}" * max(depth, 0)})
        prev = m.end()
    return out


def slides(corpus: Path, tags: tuple[str, ...]) -> list[Slide]:
    """Every slide of the corpus that both tags wrote, with its kind and the two frames."""
    out: list[Slide] = []
    for deck in decks(corpus, tags):
        fs = {t: frames_with_wrapper(readability.tree_source(tree(deck, t, corpus))) for t in tags}
        if len({len(f) for f in fs.values()}) != 1:
            continue  # the two runs disagree on the slides: nothing can be paired
        for i in range(len(fs[tags[0]])):
            here = {t: fs[t][i] for t in tags}
            if any(len(f["body"].strip().split("\n")) < 2 for f in here.values()):
                continue
            out.append({"deck": deck, "index": i, "kind": frame_kind(here[tags[-1]]["body"]),
                        "bodies": {t: here[t]["body"] for t in tags},
                        "frames": here})
    return out


def sample(corpus: Path, seed: str, quota: dict[Kind, int], per_deck: int) -> list[Slide]:
    """The stratified draw: `quota` slides of each kind, ordered by a hash of (seed, deck, index), no
    more than `per_deck` of a kind from one deck while other decks can still fill it."""
    pool = slides(corpus, TAGS)
    picked: list[Slide] = []
    for kind in KINDS:
        want = quota.get(kind, 0)
        cands = sorted((s for s in pool if s["kind"] == kind),
                       key=lambda s: _hash(seed, s["deck"], s["index"]))
        taken: dict[str, int] = {}
        for limit in (per_deck, 10 ** 6):   # a second sweep only if too few decks have this kind
            for s in cands:
                if len([p for p in picked if p["kind"] == kind]) >= want:
                    break
                if s in picked or taken.get(s["deck"], 0) >= limit:
                    continue
                picked.append(s)
                taken[s["deck"]] = taken.get(s["deck"], 0) + 1
    return sorted(picked, key=lambda s: _hash(seed, "order", s["deck"], s["index"]))


def human_frames(root: Path) -> list[HumanFrame]:
    """Frames out of the sources people wrote, as anchors: the scale adopt is placed on."""
    out: list[HumanFrame] = []
    for pattern in HUMAN_SOURCES:
        for path in sorted(root.glob(pattern)):
            if SKIP_SOURCE.match(path.stem):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "%<" in text:            # docstrip guards: the file is a template, not a source
                text = re.sub(r"^%<[^>]*>.*\n?|^%</?\*?[^>]*>.*\n?", "", text, flags=re.M)
            for i, body in enumerate(readability.frames(text)):
                lines = [ln for ln in body.split("\n") if ln.strip()]
                if len(lines) < 4 or len(readability.visible(body).split()) < 6:
                    continue
                out.append({"source": path.stem, "index": i, "kind": frame_kind(body), "body": body})
    return out


# ------------------------------------------------------------------------------------------- pairing

def side_of(what: str, frame: Wrapped) -> Side:
    return {"what": what, "body": frame["body"], "lead": frame["lead"], "tail": frame["tail"]}


def build_pairs(picked: list[Slide], humans: list[HumanFrame], seed: str, human_pairs: int) -> list[Pair]:
    """One `form` pair per sampled slide, and `human_pairs` of them again against a hand-written
    frame. Sides are shuffled by hash, so neither form is always on the left."""
    pairs: list[Pair] = []
    for s in picked:
        pid = f"f{len(pairs) + 1:02d}"
        left_is_old = int(_hash(seed, "side", s["deck"], s["index"])[:8], 16) % 2 == 0
        order = list(TAGS) if left_is_old else list(reversed(TAGS))
        pairs.append({"id": pid, "kind": "form", "slide_kind": s["kind"],
                      "deck": s["deck"], "index": s["index"],
                      "left": side_of(order[0], s["frames"][order[0]]),
                      "right": side_of(order[1], s["frames"][order[1]])})
    # every second slide in the draw's order, so the anchors keep the stratification
    step = max(1, len(picked) // max(human_pairs, 1))
    chosen = picked[::step][:human_pairs]
    used: dict[str, int] = {}
    for s in chosen:
        by_kind = [h for h in humans if h["kind"] == s["kind"]] or humans
        h = min(by_kind, key=lambda h: (used.get(h["source"] + str(h["index"]), 0),
                                        _hash(seed, "anchor", s["deck"], s["index"], h["source"], h["index"])))
        used[h["source"] + str(h["index"])] = used.get(h["source"] + str(h["index"]), 0) + 1
        pid = f"h{len([p for p in pairs if p['kind'] == 'human']) + 1:02d}"
        left_is_adopt = int(_hash(seed, "hside", s["deck"], s["index"])[:8], 16) % 2 == 0
        adopt = side_of(TAGS[-1], s["frames"][TAGS[-1]])
        human: Side = {"what": "human", "body": h["body"], "lead": "", "tail": ""}
        pairs.append({"id": pid, "kind": "human", "slide_kind": s["kind"],
                      "deck": s["deck"], "index": s["index"],
                      "human_source": h["source"], "human_index": h["index"],
                      "left": adopt if left_is_adopt else human,
                      "right": human if left_is_adopt else adopt})
    return pairs


# ------------------------------------------------------------------------------------------- prompts

# a comment naming the writer says which form this is: the whole comment goes
DROP = re.compile(r"beamer2slides|\badopt\w*\b|slides\.sty|adopt-corpus|\bm6-a\b|\bls-a\b", re.I)
# the deck the source came from is named in the documentation; the name says nothing about the form,
# but it says what the source is *of*, which a judge has no business knowing
RENAME = ((re.compile(r"Google Slides'|\bSlides'"), "the deck's"),
          (re.compile(r"Google Slides|\bSlides\b"), "the deck"))


def scrub(text: str) -> str:
    """Take out what would say which form a reader is looking at, or who wrote it. Only comments are
    touched (the code never mentions either), and a comment that cannot be cleaned is dropped."""
    kept: list[str] = []
    for ln in text.split("\n"):
        at = re.search(r"(?<!\\)%", ln)      # an escaped \% is not a comment
        code, comment = (ln[:at.start()], ln[at.start():]) if at else (ln, "")
        if comment and DROP.search(comment):
            if not code.strip():
                continue
            comment = ""
        for pat, to in RENAME:
            comment = pat.sub(to, comment)
        kept.append((code + comment).rstrip())
    return "\n".join(kept)


# a public name only: `\slides@keys` and the rest of a package's insides are not vocabulary
PUBLIC = re.compile(r"^\\(?:newcommand|renewcommand|providecommand|def|DeclareDocumentCommand)\s*\\?\{?"
                    r"\\((?:slide|layout|with|def|draw|set)[A-Za-z]*)(?![A-Za-z@])|"
                    r"^\\newenvironment\{(slide[a-z]+)\}")
# a .sty holds its own internals; main.tex's preamble defines only what its frames use, so all of it
ANY_DEF = re.compile(r"^\\(?:newcommand|renewcommand|providecommand|def|newenvironment|"
                     r"DeclareDocumentCommand|newlength|newcount|newdimen|newif|newsavebox)"
                     r"(?![A-Za-z])\s*\\?\{?\\?([A-Za-z@]+)")


def vocabulary_entries(form: str, deck: str, corpus: Path) -> list[tuple[str, str]]:
    """What a reader is handed once: every macro a form defines, with the comment written above it,
    as (name, text). The implementation is left out - a macro is read as `\\includegraphics` is read,
    by its signature and what the comment says it takes."""
    folder = tree(deck, form, corpus)
    preamble = readability.tree_source(folder).split("\\begin{document}", 1)[0]
    texts = [preamble] + [p.read_text(encoding="utf-8", errors="replace") for p in sorted(folder.glob("*.sty"))]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for n, text in enumerate(texts):
        block: list[str] = []
        for ln in scrub(text).split("\n"):
            if ln.lstrip().startswith("%"):
                block.append(ln)
                continue
            head = ln.strip()
            m = PUBLIC.match(head) or (ANY_DEF.match(head) if n == 0 else None)
            if m or re.match(r"^\\(newif\\if[a-z]+$|AddToHook|define@key\{beamerframe\})", head):
                name = next((g for g in (m.groups() if m else ()) if g), head[:24])
                sig = re.sub(r"\{%?$", "{...}", ln.rstrip()) if ln.rstrip().endswith("{") else ln.rstrip()
                entry = "\n".join(block + [sig])
                if entry not in seen:
                    seen.add(entry)
                    out.append((name, entry))
                block = []
            elif not head:      # a comment stands above the next definition, not above the next line
                block = []
    return out


def vocabulary_reference(form: str, decks_here: list[str], corpus: Path) -> str:
    """The vocabulary of one form over the decks of a batch: the same macros, said once."""
    if form == "human":
        return ("Plain beamer and LaTeX: the `frame`, `itemize`, `tabular`, `tikzpicture` and\n"
                "`block` environments, `\\includegraphics`, `\\textbf`, `\\alert`, and so on.\n"
                "No macros of its own.\n")
    out: list[str] = []
    seen: set[str] = set()
    for deck in decks_here:
        for _, entry in vocabulary_entries(form, deck, corpus):
            if entry in seen:
                continue
            seen.add(entry)
            out.append(entry)
    return "\n".join(out).strip() + "\n"


PREAMBLE_DEF = re.compile(r"^\\(slidestyle|slidemark|definecolor|setslide\w+)\{?")


def deck_preamble(form: str, deck: str, body: str, corpus: Path) -> str:
    """The lines of that deck's preamble the frame leans on: the named styles, colours and defaults
    it uses. A person editing the frame would look them up there; nobody would read the rest."""
    if form == "human":
        return ""
    preamble = readability.tree_source(tree(deck, form, corpus)).split("\\begin{document}", 1)[0]
    lines = [ln.strip() for ln in scrub(preamble).split("\n") if PREAMBLE_DEF.match(ln.strip())]
    defaults = [ln for ln in lines if ln.startswith("\\setslide")]
    wanted = set(re.findall(r"[\w@.-]+", body + " " + " ".join(defaults)))
    out = list(defaults)
    for _ in range(2):   # a kept style names a colour, which must be kept too
        for ln in lines:
            m = re.match(r"^\\(?:slidestyle|slidemark|definecolor)\{([^}]*)\}", ln)
            if ln in out or not m or m.group(1) not in wanted:
                continue
            out.append(ln)
            wanted |= set(re.findall(r"[\w@.-]+", ln))
    return "\n".join(ln for ln in lines if ln in out)


def show(side: Wrapped, max_lines: int) -> str:
    """The frame as it stands in its file, wrapper and all. A frame too long to read whole is elided
    in the middle, with the number of lines left out said: its length is part of what is judged."""
    text = (side["lead"] + "\n" + "\\begin{frame}" + side["body"].rstrip()
            + "\n\\end{frame}\n" + side["tail"])
    lines = text.strip("\n").split("\n")
    if len(lines) <= max_lines:
        return "\n".join(lines)
    head = max_lines - 12
    return "\n".join(lines[:head] + [f"    [... {len(lines) - max_lines} further lines of the same "
                                     f"forms as above ...]"] + lines[-12:])


TASK = """You are handed the LaTeX source of a slide deck and asked to make three edits to one slide:

1. change the wording of one line of its text;
2. move one box 20 pt to the right;
3. restyle a phrase in it (bold, or another colour).

Below are pairs of sources. In each pair you see two sources, "A" and "B". For each pair, say which
of the two you would rather be handed to do that job, and - this is the part that matters - what
stands in the way in each of them. Be concrete: name the line or the construct that would slow you
down, or the one that would make it quick.

There is no right answer and no trick: some pairs show the same slide written in two ways, some show
two different slides from two different talks. Judge the source you would have to edit, not how
pretty the slide would look. If they are genuinely equal, say so.

Each source is shown as the body of one `frame`. The macros a source uses are defined once, at the
top, under "Vocabulary": read that first, as you would read a package's documentation once.

Answer with JSON and nothing else: a list with one object per pair, in the order the pairs are given:

[{"pair": "<the pair's id>",
  "prefer": "A" | "B" | "tie",
  "confidence": 1 | 2 | 3,
  "reason": "<one or two sentences: why that one>",
  "in_the_way_A": "<what would slow you down in A>",
  "in_the_way_B": "<what would slow you down in B>"}]
"""


def prompt(batch: list[Pair], corpus: Path) -> str:
    """One batch's prompt: the task, the vocabulary of every form in it (said once, as a reader reads
    a package once), then the pairs, each source with the preamble lines it leans on."""
    forms: dict[str, list[str]] = {}
    for p in batch:
        for side, _ in HANDS:
            forms.setdefault(p[side]["what"], [])
            if p["deck"] not in forms[p[side]["what"]]:
                forms[p[side]["what"]].append(p["deck"])
    names = {form: f"V{n}" for n, form in enumerate(sorted(forms, key=lambda f: list(forms).index(f)), 1)}
    out = [TASK, "\n## Vocabulary\n",
           "Each source below names the vocabulary it is written in. Read that section once. Only each\n"
           "macro's first line and the comment written above it are shown - the bodies are left out, as\n"
           "you would not read a package's implementation to use it.\n"]
    for form, decks_here in forms.items():
        out.append(f"### Vocabulary {names[form]}\n\n```latex\n"
                   f"{vocabulary_reference(form, decks_here, corpus)}```\n")
    out.append("\n## The pairs\n")
    for p in batch:
        note = ("The two sources are the same slide written two ways."
                if p["kind"] == "form" else "The two sources are different slides from different talks.")
        out.append(f"\n### Pair {p['id']}\n\n{note}\n")
        for side, letter in HANDS:
            pre = deck_preamble(p[side]["what"], p["deck"], p[side]["body"], corpus)
            out.append(f"\n**{p['id']} source {letter}** (vocabulary {names[p[side]['what']]})\n")
            if pre:
                out.append(f"\nIts deck's preamble, the part this frame leans on:\n\n```latex\n{pre}\n```\n")
            out.append(f"\nThe frame:\n\n```latex\n{show(p[side], MAX_LINES)}\n```\n")
    out.append("\nAnswer with the JSON list described above, one object per pair, nothing else.\n")
    return "\n".join(out)


def batches(pairs: list[Pair], size: int) -> list[list[Pair]]:
    return [pairs[i:i + size] for i in range(0, len(pairs), size)]


# --------------------------------------------------------------------------------------- the numbers

def frame_measure(body: str, known: set[str]) -> readability.Measured | None:
    doc = "\\begin{document}\n\\begin{frame}" + body + "\\end{frame}\n"   # the frame as it stood
    return readability.measure(doc, known, "")


def frame_score(body: str, known: set[str]) -> float:
    m = frame_measure(body, known)
    return readability.score(m) if m else 0.0


def frame_components(body: str, known: set[str]) -> dict[readability.Component, float]:
    m = frame_measure(body, known)
    return readability.components(m) if m else {}


def side_vocabulary(pair: Pair, side: Hand, corpus: Path) -> set[str]:
    """The vocabulary a side's form defines (none for a hand-written frame)."""
    form = pair[side]["what"]
    return set() if form == "human" else readability.tree_vocabulary(tree(pair["deck"], form, corpus))


def proxy(pairs: list[Pair], corpus: Path) -> dict[str, ProxyScore]:
    """Each pair's two scores and which side the proxy prefers."""
    out: dict[str, ProxyScore] = {}
    for p in pairs:
        sides: dict[Hand, float] = {}
        for side, _ in HANDS:
            sides[side] = frame_score(p[side]["body"], side_vocabulary(p, side, corpus))
        best = "tie" if abs(sides["left"] - sides["right"]) < 1e-9 else (
            "A" if sides["left"] > sides["right"] else "B")
        out[p["id"]] = {"A": sides["left"], "B": sides["right"], "prefer": best,
                        "margin": sides["left"] - sides["right"]}
    return out


def _vote(v: str) -> float:
    return {"A": 1.0, "B": -1.0}.get(v, 0.0)


def majority(votes: list[str]) -> str:
    s = sum(_vote(v) for v in votes)
    return "tie" if abs(s) < 1e-9 else ("A" if s > 0 else "B")


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    def ranks(vs: Sequence[float]) -> list[float]:
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        r = [0.0] * len(vs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            mean = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = mean
            i = j + 1
        return r
    if len(xs) < 3:
        return float("nan")
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else float("nan")


def _kind_agreement(rows: list[Row], kind: str) -> KindAgreement:
    sub = [r for r in rows if r["kind"] == kind and r["agree"] is not None]
    return {"pairs": len(sub),
            "agreement": (sum(1 for r in sub if r["agree"]) / len(sub)) if sub else float("nan"),
            "no_majority": len([r for r in rows if r["kind"] == kind and r["agree"] is None]),
            "by_slide_kind": {k: (sum(1 for r in sub if r["slide_kind"] == k and r["agree"]),
                                  len([r for r in sub if r["slide_kind"] == k]))
                              for k in KINDS if any(r["slide_kind"] == k for r in sub)}}


def agreement(pairs: list[Pair], verdicts: Verdicts, scores: dict[str, ProxyScore]) -> Agreement:
    """`verdicts` is judge -> pair id -> verdict. Everything the calibration claims is computed here."""
    by_id = {p["id"]: p for p in pairs}
    judges = sorted(verdicts)
    rows: list[Row] = []
    for pid, p in by_id.items():
        votes = [verdicts[j][pid]["prefer"] for j in judges if pid in verdicts.get(j, {})]
        if not votes:
            continue
        maj = majority(votes)
        rows.append({"id": pid, "kind": p["kind"], "slide_kind": p["slide_kind"],
                     "votes": votes, "majority": maj, "unanimous": len(set(votes)) == 1,
                     "proxy": scores[pid]["prefer"], "margin": scores[pid]["margin"],
                     "judge_margin": sum(_vote(v) for v in votes) / max(len(votes), 1),
                     "agree": None if maj == "tie" else maj == scores[pid]["prefer"]})
    form, human = _kind_agreement(rows, "form"), _kind_agreement(rows, "human")
    # inter-judge: how often all three said the same, and how each one stands to the majority
    triples = [r for r in rows if len(r["votes"]) >= 3]
    judged: Judges = {
        "n": len(judges), "pairs": len(triples),
        "unanimous": (sum(r["unanimous"] for r in triples) / len(triples)) if triples else float("nan"),
        "with_majority": {j: _with_majority(j, by_id, verdicts) for j in judges},
        "pairwise": _pairwise(judges, verdicts)}
    have = [r for r in rows if r["id"] in scores]
    everything = spearman([scores[r["id"]]["margin"] for r in have], [r["judge_margin"] for r in have])
    same_slide = [r for r in have if r["kind"] == "form"]
    forms = spearman([scores[r["id"]]["margin"] for r in same_slide], [r["judge_margin"] for r in same_slide])
    return {"form": form, "human": human, "judges": judged,
            "spearman_margin": everything, "spearman_margin_form": forms,
            "frames": frame_ranking(pairs, verdicts, scores), "rows": rows}


def _with_majority(judge: str, by_id: dict[str, Pair], verdicts: Verdicts) -> float:
    n = hit = 0
    for pid in by_id:
        others = [verdicts[j][pid]["prefer"] for j in verdicts if j != judge and pid in verdicts[j]]
        said = verdicts.get(judge, {}).get(pid)
        mine = said["prefer"] if said else None
        if mine is None or not others:
            continue
        maj = majority(others)
        if maj == "tie":
            continue
        n += 1
        hit += mine == maj
    return hit / n if n else float("nan")


def _pairwise(judges: list[str], verdicts: Verdicts) -> float:
    n = hit = 0
    for i, a in enumerate(judges):
        for b in judges[i + 1:]:
            for pid in set(verdicts[a]) & set(verdicts[b]):
                n += 1
                hit += verdicts[a][pid]["prefer"] == verdicts[b][pid]["prefer"]
    return hit / n if n else float("nan")


def frame_ranking(pairs: list[Pair], verdicts: Verdicts, scores: dict[str, ProxyScore]) -> Ranking:
    """Every frame's share of judge votes against its proxy score: the Spearman over frames, which
    puts the hand-written anchors on the same scale as the two forms."""
    won: dict[str, list[float]] = {}
    sc: dict[str, float] = {}
    for p in pairs:
        for side, letter in HANDS:
            key = _frame_key(p, side)
            sc[key] = scores[p["id"]][letter]
            for j in verdicts:
                v = verdicts[j].get(p["id"])
                if v:
                    won.setdefault(key, []).append(1.0 if v["prefer"] == letter
                                                   else (0.5 if v["prefer"] == "tie" else 0.0))
    keys = sorted(won)
    rate = {k: sum(won[k]) / len(won[k]) for k in keys}
    return {"n": len(keys), "spearman": spearman([sc[k] for k in keys], [rate[k] for k in keys]),
            "by_form": {f: _mean([rate[k] for k in keys if k.startswith(f + "/")]) for f in
                        (TAGS[0], TAGS[1], "human")},
            "score_by_form": {f: _mean([sc[k] for k in keys if k.startswith(f + "/")]) for f in
                              (TAGS[0], TAGS[1], "human")},
            "rate": rate, "score": sc}


def _frame_key(pair: Pair, side: Hand) -> str:
    what = pair[side]["what"]
    if what == "human":
        if pair["kind"] != "human":
            raise ValueError(f"pair {pair['id']}: a hand-written side in a {pair['kind']} pair")
        return f"human/{pair['human_source']}/{pair['human_index']}"
    return f"{what}/{pair['deck']}/{pair['index']}"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


# ----------------------------------------------------------------------------------------- the files

def sample_json(seed: str, picked: list[Slide], pairs: list[Pair]) -> str:
    """sample.json: the draw and each pair without its frames (a side is named by its form)."""
    return json.dumps(
        {"seed": seed, "tags": list(TAGS), "quota": QUOTA, "per_deck": PER_DECK,
         "slides": [{"deck": s["deck"], "index": s["index"], "kind": s["kind"]} for s in picked],
         "pairs": [{k: v for k, v in p.items() if k not in ("left", "right")} |
                   {"left": p["left"]["what"], "right": p["right"]["what"]} for p in pairs]},
        indent=1)


def build(out: Path, corpus: Path, seed: str) -> list[Pair]:
    picked = sample(corpus, seed, QUOTA, PER_DECK)
    pairs = build_pairs(picked, human_frames(CHECKOUT), seed, HUMAN_PAIRS)
    out.mkdir(parents=True, exist_ok=True)
    (out / "sample.json").write_text(sample_json(seed, picked, pairs), encoding="utf-8")
    folder = out / "prompts"
    folder.mkdir(exist_ok=True)
    for old in folder.glob("batch-*.md"):
        old.unlink()
    for i, batch in enumerate(batches(pairs, BATCH), 1):
        (folder / f"batch-{i:02d}.md").write_text(prompt(batch, corpus), encoding="utf-8")
    return pairs


def rebuild_pairs(corpus: Path, seed: str) -> list[Pair]:
    return build_pairs(sample(corpus, seed, QUOTA, PER_DECK), human_frames(CHECKOUT), seed, HUMAN_PAIRS)


def load_verdicts(folder: Path) -> Verdicts:
    """Every judge's answers (`<judge>-batch-NN.json`), read where they enter: a pair id, a vote and
    the reason given (none reads as "")."""
    out: Verdicts = {}
    for path in sorted(folder.glob("*.json")):
        answers: Json = json.loads(path.read_text(encoding="utf-8"))
        judge = path.stem.split("-batch")[0]
        for i, row in enumerate(as_objects(answers, path.name)):
            where = f"{path.name}[{i}]"
            out.setdefault(judge, {})[as_str(row.get("pair"), f"{where}.pair")] = {
                "prefer": as_str(row.get("prefer"), f"{where}.prefer"),
                "reason": as_optional_str(row.get("reason"), f"{where}.reason") or ""}
    return out


def report(data: Path, corpus: Path, seed: str, verbose: bool) -> Agreement | None:
    pairs = rebuild_pairs(corpus, seed)
    scores = proxy(pairs, corpus)
    verdicts = load_verdicts(data / "verdicts")
    if not verdicts:
        print(f"no verdicts under {data / 'verdicts'}")
        return None
    a = agreement(pairs, verdicts, scores)
    print(f"{len(pairs)} pairs, {a['judges']['n']} judges\n")
    for kind, k in (("form", a["form"]), ("human", a["human"])):
        print(f"{kind:<6} pairs {k['pairs']:3}  proxy agrees with the judges' majority "
              f"{k['agreement']:.2f}   (no majority: {k['no_majority']})")
        if k["by_slide_kind"]:
            print("       " + "  ".join(f"{name} {hit}/{n}"
                                        for name, (hit, n) in k["by_slide_kind"].items()))
    j = a["judges"]
    print(f"\ninter-judge  all three agree {j['unanimous']:.2f}   pairwise {j['pairwise']:.2f}")
    for name, v in sorted(j["with_majority"].items()):
        print(f"   {name:<10} with the other two {v:.2f}")
    print(f"\nSpearman (proxy margin vs judge margin): all {a['spearman_margin']:.2f}, "
          f"form pairs {a['spearman_margin_form']:.2f}")
    f = a["frames"]
    print(f"Spearman over {f['n']} frames (score vs share of votes won): {f['spearman']:.2f}")
    print("   " + ", ".join(f"{k}: score {f['score_by_form'][k]:.2f} wins {f['by_form'][k]:.2f}"
                            for k in f["by_form"] if f["by_form"][k] == f["by_form"][k]))
    by_id = {p["id"]: p for p in pairs}
    bad = [r for r in a["rows"] if r["agree"] is False]
    print(f"\n{len(bad)} pairs where the proxy and the judges' majority disagree:")
    for r in sorted(bad, key=lambda r: -abs(r["margin"])):
        p = by_id[r["id"]]
        print(f"   {r['id']} {r['kind']:<5} {r['slide_kind']:<7} {p['deck']:<20} "
              f"judges {''.join(v[0] for v in r['votes'])} -> {r['majority']}, "
              f"proxy {r['proxy']} by {abs(r['margin']):.3f}")
        if verbose:
            for side, letter in HANDS:
                c = frame_components(p[side]["body"], side_vocabulary(p, side, corpus))
                print(f"      {letter} {p[side]['what']:<6} " +
                      " ".join(f"{k} {v:.2f}" for k, v in c.items()))
            for judge, v in sorted(verdicts.items()):
                if r["id"] in v:
                    print(f"      {judge}: {v[r['id']]['reason']}")
    return a


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="draw the sample and write the judge prompts")
    sub.add_parser("sample", help="print the drawn slides")
    r = sub.add_parser("report", help="agreement and correlation from the verdicts")
    r.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        pairs = build(DATA, CORPUS, SEED)
        print(f"{len(pairs)} pairs in {len(batches(pairs, BATCH))} batches -> {DATA}")
    elif args.cmd == "sample":
        for s in sample(CORPUS, SEED, QUOTA, PER_DECK):
            print(f"{s['kind']:<8} {s['deck']:<22} slide {s['index'] + 1}")
    else:
        report(DATA, CORPUS, SEED, args.verbose)


if __name__ == "__main__":
    main(sys.argv[1:])
