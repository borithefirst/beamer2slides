"""The inverse problem: edit a beamer source until its conversion matches a target IR.

    converge(tex, target, work)  ->  Result (edited files, iterations, unresolved residuals)

Each iteration compiles a working copy of the source (SyncTeX on), extracts and classifies the PDF
like `convert` does, maps slides to frames (texmap), compares with the target (compare) and turns
residuals into source edits (translators below). Structural edits (slides) go first; text,
styles, lists and placement follow; positions are corrected from the measured error until
everything is within tolerance or nothing more can be done. What can't be translated goes into
the report with its context.

Translators (residual -> Edit):
  slide_missing   new \\begin{frame}[label=..]{title} with flow text, lists and pictures
  slide_extra     frame deleted
  slide_order     frame moved after its target predecessor
  notes           \\note{} replaced, added or removed
  text            word-level replace in the mapped source span (commands, braces, math untouched)
  style           \\textbf, \\emph, \\underline, \\texttt, \\textcolor (+ \\definecolor), size switches;
                  wrap, unwrap or split the enclosing group
  lists           paragraph_missing/extra/order and bullet residuals in a list: the list environment
                  rebuilt from its items' own source (added items from the target text)
  paragraphs      plain paragraph inserted after its predecessor, or deleted
  geometry        flow content that must move goes into a textblock* (textpos) at page coordinates,
                  its flow room kept by \\vspace; textblocks are corrected by the measured error;
                  flow pictures change width first
  element_missing positioned textblock with the text (lists) or \\includegraphics of the picture
  element_extra   pictures deleted
  background      \\setbeamercolor{background canvas} in a group around the frame
"""

import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .compare import (HOLE, TOL, Comparison, Para, char_styles, compare, norm_text, para_text, picture_hash,
                      residual_line, slide_paragraphs, slide_title, text_anchor)
from .texmap import (OPAQUE, PARA, Frame, Item, ListEnv, Source, Visible, WordMap, build_visible, frame_visible,
                     line_of, locate_words, mask_comments, match_group, page_frames, read_args, skip_space,
                     synctex_pages)

MIKTEX_BIN = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64"
SKIP_DIRS = {"out", ".git", ".venv", "build", "__pycache__", "node_modules", ".b2s"}
BUILD_EXT = {".aux", ".log", ".nav", ".snm", ".toc", ".out", ".vrb", ".fls", ".fdb_latexmk"}
TEXTPOS = r"\usepackage[absolute,overlay]{textpos}"
TEXTBLOCK_RE = re.compile(r"\\begin\{textblock\*\}\{(?P<w>[-\d.]+)pt\}\((?P<x>[-\d.]+)pt,\s*(?P<y>[-\d.]+)pt\)")
SIZE_TABLES = {  # beamer's font size switches by class option
    10: {"tiny": 5, "scriptsize": 7, "footnotesize": 8, "small": 9, "normalsize": 10, "large": 12,
         "Large": 14.4, "LARGE": 17.28, "huge": 20.74, "Huge": 24.88},
    11: {"tiny": 6, "scriptsize": 8, "footnotesize": 9, "small": 10, "normalsize": 10.95, "large": 12,
         "Large": 14.4, "LARGE": 17.28, "huge": 20.74, "Huge": 24.88},
    12: {"tiny": 6, "scriptsize": 8, "footnotesize": 10, "small": 10.95, "normalsize": 12, "large": 14.4,
         "Large": 17.28, "LARGE": 20.74, "huge": 24.88, "Huge": 24.88},
}
NAMED_COLOURS = {"#ff0000": "red", "#00ff00": "green", "#0000ff": "blue", "#000000": "black", "#ffffff": "white",
                 "#00ffff": "cyan", "#ff00ff": "magenta", "#ffff00": "yellow", "#808080": "gray",
                 "#404040": "darkgray", "#bfbfbf": "lightgray", "#ff8000": "orange", "#800080": "violet",
                 "#bf0040": "purple", "#bf8040": "brown", "#008080": "teal", "#808000": "olive"}
RESHAPING = {"paragraph_missing", "paragraph_extra", "paragraph_order", "bullet", "element_missing", "element_extra",
             "image"}
ADDITIVE = {"paragraph_missing", "element_missing", "slide_missing"}
STYLE_CMDS = {
    "bold": (("textbf", "bfseries", "alert"), "textbf", "textmd"),
    "italic": (("emph", "textit", "itshape", "em", "textsl", "slshape"), "emph", "textup"),
    "underline": (("underline", "uline", "ul"), "underline", None),
    "mono": (("texttt", "ttfamily"), "texttt", None),
}


def tex_env() -> dict:
    env = dict(os.environ)
    if MIKTEX_BIN.is_dir() and str(MIKTEX_BIN) not in env.get("PATH", ""):
        env["PATH"] = f"{MIKTEX_BIN};{env.get('PATH', '')}"
    return env


# ---------------------------------------------------------------- LaTeX writing helpers

ESCAPE = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
          "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "\u00a0": "~", "\x0b": r"\\ ",
          "\t": " "}


def latex_escape(text: str) -> str:
    return "".join(ESCAPE.get(c, c) for c in text)


def colour_name(hex_colour: str, defined: dict[str, str]) -> str:
    h = hex_colour.lower()
    if h in NAMED_COLOURS:
        return NAMED_COLOURS[h]
    name = "b2s" + h.lstrip("#").upper()
    defined[name] = h.lstrip("#").upper()
    return name


def size_switch(size: float, pt_option: int) -> str:
    table = SIZE_TABLES.get(pt_option, SIZE_TABLES[11])
    name, got = min(table.items(), key=lambda kv: abs(math.log(kv[1] / size)))
    if abs(got - size) <= 0.04 * size:
        return "\\" + name
    return f"\\fontsize{{{size:.1f}}}{{{1.2 * size:.1f}}}\\selectfont"


def runs_latex(runs: list[dict], base: dict, ctx: "Context") -> str:
    """LaTeX for styled runs, styles written relative to `base` (size, colour, bold, italic)."""
    out = []
    for r in runs:
        if r.get("hole"):
            continue
        text = latex_escape(r["text"])
        if not text:
            continue
        lead = len(text) - len(text.lstrip(" "))
        trail = len(text) - len(text.rstrip(" "))
        core = text.strip(" ")
        if core:
            if r.get("family") == "mono" and base.get("family") != "mono":
                core = f"\\texttt{{{core}}}"
            if r.get("bold") and not base.get("bold"):
                core = f"\\textbf{{{core}}}"
            if r.get("italic") and not base.get("italic"):
                core = f"\\emph{{{core}}}"
            if r.get("underline"):
                core = f"\\underline{{{core}}}"
            if r.get("script") == "super":
                core = f"\\textsuperscript{{{core}}}"
            elif r.get("script") == "sub":
                core = f"\\textsubscript{{{core}}}"
            if r.get("color") and base.get("color") and compare_colour(r["color"], base["color"]):
                core = f"\\textcolor{{{colour_name(r['color'], ctx.colours)}}}{{{core}}}"
            if r.get("size") and base.get("size") and abs(r["size"] - base["size"]) > 0.06 * base["size"]:
                core = f"{{{size_switch(r['size'], ctx.pt_option)} {core}}}"
            if r.get("link") and not str(r["link"]).startswith("#"):
                core = f"\\href{{{r['link']}}}{{{core}}}"
        out.append(" " * lead + core + " " * trail)
    return "".join(out)


def compare_colour(a: str, b: str) -> bool:
    from .compare import colour_distance
    return colour_distance(a, b) > TOL["color"]


# ---------------------------------------------------------------- edits

@dataclass
class Edit:
    file: Path
    start: int
    end: int
    text: str
    kind: str
    signature: tuple
    note: str = ""


@dataclass
class Context:
    colours: dict[str, str] = field(default_factory=dict)   # \definecolor names to add
    packages: set[str] = field(default_factory=set)          # preamble lines to add
    pt_option: int = 11
    files: dict[str, Path] = field(default_factory=dict)     # picture copies: source path -> path in the work tree


@dataclass
class ParaLoc:
    file: Path
    visible: Visible
    words: WordMap
    lo: int          # visible range of the paragraph's found words
    hi: int
    item: Item | None
    title: bool = False

    def src(self) -> tuple[int, int]:
        return self.visible.starts[self.lo], self.visible.ends[self.hi - 1]


# ---------------------------------------------------------------- candidate build

@dataclass
class Candidate:
    source: Source
    pdf: Path
    deck: dict
    frames: list[Frame | None]            # per deck slide
    locs: dict[int, dict] = field(default_factory=dict)
    text_masked: dict[Path, str] = field(default_factory=dict)

    def masked(self, path: Path) -> str:
        if path not in self.text_masked:
            self.text_masked[path] = mask_comments(self.source.text(path))
        return self.text_masked[path]

    def paragraph_locations(self, si: int) -> dict[tuple[str, int], ParaLoc]:
        """(element id, paragraph index) -> where its words are in the source."""
        if si in self.locs:
            return self.locs[si]
        out: dict[tuple[str, int], ParaLoc] = {}
        frame = self.frames[si]
        self.locs[si] = out
        if frame is None:
            return out
        vis = frame_visible(self.source, frame)
        pre = title_page_visible(self)
        pos = 0
        for para in slide_paragraphs(self.deck["slides"][si]):
            candidates = []
            is_title = para.el.get("role") == "title"
            if is_title and vis.title and not self.deck["slides"][si].get("title_page"):
                candidates.append((frame.file, vis, vis.title[0], vis.title[1]))
            candidates += [(frame.file, vis, pos, len(vis.text)), (frame.file, vis, 0, len(vis.text))]
            if pre is not None:
                candidates.append((self.source.main, pre, 0, len(pre.text)))
            best = None
            for path, v, lo, hi in candidates:
                wm = locate_words(para.text, v, lo, hi)
                if best is None or wm.score > best[2].score + 1e-9:
                    best = (path, v, wm)
                if wm.score >= 0.8:
                    break
            path, v, wm = best
            found = [g for g in wm.vis if g is not None]
            if wm.score < 0.34 or not found:
                continue
            lo, hi = found[0][0], found[-1][1]
            item = None
            if v is vis:
                src = v.starts[lo]
                inside = [it for it in v.items if it.start <= src < max(it.end, it.body)]
                item = max(inside, key=lambda it: it.level) if inside else None
                pos = hi
            out[(para.el["id"], para.pi)] = ParaLoc(path, v, wm, lo, hi, item, is_title)
        return out


def title_page_visible(cand: Candidate) -> Visible | None:
    """Printed text of \\title, \\subtitle, \\author, \\institute and \\date in the preamble."""
    text = cand.masked(cand.source.main)
    end = cand.source.preamble_end()
    out = Visible()
    for name in ("title", "subtitle", "author", "institute", "date"):
        m = re.search(r"\\" + name + r"\s*(?=[\[{])", text[:end])
        if not m:
            continue
        args, _ = read_args(text, m.end(), "oM")
        if args[1]:
            part = build_visible(text, args[1][1], args[1][2])
            for ch, a, b in zip(part.text, part.starts, part.ends):
                out.add(ch, a, b)
            out.add(PARA, args[1][2], args[1][2])
    return out if out.text else None


class Workspace:
    """A working copy of the source tree, compiled and classified on demand."""

    def __init__(self, tex: Path, work: Path, handout: bool = False, engine: str | None = None,
                 fresh: bool = True):
        self.original = Path(tex).resolve()
        self.root = self.original.parent
        self.work = Path(work).resolve()
        self.src = self.work / "src"
        self.build_dir = self.work / "build"
        if fresh and self.src.exists():
            shutil.rmtree(self.src)
        if not self.src.exists():
            copy_tree(self.root, self.src)
        self.build_dir.mkdir(parents=True, exist_ok=True)
        self.main = self.src / self.original.name
        self.handout = handout
        self.engine = engine
        self.notes = False
        self.source = Source(self.main)

    def reload(self) -> None:
        self.source = Source(self.main)

    def write(self, edits: list[Edit]) -> list[Edit]:
        """Apply non-overlapping edits (first come first served); returns the applied ones."""
        applied: list[Edit] = []
        taken: dict[Path, list[tuple[int, int]]] = {}
        for e in edits:
            spans = taken.setdefault(e.file, [])
            if any((e.start < b and e.end > a) or (e.start == e.end and a < e.start < b) for a, b in spans):
                continue
            if e.start == e.end and any(a == b == e.start for a, b in spans):
                continue
            spans.append((e.start, e.end))
            applied.append(e)
        for path in {e.file for e in applied}:
            text = self.source.text(path)
            for e in sorted((e for e in applied if e.file == path), key=lambda e: (e.start, e.end), reverse=True):
                text = text[:e.start] + e.text + text[e.end:]
            path.write_text(text, encoding="utf-8", newline="")
            self.source.set_text(path, text)
        self.reload()
        return applied

    def compile(self) -> tuple[Path | None, str]:
        engine = self.engine or self.source.engine()
        prefix = ""
        if self.handout:
            prefix += r"\PassOptionsToClass{handout}{beamer}"
        if self.notes:
            prefix += r"\PassOptionsToClass{notes=show}{beamer}"
        job = self.main.stem
        rel = self.main.relative_to(self.src).as_posix()
        exe = shutil.which(engine, path=tex_env()["PATH"]) or engine
        cmd = [exe,"-interaction=nonstopmode", "-halt-on-error", "-synctex=1", "-file-line-error",
               f"-jobname={job}", f"-output-directory={self.build_dir}", prefix + r"\input{" + rel + "}"]
        log = ""
        for attempt in range(3):
            r = subprocess.run(cmd, cwd=self.src, capture_output=True, text=True, errors="replace", env=tex_env())
            log_path = self.build_dir / f"{job}.log"
            log = log_path.read_text(errors="replace") if log_path.exists() else r.stdout
            if r.returncode != 0:
                return None, error_excerpt(log)
            if not re.search(r"Rerun to get|may have changed\. Rerun|\(rerunfilecheck\)", log) or attempt == 2:
                break
        return self.build_dir / f"{job}.pdf", ""

    def build(self, out: Path, target_has_notes: bool = False) -> Candidate | str:
        """Compile, extract and classify like `convert` (overlays: last step of each frame), and
        map every slide to its frame. A string is a compile error."""
        from .classify import classify
        from .extract import extract, select_overlays
        from .notes import prepare as prepare_notes

        self.notes = target_has_notes
        pdf, err = self.compile()
        if pdf is None:
            return err
        out.mkdir(parents=True, exist_ok=True)
        prepared = prepare_notes(pdf, out)
        raw = extract(prepared.pdf, prepared.labels)
        for page in raw["pages"]:
            page["notes"] = prepared.notes.get(page["index"])
        kept_original = original_pages(pdf, prepared)
        selected = select_overlays(raw, "last")
        deck = classify(selected)
        sync = synctex_pages(self.build_dir / f"{self.main.stem}.synctex.gz")
        frames_by_page = page_frames(self.source, sync, [p["label"] for p in raw["pages"]], self.src)
        frames = []
        for slide in deck["slides"]:
            orig = kept_original[slide["page"]] if slide["page"] < len(kept_original) else slide["page"]
            frame = frames_by_page[orig] if orig < len(frames_by_page) else None
            frames.append(frame)
            slide["key"] = frame.label if frame and frame.label else None
            slide["frame_index"] = frame.index if frame else None
        return Candidate(self.source, prepared.pdf, deck, frames)


def original_pages(pdf: Path, prepared) -> list[int]:
    """Page index in the compiled PDF of each page of the notes-free PDF."""
    if prepared.mode != "note pages":
        return list(range(10 ** 5))
    from .extract import spans as page_spans
    from .notes import _note_header
    from .pdf import Document
    doc = Document(pdf)
    try:
        keep = []
        for page in doc:
            spans = page_spans(page) if page.index else []
            header = _note_header(page, spans, page.rect) if page.index else None
            if header is None:
                keep.append(page.index)
        return keep
    finally:
        doc.close()


def error_excerpt(log: str) -> str:
    lines = log.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("!") or re.match(r"^.*:\d+: ", ln):
            return "\n".join(lines[i:i + 6])
    return "\n".join(lines[-12:])


def copy_tree(src: Path, dst: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix in BUILD_EXT or name.endswith(".synctex.gz") or p.stat().st_size > 50_000_000:
                continue
            shutil.copy2(p, dst / rel / name)


# ---------------------------------------------------------------- source structure helpers

def line_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand [start, end) to whole lines when only whitespace surrounds it on them."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    le = len(text) if le < 0 else le + 1
    if text[ls:start].strip() or text[end:le].strip():
        return start, end
    return ls, le


def indent_at(text: str, pos: int) -> str:
    ls = text.rfind("\n", 0, pos) + 1
    m = re.match(r"[ \t]*", text[ls:])
    return m.group(0)


def frame_insert_point(cand: Candidate, frame: Frame) -> int:
    """Where new content goes in a frame: before \\note{...} lines at its end, else before \\end{frame}."""
    text = cand.masked(frame.file)
    body = text[frame.body:frame.body_end]
    m = None
    for m2 in re.finditer(r"\\note\s*(<[^>]*>)?\s*(\[[^\]]*\])?\s*\{", body):
        m = m2
    pos = frame.body_end
    if m:
        pos = frame.body + m.start()
    return text.rfind("\n", 0, pos) + 1 if text[text.rfind("\n", 0, pos) + 1:pos].strip() == "" else pos


def enclosing_group(text: str, a: int, b: int, names: tuple[str, ...], lo: int) -> tuple[int, int, int, int, str] | None:
    """The innermost `\\name{...}` or `{\\name ...}` around [a, b) that starts after `lo`:
    (command start, content start, content end, group end, name)."""
    pat = re.compile(r"\\(" + "|".join(re.escape(n) for n in names) + r")\b")
    best = None
    for m in pat.finditer(text, lo, a):
        name = m.group(1)
        j = skip_space(text, m.end())
        if name in ("bfseries", "itshape", "em", "slshape", "ttfamily") or (j < len(text) and text[j] != "{" and name != "alert"):
            # {\bfseries ...}: the group opening right before the switch
            k = m.start() - 1
            while k >= lo and text[k] in " \t\n":
                k -= 1
            if k < lo or text[k] != "{":
                continue
            close = match_group(text, k)
            if close < 0 or close - 1 < b:
                continue
            cand = (k, skip_space(text, m.end()), close - 1, close, name)
        else:
            args, after = read_args(text, m.end(), "<M")
            if not args[1] or text[args[1][1] - 1] != "{":
                continue
            if args[1][1] > a or args[1][2] < b:
                continue
            cand = (m.start(), args[1][1], args[1][2], after, name)
        if best is None or cand[1] >= best[1]:
            best = cand
    return best


def balanced(text: str) -> bool:
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0 and text.count("$") % 2 == 0 and "\\item" not in text and "\\begin" not in text \
        and "\\end" not in text and "\n\n" not in text


def plain_source(text: str) -> bool:
    """Source that is only words: no commands (escapes allowed), braces or math."""
    stripped = re.sub(r"\\[&%$#_{}]", "", text)
    return not re.search(r"[\\{}$]", stripped)


# ---------------------------------------------------------------- the planner

class Planner:
    def __init__(self, cand: Candidate, comp: Comparison, target: dict, ctx: Context, ws: Workspace,
                 blocked: set, last_values: dict):
        self.cand, self.comp, self.target, self.ctx, self.ws = cand, comp, target, ctx, ws
        self.blocked = blocked
        self.last = last_values
        self.edits: list[Edit] = []
        self.unresolved: list[dict] = []
        self.shifted: set[int] = set()  # slides whose flow got a \vspace this round
        deck = cand.deck
        self.cur_slides = deck["slides"]
        self.tgt_slides = target["slides"]
        self.t2c = {j: i for i, j in comp.slides if i is not None and j is not None}
        self.c2t = {i: j for i, j in comp.slides if i is not None and j is not None}
        self.base = body_style(deck)

    # -- helpers
    def fail(self, r: dict, why: str) -> None:
        self.unresolved.append({**r, "why": why})

    def edit(self, file: Path, start: int, end: int, text: str, r: dict, note: str = "") -> None:
        self.edits.append(Edit(file, start, end, text, r["kind"], signature(r), note))

    def element(self, slide: dict, eid: str) -> dict | None:
        return next((e for e in slide["elements"] if e["id"] == eid), None)

    def plan(self) -> tuple[list[Edit], list[dict]]:
        res = [r for r in self.comp.open() if signature(r) not in self.blocked]
        structural = [r for r in res if r["kind"] in ("slide_missing", "slide_extra", "slide_order")]
        if structural:
            for r in structural:
                getattr(self, r["kind"])(r)
            return self.edits, self.unresolved
        # A slide whose content changes size this round (items, boxes, a resized picture) is
        # measured again before anything on it is moved.
        reshaping = {r["slide"] for r in res if r["kind"] in RESHAPING or
                     (r["kind"] == "geometry" and (abs(r.get("dw", 0)) > TOL["size"] or abs(r.get("dh", 0)) > TOL["size"]))}
        res = [r for r in res if not (r["kind"] == "geometry" and r["slide"] in reshaping and
                                      abs(r.get("dw", 0)) <= TOL["size"] and abs(r.get("dh", 0)) <= TOL["size"])]
        lists_done = set()
        for r in sorted(res, key=lambda r: (r["kind"] == "geometry", r.get("cur", [0, 0])[1] if r["kind"] == "geometry"
                                            and isinstance(r.get("cur"), list) and len(r["cur"]) == 2 else 0)):
            k = r["kind"]
            if k in ("paragraph_missing", "paragraph_extra", "paragraph_order", "bullet"):
                self.list_residual(r, res, lists_done)
            elif hasattr(self, k):
                getattr(self, k)(r)
            else:
                self.fail(r, "no translator")
        return self.edits, self.unresolved

    # -- slides
    def slide_missing(self, r: dict) -> None:
        j = r["target_slide"]
        ts = self.tgt_slides[j]
        prev = next((self.t2c[k] for k in range(j - 1, -1, -1) if k in self.t2c and self.cand.frames[self.t2c[k]]), None)
        if prev is not None:
            frame = self.cand.frames[prev]
            text = self.cand.source.text(frame.file)
            pos = text.find("\n", frame.end)
            pos = len(text) if pos < 0 else pos + 1
            file = frame.file
        else:
            first = next((f for f in self.cand.source.frames), None)
            if first is None:
                self.fail(r, "no frame to insert before")
                return
            file, pos = first.file, self.cand.source.text(first.file).rfind("\n", 0, first.start) + 1
        self.edit(file, pos, pos, "\n" + frame_latex(ts, self.level_style, self.ctx), r)

    def slide_extra(self, r: dict) -> None:
        frame = self.cand.frames[r["slide"]]
        if frame is None:
            self.fail(r, "the page comes from no frame (theme page)")
            return
        shared = [i for i, f in enumerate(self.cand.frames) if f is frame]
        if len(shared) > 1:
            self.fail(r, "the frame also makes other slides")
            return
        text = self.cand.source.text(frame.file)
        a, b = line_span(text, frame.start, frame.end)
        if text[b:b + 1] == "\n":
            b += 1
        self.edit(frame.file, a, b, "", r)

    def slide_order(self, r: dict) -> None:
        i, j = r["slide"], r["target_slide"]
        frame = self.cand.frames[i]
        prev = next((self.t2c[k] for k in range(j - 1, -1, -1) if k in self.t2c), None)
        if frame is None:
            self.fail(r, "the page comes from no frame")
            return
        text = self.cand.source.text(frame.file)
        a, b = line_span(text, frame.start, frame.end)
        block = text[a:b] if text[a:b].endswith("\n") else text[a:b] + "\n"
        if prev is None:
            first = self.cand.source.frames[0]
            if first.file != frame.file:
                self.fail(r, "frames in different files")
                return
            pos = text.rfind("\n", 0, first.start) + 1
        else:
            pf = self.cand.frames[prev]
            if pf is None or pf.file != frame.file:
                self.fail(r, "frames in different files")
                return
            pos = text.find("\n", pf.end)
            pos = len(text) if pos < 0 else pos + 1
        if a <= pos <= b:
            return
        self.edit(frame.file, a, b, "", r)
        self.edits.append(Edit(frame.file, pos, pos, "\n" + block, "slide_order", signature(r) + ("insert",)))

    # -- notes and background
    def notes(self, r: dict) -> None:
        frame = self.cand.frames[r["slide"]]
        if frame is None:
            self.fail(r, "the page comes from no frame")
            return
        text = self.cand.masked(frame.file)
        found = list(re.finditer(r"\\note\s*(<[^>]*>)?\s*(\[[^\]]*\])?\s*\{", text[frame.body:frame.body_end]))
        new = r.get("tgt") or ""
        body = "\n\n".join(latex_escape(p) for p in new.split("\n") if p.strip())
        if found:
            m = found[0]
            start = frame.body + m.start()
            close = match_group(text, frame.body + m.end() - 1)
            if not new:
                a, b = line_span(text, start, close)
                self.edit(frame.file, a, b, "", r)
            else:
                self.edit(frame.file, frame.body + m.end(), close - 1, body, r)
            for extra in found[1:]:
                s = frame.body + extra.start()
                e = match_group(text, frame.body + extra.end() - 1)
                a, b = line_span(text, s, e)
                self.edits.append(Edit(frame.file, a, b, "", "notes", signature(r) + (s,)))
        elif new:
            pos = text.rfind("\n", 0, frame.body_end) + 1
            ind = indent_at(text, frame.start) + "  "
            self.edit(frame.file, pos, pos, f"{ind}\\note{{{body}}}\n", r)

    def background(self, r: dict) -> None:
        frame = self.cand.frames[r["slide"]]
        if frame is None:
            self.fail(r, "the page comes from no frame")
            return
        text = self.cand.masked(frame.file)
        name = colour_name(r["tgt"], self.ctx.colours)
        before = text[max(0, frame.start - 200):frame.start]
        m = re.search(r"\{\\setbeamercolor\{background canvas\}\{bg=([^}]*)\}\s*$", before)
        if m:
            s = frame.start - len(before) + m.start(1)
            self.edit(frame.file, s, s + len(m.group(1)), name, r)
            return
        a, b = line_span(text, frame.start, frame.end)
        self.edit(frame.file, a, a, f"{{\\setbeamercolor{{background canvas}}{{bg={name}}}\n", r)
        self.edits.append(Edit(frame.file, b, b, "}\n", "background", signature(r) + ("close",)))

    # -- text
    def loc(self, r: dict) -> ParaLoc | None:
        return self.cand.paragraph_locations(r["slide"]).get((r["element"], r["para"]))

    def text(self, r: dict) -> None:
        loc = self.loc(r)
        if loc is None:
            self.fail(r, "paragraph not found in the source")
            return
        src = self.cand.masked(loc.file)
        cur_words = r["cur"].split()
        for op in r["ops"]:
            c0, c1 = op["c"]
            new = latex_escape(op["tgt"])
            if any(HOLE in w or OPAQUE in w for w in cur_words[c0:c1]):
                self.fail({**r, "op": op}, "the words include a formula")
                continue
            if op["op"] == "insert":
                if c0 > 0 and loc.words.vis[c0 - 1] is not None:
                    at = loc.visible.ends[loc.words.vis[c0 - 1][1] - 1]
                    self.edits.append(Edit(loc.file, at, at, " " + new, "text", signature(r) + (c0,)))
                elif c0 < len(cur_words) and loc.words.vis[c0] is not None:
                    at = loc.visible.starts[loc.words.vis[c0][0]]
                    self.edits.append(Edit(loc.file, at, at, new + " ", "text", signature(r) + (c0,)))
                else:
                    self.fail({**r, "op": op}, "insertion point not found")
                continue
            span = loc.words.span(c0, c1)
            if span is None:
                self.fail({**r, "op": op}, "words not found in the source")
                continue
            a, b = loc.visible.starts[span[0]], loc.visible.ends[span[1] - 1]
            if plain_source(src[a:b]):
                if op["op"] == "delete":
                    a, b = widen_delete(src, a, b)
                    new = ""
                self.edits.append(Edit(loc.file, a, b, new, "text", signature(r) + (c0,)))
                continue
            # the words cross commands: replace word by word where counts agree
            tw = op["tgt"].split()
            if op["op"] == "replace" and len(tw) == c1 - c0:
                ok = True
                parts = []
                for d in range(c1 - c0):
                    s = loc.words.span(c0 + d, c0 + d + 1)
                    wa, wb = loc.visible.starts[s[0]], loc.visible.ends[s[1] - 1]
                    if not plain_source(src[wa:wb]):
                        ok = False
                        break
                    if cur_words[c0 + d] != tw[d]:
                        parts.append(Edit(loc.file, wa, wb, latex_escape(tw[d]), "text", signature(r) + (c0 + d,)))
                if ok:
                    self.edits += parts
                    continue
            self.fail({**r, "op": op, "source": src[a:b]}, "the words span LaTeX commands")

    def style(self, r: dict) -> None:
        loc = self.loc(r)
        if loc is None:
            self.fail(r, "paragraph not found in the source")
            return
        cs = self.cur_slides[r["slide"]]
        el = self.element(cs, r["element"])
        para = el["paragraphs"][r["para"]]
        ptext = norm_text(para_text(para))
        span = char_span(ptext, r["c0"], r["c1"], loc)
        if span is None:
            self.fail(r, "styled words not found in the source")
            return
        a, b = span
        src = self.cand.masked(loc.file)
        fld = r["field"]
        lo = loc.visible.starts[loc.lo] if not loc.title else max(0, a - 400)
        lo = max(0, min(lo, a) - 200)
        if fld in STYLE_CMDS:
            names, on_cmd, off_cmd = STYLE_CMDS[fld]
            if r["tgt"]:
                if not balanced(src[a:b]):
                    self.fail(r, "styled range isn't balanced LaTeX")
                    return
                self.wrap(loc.file, a, b, on_cmd, r)
            else:
                g = enclosing_group(src, a, b, names, lo)
                if g is None:
                    if off_cmd and balanced(src[a:b]):
                        self.wrap(loc.file, a, b, off_cmd, r)
                    else:
                        self.fail(r, "no enclosing style command to remove")
                    return
                self.unwrap(loc.file, src, g, a, b, r)
        elif fld == "color":
            tgt = r["tgt"]
            base_colour = dominant(para, "color")
            g = enclosing_group(src, a, b, ("textcolor", "color", "alert"), lo)
            if g is not None and src[g[1]:g[2]].strip() == src[a:b].strip():
                cmd_start, c_start, c_end, g_end, name = g
                if compare_colour(tgt, base_colour) or name == "alert":
                    if name == "alert":
                        self.edit(loc.file, cmd_start, g_end, f"\\textcolor{{{colour_name(tgt, self.ctx.colours)}}}{{{src[c_start:c_end]}}}", r)
                    else:
                        m = re.match(r"\\(textcolor|color)\s*(<[^>]*>)?\s*(\[[^\]]*\])?\s*\{([^}]*)\}", src[cmd_start:] if name == "textcolor" else src[cmd_start:])
                        mm = re.search(r"\\(textcolor|color)\s*(<[^>]*>)?\s*(\[[^\]]*\])?\s*\{([^}]*)\}", src[cmd_start:c_end])
                        if mm:
                            s = cmd_start + mm.start(4)
                            self.edit(loc.file, s, s + len(mm.group(4)), colour_name(tgt, self.ctx.colours), r)
                            src_model = src[:s]
                        else:
                            self.fail(r, "colour command not understood")
                    return
                self.unwrap(loc.file, src, g, a, b, r)
                return
            if not compare_colour(tgt, base_colour) and g is not None:
                self.unwrap(loc.file, src, g, a, b, r)
                return
            if not balanced(src[a:b]):
                self.fail(r, "coloured range isn't balanced LaTeX")
                return
            self.edit(loc.file, a, b, f"\\textcolor{{{colour_name(tgt, self.ctx.colours)}}}{{{src[a:b]}}}", r)
        elif fld == "size":
            base_size = dominant(para, "size")
            g = enclosing_group(src, a, b, tuple(SIZE_TABLES[11]) + ("fontsize",), lo)
            if g is not None and src[g[1]:g[2]].strip() == src[a:b].strip():
                if abs(r["tgt"] - base_size) <= 0.06 * base_size:
                    self.unwrap(loc.file, src, g, a, b, r)
                else:
                    m = re.match(r"\\(" + "|".join(SIZE_TABLES[11]) + r")\b", src[g[0] + 1:])
                    k = skip_space(src, g[0] + 1)
                    mm = re.match(r"\\(?:[A-Za-z]+size|tiny|small|large|Large|LARGE|huge|Huge)\b|\\fontsize\{[^}]*\}\{[^}]*\}\\selectfont",
                                  src[k:])
                    if mm:
                        self.edit(loc.file, k, k + mm.end(), size_switch(r["tgt"], self.ctx.pt_option), r)
                    else:
                        self.fail(r, "size command not understood")
                return
            if not balanced(src[a:b]):
                self.fail(r, "sized range isn't balanced LaTeX")
                return
            self.edit(loc.file, a, b, f"{{{size_switch(r['tgt'], self.ctx.pt_option)} {src[a:b]}}}", r)
        else:
            self.fail(r, f"no translator for {fld}")

    def wrap(self, file: Path, a: int, b: int, cmd: str, r: dict) -> None:
        src = self.cand.masked(file)
        self.edit(file, a, b, f"\\{cmd}{{{src[a:b]}}}", r)

    def unwrap(self, file: Path, src: str, g: tuple, a: int, b: int, r: dict) -> None:
        cmd_start, c_start, c_end, g_end, name = g
        head = src[cmd_start:c_start]
        tail = src[c_end:g_end]
        pre, mid, post = src[c_start:a], src[a:b], src[b:c_end]
        out = ""
        if pre.strip():
            out += head + pre.rstrip() + tail + pre[len(pre.rstrip()):]
        else:
            out += pre
        out += mid
        if post.strip():
            out += post[:len(post) - len(post.lstrip())] + head + post.lstrip() + tail
        else:
            out += post
        self.edit(file, cmd_start, g_end, out, r)

    # -- lists and paragraphs
    def list_residual(self, r: dict, res: list[dict], done: set) -> None:
        si = r["slide"]
        locs = self.cand.paragraph_locations(si)
        ti = r["target_slide"]
        ts = self.tgt_slides[ti]
        cs = self.cur_slides[si]
        # the list environment concerned
        lst = None
        if r["kind"] in ("paragraph_extra", "paragraph_order", "bullet"):
            loc = locs.get((r["element"], r["para"]))
            if loc is None:
                self.fail(r, "paragraph not found in the source")
                return
            lst = top_list(loc)
            if lst is None:
                if r["kind"] == "paragraph_extra":
                    self.delete_paragraph(loc, r)
                elif r["kind"] == "bullet" and r["tgt"][0] is not None:
                    self.itemize_paragraph(loc, r)
                else:
                    self.fail(r, "paragraph is not in a list")
                return
        else:  # paragraph_missing
            if self.target_element_is_new(ts, r["target_element"], si):
                return  # element_missing adds it as a whole
            after = r.get("after")
            tpara = self.element(ts, r["target_element"])["paragraphs"][r["target_para"]]
            aloc = locs.get((after["element"], after["para"])) if after else None
            if aloc is None and after:
                self.fail(r, "predecessor not found in the source")
                return
            if aloc is None:
                first = next(iter(locs.values()), None)
                frame = self.cand.frames[si]
                if frame is None:
                    self.fail(r, "the page comes from no frame")
                    return
                self.insert_paragraph(None, frame, tpara, r)
                return
            lst = top_list(aloc)
            if lst is None or not tpara.get("bullet"):
                if lst is not None and aloc.item is not None:
                    # a plain paragraph after a list: after the whole list
                    text = self.cand.masked(aloc.file)
                    pos = text.find("\n", lst.end)
                    pos = len(text) if pos < 0 else pos + 1
                    self.edit(aloc.file, pos, pos, indent_at(text, lst.start) + runs_latex(tpara["runs"], self.level_style(tpara), self.ctx) + "\n", r)
                    return
                self.insert_paragraph(aloc, None, tpara, r)
                return
        key = (aloc_file := (locs.get((r.get("element"), r.get("para"))) or locs.get(((r.get("after") or {}).get("element"), (r.get("after") or {}).get("para"))))).file, lst.start
        if key in done:
            return
        done.add(key)
        self.rebuild_list(si, ti, aloc_file.file, aloc_file.visible, lst, r)

    def target_element_is_new(self, ts: dict, eid: str, si: int) -> bool:
        return any(x["kind"] == "element_missing" and x["target_element"] == eid and x["target_slide"] == self.c2t.get(si)
                   for x in self.comp.open())

    def insert_paragraph(self, aloc: ParaLoc | None, frame: Frame | None, tpara: dict, r: dict) -> None:
        body = runs_latex(tpara["runs"], self.level_style(tpara), self.ctx)
        if aloc is not None:
            text = self.cand.masked(aloc.file)
            _, end = aloc.src()
            pos = text.find("\n", end)
            pos = len(text) if pos < 0 else pos + 1
            self.edit(aloc.file, pos, pos, "\n" + indent_at(text, end) + body + "\n", r)
        else:
            text = self.cand.masked(frame.file)
            pos = text.find("\n", frame.body)
            pos = len(text) if pos < 0 else pos + 1
            self.edit(frame.file, pos, pos, indent_at(text, frame.start) + "  " + body + "\n\n", r)

    def delete_paragraph(self, loc: ParaLoc, r: dict) -> None:
        text = self.cand.masked(loc.file)
        a, b = loc.src()
        if loc.title:
            self.edit(loc.file, a, b, "", r)
            return
        a, b = line_span(text, a, b)
        self.edit(loc.file, a, b, "", r)

    def itemize_paragraph(self, loc: ParaLoc, r: dict) -> None:
        text = self.cand.masked(loc.file)
        a, b = line_span(text, *loc.src())
        env = "enumerate" if r["tgt"][0] == "number" else "itemize"
        ind = indent_at(text, a)
        self.edit(loc.file, a, b, f"{ind}\\begin{{{env}}}\n{ind}  \\item {text[a:b].strip()}\n{ind}\\end{{{env}}}\n", r)

    def rebuild_list(self, si: int, ti: int, file: Path, vis: Visible, lst: ListEnv, r: dict) -> None:
        """The whole top-level list rewritten from the target's paragraphs: matched items keep their
        own source, new ones come from the target runs."""
        locs = self.cand.paragraph_locations(si)
        cs, ts = self.cur_slides[si], self.tgt_slides[ti]
        cps, tps = slide_paragraphs(cs), slide_paragraphs(ts)
        from .compare import match_paragraphs
        pm = match_paragraphs(cps, tps)
        c_of_t = {j: i for i, j, _ in pm}
        text = self.cand.masked(file)
        items = [it for it in vis.items if lst.start <= it.start < lst.end]
        item_para: dict[int, int] = {}  # item start -> current paragraph index
        for ci, cp in enumerate(cps):
            loc = locs.get((cp.el["id"], cp.pi))
            if loc is not None and loc.item is not None and loc.visible is vis and lst.start <= loc.item.start < lst.end:
                item_para[loc.item.start] = ci
        members_c = set(item_para.values())
        t_members = [j for j in range(len(tps)) if j in c_of_t and c_of_t[j] in members_c]
        if not t_members:
            self.fail(r, "list items not matched")
            return
        lo_t, hi_t = min(t_members), max(t_members)
        # new bulleted target paragraphs right before, inside or right after the matched range
        while lo_t > 0 and lo_t - 1 not in c_of_t and tps[lo_t - 1].p.get("bullet") and \
                not self.target_element_is_new(ts, tps[lo_t - 1].el["id"], si):
            lo_t -= 1
        while hi_t + 1 < len(tps) and hi_t + 1 not in c_of_t and tps[hi_t + 1].p.get("bullet") and \
                not self.target_element_is_new(ts, tps[hi_t + 1].el["id"], si):
            hi_t += 1
        by_para = {ci: it for it_start, ci in item_para.items() for it in items if it.start == it_start}
        if self.small_list_edit(file, text, items, item_para, tps, c_of_t, lo_t, hi_t, lst, r):
            return
        ind0 = indent_at(text, lst.start)
        env_at_level: dict[int, str] = {}
        item_ind: dict[int, str] = {}
        env_ind: dict[int, str] = {0: ind0}
        for it in items:
            env_at_level.setdefault(it.level, it.env)
            item_ind.setdefault(it.level, indent_at(text, it.start))
        for l in vis.lists:
            if lst.start <= l.start < lst.end:
                env_ind.setdefault(l.level, indent_at(text, l.start))

        def env_indent(level: int) -> str:
            return env_ind.get(level) or item_ind.get(level - 1, ind0 + "  " * (2 * level - 1)) + "  "

        def item_indent(level: int) -> str:
            return item_ind.get(level) or env_indent(level) + "  "
        lines = []
        stack: list[str] = []
        for j in range(lo_t, hi_t + 1):
            tp = tps[j]
            if not tp.p.get("bullet") and tp.p.get("tab_x0") is None:
                continue
            level = tp.p.get("level", 0)
            numbered = (tp.p.get("bullet") or {}).get("kind") == "number"
            env = env_at_level.get(level) or ("enumerate" if numbered else "itemize")
            if tp.p.get("tab_x0") is not None and not tp.p.get("bullet"):
                env = "description"
            elif env == "description" or (numbered != (env == "enumerate")):
                env = "enumerate" if numbered else "itemize"
            while len(stack) > level + 1 or (len(stack) == level + 1 and stack[-1] != env):
                lines.append(env_indent(len(stack) - 1) + f"\\end{{{stack.pop()}}}")
            while len(stack) < level + 1:
                e = env if len(stack) == level else env_at_level.get(len(stack), "itemize")
                lines.append(env_indent(len(stack)) + f"\\begin{{{e}}}")
                stack.append(e)
            ci = c_of_t.get(j)
            it = by_para.get(ci) if ci is not None else None
            if it is not None:
                own = text[it.start:it.end].rstrip()
                own = re.sub(r"\s*\n\s*", " ", own) if "\n\n" not in own and "\\begin" not in own and "%" not in own else own
            else:
                label = ""
                runs = tp.p["runs"]
                if env == "description":
                    joined = runs_latex(runs, self.level_style(tp.p), self.ctx)
                    head, _, rest = joined.partition("\t")
                    own = f"\\item[{head.strip()}] {rest.strip()}"
                else:
                    own = "\\item " + runs_latex(runs, self.level_style(tp.p), self.ctx).strip()
            lines.append(item_indent(len(stack) - 1) + own)
        while stack:
            lines.append(env_indent(len(stack) - 1) + f"\\end{{{stack.pop()}}}")
        a, b = line_span(text, lst.start, lst.end)
        new = "\n".join(lines) + "\n"
        if not text[a:b].endswith("\n"):
            new = new.rstrip("\n")
            a = lst.start
        if new.strip() == text[a:b].strip():
            self.fail(r, "list already as the target says")
            return
        self.edit(file, a if a != lst.start else a, b, new if a != lst.start else new.lstrip(), r)

    def small_list_edit(self, file: Path, text: str, items: list[Item], item_para: dict, tps: list[Para],
                        c_of_t: dict, lo_t: int, hi_t: int, lst: ListEnv, r: dict) -> bool:
        """Items only deleted, or only added at an existing level: edit those lines alone."""
        desired = [(c_of_t.get(j), tps[j].p.get("level", 0), j) for j in range(lo_t, hi_t + 1)
                   if tps[j].p.get("bullet") or tps[j].p.get("tab_x0") is not None]
        current = [(item_para.get(it.start), it.level, it) for it in sorted(items, key=lambda it: it.start)]
        if any(ci is None for ci, _, _ in current):
            return False
        want = [(ci, lv) for ci, lv, _ in desired if ci is not None]
        kept = [(ci, lv) for ci, lv, _ in current if ci in {c for c, _ in want}]
        if [c for c, _ in want] != [c for c, _ in kept] or any(
                lv != it.level for (ci, lv), (_, _, it) in zip(want, [x for x in current if x[0] in {c for c, _ in want}])):
            return False
        new = [(lv, j, k) for k, (ci, lv, j) in enumerate(desired) if ci is None]
        removed = [it for ci, _, it in current if ci not in {c for c, _ in want}]
        if new and removed:
            return False
        for it in removed:
            siblings = [x for x in items if x.env == it.env and x.level == it.level and
                        self.same_env(x, it, lst, items)]
            if all(s in removed for s in siblings) and it.level > 0:
                env = next((l for l in self.cand.paragraph_locations(r["slide"]).values()
                            for l in l.visible.lists if l.start < it.start < l.end and l.level == it.level), None)
                if env is not None and siblings and it is siblings[0]:
                    a, b = line_span(text, env.start, env.end)
                    self.edits.append(Edit(file, a, b, "", r["kind"], signature(r) + (it.start,)))
                continue
            a, b = line_span(text, it.start, len(text[:it.end].rstrip()))
            self.edits.append(Edit(file, a, b, "", r["kind"], signature(r) + (it.start,)))
        by_ci = {ci: it for ci, _, it in current}
        for lv, j, k in new:
            prev = next(((by_ci[c], l) for c, l, _ in reversed(desired[:k]) if c is not None), None)
            nxt = next(((by_ci[c], l) for c, l, _ in desired[k + 1:] if c is not None), None)
            line = "\\item " + runs_latex(tps[j].p["runs"], self.level_style(tps[j].p), self.ctx).strip()
            if prev and prev[1] == lv:
                pos = text.find("\n", len(text[:prev[0].end].rstrip()))
                pos = len(text) if pos < 0 else pos + 1
                ind = indent_at(text, prev[0].start)
            elif nxt and nxt[1] == lv:
                pos = text.rfind("\n", 0, nxt[0].start) + 1
                ind = indent_at(text, nxt[0].start)
            else:
                return False
            self.edits.append(Edit(file, pos, pos, ind + line + "\n", r["kind"], signature(r) + (j,)))
        return bool(new or removed)

    @staticmethod
    def same_env(a: Item, b: Item, lst: ListEnv, items: list[Item]) -> bool:
        """Items of one environment: no item of a shallower level between them."""
        lo, hi = sorted((a.start, b.start))
        return not any(lo < x.start < hi and x.level < a.level for x in items)

    def level_style(self, p: dict) -> dict:
        """Base style for new text at a paragraph's list level: what that level shows elsewhere
        in the deck (themes shrink nested items), so no size switch is written for it."""
        level = p.get("level", 0) if p.get("bullet") else None
        counts: dict = {}
        for s in self.cur_slides:
            for e in s["elements"]:
                if e["kind"] != "text" or e.get("role") != "body":
                    continue
                for q in e["paragraphs"]:
                    if (q.get("level", 0) if q.get("bullet") else None) != level:
                        continue
                    for run in q["runs"]:
                        k = (round(run["size"], 1), run["color"], run.get("family"))
                        counts[k] = counts.get(k, 0) + len(run["text"])
        if not counts:
            return self.base
        size, colour, family = max(counts, key=counts.get)
        return {**self.base, "size": size, "color": colour, "family": family}

    # -- elements and geometry
    def element_missing(self, r: dict) -> None:
        ci = r["slide"]
        frame = self.cand.frames[ci]
        if frame is None:
            self.fail(r, "the page comes from no frame")
            return
        te = self.element(self.tgt_slides[r["target_slide"]], r["target_element"])
        text = self.cand.masked(frame.file)
        pos = frame_insert_point(self.cand, frame)
        ind = indent_at(text, frame.start) + "  "
        if te["kind"] == "text":
            if te.get("role") == "title":
                title = runs_latex(te["paragraphs"][0]["runs"], {**self.base, "size": None, "color": None}, self.ctx)
                a = frame.body
                self.edit(frame.file, a, a, f"\n{ind}\\frametitle{{{title.strip()}}}", r)
                return
            vis = frame_visible(self.cand.source, frame)
            wanted = norm_text(" ".join(para_text(p) for p in te["paragraphs"]))
            if wanted and locate_words(wanted, vis).score >= 0.8:
                self.fail(r, "the text is in the frame already but doesn't convert as its own text "
                             "(covered by a figure, or merged into another box)")
                return
            self.ctx.packages.add(TEXTPOS)
            self.edit(frame.file, pos, pos, textblock_latex(te, self.level_style, self.ctx, ind, aligned_frame(text, frame)) + "\n", r)
        elif te["kind"] == "image":
            path = te.get("file")
            if not path or not Path(path).exists():
                self.fail(r, "the picture file is not available")
                return
            rel = self.picture_file(Path(path))
            x0, y0, x1, y1 = te["bbox"]
            self.ctx.packages.add(TEXTPOS)
            self.edit(frame.file, pos, pos,
                      f"{ind}\\begin{{textblock*}}{{{x1 - x0:.1f}pt}}({x0:.1f}pt,{y0:.1f}pt)\n"
                      f"{ind}  \\includegraphics[width={x1 - x0:.1f}pt,height={y1 - y0:.1f}pt]{{{rel}}}\n"
                      f"{ind}\\end{{textblock*}}\n", r)
        else:
            self.fail(r, f"new {te['kind']} elements are not translated")

    def picture_file(self, path: Path) -> str:
        data = path.read_bytes()
        name = f"b2s-{hashlib.sha1(data).hexdigest()[:10]}{path.suffix.lower() if path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.pdf') else '.png'}"
        dest = self.ws.src / "figures" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf"):
                shutil.copy2(path, dest)
            else:
                from PIL import Image
                Image.open(path).save(dest)
        rel = dest.relative_to(self.ws.src).as_posix()
        self.ctx.files[str(path)] = dest
        return rel

    def element_extra(self, r: dict) -> None:
        if r["kind"] == "element_extra" and r.get("kind") == "element_extra" and r.get("text") is not None:
            return  # text: paragraph_extra residuals delete the words
        frame = self.cand.frames[r["slide"]]
        el = self.element(self.cur_slides[r["slide"]], r["element"])
        if frame is None or el is None or el["kind"] != "image":
            self.fail(r, "only pictures are deleted as elements")
            return
        g = self.graphics_command(frame, el)
        if g is None:
            self.fail(r, "\\includegraphics not found")
            return
        text = self.cand.masked(frame.file)
        a, b, block = g
        if block:
            a, b = line_span(text, *block)
        else:
            a, b = line_span(text, a, b)
        self.edit(frame.file, a, b, "", r)

    def graphics_command(self, frame: Frame, el: dict) -> tuple[int, int, tuple[int, int] | None] | None:
        """(start, end, enclosing textblock span or None) of the \\includegraphics showing a picture:
        the one whose textblock box is nearest, else the only / order-matched one in the frame."""
        text = self.cand.masked(frame.file)
        found = [(m.start(), read_args(text, m.end(), "<som")[1]) for m in
                 re.finditer(r"\\includegraphics\b", text[:frame.body_end]) if m.start() >= frame.body]
        if not found:
            return None
        blocks = [(m, m.start(), text.find("\\end{textblock*}", m.end())) for m in TEXTBLOCK_RE.finditer(text, frame.body, frame.body_end)]
        best, best_d = None, None
        pics = [e for e in self.cur_slides[self.cand.frames.index(frame)]["elements"] if e["kind"] == "image"] \
            if frame in self.cand.frames else []
        for k, (a, b) in enumerate(found):
            block = next(((s, e + len("\\end{textblock*}")) for m, s, e in blocks if s < a < e), None)
            if block:
                m = next(m for m, s, e in blocks if s == block[0])
                d = abs(float(m.group("x")) - el["bbox"][0]) + abs(float(m.group("y")) - el["bbox"][1])
            else:
                order = pics.index(el) if el in pics else 0
                d = 50.0 + 10 * abs(order - k)
            if best_d is None or d < best_d:
                best, best_d = (a, b, block), d
        return best

    def geometry(self, r: dict) -> None:
        ci = r["slide"]
        frame = self.cand.frames[ci]
        if frame is None:
            self.fail(r, "the page comes from no frame")
            return
        cs = self.cur_slides[ci]
        el = self.element(cs, r["element"])
        te = self.element(self.tgt_slides[r["target_slide"]], r["target_element"])
        text = self.cand.masked(frame.file)
        if el["kind"] == "image":
            self.picture_geometry(r, frame, el, te)
            return
        if el["kind"] != "text":
            self.fail(r, f"{el['kind']} elements are not moved")
            return
        if te.get("role") == "title" or el.get("role") == "title":
            self.fail(r, "frame titles are placed by the theme")
            return
        locs = self.cand.paragraph_locations(ci)
        mine = [(k, loc) for k, loc in locs.items() if k[0] == el["id"] and k[1] >= r.get("para", 0)]
        if not mine:
            self.fail(r, "element not found in the source")
            return
        a = min(loc.src()[0] for _, loc in mine)
        b = max(loc.src()[1] for _, loc in mine)
        if any(loc.file != frame.file for _, loc in mine):
            self.fail(r, "element text is outside the frame")
            return
        block = enclosing_textblock(text, a, frame)
        if block is not None:
            m, bs, be = block
            x, y = float(m.group("x")) + r["dx"], float(m.group("y")) + r["dy"]
            w = float(m.group("w"))
            if te.get("wrap_width") and abs(te["wrap_width"] - w) > 6 and len(te["paragraphs"]) >= 1:
                w = te["wrap_width"]
            s = bs
            self.edit(frame.file, s, s + len(m.group(0)), f"\\begin{{textblock*}}{{{w:.1f}pt}}({x:.1f}pt,{y:.1f}pt)", r)
            return
        # flow content -> a textblock at the target position; lists move whole
        for _, loc in mine:
            lst = top_list(loc)
            if lst is not None:
                a, b = min(a, lst.start), max(b, lst.end)
        a, b = balance_span(text, a, b, frame.body, frame.body_end)
        if abs(r["dx"]) <= TOL["pos"] and self.flow_shift(r, frame, text, a, cs, el):
            return
        others = [loc for k, loc in locs.items() if k[0] != el["id"] and a <= loc.src()[0] < b]
        if others:
            self.fail(r, "the element shares its source (a list or paragraph) with other boxes")
            return
        a, b = line_span(text, a, b)
        chunk = text[a:b]
        if "\\begin{frame}" in chunk or "\\end{frame}" in chunk:
            self.fail(r, "element source not isolated")
            return
        size = max((run.get("size") or 10) for p in el["paragraphs"] for run in p["runs"])
        x, y = te_anchor = text_anchor(te)
        align = r.get("align", "left")
        left = text_anchor({**el, "anchor": None})[0]
        right = max(l["x1"] for p in el["paragraphs"] for l in p["lines"])
        width = te.get("wrap_width") or (1.1 * (right - left) + 12)
        bx = x - (width / 2 if align == "center" else width if align == "right" else 0)
        by = y - 0.75 * size
        ind = indent_at(text, frame.start) + "  "
        align_cmd = {"center": "\\centering ", "right": "\\raggedleft "}.get(align, "\\raggedright " if aligned_frame(text, frame) else "")
        # keep the room it took in the flow when other flow text follows
        below = [e for e in cs["elements"] if e["kind"] == "text" and e is not el and e.get("role") not in ("title", "footer")
                 and e["bbox"][1] >= el["bbox"][3] - 1 and not in_textblock(self.cand, ci, e, frame)]
        room = min((e["bbox"][1] for e in below), default=None)
        spacer = f"{indent_at(text, a)}\\vspace{{{room - el['bbox'][1]:.1f}pt}}\n" if room is not None else ""
        self.ctx.packages.add(TEXTPOS)
        pos = frame_insert_point(self.cand, frame)
        self.edit(frame.file, a, b, spacer, r)
        block_text = (f"{ind}\\begin{{textblock*}}{{{width:.1f}pt}}({bx:.1f}pt,{by:.1f}pt)\n{ind}  {align_cmd}"
                      + dedent_block(chunk, ind + "  ").lstrip() + f"{ind}\\end{{textblock*}}\n")
        self.edits.append(Edit(frame.file, pos, pos, block_text, "geometry", signature(r) + ("block",)))

    def flow_shift(self, r: dict, frame: Frame, text: str, a: int, cs: dict, el: dict) -> bool:
        """A vertical move of flow text: \\vspace before it (adjusted on later rounds). Beamer centres
        a frame's content vertically, so the text moves by about half the space (secant updates
        learn the real gain). False when this isn't the way (too many tries, not converging)."""
        key = signature(r) + ("vspace",)
        state = self.last.setdefault(key, {"tries": 0, "v": 0.0, "err": None, "gain": None})
        if state["tries"] >= 5 or r["slide"] in self.shifted:
            return state["tries"] < 5
        err = r["dy"]
        gain = state["gain"] or (1.0 if re.search(r"(^|,)\s*t\s*(,|$)", frame.options) else 0.5)
        if state["err"] is not None and abs(state["dv"]) > 0.1:
            g = (err - state["err"]) / -state["dv"] if state["dv"] else None
            if g is not None and 0.2 <= g <= 1.5:
                gain = g
        if state["err"] is not None and abs(err) > 0.8 * abs(state["err"]) and state["tries"] >= 2:
            state["tries"] = 5
            return False
        dv = err / gain
        ls = text.rfind("\n", 0, a) + 1
        before = text[max(frame.body, ls - 120):ls]
        m = re.search(r"\\vspace\*?\{(-?[\d.]+)pt\}[ \t]*\n[ \t]*$", before)
        if m:
            start = ls - len(before) + m.start(1)
            value = float(m.group(1)) + dv
            if abs(value) < 0.05:
                line_start = text.rfind("\n", 0, ls - len(before) + m.start()) + 1
                self.edit(frame.file, line_start, ls, "", r)
            else:
                self.edit(frame.file, start, start + len(m.group(1)), f"{value:.1f}", r)
        else:
            self.edit(frame.file, ls, ls, f"{indent_at(text, a)}\\vspace{{{dv:.1f}pt}}\n", r)
        state.update(tries=state["tries"] + 1, err=err, dv=dv, gain=gain)
        self.shifted.add(r["slide"])
        return True

    def picture_geometry(self, r: dict, frame: Frame, el: dict, te: dict) -> None:
        g = self.graphics_command(frame, el)
        if g is None:
            self.fail(r, "\\includegraphics not found")
            return
        text = self.cand.masked(frame.file)
        a, b, block = g
        tw, th = te["bbox"][2] - te["bbox"][0], te["bbox"][3] - te["bbox"][1]
        cw, ch = el["bbox"][2] - el["bbox"][0], el["bbox"][3] - el["bbox"][1]
        opts_m = re.match(r"\\includegraphics\s*(\[[^\]]*\])?", text[a:b])
        opts = opts_m.group(1) or ""
        keep = [o.strip() for o in opts.strip("[]").split(",") if o.strip() and not re.match(r"(width|height|scale|keepaspectratio)\b", o.strip())]
        sized = abs(r.get("dw", 0)) > TOL["size"] or abs(r.get("dh", 0)) > TOL["size"]
        size_opts = [f"width={tw:.1f}pt", f"height={th:.1f}pt"]
        rel = re.search(r"width\s*=\s*([\d.]*)\s*\\(textwidth|linewidth|columnwidth)", opts)
        if rel and abs(tw / max(cw, 1) - th / max(ch, 1)) < 0.03:  # same aspect: keep the relative width
            factor = float(rel.group(1) or 1) * tw / max(cw, 1)
            size_opts = [f"width={factor:.3g}\\{rel.group(2)}"]
        new_opts = ",".join(keep + size_opts) if sized else opts.strip("[]")
        cmd = f"\\includegraphics[{new_opts}]" + text[a + opts_m.end():b] if new_opts else text[a:b]
        if block is not None:
            m = TEXTBLOCK_RE.match(text, block[0])
            x = float(m.group("x")) + r["dx"]
            y = float(m.group("y")) + r["dy"]
            self.edit(frame.file, block[0], block[0] + len(m.group(0)), f"\\begin{{textblock*}}{{{tw:.1f}pt}}({x:.1f}pt,{y:.1f}pt)", r)
            if sized:
                self.edits.append(Edit(frame.file, a, b, cmd, "geometry", signature(r) + ("size",)))
            return
        moved = abs(r["dx"]) > TOL["pos"] or abs(r["dy"]) > TOL["pos"]
        tries = self.last.get(signature(r) + ("flow",), 0)
        if sized and tries == 0:
            self.last[signature(r) + ("flow",)] = 1
            self.edit(frame.file, a, b, cmd, r)
            return
        if not moved:
            self.edit(frame.file, a, b, cmd, r)
            return
        # out of the flow, its room kept by a phantom of the same size
        self.ctx.packages.add(TEXTPOS)
        ind = indent_at(text, frame.start) + "  "
        pos = frame_insert_point(self.cand, frame)
        cur_cmd = text[a:b]
        self.edit(frame.file, a, b, f"\\phantom{{{cur_cmd}}}", r)
        pic = f"\\includegraphics[{','.join(keep + [f'width={tw:.1f}pt', f'height={th:.1f}pt'])}]" + text[a + opts_m.end():b]
        self.edits.append(Edit(frame.file, pos, pos,
                               f"{ind}\\begin{{textblock*}}{{{tw:.1f}pt}}({te['bbox'][0]:.1f}pt,{te['bbox'][1]:.1f}pt)\n"
                               f"{ind}  {pic}\n{ind}\\end{{textblock*}}\n", "geometry", signature(r) + ("block",)))

    def image(self, r: dict) -> None:
        frame = self.cand.frames[r["slide"]]
        el = self.element(self.cur_slides[r["slide"]], r["element"])
        path = r.get("file")
        if frame is None or not path or not Path(path).exists():
            self.fail(r, "replacement picture not available")
            return
        g = self.graphics_command(frame, el)
        if g is None:
            self.fail(r, "\\includegraphics not found")
            return
        text = self.cand.masked(frame.file)
        a, b, _ = g
        args, end = read_args(text, a + len("\\includegraphics"), "<som")
        rel = self.picture_file(Path(path))
        self.edit(frame.file, args[3][1], args[3][2], rel, r)

    def align(self, r: dict) -> None:
        self.fail(r, "paragraph alignment is not translated")

    def shape(self, r: dict) -> None:
        self.fail(r, "shape colours are not translated")

    def table(self, r: dict) -> None:
        self.fail(r, "table cells are not translated")


def aligned_frame(text: str, frame: Frame) -> bool:
    """The frame switches paragraph alignment somewhere: textblocks then state theirs."""
    return bool(re.search(r"\\(centering|raggedleft|flushright|center)\b", text[frame.body:frame.body_end]))


def top_list(loc: ParaLoc) -> ListEnv | None:
    if loc.item is None:
        return None
    src = loc.item.start
    outer = [l for l in loc.visible.lists if l.start <= src < l.end and l.level == 0]
    return outer[0] if outer else None


def enclosing_textblock(text: str, pos: int, frame: Frame):
    for m in TEXTBLOCK_RE.finditer(text, frame.body, frame.body_end):
        end = text.find("\\end{textblock*}", m.end())
        if m.start() < pos < end:
            return m, m.start(), end
    return None


def in_textblock(cand: Candidate, si: int, el: dict, frame: Frame) -> bool:
    locs = cand.paragraph_locations(si)
    text = cand.masked(frame.file)
    for (eid, _), loc in locs.items():
        if eid == el["id"]:
            return enclosing_textblock(text, loc.src()[0], frame) is not None
    return False


def balance_span(text: str, a: int, b: int, lo: int, hi: int) -> tuple[int, int]:
    """Widen [a, b) until its braces balance: groups opened inside close inside, groups closed
    inside open inside (within lo..hi)."""
    for _ in range(20):
        depth, unmatched_close, i = 0, 0, a
        while i < b:
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if depth:
                    depth -= 1
                else:
                    unmatched_close += 1
            i += 1
        if not depth and not unmatched_close:
            return a, b
        if depth:
            k, d = b, depth
            while k < hi and d:
                if text[k] == "\\":
                    k += 2
                    continue
                d += text[k] == "{"
                d -= text[k] == "}"
                k += 1
            b = k
        if unmatched_close:
            k, d = a - 1, unmatched_close
            while k >= lo and d:
                if text[k] == "}" and text[k - 1:k] != "\\":
                    d += 1
                elif text[k] == "{" and text[k - 1:k] != "\\":
                    d -= 1
                k -= 1
            a = k + 1
            # a command directly before the group belongs to it
            m = re.search(r"\\[A-Za-z]+\*?\s*(\[[^\]]*\])?\s*$", text[max(lo, a - 60):a])
            if m:
                a = max(lo, a - 60) + m.start()
    return a, b


def dedent_block(chunk: str, ind: str) -> str:
    lines = chunk.splitlines()
    pads = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    cut = min(pads) if pads else 0
    return "".join((ind + l[cut:] if l.strip() else "") + "\n" for l in lines)


def widen_delete(src: str, a: int, b: int) -> tuple[int, int]:
    if a > 0 and src[a - 1] in " ~":
        return a - 1, b
    if b < len(src) and src[b] in " ~":
        return a, b + 1
    return a, b


def char_span(ptext: str, c0: int, c1: int, loc: ParaLoc) -> tuple[int, int] | None:
    """Source span of characters c0..c1 of a paragraph's normalised text: whole words when the
    range covers them, characters inside a word when its source is plain."""
    words = [(m.start(), m.end()) for m in re.finditer(r"\S+", ptext)]
    idx = [k for k, (s, e) in enumerate(words) if s < c1 and e > c0]
    if not idx:
        return None
    k0, k1 = idx[0], idx[-1]
    span = loc.words.span(k0, k1 + 1)
    if span is None:
        return None
    a, b = loc.visible.starts[span[0]], loc.visible.ends[span[1] - 1]
    s0, e1 = words[k0][0], words[k1][1]
    if c0 > s0 or c1 < e1:
        va, vb = loc.words.vis[k0][0], loc.words.vis[k1][1]
        if vb - va == e1 - s0 and all(loc.visible.ends[v] - loc.visible.starts[v] == 1 for v in range(va, vb)):
            a = loc.visible.starts[va + (c0 - s0)] if c0 > s0 else a
            b = loc.visible.ends[vb - 1 - (e1 - c1)] if c1 < e1 else b
    return a, b


def dominant(para: dict, key: str):
    weights: dict = {}
    for r in para["runs"]:
        weights[r.get(key)] = weights.get(r.get(key), 0) + len(r["text"])
    return max(weights, key=weights.get) if weights else None


def body_style(deck: dict) -> dict:
    counts: dict = {}
    for s in deck["slides"]:
        for e in s["elements"]:
            if e["kind"] != "text" or e.get("role") != "body":
                continue
            for p in e["paragraphs"]:
                for r in p["runs"]:
                    k = (round(r["size"], 1), r["color"], r.get("family"))
                    counts[k] = counts.get(k, 0) + len(r["text"])
    if not counts:
        return {"size": 10.91, "color": "#000000", "family": "sans", "bold": False, "italic": False}
    size, colour, family = max(counts, key=counts.get)
    return {"size": size, "color": colour, "family": family, "bold": False, "italic": False}


def signature(r: dict) -> tuple:
    return (r["kind"], r.get("target_slide"), r.get("target_element"), r.get("target_para"), r.get("field"),
            r.get("t0"), r.get("slide") if r["kind"] in ("slide_extra",) else None,
            r.get("element") if r["kind"] in ("paragraph_extra", "element_extra") else None,
            r.get("para") if r["kind"] == "paragraph_extra" else None)


# ---------------------------------------------------------------- generated LaTeX

def paragraphs_latex(paragraphs: list[dict], style_for, ctx: Context, ind: str) -> str:
    lines, stack = [], []
    for p in paragraphs:
        runs = runs_latex(p["runs"], style_for(p), ctx).strip()
        if not runs:
            continue
        if p.get("bullet"):
            level = p.get("level", 0)
            env = "enumerate" if p["bullet"].get("kind") == "number" else "itemize"
            while len(stack) > level + 1:
                lines.append(ind + "  " * (len(stack) - 1) + f"\\end{{{stack.pop()}}}")
            while len(stack) < level + 1:
                lines.append(ind + "  " * len(stack) + f"\\begin{{{env}}}")
                stack.append(env)
            lines.append(ind + "  " * len(stack) + f"\\item {runs}")
        else:
            while stack:
                lines.append(ind + "  " * (len(stack) - 1) + f"\\end{{{stack.pop()}}}")
            if lines:
                lines.append("")
            align = {"center": "\\centering ", "right": "\\raggedleft "}.get(p.get("align"), "")
            lines.append(ind + align + runs.replace("\t", " "))
    while stack:
        lines.append(ind + "  " * (len(stack) - 1) + f"\\end{{{stack.pop()}}}")
    return "\n".join(lines)


def textblock_latex(te: dict, style_for, ctx: Context, ind: str, reset: bool = False) -> str:
    x, y = text_anchor(te)
    size = max((r.get("size") or 10.0) for p in te["paragraphs"] for r in p["runs"])
    xs = [l["x1"] for p in te["paragraphs"] for l in p.get("lines", []) if l.get("x1")]
    width = te.get("wrap_width") or ((max(xs) - x + 8) if xs else 200.0)
    align = {p.get("align") for p in te["paragraphs"]}
    bx = x - width / 2 if align == {"center"} else x - width if align == {"right"} else x
    body = paragraphs_latex(te["paragraphs"], style_for, ctx, ind + "  ")
    if reset and align not in ({"center"}, {"right"}):
        body = f"{ind}  \\raggedright\n" + body
    return (f"{ind}\\begin{{textblock*}}{{{width:.1f}pt}}({bx:.1f}pt,{y - 0.75 * size:.1f}pt)\n"
            + body + f"\n{ind}\\end{{textblock*}}")


def frame_latex(ts: dict, style_for, ctx: Context) -> str:
    key = ts.get("key")
    label = f"[label={key}]" if key and re.fullmatch(r"[A-Za-z][\w:.-]*", key) else ""
    title = ""
    body = []
    for e in ts["elements"]:
        if e["kind"] == "text" and e.get("role") == "title" and not title:
            title = runs_latex(e["paragraphs"][0]["runs"], {"size": None, "color": None, "bold": True, "family": "sans"}, ctx).strip()
        elif e["kind"] == "text" and e.get("role") not in ("footer", "math", "icon"):
            body.append(paragraphs_latex(e["paragraphs"], style_for, ctx, "  "))
    out = f"\\begin{{frame}}{label}{{{title}}}\n" + "\n\n".join(body) + "\n"
    if ts.get("notes"):
        out += "  \\note{" + "\n\n".join(latex_escape(p) for p in ts["notes"].split("\n") if p.strip()) + "}\n"
    return out + "\\end{frame}\n"


def ensure_preamble(ws: Workspace, ctx: Context) -> list[Edit]:
    text = ws.source.text(ws.main)
    masked = mask_comments(text)
    pos = ws.source.preamble_end()
    lines = []
    for pkg in sorted(ctx.packages):
        name = re.search(r"\{([^}]+)\}$", pkg).group(1)
        if not re.search(r"\\usepackage\s*(\[[^\]]*\])?\s*\{[^}]*\b" + re.escape(name) + r"\b", masked[:pos]):
            lines.append(pkg)
    for name, hexv in sorted(ctx.colours.items()):
        if not re.search(r"\\definecolor\s*\{" + re.escape(name) + r"\}", masked[:pos]):
            lines.append(f"\\definecolor{{{name}}}{{HTML}}{{{hexv}}}")
    if not lines:
        return []
    return [Edit(ws.main, pos, pos, "\n".join(lines) + "\n", "preamble", ("preamble",))]


# ---------------------------------------------------------------- the loop

@dataclass
class Result:
    converged: bool
    iterations: list[dict]
    unresolved: list[dict]
    residuals: list[dict]
    files: dict[str, str]            # original path -> edited text
    patch: str
    work: Path


def picture_hashes(cand: Candidate, target: dict, comp_out: Path) -> dict:
    """Hashes of the pictures on both sides (current ones cropped from the candidate PDF)."""
    hashes = {}
    tgt_images = [e for s in target["slides"] for e in s["elements"] if e["kind"] == "image"]
    if not tgt_images:
        return hashes
    for e in tgt_images:
        if e.get("file") and Path(e["file"]).exists():
            hashes[id(e)] = picture_hash(e["file"])
    from .pdf import Document
    from PIL import Image
    doc = Document(cand.pdf)
    try:
        for s in cand.deck["slides"]:
            for e in s["elements"]:
                if e["kind"] != "image":
                    continue
                page = doc[s["page"]]
                x0, y0, x1, y1 = e["bbox"]
                if x1 - x0 < 1 or y1 - y0 < 1:
                    continue
                img = page.render(4.0, (x0, y0, x1, y1))
                hashes[id(e)] = list(Image.fromarray(img).convert("L").resize((16, 16), Image.BILINEAR).getdata())
    finally:
        doc.close()
    return hashes


def converge(tex: Path, target: dict, work: Path, max_iter: int = 10, handout: bool = False,
             engine: str | None = None, tol: dict | None = None, log=print) -> Result:
    ws = Workspace(tex, work, handout, engine)
    ctx = Context(pt_option=class_pt_option(ws.source))
    has_notes = any(s.get("notes") for s in target["slides"]) or uses_notes(ws.source)
    iterations: list[dict] = []
    blocked: set = set()
    attempts: dict[tuple, list[float]] = {}
    last_values: dict = {}
    seen: dict[tuple, int] = {}
    unresolved: list[dict] = []
    comp = None
    cand = None
    for it in range(max_iter + 1):
        built = ws.build(work / "classify", has_notes)
        if isinstance(built, str):
            raise RuntimeError(f"the source does not compile:\n{built}")
        cand = built
        comp = compare(cand.deck, target, tol, picture_hashes(cand, target, work))
        open_res = comp.open()
        summary = comp.summary()
        iterations.append({"iteration": it, "open": len(open_res), "by_kind": summary,
                           "geometry_error": round(sum(math.hypot(r.get("dx", 0), r.get("dy", 0))
                                                       for r in open_res if r["kind"] == "geometry"), 1)})
        log(f"  iteration {it}: {len(open_res)} open residuals {summary}")
        if not open_res or it == max_iter:
            break
        state = tuple(sorted(residual_line(r) for r in open_res))
        seen[state] = seen.get(state, 0) + 1
        if seen[state] >= 3:
            log("    the same residuals keep coming back: stopping")
            for r in open_res:
                unresolved.append({**r, "why": "edits oscillate (the same residuals came back)"})
            break
        # residuals that keep coming back after edits are given up on
        for r in open_res:
            sig = signature(r)
            if sig in blocked:
                continue
            mag = math.hypot(r.get("dx", 0), r.get("dy", 0)) + abs(r.get("dw", 0)) + abs(r.get("dh", 0)) \
                if r["kind"] == "geometry" else 1.0
            hist = attempts.setdefault(sig, [])
            hist.append(mag)
            if (r["kind"] in ADDITIVE and len(hist) >= 3) or \
                    (len(hist) >= 4 and (r["kind"] != "geometry" or hist[-1] >= 0.8 * hist[-3])):
                blocked.add(sig)
                unresolved.append({**r, "why": "not converging after repeated edits"})
        planner = Planner(cand, comp, target, ctx, ws, blocked, last_values)
        edits, failed = planner.plan()
        for f in failed:
            blocked.add(signature(f))
            unresolved.append(f)
        if not edits:
            break
        before = {p: ws.source.text(p) for p in ws.source.order}
        applied = ws.write(edits)
        extra = ensure_preamble(ws, ctx)
        if extra:
            ws.write(extra)
        check, err = ws.compile()
        if check is None:
            log(f"    the edits break the build:\n{err}")
            # find the edit that breaks the build: undo all, then apply one at a time
            restore(ws, before)
            good = []
            for e in applied:
                snapshot = {p: ws.source.text(p) for p in ws.source.order}
                ok_edit = remap_edit(e, before, ws)
                if ok_edit is None:
                    continue
                ws.write([ok_edit])
                pre = ensure_preamble(ws, ctx)
                if pre:
                    ws.write(pre)
                if ws.compile()[0] is None:
                    restore(ws, snapshot)
                    blocked.add(e.signature)
                    unresolved.append({"kind": e.kind, "why": "the edit breaks compilation", "edit": e.text[:200],
                                       "signature": list(map(str, e.signature))})
                else:
                    good.append(e)
            log(f"    {len(applied) - len(good)} edit(s) broke the build and were dropped")
        for sig in {e.signature for e in applied}:
            pass
    open_res = comp.open() if comp else []
    files = {}
    patch = ""
    for p in ws.source.order:
        rel = p.relative_to(ws.src)
        orig = ws.root / rel
        new = ws.source.text(p)
        old = orig.read_text(encoding="utf-8", errors="replace") if orig.exists() else ""
        if new != old:
            files[str(orig)] = new
            patch += "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                  f"a/{rel.as_posix()}", f"b/{rel.as_posix()}"))
    for src_path, dest in ctx.files.items():
        rel = dest.relative_to(ws.src)
        if not (ws.root / rel).exists():
            files[str(ws.root / rel)] = dest  # binary: copied on apply
    final_unresolved = dedupe_unresolved(unresolved, open_res)
    return Result(not open_res, iterations, final_unresolved, open_res, files, patch, ws.work)


def remap_edit(e: Edit, before: dict, ws: Workspace) -> Edit | None:
    return e if e.file in before and ws.source.text(e.file) == before[e.file] or True else None


def restore(ws: Workspace, texts: dict[Path, str]) -> None:
    for p, t in texts.items():
        p.write_text(t, encoding="utf-8", newline="")
        ws.source.set_text(p, t)
    ws.reload()


def dedupe_unresolved(unresolved: list[dict], open_res: list[dict]) -> list[dict]:
    """The residuals still open at the end, each with the reason recorded when it was given up on."""
    why = {}
    for u in unresolved:
        if "kind" in u and u["kind"] in {r["kind"] for r in open_res}:
            why.setdefault(signature(u) if "target_slide" in u or "slide" in u else None, u.get("why"))
    out = []
    for r in open_res:
        out.append({**r, "why": why.get(signature(r), "open when the loop stopped")})
    return out


def uses_notes(source: Source) -> bool:
    return any(re.search(r"\\note\b", mask_comments(t)) for t in source.texts.values())


def ir_from_tex(tex: Path, work: Path, handout: bool = False) -> dict:
    """classify IR of a source as the loop sees it (slide keys from frame labels, pictures cropped
    into work/pictures so they can be hashed): a target made from another source."""
    ws = Workspace(tex, work, handout)
    cand = ws.build(work / "classify", uses_notes(ws.source))
    if isinstance(cand, str):
        raise RuntimeError(f"{tex} does not compile:\n{cand}")
    from .pdf import Document
    from PIL import Image
    doc = Document(cand.pdf)
    try:
        for s in cand.deck["slides"]:
            for k, e in enumerate(s["elements"]):
                if e["kind"] == "image" and e["bbox"][2] - e["bbox"][0] >= 1 and e["bbox"][3] - e["bbox"][1] >= 1:
                    path = work / "pictures" / f"p{s['page']}-{k}.png"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(doc[s["page"]].render(4.0, tuple(e["bbox"]))).save(path)
                    e["file"] = str(path)
    finally:
        doc.close()
    return cand.deck


def class_pt_option(source: Source) -> int:
    m = re.search(r"\\documentclass\s*\[([^\]]*)\]\s*\{beamer\}", mask_comments(source.text(source.main)))
    if m:
        pt = re.search(r"(\d+)pt", m.group(1))
        if pt:
            return int(pt.group(1))
    return 11


# ---------------------------------------------------------------- reports

def report(result: Result, target: dict, cand_deck: dict | None = None) -> tuple[dict, str]:
    data = {"converged": result.converged, "iterations": result.iterations,
            "unresolved": [clean(u) for u in result.unresolved], "changed_files": list(result.files)}
    md = ["# Pull report", "", f"Converged: **{result.converged}** after {len(result.iterations) - 1} edit rounds.", ""]
    md.append("| iteration | open residuals | by kind | geometry error (pt) |")
    md.append("|---|---|---|---|")
    for it in result.iterations:
        md.append(f"| {it['iteration']} | {it['open']} | {json.dumps(it['by_kind'])} | {it['geometry_error']} |")
    if result.unresolved:
        md += ["", "## Left for the author", ""]
        for u in result.unresolved:
            ts = target["slides"][u["target_slide"]] if u.get("target_slide") is not None else None
            head = f"- slide {u['target_slide'] + 1 if u.get('target_slide') is not None else '?'}"
            if ts is not None:
                head += f" ({ts.get('key') or slide_title(ts) or 'untitled'})"
            md.append(f"{head}: {residual_line(u)} — {u.get('why')}")
            if u.get("where"):
                md.append(f"  - source: {u['where']}")
    if result.patch:
        md += ["", "## Patch", "", "```diff", result.patch.rstrip(), "```"]
    return data, "\n".join(md) + "\n"


def clean(r: dict) -> dict:
    return json.loads(json.dumps(r, default=str))
