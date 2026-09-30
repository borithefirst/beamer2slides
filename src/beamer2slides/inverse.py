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

import copy
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import zlib
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, Union

from .compare import (HOLE, TOL, AlignResidual, BackgroundResidual, BoxExtra, BoxGeometry, BoxMissing, BulletResidual,
                      ColorChange, Comparison, Current, DiagramResidual, FlagChange, ImageResidual, InlineImage,
                      ListResidual,
                      NotesResidual, PairedResidual, Para, SizeChange,
                      ParagraphExtra, ParagraphMissing, ParagraphOrder, PicHash, PictureHash, Residual, ShapeResidual,
                      SlideExtra, SlideMissing, SlideOrder, StyleResidual, TableResidual, TextExtra, TextGeometry,
                      TextMissing, TextResidual, WordOp, compare, current_slide_of, grey16, is_theme, norm_text,
                      para_text, picture_hash, residual_json, residual_line, slide_paragraphs, slide_title,
                      target_slide_of, text_anchor, word_op_json)
from .deck_ir_types import TargetDeck, TargetImage, element_json, is_target, parse_target, recolor_json, target_json
from .ir_types import At, Box, box
from .json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects, as_str
from .texmap import (OPAQUE, PARA, Frame, Item, ListEnv, Source, Visible, WordMap, build_visible, empty_visible,
                     frame_visible, line_of, locate_words, locate_words_in, mask_comments, match_group, norm_word, page_frames, read_args, skip_space,
                     synctex_pages)
from .typing_compat import assert_never

if TYPE_CHECKING:
    from .arrays import Floats32
    from .frame_guard import FrameGuard, Seen
    from .notes import Prepared
    from .pdf import Page

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
ONCE = {"text", "style", "notes", "element_missing", "slide_missing"}
REVERTIBLE = {"text", "style"}   # kinds that overwrite existing source text: a rewrite that never
                                  # converges is reverted rather than left as a garbled mix (converge)
STYLE_CMDS = {
    "bold": (("textbf", "bfseries", "alert"), "textbf", "textmd"),
    "italic": (("emph", "textit", "itshape", "em", "textsl", "slshape"), "emph", "textup"),
    "underline": (("underline", "uline", "ul"), "underline", None),
    "mono": (("texttt", "ttfamily"), "texttt", None),
}


def tex_env() -> dict[str, str]:
    env = dict(os.environ)
    if MIKTEX_BIN.is_dir() and str(MIKTEX_BIN) not in env.get("PATH", ""):
        env["PATH"] = f"{MIKTEX_BIN};{env.get('PATH', '')}"
    return env


# ---------------------------------------------------------------- LaTeX writing helpers

ESCAPE = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
          "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "\u00a0": "~", "\u200b": "", "\x0b": r"\\ ",
          "\t": " "}


# Set while adopt writes a source: its lengths are then rewritten from pt to bp afterwards
# (`adopt.to_bp`), and a number followed by "pt" in the deck's own words must not be one of them.
GUARD_UNITS: bool = False


def latex_escape(text: str) -> str:
    out = "".join(ESCAPE.get(c, c) for c in text)
    return re.sub(r"(\d)(?=pt)", r"\1{}", out) if GUARD_UNITS else out


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


@dataclass(frozen=True, kw_only=True)
class TextStyle:
    """The style LaTeX sets text in where a run says nothing else: `runs_latex` writes a run's
    style only where it differs from this. None where the base says nothing of it (a frame title's
    size and colour are the theme's). `font` and `weight` are adopt's: the face its base names."""
    size: float | None
    color: str | None
    family: str | None
    bold: bool
    italic: bool
    font: str | None
    weight: int | None


class RunIn(Protocol):
    """What the LaTeX writers read of a run: a target's (`deck_ir_types.TargetRun`), or the loop's
    reading of a run of either deck shape (`LoopRun`)."""
    @property
    def text(self) -> str: ...
    @property
    def hole(self) -> float | None: ...
    @property
    def family(self) -> str | None: ...
    @property
    def bold(self) -> bool: ...
    @property
    def italic(self) -> bool: ...
    @property
    def underline(self) -> bool: ...
    @property
    def script(self) -> str | None: ...
    @property
    def color(self) -> str | None: ...
    @property
    def size(self) -> float | None: ...
    @property
    def link(self) -> str | None: ...


class BulletIn(Protocol):
    @property
    def kind(self) -> str | None: ...


class ParagraphIn(Protocol):
    """What `paragraphs_latex` reads of a paragraph: a target's text or cell paragraph, or the loop's
    reading of one (`LoopParagraph`)."""
    @property
    def runs(self) -> Sequence[RunIn]: ...
    @property
    def bullet(self) -> BulletIn | None: ...
    @property
    def level(self) -> int: ...
    @property
    def align(self) -> str | None: ...
    @property
    def direction(self) -> str | None: ...


P = TypeVar("P", bound=ParagraphIn)


@dataclass(frozen=True, kw_only=True)
class LoopRun:
    """A run as the loop reads it from either deck shape (deck.json, a target): what a missing key
    reads as is what the dict reads of it did (`loop_run`)."""
    text: str
    hole: float | None
    family: str | None
    bold: bool
    italic: bool
    underline: bool
    script: str | None
    color: str | None
    size: float | None
    link: str | None


@dataclass(frozen=True, kw_only=True)
class LoopBullet:
    kind: str | None


@dataclass(frozen=True, kw_only=True)
class LoopParagraph:
    runs: tuple[LoopRun, ...]
    bullet: LoopBullet | None
    level: int
    align: str | None
    direction: str | None
    tab_x0: float | None
    line_ends: tuple[float, ...]
    """Each line's `x1`, where one says it (`textblock_latex` sizes a new box by them)."""


def _loop_str(o: JsonObject, key: str, where: str) -> str | None:
    v = o.get(key)
    if v is None or isinstance(v, str):
        return v
    raise JsonShapeError(f"{where}: {key} is no string")


def _number(v: Json, where: str) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    raise JsonShapeError(f"{where}: a number was expected")


def _loop_number(o: JsonObject, key: str, where: str) -> float | None:
    v = o.get(key)
    return None if v is None else _number(v, f"{where}: {key}")


def loop_run(r: JsonObject, where: str) -> LoopRun:
    return LoopRun(text=as_str(r["text"], f"{where}: text"), hole=_loop_number(r, "hole", where),
                   family=_loop_str(r, "family", where), bold=bool(r.get("bold")), italic=bool(r.get("italic")),
                   underline=bool(r.get("underline")), script=_loop_str(r, "script", where),
                   color=_loop_str(r, "color", where), size=_loop_number(r, "size", where),
                   link=None if r.get("link") is None else str(r.get("link")))


def loop_paragraph(p: JsonObject, where: str) -> LoopParagraph:
    """A paragraph of either deck shape as the writers read it. A bullet that says nothing (`{}`)
    is none, as `p.get("bullet")` read it."""
    b = p.get("bullet")
    bullet = LoopBullet(kind=_loop_str(as_object(b, f"{where}: bullet"), "kind", where)) if b else None
    level = p.get("level", 0)
    return LoopParagraph(runs=tuple(loop_run(r, where) for r in as_objects(p["runs"], f"{where}: runs")),
                         bullet=bullet, level=as_int(level, f"{where}: level"), align=_loop_str(p, "align", where),
                         direction=_loop_str(p, "direction", where), tab_x0=_loop_number(p, "tab_x0", where),
                         line_ends=tuple(_number(ln["x1"], f"{where}: x1")
                                         for ln in as_objects(p.get("lines", []), f"{where}: lines") if ln.get("x1")))


def loop_paragraphs(el: JsonObject, where: str) -> tuple[LoopParagraph, ...]:
    return tuple(loop_paragraph(p, f"{where} paragraph {i}")
                 for i, p in enumerate(as_objects(el["paragraphs"], f"{where}: paragraphs")))


def loop_texts(slide: JsonObject, where: str) -> list[tuple[str | None, tuple[LoopParagraph, ...]]]:
    """A slide's text elements as `frame_latex` reads them: each one's role and paragraphs."""
    return [(_loop_str(e, "role", where), loop_paragraphs(e, f"{where} element {e.get('id')}"))
            for e in as_objects(slide["elements"], f"{where}: elements") if e["kind"] == "text"]


@dataclass(frozen=True, kw_only=True)
class LoopCrop:
    """Fractions of a picture's file cut off, per side."""
    l: float  # noqa: E741 - the JSON's own name
    t: float
    r: float
    b: float


@dataclass(frozen=True, kw_only=True)
class LoopOutline:
    color: str
    weight: float
    dash: str


BAKED = ("brightness", "contrast", "recolor")


@dataclass(frozen=True, kw_only=True)
class LoopPicture:
    """A picture element as pull writes it, from either deck shape: its file and the Slides edits
    `picture_latex` writes as LaTeX. What the element leaves out or sets to nothing is none, as the
    dict reads (`te.get(k)`) took it."""
    bbox: Box
    frame: Box                 # its unturned frame: `box`, else its bbox
    file: str | None
    alt: str | None
    source_url: str | None
    crop: LoopCrop | None
    rotation: float | None
    flip: bool
    opacity: float | None
    outline: LoopOutline | None
    mask: str | None
    bake: JsonObject
    """Brightness, contrast and recolour where set, as the element says them: LaTeX has no option
    for them, so they are baked into the file (`Planner.picture`)."""


@dataclass(frozen=True, kw_only=True)
class LoopElement:
    """An element of either deck shape (deck.json's, a target's) as the planner reads it. `json` is
    the element itself: compare's readers take it (`text_anchor`, `para_text`), and its identity
    keys the element's picture in the planner's `hashes`."""
    json: JsonObject
    id: str
    kind: str
    role: str | None
    bbox: Box
    wrap_width: float | None
    picture: LoopPicture | None    # an image's


def loop_picture(e: JsonObject, bbox: Box, where: str) -> LoopPicture:
    frame = e.get("box")
    crop = as_object(e["crop"], f"{where}: crop") if e.get("crop") else None
    outline = as_object(e["outline"], f"{where}: outline") if e.get("outline") else None
    return LoopPicture(
        bbox=bbox, frame=box(frame, At(where=where, path="box")) if frame else bbox,
        file=_loop_str(e, "file", where), alt=_loop_str(e, "alt", where), source_url=_loop_str(e, "source_url", where),
        crop=None if crop is None else LoopCrop(l=_number(crop["l"], f"{where}: crop"), t=_number(crop["t"], f"{where}: crop"),
                                                r=_number(crop["r"], f"{where}: crop"), b=_number(crop["b"], f"{where}: crop")),
        rotation=_loop_number(e, "rotation", where), flip=bool(e.get("flip")), opacity=_loop_number(e, "opacity", where),
        outline=None if outline is None else LoopOutline(
            color=as_str(outline["color"], f"{where}: outline"), weight=_number(outline["weight"], f"{where}: outline"),
            dash=as_str(outline.get("dash", "SOLID"), f"{where}: outline")),
        mask=_loop_str(e, "mask", where), bake={k: v for k in BAKED if (v := e.get(k))})


def target_picture(el: TargetImage) -> LoopPicture:
    """`loop_picture` of a target's picture record: what its JSON (`element_json`) reads as."""
    bake: JsonObject = {}
    if el.brightness:
        bake["brightness"] = el.brightness
    if el.contrast:
        bake["contrast"] = el.contrast
    if el.recolor is not None:
        bake["recolor"] = recolor_json(el.recolor)
    crop, outline = el.crop, el.outline
    return LoopPicture(
        bbox=el.bbox, frame=el.bbox if el.box is None else el.box, file=el.file, alt=el.alt, source_url=el.source_url,
        crop=None if crop is None else LoopCrop(l=crop.l, t=crop.t, r=crop.r, b=crop.b),
        rotation=el.rotation, flip=bool(el.flip), opacity=el.opacity,
        outline=None if outline is None else LoopOutline(color=outline.color, weight=outline.weight, dash=outline.dash),
        mask=el.mask, bake=bake)


def loop_element(e: JsonObject, where: str) -> LoopElement:
    kind = as_str(e["kind"], f"{where}: kind")
    bbox = box(e["bbox"], At(where=where, path="bbox"))
    return LoopElement(json=e, id=as_str(e["id"], f"{where}: id"), kind=kind, role=_loop_str(e, "role", where),
                       bbox=bbox, wrap_width=_loop_number(e, "wrap_width", where),
                       picture=loop_picture(e, bbox, where) if kind == "image" else None)


def loop_elements(slide: JsonObject, where: str) -> list[LoopElement]:
    return [loop_element(e, f"{where} element {i}") for i, e in enumerate(as_objects(slide["elements"], where))]


def element_paragraphs(el: LoopElement) -> list[JsonObject]:
    """A text element's paragraphs as JSON, for compare's readers (`para_text`)."""
    return as_objects(el.json["paragraphs"], f"element {el.id}: paragraphs")


def run_latex(r: RunIn, text: str, base: TextStyle, ctx: "Context") -> str:
    """One run's words `text` (the run's own, or a line of them) in its style, relative to `base`."""
    text = latex_escape(text)
    if not text:
        return ""
    lead = len(text) - len(text.lstrip(" "))
    trail = len(text) - len(text.rstrip(" "))
    core = text.strip(" ")
    if core:
        if r.family == "mono" and base.family != "mono":
            core = f"\\texttt{{{core}}}"
        if r.bold and not base.bold:
            core = f"\\textbf{{{core}}}"
        if r.italic and not base.italic:
            core = f"\\emph{{{core}}}"
        if r.underline:
            core = f"\\underline{{{core}}}"
        if r.script == "super":
            core = f"\\textsuperscript{{{core}}}"
        elif r.script == "sub":
            core = f"\\textsubscript{{{core}}}"
        if r.color and base.color and compare_colour(r.color, base.color):
            core = f"\\textcolor{{{colour_name(r.color, ctx.colours)}}}{{{core}}}"
        if r.size and base.size and abs(r.size - base.size) > 0.06 * base.size:
            core = f"{{{size_switch(r.size, ctx.pt_option)} {core}}}"
        if r.link and not r.link.startswith("#"):
            # hyperref reads `#` and `%` itself only outside another command's argument: inside a
            # frame (whose body is one) a bare `#` is a parameter number and stops the build
            url = r.link.replace("\\", "/").replace("#", "\\#").replace("%", "\\%")
            core = f"\\href{{{url}}}{{{core}}}"
    return " " * lead + core + " " * trail


def runs_latex(runs: Sequence[RunIn], base: TextStyle, ctx: "Context") -> str:
    """LaTeX for styled runs, styles written relative to `base` (size, colour, bold, italic)."""
    out = []
    for r in runs:
        if r.hole:
            continue
        if "\x0b" in r.text:
            # A soft break (Shift+Enter) ends a line, which it cannot do inside the box a style puts
            # its run in (`\underline{a\\ b}`: "Not allowed in LR mode"), so the run's style goes
            # around each line's words and the break stands between them.
            out.append("\\\\ ".join(run_latex(r, t, base, ctx) if t else "" for t in r.text.split("\x0b")))
            continue
        out.append(run_latex(r, r.text, base, ctx))
    # Two breaks in a row are an empty line, and a second `\\` with nothing on its line stops a
    # ragged or centred paragraph ("There's no line here to end"): the line gets an empty box.
    return re.sub(r"(?<=\\\\ )(\s*)(?=\\\\)", r"\1\\mbox{}", "".join(out))


def compare_colour(a: str, b: str | None) -> bool:
    from .compare import colour_distance
    return colour_distance(a, b) > TOL["color"]


# ---------------------------------------------------------------- edits

# A residual's signature (`signature`), which knows it across rounds, and an Edit's: that
# signature and what the translator adds to tell its edits apart (a word index, "block", ...).
SigPart = Union[str, int, float, Path, None]
Signature = tuple[SigPart, ...]


@dataclass
class Edit:
    file: Path
    start: int
    end: int
    text: str
    kind: str
    signature: Signature
    note: str        # what the report says about the edit, "" for nothing


@dataclass(frozen=True, kw_only=True)
class FrameAt:
    """Where a residual's frame stands in the author's tree (`file:first-last line`), and its label."""
    where: str
    label: str | None


@dataclass(frozen=True, kw_only=True)
class Unresolved:
    """A residual no edit was written for, and why. `op` and `source`: the word operation of a
    `text` residual that failed, and the source it would have rewritten; `frame`: where the
    residual's frame stands, for the report (set once the loop ends)."""
    residual: Residual
    op: WordOp | None
    source: str | None
    why: str
    frame: FrameAt | None


@dataclass(frozen=True, kw_only=True)
class BrokenEdit:
    """An edit dropped because the source no longer compiled with it."""
    kind: str
    why: str
    edit: str
    signature: tuple[str, ...]


GivenUp = Union[Unresolved, BrokenEdit]


def given_up(r: Residual, why: str) -> Unresolved:
    return Unresolved(residual=r, op=None, source=None, why=why, frame=None)


def unresolved_json(u: GivenUp) -> JsonObject:
    """As edits.json, the agents and the reports read it: the residual's own keys, then `op`,
    `source`, `why`, `where` and `frame_label` where known."""
    if isinstance(u, BrokenEdit):
        sig: list[Json] = [s for s in u.signature]
        return {"kind": u.kind, "why": u.why, "edit": u.edit, "signature": sig}
    out = residual_json(u.residual)
    if u.op is not None:
        out["op"] = word_op_json(u.op)
    if u.source is not None:
        out["source"] = u.source
    out["why"] = u.why
    if u.frame is not None:
        out["where"] = u.frame.where
        out["frame_label"] = u.frame.label
    return out


@dataclass(kw_only=True)
class Context:
    """What the edits add beside the frames they rewrite (`fresh_context` has added nothing)."""
    colours: dict[str, str]   # \definecolor names to add
    packages: set[str]          # preamble lines to add
    pt_option: int              # the class's size option (`class_pt_option`)
    files: dict[str, Path]     # new picture files: key -> path in the work tree
    notes: list[str]           # what the report must say about pictures
    pictures: dict[tuple[str, str, str | None], "Picture"]  # (deck file, edits) -> Picture
    index: "list[PictureFile] | None"                 # picture files of the source tree
    label_notes: list[str]     # what the report must say about renamed labels


BEAMER_PT = 11
"""beamer's size when `\\documentclass` names none (`class_pt_option`)."""


def fresh_context(pt_option: int) -> Context:
    """A context that has added nothing yet, for a class set at `pt_option` pt."""
    return Context(colours={}, packages=set(), pt_option=pt_option, files={}, notes=[], pictures={}, index=None,
                   label_notes=[])


@dataclass
class ParaLoc:
    file: Path
    visible: Visible
    words: WordMap
    lo: int          # visible range of the paragraph's found words
    hi: int
    item: Item | None
    title: bool      # the frame's title, not its body

    def src(self) -> tuple[int, int]:
        return self.visible.starts[self.lo], self.visible.ends[self.hi - 1]


# ---------------------------------------------------------------- candidate build

@dataclass
class Candidate:
    source: Source
    pdf: Path
    deck: JsonObject
    frames: list[Frame | None]            # per deck slide
    locs: dict[int, dict[tuple[str, int], ParaLoc]]
    text_masked: dict[Path, str]
    words: dict[int, list[str]]   # PDF page -> the words it shows (extract, not classify)

    def keys(self) -> tuple[str | None, ...]:
        """The label of each slide's frame (None: unlabelled, or no frame), which pairs it with the
        target's slides first. deck.json has no place for it: it stays beside the deck."""
        return tuple(f.label if f is not None and f.label else None for f in self.frames)

    def current(self) -> Current:
        """The current side of a comparison: the deck, and its slides' frame labels."""
        return Current(deck=self.deck, keys=self.keys())

    def slides(self) -> list[JsonObject]:
        return as_objects(self.deck["slides"], "deck.json: slides")

    def page(self, si: int) -> int:
        """The PDF page (of the notes-free PDF) slide `si` shows."""
        return as_int(self.slides()[si]["page"], f"deck.json: slide {si}: page")

    def target(self) -> JsonObject:
        """The deck as another source's target (`ir_from_tex`): a copy whose slides carry their
        frame's label as `key`, and the frame's index, as target.json has always had them."""
        out = copy.deepcopy(self.deck)
        for s, key, frame in zip(as_objects(out["slides"], "deck.json: slides"), self.keys(), self.frames):
            s["key"] = key
            s["frame_index"] = frame.index if frame is not None else None
        return out

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
        slide = self.slides()[si]
        for para in slide_paragraphs(slide):
            candidates: list[tuple[Path, Visible, int, int]] = []
            is_title = para.el.get("role") == "title"
            if is_title and vis.title and not slide.get("title_page"):
                candidates.append((frame.file, vis, vis.title[0], vis.title[1]))
            candidates += [(frame.file, vis, pos, len(vis.text)), (frame.file, vis, 0, len(vis.text))]
            if pre is not None:
                candidates.append((self.source.main, pre, 0, len(pre.text)))
            best: tuple[Path, Visible, WordMap] | None = None
            for path, v, lo, hi in candidates:
                wm = locate_words_in(para.text, v, lo, hi)
                if best is None or wm.score > best[2].score + 1e-9:
                    best = (path, v, wm)
                if wm.score >= 0.8:
                    break
            assert best is not None  # there are always two candidates
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
            out[(as_str(para.el["id"], "element id"), para.pi)] = ParaLoc(path, v, wm, lo, hi, item, is_title)
        return out


def title_page_visible(cand: Candidate) -> Visible | None:
    """Printed text of \\title, \\subtitle, \\author, \\institute and \\date in the preamble."""
    text = cand.masked(cand.source.main)
    end = cand.source.preamble_end()
    out = empty_visible()
    for name in ("title", "subtitle", "author", "institute", "date"):
        m = re.search(r"\\" + name + r"\s*(?=[\[{])", text[:end])
        if not m:
            continue
        args, _ = read_args(text, m.end(), "oM")
        if args[1]:
            part = build_visible(text, args[1][1], args[1][2], title_frame=False)
            for ch, a, b in zip(part.text, part.starts, part.ends):
                out.add(ch, a, b)
            out.add(PARA, args[1][2], args[1][2])
    return out if out.text else None


class Workspace:
    """A working copy of the source tree, compiled and classified on demand."""

    def __init__(self, tex: Path, work: Path, *, handout: bool, engine: str | None, fresh: bool):
        """`engine`: None for the one the source names (`Source.engine`); `fresh`: copy the source
        tree again over what an earlier run left in `work`."""
        self.original = Path(tex).resolve()
        self.root = self.original.parent
        self.work = Path(work).resolve()
        self.src = self.work / "src"
        self.build_dir = self.work / "build"
        if fresh and self.src.exists():
            shutil.rmtree(self.src)
        if not self.src.exists():
            copy_tree(self.root, self.src)
        self.originals = source_hashes(self.root, self.src)
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
        before = aux_state(self.build_dir)
        for attempt in range(3):
            r = subprocess.run(cmd, cwd=self.src, capture_output=True, text=True, errors="replace", env=tex_env())
            log_path = self.build_dir / f"{job}.log"
            log = log_path.read_text(errors="replace") if log_path.exists() else r.stdout
            if r.returncode != 0:
                return None, error_excerpt(log)
            before, after = aux_state(self.build_dir), before
            if (before == after and not needs_rerun(log)) or attempt == 2:
                break
        return self.build_dir / f"{job}.pdf", ""

    def build(self, out: Path, target_has_notes: bool,
              compiled: tuple[Path | None, str] | None) -> Candidate | str:
        """Compile, extract and classify like `convert` (overlays: last step of each frame), and
        map every slide to its frame. A string is a compile error.

        `compiled`: what `compile` already returned (None: compile now), where the caller ran it while it had nothing
        else to do (`converge` reads the deck meanwhile). Taken only when this workspace stood at
        the same options then, so a caller cannot hand over a PDF of another document."""
        from .classify import classify
        from .extract import extract, select_overlays
        from .ir import deck_json
        from .notes import prepare as prepare_notes

        reuse = compiled is not None and self.notes == target_has_notes
        self.notes = target_has_notes
        pdf, err = compiled if reuse and compiled is not None else self.compile()
        if pdf is None:
            return err
        out.mkdir(parents=True, exist_ok=True)
        prepared = prepare_notes(pdf, out)
        raw = extract(prepared.pdf, prepared.labels)
        for page in raw["pages"]:
            page["notes"] = prepared.notes.get(page["index"])
        kept_original = original_pages(pdf, prepared)
        selected = select_overlays(raw, "last")
        classified = classify(selected)
        deck = deck_json(classified)
        slides = as_objects(deck["slides"], "deck.slides")
        if any(e["kind"] == "shape" for s in classified["slides"] for e in s["elements"]):
            from .pdf import Document
            from .render import keep_visible_shapes
            raw_pages = {p["index"]: p for p in selected["pages"]}
            doc = Document(prepared.pdf)
            try:  # as convert's render does: beamer's soft-masked shadow boxes aren't panels
                for slide in slides:
                    index = as_int(slide["page"], "slide.page")
                    keep_visible_shapes(doc[index], slide, raw_pages[index])
            finally:
                doc.close()
        sync = synctex_pages(self.build_dir / f"{self.main.stem}.synctex.gz")
        frames_by_page = page_frames(self.source, sync, [p["label"] for p in raw["pages"]], self.src)
        frames = []
        for slide in classified["slides"]:
            orig = kept_original[slide["page"]] if slide["page"] < len(kept_original) else slide["page"]
            frames.append(frames_by_page[orig] if orig < len(frames_by_page) else None)
        words = {p["index"]: [w for s in p["spans"] for w in s["text"].split()] for p in selected["pages"]}
        return Candidate(self.source, prepared.pdf, deck, frames, locs={}, text_masked={}, words=words)


def original_pages(pdf: Path, prepared: "Prepared") -> list[int]:
    """Page index in the compiled PDF of each page of the notes-free PDF."""
    if prepared.mode != "note pages":
        return list(range(10 ** 5))
    from .extract import shown_spans
    from .notes import _note_header
    from .pdf import Document
    doc = Document(pdf)
    try:
        keep = []
        for page in doc:
            spans = shown_spans(page) if page.index else []
            header = _note_header(page, spans, page.rect) if page.index else None
            if header is None:
                keep.append(page.index)
        return keep
    finally:
        doc.close()


# What rerunfilecheck reports for an auxiliary file that came out empty: the MD5 of nothing.
EMPTY_AUX = "D41D8CD98F00B204E9800998ECF8427E;0."


def needs_rerun(log: str) -> bool:
    """Whether LaTeX asked for another pass. An outline file that went from none to empty is not a
    reason: a deck with no sections has no bookmarks, and hyperref's "Rerun to get outlines right"
    after the first pass of every such document doubled each compile (an adopted deck is all
    `[plain]` frames; devfest2020's two passes took 28 s where one gives the same PDF)."""
    for name in re.findall(r"Package rerunfilecheck Warning: File `([^']+)' has changed", log):
        after = re.search(rf"Checksums for `{re.escape(name)}':\s*\n\(rerunfilecheck\)\s+Before:.*\n"
                          r"\(rerunfilecheck\)\s+After:\s*(\S+)", log)
        if not after or after.group(1) != EMPTY_AUX:
            return True
    # rerunfilecheck's own lines are settled above; anything else asking for a pass still counts
    other = "\n".join(l for l in log.splitlines() if not l.startswith("(rerunfilecheck)"))
    return bool(re.search(r"Rerun to get|may have changed\. Rerun", other))


#: What a pass reads at its start and writes at its end; while any of them moves, the next run
#: draws something different (labels, beamer's navigation and frame total, the TOC, bookmarks).
AUX_SUFFIXES = (".aux", ".toc", ".nav", ".snm", ".out", ".lof", ".lot", ".bbl", ".vrb")


def aux_state(folder: Path) -> dict[str, int]:
    """A digest of every auxiliary file under `folder`, for telling one pass from the next.

    The rule `needs_rerun` cannot state: a talk with no sections asks for no rerun after its first
    pass (rightly - there are no bookmarks to settle) while Madrid's footline still reads `2/1`,
    beamer's \\inserttotalframenumber coming out of the .nav the *next* pass reads. So a compile
    runs again while the auxiliary files are still moving (latexmk's rule), which on a folder the
    turn before left settled - what an agent's edit -> compile -> sync loop hands it - is no second
    pass at all.
    """
    return {str(p.relative_to(folder)): zlib.crc32(p.read_bytes())
            for p in (folder.rglob("*") if folder.is_dir() else ())
            if p.suffix in AUX_SUFFIXES and p.is_file()}


def error_excerpt(log: str) -> str:
    lines = log.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("!") or re.match(r"^.*:\d+: ", ln):
            return "\n".join(lines[i:i + 6])
    return "\n".join(lines[-12:])


def source_hashes(root: Path, src: Path) -> dict[str, str]:
    """sha1 of every file the working copy was made from, keyed by its path in the real tree.
    `pull --apply` compares them before it writes: a file the person edited while the loop was
    compiling is never overwritten (docs/sync.md, "If a sync or a pull dies")."""
    out: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        for name in filenames:
            p = root / rel / name
            try:
                out[str(p)] = hashlib.sha1(p.read_bytes()).hexdigest()
            except OSError:
                pass
    return out


def backup_for(path: Path) -> Path:
    """A backup name that takes nothing away: `<file>.bak`, then `<file>.bak2`, `.bak3`, ..."""
    candidate, n = path.with_name(path.name + ".bak"), 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak{n}")
        n += 1
    return candidate


def keep_backup(path: Path, new: "Path | str") -> Path | None:
    """Copy `path` aside before `new` replaces it (None: nothing to keep, or it holds that content
    already). A second `pull --apply` must not write over the copy the first one made - that copy
    is the author's own version, and the file itself is by then the first pull's work."""
    if not path.exists():
        return None
    old = path.read_bytes()
    fresh = new.read_bytes() if isinstance(new, Path) else new.encode("utf-8")
    if old == fresh:
        return None
    bak = backup_for(path)
    shutil.copy2(path, bak)
    return bak


def copy_tree(src: Path, dst: Path) -> list[Path]:
    """Copies the source tree (build products and huge files left out). Returns the backups it made:
    `dst` is normally a fresh folder, but `pull --out` may be pointed at a tree that holds files of
    its own, and those are kept (`keep_backup`) instead of being written over."""
    dst = Path(dst).resolve()
    backups = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                       and (Path(dirpath) / d).resolve() != dst and dst not in (Path(dirpath) / d).resolve().parents
                       and (Path(dirpath) / d).resolve() not in dst.parents]
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix in BUILD_EXT or name.endswith(".synctex.gz") or p.stat().st_size > 50_000_000:
                continue
            bak = keep_backup(dst / rel / name, p)
            if bak:
                backups.append(bak)
            shutil.copy2(p, dst / rel / name)
    return backups


# ---------------------------------------------------------------- source structure helpers

def line_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand [start, end) to whole lines when only whitespace surrounds it on them."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    le = len(text) if le < 0 else le + 1
    if text[ls:start].strip() or text[end:le].strip():
        return start, end
    return ls, le


INDENT_RE = re.compile(r"[ \t]*")


def indent_at(text: str, pos: int) -> str:
    ls = text.rfind("\n", 0, pos) + 1
    m = INDENT_RE.match(text, ls)
    return m.group(0) if m else ""  # it always matches: nothing is an indent too


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


# ---------------------------------------------------------------- pictures

TIKZ = r"\usepackage{tikz}"
LATEX_PICTURES = (".png", ".jpg", ".jpeg", ".pdf")
FIGURE_ENV_RE = re.compile(r"\\begin\s*\{(tikzpicture|pgfpicture)\}")
GRAPHICS_OPTS_RE = re.compile(r"\\includegraphics\s*(\[[^\]]*\])?")
INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics\s*(<[^>]*>)?\s*(\[([^\]]*)\])?")
WRAPPERS = [  # (text right before the wrapped command, what closes it)
    (re.compile(r"\\reflectbox\s*\{\s*$"), "}"),
    (re.compile(r"\\rotatebox\s*(\[[^\]]*\])?\s*\{[^{}]*\}\s*\{\s*$"), "}"),
    (re.compile(r"\\(resizebox|scalebox)\*?\s*\{[^{}]*\}(\s*\{[^{}]*\})?\s*(\[[^\]]*\])?\s*\{\s*$"), "}"),
    (re.compile(r"\\tikz\s*\\node\s*\[[^\]]*\]\s*\{\s*$"), "};"),
]


@dataclass
class Picture:
    rel: str                          # as written in \includegraphics (relative to the main file)
    path: Path
    natural: tuple[float, float]      # natural size in bp: what trim counts in


@dataclass
class PictureSource:
    """Where a picture of a slide comes from: an \\includegraphics or a tikzpicture/pgfpicture,
    with the wrappers pull writes around it (\\rotatebox, \\reflectbox, \\tikz\\node) or a
    \\resizebox/\\scalebox."""
    kind: str                         # "graphics" or "env"
    start: int                        # span with its wrappers
    end: int
    inner: tuple[int, int]            # the command or the environment alone
    block: tuple[int, int] | None     # the textblock* around it
    xy: tuple[float, float] | None    # that textblock's position
    overlay: bool                     # tikzpicture[overlay]


def picture_slug(alt: str | None) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", alt or "").encode("ascii", "ignore").decode().lower()
    if text.startswith("b2s"):  # the converter's own ids and tags
        return "picture"
    return "-".join(re.findall(r"[a-z0-9]+", text))[:40].strip("-") or "picture"


def natural_size(path: Path) -> tuple[float, float]:
    """The size graphicx gives a file before scaling: pixels at the file's resolution (72 dpi
    without one), a PDF's first page."""
    if path.suffix.lower() == ".pdf":
        from .pdf import Document
        doc = Document(path)
        try:
            return doc[0].width, doc[0].height
        finally:
            doc.close()
    from PIL import Image
    with Image.open(path) as img:
        w, h = img.size
        dpi = img.info.get("dpi")
    if dpi and dpi[0] and dpi[1] and float(dpi[0]) > 1:
        return w * 72 / float(dpi[0]), h * 72 / float(dpi[1])
    return float(w), float(h)


Look = tuple[float, "Floats32", float]
"""A picture's look (`picture_look`): aspect ratio, 64x64 grey array, pixel count."""


def picture_look(path: Path) -> Look | None:
    """(aspect ratio, 64x64 grey array, pixel count) of a raster file or a one-page PDF; None if unreadable."""
    import numpy as np
    from PIL import Image
    pixels: float
    try:
        if path.suffix.lower() == ".pdf":
            from .pdf import Document
            doc = Document(path)
            try:
                if len(doc) != 1:
                    return None
                w, h = doc[0].width, doc[0].height
                img = Image.fromarray(doc[0].render(256 / max(w, h), clip=None, transparent=False))
                pixels = float("inf")  # vector: better than any raster
            finally:
                doc.close()
        else:
            img = Image.open(path)
            img.seek(0)
            pixels = img.size[0] * img.size[1]
        rgba = img.convert("RGBA")
        ground = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        ground.alpha_composite(rgba)
        grey = np.asarray(np.asarray(ground.convert("L").resize((64, 64), Image.Resampling.BILINEAR), np.float32) / 255,
                          dtype=np.float32)
        return rgba.size[0] / rgba.size[1], grey, pixels
    except Exception:  # noqa: BLE001 - any unreadable file is just not a candidate
        return None


def same_look(a: Look | None, b: Look | None) -> bool:
    """Two pictures that are the same image at another resolution or encoding."""
    import numpy as np
    if a is None or b is None or abs(math.log(a[0] / b[0])) > 0.02:
        return False
    x, y = a[1].ravel(), b[1].ravel()
    if float(np.abs(x - y).mean()) > 0.04:
        return False
    if x.std() < 0.02 or y.std() < 0.02:
        return float(np.abs(x - y).mean()) < 0.01
    return float(np.corrcoef(x, y)[0, 1]) >= 0.98


class PictureFile:
    """A picture file of the source tree, its look read when first asked (`find_picture`)."""

    def __init__(self, path: Path, sha1: str) -> None:
        self.path = path
        self.sha1 = sha1
        self.looked = False
        self.look: Look | None = None


def picture_index(root: Path) -> list[PictureFile]:
    """Picture files of a source tree LaTeX can include."""
    out: list[PictureFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in LATEX_PICTURES and p.stat().st_size <= 50_000_000:
                out.append(PictureFile(p, hashlib.sha1(p.read_bytes()).hexdigest()))
    return out


def find_picture(index: list[PictureFile], sha: str, look: Look | None) -> PictureFile | None:
    """The same picture in the source tree: identical bytes, else the best-resolution file that
    looks the same (a PDF before any raster)."""
    exact = next((e for e in index if e.sha1 == sha), None)
    if exact or look is None:
        return exact
    found: list[tuple[PictureFile, Look]] = []
    for e in index:
        if not e.looked:
            e.look, e.looked = picture_look(e.path), True
        if e.look is not None and same_look(look, e.look):
            found.append((e, e.look))
    return max(found, key=lambda f: f[1][2])[0] if found else None


def picture_edits(te: LoopPicture) -> bool:
    return bool(te.crop is not None or te.rotation or te.flip or te.opacity or te.outline is not None or te.bake
                or te.mask)


def picture_latex(te: LoopPicture, pic: Picture, ctx: "Context", size: list[str] | None) -> str:
    """\\includegraphics for a deck picture with its Slides edits as LaTeX: crop -> trim/clip (in the
    file's natural bp), rotation -> angle (Slides turns clockwise about the centre; the rotated box
    is placed by its bounding box, so no origin is needed), mirroring -> \\reflectbox, transparency
    and outline -> a \\tikz node (text opacity, draw), turned as a whole by \\rotatebox."""
    x0, y0, x1, y1 = te.frame
    opts: list[str] = []
    crop = te.crop
    if crop is not None:
        nw, nh = pic.natural
        trim = (crop.l * nw, crop.b * nh, crop.r * nw, crop.t * nh)
        opts += ["trim=" + " ".join(f"{max(0.0, v):.2f}" for v in trim), "clip"]
    opts += size or [f"width={x1 - x0:.1f}pt", f"height={y1 - y0:.1f}pt"]
    angle = round(-(te.rotation or 0.0), 2)
    node: list[str] = []
    if te.opacity is not None and te.opacity < 0.995:
        node.append(f"text opacity={te.opacity:.2f}")
    outline = te.outline
    if outline is not None:
        node += [f"draw={colour_name(outline.color, ctx.colours)}", f"line width={outline.weight:.2f}pt"]
        if outline.dash != "SOLID":
            node.append("dotted" if "DOT" in outline.dash and "DASH" not in outline.dash else "dashed")
    wrapped = bool(node or te.flip or te.mask)
    if angle and not wrapped:
        opts.append(f"angle={angle:g}")
    cmd = f"\\includegraphics[{','.join(opts)}]{{{pic.rel}}}"
    if te.flip:
        cmd = f"\\reflectbox{{{cmd}}}"
    if te.mask == "ellipse":
        # shown only inside the ellipse its frame inscribes, the outline drawn round it too
        ctx.packages.add(TIKZ)
        radii = f"ellipse [x radius={(x1 - x0) / 2:.2f}pt,y radius={(y1 - y0) / 2:.2f}pt]"
        fill = [n for n in node if n.startswith("text opacity")]
        line = [n for n in node if not n.startswith("text opacity")]
        cmd = (f"\\tikz{{\\begin{{scope}}\\clip (0pt,0pt) {radii};"
               f"\\node[inner sep=0pt{''.join(',' + n for n in fill)}]{{{cmd}}};\\end{{scope}}"
               + (f"\\draw[{','.join(line)}] (0pt,0pt) {radii};" if line else "") + "}")
    elif node:
        ctx.packages.add(TIKZ)
        cmd = f"\\tikz\\node[inner sep=0pt,{','.join(node)}]{{{cmd}}};"
    if angle and wrapped:
        cmd = f"\\rotatebox{{{angle:g}}}{{{cmd}}}"
    return cmd


def picture_block(te: LoopPicture, pic: Picture, ctx: "Context", ind: str) -> str:
    """A picture at its deck position: a textblock* at the top-left of its bounding box."""
    x0, y0, x1, y1 = te.bbox
    pad = te.outline.weight / 2 if te.outline is not None and not te.rotation else 0.0
    return (f"{ind}\\begin{{textblock*}}{{{x1 - x0 + 2 * pad:.1f}pt}}({x0 - pad:.1f}pt,{y0 - pad:.1f}pt)\n"
            f"{ind}  {picture_latex(te, pic, ctx, None)}\n{ind}\\end{{textblock*}}\n")


def wrapper_span(text: str, a: int, b: int) -> tuple[int, int]:
    """[a, b) widened to the wrappers directly around it."""
    while True:
        head = text[max(0, a - 300):a]
        for pat, close in WRAPPERS:
            m = pat.search(head)
            if not m:
                continue
            j = skip_space(text, b)
            if not text.startswith("}", j):
                continue
            nb = j + 1
            if close == "};":
                k = skip_space(text, nb)
                if not text.startswith(";", k):
                    continue
                nb = k + 1
            a, b = a - (len(head) - m.start()), nb
            break
        else:
            return a, b


def picture_sources(text: str, frame: Frame) -> list[PictureSource]:
    """Pictures a frame's source draws, in source order: outermost tikzpicture/pgfpicture
    environments and the \\includegraphics outside them."""
    lo, hi = frame.body, frame.body_end
    envs = []
    for m in FIGURE_ENV_RE.finditer(text, lo, hi):
        if any(a <= m.start() < b for a, b, _ in envs):
            continue
        name = re.escape(m.group(1))
        depth, end = 1, None
        for t in re.finditer(r"\\(begin|end)\s*\{" + name + r"\}", text[m.end():hi]):
            depth += 1 if t.group(1) == "begin" else -1
            if depth == 0:
                end = m.end() + t.end()
                break
        if end is None:
            continue
        args, _ = read_args(text, m.end(), "o")
        overlay = bool(args[0] and re.search(r"(^|,)\s*overlay\s*(,|$)", text[args[0][1]:args[0][2]]))
        envs.append((m.start(), end, overlay))
    blocks = [(float(m.group("x")), float(m.group("y")), m.start(), text.find("\\end{textblock*}", m.end()))
              for m in TEXTBLOCK_RE.finditer(text, lo, hi)]
    items = [("env", a, b, ov) for a, b, ov in envs]
    for m in re.finditer(r"\\includegraphics\b", text[:hi]):
        if m.start() < lo or any(a <= m.start() < b for a, b, _ in envs):
            continue
        items.append(("graphics", m.start(), read_args(text, m.end(), "<som")[1], False))
    out = []
    for kind, a, b, ov in items:
        s, e = wrapper_span(text, a, b)
        block = next(((x, y, bs, be + len("\\end{textblock*}")) for x, y, bs, be in blocks if bs < a and be >= b), None)
        out.append(PictureSource(kind, s, e, (a, b), block[2:] if block else None, block[:2] if block else None, ov))
    return sorted(out, key=lambda p: p.start)


class _Column:
    def __init__(self, x0: float, x1: float, e: LoopElement) -> None:
        self.x0, self.x1 = x0, x1
        self.els = [e]


def reading_order(els: Sequence[LoopElement]) -> list[LoopElement]:
    """Elements column by column (boxes overlapping horizontally share a column), top to bottom:
    the order a beamer source writes them in."""
    cols: list[_Column] = []
    for e in sorted(els, key=lambda e: e.bbox[0]):
        x0, x1 = e.bbox[0], e.bbox[2]
        for col in cols:
            if min(col.x1, x1) - max(col.x0, x0) > 0.3 * min(col.x1 - col.x0, x1 - x0):
                col.els.append(e)
                col.x0, col.x1 = min(col.x0, x0), max(col.x1, x1)
                break
        else:
            cols.append(_Column(x0, x1, e))
    return [e for col in cols for e in sorted(col.els, key=lambda e: e.bbox[1])]


def overlap_share(a: Sequence[float], b: Sequence[float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / max(1e-6, min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])))


def commented_out(orig: str, a: int, b: int, ind: str, note: str) -> str:
    """Lines [a, b) as comments under a `% b2s pull:` note."""
    lines = orig[a:b].rstrip("\n").split("\n")
    return f"{ind}% b2s pull: {note}\n" + "".join(
        f"{l[:len(l) - len(l.lstrip())]}% {l.lstrip()}\n" if l.strip() else "%\n" for l in lines)


# ---------------------------------------------------------------- the planner

class FlowShift:
    """What `Planner.flow_shift` learnt of one flow move across rounds: its tries, the last error and
    \\vspace step, and the gain those measured. It changes in place, round after round."""

    def __init__(self) -> None:
        self.tries = 0
        self.err: float | None = None
        self.dv = 0.0
        self.gain: float | None = None


class Planner:
    """Edits for one round's open residuals, and the residuals it could write none for."""

    def __init__(self, cand: Candidate, comp: Comparison, target: JsonObject, ctx: Context, ws: Workspace,
                 blocked: set[Signature], last_values: "dict[Signature, FlowShift | int]",
                 hashes: Mapping[int, PictureHash]) -> None:
        self.cand, self.comp, self.target, self.ctx, self.ws = cand, comp, target, ctx, ws
        self.blocked = blocked
        self.last = last_values
        self.hashes = hashes
        self.replacing: dict[tuple[int, str], str] = {}   # (slide, target element) -> current element it replaces
        self.replaced: set[tuple[int, str]] = set()       # (slide, current element) taken by a replacement
        self.edits: list[Edit] = []
        self.unresolved: list[Unresolved] = []
        self.shifted: set[int] = set()  # slides whose flow got a \vspace this round
        deck = cand.deck
        self.cur_slides = cand.slides()
        self.tgt_slides = as_objects(target["slides"], "target: slides")
        self.t2c = {j: i for i, j in comp.slides if i is not None and j is not None}
        self.c2t = {i: j for i, j in comp.slides if i is not None and j is not None}
        self.base = body_style(deck)
        self.used_labels = {f.label for f in cand.source.frames if f.label}

    # -- helpers
    def fail(self, r: Residual, why: str) -> None:
        self.unresolved.append(given_up(r, why))

    def edit(self, file: Path, start: int, end: int, text: str, r: Residual, note: str) -> None:
        self.edits.append(Edit(file, start, end, text, r.kind, signature(r), note))

    def find_element(self, slide: JsonObject, eid: str) -> LoopElement | None:
        """The element `eid` of a slide, None when the slide has none."""
        found = next((e for e in as_objects(slide["elements"], "slide: elements") if e["id"] == eid), None)
        return None if found is None else loop_element(found, f"element {eid}")

    def element(self, slide: JsonObject, eid: str) -> LoopElement:
        """The element `eid` of a slide a residual names: compare made the residual from it, so it
        is there."""
        el = self.find_element(slide, eid)
        if el is None:
            raise LookupError(f"a residual names element {eid}, which its slide does not have")
        return el

    def plan(self) -> tuple[list[Edit], list[Unresolved]]:
        res = [r for r in self.comp.open() if signature(r) not in self.blocked]
        structural = [r for r in res if isinstance(r, (SlideMissing, SlideExtra, SlideOrder))]
        if structural:
            for s in structural:
                if isinstance(s, SlideMissing):
                    self.slide_missing(s)
                elif isinstance(s, SlideExtra):
                    self.slide_extra(s)
                else:
                    self.slide_order(s)
            return self.edits, self.unresolved
        paired = [r for r in res if not isinstance(r, (SlideMissing, SlideExtra, SlideOrder))]
        # A slide whose content changes size this round (items, boxes, a resized picture) is
        # measured again before anything on it is moved.
        self.pair_replacements(paired)
        reshaping = {r.slide for r in paired if r.kind in RESHAPING or
                     (isinstance(r, BoxGeometry) and (abs(r.dw) > TOL["size"] or abs(r.dh) > TOL["size"]))}
        paired = [r for r in paired if not (isinstance(r, TextGeometry) and r.slide in reshaping) and
                  not (isinstance(r, BoxGeometry) and r.slide in reshaping and
                       abs(r.dw) <= TOL["size"] and abs(r.dh) <= TOL["size"])]
        lists_done: set[tuple[Path, int]] = set()
        for r in sorted(paired, key=lambda r: (r.kind == "geometry", r.cur[1] if isinstance(r, TextGeometry) else 0)):
            if isinstance(r, (ParagraphMissing, ParagraphExtra, ParagraphOrder, BulletResidual)):
                self.list_residual(r, lists_done)
            elif isinstance(r, NotesResidual):
                self.notes(r)
            elif isinstance(r, BackgroundResidual):
                self.background(r)
            elif isinstance(r, TextResidual):
                self.text(r)
            elif isinstance(r, StyleResidual):
                self.style(r)
            elif isinstance(r, AlignResidual):
                self.fail(r, "paragraph alignment is not translated")
            elif isinstance(r, (TextMissing, BoxMissing)):
                self.element_missing(r)
            elif isinstance(r, (TextGeometry, BoxGeometry)):
                self.geometry(r)
            elif isinstance(r, TextExtra):
                pass  # text: paragraph_extra residuals delete the words
            elif isinstance(r, BoxExtra):
                self.element_extra(r)
            elif isinstance(r, DiagramResidual):
                self.fail(r, "diagram labels are not translated: edit the node texts of the tikzpicture")
            elif isinstance(r, ImageResidual):
                self.image(r)
            elif isinstance(r, InlineImage):
                self.fail(r, "a formula or icon picture inside a text line was replaced in the deck: it is not "
                             "written back (it would turn math into a picture); change the source by hand")
            elif isinstance(r, ShapeResidual):
                self.fail(r, "shape colours are not translated")
            elif isinstance(r, TableResidual):
                self.fail(r, "table cells are not translated: edit the tabular cells")
            else:
                assert_never(r)
        return self.edits, self.unresolved

    # -- slides
    def slide_missing(self, r: SlideMissing) -> None:
        j = r.target_slide
        ts = self.tgt_slides[j]
        prev = next((self.t2c[k] for k in range(j - 1, -1, -1) if k in self.t2c and self.cand.frames[self.t2c[k]]), None)
        frame = None if prev is None else self.cand.frames[prev]
        if frame is not None:
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
        key = _loop_str(ts, "key", "target slide")
        label = key
        # two slides carrying the same key (a slide duplicated in the deck) would otherwise get the
        # same \label{}: hyperref keeps only the first, silently breaking identity for later syncs
        if key is not None and is_frame_label(key) and key in self.used_labels:
            n = 2
            while f"{key}-{n}" in self.used_labels:
                n += 1
            label = f"{key}-{n}"
            self.ctx.label_notes.append(
                f"slide {j + 1} ({slide_title(ts) or key}): another slide already carries the label "
                f"'{key}' (a duplicated slide?) - wrote this frame as '{label}' instead of a repeated "
                f"\\label{{{key}}}, which hyperref would silently drop")
        if label is not None and is_frame_label(label):
            self.used_labels.add(label)
        self.edit(file, pos, pos, "\n" + frame_latex(loop_texts(ts, "target slide"), key,
                                                     _loop_str(ts, "notes", "target slide"), self.level_style,
                                                     self.ctx, label), r, "")

    def slide_extra(self, r: SlideExtra) -> None:
        frame = self.cand.frames[r.slide]
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
        self.edit(frame.file, a, b, "", r, "")

    def slide_order(self, r: SlideOrder) -> None:
        i, j = r.slide, r.target_slide
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
        self.edit(frame.file, a, b, "", r, "")
        self.edits.append(Edit(frame.file, pos, pos, "\n" + block, "slide_order", signature(r) + ("insert",), ""))

    # -- notes and background
    def notes(self, r: NotesResidual) -> None:
        frame = self.cand.frames[r.slide]
        if frame is None:
            self.fail(r, "the page comes from no frame")
            return
        text = self.cand.masked(frame.file)
        found = list(re.finditer(r"\\note\s*(<[^>]*>)?\s*(\[[^\]]*\])?\s*\{", text[frame.body:frame.body_end]))
        new = r.tgt or ""
        body = "\n\n".join(latex_escape(p) for p in new.split("\n") if p.strip())
        if found:
            m = found[0]
            start = frame.body + m.start()
            close = match_group(text, frame.body + m.end() - 1)
            if not new:
                a, b = line_span(text, start, close)
                self.edit(frame.file, a, b, "", r, "")
            else:
                self.edit(frame.file, frame.body + m.end(), close - 1, body, r, "")
            for extra in found[1:]:
                s = frame.body + extra.start()
                e = match_group(text, frame.body + extra.end() - 1)
                a, b = line_span(text, s, e)
                self.edits.append(Edit(frame.file, a, b, "", "notes", signature(r) + (s,), ""))
        elif new:
            pos = text.rfind("\n", 0, frame.body_end) + 1
            ind = indent_at(text, frame.start) + "  "
            self.edit(frame.file, pos, pos, f"{ind}\\note{{{body}}}\n", r, "")

    def background(self, r: BackgroundResidual) -> None:
        frame = self.cand.frames[r.slide]
        if frame is None:
            self.fail(r, "the page comes from no frame")
            return
        text = self.cand.masked(frame.file)
        name = colour_name(r.tgt, self.ctx.colours)
        before = text[max(0, frame.start - 200):frame.start]
        m = re.search(r"\{\\setbeamercolor\{background canvas\}\{bg=([^}]*)\}\s*$", before)
        if m:
            s = frame.start - len(before) + m.start(1)
            self.edit(frame.file, s, s + len(m.group(1)), name, r, "")
            return
        a, b = line_span(text, frame.start, frame.end)
        self.edit(frame.file, a, a, f"{{\\setbeamercolor{{background canvas}}{{bg={name}}}\n", r, "")
        self.edits.append(Edit(frame.file, b, b, "}\n", "background", signature(r) + ("close",), ""))

    # -- text
    def loc(self, slide: int, element: str, para: int) -> ParaLoc | None:
        return self.cand.paragraph_locations(slide).get((element, para))

    def fail_op(self, r: TextResidual, op: WordOp, source: str | None, why: str) -> None:
        self.unresolved.append(Unresolved(residual=r, op=op, source=source, why=why, frame=None))

    def text(self, r: TextResidual) -> None:
        loc = self.loc(r.slide, r.element, r.para)
        if loc is None:
            self.fail(r, "paragraph not found in the source")
            return
        src = self.cand.masked(loc.file)
        cur_words = r.cur.split()
        for op in r.ops:
            c0, c1 = op.c
            new = latex_escape(op.tgt)
            if any(HOLE in w or OPAQUE in w for w in cur_words[c0:c1]):
                self.fail_op(r, op, None, "the words include a formula")
                continue
            if op.op == "insert":
                want = [norm_word(w) for w in op.tgt.split()]
                near: list[list[str]] = []
                before = loc.words.vis[c0 - 1] if c0 > 0 else None
                after = loc.words.vis[c0] if c0 < len(cur_words) else None
                if before is not None:
                    near.append([norm_word(w) for w in loc.visible.text[before[1]:].split()[:len(want)]])
                if after is not None:
                    near.append([norm_word(w) for w in loc.visible.text[:after[0]].split()[-len(want):]])
                if want in near:
                    self.fail_op(r, op, None, "the words are already in the source: the conversion splits the "
                                              "paragraph differently (box width, line breaks)")
                    continue
                if before is not None:
                    at = loc.visible.ends[before[1] - 1]
                    self.edits.append(Edit(loc.file, at, at, " " + new, "text", signature(r) + (c0,), ""))
                elif after is not None:
                    at = loc.visible.starts[after[0]]
                    self.edits.append(Edit(loc.file, at, at, new + " ", "text", signature(r) + (c0,), ""))
                else:
                    self.fail_op(r, op, None, "insertion point not found")
                continue
            span = loc.words.span(c0, c1)
            if span is None:
                self.fail_op(r, op, None, "words not found in the source")
                continue
            a, b = loc.visible.starts[span[0]], loc.visible.ends[span[1] - 1]
            if plain_source(src[a:b]):
                if op.op == "delete":
                    a, b = widen_delete(src, a, b)
                    new = ""
                self.edits.append(Edit(loc.file, a, b, new, "text", signature(r) + (c0,), ""))
                continue
            # the words cross commands: replace word by word where counts agree
            tw = op.tgt.split()
            if op.op == "replace" and len(tw) == c1 - c0:
                ok = True
                parts: list[Edit] = []
                for d in range(c1 - c0):
                    s = loc.words.span(c0 + d, c0 + d + 1)
                    if s is None:  # never: the words' whole span was found
                        ok = False
                        break
                    wa, wb = loc.visible.starts[s[0]], loc.visible.ends[s[1] - 1]
                    if not plain_source(src[wa:wb]):
                        ok = False
                        break
                    if cur_words[c0 + d] != tw[d]:
                        parts.append(Edit(loc.file, wa, wb, latex_escape(tw[d]), "text", signature(r) + (c0 + d,), ""))
                if ok:
                    self.edits += parts
                    continue
            self.fail_op(r, op, src[a:b], "the words span LaTeX commands")

    def style(self, r: StyleResidual) -> None:
        loc = self.loc(r.slide, r.element, r.para)
        if loc is None:
            self.fail(r, "paragraph not found in the source")
            return
        cs = self.cur_slides[r.slide]
        el = self.element(cs, r.element)
        para = element_paragraphs(el)[r.para]
        ptext = norm_text(para_text(para))
        # a style command (bold/italic/colour/size) is never wrapped around less than a whole word
        # by a person; only a character-level diff coincidence would suggest it - so unlike `text`,
        # a style edit always snaps outward to whole words (char_span, checked by the h6b hunt).
        span = char_span(ptext, r.c0, r.c1, loc, True)
        if span is None:
            self.fail(r, "styled words not found in the source")
            return
        a, b = span
        src = self.cand.masked(loc.file)
        change = r.change
        lo = loc.visible.starts[loc.lo] if not loc.title else max(0, a - 400)
        lo = max(0, min(lo, a) - 200)
        if isinstance(change, FlagChange):
            names, on_cmd, off_cmd = STYLE_CMDS[change.field]
            if change.tgt:
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
        elif isinstance(change, ColorChange):
            tgt = change.tgt
            base_colour = dominant(para, run_colour)
            g = enclosing_group(src, a, b, ("textcolor", "color", "alert"), lo)
            if g is not None and src[g[1]:g[2]].strip() == src[a:b].strip():
                cmd_start, c_start, c_end, g_end, name = g
                if compare_colour(tgt, base_colour) or name == "alert":
                    if name == "alert":
                        self.edit(loc.file, cmd_start, g_end, f"\\textcolor{{{colour_name(tgt, self.ctx.colours)}}}{{{src[c_start:c_end]}}}", r, "")
                    else:
                        mm = re.search(r"\\(textcolor|color)\s*(<[^>]*>)?\s*(\[[^\]]*\])?\s*\{([^}]*)\}", src[cmd_start:c_end])
                        if mm:
                            s = cmd_start + mm.start(4)
                            self.edit(loc.file, s, s + len(mm.group(4)), colour_name(tgt, self.ctx.colours), r, "")
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
            self.edit(loc.file, a, b, f"\\textcolor{{{colour_name(tgt, self.ctx.colours)}}}{{{src[a:b]}}}", r, "")
        elif isinstance(change, SizeChange):
            base_size = dominant(para, run_size)
            g = enclosing_group(src, a, b, tuple(SIZE_TABLES[11]) + ("fontsize",), lo)
            if g is not None and src[g[1]:g[2]].strip() == src[a:b].strip():
                if base_size is not None and abs(change.tgt - base_size) <= 0.06 * base_size:
                    self.unwrap(loc.file, src, g, a, b, r)
                else:
                    k = skip_space(src, g[0] + 1)
                    mm = re.match(r"\\(?:[A-Za-z]+size|tiny|small|large|Large|LARGE|huge|Huge)\b|\\fontsize\{[^}]*\}\{[^}]*\}\\selectfont",
                                  src[k:])
                    if mm:
                        self.edit(loc.file, k, k + mm.end(), size_switch(change.tgt, self.ctx.pt_option), r, "")
                    else:
                        self.fail(r, "size command not understood")
                return
            if not balanced(src[a:b]):
                self.fail(r, "sized range isn't balanced LaTeX")
                return
            self.edit(loc.file, a, b, f"{{{size_switch(change.tgt, self.ctx.pt_option)} {src[a:b]}}}", r, "")
        else:
            assert_never(change)

    def wrap(self, file: Path, a: int, b: int, cmd: str, r: StyleResidual) -> None:
        src = self.cand.masked(file)
        self.edit(file, a, b, f"\\{cmd}{{{src[a:b]}}}", r, "")

    def unwrap(self, file: Path, src: str, g: tuple[int, int, int, int, str], a: int, b: int, r: StyleResidual) -> None:
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
        self.edit(file, cmd_start, g_end, out, r, "")

    # -- lists and paragraphs
    def list_residual(self, r: ListResidual, done: set[tuple[Path, int]]) -> None:
        """A paragraph missing, extra or out of order, or a bullet changed: in a list, the list is
        rebuilt (once a round); outside one, the paragraph is inserted, deleted or itemized."""
        si, ti = r.slide, r.target_slide
        locs = self.cand.paragraph_locations(si)
        ts = self.tgt_slides[ti]
        # the list environment concerned, and where its source is
        if isinstance(r, ParagraphMissing):
            where = self.missing_paragraph(r, locs, ts)
            if where is None:
                return
        else:
            loc = locs.get((r.element, r.para))
            if loc is None:
                self.fail(r, "paragraph not found in the source")
                return
            lst = top_list(loc)
            if lst is None:
                if isinstance(r, ParagraphExtra):
                    self.delete_paragraph(loc, r)
                elif isinstance(r, BulletResidual) and r.tgt[0] is not None:
                    self.itemize_paragraph(loc, r)
                else:
                    self.fail(r, "paragraph is not in a list")
                return
            where = (loc, lst)
        loc, lst = where
        if (loc.file, lst.start) in done:
            return
        done.add((loc.file, lst.start))
        self.rebuild_list(si, ti, loc.file, loc.visible, lst, r)

    def missing_paragraph(self, r: ParagraphMissing, locs: Mapping[tuple[str, int], ParaLoc],
                          ts: JsonObject) -> tuple[ParaLoc, ListEnv] | None:
        """A missing paragraph written where it goes; the list to rebuild when it is an item of one
        (with where its predecessor's source is), None when that is all."""
        si = r.slide
        if self.target_element_is_new(ts, r.target_element, si):
            return None  # element_missing adds it as a whole
        after = r.after
        tpara = element_paragraphs(self.element(ts, r.target_element))[r.target_para]
        aloc = locs.get((after.element, after.para)) if after is not None else None
        if aloc is None and after is not None:
            self.fail(r, "predecessor not found in the source")
            return None
        if aloc is None:
            frame = self.cand.frames[si]
            if frame is None:
                self.fail(r, "the page comes from no frame")
                return None
            self.insert_paragraph(None, frame, tpara, r)
            return None
        lst = top_list(aloc)
        if lst is None or not tpara.get("bullet"):
            if lst is not None and aloc.item is not None:
                # a plain paragraph after a list: after the whole list
                text = self.cand.masked(aloc.file)
                pos = text.find("\n", lst.end)
                pos = len(text) if pos < 0 else pos + 1
                self.edit(aloc.file, pos, pos, indent_at(text, lst.start) + self.paragraph_latex(tpara) + "\n", r, "")
                return None
            self.insert_paragraph(aloc, None, tpara, r)
            return None
        return aloc, lst

    def target_element_is_new(self, ts: JsonObject, eid: str, si: int) -> bool:
        return any(isinstance(x, (TextMissing, BoxMissing)) and x.target_element == eid and x.target_slide == self.c2t.get(si)
                   for x in self.comp.open())

    def insert_paragraph(self, aloc: ParaLoc | None, frame: Frame | None, tpara: JsonObject, r: ParagraphMissing) -> None:
        body = self.paragraph_latex(tpara)
        if aloc is not None:
            text = self.cand.masked(aloc.file)
            _, end = aloc.src()
            pos = text.find("\n", end)
            pos = len(text) if pos < 0 else pos + 1
            self.edit(aloc.file, pos, pos, "\n" + indent_at(text, end) + body + "\n", r, "")
        elif frame is not None:
            text = self.cand.masked(frame.file)
            pos = text.find("\n", frame.body)
            pos = len(text) if pos < 0 else pos + 1
            self.edit(frame.file, pos, pos, indent_at(text, frame.start) + "  " + body + "\n\n", r, "")

    def delete_paragraph(self, loc: ParaLoc, r: ParagraphExtra) -> None:
        text = self.cand.masked(loc.file)
        a, b = loc.src()
        if loc.title:
            self.edit(loc.file, a, b, "", r, "")
            return
        a, b = line_span(text, a, b)
        self.edit(loc.file, a, b, "", r, "")

    def itemize_paragraph(self, loc: ParaLoc, r: BulletResidual) -> None:
        text = self.cand.masked(loc.file)
        a, b = line_span(text, *loc.src())
        env = "enumerate" if r.tgt[0] == "number" else "itemize"
        ind = indent_at(text, a)
        self.edit(loc.file, a, b, f"{ind}\\begin{{{env}}}\n{ind}  \\item {text[a:b].strip()}\n{ind}\\end{{{env}}}\n", r, "")

    def rebuild_list(self, si: int, ti: int, file: Path, vis: Visible, lst: ListEnv, r: ListResidual) -> None:
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
            loc = locs.get((as_str(cp.el["id"], "element id"), cp.pi))
            if loc is not None and loc.item is not None and loc.visible is vis and lst.start <= loc.item.start < lst.end:
                item_para[loc.item.start] = ci
        members_c = set(item_para.values())
        t_members = [j for j in range(len(tps)) if j in c_of_t and c_of_t[j] in members_c]
        if not t_members:
            self.fail(r, "list items not matched")
            return
        lo_t, hi_t = min(t_members), max(t_members)
        tls = [loop_paragraph(tp.p, f"target paragraph {j}") for j, tp in enumerate(tps)]
        # new bulleted target paragraphs right before, inside or right after the matched range
        while lo_t > 0 and lo_t - 1 not in c_of_t and tls[lo_t - 1].bullet is not None and \
                not self.target_element_is_new(ts, as_str(tps[lo_t - 1].el["id"], "element id"), si):
            lo_t -= 1
        while hi_t + 1 < len(tps) and hi_t + 1 not in c_of_t and tls[hi_t + 1].bullet is not None and \
                not self.target_element_is_new(ts, as_str(tps[hi_t + 1].el["id"], "element id"), si):
            hi_t += 1
        by_para = {ci: it for it_start, ci in item_para.items() for it in items if it.start == it_start}
        if self.small_list_edit(file, text, items, item_para, tps, tls, c_of_t, lo_t, hi_t, lst, r):
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
        lines: list[str] = []
        stack: list[str] = []
        for j in range(lo_t, hi_t + 1):
            tp, tl = tps[j], tls[j]
            if tl.bullet is None and tl.tab_x0 is None:
                continue
            level = tl.level
            numbered = tl.bullet is not None and tl.bullet.kind == "number"
            env = env_at_level.get(level) or ("enumerate" if numbered else "itemize")
            if tl.tab_x0 is not None and tl.bullet is None:
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
                if env == "description":
                    joined = self.paragraph_latex(tp.p)
                    head, _, rest = joined.partition("\t")
                    own = f"\\item[{head.strip()}] {rest.strip()}"
                else:
                    own = "\\item " + self.paragraph_latex(tp.p).strip()
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
        self.edit(file, a if a != lst.start else a, b, new if a != lst.start else new.lstrip(), r, "")

    def small_list_edit(self, file: Path, text: str, items: list[Item], item_para: dict[int, int], tps: list[Para],
                        tls: Sequence[LoopParagraph], c_of_t: dict[int, int], lo_t: int, hi_t: int, lst: ListEnv,
                        r: ListResidual) -> bool:
        """Items only deleted, or only added at an existing level: edit those lines alone. `tls`: the
        target paragraphs `tps` as the loop reads them."""
        desired = [(c_of_t.get(j), tls[j].level, j) for j in range(lo_t, hi_t + 1)
                   if tls[j].bullet is not None or tls[j].tab_x0 is not None]
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
                env = next((l for l in self.cand.paragraph_locations(r.slide).values()
                            for l in l.visible.lists if l.start < it.start < l.end and l.level == it.level), None)
                if env is not None and siblings and it is siblings[0]:
                    a, b = line_span(text, env.start, env.end)
                    self.edits.append(Edit(file, a, b, "", r.kind, signature(r) + (it.start,), ""))
                continue
            a, b = line_span(text, it.start, len(text[:it.end].rstrip()))
            self.edits.append(Edit(file, a, b, "", r.kind, signature(r) + (it.start,), ""))
        by_ci = {ci: it for ci, _, it in current}
        for lv, j, k in new:
            prev = next(((by_ci[c], l) for c, l, _ in reversed(desired[:k]) if c is not None), None)
            nxt = next(((by_ci[c], l) for c, l, _ in desired[k + 1:] if c is not None), None)
            line = "\\item " + self.paragraph_latex(tps[j].p).strip()
            if prev and prev[1] == lv:
                pos = text.find("\n", len(text[:prev[0].end].rstrip()))
                pos = len(text) if pos < 0 else pos + 1
                ind = indent_at(text, prev[0].start)
            elif nxt and nxt[1] == lv:
                pos = text.rfind("\n", 0, nxt[0].start) + 1
                ind = indent_at(text, nxt[0].start)
            else:
                return False
            self.edits.append(Edit(file, pos, pos, ind + line + "\n", r.kind, signature(r) + (j,), ""))
        return bool(new or removed)

    @staticmethod
    def same_env(a: Item, b: Item, lst: ListEnv, items: list[Item]) -> bool:
        """Items of one environment: no item of a shallower level between them."""
        lo, hi = sorted((a.start, b.start))
        return not any(lo < x.start < hi and x.level < a.level for x in items)

    def paragraph_latex(self, p: JsonObject) -> str:
        """A target paragraph's runs, against the style its list level shows in the deck."""
        para = loop_paragraph(p, "target paragraph")
        return runs_latex(para.runs, self.level_style(para), self.ctx)

    def level_style(self, p: LoopParagraph) -> TextStyle:
        """Base style for new text at a paragraph's list level: what that level shows elsewhere
        in the deck (themes shrink nested items), so no size switch is written for it."""
        level = p.level if p.bullet is not None else None
        counts: dict[StyleKey, int] = {}
        for si, s in enumerate(self.cur_slides):
            for e in as_objects(s["elements"], f"slide {si}: elements"):
                if e["kind"] != "text" or e.get("role") != "body":
                    continue
                for q in loop_paragraphs(e, f"slide {si} element {e.get('id')}"):
                    if (q.level if q.bullet is not None else None) != level:
                        continue
                    for run in q.runs:
                        k = (round(_size(run), 1), run.color, run.family)
                        counts[k] = counts.get(k, 0) + len(run.text)
        return common_style(counts, self.base)

    # -- elements and geometry
    def element_missing(self, r: TextMissing | BoxMissing) -> None:
        ci = r.slide
        frame = self.cand.frames[ci]
        if frame is None:
            self.fail(r, "the page comes from no frame")
            return
        te = self.element(self.tgt_slides[r.target_slide], r.target_element)
        text = self.cand.masked(frame.file)
        pos = frame_insert_point(self.cand, frame)
        ind = indent_at(text, frame.start) + "  "
        if te.kind == "text":
            if te.role == "title":
                title = runs_latex(loop_paragraphs(te.json, "title")[0].runs, replace(self.base, size=None, color=None), self.ctx)
                a = frame.body
                self.edit(frame.file, a, a, f"\n{ind}\\frametitle{{{title.strip()}}}", r, "")
                return
            vis = frame_visible(self.cand.source, frame)
            wanted = norm_text(" ".join(para_text(p) for p in element_paragraphs(te)))
            if wanted and locate_words(wanted, vis).score >= 0.8:
                self.fail(r, "the text is in the frame already but doesn't convert as its own text "
                             "(covered by a figure, or merged into another box)")
                return
            self.ctx.packages.add(TEXTPOS)
            self.edit(frame.file, pos, pos, textblock_latex(te.json, self.level_style, self.ctx, ind, aligned_frame(text, frame), "") + "\n", r, "")
        elif te.picture is not None:  # an image
            ref = self.replacing.get((r.slide, te.id))
            if ref is not None:
                self.replace_picture(r, frame, self.find_element(self.cur_slides[r.slide], ref), te.picture)
                return
            pic = self.picture(te.picture)
            if pic is None:
                self.fail(r, "the picture file is not available")
                return
            self.ctx.packages.add(TEXTPOS)
            self.edit(frame.file, pos, pos, picture_block(te.picture, pic, self.ctx, ind), r, "")
        else:
            self.fail(r, f"new {te.kind} elements are not translated")

    # -- pictures
    def pair_replacements(self, res: Sequence[PairedResidual]) -> None:
        """A new deck picture over a converted figure (picture or diagram) the deck no longer has
        replaces that figure's source."""
        extras = [r for r in res if isinstance(r, BoxExtra) and r.el_kind in ("image", "diagram")]
        for r in res:
            if not isinstance(r, BoxMissing) or r.el_kind != "image":
                continue
            te = self.element(self.tgt_slides[r.target_slide], r.target_element)
            best: tuple[float, BoxExtra | None] = max(
                ((overlap_share(te.bbox, x.bbox), x) for x in extras if x.slide == r.slide
                 and (x.slide, x.element) not in self.replaced), key=lambda p: p[0], default=(0, None))
            share, hit = best
            if share >= 0.6 and hit is not None:
                self.replacing[(r.slide, te.id)] = hit.element
                self.replaced.add((r.slide, hit.element))

    def picture(self, te: LoopPicture) -> Picture | None:
        """The file for a deck picture in the source tree: the same picture already there (identical
        bytes, else the best file that looks the same), else the deck's bytes as they are
        (`figures/<alt text slug>-<sha8>.<ext>`). Brightness, contrast and recolour are baked into
        a PNG; GIF, WEBP and other formats become PNG (first frame)."""
        from PIL import Image
        from .compare import adjusted_picture
        from .deck_ir import image_format
        path = Path(te.file or "")
        if not te.file or not path.exists():
            return None
        bake = te.bake
        key = (str(path), json.dumps(bake, sort_keys=True), te.alt)
        if key in self.ctx.pictures:
            return self.ctx.pictures[key]
        index = self.ctx.index
        if index is None:
            index = self.ctx.index = picture_index(self.ws.src)
        data = path.read_bytes()
        fmt = image_format(data)
        look = picture_look(path)
        best = find_picture(index, hashlib.sha1(data).hexdigest(), look)
        if best is None and te.source_url:
            data, fmt, look = self.source_url_bytes(te, data, fmt, look)
        pic = None
        if best is not None and (not bake or best.path.suffix.lower() != ".pdf"):
            if best.sha1 != hashlib.sha1(data).hexdigest():
                self.ctx.notes.append(f"{best.path.relative_to(self.ws.src).as_posix()}: the deck's picture "
                                      f"{path.name} is the same image; the source file is kept (its resolution)")
            if not bake:
                pic = Picture(best.path.relative_to(self.ws.src).as_posix(), best.path, natural_size(best.path))
            else:
                data, fmt = best.path.read_bytes(), image_format(best.path.read_bytes())
        if pic is None:
            ext = {"png": ".png", "jpeg": ".jpg"}.get(fmt)
            note = None
            if bake or ext is None:
                if fmt in ("svg", "emf", "wmf", "unknown"):
                    self.ctx.notes.append(f"{path.name}: {fmt} pictures can't be included by LaTeX; export it as PNG or PDF")
                    return None
                import io
                img = Image.open(io.BytesIO(data))
                frames = getattr(img, "n_frames", 1)
                img.seek(0)
                img = img.convert("RGBA")
                if bake:
                    img = adjusted_picture(img, bake)
                    note = f"{', '.join(bake)} baked into the file (LaTeX has no option for {'them' if len(bake) > 1 else 'it'})"
                if fmt not in ("png", "jpeg"):
                    note = (note + "; " if note else "") + f"{fmt.upper()} converted to PNG" + \
                        (f" (first frame of {frames}: LaTeX shows no animation)" if frames > 1 else "")
                buf = io.BytesIO()
                img.save(buf, "PNG")
                data, ext = buf.getvalue(), ".png"
            sha = hashlib.sha1(data).hexdigest()
            dest = self.ws.src / "figures" / f"{picture_slug(te.alt)}-{sha[:8]}{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                dest.write_bytes(data)
                index.append(PictureFile(dest, sha))
            self.ctx.files[str(dest)] = dest
            if note:
                self.ctx.notes.append(f"{dest.relative_to(self.ws.src).as_posix()}: {note}")
            pic = Picture(dest.relative_to(self.ws.src).as_posix(), dest, natural_size(dest))
        self.ctx.pictures[key] = pic
        return pic

    def source_url_bytes(self, te: LoopPicture, data: bytes, fmt: str, look: Look | None) -> tuple[bytes, str, Look | None]:
        """A picture inserted by URL: the original there when it is the same image with more pixels
        than Google kept (it stores at most about 2046 px on the long side)."""
        import io
        from PIL import Image
        from .deck_ir import fetch_url, image_format
        url = te.source_url
        if url is None:
            return data, fmt, look
        try:
            got = fetch_url(url, None)
            img = Image.open(io.BytesIO(got))
            cur = Image.open(io.BytesIO(data))
            tmp = self.ws.work / "source-url.bin"
            tmp.write_bytes(got)
            if img.size[0] * img.size[1] > cur.size[0] * cur.size[1] and same_look(look, picture_look(tmp)):
                self.ctx.notes.append(f"{url}: the original of an inserted picture, at {img.size[0]}x{img.size[1]}")
                return got, image_format(got), picture_look(tmp)
        except Exception:  # noqa: BLE001 - the deck's bytes are good enough
            pass
        return data, fmt, look

    def current_picture(self, text: str, src: PictureSource) -> Picture | None:
        """The file an \\includegraphics names, as a Picture."""
        args, _ = read_args(text, src.inner[0] + len("\\includegraphics"), "<som")
        if not args[3]:
            return None
        rel = text[args[3][1]:args[3][2]].strip()
        base = (self.ws.src / rel)
        for p in [base] + [base.with_name(base.name + e) for e in LATEX_PICTURES]:
            if p.is_file():
                return Picture(rel, p, natural_size(p))
        return None

    def picture_source(self, ci: int, frame: Frame, el: LoopElement) -> PictureSource | None:
        """The source of a picture (or diagram) of a slide: the textblock at its corner, else the
        source in the same place of the frame's order of pictures."""
        text = self.cand.masked(frame.file)
        srcs = picture_sources(text, frame)
        if not srcs:
            return None
        els = [e for e in loop_elements(self.cur_slides[ci], f"slide {ci}")
               if (e.kind == "image" and e.role not in ("math", "icon", "highlight")) or e.kind == "diagram"]

        def gap(s: PictureSource, e: LoopElement) -> float:
            """How far a textblock's corner is from an element's (none: infinitely)."""
            return math.inf if s.xy is None else abs(s.xy[0] - e.bbox[0]) + abs(s.xy[1] - e.bbox[1])

        def near(s: PictureSource, e: LoopElement) -> bool:
            return gap(s, e) <= 12
        placed = [s for s in srcs if s.xy is not None]
        hit = min(placed, key=lambda s: gap(s, el), default=None)
        if hit is not None and near(hit, el):
            return hit
        free_srcs = [s for s in srcs if not any(near(s, e) for e in els)]
        free_els = [e for e in reading_order(els) if not any(near(s, e) for s in placed)]
        if el not in free_els:
            return None
        k = free_els.index(el)
        return free_srcs[k] if k < len(free_srcs) else None

    def graphics_command(self, ci: int, frame: Frame, el: LoopElement) -> tuple[int, int, tuple[int, int] | None] | None:
        """(start, end, enclosing textblock span or None) of the \\includegraphics showing a picture."""
        src = self.picture_source(ci, frame, el)
        if src is None or src.kind != "graphics":
            return None
        return src.inner[0], src.inner[1], src.block

    def element_extra(self, r: BoxExtra) -> None:
        if (r.slide, r.element) in self.replaced:
            return  # a new picture replaces it (element_missing)
        frame = self.cand.frames[r.slide]
        el = self.find_element(self.cur_slides[r.slide], r.element)
        if frame is None or el is None or el.kind not in ("image", "diagram"):
            self.fail(r, "only pictures are deleted as elements")
            return
        src = self.picture_source(r.slide, frame, el)
        if src is None:
            self.fail(r, "the picture's source was not found in the frame")
            return
        text = self.cand.masked(frame.file)
        if src.kind == "env":  # a figure drawn by the source: kept as a comment
            a, b = line_span(text, src.start, src.end)
            if (a, b) == (src.start, src.end):
                self.fail(r, "the figure shares its lines with other source")
                return
            orig = self.cand.source.text(frame.file)
            self.edit(frame.file, a, b, commented_out(orig, a, b, indent_at(text, src.start), "deleted in the deck"), r, "")
            return
        a, b = line_span(text, *src.block) if src.block else line_span(text, src.start, src.end)
        self.edit(frame.file, a, b, "", r, "")

    def replace_picture(self, r: BoxMissing | TextMissing | ImageResidual, frame: Frame, el: LoopElement | None,
                        te: LoopPicture) -> None:
        """The deck shows `te` where the source draws `el`: a replaced picture gets the new file (and
        the deck's edits as options); a figure the source draws (tikzpicture, pgfplots) is commented
        out under a `% b2s pull: replaced by <file>` note, the picture in its place. When the deck's
        picture is the one the source makes, only its edits change: options for an
        \\includegraphics, \\rotatebox or a transparency group for a tikzpicture."""
        ci = r.slide
        src = self.picture_source(ci, frame, el) if el is not None else None
        if src is None or el is None:
            self.fail(r, "the replaced picture's source was not found in the frame")
            return
        text = self.cand.masked(frame.file)
        orig = self.cand.source.text(frame.file)
        from .compare import hash_distance, picture_differs, picture_hash
        raw = picture_hash(te.file) if el.kind == "image" and te.file else None
        # the hashes are keyed by the current deck's element objects (`picture_hashes`)
        same = raw is not None and hash_distance(self.hashes.get(id(el.json)), raw) is not None \
            and not picture_differs(self.hashes.get(id(el.json)), raw, TOL["phash"])
        if same and not picture_edits(te):
            self.fail(r, "the picture looks the same as the source's; nothing to write")
            return
        if src.kind == "graphics":
            pic = self.current_picture(text, src) if same else self.picture(te)
            if pic is None:
                self.fail(r, "the picture file is not available")
                return
            size = None
            m = INCLUDEGRAPHICS_RE.match(text, src.inner[0], src.inner[1])
            old = [o.strip() for o in ((m.group(3) if m else None) or "").split(",")
                   if re.match(r"\s*(width|height|scale|keepaspectratio)\b", o)]
            box = te.frame
            if old and not te.rotation and te.crop is None and \
                    abs((box[2] - box[0]) - (el.bbox[2] - el.bbox[0])) <= TOL["size"] and \
                    abs((box[3] - box[1]) - (el.bbox[3] - el.bbox[1])) <= TOL["size"]:
                size = old  # the source's own size (\textwidth fractions) still fits
            self.edit(frame.file, src.start, src.end, picture_latex(te, pic, self.ctx, size), r, "")
            return
        if same and te.crop is None and te.outline is None and not te.bake \
                and text[src.inner[0]:src.inner[1]].lstrip().startswith("\\begin{tikzpicture}"):
            self.edit(frame.file, src.start, src.end, self.tikz_edits(orig, text, src, te), r, "")
            return
        pic = self.picture(te)
        if pic is None:
            self.fail(r, "the picture file is not available")
            return
        ind = indent_at(text, src.inner[0])
        a, b = line_span(text, src.start, src.end)
        lead = "" if (a, b) != (src.start, src.end) else "\n"
        note = f"replaced by {pic.rel}"
        if src.overlay or src.block:
            self.ctx.packages.add(TEXTPOS)
            new = lead + commented_out(orig, a, b, ind, note)
            if src.block:
                self.edit(frame.file, a, b, new, r, "")
                self.edits.append(Edit(frame.file, src.block[1], src.block[1], "\n" + picture_block(te, pic, self.ctx, ind),
                                       r.kind, signature(r) + ("block",), ""))
            else:
                pos = frame_insert_point(self.cand, frame)
                self.edit(frame.file, a, b, new, r, "")
                self.edits.append(Edit(frame.file, pos, pos, picture_block(te, pic, self.ctx, ind), r.kind,
                                       signature(r) + ("block",), ""))
        else:
            self.edit(frame.file, a, b, lead + commented_out(orig, a, b, ind, note) + f"{ind}{picture_latex(te, pic, self.ctx, None)}\n",
                      r, "")
        self.ctx.notes.append(f"{(self.ws.root / frame.file.relative_to(self.ws.src)).as_posix()}:{line_of(text, src.inner[0])}: "
                              f"the {'figure' if el.kind == 'image' else 'diagram'} was replaced in the deck by "
                              f"{pic.rel}; the original is kept as a comment")

    def tikz_edits(self, orig: str, text: str, src: PictureSource, te: LoopPicture) -> str:
        """A tikzpicture turned (\\rotatebox, \\reflectbox) and made translucent (a transparency group
        inside it) like its picture in the deck; earlier such edits are replaced."""
        a, b = src.inner
        env = orig[a:b]
        _, head = read_args(text, a + text[a:b].index("}") + 1, "o")
        head -= a
        tail = env.rindex("\\end")
        body = env[head:tail]
        m = re.match(r"\s*\\begin\{scope\}\[transparency group,opacity=[\d.]+\]\n?(?P<inner>.*?)\s*\\end\{scope\}\s*$", body, re.S)
        if m:
            body = "\n" + m.group("inner") + "\n"
        ind = indent_at(text, a)
        if te.opacity is not None and te.opacity < 0.995:
            body = f"\n{ind}  \\begin{{scope}}[transparency group,opacity={te.opacity:.2f}]" + body.rstrip() + \
                f"\n{ind}  \\end{{scope}}\n{ind}"
        out = env[:head] + body + env[tail:]
        # wrappers written by an earlier round go; others (\resizebox) stay around
        prefix, suffix, ours = orig[src.start:a], orig[b:src.end], 0
        while (w := re.search(r"\\(rotatebox\s*\{[^{}]*\}|reflectbox)\s*\{\s*$", prefix)):
            prefix, ours = prefix[:w.start()], ours + 1
        for _ in range(ours):
            suffix = suffix.lstrip()[1:]
        if te.flip:
            out = f"\\reflectbox{{{out}}}"
        angle = round(-(te.rotation or 0.0), 2)
        if angle:
            out = f"\\rotatebox{{{angle:g}}}{{{out}}}"
        return prefix + out + suffix

    def geometry(self, r: TextGeometry | BoxGeometry) -> None:
        ci = r.slide
        frame = self.cand.frames[ci]
        if frame is None:
            self.fail(r, "the page comes from no frame")
            return
        cs = self.cur_slides[ci]
        el = self.element(cs, r.element)
        te = self.element(self.tgt_slides[r.target_slide], r.target_element)
        text = self.cand.masked(frame.file)
        if isinstance(r, BoxGeometry):
            if el.kind == "image" and te.picture is not None:  # compare pairs boxes of one kind
                self.picture_geometry(r, frame, el, te.picture)
            else:
                self.fail(r, f"{el.kind} elements are not moved")
            return
        if te.role == "title" or el.role == "title":
            self.fail(r, "frame titles are placed by the theme")
            return
        locs = self.cand.paragraph_locations(ci)
        mine = [(k, loc) for k, loc in locs.items() if k[0] == el.id and k[1] >= r.para]
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
            x, y = float(m.group("x")) + r.dx, float(m.group("y")) + r.dy
            w = float(m.group("w"))
            if te.wrap_width and abs(te.wrap_width - w) > 6 and len(element_paragraphs(te)) >= 1:
                w = te.wrap_width
            s = bs
            self.edit(frame.file, s, s + len(m.group(0)), f"\\begin{{textblock*}}{{{w:.1f}pt}}({x:.1f}pt,{y:.1f}pt)", r, "")
            return
        # flow content -> a textblock at the target position; lists move whole
        for _, loc in mine:
            lst = top_list(loc)
            if lst is not None:
                a, b = min(a, lst.start), max(b, lst.end)
        a, b = balance_span(text, a, b, frame.body, frame.body_end)
        if abs(r.dx) <= TOL["pos"] and self.flow_shift(r, frame, text, a):
            return
        others = [loc for k, loc in locs.items() if k[0] != el.id and a <= loc.src()[0] < b]
        if others:
            self.fail(r, "the element shares its source (a list or paragraph) with other boxes")
            return
        a, b = line_span(text, a, b)
        chunk = text[a:b]
        if "\\begin{frame}" in chunk or "\\end{frame}" in chunk:
            self.fail(r, "element source not isolated")
            return
        paragraphs = element_paragraphs(el)
        size = max((run_size(run) or 10) for p in paragraphs for run in as_objects(p["runs"], "runs"))
        x, y = text_anchor(te.json)
        align = r.align
        left = text_anchor({**el.json, "anchor": None})[0]
        right = max(_number(ln["x1"], "line x1") for p in paragraphs for ln in as_objects(p["lines"], "lines"))
        width = te.wrap_width or (1.1 * (right - left) + 12)
        bx = x - (width / 2 if align == "center" else width if align == "right" else 0)
        by = y - 0.75 * size
        ind = indent_at(text, frame.start) + "  "
        align_cmd = {"center": "\\centering ", "right": "\\raggedleft "}.get(align, "\\raggedright " if aligned_frame(text, frame) else "")
        # keep the room it took in the flow when other flow text follows
        below = [e for e in loop_elements(cs, f"slide {ci}") if e.kind == "text" and e.json is not el.json
                 and e.role not in ("title", "footer")
                 and e.bbox[1] >= el.bbox[3] - 1 and not in_textblock(self.cand, ci, e, frame)]
        room = min((e.bbox[1] for e in below), default=None)
        spacer = f"{indent_at(text, a)}\\vspace{{{room - el.bbox[1]:.1f}pt}}\n" if room is not None else ""
        self.ctx.packages.add(TEXTPOS)
        pos = frame_insert_point(self.cand, frame)
        self.edit(frame.file, a, b, spacer, r, "")
        block_text = (f"{ind}\\begin{{textblock*}}{{{width:.1f}pt}}({bx:.1f}pt,{by:.1f}pt)\n{ind}  {align_cmd}"
                      + dedent_block(chunk, ind + "  ").lstrip() + f"{ind}\\end{{textblock*}}\n")
        self.edits.append(Edit(frame.file, pos, pos, block_text, "geometry", signature(r) + ("block",), ""))

    def flow_shift(self, r: TextGeometry, frame: Frame, text: str, a: int) -> bool:
        """A vertical move of flow text: \\vspace before it (adjusted on later rounds). Beamer centres
        a frame's content vertically, so the text moves by about half the space (secant updates
        learn the real gain). False when this isn't the way (too many tries, not converging)."""
        key = signature(r) + ("vspace",)
        found = self.last.get(key)
        state = found if isinstance(found, FlowShift) else FlowShift()
        self.last[key] = state
        if state.tries >= 5 or r.slide in self.shifted:
            return state.tries < 5
        err = r.dy
        gain = state.gain or (1.0 if re.search(r"(^|,)\s*t\s*(,|$)", frame.options) else 0.5)
        if state.err is not None and abs(state.dv) > 0.1:
            g = (err - state.err) / -state.dv if state.dv else None
            if g is not None and 0.2 <= g <= 1.5:
                gain = g
        if state.err is not None and abs(err) > 0.8 * abs(state.err) and state.tries >= 2:
            state.tries = 5
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
                self.edit(frame.file, line_start, ls, "", r, "")
            else:
                self.edit(frame.file, start, start + len(m.group(1)), f"{value:.1f}", r, "")
        else:
            # \par: a column or item starts in horizontal mode, where \vspace would land after the first line
            self.edit(frame.file, ls, ls, f"{indent_at(text, a)}\\par\\vspace{{{dv:.1f}pt}}\n", r, "")
        state.tries, state.err, state.dv, state.gain = state.tries + 1, err, dv, gain
        self.shifted.add(r.slide)
        return True

    def picture_geometry(self, r: BoxGeometry, frame: Frame, el: LoopElement, te: LoopPicture) -> None:
        src = self.picture_source(r.slide, frame, el)
        if src is None or src.kind != "graphics":
            self.fail(r, "\\includegraphics not found" if src is None else "figures drawn by the source are not moved")
            return
        text = self.cand.masked(frame.file)
        if picture_edits(te) and src.block is not None:
            # a turned, cropped or translucent picture: rewritten whole at its place
            pic = self.current_picture(text, src)
            if pic is None:
                self.fail(r, "the picture file is not available")
                return
            m = TEXTBLOCK_RE.match(text, src.block[0])
            if m is None:  # never: picture_sources found the textblock by this pattern
                self.fail(r, "the picture's textblock was not understood")
                return
            pad = te.outline.weight / 2 if te.outline is not None and not te.rotation else 0.0
            x, y = float(m.group("x")) + r.dx, float(m.group("y")) + r.dy
            self.edit(frame.file, src.block[0], src.block[0] + len(m.group(0)),
                      f"\\begin{{textblock*}}{{{te.bbox[2] - te.bbox[0] + 2 * pad:.1f}pt}}({x:.1f}pt,{y:.1f}pt)", r, "")
            self.edits.append(Edit(frame.file, src.start, src.end, picture_latex(te, pic, self.ctx, None), "geometry",
                                   signature(r) + ("size",), ""))
            return
        a, b, block = src.inner[0], src.inner[1], src.block
        box = te.frame
        tw, th = box[2] - box[0], box[3] - box[1]
        cw, ch = el.bbox[2] - el.bbox[0], el.bbox[3] - el.bbox[1]
        opts_m = GRAPHICS_OPTS_RE.match(text, a, b)
        if opts_m is None:  # never: the pattern matches any \includegraphics
            self.fail(r, "\\includegraphics not understood")
            return
        opts = opts_m.group(1) or ""
        keep = [o.strip() for o in opts.strip("[]").split(",") if o.strip() and not re.match(r"(width|height|scale|keepaspectratio)\b", o.strip())]
        sized = abs(r.dw) > TOL["size"] or abs(r.dh) > TOL["size"]
        size_opts = [f"width={tw:.1f}pt", f"height={th:.1f}pt"]
        rel = re.search(r"width\s*=\s*([\d.]*)\s*\\(textwidth|linewidth|columnwidth)", opts)
        if rel and abs(tw / max(cw, 1) - th / max(ch, 1)) < 0.03:  # same aspect: keep the relative width
            factor = float(rel.group(1) or 1) * tw / max(cw, 1)
            size_opts = [f"width={factor:.3g}\\{rel.group(2)}"]
        new_opts = ",".join(keep + size_opts) if sized else opts.strip("[]")
        cmd = f"\\includegraphics[{new_opts}]" + text[a + opts_m.end():b] if new_opts else text[a:b]
        if block is not None:
            m = TEXTBLOCK_RE.match(text, block[0])
            if m is None:  # never: picture_sources found the textblock by this pattern
                self.fail(r, "the picture's textblock was not understood")
                return
            x = float(m.group("x")) + r.dx
            y = float(m.group("y")) + r.dy
            self.edit(frame.file, block[0], block[0] + len(m.group(0)), f"\\begin{{textblock*}}{{{tw:.1f}pt}}({x:.1f}pt,{y:.1f}pt)", r, "")
            if sized:
                self.edits.append(Edit(frame.file, a, b, cmd, "geometry", signature(r) + ("size",), ""))
            return
        moved = abs(r.dx) > TOL["pos"] or abs(r.dy) > TOL["pos"]
        found = self.last.get(signature(r) + ("flow",), 0)
        tries = found if isinstance(found, int) else 0
        if sized and tries == 0:
            self.last[signature(r) + ("flow",)] = 1
            self.edit(frame.file, a, b, cmd, r, "")
            return
        if not moved:
            self.edit(frame.file, a, b, cmd, r, "")
            return
        # out of the flow, its room kept by a phantom of the same size
        self.ctx.packages.add(TEXTPOS)
        ind = indent_at(text, frame.start) + "  "
        pos = frame_insert_point(self.cand, frame)
        cur_cmd = text[a:b]
        self.edit(frame.file, a, b, f"\\phantom{{{cur_cmd}}}", r, "")
        pic = f"\\includegraphics[{','.join(keep + [f'width={tw:.1f}pt', f'height={th:.1f}pt'])}]" + text[a + opts_m.end():b]
        self.edits.append(Edit(frame.file, pos, pos,
                               f"{ind}\\begin{{textblock*}}{{{tw:.1f}pt}}({te.bbox[0]:.1f}pt,{te.bbox[1]:.1f}pt)\n"
                               f"{ind}  {pic}\n{ind}\\end{{textblock*}}\n", "geometry", signature(r) + ("block",), ""))

    def image(self, r: ImageResidual) -> None:
        frame = self.cand.frames[r.slide]
        el = self.find_element(self.cur_slides[r.slide], r.element)
        te = self.element(self.tgt_slides[r.target_slide], r.target_element).picture
        if frame is None or te is None or not te.file or not Path(te.file).exists():
            self.fail(r, "replacement picture not available")
            return
        self.replace_picture(r, frame, el, te)


def aligned_frame(text: str, frame: Frame) -> bool:
    """The frame switches paragraph alignment somewhere: textblocks then state theirs."""
    return bool(re.search(r"\\(centering|raggedleft|flushright|center)\b", text[frame.body:frame.body_end]))


def top_list(loc: ParaLoc) -> ListEnv | None:
    if loc.item is None:
        return None
    src = loc.item.start
    outer = [l for l in loc.visible.lists if l.start <= src < l.end and l.level == 0]
    return outer[0] if outer else None


def enclosing_textblock(text: str, pos: int, frame: Frame) -> "tuple[re.Match[str], int, int] | None":
    for m in TEXTBLOCK_RE.finditer(text, frame.body, frame.body_end):
        end = text.find("\\end{textblock*}", m.end())
        if m.start() < pos < end:
            return m, m.start(), end
    return None


def in_textblock(cand: Candidate, si: int, el: LoopElement, frame: Frame) -> bool:
    locs = cand.paragraph_locations(si)
    text = cand.masked(frame.file)
    for (eid, _), loc in locs.items():
        if eid == el.id:
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


def char_span(ptext: str, c0: int, c1: int, loc: ParaLoc, whole_words: bool) -> tuple[int, int] | None:
    """Source span of characters c0..c1 of a paragraph's normalised text: whole words when the
    range covers them, characters inside a word when its source is plain (`whole_words=True`
    still lets a trailing/leading run of punctuation - "box by box" out of "box by box." - be left
    out, but never narrows into a letter or digit: a style command wrapped around less than that is
    never something a person wrote, only a coincidence of a character-level diff, `style`)."""
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
        va, vb = span  # the first word's start and the last one's end
        if vb - va == e1 - s0 and all(loc.visible.ends[v] - loc.visible.starts[v] == 1 for v in range(va, vb)):
            if c0 > s0 and (not whole_words or not re.search(r"\w", ptext[s0:c0])):
                a = loc.visible.starts[va + (c0 - s0)]
            if c1 < e1 and (not whole_words or not re.search(r"\w", ptext[c1:e1])):
                b = loc.visible.ends[vb - 1 - (e1 - c1)]
    return a, b


K = TypeVar("K")


def dominant(para: JsonObject, read: Callable[[JsonObject], K]) -> K | None:
    """The value `read` gives most of a paragraph's letters (the first so read among equals)."""
    weights: dict[K, int] = {}
    for r in as_objects(para["runs"], "paragraph runs"):
        k = read(r)
        weights[k] = weights.get(k, 0) + len(as_str(r["text"], "run text"))
    return max(weights.items(), key=lambda kw: kw[1])[0] if weights else None


def run_colour(r: JsonObject) -> str | None:
    return _loop_str(r, "color", "run")


def run_size(r: JsonObject) -> float | None:
    return _loop_number(r, "size", "run")


StyleKey = tuple[float, str | None, str | None]


def common_style(counts: Mapping[StyleKey, int], base: TextStyle) -> TextStyle:
    """`base` in the most common (size, colour, family) of `counts`, the letters each has; `base`
    itself when there are none."""
    if not counts:
        return base
    size, colour, family = max(counts, key=counts.__getitem__)
    return replace(base, size=size, color=colour, family=family)


def body_style(deck: JsonObject) -> TextStyle:
    counts: dict[StyleKey, int] = {}
    for si, s in enumerate(as_objects(deck["slides"], "slides")):
        for e in as_objects(s["elements"], f"slide {si}: elements"):
            if e["kind"] != "text" or e.get("role") != "body":
                continue
            for p in loop_paragraphs(e, f"slide {si} element {e.get('id')}"):
                for r in p.runs:
                    k = (round(_size(r), 1), r.color, r.family)
                    counts[k] = counts.get(k, 0) + len(r.text)
    return common_style(counts, TextStyle(size=10.91, color="#000000", family="sans", bold=False, italic=False,
                                          font=None, weight=None))


def _size(r: LoopRun) -> float:
    """A run's size where the count needs one, as `r["size"]` read it: a TypeError for none."""
    if r.size is None:
        raise TypeError("a run with no size")
    return r.size


SIG_LEN = 9  # an Edit's signature is its residual's signature plus translator details


def signature(r: Residual) -> Signature:
    """What a residual is about, the same round after round: kind, target slide, element and
    paragraph, a style's field and first character; the current slide, element and paragraph only
    for what the target does not have."""
    target_element = None if isinstance(r, (SlideOrder, SlideMissing, SlideExtra, NotesResidual, BackgroundResidual,
                                            ParagraphExtra, TextExtra, BoxExtra)) else r.target_element
    target_para = r.target_para if isinstance(r, (ParagraphMissing, ParagraphOrder, TextResidual, StyleResidual,
                                                  BulletResidual, AlignResidual)) else None
    return (r.kind, target_slide_of(r), target_element, target_para,
            r.change.field if isinstance(r, StyleResidual) else None,
            r.t0 if isinstance(r, StyleResidual) else None,
            r.slide if isinstance(r, SlideExtra) else None,
            r.element if isinstance(r, (ParagraphExtra, TextExtra, BoxExtra)) else None,
            r.para if isinstance(r, ParagraphExtra) else None)


def edit_group(sig: Signature) -> Signature:
    """A residual signature without its character offset (index 5, `t0`): several sub-ranges of the
    same field of the same paragraph (a colour narrowed to one word at a time, say) are one rewrite
    in progress, not independent edits, so converge()'s revert undoes a blocked one's whole group."""
    return sig[:5] + sig[6:]


# ---------------------------------------------------------------- generated LaTeX

def script_paragraph(p: ParagraphIn) -> JsonObject:
    """What `scripts`' direction helpers read of a paragraph, in the dict they read."""
    return {"align": p.align, "direction": p.direction, "runs": [{"text": r.text} for r in p.runs]}


def paragraphs_latex(paragraphs: Sequence[P], style_for: Callable[[P], TextStyle], ctx: Context, ind: str) -> str:
    from .scripts import align_switch, block_direction, block_rtl, paragraph_direction
    lines: list[str] = []
    stack: list[str] = []
    items: list[bool] = []    # whether each open list has had an \item yet
    scripted = [script_paragraph(p) for p in paragraphs]
    rtl_block = block_rtl(scripted)
    for p, sp in zip(paragraphs, scripted):
        blank = bool(p.runs) and not any(r.text.strip() for r in p.runs)
        runs = runs_latex(p.runs, style_for(p), ctx).strip()
        if blank:
            # A paragraph of nothing but spaces is a blank line someone left in a text box, and it
            # takes a line of its own. Only a foreign deck has them (`deck_ir.text_paragraphs`
            # keeps them for `adopt`; classify cannot see one, a PDF having only the gap it
            # leaves), and TeX would drop a paragraph whose entire content is a space - so the
            # line is a `\strut` at the size the person's Return left room for. The size stays
            # inside a group ended by the line's own `\par` (so the line keeps its pitch): what
            # follows is written against the base style and must find it still in effect.
            size = p.runs[0].size
            switch = size_switch(size, ctx.pt_option) if size else ""
            runs = "{" + switch + "\\strut\\par}" if switch else "\\strut"
        elif not runs:
            continue
        if "".join(r.text for r in p.runs).startswith("\x0b"):
            # a soft break (Shift+Enter) opening a paragraph: `\\` there has no line to end yet
            runs = "\\leavevmode" + runs
        if p.bullet is not None:
            # beamer nests itemize/enumerate three deep; Slides allows nine
            level = min(p.level, 2)
            env = "enumerate" if p.bullet.kind == "number" else "itemize"
            while len(stack) > level + 1:
                lines.append(ind + "  " * (len(stack) - 1) + f"\\end{{{stack.pop()}}}")
            del items[len(stack):]
            while len(stack) < level + 1:
                if stack and not items[-1]:
                    # a list opening deeper than its first item ("missing \item"): an empty item
                    # holds it, and LaTeX sets a list that opens an item on that item's line
                    lines.append(ind + "  " * len(stack) + "\\item[]")
                    items[-1] = True
                lines.append(ind + "  " * len(stack) + f"\\begin{{{env}}}")
                stack.append(env)
                items.append(False)
            lines.append(ind + "  " * len(stack) + f"\\item {runs}")
            items[-1] = True
        else:
            while stack:
                lines.append(ind + "  " * (len(stack) - 1) + f"\\end{{{stack.pop()}}}")
            if lines:
                lines.append("")
            # the switch and the language group follow the paragraph's direction (scripts.py)
            line = align_switch(sp) + runs.replace("\t", " ")
            lines.append(ind + paragraph_direction(sp, line, rtl_block))
    while stack:
        lines.append(ind + "  " * (len(stack) - 1) + f"\\end{{{stack.pop()}}}")
    return block_direction(scripted, "\n".join(lines), ind)


def textblock_latex(te: JsonObject, style_for: Callable[[LoopParagraph], TextStyle], ctx: Context, ind: str,
                    reset: bool, lead: str) -> str:
    """`lead` is put at the top of the block, before the text: the switches that make the base style
    `runs_latex` writes against actually true here (`adopt.base_lead`). The loop leaves it empty,
    because in a source it is refining the surrounding document already sets that base."""
    x, y = text_anchor(te)
    paragraphs = loop_paragraphs(te, f"text element {te.get('id')}")
    size = max((r.size or 10.0) for p in paragraphs for r in p.runs)
    xs = [x1 for p in paragraphs for x1 in p.line_ends]
    wrap = te.get("wrap_width")
    width = (_number(wrap, "wrap_width") if wrap else 0.0) or ((max(xs) - x + 8) if xs else 200.0)
    align = {p.align for p in paragraphs}
    bx = x - width / 2 if align == {"center"} else x - width if align == {"right"} else x
    body = paragraphs_latex(paragraphs, style_for, ctx, ind + "  ")
    if reset and align not in ({"center"}, {"right"}):
        body = f"{ind}  \\raggedright\n" + body
    if lead:
        body = f"{ind}  {lead}\n" + body
    return (f"{ind}\\begin{{textblock*}}{{{width:.1f}pt}}({bx:.1f}pt,{y - 0.75 * size:.1f}pt)\n"
            + body + f"\n{ind}\\end{{textblock*}}")


def is_frame_label(key: str | None) -> bool:
    """Whether `key` is a slide's own LaTeX label, not one of sync's synthetic keys for an
    unlabelled frame (title:..., page:N) - those are never written as `[label=...]`."""
    return bool(key) and bool(re.fullmatch(r"[A-Za-z][\w:.-]*", key)) and not re.match(r"(title|page):", key)


# A frame title's base: the theme sets its size and colour, and it is bold sans.
TITLE_STYLE = TextStyle(size=None, color=None, family="sans", bold=True, italic=False, font=None, weight=None)


def frame_latex(texts: Sequence[tuple[str | None, Sequence[P]]], key: str | None, notes: str | None,
                style_for: Callable[[P], TextStyle], ctx: Context, label: str | None) -> str:
    """A slide as a readable frame: `texts` are its text elements' roles and paragraphs, in order;
    `label` the frame's label, else the slide's `key`."""
    key = label or key
    label = f"[label={key}]" if is_frame_label(key) else ""
    title = ""
    body = []
    for role, paragraphs in texts:
        if role == "title" and not title:
            title = runs_latex(paragraphs[0].runs, TITLE_STYLE, ctx).strip()
        elif role not in ("footer", "math", "icon"):
            body.append(paragraphs_latex(paragraphs, style_for, ctx, "  "))
    out = f"\\begin{{frame}}{label}{{{title}}}\n" + "\n\n".join(body) + "\n"
    if notes:
        out += note_latex(notes) + "\n"
    return out + "\\end{frame}\n"


def note_latex(notes: str) -> str:
    """A slide's speaker notes as `\\note{...}`, a paragraph per line. A paragraph opening on a
    soft break starts with `\\leavevmode`, as paragraphs_latex's do: `\\\\` there has no line to
    end, and the notes=show compile the pull loop runs stopped on it."""
    paragraphs = [latex_escape(p) for p in notes.split("\n") if p.strip()]
    return "  \\note{" + "\n\n".join(("\\leavevmode" if p.startswith("\\\\") else "") + p
                                     for p in paragraphs) + "}"


def ensure_preamble(ws: Workspace, ctx: Context) -> list[Edit]:
    text = ws.source.text(ws.main)
    masked = mask_comments(text)
    pos = ws.source.preamble_end()
    lines: list[str] = []
    for pkg in sorted(ctx.packages):
        braced = re.search(r"\{([^}]+)\}$", pkg)
        if braced is None:  # every package line pull writes ends on its name in braces
            raise ValueError(f"not a package line: {pkg}")
        name = braced.group(1)
        if not re.search(r"\\usepackage\s*(\[[^\]]*\])?\s*\{[^}]*\b" + re.escape(name) + r"\b", masked[:pos]):
            lines.append(pkg)
    for name, hexv in sorted(ctx.colours.items()):
        if not re.search(r"\\definecolor\s*\{" + re.escape(name) + r"\}", masked[:pos]):
            lines.append(f"\\definecolor{{{name}}}{{HTML}}{{{hexv}}}")
    if not lines:
        return []
    return [Edit(ws.main, pos, pos, "\n".join(lines) + "\n", "preamble", ("preamble",), "")]


# ---------------------------------------------------------------- the loop

@dataclass(frozen=True, kw_only=True)
class Result:
    """What the loop leaves, in the JSON the reports, edits.json and the agents read."""
    converged: bool
    iterations: list[JsonObject]
    unresolved: list[JsonObject]     # open residuals with why and where (`unresolved_json`)
    residuals: list[JsonObject]      # open residuals (`compare.residual_json`)
    files: dict[str, str | Path]     # original path -> edited text, or a new picture to copy
    patch: str
    work: Path
    theme: list[JsonObject]          # differences the theme owns (titles), not written back
    notes: list[str]                 # pictures: reused files, baked edits, converted formats, replaced figures
    originals: dict[str, str]        # source path -> sha1 when the loop copied it (apply checks it)
    labels: list[str]                # slide labels renamed to dodge a collision
    restored: list[JsonObject]       # frames the guard put back to a better round (`put_back`)


@dataclass(frozen=True, kw_only=True)
class PutBack:
    """A frame the guard put back to its best round: `file` is the work tree's, `final` the guard's
    record of the frame as the loop left it."""
    target_slides: tuple[int, ...]
    frame_label: str | None
    iteration: int
    mode: str
    score_left: float
    score_best: float
    penalty_left: float
    penalty_best: float
    why: str
    file: Path
    final: "Seen"


def put_back_json(p: PutBack, score_now: float | None, where: str) -> JsonObject:
    """A frame put back as the report says it (`score_now`: the frame's score once they all are)."""
    out: JsonObject = {"target_slides": [j for j in p.target_slides], "frame_label": p.frame_label,
                       "iteration": p.iteration, "mode": p.mode, "score_left": p.score_left,
                       "score_best": p.score_best, "penalty_left": p.penalty_left, "penalty_best": p.penalty_best,
                       "why": p.why}
    if score_now is not None:
        out["score_now"] = round(score_now, 3)
    out["where"] = where
    return out


def typed_target(deck: JsonObject) -> TargetDeck | JsonObject:
    """What compare and `picture_hashes` read of a pull's target: `deck_ir`'s read parsed
    (`deck_ir_types`, an `IRError` for one that is not what deck_ir writes), and a deck.json given
    as the target (`tex_converge`'s classify output, a test's stand-in) as it is."""
    return parse_target(deck) if is_target(deck) else deck


def target_pictures(target: TargetDeck | JsonObject) -> list[tuple[int, JsonObject]]:
    """The target's pictures as their JSON, each with the key compare names it by in `hashes`: the
    typed element's identity, or the dict's (`compare.deck_views`)."""
    if isinstance(target, TargetDeck):
        return [(id(e), element_json(e)) for s in target.slides for e in s.elements if isinstance(e, TargetImage)]
    return [(id(e), e) for s in as_objects(target["slides"], "target slides")
            for e in as_objects(s["elements"], "target elements") if e["kind"] == "image"]


def picture_hashes(cand: Candidate, target: TargetDeck | JsonObject, comp_out: Path) -> dict[int, PictureHash]:
    """Hashes of the pictures on both sides (current ones cropped from the candidate PDF)."""
    from .compare import displayed_picture
    hashes: dict[int, PictureHash] = {}
    tgt_images = target_pictures(target)
    if not tgt_images:
        return hashes
    for ref, e in tgt_images:
        file = e.get("file")
        if isinstance(file, str) and file and Path(file).exists():
            plain = picture_hash(file)
            edited = picture_edits(loop_picture(e, box(e["bbox"], At(where="target picture", path="bbox")), "target picture"))
            shown = displayed_picture(e) if edited else None  # as Slides shows it: crop, turn, opacity
            hashes[ref] = PicHash(grey16(shown), plain.coverage if plain else 1.0) if shown is not None else plain
    from .pdf import Document
    from PIL import Image
    doc = Document(cand.pdf)
    try:
        for si, s in enumerate(cand.slides()):
            for e in as_objects(s["elements"], f"deck.json slide {si}: elements"):
                if e["kind"] != "image":
                    continue
                page = doc[cand.page(si)]
                x0, y0, x1, y1 = box(e["bbox"], At(where=f"deck.json slide {si}", path="bbox"))
                if x1 - x0 < 1 or y1 - y0 < 1:
                    continue
                mark = e.get("mark_n")
                off = others_than(page, mark) if isinstance(mark, (int, str)) else []
                page.set_active(off, False)
                try:
                    img = page.render(4.0, (x0, y0, x1, y1), transparent=False)
                finally:
                    page.set_active(off, True)
                hashes[id(e)] = grey16(Image.fromarray(img))
    finally:
        doc.close()
    return hashes


def others_than(page: "Page", n: int | str) -> list[int]:
    """The page's objects that are not marked element `n`'s (`marked.py`), and hold none of its:
    a picture adopt wrote is judged by what it drew alone - a layout's picture over the slide's own
    full-page one showed both, and neither matched its file.

    `n` is the element's `mark_n`, which `marked.split` groups by `marked.group_id` (the mark's `/n`,
    else its `/k`), so an object is its element's by that same reading: by `/n` alone, a mark with no
    `/n` matched no object and every object went off - the picture judged blank. When no object is
    the element's, nothing goes off (the crop as the page draws it, as before marks)."""
    from .extract import page_marks
    from .marked import ELEMENT, group_id
    marks = page_marks(page)
    objects = page.objects()

    def top(oid: int) -> int | str | None:  # (the outermost element mark says whose it is, as in marked.split)
        return next((group_id(p) for t, p in marks.get(oid, ()) if t == ELEMENT), None)
    keep = {po.id for po in objects if top(po.id) == n}
    if not keep:
        return []
    parent = {po.id: po.parent for po in objects}
    for k in list(keep):
        up = parent.get(k)
        while up is not None:
            keep.add(up)
            up = parent.get(up)
    return [po.id for po in objects if po.id not in keep]


class Later:
    """A target fetched on demand, once, and remembered: `converge` calls it on a thread of its own
    while the source compiles for the first time, and the caller reads `value` afterwards."""

    def __init__(self, fn: Callable[[], JsonObject]) -> None:
        self.fn = fn
        self.value: JsonObject | None = None

    def __call__(self) -> JsonObject:
        self.value = self.fn()
        return self.value


def converge(tex: Path, target: "JsonObject | Later", work: Path, max_iter: int, handout: bool, engine: str | None,
             tol: Mapping[str, float], log: Callable[[str], object], guard: bool,
             thumbnails: "Callable[[int], object] | None | Literal[False]") -> Result:
    """`target`: the deck to converge to, or a `Later` fetching it - which is read on a thread of
    its own while the first compile runs, the two needing nothing of each other. `tol`: compare's
    limits (`compare.TOL`).

    `guard`: put back every frame the rounds left worse than its best round (`frame_guard`), scored
    against `thumbnails(j)` (Google's picture of target slide j: a path, image or array) - None for
    the ones the target carries, False for none (then by weighted residuals and words)."""
    ws = Workspace(tex, work, handout=handout, engine=engine, fresh=True)
    ready: tuple[Path | None, str] | None = None
    if isinstance(target, Later):
        ws.notes = uses_notes(ws.source)   # what the first compile can know without the deck
        with ThreadPoolExecutor(1, thread_name_prefix="b2s-pull") as pool:
            reading = pool.submit(target)
            try:
                ready = ws.compile()
            finally:
                deck = reading.result()
    else:
        deck = target
    ctx = fresh_context(class_pt_option(ws.source))
    typed = typed_target(deck)
    has_notes = any(s.get("notes") for s in as_objects(deck["slides"], "target: slides")) or uses_notes(ws.source)
    iterations: list[JsonObject] = []
    blocked: set[Signature] = set()
    attempts: dict[Signature, list[float]] = {}
    tried: dict[Signature, int] = {}   # residual signature -> rounds with an edit written for it
    attempted: dict[Signature, list[tuple[Signature, Path, str, str]]] = {}   # a REVERTIBLE edit's group (its
        # signature without the character offset: sub-ranges of the same field/paragraph are one
        # rewrite in progress, not independent edits) -> [(signature, file, text before, text
        # after), ...] in the order they were applied
    last_values: dict[Signature, FlowShift | int] = {}
    seen: dict[tuple[str, ...], int] = {}
    unresolved: list[GivenUp] = []
    comp: Comparison | None = None
    cand: Candidate | None = None
    fg: "FrameGuard | None" = None
    if guard:
        from .frame_guard import FrameGuard, target_thumbnails
        fg = FrameGuard(typed, target_thumbnails(typed) if thumbnails is None else thumbnails or None, log)
    for it in range(max_iter + 1):
        built = ws.build(work / "classify", has_notes, compiled=ready)
        ready = None
        if isinstance(built, str):
            raise RuntimeError(f"the source does not compile:\n{built}")
        cand = built
        hashes = picture_hashes(cand, typed, work)
        comp = compare(cand.current(), typed, tol, hashes)
        open_res = comp.open()
        summary = comp.summary()
        by_kind: JsonObject = {k: n for k, n in summary.items()}
        iterations.append({"iteration": it, "open": len(open_res), "by_kind": by_kind,
                           "geometry_error": round(sum(math.hypot(r.dx, r.dy)
                                                       for r in open_res if isinstance(r, (TextGeometry, BoxGeometry))), 1)})
        ink = fg.observe(it, cand, comp) if fg else None
        if ink is not None:
            iterations[-1]["ink"] = round(ink, 3)
        log(f"  iteration {it}: {len(open_res)} open residuals {summary}"
            + (f", ink {ink:.3f}" if ink is not None else ""))
        if not open_res or it == max_iter:
            break
        state = tuple(sorted(residual_line(residual_json(r)) for r in open_res))
        seen[state] = seen.get(state, 0) + 1
        if seen[state] >= 3:
            log("    the same residuals keep coming back: stopping")
            for r in open_res:
                unresolved.append(given_up(r, "edits oscillate (the same residuals came back)"))
            break
        # residuals that keep coming back after edits are given up on; a text/style rewrite (the
        # only kinds that overwrite existing source text) that never converged is reverted below,
        # so a bad attempt is never left as a garbled mix of the author's words and the failed edit
        newly_blocked: dict[Signature, int] = {}   # signature -> its entry in `unresolved`
        blocked_groups: set[Signature] = set()
        for r in open_res:
            sig = signature(r)
            if sig in blocked:
                continue
            hist = attempts.setdefault(sig, [])
            hist.append(magnitude(r))
            # an edit that adds or rewrites words is tried once: repeating it would pile up text
            if (r.kind in ONCE and tried.get(sig, 0) >= 1) or (r.kind in ADDITIVE and tried.get(sig, 0) >= 2) or \
                    (len(hist) >= 4 and (r.kind != "geometry" or hist[-1] >= 0.8 * hist[-3])):
                blocked.add(sig)
                unresolved.append(given_up(r, "not converging after repeated edits"))
                group = edit_group(sig)
                if group in attempted:
                    newly_blocked[sig] = len(unresolved) - 1
                    blocked_groups.add(group)
        planner = Planner(cand, comp, deck, ctx, ws, blocked, last_values, hashes)
        edits, failed = planner.plan()
        for f in failed:
            blocked.add(signature(f.residual))
            unresolved.append(f)
        if not edits and not newly_blocked:
            break
        if edits:
            before = {p: ws.source.text(p) for p in ws.source.order}
            applied = ws.write(edits)
            extra = ensure_preamble(ws, ctx)
            if extra:
                ws.write(extra)
            check, err = ws.compile()
            if check is None:
                log(f"    the edits break the build:\n{err}")
                # find the edits that break the build: from the text before the round, keep adding one
                # edit to those known to be good (offsets stay those of the original text)
                good: list[Edit] = []
                for e in applied:
                    restore(ws, before)
                    ws.write(good + [e])
                    pre = ensure_preamble(ws, ctx)
                    if pre:
                        ws.write(pre)
                    if ws.compile()[0] is None:
                        blocked.add(e.signature[:SIG_LEN])
                        unresolved.append(BrokenEdit(kind=e.kind, why="the edit breaks compilation", edit=e.text[:200],
                                                     signature=tuple(map(str, e.signature))))
                    else:
                        good.append(e)
                restore(ws, before)
                ws.write(good)
                pre = ensure_preamble(ws, ctx)
                if pre:
                    ws.write(pre)
                log(f"    {len(applied) - len(good)} edit(s) broke the build and were dropped")
                applied = good
            for e in applied:
                sig = e.signature[:SIG_LEN]
                if e.kind in REVERTIBLE:
                    attempted.setdefault(edit_group(sig), []).append((sig, e.file, before[e.file][e.start:e.end], e.text))
            for sig in {e.signature[:SIG_LEN] for e in applied}:
                tried[sig] = tried.get(sig, 0) + 1
        if blocked_groups:
            # undo every REVERTIBLE edit a now-blocked group made, most recent first: each of its
            # sub-ranges (a colour/style command narrowed to less than the group's edits, e.g. one
            # word at a time) is chased by content, not the offsets it was written at, since a
            # later sub-range's edit may have grown or nested around an earlier one (h6b) - so an
            # earlier step's exact text can vanish from the file until its followers are undone too
            reverted: set[Signature] = set()
            for group in blocked_groups:
                for sig, file, orig, new in reversed(attempted.pop(group, [])):
                    if not new or new == orig:
                        continue
                    text = ws.source.text(file)
                    at = text.find(new)
                    if at >= 0 and text.find(new, at + 1) < 0:
                        ws.write([Edit(file, at, at + len(new), orig, "revert", sig, "")])
                        reverted.add(group)
            for sig, at in newly_blocked.items():
                entry = unresolved[at]
                if edit_group(sig) in reverted and isinstance(entry, Unresolved):
                    unresolved[at] = replace(entry, why=entry.why + " (the unconverged rewrite was reverted)")
    put: list[PutBack] = []
    now_scores: list[float | None] = []
    if fg is not None and cand is not None:
        put = put_back(ws, fg, log)
        if put:
            # what the report says (residuals, converged) must be of the source it hands over
            built = ws.build(work / "classify", has_notes, compiled=(ws.build_dir / f"{ws.main.stem}.pdf", ""))
            if isinstance(built, str):
                raise RuntimeError(f"the source does not compile:\n{built}")
            cand = built
            hashes = picture_hashes(cand, typed, work)
            comp = compare(cand.current(), typed, tol, hashes)
            fg.observe(len(iterations), cand, comp)
            now_scores = [fg.score_now(p.final) for p in put]
            log(f"  after the frame guard: {len(comp.open())} open residuals {comp.summary()}")
    open_res = comp.open() if comp else []
    files: dict[str, str | Path] = {}
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
    sources = "".join(ws.source.text(p) for p in ws.source.order)
    for dest in ctx.files.values():
        rel = dest.relative_to(ws.src)
        if not (ws.root / rel).exists() and rel.as_posix() in sources:
            files[str(ws.root / rel)] = dest  # binary: copied on apply
    final_unresolved: list[Unresolved] = []
    for u in dedupe_unresolved(unresolved, open_res):
        si = current_slide_of(u.residual)
        frame = cand.frames[si] if cand and si is not None and si < len(cand.frames) else None
        if frame is not None:
            u = replace(u, frame=FrameAt(
                where=f"{(ws.root / frame.file.relative_to(ws.src)).as_posix()}:{frame.begin_line}-{frame.end_line}",
                label=frame.label))
        final_unresolved.append(u)
    shown: dict[int, int] = {tj: ci for ci, tj in comp.slides if ci is not None and tj is not None} if comp else {}
    restored: list[JsonObject] = []
    for k, p in enumerate(put):
        # where the frame stands now, in the author's tree
        ci = next((shown[j] for j in p.target_slides if j in shown), None)
        frame = cand.frames[ci] if cand and ci is not None and ci < len(cand.frames) else None
        where = (ws.root / p.file.relative_to(ws.src)).as_posix() + \
            (f":{frame.begin_line}-{frame.end_line}" if frame else "")
        restored.append(put_back_json(p, now_scores[k] if now_scores else None, where))
    theme = [residual_json(r) for r in comp.residuals if is_theme(r)] if comp else []

    def used(n: str) -> bool:
        """A note about a picture file the patch no longer names is left out."""
        m = re.match(r"(\S+\.(png|jpg|pdf)): ", n)
        return m is None or m.group(1) in patch
    notes = list(dict.fromkeys(n for n in ctx.notes if used(n)))
    labels = list(dict.fromkeys(ctx.label_notes))
    return Result(converged=not open_res, iterations=iterations,
                  unresolved=[unresolved_json(u) for u in final_unresolved],
                  residuals=[residual_json(r) for r in open_res], files=files, patch=patch, work=ws.work,
                  theme=theme, notes=notes, originals=ws.originals, labels=labels, restored=restored)


def magnitude(r: Residual) -> float:
    """How far off a residual is, for telling a converging one: a move's length plus its change of
    size, 1 for anything that is not a place."""
    if isinstance(r, BoxGeometry):
        return math.hypot(r.dx, r.dy) + abs(r.dw) + abs(r.dh)
    if isinstance(r, TextGeometry):
        return math.hypot(r.dx, r.dy)
    return 1.0


def put_back(ws: Workspace, fg: "FrameGuard", log: Callable[[str], object]) -> list[PutBack]:
    """Write back every frame the guard found worse than at its best round (`FrameGuard.plan`), and
    compile. Frames put back together come from different rounds; should they not compile together,
    they go back one at a time and a frame that breaks the build stays as the loop left it. Returns
    one entry per frame put back, for the report."""
    from .frame_guard import why
    plan = fg.plan()
    if not plan:
        return []
    edits = [Edit(final.file, final.span[0], final.span[1], best.text, "restore", ("restore", final.file, final.span[0]), "")
             for final, best in plan]
    before = {p: ws.source.text(p) for p in ws.source.order}
    ws.write(edits)
    kept: list[int] = list(range(len(plan)))
    if ws.compile()[0] is None:
        kept = []
        for k, e in enumerate(edits):
            restore(ws, before)
            ws.write([edits[i] for i in kept] + [e])
            if ws.compile()[0] is not None:
                kept.append(k)
        restore(ws, before)
        ws.write([edits[i] for i in kept])
        if ws.compile()[0] is None:          # cannot happen: the last good set compiled
            restore(ws, before)
            ws.compile()
            return []
    out: list[PutBack] = []
    for k in kept:
        final, best = plan[k]
        out.append(PutBack(target_slides=tuple(final.slides), frame_label=final.label, iteration=best.round,
                           mode=final.mode, score_left=round(final.score, 3), score_best=round(best.score, 3),
                           penalty_left=final.penalty, penalty_best=best.penalty, why=why(final, best),
                           file=final.file, final=final))
    log(f"  frame guard: {len(out)} frame(s) put back to their best round"
        + (f", {len(plan) - len(kept)} not (they break the build together)" if len(kept) < len(plan) else ""))
    return out


def restore(ws: Workspace, texts: dict[Path, str]) -> None:
    for p, t in texts.items():
        p.write_text(t, encoding="utf-8", newline="")
        ws.source.set_text(p, t)
    ws.reload()


def dedupe_unresolved(unresolved: Sequence[GivenUp], open_res: Sequence[Residual]) -> list[Unresolved]:
    """The residuals still open at the end, each with the reason recorded when it was first given up
    on (an edit that broke the build is about no residual)."""
    why: dict[Signature, str] = {}
    for u in unresolved:
        if isinstance(u, Unresolved):
            why.setdefault(signature(u.residual), u.why)
    return [given_up(r, why.get(signature(r), "open when the loop stopped")) for r in open_res]


def uses_notes(source: Source) -> bool:
    return any(re.search(r"\\note\b", mask_comments(t)) for t in source.texts.values())


def ir_from_tex(tex: Path, work: Path, handout: bool) -> JsonObject:
    """classify IR of a source as the loop sees it (slide keys from frame labels, pictures cropped
    into work/pictures so they can be hashed): a target made from another source."""
    ws = Workspace(tex, work, handout=handout, engine=None, fresh=True)
    cand = ws.build(work / "classify", uses_notes(ws.source), None)
    if isinstance(cand, str):
        raise RuntimeError(f"{tex} does not compile:\n{cand}")
    from .pdf import Document
    from PIL import Image
    target = cand.target()
    doc = Document(cand.pdf)
    try:
        for si, s in enumerate(as_objects(target["slides"], "target slides")):
            for k, e in enumerate(as_objects(s["elements"], f"target slide {si}: elements")):
                if e["kind"] != "image":
                    continue
                bbox = box(e["bbox"], At(where=f"target slide {si} element {k}", path="bbox"))
                if bbox[2] - bbox[0] >= 1 and bbox[3] - bbox[1] >= 1:
                    page = as_int(s["page"], f"target slide {si}: page")
                    path = work / "pictures" / f"p{page}-{k}.png"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(doc[page].render(4.0, bbox, transparent=False)).save(path)
                    e["file"] = str(path)
    finally:
        doc.close()
    return target


def class_pt_option(source: Source) -> int:
    m = re.search(r"\\documentclass\s*\[([^\]]*)\]\s*\{beamer\}", mask_comments(source.text(source.main)))
    if m:
        pt = re.search(r"(\d+)pt", m.group(1))
        if pt:
            return int(pt.group(1))
    return BEAMER_PT


# ---------------------------------------------------------------- reports

def report(result: Result, target: JsonObject) -> tuple[JsonObject, str]:
    iterations: list[Json] = [it for it in result.iterations]
    unresolved: list[Json] = [clean(u) for u in result.unresolved]
    theme: list[Json] = [clean(u) for u in result.theme]
    changed: list[Json] = [f for f in result.files]
    pictures: list[Json] = [n for n in result.notes]
    labels: list[Json] = [n for n in result.labels]
    restored: list[Json] = [clean(r) for r in result.restored]
    data: JsonObject = {"converged": result.converged, "iterations": iterations, "unresolved": unresolved,
                        "theme": theme, "changed_files": changed, "pictures": pictures, "labels": labels,
                        "restored": restored}
    md = ["# Pull report", "", f"Converged: **{result.converged}** after {len(result.iterations) - 1} edit rounds.", ""]
    md.append("| iteration | open residuals | by kind | geometry error (pt) |")
    md.append("|---|---|---|---|")
    for it in result.iterations:
        md.append(f"| {it['iteration']} | {it['open']} | {json.dumps(it['by_kind'])} | {it['geometry_error']} |")
    if result.restored:
        md += ["", "## Frames put back", "",
               "The loop's edits made these frames worse than they had been, so each has its best round's text:", ""]
        for r in result.restored:
            slides = ", ".join(str(as_int(j, "target_slides") + 1) for j in as_array(r["target_slides"], "restored"))
            label = f" ({r['frame_label']})" if r.get("frame_label") else ""
            md.append(f"- slide {slides}{label}: {r['why']}")
            if r.get("where"):
                md.append(f"  - source: {r['where']}")
    if result.unresolved:
        md += ["", "## Left for the author", ""]
        for u in result.unresolved:
            tj = u.get("target_slide")
            ts = as_objects(target["slides"], "target slides")[tj] if isinstance(tj, int) else None
            head = f"- slide {tj + 1 if isinstance(tj, int) else '?'}"
            if ts is not None:
                head += f" ({ts.get('key') or slide_title(ts) or 'untitled'})"
            md.append(f"{head}: {residual_line(u)} — {u.get('why')}")
            if u.get("where"):
                md.append(f"  - source: {u['where']}")
    if result.notes:
        md += ["", "## Pictures", ""] + [f"- {n}" for n in result.notes]
    if result.labels:
        md += ["", "## Duplicate labels", ""] + [f"- {n}" for n in result.labels]
    if result.theme:
        md += ["", "## Theme differences (not written to the source)", ""]
        md += [f"- {residual_line(u)}" for u in result.theme]
    if result.patch:
        md += ["", "## Patch", "", "```diff", result.patch.rstrip(), "```"]
    return data, "\n".join(md) + "\n"


def clean(r: JsonObject) -> JsonObject:
    return as_object(json.loads(json.dumps(r, default=str)), "clean")


# ---------------------------------------------------------------- commands

def file_sha1(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def replace_file(path: Path, new: str | Path) -> None:
    """Write `new` (text, or a file to copy) to `path` through a temporary file in the same folder
    (the copy of what was there is `keep_backup`'s job). A process killed at any moment leaves either
    the old file or the new one, never half of one: everything is written under another name and
    moved into place, and os.replace is atomic on Windows as it is on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".b2s-writing")
    try:
        if isinstance(new, Path):
            shutil.copy2(new, tmp)
        else:
            tmp.write_text(new, encoding="utf-8", newline="")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def unchanged_since_pull(path: Path, originals: dict[str, str]) -> bool:
    """Whether the file on disk is still the one the loop started from (a new file must still be
    absent). A file edited meanwhile is left alone: the pull's version goes next to it."""
    was = originals.get(str(path))
    now = file_sha1(path)
    return now == was if was is not None else now is None


def write_outputs(result: Result, target: JsonObject, tex: Path, work: Path, apply: bool, out: Path | None,
                  log: Callable[[str], object]) -> None:
    """pull.patch, edits.json and edits.md in `work`; the edited files in place when `apply`, each
    with a backup that never replaces an older one (`keep_backup`), or the edited source tree in
    `out`."""
    data, md = report(result, target)
    (work / "pull.patch").write_text(result.patch, encoding="utf-8", newline="\n")
    (work / "edits.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    (work / "edits.md").write_text(md, encoding="utf-8")
    if out is not None:
        out = Path(out).resolve()
        kept = copy_tree(result.work / "src", out)
        log(f"edited source tree -> {out}")
        if kept:
            log(f"  {len(kept)} file(s) that were already there kept as .bak (e.g. {kept[0].name})")
    elif apply:
        from .faults import fail_at
        skipped: list[Json] = []
        for name, new in result.files.items():
            path = Path(name)
            if result.originals and not unchanged_since_pull(path, result.originals):
                # Someone wrote to this file while the pull was compiling: their version wins.
                side = path.with_name(path.name + ".b2s-new")
                replace_file(side, new)
                skipped.append(str(path))
                log(f"  {path} changed since the pull started: left alone, its new version is {side}")
                continue
            fail_at("pull:apply")
            path.parent.mkdir(parents=True, exist_ok=True)
            bak = keep_backup(path, new)
            replace_file(path, new)
            log(f"  wrote {path}" + (f" (what was there is now {bak.name})" if bak else ""))
        if skipped:
            data["not_applied"] = skipped
            (work / "edits.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    state = "converged" if result.converged else f"{len(result.unresolved)} residual(s) left"
    log(f"{state} after {len(result.iterations) - 1} edit round(s); {len(result.files)} file(s) changed; "
        f"report {work / 'edits.md'}")


def run_pull(target: "JsonObject | Later", tex: Path, work: Path, apply: bool, out: Path | None, max_iter: int,
             handout: bool, engine: str | None, log: Callable[[str], object]) -> Result:
    """`target`: the deck, or a `Later` that fetches it - read while the source first compiles."""
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    result = converge(Path(tex), target, work / "loop", max_iter, handout, engine, TOL, log, True, None)
    deck = target.value if isinstance(target, Later) else target
    if deck is None:
        raise RuntimeError("the deck was never read")   # converge reads it before anything else
    (work / "target.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    write_outputs(result, deck, Path(tex), work, apply, out, log)
    return result


def cmd_pull(deck: str, tex: Path, work: Path | None, apply: bool, out: Path | None, max_iter: int,
             handout: bool, engine: str | None) -> Result:
    """Read a live deck (presentations.get only: the deck is never written) and converge the source to it."""
    from .deck_ir import read_deck
    ref = Path(deck)
    if work is None:
        work = ref / "pull" if ref.is_dir() else Path(tex).resolve().parent / "out" / "pull"
    work = Path(work).resolve()

    def read():
        # On a thread of its own while the source compiles for the first time (`converge`): the
        # deck read needs no PDF and the compile needs no deck, and this is the one place in a
        # pull where a Google round trip and a TeX run stand in each other's way.
        read_back = read_deck(deck, work / "target-images", None, None, None, False, None, None)
        print(f"deck: {len(read_back.slides)} slides read")
        return target_json(read_back)

    return run_pull(Later(read), tex, work, apply, out, max_iter, handout, engine, print)


def cmd_converge(target_path: Path, tex: Path, work: Path | None, apply: bool, out: Path | None, max_iter: int,
                 handout: bool, engine: str | None) -> Result:
    """Offline twin of pull: the target is a deck.json-shaped file (deck_ir output or classify's)."""
    target = json.loads(Path(target_path).read_text(encoding="utf-8"))
    work = Path(work) if work else Path(target_path).resolve().parent / "pull"
    return run_pull(target, tex, work, apply, out, max_iter, handout, engine, print)
