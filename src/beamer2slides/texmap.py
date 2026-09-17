"""From PDF pages and elements back to the beamer source.

- `Source`: the main .tex with its \\input/\\include files, frames in document order (spans, labels,
  titles, fragile), with comments masked so offsets stay valid.
- `synctex_pages`: page -> (file, line) votes from a `-synctex=1` build. Beamer collects a frame's
  body as a macro argument, so every node inside a frame reports the `\\end{frame}` line: SyncTeX
  tells which frame a page shows, the words themselves are found by text (`Visible`).
- `Visible`: the text a stretch of LaTeX prints, with a source span per character (commands,
  braces, comments, overlay specs left out; `~`, `--`, quotes and accents as the PDF shows them,
  math as one opaque character).
- `locate`: paragraphs of a classified element -> source spans of their words.
"""

import difflib
import gzip
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

OPAQUE = "\ue000"  # stands for math and other things whose PDF text can't be predicted
PARA = "\n"

INPUT_RE = re.compile(r"\\(input|include|subfile)\s*\{([^}]+)\}")
BEGIN_FRAME_RE = re.compile(r"\\begin\s*\{frame\}")
END_FRAME_RE = re.compile(r"\\end\s*\{frame\}")


def mask_comments(text: str) -> str:
    """Comments replaced by spaces (same length): offsets stay valid, `%` inside \\% kept."""
    out = []
    for line in text.split("\n"):
        i = 0
        while True:
            j = line.find("%", i)
            if j < 0:
                break
            k = j - 1
            slashes = 0
            while k >= 0 and line[k] == "\\":
                slashes += 1
                k -= 1
            if slashes % 2 == 0:
                line = line[:j] + " " * (len(line) - j)
                break
            i = j + 1
        out.append(line)
    return "\n".join(out)


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def offset_of_line(text: str, line: int) -> int:
    pos = 0
    for _ in range(line - 1):
        pos = text.find("\n", pos) + 1
        if pos == 0:
            return len(text)
    return pos


def skip_space(s: str, i: int, end: int | None = None) -> int:
    end = len(s) if end is None else end
    while i < end and s[i] in " \t\r\n":
        i += 1
    return i


def match_group(s: str, i: int, open_: str = "{", close: str = "}") -> int:
    """Index after the group opening at s[i] (balanced, backslash escapes skipped); -1 if unbalanced."""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def read_args(s: str, i: int, spec: str) -> tuple[list[tuple[str, int, int] | None], int]:
    """Arguments after a command at s[i] by spec letters: s star, < overlay, o/O optional [..],
    m/M mandatory {..} or one token, ( coordinate pair. Returns [(kind, start, end)] (content
    offsets) and the index after them."""
    args = []
    for kind in spec:
        j = skip_space(s, i) if kind in "mM(" else i
        if kind == "s":
            if j < len(s) and s[j] == "*":
                args.append(("s", j, j + 1))
                i = j + 1
            else:
                args.append(None)
        elif kind == "<":
            j = skip_space(s, i)
            if j < len(s) and s[j] == "<":
                k = s.find(">", j)
                args.append(("<", j + 1, k) if k > 0 else None)
                i = k + 1 if k > 0 else i
            else:
                args.append(None)
        elif kind in "oO":
            j = skip_space(s, i)
            if j < len(s) and s[j] == "[":
                k = match_group(s, j, "[", "]")
                if k < 0:
                    args.append(None)
                    continue
                args.append((kind, j + 1, k - 1))
                i = k
            else:
                args.append(None)
        elif kind == "(":
            if j < len(s) and s[j] == "(":
                k = s.find(")", j)
                args.append(("(", j + 1, k) if k > 0 else None)
                i = k + 1 if k > 0 else i
            else:
                args.append(None)
        else:
            if j < len(s) and s[j] == "{":
                k = match_group(s, j)
                if k < 0:
                    args.append(None)
                    continue
                args.append((kind, j + 1, k - 1))
                i = k
            elif j < len(s) and s[j] == "\\":
                m = re.match(r"\\([A-Za-z@]+|.)", s[j:])
                args.append((kind, j, j + len(m.group(0))))
                i = j + len(m.group(0))
            elif j < len(s) and s[j] not in "}":
                args.append((kind, j, j + 1))
                i = j + 1
            else:
                args.append(None)
    return args, i


# ---------------------------------------------------------------- source files and frames

@dataclass
class Frame:
    index: int
    file: Path
    start: int            # offset of \begin{frame}
    end: int              # offset after \end{frame}
    body: int             # offset after the frame's arguments
    body_end: int         # offset of \end{frame}
    begin_line: int
    end_line: int
    label: str | None
    title: str | None
    options: str
    fragile: bool


class Source:
    """The document: main file, included files (in document order) and its frames."""

    def __init__(self, main: Path):
        self.main = Path(main).resolve()
        self.root = self.main.parent
        self.texts: dict[Path, str] = {}
        self.order: list[Path] = []
        self.frames: list[Frame] = []
        self._load(self.main, set())

    def resolve(self, name: str) -> Path | None:
        for cand in (self.root / name, self.root / f"{name}.tex"):
            if cand.is_file():
                return cand.resolve()
        return None

    def _load(self, path: Path, seen: set) -> None:
        if path in seen:
            return
        seen.add(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        self.texts[path] = text
        self.order.append(path)
        masked = mask_comments(text)
        events = [(m.start(), "input", m) for m in INPUT_RE.finditer(masked)]
        events += [(m.start(), "frame", m) for m in BEGIN_FRAME_RE.finditer(masked)]
        pos_end = 0
        for start, kind, m in sorted(events, key=lambda e: e[0]):
            if start < pos_end:
                continue  # inside a frame already taken
            if kind == "input":
                sub = self.resolve(m.group(2).strip())
                if sub:
                    self._load(sub, seen)
                continue
            end_m = END_FRAME_RE.search(masked, m.end())
            if not end_m:
                continue
            frame = self._frame(path, masked, m.start(), m.end(), end_m.start(), end_m.end())
            self.frames.append(frame)
            pos_end = end_m.end()
            # \input inside a frame body: the included file's frames can't exist; its text is body

    def _frame(self, path: Path, masked: str, start: int, after_begin: int, end_start: int, end: int) -> Frame:
        args, body = read_args(masked, after_begin, "<o<")
        opts = masked[args[1][1]:args[1][2]] if args[1] else ""
        # {title}{subtitle} directly after the options (only when a brace follows)
        title = None
        j = skip_space(masked, body)
        if j < end_start and masked[j] == "{":
            targs, body2 = read_args(masked, body, "M")
            title = masked[targs[0][1]:targs[0][2]] if targs[0] else None
            body = body2
            j = skip_space(masked, body)
            if j < end_start and masked[j] == "{":
                _, body = read_args(masked, body, "M")
        label = None
        lm = re.search(r"label\s*=\s*([^,\]\s]+)", opts)
        if lm:
            label = lm.group(1).strip("{}")
        if title is None:
            tm = re.search(r"\\frametitle\s*(?:<[^>]*>)?\s*(?:\[[^\]]*\])?\s*\{", masked[body:end_start])
            if tm:
                k = match_group(masked, body + tm.end() - 1)
                title = masked[body + tm.end():k - 1] if k > 0 else None
        return Frame(len(self.frames), path, start, end, body, end_start, line_of(masked, start),
                     line_of(masked, end_start), label, visible_text(title) if title else None, opts,
                     "fragile" in opts)

    def frame_at(self, path: Path, line: int) -> Frame | None:
        for f in self.frames:
            if f.file == path and f.begin_line <= line <= f.end_line:
                return f
        return None

    def text(self, path: Path) -> str:
        return self.texts[path]

    def set_text(self, path: Path, text: str) -> None:
        self.texts[path] = text

    def preamble_end(self) -> int:
        m = re.search(r"\\begin\s*\{document\}", mask_comments(self.texts[self.main]))
        return m.start() if m else 0

    def engine(self) -> str:
        head = self.texts[self.main][:400]
        m = re.search(r"%\s*!\s*(?:TEX\s+program|engine)\s*=\s*(\w+)", head, re.I)
        if m:
            return m.group(1).lower()
        pre = mask_comments(self.texts[self.main][:self.preamble_end() or None])
        return "lualatex" if re.search(r"\\usepackage(\[[^\]]*\])?\{(fontspec|unicode-math)\}", pre) else "pdflatex"

    def uses_overlays(self) -> bool:
        pat = re.compile(r"\\(pause|only|uncover|visible|invisible|onslide|alt|temporal)\b|\\item\s*<|<\d+-?\d*>")
        return any(pat.search(mask_comments(t)) for t in self.texts.values())


# ---------------------------------------------------------------- SyncTeX

@dataclass
class SyncPage:
    votes: dict[tuple[str, int], int] = field(default_factory=dict)
    boxes: list[tuple[str, int, float, float, float, float]] = field(default_factory=list)  # file, line, x0, y0, x1, y1


SYNC_RECORD_RE = re.compile(r"^([\[\(xkg$vh])(\d+),(\d+):(-?\d+),(-?\d+)(?::(-?\d+),(-?\d+),(-?\d+))?")
SP_TO_BP = 72 / 72.27 / 65536


def synctex_pages(path: Path) -> list[SyncPage]:
    """Per PDF page: how many SyncTeX records name each (input file, line), and hbox boxes in
    PDF pt (top-left origin)."""
    if not path.exists():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    inputs: dict[str, str] = {}
    unit, mag = 1.0, 1.0
    pages: list[SyncPage] = []
    # A sheet is `{n` ... `}n`. lualatex writes box resources (\saveboxresource: transparency
    # groups, shadings - beamer block shadows produce them) the same way, but closes them with
    # `}0`: counted as pages, every page after the first would be mapped to the wrong frame.
    stack: list[tuple[str, SyncPage]] = []
    page = None
    for ln in lines:
        if ln.startswith("Input:"):
            tag, _, name = ln[6:].partition(":")
            inputs[tag] = name
        elif ln.startswith("Unit:"):
            unit = float(ln[5:] or 1)
        elif ln.startswith("Magnification:"):
            mag = float(ln[14:] or 1000) / 1000
        elif ln.startswith("{"):
            page = SyncPage()
            stack.append((ln[1:].strip(), page))
        elif ln.startswith("}"):
            if stack:
                number, closed = stack.pop()
                page = stack[-1][1] if stack else None
                if ln[1:].strip() == number:   # a real sheet, in shipout order
                    pages.append(closed)
                elif page is not None:         # a box resource drawn on the page around it
                    for key, n in closed.votes.items():
                        page.votes[key] = page.votes.get(key, 0) + n
                    page.boxes += closed.boxes
            else:
                page = None
        elif page is not None:
            m = SYNC_RECORD_RE.match(ln)
            if not m:
                continue
            name = inputs.get(m.group(2))
            if name is None:
                continue
            key = (name, int(m.group(3)))
            page.votes[key] = page.votes.get(key, 0) + 1
            if m.group(1) == "(" and m.group(6) is not None:
                k = unit * mag * SP_TO_BP
                x, y = int(m.group(4)) * k, int(m.group(5)) * k
                w, h, d = int(m.group(6)) * k, int(m.group(7)) * k, int(m.group(8)) * k
                if w > 0:
                    page.boxes.append((name, key[1], x, y - h, x + w, y + d))
    return pages


def page_frames(source: Source, sync: list[SyncPage], labels: list[str] | None = None,
                cwd: Path | None = None) -> list[Frame | None]:
    """The frame each PDF page shows: SyncTeX votes for lines inside a frame's span; pages
    without votes (section pages from \\AtBeginSection, or no SyncTeX) fall back to counting
    frames along the page labels. `cwd`: the directory TeX ran in (relative input names)."""
    by_file = {str(p).lower(): p for p in source.order}
    base = Path(cwd) if cwd else source.root
    out: list[Frame | None] = []
    for page in sync:
        tally: dict[int, int] = {}
        for (name, line), n in page.votes.items():
            path = by_file.get(str((base / name).resolve()).lower()) if name else None
            if path is None:
                continue
            f = source.frame_at(path, line)
            if f:
                tally[f.index] = tally.get(f.index, 0) + n
        out.append(source.frames[max(tally, key=tally.get)] if tally else None)
    if labels and (not sync or all(f is None for f in out)):
        out, fi, prev = [], -1, None
        for label in labels:
            if label != prev:
                fi += 1
            prev = label
            out.append(source.frames[fi] if 0 <= fi < len(source.frames) else None)
    return out


def lines_for_box(page: SyncPage, bbox: list[float]) -> dict[tuple[str, int], float]:
    """(file, line) -> overlap area of SyncTeX hboxes with a PDF box."""
    out: dict[tuple[str, int], float] = {}
    for name, line, x0, y0, x1, y1 in page.boxes:
        w = min(x1, bbox[2]) - max(x0, bbox[0])
        h = min(y1, bbox[3]) - max(y0, bbox[1])
        if w > 0 and h > 0:
            out[(name, line)] = out.get((name, line), 0.0) + w * h
    return out


# ---------------------------------------------------------------- visible text

# Command -> argument spec (read_args letters; upper case arguments print).
ARGS = {
    "textbf": "M", "textit": "M", "emph": "M", "texttt": "M", "textsc": "M", "textsf": "M", "textrm": "M",
    "textsl": "M", "textup": "M", "textmd": "M", "textnormal": "M", "underline": "M", "uline": "M",
    "sout": "M", "hl": "M", "ul": "M", "st": "M", "mbox": "M", "text": "M", "textsuperscript": "M",
    "textsubscript": "M", "alert": "<M", "structure": "<M", "textcolor": "<mM", "color": "<om",
    "colorbox": "omM", "fcolorbox": "ommM", "href": "mM", "url": "M", "nolinkurl": "M", "hyperlink": "mM",
    "hypertarget": "mM", "footnote": "<oM", "frametitle": "<oM", "framesubtitle": "<M",
    "vspace": "sm", "hspace": "sm", "includegraphics": "<som", "label": "m", "ref": "m", "eqref": "m",
    "cite": "om", "note": "<om", "pause": "o", "only": "<M", "uncover": "<M", "visible": "<M",
    "invisible": "<m", "onslide": "s<", "alt": "<Mm", "temporal": "<mMm", "setbeamercolor": "smm",
    "setbeamerfont": "smm", "setbeamertemplate": "mo", "usebeamercolor": "om", "usebeamerfont": "sm",
    "definecolor": "mmm", "colorlet": "mm", "fontsize": "mm", "makebox": "ooM", "framebox": "ooM",
    "fbox": "M", "parbox": "ooomM", "raisebox": "moM", "scalebox": "moM", "resizebox": "smmM",
    "rotatebox": "omM", "phantom": "m", "hphantom": "m", "vphantom": "m", "ding": "m", "ensuremath": "m",
    "setlength": "mm", "addtolength": "mm", "setcounter": "mm", "tikz": "om", "textblockcolour": "m",
    "column": "om", "item": "<o", "caption": "oM", "section": "soM", "subsection": "soM",
    "usetheme": "om", "usecolortheme": "om", "usefonttheme": "om", "graphicspath": "m",
    "againframe": "<om", "circled": "M", "newline": "", "linebreak": "o", "hfill": "", "vfill": "",
}
TEXT_MACROS = {"LaTeX": "LATEX", "TeX": "TEX", "ldots": "...", "dots": "...", "textellipsis": "...",
               "textbackslash": "\\", "textbullet": "•", "S": "§", "P": "¶", "copyright": "©",
               "textendash": "–", "textemdash": "—", "textasciitilde": "~", "textasciicircum": "^",
               "textbar": "|", "textless": "<", "textgreater": ">", "ss": "ß", "ae": "æ", "oe": "œ",
               "o": "ø", "aa": "å", "l": "ł", "i": "ı", "euro": "€", "pounds": "£", "dag": "†",
               "ddag": "‡", "checkmark": OPAQUE, "today": OPAQUE, "insertframenumber": OPAQUE}
OPAQUE_PARAS = {"titlepage", "maketitle", "tableofcontents", "bibliography", "printbibliography"}
SPACE_CMDS = {",", ";", ":", " ", "quad", "qquad", "enspace", "thinspace", "enskip", "\\", "newline", "linebreak",
              "hfill", "hspace", "cr"}
PARA_CMDS = {"par", "vspace", "bigskip", "medskip", "smallskip", "vfill", "centering", "raggedright", "raggedleft"}
ACCENT_MARKS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304",
                ".": "\u0307", "c": "\u0327", "v": "\u030c", "u": "\u0306", "H": "\u030b", "r": "\u030a"}
ESCAPES = {"&": "&", "%": "%", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}"}
# Environments whose mandatory arguments don't print; figures and code are opaque.
ENV_ARGS = {"frame": "<o<", "minipage": "ooom", "column": "om", "columns": "o", "tabular": "om",
            "tabularx": "mom", "textblock": "m(", "textblock*": "m(", "overlayarea": "mm", "onlyenv": "<",
            "block": "<M", "alertblock": "<M", "exampleblock": "<M", "itemize": "<o", "enumerate": "<o",
            "description": "<o", "center": "", "flushleft": "", "flushright": "", "quote": "",
            "actionenv": "<", "visibleenv": "<", "uncoverenv": "<", "altenv": "<mmmm", "multicols": "m"}
OPAQUE_ENVS = {"tikzpicture", "equation", "equation*", "align", "align*", "gather", "gather*", "multline",
               "displaymath", "math", "figure", "pgfpicture", "axis", "table"}
VERBATIM_ENVS = {"verbatim", "lstlisting", "minted", "semiverbatim", "Verbatim"}
LIST_ENVS = {"itemize", "enumerate", "description"}


@dataclass
class Item:
    start: int         # offset of \item
    body: int          # offset after \item<..>[..]
    end: int           # where the item's own text ends (next \item, nested list or \end)
    level: int
    env: str
    vis: int           # visible offset of the item's first character


@dataclass
class ListEnv:
    env: str
    start: int         # offset of \begin
    end: int           # offset after \end{env}
    level: int
    items: list[Item] = field(default_factory=list)


@dataclass
class Visible:
    """Printed text of a source range, one source span per character."""
    text: str = ""
    starts: list[int] = field(default_factory=list)
    ends: list[int] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    lists: list[ListEnv] = field(default_factory=list)
    title: tuple[int, int] | None = None        # visible range of the frame title
    title_src: tuple[int, int] | None = None     # its source range

    def add(self, s: str, a: int, b: int) -> None:
        for ch in s:
            if ch in " " + PARA and (not self.text or self.text[-1] in " " + PARA):
                if ch == PARA and self.text and self.text[-1] == " ":
                    self.text = self.text[:-1] + PARA
                    self.ends[-1] = b
                continue
            self.text += ch
            self.starts.append(a)
            self.ends.append(b)


def _decode_accent(mark: str, letter: str) -> str:
    base = {"i": "i", "\\i": "i", "\\j": "j"}.get(letter, letter)
    return unicodedata.normalize("NFC", base + ACCENT_MARKS[mark])


def build_visible(s: str, start: int, end: int, title_frame: bool = False) -> Visible:
    """Walk s[start:end] (comments already masked) and collect what prints."""
    v = Visible()
    list_stack: list[ListEnv] = []

    def close_item(at: int) -> None:
        if list_stack and list_stack[-1].items and list_stack[-1].items[-1].end < 0:
            list_stack[-1].items[-1].end = at

    def walk(i: int, stop: int) -> None:
        while i < stop:
            c = s[i]
            if c == "\\":
                m = re.match(r"\\([A-Za-z@]+\*?|.)", s[i:stop])
                if not m:
                    i += 1
                    continue
                name = m.group(1)
                j = i + len(m.group(0))
                if name in ESCAPES:
                    v.add(ESCAPES[name], i, j)
                    i = j
                elif name in ACCENT_MARKS and len(name) == 1 and (name in "'`^\"~=." or not name.isalpha()):
                    args, k = read_args(s, j, "M")
                    letter = s[args[0][1]:args[0][2]] if args[0] else ""
                    v.add(_decode_accent(name, letter) if len(letter.replace("\\", "")) == 1 else letter, i, k)
                    i = k
                elif name in ("(", "["):
                    close = "\\)" if name == "(" else "\\]"
                    k = s.find(close, j, stop)
                    k = stop if k < 0 else k + 2
                    if name == "[":
                        v.add(PARA, i, i)
                    v.add(OPAQUE, i, k)
                    if name == "[":
                        v.add(PARA, k, k)
                    i = k
                elif name == "begin":
                    args, k = read_args(s, j, "m")
                    env = s[args[0][1]:args[0][2]].strip() if args[0] else ""
                    i = begin_env(env, i, k, stop)
                elif name == "end":
                    args, k = read_args(s, j, "m")
                    env = s[args[0][1]:args[0][2]].strip() if args[0] else ""
                    if env in LIST_ENVS and list_stack:
                        close_item(i)
                        lst = list_stack.pop()
                        lst.end = k
                    v.add(PARA, i, k)
                    i = k
                elif name == "item":
                    args, k = read_args(s, j, ARGS["item"])
                    close_item(i)
                    v.add(PARA, i, i)
                    lst = list_stack[-1] if list_stack else None
                    if lst is not None:
                        lst.items.append(Item(i, k, -1, lst.level, lst.env, len(v.text)))
                        v.items.append(lst.items[-1])
                    if args[1] and (lst is None or lst.env == "description"):
                        walk(args[1][1], args[1][2])
                        v.add("\t", k, k)
                    i = k
                elif name in TEXT_MACROS:
                    k = j + 2 if j < stop and s[j:j + 2] == "{}" else j
                    v.add(TEXT_MACROS[name], i, k)
                    i = k
                elif name in OPAQUE_PARAS:
                    v.add(PARA, i, i)
                    v.add(OPAQUE, i, j)
                    v.add(PARA, j, j)
                    i = j
                elif name in ARGS:
                    spec = ARGS[name]
                    args, k = read_args(s, j, spec)
                    if name in PARA_CMDS:
                        v.add(PARA, i, k)
                    elif name in SPACE_CMDS:
                        v.add(" ", i, k)
                    if name in ("ding", "ensuremath", "tikz", "ref", "eqref", "cite") and any(args):
                        v.add(OPAQUE, i, k)
                    heading = name in ("frametitle", "framesubtitle")
                    if heading:
                        v.add(PARA, i, i)
                    for kind, a in zip(spec, args):
                        if a and kind in "MO":
                            if heading and v.title is None and name == "frametitle":
                                t0 = len(v.text)
                                walk(a[1], a[2])
                                v.title, v.title_src = (t0, len(v.text)), (a[1], a[2])
                            else:
                                walk(a[1], a[2])
                    if heading:
                        v.add(PARA, k, k)
                    i = k
                elif name in SPACE_CMDS:
                    v.add(" ", i, j)
                    i = j
                elif name in PARA_CMDS:
                    v.add(PARA, i, j)
                    i = j
                else:
                    # unknown commands print nothing; a group right after them is their text
                    i = j
                    if name[:1].isalpha():
                        while i < stop and s[i] in " \t":
                            i += 1
            elif c == "$":
                double = s[i:i + 2] == "$$"
                k = s.find("$$" if double else "$", i + (2 if double else 1), stop)
                while k > 0 and s[k - 1] == "\\":
                    k = s.find("$", k + 1, stop)
                k = stop if k < 0 else k + (2 if double else 1)
                if double:
                    v.add(PARA, i, i)
                v.add(OPAQUE, i, k)
                if double:
                    v.add(PARA, k, k)
                i = k
            elif c in "{}":
                i += 1
            elif c == "~":
                v.add(" ", i, i + 1)
                i += 1
            elif c == "-" and s[i:i + 3] == "---":
                v.add("—", i, i + 3)
                i += 3
            elif c == "-" and s[i:i + 2] == "--":
                v.add("–", i, i + 2)
                i += 2
            elif c == "`" and s[i:i + 2] == "``":
                v.add("“", i, i + 2)
                i += 2
            elif c == "'" and s[i:i + 2] == "''":
                v.add("”", i, i + 2)
                i += 2
            elif c == "`":
                v.add("‘", i, i + 1)
                i += 1
            elif c == "'":
                v.add("’", i, i + 1)
                i += 1
            elif c == "&":
                v.add(" ", i, i + 1)
                i += 1
            elif c in " \t\r":
                i += 1
                v.add(" ", i - 1, i)
            elif c == "\n":
                k = i + 1
                while k < stop and s[k] in " \t\r":
                    k += 1
                if k < stop and s[k] == "\n":
                    v.add(PARA, i, k)
                else:
                    v.add(" ", i, i + 1)
                i = k if k < stop and s[k] == "\n" else i + 1
            else:
                v.add(c, i, i + 1)
                i += 1

    def begin_env(env: str, i: int, k: int, stop: int) -> int:
        if env in VERBATIM_ENVS:
            m = re.compile(r"\\end\s*\{" + re.escape(env) + r"\}").search(s, k, stop)
            e = m.start() if m else stop
            v.add(PARA, i, k)
            for a in range(skip_space(s, k, e), e):
                v.add(s[a] if s[a] != "\n" else PARA, a, a + 1)
            v.add(PARA, e, m.end() if m else stop)
            return m.end() if m else stop
        if env in OPAQUE_ENVS:
            m = re.compile(r"\\end\s*\{" + re.escape(env) + r"\}").search(s, k, stop)
            e = m.end() if m else stop
            v.add(PARA, i, i)
            v.add(OPAQUE, i, e)
            v.add(PARA, e, e)
            return e
        spec = ENV_ARGS.get(env, "o")
        args, k2 = read_args(s, k, spec)
        v.add(PARA, i, k2)
        if env in LIST_ENVS:
            lst = ListEnv(env, i, -1, len(list_stack))
            if list_stack:
                close_item(i)
            list_stack.append(lst)
            v.lists.append(lst)
        for kind, a in zip(spec, args):
            if a and kind in "MO":
                walk(a[1], a[2])
                v.add(PARA, a[2], a[2])
        return k2

    if title_frame:
        # a frame: \begin{frame}<..>[..]<..>{title}{subtitle} body \end{frame}
        m = BEGIN_FRAME_RE.match(s, start)
        k = m.end() if m else start
        args, k = read_args(s, k, "<o<")
        j = skip_space(s, k)
        if j < end and s[j] == "{":
            targs, k2 = read_args(s, k, "M")
            a = len(v.text)
            walk(targs[0][1], targs[0][2])
            v.title, v.title_src = (a, len(v.text)), (targs[0][1], targs[0][2])
            v.add(PARA, k2, k2)
            k = k2
            j = skip_space(s, k)
            if j < end and s[j] == "{":
                sargs, k = read_args(s, k, "M")
                walk(sargs[0][1], sargs[0][2])
                v.add(PARA, k, k)
        tm = END_FRAME_RE.search(s, k, end)
        walk(k, tm.start() if tm else end)
    else:
        walk(start, end)
    for lst in list_stack:
        close_item(end)
        lst.end = end
    for lst in v.lists:
        for it in lst.items:
            if it.end < 0:
                it.end = lst.end
    return v


def visible_text(latex: str) -> str:
    """Plain printed text of a LaTeX snippet (paragraph breaks as spaces)."""
    masked = mask_comments(latex)
    return " ".join(build_visible(masked, 0, len(masked)).text.replace(PARA, " ").split())


def frame_visible(source: Source, frame: Frame) -> Visible:
    masked = mask_comments(source.text(frame.file))
    return build_visible(masked, frame.start, frame.end, title_frame=True)


# ---------------------------------------------------------------- locating words

WORD_RE = re.compile(r"\S+")
NORMALISE = str.maketrans({"\u00a0": " ", "\u2009": " ", "\u202f": " ", "\t": " ", "\x0b": " ", "“": '"', "”": '"',
                           "‘": "'", "’": "'", "ﬁ": "fi", "ﬂ": "fl", "…": "...", "−": "-"})


def norm_word(w: str) -> str:
    return unicodedata.normalize("NFC", w.translate(NORMALISE)).replace(" ", "")


def words_with_spans(text: str) -> list[tuple[str, int, int]]:
    t = text.translate(NORMALISE)
    return [(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(t)]


@dataclass
class WordMap:
    """Words of an element's text (the classified PDF text) found in a frame's visible text:
    for each word, (visible start, visible end) or None."""
    words: list[tuple[str, int, int]]
    vis: list[tuple[int, int] | None]
    score: float

    def span(self, i0: int, i1: int) -> tuple[int, int] | None:
        """Visible range covering words i0..i1-1, when all of them were found."""
        got = self.vis[i0:i1]
        if not got or any(g is None for g in got):
            return None
        return got[0][0], got[-1][1]


def locate_words(text: str, visible: Visible, lo: int = 0, hi: int | None = None) -> WordMap:
    """Align the words of `text` with the visible words in [lo, hi): exact word matches first,
    then single words differing only in punctuation or case."""
    hi = len(visible.text) if hi is None else hi
    words = words_with_spans(text)
    vwords = [(w, a + lo, b + lo) for w, a, b in words_with_spans(visible.text[lo:hi])]
    na = [norm_word(w) for w, _, _ in words]
    nb = [norm_word(w) for w, _, _ in vwords]
    sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    vis: list[tuple[int, int] | None] = [None] * len(words)
    matched = 0
    for tag, a0, a1, b0, b1 in sm.get_opcodes():
        if tag == "equal":
            for d in range(a1 - a0):
                vis[a0 + d] = (vwords[b0 + d][1], vwords[b0 + d][2])
            matched += a1 - a0
        elif tag == "replace" and a1 - a0 == b1 - b0:
            for d in range(a1 - a0):
                x, y = na[a0 + d], nb[b0 + d]
                if OPAQUE not in y and difflib.SequenceMatcher(None, x.casefold(), y.casefold()).ratio() >= 0.75:
                    vis[a0 + d] = (vwords[b0 + d][1], vwords[b0 + d][2])
                    matched += 0.5
    return WordMap(words, vis, matched / max(1, len(words)))


def find_block(text: str, visible: Visible) -> tuple[int, int, float]:
    """The visible range [lo, hi) best matching `text` (a paragraph): the matched words' extent."""
    wm = locate_words(text, visible)
    found = [g for g in wm.vis if g is not None]
    if not found:
        return 0, 0, 0.0
    # the densest run: drop stray single-word matches far from the rest
    mids = sorted(g[0] for g in found)
    med = mids[len(mids) // 2]
    near = [g for g in found if abs(g[0] - med) <= 3 * max(40, len(text))]
    return min(g[0] for g in near), max(g[1] for g in near), wm.score


def source_span(visible: Visible, a: int, b: int) -> tuple[int, int]:
    """Source offsets of visible characters a..b-1."""
    return visible.starts[a], visible.ends[b - 1]
