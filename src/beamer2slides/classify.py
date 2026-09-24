"""Stage 2: turn raw page content into native text boxes plus background leftovers (deck.json).

Pipeline per page:
  drawings/images -> panels (theme bars, blocks), figure regions, short bars, small images
  spans           -> lines (baseline clustering, scripts attached)
  lines           -> reasons: rotated | figure | theme | math, and bullets
  lines           -> paragraphs (continuation, alignment, TeX paragraph-break test)
  paragraphs      -> text boxes (same panel, same column, close vertically)
Every span ends up in exactly one text element or in `left_in_background`.
"""

import math
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace

from . import bidi
from .fonts import MATH_ITALIC_RE, FontInfo, font_info, serif_math_letters

BULLET_GLYPHS = set("▶►▸‣•◦▪■□○●★⋆✓∗–")
PRESET_GLYPHS = set("▶►▸‣•●★⋆")  # glyphs with a close Slides bullet preset (see emit.bullet_preset)
LABEL_GLYPHS = BULLET_GLYPHS | set("+✗✘→⇒—♦◆⋄")
ENUM_RE = re.compile(r"^(\(?\d{1,2}[.)]|\(?[a-z][.)]|\([a-z]\)|\(?[ivx]{1,4}[.)])$")
LINE_LABEL_RE = re.compile(r"^(\d{1,3}:|\[\d{1,3}\])$")
NUMBERING_RE = re.compile(r"^\(?(\d{1,2}(\.\d{1,2}){0,3}|[a-z]|[ivx]{1,4})[.)]?$")  # an item's number: 2.1, (b), iv.
LABEL_SEP_EM = 0.4  # gap after a description label (beamer: 0.5 em; word spaces are about 0.33 em)
GUTTER_PROSE_EM = 8.0  # a column's line beside a gutter is wider than this; ticks and table cells are not
EM_SPACE = chr(0x2003)
FRAME_COUNTER_RE =re.compile(r"^\d{1,4}( ?/ ?\d{1,4})?$")
EQ_NUMBER_RE = re.compile(r"^\(\d+(\.\d+)*[a-z]?\)$")
# A caption's label: "Figure:", "Figure 3:", "Fig. 2.", "Table IV:" (beamer's caption templates).
CAPTION_RE = re.compile(r"^\S+\.?(\s+[\dIVXivx]+(\.\d+)*)?\s*[:.](\s|$)")
MATH_OPERATORS = set("=+−<>≤≥×·/∑∏∫∈∉⊂⊆∪∩→←⇒⇔≈≠±∞")
RELATIONS = set("=<>≤≥≈≠≡∼≃≅∝⇒⇔→")
# Operator names set upright inside formulas (\min, \lim, \log, \operatorname{Var}): words of a
# formula, not of prose around it.
OPERATOR_NAMES = {
    "min", "max", "sup", "inf", "lim", "liminf", "limsup", "log", "ln", "lg", "exp", "sin", "cos", "tan",
    "cot", "sec", "csc", "sinh", "cosh", "tanh", "coth", "arcsin", "arccos", "arctan", "det", "dim", "ker",
    "deg", "arg", "gcd", "lcm", "Pr", "hom", "tr", "Tr", "rank", "diag", "sgn", "sign", "span", "ess", "argmin",
    "argmax", "mod", "Var", "Cov", "Corr", "softmax", "erf", "Re", "Im", "id", "supp", "vol", "dist", "div",
    "grad", "curl", "rot", "Hom", "End", "Aut", "Ker", "KL",
}
DELIMITERS = set("|‖∣∥()[]{}⟨⟩⌊⌋⌈⌉")
# Math extension fonts: big operators, big delimiters, radical signs - glyphs that hang from their
# origin (Computer Modern's CMEX, Latin Modern's LMMathExtension, txfonts/pxfonts/newtx's).
EXTENSION_FONT_RE = re.compile(r"^(CMEX|EUEX|ESINT|(NEW)?(N?TX|PX)EX)|MATHEXTENSION")


def extension_font(font: str) -> bool:
    return bool(EXTENSION_FONT_RE.search(re.sub(r"[^A-Z0-9]", "", font.split("+", 1)[-1].upper())))
DISPLAY_WORD_SHARE = 0.35  # a line with fewer of its characters in words of prose is a formula
# Lone glyphs of bitmap (Type 3) text companion fonts come back as their TS1 code read as
# Latin-1 (the glyph names are /aNNN): \textbullet (metropolis' itemize item under pdflatex
# without cm-super) a control character, \texteuro an inverted question mark. TS1's codes
# 0xA2-0xBE are Latin-1's own characters but for these.
TYPE3_SYMBOLS = {"\x88": "•"}
TS1_SYMBOLS = {
    "\x84": "†", "\x85": "‡", "\x86": "‖", "\x87": "‰", "\x89": "℃", "\x8c": "ƒ", "\x8d": "₡",
    "\x8e": "₩", "\x8f": "₦", "\x90": "₲", "\x91": "₱", "\x92": "₤", "\x93": "℞", "\x94": "‽",
    "\x96": "₫", "\x97": "™", "\x98": "‱", "\x99": "¶", "\x9a": "฿", "\x9b": "№", "\x9d": "℮",
    "\x9e": "◦", "\x9f": "℠", "\xad": "℗", "\xb8": "※", "\xbb": "√", "\xbf": "€",
}


def type3_text_page(spans: list[dict]) -> bool:
    """The page's text itself is in bitmap fonts (T1 without cm-super): Type 3 words. Then a lone
    Type 3 glyph may be a letter of that encoding (T1's 0xBF is £), not a TS1 symbol."""
    return any(s["font"] == "Type3" and sum(c.isalpha() for c in s["text"]) >= 2 for s in spans)


def type3_symbol(text: str, type3_words: bool) -> str:
    """A Type 3 span's text: a lone TS1 glyph as its character (TYPE3_SYMBOLS always, the other
    TS1 symbols where the page's words are not Type 3 themselves, so the lone glyph can only be a
    companion-font symbol)."""
    if text in TYPE3_SYMBOLS:
        return TYPE3_SYMBOLS[text]
    glyph = text.strip()
    if type3_words or glyph not in TS1_SYMBOLS:
        return text
    return text.replace(glyph, TS1_SYMBOLS[glyph])
SMALL_IMAGE_PT = 12
FRAME_RULE_PT = 1.5  # a filled box this thin is a rule (\fcolorbox's \fboxrule, a tcolorbox's frame)
FRAME_REACH = 1.5    # how far a frame's rule may lie from the edge of the box it frames
DECOR_SHORT = 0.12 # em a decoration ends before its span's end for its trailing punctuation to be left out
TRAILING_PUNCT = re.compile(r"(?<=\w)[,.;:!?)\]]+\s*$")
BOX_PAD = " "   # a padded \colorbox's \fboxsep, highlighted (a no-break space: the box never breaks)
MAX_PLAIN_RECTANGLES = 32  # more rectangles in one cluster (a QR code, a pixel grid) are a picture
HOLE_PAD = 1.0  # pt of page around an inline formula picture (antialiasing, italic overhang)


# ---------------------------------------------------------------- geometry

@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def of(cls, values) -> "Rect":
        return cls(*values)

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def expand(self, d: float) -> "Rect":
        return Rect(self.x0 - d, self.y0 - d, self.x1 + d, self.y1 + d)

    def intersects(self, o: "Rect") -> bool:
        return self.x0 < o.x1 and o.x0 < self.x1 and self.y0 < o.y1 and o.y0 < self.y1

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def contains_rect(self, o: "Rect", tol: float = 0.5) -> bool:
        return self.x0 - tol <= o.x0 and self.y0 - tol <= o.y0 and o.x1 <= self.x1 + tol and o.y1 <= self.y1 + tol

    def union(self, o: "Rect") -> "Rect":
        return Rect(min(self.x0, o.x0), min(self.y0, o.y0), max(self.x1, o.x1), max(self.y1, o.y1))

    def distance(self, o: "Rect") -> float:
        dx = max(0.0, o.x0 - self.x1, self.x0 - o.x1)
        dy = max(0.0, o.y0 - self.y1, self.y0 - o.y1)
        return max(dx, dy)

    def as_list(self) -> list[float]:
        return [round(v, 2) for v in (self.x0, self.y0, self.x1, self.y1)]


def overlap(a: Rect, b: Rect) -> float:
    return max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0)) * max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))


def union_all(rects) -> Rect:
    rects = list(rects)
    out = rects[0]
    for r in rects[1:]:
        out = out.union(r)
    return out


def cluster_rects(rects: list[Rect], gap: float) -> list[Rect]:
    """Merge rectangles that come within `gap` of each other, transitively."""
    parent = list(range(len(rects)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if rects[i].expand(gap).intersects(rects[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list[Rect]] = {}
    for i, r in enumerate(rects):
        groups.setdefault(find(i), []).append(r)
    return [union_all(g) for g in groups.values()]


# ---------------------------------------------------------------- text model

@dataclass(eq=False)
class Span:
    id: str
    text: str
    font: str
    size: float
    color: str
    rect: Rect
    baseline: float
    horizontal: bool
    info: FontInfo
    link: str | None = None
    underline: bool = False
    strike: bool = False          # \sout
    highlight: str | None = None  # background colour (\colorbox)
    drawn: bool = False           # a character the PDF draws as a rule, not a glyph (underscores)
    decor_to: float | None = None  # where its underline, strike or highlight ends, when before its end
    pad_left: bool = False         # the first / last word of a padded \colorbox highlight
    pad_right: bool = False
    visual: str | None = None     # the text as the page shows it, left to right (bidi: RTL lines)
    reading: tuple | None = None  # (line, rank, base, gap before) on a right-to-left line (read_lines)


@dataclass(eq=False)
class Line:
    spans: list[Span]
    bullet: dict | None = None
    bullet_spans: list[Span] = field(default_factory=list)
    reason: str | None = None
    inline_math: bool = False
    fractions: list = field(default_factory=list)  # (bar, numerator spans, denominator spans)
    tab: Span | None = None  # content after a line label ("4:") starts here, reached by a tab
    holes: list = field(default_factory=list)  # complex inline formulas: pictures over gaps in the text
    hole_pads: list = field(default_factory=list)  # graphics drawn around words of a hole (a circle, a badge)
    limits: list = field(default_factory=list)  # lines of the limits of a big operator in a hole (∑ with n=1 and ∞)
    code_number: bool = False  # a listing's line number (split_line_numbers): a paragraph of its own
    def hole_rect(self, hole: list) -> "Rect":
        """A hole's extent: its glyphs and the graphics drawn around them."""
        rect = union_all(s.rect for s in hole)
        return union_all([rect] + [g for g in self.hole_pads if g.intersects(rect.expand(0.5))])

    def add_holes(self, groups: list[list]) -> None:
        """Merge new holes with the line's: overlapping holes become one, and a word lying over
        or under a hole (a wavy underline's glyphs below it) or kerned into it (the "TEX" of the
        LaTeX logo) joins it."""
        holes = [list(h) for h in self.holes + groups]
        off_baseline = lambda s: abs(s.baseline - self.baseline) > 0.1 * self.size
        for h in holes:
            grown = True
            while grown:
                r = self.hole_rect(h)
                overlap_x = lambda s: min(s.rect.x1, r.x1) - max(s.rect.x0, r.x0)
                # (a letter raised or lowered into its neighbour, not an italic overhang)
                more = [s for s in self.content if s not in h and s.text.strip() and s.rect.w > 0
                        and (overlap_x(s) >= 0.5 * s.rect.w or
                             (overlap_x(s) >= min(1.0, 0.5 * s.rect.w) and (off_baseline(s) or any(map(off_baseline, h)))))]
                h += more
                grown = bool(more)
        def side_by_side(a: list, b: list) -> bool:
            """Two holes with no word between them (\\uwave{all benchmarks}: a picture per word,
            kerned together): Slides' text has one gap there, as wide as both, and each picture
            was measured into the same gap, one over the other. They are one hole."""
            ra, rb = sorted((self.hole_rect(a), self.hole_rect(b)), key=lambda r: r.x0)
            if rb.x0 - ra.x1 > 0.5 * self.size:
                return False
            return not any(s not in a and s not in b and s.text.strip() and ra.x1 - 0.5 < s.rect.cx < rb.x0 + 0.5
                           for s in self.content)

        merged = True
        while merged:
            merged = False
            for i, a in enumerate(holes):
                for b in holes[i + 1:]:
                    if set(map(id, a)) & set(map(id, b)) or self.hole_rect(a).intersects(self.hole_rect(b)) \
                            or side_by_side(a, b):
                        a += [s for s in b if s not in a]
                        holes.remove(b)
                        merged = True
                        break
                if merged:
                    break
        self.holes = [sorted(h, key=lambda s: s.rect.x0) for h in holes]

    def __post_init__(self):
        self.spans.sort(key=lambda s: s.rect.x0)
        # An accent reaching left of its letter follows the letter (it becomes a combining mark).
        for i in range(len(self.spans) - 1):
            a, b = self.spans[i], self.spans[i + 1]
            if a.text.strip() in ACCENTS and b.text.strip() not in ACCENTS and \
                    min(a.rect.x1, b.rect.x1) - max(a.rect.x0, b.rect.x0) > 0.5 * a.rect.w:
                self.spans[i], self.spans[i + 1] = b, a

    @property
    def rect(self) -> Rect:
        return union_all(s.rect for s in self.spans)

    @property
    def main(self) -> Span:
        top = max(s.size for s in self.spans)
        big = [s for s in self.spans if s.size >= 0.9 * top]
        # (not a big-operator or brace glyph: it sits off the baseline)
        main = max(big, key=lambda s: (not extension_font(s.font), len(s.text.strip())))
        # A span's baseline is its first glyph's: a listing's lowered '*' or raised '_' opening
        # the longest span ("*)NULL);", "_exit(127);") put the whole line 2 pt off, and
        # Slides set two code lines almost touching (r2_code_v3 s2). The line's baseline is the
        # one most of its letters stand on.
        letters = lambda ss: sum(len(s.text.strip()) for s in ss)
        on = lambda b: [s for s in big if abs(s.baseline - b) <= 0.05 * top]
        if 2 * letters(on(main.baseline)) >= letters(big):
            return main
        return max(big, key=lambda s: (not extension_font(s.font), letters(on(s.baseline)), len(s.text.strip())))

    @property
    def baseline(self) -> float:
        return self.main.baseline

    @property
    def size(self) -> float:
        return self.main.size

    @property
    def content(self) -> list[Span]:
        return [s for s in self.spans if s not in self.bullet_spans]

    @property
    def x0(self) -> float:
        return min(s.rect.x0 for s in self.content)

    @property
    def x1(self) -> float:
        return max(s.rect.x1 for s in self.content)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in bidi.logical_spans(self.spans))


def reads_rtl(line: Line) -> bool:
    """The line was read right to left (`PageClassifier.read_lines`: its base direction), so it
    starts at its right end - where a Hebrew item's bullet hangs."""
    bases = {s.reading[2] for s in line.spans if s.reading}
    return bases == {bidi.RIGHT}


@dataclass(eq=False)
class Paragraph:
    lines: list[Line]
    align: str = "left"
    reason: str | None = None
    role: str = "body"
    level: int = 0
    indent: float = 0.0  # a first line set in by \parindent: how far right of the others it starts
    justified: bool = False

    @property
    def first(self) -> Line:
        return self.lines[0]

    @property
    def last(self) -> Line:
        return self.lines[-1]

    @property
    def size(self) -> float:
        return self.first.size

    @property
    def bullet(self) -> dict | None:
        return self.first.bullet

    @property
    def direction(self) -> str | None:
        """`rtl` where the paragraph reads right to left, else None - Unicode's P2 over the
        words as they are now read (`bidi`). Slides has to be told: in a paragraph it takes
        for left-to-right, a Hebrew sentence's full stop lands at the wrong end, a bullet
        hangs on the wrong side and the cursor walks the wrong way."""
        bases = {s.reading[2] for s in self.lines[0].spans if s.reading} if self.lines else set()
        if len(bases) == 1:  # the way its first line was read (PageClassifier.read_lines)
            return "rtl" if bases.pop() == bidi.RIGHT else None
        return "rtl" if bidi.reads_rtl(" ".join(l.text for l in self.lines)) else None

    @property
    def x0(self) -> float:
        return self.first.x0

    @property
    def rect(self) -> Rect:
        return union_all(l.rect for l in self.lines)

    @property
    def spans(self) -> list[Span]:
        return [s for l in self.lines for s in l.spans]


# Slides draws a script run at 2/3 of its size (docs/calibration.md: 0.665): a script smaller
# than 2/3 of its line (a script's script, 0.55) is given the size that draws it as small.
SLIDES_SCRIPT = 0.665


def script_of(span: Span, line: "Line") -> str | None:
    """'super' / 'sub' for a smaller span raised / lowered from the line's baseline.

    TeX lowers a subscript 0.15 em (0.25 beside a superscript) and \\textsubscript in a subtitle
    0.11 em; it raises a superscript 0.36-0.53 em, a keycap's small letters 0.09-0.11 em (not a
    script), so a subscript starts at 0.10 em and a superscript at 0.12. listings raises the
    underscore of an inline `_exit` into a span of its own font's words: that is code, not a
    superscript."""
    if span.size >= 0.85 * line.size:
        return None
    shift = span.baseline - line.baseline
    if shift < -0.12 * line.size:
        return None if span.text.lstrip().startswith("_") else "super"
    if shift > 0.10 * line.size:
        return "sub"
    return None


def script_size(span: Span, line: "Line") -> float:
    """The IR size of a script run: its line's, which Slides draws at 2/3 - or larger than the
    span by that factor when the span is smaller still (a subscript of a subscript, 0.5-0.55;
    a script is 0.66-0.73 of its line)."""
    return line.size if span.size >= 0.6 * line.size else span.size / SLIDES_SCRIPT


# A raised ring (^\circ, siunitx's degree) or asterisk (\textsuperscript{*}, x^*) is drawn by the
# text face's own degree sign and asterisk at the line's size where TeX has it: Lato's ° spans
# 0.40-0.73 em and * 0.43-0.75, TeX's raised 7 pt ring and star 0.43-0.74 of a 10 pt line. Set
# as a superscript, Slides shrinks and raises marks that sit high already: a speck.
RAISED_MARKS = {"°": "°", "∘": "°", "◦": "°", "*": "*", "∗": "*"}


def raised_mark(text: str) -> str:
    return "".join(RAISED_MARKS.get(c, c) for c in text)


DOUBLE_STRUCK = {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}
# \mathcal / \mathscr capitals as Unicode's script letters (the Letterlike Symbols ones where
# Unicode has them there), \mathfrak as Fraktur. Slides draws them from its fallback font, as it
# does the double-struck ones: a calligraphic letter, not a plain capital.
SCRIPT = {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ"}
FRAKTUR = {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"}
SCRIPT_FONTS = ("CMSY", "CMBSY", "LMMATHSYMBOLS", "RSFS", "EUSM", "EUSB", "TXSY", "PXSY", "NTXSY")
FRAKTUR_FONTS = ("EUFM", "EUFB")
# Unicode math letters (unicode-math, OpenType math fonts) that are plain letters set italic.
MATH_ITALIC_NAMES = ("MATHEMATICAL ITALIC ", "PLANCK CONSTANT")  # ℎ is the italic h
# Characters no Slides text face has, drawn by a fallback font slanted and ~1 em wide (norm bars
# read as "//"): the upright ASCII bars say the same.
MATH_SUBSTITUTES = {"∥": "||", "‖": "||", "∣": "|"}
NEGATION = "̸"  # TeX's \not: a slash laid over the relation after it (\neq, \not\subseteq)
# Accents TeX sets as glyphs of their own over a letter (\bar{X}, and every accent in the OT1
# encoding, pdflatex's default: Schr¨odinger): combining marks in Slides.
ACCENTS = {"¯": "̄", "ˆ": "̂", "˜": "̃", "˙": "̇", "¨": "̈", "´": "́",
           "`": "̀", "ˇ": "̌", "˘": "̆", "˚": "̊", "˝": "̋", "¸": "̧", "˛": "̨"}
BELOW_ACCENTS = set("¸˛")  # \c{S} is set letter first, then its cedilla (\ooalign)
# Symbols TeX builds from pieces it overlaps (\cong: ∼ over =; \implies, \longrightarrow,
# \xrightarrow and mhchem's arrows: relbars kerned under an arrow by \joinrel): one Unicode
# symbol, since Slides sets the pieces one after the other, apart (−−→, =⇒, ∼=). A pair
# folds left to right, so a chain of relbars of any length ends as one long arrow.
COMPOSED = {("−", "−"): "−", ("=", "="): "=", ("−", "→"): "⟶", ("−", "⟶"): "⟶", ("←", "−"): "⟵",
            ("⟵", "−"): "⟵", ("⟵", "→"): "⟷", ("←", "→"): "⟷", ("←", "⟶"): "⟷",
            ("=", "⇒"): "⟹", ("=", "⟹"): "⟹", ("⇐", "="): "⟸", ("⟸", "="): "⟸", ("⟸", "⇒"): "⟺",
            ("⇐", "⇒"): "⟺", ("⇐", "⟹"): "⟺", ("∼", "="): "≅", ("=", "∼"): "≅"}


def with_accent(letter: str, mark: str) -> str:
    """A letter with a combining mark, precomposed where Unicode has the pair (e + ´ -> é). An
    accent over a dotless i or j (OT1's \\'{\\i}) is over an i or j."""
    if unicodedata.combining(mark) == 230 and letter in "ıȷ":  # a mark above
        letter = "ij"["ıȷ".index(letter)]
    return unicodedata.normalize("NFC", letter + mark)


def accent_beside(accent: "Span", base: "Span") -> bool:
    """True if `accent` is a TeX accent set over a letter of `base` that Slides would set it
    beside: a letter outside the Latin script with no precomposed form with that mark (\\hat\\beta,
    \\tilde\\mu). The combining mark comes after the letter, and no Slides face anchors a mark on
    a Greek letter, so the hat stands to its right, over the next word (r1_econ_v4 s1); such a
    letter is a formula hole, its picture the page's. (A Latin letter keeps its mark: its faces
    place one, and x̄ is a precomposed ȳ's neighbour.)"""
    mark = ACCENTS.get(accent.text.strip())
    if not mark or not (base.rect.x0 < accent.rect.x1 - 0.2 and accent.rect.x0 < base.rect.x1 - 0.2):
        return False
    text = base.text
    if not text.strip():
        return False
    # the letter under the accent's middle (the span's advance shared out evenly)
    k = int((accent.rect.cx - base.rect.x0) / max(base.rect.w, 1e-6) * len(text))
    letter = text[min(max(k, 0), len(text) - 1)]
    return letter.isalpha() and not unicodedata.name(letter, "LATIN").startswith("LATIN") \
        and letter not in "ıȷ" and len(unicodedata.normalize("NFC", letter + mark)) > 1


def compose_accents(text: str, mono: bool = False) -> str:
    """Spacing accents built with their letter (OT1: accent glyph, then the letter under it;
    a cedilla after a tall letter) as the accented letter: "Schr¨odinger" -> "Schrödinger",
    "Garc´ıa" -> "García", "S¸." -> "Ş.". In a monospaced face ` is a backquote, not an accent."""
    if not any(c in ACCENTS for c in text):
        return text
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c in ACCENTS and not (mono and c == "`"):
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if nxt.isalpha():
                out.append(with_accent(nxt, ACCENTS[c]))
                i += 2
                continue
            if c in BELOW_ACCENTS and out and out[-1][-1:].isalpha():
                out[-1] = with_accent(out[-1], ACCENTS[c])
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def negate(text: str) -> str:
    """TeX's \\not before its relation as the negated relation: " ̸ =" -> " ≠", "̸⊆" -> "⊈" (the
    relation with a combining long solidus where Unicode has no precomposed one)."""
    if NEGATION not in text:
        return text
    return re.sub(NEGATION + r"\s*(\S)", lambda m: unicodedata.normalize("NFC", m.group(1) + NEGATION), text)


def compose_symbols(text: str) -> str:
    """The pieces of a composed symbol set in one math font as the symbol: "−−→" -> "⟶",
    "⇐⇒" -> "⟺" (two relation pieces never stand side by side unspaced in TeX math unless
    \\joinrel overlapped them). Pieces in two fonts (CMR's =, CMSY's ⇒) are joined where they
    overlap on the page (`PageClassifier` line runs)."""
    out = ""
    for c in text:
        out = out[:-1] + COMPOSED[(out[-1], c)] if out and (out[-1], c) in COMPOSED else out + c
    return out


def math_pieces(font: str, text: str) -> list[tuple[str, bool]]:
    """Unicode text of a span set in a math font, as pieces with their italic flag. A TeX math
    italic font (CMMI, Latin Modern's LMMathItalic, newtx's NewTXMI...) makes the span italic
    where it has letters; an OpenType math font's span mixes italic letters (𝑥, 𝜆: plain
    letters set italic) with upright operators and digits, piece by piece."""
    key = re.sub(r"[^A-Z0-9]", "", font.split("+", 1)[-1].upper())
    text = compose_symbols(negate(text))
    if key.startswith("MSBM"):  # \mathbb
        return [("".join(DOUBLE_STRUCK.get(c, chr(0x1D538 + ord(c) - 65) if "A" <= c <= "Z" else c)
                         for c in text), False)]
    if key.startswith(SCRIPT_FONTS):  # \mathcal, \mathscr
        text = "".join(SCRIPT.get(c, chr(0x1D49C + ord(c) - 65)) if "A" <= c <= "Z" else c for c in text)
    elif key.startswith(FRAKTUR_FONTS):  # \mathfrak
        text = "".join(FRAKTUR.get(c, chr(0x1D504 + ord(c) - 65)) if "A" <= c <= "Z" else
                       chr(0x1D51E + ord(c) - 97) if "a" <= c <= "z" else c for c in text)
    text = "".join(MATH_SUBSTITUTES.get(c, c) for c in text)
    if MATH_ITALIC_RE.search(key):
        return [(text, any(c.isalpha() for c in text))]
    pieces: list[tuple[str, bool]] = []
    for c in text:
        italic = unicodedata.name(c, "").startswith(MATH_ITALIC_NAMES)
        if italic:  # 𝜆 -> λ, 𝜕 -> ∂, 𝜙 -> ϕ
            c = unicodedata.normalize("NFKC", c)
        if pieces and (pieces[-1][1] == italic or c.isspace() or unicodedata.combining(c)):
            pieces[-1] = (pieces[-1][0] + c, pieces[-1][1])
        elif pieces and not pieces[-1][0].strip():  # the span's leading space goes with its first piece
            pieces[-1] = (pieces[-1][0] + c, italic)
        else:
            pieces.append((c, italic))
    return pieces or [("", False)]


def math_text(font: str, text: str) -> tuple[str, bool]:
    """Unicode text and italic flag for a span set in a math font (italic: any of it is)."""
    pieces = math_pieces(font, text)
    return "".join(t for t, _ in pieces), any(i for _, i in pieces)


def math_family(line: "Line", par: "Paragraph | None" = None) -> str:
    """The text family math is shown in: the family most of the words around it are set in -
    on its line, else in its paragraph - never a monospaced one (a formula after \\texttt{x} is
    not code), and serif when there are no words (TeX's math is Computer Modern's)."""
    for spans in ([line.content], [l.content for l in par.lines] if par else []):
        weight = Counter()
        for s in (s for group in spans for s in group):
            if s.info.family in ("sans", "serif"):
                weight[s.info.family] += len(s.text.strip())
        if weight:
            return weight.most_common(1)[0][0]
    return "serif"


FRACTION_SLASH = "⁄"


def reading_order(line: "Line") -> list[tuple]:
    """The line's content spans in reading order - left to right, or right to left where that is
    how the line reads (`bidi.logical_spans`) - except that each simple fraction becomes
    numerator (superscript), fraction slash, denominator (subscript)."""
    owner = {id(s): f for f in line.fractions for s in f[1] + f[2]}
    out, emitted = [], set()
    for s in bidi.logical_spans(line.content):
        f = owner.get(id(s))
        if f is None:
            out.append((s, None))
        elif id(f) not in emitted:
            emitted.add(id(f))
            out += [(x, "super") for x in f[1]] + [(FRACTION_SLASH, None)] + [(x, "sub") for x in f[2]]
    return out


def gap_between(a: "Span", b: "Span") -> float:
    """The room between two neighbours on a line, whichever of them the page draws first: on a
    right-to-left line the next span stands to the *left* of the one before it. Two spans read
    one after the other on such a line need not stand side by side (a formula's first number
    and the full stop after it, at the line's left end): the room between them is the word space
    read between them, or none (`PageClassifier.read_lines`)."""
    if a.reading and b.reading and a.reading[0] == b.reading[0] and b.reading[1] == a.reading[1] + 1:
        return b.reading[3]
    return max(b.rect.x0 - a.rect.x1, a.rect.x0 - b.rect.x1)


def span_runs(spans: list[Span]) -> list[dict]:
    """Runs for a short piece of text given as spans left to right (cells, node labels), put into
    reading order first. Simple math works as in text lines: symbols from math fonts,
    sub/superscripts."""
    runs: list[dict] = []
    main = max(spans, key=lambda s: s.size) if spans else None
    base_family = next((s.info.family for s in spans if s.info.family not in ("math", "icon")), "sans")
    spans = bidi.logical_spans(spans)
    lead = bidi.lead_mark(spans)
    for i, s in enumerate(spans):
        text = s.text
        if i and gap_between(spans[i - 1], s) > 0.15 * s.size and not text.startswith(" "):
            if runs and not runs[-1]["script"] and s.info.family == "mono" and spans[i - 1].info.family != "mono" \
                    and runs[-1]["family"] != "mono" and not runs[-1]["text"].endswith(" "):
                runs[-1]["text"] += " "  # the word space before \texttt is the prose's, not a monospaced one
            else:
                text = " " + text
        family, italic, script = s.info.family, s.info.italic, None
        pieces = [(text, italic)]
        if family == "math":
            family = base_family
            pieces = math_pieces(s.font, text)
        script = script_of(s, main)  # (the largest span stands for the line)
        size = script_size(s, main) if script else s.size
        if script == "super" and text.strip() in RAISED_MARKS:
            pieces, script, size = [(raised_mark(text), False)], None, main.size
        for text, italic in pieces:
            style = {"font": s.font, "family": family, "size": round(size, 2),
                     "bold": s.info.bold, "italic": italic, "smallcaps": s.info.smallcaps, "color": s.color,
                     "link": s.link, "script": script, "underline": s.underline, "strike": s.strike, "highlight": s.highlight}
            if runs and all(runs[-1][k] == v for k, v in style.items()):
                runs[-1]["text"] += text
            else:
                runs.append({"text": text, **style})
    prose_spaces(runs)
    runs = [r for r in runs if r["text"]]
    if lead and runs:  # (a right-to-left cell starting with a Latin word says which way it reads)
        runs[0]["text"] = lead + runs[0]["text"]
    return runs


def prose_spaces(runs: list[dict]) -> None:
    """A word space at the edge of inline code belongs to the surrounding text: in a monospaced
    font it would be twice as wide. (In a table cell too: the span ' __exit__' comes with its
    space, 'paired with  __exit__', r2_code_v4 s5.)"""
    for a, b in zip(runs, runs[1:]):
        if b["family"] == "mono" and a["family"] != "mono" and b["text"].startswith(" ") and not a["script"]:
            if not a["text"].endswith(" "):
                a["text"] += " "
            b["text"] = b["text"][1:]
        elif a["family"] == "mono" and b["family"] != "mono" and a["text"].endswith(" ") and not b["script"]:
            a["text"] = a["text"][:-1]
            if not b["text"].startswith(" "):
                b["text"] = " " + b["text"]


def cell_runs(lines: list[list[Span]]) -> tuple[list[dict], list[int]]:
    """Runs of a table cell given as its lines of spans (a paragraph column wraps): one paragraph,
    the lines joined by a space, or by nothing where TeX hyphenated a word at the line's end.
    And where in the runs' text each line after the first starts."""
    runs: list[dict] = []
    starts = []
    for spans in lines:
        more = span_runs(spans)
        if runs and more:
            tail, head = runs[-1]["text"], more[0]["text"].lstrip()
            if len(tail) >= 2 and tail.endswith("-") and tail[-2].isalpha() and head[:1].islower():
                runs[-1] = {**runs[-1], "text": tail[:-1]}
                more[0] = {**more[0], "text": head}
            else:
                more[0] = {**more[0], "text": " " + head}
            starts.append(sum(len(r["text"]) for r in runs) + len(more[0]["text"]) - len(head))
            if all(runs[-1][k] == v for k, v in more[0].items() if k != "text"):
                runs[-1] = {**runs[-1], "text": runs[-1]["text"] + more[0]["text"]}
                more = more[1:]
        runs += more
    return runs, starts


def polygon_shape(points: list, r: "Rect") -> str | None:
    """Slides shape for a closed polygon path: a diamond touching the middle of each side of its
    bounding box, or a triangle with its apex centred on the top or bottom side."""
    tol = 0.08 * max(r.w, r.h)
    def near(p, x, y):
        return abs(p[0] - x) <= tol and abs(p[1] - y) <= tol
    corners = {(round(p[0], 1), round(p[1], 1)) for p in points}
    mids = [(r.cx, r.y0), (r.x1, r.cy), (r.cx, r.y1), (r.x0, r.cy)]
    if len(corners) == 4 and all(any(near(p, *m) for p in corners) for m in mids):
        return "DIAMOND"
    if len(corners) == 3:
        if any(near(p, r.cx, r.y0) for p in corners) and any(near(p, r.x0, r.y1) for p in corners) \
                and any(near(p, r.x1, r.y1) for p in corners):
            return "TRIANGLE"
    return None


MITER_LIMIT = 10.0  # PDF's default, and TikZ's


def miter_reach(points: list, direction: tuple[float, float], width: float) -> float:
    """How far an arrow head's outline, stroked `width` wide with mitred joins, reaches past the
    point of its path furthest along `direction`: half the width over the sine of half the angle
    at that point (a bevel's half width beyond the miter limit)."""
    if width <= 0 or len(points) < 3:
        return 0.0
    ux, uy = direction
    along = lambda p: (p[0] * ux + p[1] * uy)
    corners = [tuple(p) for k, p in enumerate(points) if k == 0 or math.dist(p, points[k - 1]) > 1e-6]
    if len(corners) > 2 and math.dist(corners[0], corners[-1]) <= 1e-6:
        corners.pop()
    if len(corners) < 3:
        return 0.0
    k = max(range(len(corners)), key=lambda i: along(corners[i]))
    apex, a, b = corners[k], corners[k - 1], corners[(k + 1) % len(corners)]
    va, vb = (a[0] - apex[0], a[1] - apex[1]), (b[0] - apex[0], b[1] - apex[1])
    na, nb = math.hypot(*va), math.hypot(*vb)
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    cos = max(-1.0, min(1.0, (va[0] * vb[0] + va[1] * vb[1]) / (na * nb)))
    half = math.acos(cos) / 2
    if half < 1e-3 or 1 / math.sin(half) > MITER_LIMIT:
        return width / 2
    return width / 2 / math.sin(half)


def upright_ellipse(path: list, r: Rect) -> bool:
    """Four curves closing an ellipse whose axes are the box's: they join end to start, and
    meet the box at the middle of each side. A sine wave is four curves too (TikZ's sin cos
    sin cos), and a rotated or sheared ellipse touches its box elsewhere: as an ELLIPSE of the
    box they came out a closed upright ring."""
    if [op for op, _ in path] != ["c"] * 4:
        return False
    tol = 0.05 * max(r.w, r.h) + 0.1
    ends = [pts[-1] for _, pts in path]
    if any(math.dist(pts[0], prev) > tol for (_, pts), prev in zip(path, ends[-1:] + ends[:-1])):
        return False  # open (a wave), or pieces of different outlines
    mids = [(r.cx, r.y0), (r.x1, r.cy), (r.cx, r.y1), (r.x0, r.cy)]
    return all(any(math.dist(e, m) <= tol for e in ends) for m in mids)


def box_outline(d: dict, r: Rect) -> bool:
    """A path that outlines its own bounding box: one rectangle, or one outline of axis-aligned
    edges with rounded corners (a beamer block). A panel is rebuilt as a shape of its box, so
    a funnel's trapezium, a band between two curves or a bar series (one path, a rectangle per
    bar) would turn into one big rectangle."""
    path = d.get("path")
    if not path:
        return False
    if [op for op, _ in path] == ["re"]:
        return True
    ops = {op for op, _ in path}
    if not ops <= {"l", "c"} or "l" not in ops:
        return False
    tol = max(0.1, 0.01 * max(r.w, r.h))
    on_border = lambda x, y: min(abs(x - r.x0), abs(x - r.x1)) <= tol or min(abs(y - r.y0), abs(y - r.y1)) <= tol
    end = None
    for op, pts in path:
        if end is not None and math.dist(pts[0], end) > tol:
            return False  # a second outline (another bar)
        if not all(on_border(x, y) for x, y in pts):
            return False
        if op == "l" and abs(pts[0][0] - pts[-1][0]) > tol and abs(pts[0][1] - pts[-1][1]) > tol:
            return False  # a slanted edge
        end = pts[-1]
    return True


def label_of(spans: list[Span]) -> dict | None:
    """Where and how a list number is drawn, to write it as literal text if Slides can't number it."""
    if not spans:
        return None
    s = min(spans, key=lambda s: s.rect.x0)
    return {"x0": round(s.rect.x0, 2), "baseline": round(s.baseline, 2), "font": s.font, "family": s.info.family, "size": round(s.size, 2),
            "bold": s.info.bold, "italic": s.info.italic, "color": s.color}


OUTLINE_MIN = 0.3  # pt: a stroke this wide around a filled bullet shows as an outline


def bullet_shape(d: dict | None) -> dict:
    """Shape and colour of a bullet drawn as a path (emit picks the Slides glyph): a filled
    rectangle is a square, curves are a disc (a circle when only stroked), three corners a
    triangle. A filled mark outlined in another colour (a legend's swatch) has no Slides glyph:
    none (it becomes a picture beside its text, r3_charts_v1 s9)."""
    if not d:
        return {}
    filled = d["type"] in ("f", "fs") and d.get("fill")
    if filled and d["type"] == "fs" and d.get("stroke") and d["stroke"] != d["fill"] and \
            (d.get("width") or 0.0) >= OUTLINE_MIN and (d.get("stroke_opacity") or 0.0) > 0.5:
        return {}
    ops = set(d["items"])
    points = {(round(x, 1), round(y, 1)) for op, pts in d.get("path", []) for x, y in pts}
    if ops <= {"r", "e", "q", "u"}:
        shape = "square" if filled else "open_square"
    elif "c" in ops:
        shape = "disc" if filled else "circle"
    elif ops == {"l"} and len(points) == 3:
        shape = "triangle"
    else:
        return {}
    return {"shape": shape, "color": d["fill"] if filled else d.get("stroke") or "#000000"}


WORD_RE = re.compile(r"[^\W\d_]{2,}")


def prose_share(spans: list[Span]) -> float:
    """Share of the characters that are words of prose: runs of two or more letters in a text
    face, operator names (\\min, \\log) not counted. A display formula has few (a \\text{for all},
    "eigenvalues of" in a set), a line of prose with formulas in it mostly these."""
    total = sum(len(s.text.replace(" ", "")) for s in spans)
    # (letters of a word may come as spans of their own: right-to-left text, letterspacing)
    text, prev = "", None
    for s in sorted(spans, key=lambda s: s.rect.x0):
        if s.info.family in ("math", "icon"):
            text += " "
        else:
            text += ("" if prev is not None and s.rect.x0 - prev.rect.x1 <= 0.1 * s.size else " ") + s.text
        prev = s
    # (a name right before its parenthesis is a function's, \operatorname{SSIM}(I_t, ...))
    words = sum(len(m.group()) for m in WORD_RE.finditer(text)
                if m.group() not in OPERATOR_NAMES and text[m.end():m.end() + 1] != "(")
    return words / total if total else 0.0


def math_content(line: "Line") -> list[Span]:
    """The spans math analysis looks at: a hanging label before a tab ("a)" on a ball) is not math."""
    if line.tab is None:
        return line.content
    return [s for s in line.content if s.rect.x0 >= line.tab.rect.x0 - 0.1]


# Relative glyph widths (Helvetica, per mille) to share a span's width out among its words.
_WIDTHS = dict(zip("abcdefghijklmnopqrstuvwxyz", (556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833,
                                                  556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500)))
_WIDTHS.update(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833,
                                                  722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611)))
_WIDTHS.update({" ": 278, ".": 278, ",": 278, ":": 278, ";": 278, "'": 191, "’": 222, "-": 333, "(": 333, ")": 333,
                "!": 278, "|": 260, "/": 278})


def cjk(ch: str) -> bool:
    """A character of Chinese or Japanese text (ideograph, kana, CJK or fullwidth punctuation):
    a line breaks between any two of them and joins with no space. (Korean breaks at spaces.)"""
    from .scripts import script_of
    return bool(ch) and script_of(ch) in ("han", "kana")


def first_word_width(span: Span) -> float:
    """Width of a span's first word: a span can hold one word or a whole line of them. In Chinese
    or Japanese a line can break after any character, so a word there is one character."""
    text = span.text.strip()
    if not text:
        return span.rect.w
    weight = lambda t: sum(1000 if cjk(c) else _WIDTHS.get(c, 556) for c in t)
    word = text.split()[0]
    cut = next((i for i, c in enumerate(word) if cjk(c)), None)
    if cut is not None:
        word = word[:max(cut, 1)]  # up to the first ideograph, or that one alone
    return span.rect.w * weight(word) / weight(text)


def line_word_width(line: "Line") -> float:
    """Width of a line's first word, across the spans it is set in: a small-caps word is its
    capital in one span and its small letters in the next ('G' + 'oldbach', r2_fonts_pazo s5),
    and the capital alone looked short enough to have ended the line above - the paragraph was
    cut in two there (V-fonts-8)."""
    spans = line.content
    head = spans[0]
    x1 = head.rect.x0 + first_word_width(head)
    for a, b in zip(spans, spans[1:]):
        if len(a.text.split()) != 1 or a.text != a.text.rstrip() or b.text[:1].isspace() or \
                b.rect.x0 - a.rect.x1 > 0.1 * line.size or cjk(a.text[-1:]):
            break
        x1 = b.rect.x0 + first_word_width(b)
    return x1 - head.rect.x0


def last_word_width(span: Span) -> float:
    """Width of a span's last word (first_word_width's other end)."""
    text = span.text.strip()
    if not text:
        return span.rect.w
    weight = lambda t: sum(1000 if cjk(c) else _WIDTHS.get(c, 556) for c in t)
    return span.rect.w * weight(text.split()[-1]) / weight(text)


def line_starts(lines: list["Line"], runs: list[dict]) -> list[int] | None:
    """Where each of a paragraph's lines after the first starts in its runs' joined text (at the
    word after a space), then that text's length - or None where a line's first word is not found
    there (a hyphenated or CJK line end, a hole, a glyph read another way). emit sets each PDF line's
    words as Slides will to size a wrapped paragraph's box (`emit.slides_lines`); where TeX's
    widths are not known (Palatino, a formula's letters without CM metrics) it could not find the
    lines from their extents, the box came from the PDF's and Slides' narrower substitute pulled a
    word up ('ω.' onto line 1, V-control-16; 'Goldbach', V-fonts-8). The length says the text is
    still the one the starts count in."""
    text = "".join(r["text"] for r in runs)
    if len(lines) < 2 or chr(11) in text or "\t" in text:
        return None
    starts, at = [], 0
    for above, line in zip(lines, lines[1:]):
        first = next((s for s, _ in reading_order(line) if isinstance(s, Span) and s.text.strip()), None)
        if first is None:
            return None
        word = first.text.split()[0]
        heads = {word, unicodedata.normalize("NFC", word), unicodedata.normalize("NFKC", word)}
        # (about where it should be: after the line above's words, a space between spans)
        guess = at + len(" ".join(s.text.strip() for s in above.content if s.text.strip())) + 1
        found = [i for i in range(at + 1, len(text)) if text[i - 1] == " " and any(text.startswith(h, i) for h in heads)]
        if not found:
            return None
        at = min(found, key=lambda i: abs(i - guess))
        starts.append(at)
    return starts + [len(text)]


def line_spaces(line: "Line") -> dict[str, float]:
    """The word space of each text font on a line (em): the lower median of the gaps between
    neighbouring spans of that font (font_gaps). TeX gives every interword glue of one font on a
    line one width; the gaps that differ are not word spaces of that glue - a hanging label's
    (0.6 em), a gap across a formula or before a relation (0.8 em) - and are wider, so the lower
    median is the line's space where a mean was pulled up by one of them."""
    out = {}
    for font in dict.fromkeys(s.font for s in line.content if s.text.strip() and s.info.family != "math"):
        gaps = sorted(font_gaps(line, font))
        if gaps:
            out[font] = gaps[(len(gaps) - 1) // 2]
    return out


def stretched(lines: list["Line"]) -> bool:
    """Were these lines' word spaces stretched or shrunk to fill the measure? Justified lines each
    get spaces of their own width; ragged lines (and a justified paragraph's last line) keep
    the font's natural space, the same on every line to a hundredth of an em. Spaces are compared
    font by font (`line_spaces`): a bold or italic face has a wider space of its own (a bold
    heading line over a regular one, a reference's italic title over its roman venue), and a
    line's mean was pulled up by a label's or a formula's gap (visual hunt r7: 13 ragged
    paragraphs, items and references set JUSTIFIED)."""
    spaces = [line_spaces(l) for l in lines]
    for font in {f for s in spaces for f in s}:
        widths = [s[font] for s in spaces if font in s]
        if len(widths) >= 2 and max(widths) - min(widths) > 0.02:
            return True
    # A line held in one span of several words had its spaces under extract's 0.3 em split:
    # beside a line of that font whose words stand at least that far apart, it was shrunk.
    fonts = Counter(s.font for l in lines for s in l.content if s.text.strip() and s.info.family != "math")
    if not fonts or not any(spaces):
        return False
    font = fonts.most_common(1)[0][0]
    # (not a quantity's thin spaces, see thin_span)
    whole = lambda l: not font_gaps(l, font) and any(
        s.font == font and s.text.strip().count(" ") >= 3 and not any(c.isdigit() for c in s.text) for s in l.content)
    return any(map(whole, lines)) and any(len(font_gaps(l, font)) >= 2 for l in lines)


def justified_cells(cells: list[list[list[Span]]], right: float) -> bool:
    """Are a column's wrapped cells (each a list of lines, each line its spans) set justified?
    Every line but a cell's last ends at the column's right edge `right`, and the lines' word
    spaces differ (stretched, `stretched`): ragged lines keep the font's natural space. A
    tabularx X column is justified, and written ragged it read as another table."""
    ends = [max(s.rect.x1 for s in line if s.text.strip()) for lines in cells for line in lines[:-1]
            if any(s.text.strip() for s in line)]
    if not ends or any(abs(e - right) > 0.75 for e in ends):
        return False

    def gaps(line: list[Span]) -> list[float]:
        words = sorted((s for s in line if s.text.strip() and s.info.family != "math"), key=lambda s: s.rect.x0)
        if len(words) < 2:
            return []
        size = max(s.size for s in words)
        font = Counter(s.font for s in words).most_common(1)[0][0]
        return [g for a, b in zip(words, words[1:]) if a.font == b.font == font and abs(a.size - size) <= 0.5
                and a.text.rstrip()[-1:] not in ".?!:;)”’\"'" for g in [(b.rect.x0 - a.rect.x1) / size] if 0.1 <= g <= 0.9]
    means = [sum(g) / len(g) for g in (gaps(line) for lines in cells for line in lines) if g]
    return len(means) >= 2 and max(means) - min(means) > 0.02


def font_gaps(line: "Line", font: str) -> list[float]:
    """Word spaces (em) between neighbouring spans of `line` both in `font` (not after a
    sentence's end or a colon, where TeX widens them)."""
    words = [s for s in line.content if s.text.strip()]
    return [g for a, b in zip(words, words[1:]) if a.font == b.font == font and abs(a.size - line.size) <= 0.5
            and a.text.rstrip()[-1:] not in ".?!:;)”’\"'" and b is not line.tab  # (a label's gap)
            for g in [(b.rect.x0 - a.rect.x1) / line.size] if 0.1 <= g <= 0.9]


def thin_span(span: Span, line: "Line", lines: list["Line"]) -> bool:
    """Are the spaces inside this span thin ones - TeX's \\, (siunitx between a number's digit
    groups and before its unit), kerns no line breaks at? Extract splits spans at gaps of
    0.3 em, and TeX sets every word space of a line in one font alike: where the span's font
    stands its words that far apart on this line (or on the paragraph's other lines when they
    are ragged, with natural spaces), a space kept inside the span is narrower than a word
    space. (A span holding a whole shrunk line, or phrases of a font with a narrow space such
    as Cambria, has no such neighbours and is not thin.)"""
    text = span.text.strip()
    if not 1 <= text.count(" ") <= 3 or not any(c.isdigit() for c in text) or span.info.family in ("math", "mono"):
        return False  # (only a quantity's: other thin spaces are rare and line breaks at them harmless)
    gaps = font_gaps(line, span.font)
    if len(gaps) < 2 and not stretched(lines):
        gaps = [g for l in lines for g in font_gaps(l, span.font)]
    return len(gaps) >= 2 and min(gaps) >= 0.31


# Words that open compounds ("self-reported", "well-known") and that TeX's patterns do not split
# off a longer word: a line ending on one of them and a hyphen broke at the compound's own hyphen.
COMPOUND_HEADS = {"self", "well", "non", "cross", "half", "state", "peer", "user", "world", "quasi", "pseudo", "real"}


def explicit_hyphen(tail: str) -> bool:
    """Does the hyphen at the end of `tail` (a line's text) belong to the word - a compound broken
    at its own hyphen - rather than being TeX's hyphenation? TeX hyphenates no word that already
    holds a hyphen ("state-of-the-" + "art"), and some compound heads are words of their own."""
    word = tail.rstrip("-").split()[-1] if tail.rstrip("-").split() else ""
    return "-" in word or word.lstrip("([{“‘\"'").casefold() in COMPOUND_HEADS


def card_text(node: Rect, rows: list[list[Span]]) -> dict | None:
    """The text on a node that is more than a centred label (a card: a big number over a
    caption, a heading over wrapped body copy) as a text element placed on its baselines, or
    None for a simple label (one size, centred on the node)."""
    if not rows:
        return None
    rows = [sorted(row, key=lambda s: s.rect.x0) for row in rows]
    info = [{"x0": row[0].rect.x0, "x1": row[-1].rect.x1, "baseline": row[0].baseline,
             "size": max(s.size for s in row)} for row in rows]
    sizes = [r["size"] for r in info]
    top, bottom = rows[0][0].rect.y0, max(s.rect.y1 for s in rows[-1])
    # (lines of a justified paragraph start together and end apart, even when nearly centred)
    flush_left = lambda ls: len(ls) > 1 and all(abs(l["x0"] - ls[0]["x0"]) <= 0.5 for l in ls) and \
        any(abs(l["x1"] - ls[0]["x1"]) > 2 for l in ls)
    centred = all(abs((r["x0"] + r["x1"]) / 2 - node.cx) <= 2 for r in info) and not flush_left(info)
    if max(sizes) <= 1.1 * min(sizes) and centred and abs((top + bottom) / 2 - node.cy) <= 0.15 * node.h:
        return None
    paragraphs: list[list[int]] = []
    for i, r in enumerate(info):
        if paragraphs:
            p = info[paragraphs[-1][-1]]
            same_size = abs(p["size"] - r["size"]) <= 0.05 * r["size"]
            pitch = r["baseline"] - p["baseline"]
            aligned = abs(p["x0"] - r["x0"]) <= 1 or abs((p["x0"] + p["x1"] - r["x0"] - r["x1"]) / 2) <= 1.5
            if same_size and aligned and 0.9 * r["size"] <= pitch <= 1.6 * r["size"]:
                paragraphs[-1].append(i)
                continue
        paragraphs.append([i])
    boxes: list[list[dict]] = []
    for idx in paragraphs:
        lines = [info[i] for i in idx]
        on_centre = all(abs((l["x0"] + l["x1"]) / 2 - node.cx) <= 2 for l in lines) and not flush_left(lines)
        left = not on_centre and (len(lines) == 1 or all(abs(l["x0"] - lines[0]["x0"]) <= 1 for l in lines))
        runs: list[dict] = []
        for i in idx:
            row_runs = span_runs(rows[i])
            if runs and row_runs:
                # a centred caption keeps its breaks (see text_element.unbalanced)
                runs[-1] = {**runs[-1], "text": runs[-1]["text"].rstrip() + (" " if left else chr(11))}
            runs += row_runs
        par = {
            "align": "left" if left else "center", "level": 0, "bullet": None, "size": round(lines[0]["size"], 2),
            "text_x0": round(min(l["x0"] for l in lines), 2), "tab_x0": None,
            "lines": [{"baseline": round(l["baseline"], 2), "x0": round(l["x0"], 2), "x1": round(l["x1"], 2)} for l in lines],
            "wrap_limit": round(min(l["x0"] for l in lines) + min(
                a["x1"] - a["x0"] + 0.25 * a["size"] + first_word_width(rows[i + 1][0])
                for a, i in zip(lines[:-1], idx[:-1])), 2) if len(lines) > 1 and left else None,
            "runs": runs,
        }
        if boxes:
            prev = boxes[-1][-1]
            # Slides puts the next paragraph at least this far below (line box of the previous
            # line, ascent of the next): a caption tucked closer under a big number is its own box.
            if par["lines"][0]["baseline"] - prev["lines"][-1]["baseline"] < 0.232 * prev["size"] + 0.968 * par["size"] - 0.5:
                boxes.append([par])
                continue
            boxes[-1].append(par)
        else:
            boxes.append([par])
    return [{"paragraphs": b} for b in boxes]


def is_mono(spans: list[Span]) -> bool:
    return bool(spans) and all(s.info.family == "mono" for s in spans)


def is_code(spans: list[Span]) -> bool:
    """A code line: monospaced from its start, with at most a few words in another face
    (listings' escapeinside, "← exponential!" after the code)."""
    content = sorted((s for s in spans if s.text.strip()), key=lambda s: s.rect.x0)
    if not content or content[0].info.family != "mono":
        return False
    letters = lambda ss: sum(len(s.text.strip()) for s in ss)
    return letters(s for s in content if s.info.family == "mono") >= 0.6 * letters(content)


def mono_advance(spans: list[Span]) -> float:
    """A monospaced line's advance per character, from its longest monospaced span."""
    mono = [s for s in spans if s.info.family == "mono" and s.text] or [s for s in spans if s.text]
    span = max(mono, key=lambda s: len(s.text))
    return span.rect.w / max(1, len(span.text))


def code_pitch(spans: list[Span], x0: float) -> float | None:
    """The column pitch of a code block: every monospaced span starts a whole number of
    columns right of the block's left edge `x0`, and a span of n letters is between n - 1 and
    n columns wide (listings' columns=fixed centres each glyph in a column wider than it:
    LMMono's 0.525 em glyphs on 0.6 em columns, so a span's width over its letters is no
    measure of the grid and "fib(n  - 1)" got two spaces). None when nothing fits."""
    mono = [s for s in spans if s.info.family == "mono" and s.text.strip()]
    if not mono:
        return None
    size = statistics.median(s.size for s in mono)
    scores = []
    for k in range(80, 161):  # 0.40 to 0.80 em
        p = k * 0.005 * size
        grid = sum(abs((s.rect.x0 - x0) / p - round((s.rect.x0 - x0) / p)) <= 0.15 for s in mono)
        wide = sum(len(s.text) >= 2 and s.rect.w / len(s.text) - 0.02 <= p <= s.rect.w / (len(s.text) - 1) + 0.02
                   or len(s.text) == 1 and s.rect.w - 0.02 <= p for s in mono)
        scores.append((grid + wide, p))
    top = max(sc for sc, _ in scores)
    if top < len(mono):
        return None
    fits = [p for sc, p in scores if sc == top]
    return statistics.median(fits)


def code_indent(par: Paragraph, box_x0: float, pitch: float | None = None) -> str:
    """Leading spaces that reproduce a code line's indentation (monospace advance per char)."""
    return " " * max(0, round((par.x0 - box_x0) / (pitch or mono_advance(par.first.content))))


# A tick label that is a number, or several touching ("1,0001,0501,100"; "10%", "−0.5").
TICK_NUMBER_RE = re.compile(r"[-−+]?\d[\d.,−%]*")


# ---------------------------------------------------------------- document-level statistics

def body_size(raw: dict) -> float:
    """The deck's most common text size, not counting theme furniture: a piece of text drawn at
    the same place on at least half the frames (and three of them) is a footline or headline
    ("Author (Inst.)  Short title  date"). In a Madrid/Boadilla deck with little prose - a deck
    of charts - the \\tiny footline outweighed the words, the body came out 6 pt, and every
    size gate measured against it (tick labels belong to their chart) failed on 11 pt ticks."""
    key = lambda s: (s["text"].strip(), round(s["bbox"][0]), round(s["bbox"][1]), round(s["size"], 1))
    frames_of: dict[tuple, set] = {}
    for page in raw["pages"]:
        for s in page["spans"]:
            frames_of.setdefault(key(s), set()).add(page.get("label"))
    frames = len({page.get("label") for page in raw["pages"]})
    counts, furniture = Counter(), Counter()
    for page in raw["pages"]:
        for s in page["spans"]:
            if font_info(s["font"]).family != "math":
                repeated = frames >= 3 and len(frames_of[key(s)]) >= max(3, 0.5 * frames)
                (furniture if repeated else counts)[round(s["size"], 1)] += len(s["text"].strip())
    counts = counts or furniture
    return counts.most_common(1)[0][0] if counts else 10.0


# ---------------------------------------------------------------- page analysis

class PageClassifier:
    def __init__(self, page: dict, body: float):
        self.page = page
        self.body = body
        self.W, self.H = page["size"]
        self.panels: list[dict] = []
        self.regions: list[Rect] = []
        self.bars: list[Rect] = []
        self.small_images: list[tuple[dict, Rect]] = []
        self.leftovers: list[dict] = []
        self._raw_spans = {s["id"]: s for s in page["spans"]}
        self.icon_bullets: list[Rect] = []  # item labels that became pictures

    # -- graphics -------------------------------------------------------------

    def is_decoration(self, r: Rect) -> bool:
        """Theme artwork: things anchored to a page edge spanning much of it (sidebars, header
        and footer bars), or hairlines running across most of the page."""
        edges = (r.x0 <= 1) + (r.y0 <= 1) + (r.x1 >= self.W - 1) + (r.y1 >= self.H - 1)
        if edges >= 2:
            return True  # corner pieces (logo boxes, header/sidebar junctions)
        if edges and (r.w >= 0.4 * self.W or r.h >= 0.4 * self.H):
            return True
        return (r.h <= 3 and r.w >= 0.5 * self.W) or (r.w <= 3 and r.h >= 0.5 * self.H)

    def text_decorations(self, spans: list[Span]) -> None:
        """Underlines, strike-throughs and \\colorbox highlights become text styles: a thin rule
        just below or through a stretch of words, or a filled box tightly around words and
        touching no other graphics.
        Sets the span attributes and remembers the drawings (they leave the background with
        the text, and are not graphics)."""
        self.decor_ids: set[str] = set()
        self.decor_rects: dict[str, list[Rect]] = {}  # span id -> drawings styling it
        flat = [s for s in spans if s.horizontal and s.text.strip()]
        drawings = [(d, Rect.of(d["bbox"])) for d in self.page["drawings"]]
        is_rule = lambda d, r: r.h <= 1.2 and ((d["type"] == "f" and d["items"] == "re") or (d["type"] == "s" and d["items"] == "l"))
        # ulem draws a rule per word and per space, soul a rule or box per word piece: pieces
        # touching or overlapping end to end are one rule or box.
        is_box = lambda d, r: d["type"] == "f" and d["items"] == "re" and d.get("fill") and not is_rule(d, r)
        candidates: list[tuple[list[dict], Rect]] = [([d], r) for d, r in drawings if not is_rule(d, r) and not is_box(d, r)]
        pieces = sorted(((d, r) for d, r in drawings if is_rule(d, r) or is_box(d, r)), key=lambda x: (round(x[1].cy), x[1].x0))
        for d, r in pieces:
            last = next((c for c in reversed(candidates) if is_rule(c[0][0], c[1]) == is_rule(d, r)), None)
            if last and last[0][0]["type"] == d["type"] and abs(last[1].cy - r.cy) <= 0.2 and -0.6 <= r.x0 - last[1].x1 <= 1 \
                    and (is_rule(d, r) or (abs(last[1].h - r.h) <= 0.2 and last[0][0]["fill"] == d["fill"] and r.x0 < last[1].x1)):
                candidates[candidates.index(last)] = (last[0] + [d], last[1].union(r))
            else:
                candidates.append(([d], r))

        def run_of(s: Span) -> Rect:
            """The words joined to s on its row."""
            row = sorted((o for o in flat if abs(o.baseline - s.baseline) <= 0.1 * s.size), key=lambda o: o.rect.x0)
            j0 = j1 = row.index(s)
            while j0 > 0 and row[j0].rect.x0 - row[j0 - 1].rect.x1 <= 0.6 * s.size:
                j0 -= 1
            while j1 < len(row) - 1 and row[j1 + 1].rect.x0 - row[j1].rect.x1 <= 0.6 * s.size:
                j1 += 1
            return Rect(row[j0].rect.x0, s.rect.y0, row[j1].rect.x1, s.rect.y1)

        for group, r in candidates:
            d = group[0]
            # (ulem's pieces under a long underlined line join into a rule wider than half the
            # page, which is_decoration takes for a theme hairline: the line then read as a
            # formula over two dozen fraction bars. A theme's hairline is one piece.)
            chain = len(group) >= 2 and is_rule(d, r)
            if (self.is_decoration(r) and not chain) or r.w < 2 or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            ops = d["items"]
            if is_rule(d, r):
                within = lambda s: r.x0 - 1 <= s.rect.x0 and s.rect.x1 <= r.x1 + max(1.0, 0.3 * s.size)  # "out," past the rule
                words = sorted((s for s in flat if within(s) and 0 < r.cy - s.baseline <= 0.45 * s.size), key=lambda s: s.rect.x0)
                # \sout: the rule runs through the middle of the lower-case letters.
                struck = sorted((s for s in flat if within(s) and 0.12 * s.size <= s.baseline - r.cy <= 0.4 * s.size),
                                key=lambda s: s.rect.x0)
                strike = not words
                words = words or struck
                if not words:
                    continue
                size = max(s.size for s in words)
                gaps = [b.rect.x0 - a.rect.x1 for a, b in zip(words, words[1:])]
                covered = sum(s.rect.w for s in words)
                # (a rule over words below it: an overline, a fraction bar; not the next line of
                # text under a wrapped underline, whose words run on past the rule)
                # (nor the short last line of a wrapped underlined paragraph, flush with the rule's
                # start and ending far before its end: a fraction's part is centred on its bar)
                flush_line = lambda o: abs(run_of(o).x0 - r.x0) <= 1 and r.x1 - run_of(o).x1 > size and o.baseline - r.cy >= 0.9 * size
                below = not strike and any(s.rect.x0 < r.x1 and r.x0 < s.rect.x1 and s.baseline > r.cy and s.rect.y0 < r.cy + 0.25 * size
                                           and (s.baseline - r.cy < 0.75 * size or r.expand(size).contains(run_of(s).x0, r.cy)
                                                and r.expand(size).contains(run_of(s).x1, r.cy) and not flush_line(s))
                                           for s in flat)
                if below or covered < 0.8 * r.w or any(g > 0.6 * size for g in gaps) or \
                        (strike and max(s.baseline for s in words) - min(s.baseline for s in words) > 0.1 * size) or \
                        abs(words[0].rect.x0 - r.x0) > 0.3 * size or abs(words[-1].rect.x1 - r.x1) > 0.3 * size:
                    continue
                for s in words:
                    if strike:
                        s.strike = True
                    else:
                        s.underline = True
            elif d["type"] == "f" and ops == "re" and d.get("fill") and d.get("fill_opacity", 1.0) >= 0.99:
                # (soul's \hl ends before punctuation that follows in the same span: "words,")
                punct = lambda s: 0.3 * s.size if s.text.rstrip()[-1:] in ",.;:!?)" and s.text.rstrip()[-2:-1].isalnum() else 0.0
                inside = [s for s in flat if r.contains_rect(Rect(s.rect.x0, s.rect.y0, max(min(s.rect.x1, r.x1), s.rect.x1 - punct(s)),
                                                                   s.rect.y1), tol=0.5)]
                if not inside or any(s.rect.intersects(r) and s not in inside for s in flat):
                    continue
                size = max(s.size for s in inside)
                if not 0.9 * size <= r.h <= 2.2 * size or len({round(s.baseline) for s in inside}) != 1:
                    continue
                if sum(s.rect.w for s in inside) < 0.6 * r.w or \
                        any(o not in group and ro.expand(1).intersects(r) and not r.contains_rect(ro, tol=0)
                            for o, ro in drawings if ro.w * ro.h < 0.95 * self.W * self.H):
                    continue  # part of a figure (a filled TikZ node with lines attached)
                if any(o not in group and not is_rule(o, ro) and r.contains_rect(ro, tol=0) and not ro.contains_rect(r)
                       for o, ro in drawings):
                    continue  # a box holding marks besides its words: a legend's swatches
                row = sorted(inside, key=lambda s: s.rect.x0)
                # \colorbox pads its words by \fboxsep (3 pt), soul's \hl by a quarter point: a
                # padded box alone on its line is a panel under its words (as a highlight it
                # shrank to the glyphs), one within a line keeps its padding as highlighted
                # no-break spaces (runs).
                padded = min(row[0].rect.x0 - r.x0, r.x1 - row[-1].rect.x1) >= max(1.5, 0.15 * size)
                if padded and r.w >= 0.25 * self.W and not any(
                        abs(o.baseline - row[0].baseline) <= 0.1 * size and o not in inside for o in flat):
                    continue
                for s in inside:
                    s.highlight = d["fill"]
                if padded:
                    row[0].pad_left = row[-1].pad_right = True
                words = inside
            else:
                continue
            for s in words:
                # (\uline{matches}, and \hl{...}. end before the punctuation their span goes on
                # with: runs cut it off undecorated, or two phrases joined into one across ", ")
                if r.x1 < s.rect.x1 - DECOR_SHORT * s.size:
                    s.decor_to = r.x1
            self.decor_ids |= {g["id"] for g in group}
            for s in words:
                self.decor_rects.setdefault(s.id, []).append(r)

    def underscores(self, spans: list[Span]) -> None:
        """OT1, beamer's default encoding, has no underscore glyph: `\\_` is a rule TeX draws
        0.3 em long on the baseline (`\\kern.06em\\vbox{\\hrule width.3em}`), so "x86\\_64"
        reaches the PDF as two words with a line between them, which read as a word on a small
        graphic (a hole, `graphic_holes`) or as a formula. A rule that short, level with the
        baseline and right beside a glyph of that line, is an underscore: a span "_" joins the
        words (`drawn`: it has no page object), and the rule leaves the background with the
        text as an underline does (`decor_rects`)."""
        # (never beside a radical sign, which hangs from its origin like a CMEX glyph: its overbar
        # is level with that origin, and has its radicand right under it where an underscore
        # has nothing)
        flat = [s for s in spans if s.horizontal and s.text.strip()]
        signs = [s for s in flat if s.font.startswith("CMEX") or "√" in s.text]
        flat = [s for s in flat if s not in signs]
        for d in self.page["drawings"]:
            r = Rect.of(d["bbox"])
            if d["id"] in self.decor_ids or r.h > 0.8 or \
                    not ((d["type"] == "s" and d["items"] == "l") or (d["type"] == "f" and d["items"] == "re")):
                continue
            beside = [s for s in flat if 0.2 * s.size <= r.w <= 0.7 * s.size and abs(r.cy - s.baseline) <= 0.12 * s.size
                      and (-0.5 <= r.x0 - s.rect.x1 <= 0.25 * s.size or -0.5 <= s.rect.x0 - r.x1 <= 0.25 * s.size)]
            if not beside or any(s.rect.x0 < r.x1 and r.x0 < s.rect.x1 and r.cy < s.rect.cy < r.cy + beside[0].size
                                 for s in flat) or \
                    any(abs(s.rect.x1 - r.x0) <= 1 or abs(r.x1 - s.rect.x0) <= 1 for s in signs):
                continue  # an overbar or a fraction bar: something is set under it
            like = beside[0]
            span = Span(f"{d['id']}u", "_", like.font, like.size, like.color, Rect(r.x0, like.rect.y0, r.x1, like.rect.y1),
                        like.baseline, True, like.info, link=like.link, drawn=True)
            spans.append(span)
            self.decor_ids.add(d["id"])
            self.decor_rects[span.id] = [r]

    def table_hairlines(self) -> set[str]:
        """Rules of a table wider than half the page, which `is_decoration` would take for theme
        hairlines: two or more rules of one extent, touching no page edge, with rows of text
        between them - at least two rows, one of them cells set more than an em apart - and no
        text running out past their ends. (A \\centering booktabs table in a 4:3 frame is often
        0.55-0.7 of the page wide; left as decoration its rules stayed in the background and its
        cells became text boxes that overlapped and reflowed across columns.)"""
        rules: dict[tuple[int, int], list[tuple[str, Rect]]] = {}
        for d in self.page["drawings"]:
            r = Rect.of(d["bbox"])
            if d["id"] in self.decor_ids or r.w < 0.5 * self.W or \
                    r.x0 <= 1 or r.y0 <= 1 or r.x1 >= self.W - 1 or r.y1 >= self.H - 1:
                continue
            if (d["type"] == "s" and d["items"] == "l" and r.h <= 1.0) or \
                    (d["type"] == "f" and d["items"] == "re" and r.h <= 1.5):
                rules.setdefault((round(r.x0), round(r.x1)), []).append((d["id"], r))
        out: set[str] = set()
        cells = [Rect.of(d["bbox"]) for d in self.page["drawings"] if d["type"] == "f" and d["items"] == "re"
                 and d.get("fill_opacity", 1.0) >= 0.99 and min(Rect.of(d["bbox"]).w, Rect.of(d["bbox"]).h) > 3
                 and Rect.of(d["bbox"]).w < 0.5 * self.W]
        for group in rules.values():
            if len(group) == 1:
                # One \hline over a row of shaded cells (a heatmap's header rule): the table's.
                _, r = group[0]
                row = [f for f in cells if min(abs(f.y0 - r.cy), abs(f.y1 - r.cy)) <= 0.6 and r.x0 - 1 <= f.x0 and f.x1 <= r.x1 + 1]
                if len(row) >= 2:
                    out.add(group[0][0])
                continue
            x0, x1 = min(r.x0 for _, r in group), max(r.x1 for _, r in group)
            y0, y1 = min(r.cy for _, r in group), max(r.cy for _, r in group)
            inside = [s for s in self.page["spans"] if s["text"].strip() and y0 < (s["bbox"][1] + s["bbox"][3]) / 2 < y1
                      and s["bbox"][0] < x1 and x0 < s["bbox"][2]]
            if not inside or any(s["bbox"][0] < x0 - 1 or s["bbox"][2] > x1 + 1 for s in inside):
                continue
            rows: list[list[dict]] = []
            for s in sorted(inside, key=lambda s: s["origin"][1]):
                if rows and abs(rows[-1][0]["origin"][1] - s["origin"][1]) <= 0.3 * s["size"]:
                    rows[-1].append(s)
                else:
                    rows.append([s])
            def cells_apart(row: list[dict]) -> bool:
                row = sorted(row, key=lambda s: s["bbox"][0])
                return any(b["bbox"][0] - a["bbox"][2] >= max(a["size"], b["size"]) for a, b in zip(row, row[1:]))
            if len(rows) >= 2 and any(map(cells_apart, rows)):
                out |= {i for i, _ in group}
        return out

    def typeset_fraction(self, r: Rect) -> bool:
        """A stroke is a fraction's bar, however long, when words sit right on it and right
        under it, all within its ends, one side running from end to end (TeX makes the bar as
        long as the wider part) and the other centred on it. As a graphic it became a figure
        region with the words at it, and the numerator or denominator went with that figure
        or into the background while the rest of the formula stayed text."""
        above, below = [], []  # the glyphs of each part, scripts further off the bar too
        touching = [False, False]
        for s in self.page["spans"]:
            x0, y0, x1, y1 = s["bbox"]
            if x1 <= r.x0 or x0 >= r.x1 or not s["text"].strip():
                continue
            if r.y0 - 0.8 * self.body <= y1 <= r.y0 + 0.5:
                above.append((s["bbox"], s["size"]))
                on = y1 >= r.y0 - 0.4 * s["size"]
                touching[0] |= on
            elif r.y1 - 1 <= y0 <= r.y1 + 0.8 * self.body:
                below.append((s["bbox"], s["size"]))
                on = y0 <= r.y1 + 0.4 * s["size"]
                touching[1] |= on
            else:
                continue
            if on and (x0 < r.x0 - 1 or x1 > r.x1 + 1):
                return False  # a word across its end: an underline or a rule under a heading
        if not all(touching):
            return False
        for side in (above, below):  # one run of glyphs each, not a table's cells
            side.sort(key=lambda b: b[0][0])
            size = max(b[1] for b in side)
            if any(b[0][0] - max(a[0][2] for a in side[:i + 1]) > 0.7 * size for i, b in enumerate(side[1:])):
                return False
        above, below = [b for b, _ in above], [b for b, _ in below]
        sides = [(min(b[0] for b in side), max(b[2] for b in side)) for side in (above, below)]
        full = [abs(a - r.x0) <= 1 and abs(b - r.x1) <= 1 for a, b in sides]
        centred = [abs((a + b) / 2 - r.cx) <= 0.05 * r.w + 1 for a, b in sides]
        return any(full) and all(centred)

    @staticmethod
    def thin_rule(d: dict) -> tuple[str, float, str] | None:
        """A drawing that is a straight rule: ('h' or 'v', its thickness, its colour) - a stroked
        line, or a filled box thinner than FRAME_RULE_PT."""
        r = Rect.of(d["bbox"])
        if d["type"] == "s" and d["items"] == "l" and d.get("stroke") and min(r.w, r.h) <= 0.1:
            return ("h" if r.w > r.h else "v"), (d.get("width") or 0.4), d["stroke"]
        if d["type"] == "f" and d["items"] == "re" and d.get("fill") and d.get("fill_opacity", 1.0) >= 0.99 \
                and min(r.w, r.h) <= FRAME_RULE_PT and max(r.w, r.h) >= 2:
            return ("h" if r.w > r.h else "v"), min(r.w, r.h), d["fill"]
        return None

    def frame_of(self, box: Rect, fill: str | None, drawings: list[dict], partial: bool = False) -> dict | None:
        """The frame drawn around a filled box: rules along all four of its edges, in one colour
        other than the fill, covering each edge, with no other rule inside (then the box is a
        table's). \\fcolorbox draws its fill, then four rules on its outer edge; listings'
        frame=single a rule per line on each side and one above and below. Returns the outline
        ({"color", "width", "sides", "box": the rules' centre lines}) and the ids of the drawings
        it takes (with rules in the fill's own colour over it: listings paints the side strips
        twice). `partial`: some of the sides are enough (listings' frame=lines, leftline)."""
        by_color: dict[str, dict] = {}  # colour -> {"edges": edge -> pieces, "widths", "ids"}
        same = []
        for d in drawings:
            rule = self.thin_rule(d)
            if rule is None:
                continue
            axis, width, c = rule
            r = Rect.of(d["bbox"])
            if not box.expand(FRAME_REACH).contains_rect(r, tol=0):
                continue  # (a footnote rule reaching out under the box)
            if c == fill:
                same.append(d["id"])
                continue
            mid = r.cy if axis == "h" else r.cx
            near = [e for e, at in (("t", box.y0), ("b", box.y1)) if axis == "h" and abs(mid - at) <= FRAME_REACH] + \
                   [e for e, at in (("l", box.x0), ("r", box.x1)) if axis == "v" and abs(mid - at) <= FRAME_REACH]
            if not near:
                return None  # a rule across the box: a table's, or a figure's
            got = by_color.setdefault(c, {"edges": {"t": [], "b": [], "l": [], "r": []}, "widths": [], "ids": [],
                                          "edge_of": [], "rects": []})
            got["edges"][near[0]].append((r.x0, r.x1, mid) if axis == "h" else (r.y0, r.y1, mid))
            got["widths"].append(width)
            got["ids"].append(d["id"])
            got["edge_of"].append(near[0])
            got["rects"].append(r if min(r.w, r.h) >= width - 0.05 else
                                (Rect(r.x0, mid - width / 2, r.x1, mid + width / 2) if axis == "h"
                                 else Rect(mid - width / 2, r.y0, mid + width / 2, r.y1)))

        words = [Rect.of(s["bbox"]) for s in self.page["spans"] if s["text"].strip()]

        def labelled(e: str, a: float, b: float, at: float) -> bool:
            """A word set in the frame's rule (fancyvrb's label, a titled frame) opens it."""
            return any(w.y0 < at < w.y1 and a <= w.cx <= b if e in "tb" else w.x0 < at < w.x1 and a <= w.cy <= b
                       for w in words)

        def sides(edges: dict) -> str:
            out = ""
            for e, pieces in edges.items():
                covered, end = 0.0, None
                for a, b, m in sorted(pieces):
                    if end is not None and a > end and labelled(e, end, a, m):
                        covered += a - end
                    a = a if end is None else max(a, end)
                    covered += max(0.0, b - a)
                    end = b if end is None else max(end, b)
                if pieces and covered >= 0.9 * (box.w if e in "tb" else box.h):
                    out += e
            return out
        framing = [(c, got, sides(got["edges"])) for c, got in by_color.items()]
        framing = [f for f in framing if f[2] and (partial or len(f[2]) == 4)]
        if not framing:
            return None
        color, got, drawn = max(framing, key=lambda f: (len(f[2]), len(f[1]["ids"])))
        at = {e: sorted(m for _, _, m in pieces)[len(pieces) // 2] if pieces else None for e, pieces in got["edges"].items()}
        at = {"l": at["l"] if "l" in drawn else box.x0, "t": at["t"] if "t" in drawn else box.y0,
              "r": at["r"] if "r" in drawn else box.x1, "b": at["b"] if "b" in drawn else box.y1}
        ids = [i for i, e in zip(got["ids"], got["edge_of"]) if e in drawn]
        rules = []  # each side's pieces joined where they touch (a label's gap stays open)
        for side in drawn:
            for r in sorted((r for r, e in zip(got["rects"], got["edge_of"]) if e == side), key=lambda r: (r.x0, r.y0)):
                if rules and rules[-1][0] == side and rules[-1][1].expand(0.6).intersects(r):
                    rules[-1][1] = rules[-1][1].union(r)
                else:
                    rules.append([side, r])
        rules = [r.as_list() for _, r in rules]
        return {"color": color, "width": round(sorted(got["widths"])[len(got["widths"]) // 2], 2), "sides": drawn,
                "box": [round(at["l"], 2), round(at["t"], 2), round(at["r"], 2), round(at["b"], 2)],
                "rules": [[round(v, 2) for v in r] for r in rules], "ids": ids + same}

    def panel_frames(self) -> list[dict]:
        """Filled boxes whose frame is drawn as rules around them (\\fcolorbox, listings'
        frame=single) and fills of one colour tiling one box (listings paints its background a
        line at a time, with strips for the frame's separation; a black terminal listing is a
        stack of bands): the page's drawings with each such stack as one fill, and the frames
        found in `self.frames` (fill drawing id -> outline) and `self.frame_ids`. Without this a
        listing was a stack of panels whose shared edges show as seams, its frame and its line
        numbers were thin pictures under them, and an \\fcolorbox lost the rules the panel
        removal took from the background."""
        self.frames: dict[str, dict] = {}
        self.frame_ids: set[str] = set()
        drawings = [d for d in self.page["drawings"] if d["id"] not in self.decor_ids]
        tile = lambda d: d["type"] == "f" and d["items"] == "re" and d.get("fill") and d.get("fill_opacity", 1.0) >= 0.99
        tiles = [(d, Rect.of(d["bbox"])) for d in drawings if tile(d)
                 and Rect.of(d["bbox"]).w * Rect.of(d["bbox"]).h < 0.95 * self.W * self.H]
        parent = list(range(len(tiles)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def join(beside):
            boxes: dict[int, Rect] = {}
            for i, (_, r) in enumerate(tiles):
                boxes[find(i)] = boxes[find(i)].union(r) if find(i) in boxes else r
            keys = list(boxes)
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    ra, rb = boxes[a], boxes[b]
                    if tiles[a][0]["fill"] == tiles[b][0]["fill"] and beside(ra, rb) and find(a) != find(b):
                        parent[find(a)] = find(b)
        # a line's pieces side by side first, then the lines one under the other
        join(lambda ra, rb: abs(ra.y0 - rb.y0) <= 0.3 and abs(ra.y1 - rb.y1) <= 0.3 and
             (-0.3 <= rb.x0 - ra.x1 <= 0.3 or -0.3 <= ra.x0 - rb.x1 <= 0.3))
        join(lambda ra, rb: abs(ra.x0 - rb.x0) <= 0.6 and abs(ra.x1 - rb.x1) <= 0.6 and
             (-0.3 <= rb.y0 - ra.y1 <= 0.3 or -0.3 <= ra.y0 - rb.y1 <= 0.3))
        groups: dict[int, list[int]] = {}
        for i in range(len(tiles)):
            groups.setdefault(find(i), []).append(i)
        replaced: dict[str, dict] = {}  # first tile's id -> the stack as one fill
        gone: set[str] = set()
        spans = [(Rect.of(s["bbox"]), s) for s in self.page["spans"] if s["text"].strip()]
        for members in groups.values():
            if len(members) < 2:
                continue
            box = union_all(tiles[i][1] for i in members)
            if sum(tiles[i][1].w * tiles[i][1].h for i in members) < 0.97 * box.w * box.h or box.w < 0.25 * self.W:
                continue  # not one box (an L, a staircase of bars)
            inside = [s for r, s in spans if box.contains(r.cx, r.cy)]
            code = bool(inside) and all(font_info(s["font"]).family == "mono" for s in inside)
            first = tiles[min(members)][0]
            frame = self.frame_of(box, first["fill"], drawings, partial=code)
            # (a stack is one panel when it is framed, or when what it holds is code: the rows of
            # a table with its fills stay cells)
            if frame is None and not code:
                continue
            replaced[first["id"]] = {**first, "bbox": box.as_list(), "path": [["re", [box.x0, box.y0, box.x1, box.y1]]],
                                     "corners": {}, "tiles": [tiles[i][0]["id"] for i in members]}
            gone |= {tiles[i][0]["id"] for i in members}
            if frame:
                self.frames[first["id"]] = frame
                self.frame_ids |= set(frame["ids"])
        out = []
        for d in drawings:
            if d["id"] in replaced:
                out.append(replaced[d["id"]])
            elif d["id"] not in gone:
                out.append(d)
        for d in out:
            r = Rect.of(d["bbox"])
            if d["id"] in self.frames or d["id"] in self.frame_ids or not (
                    d["type"] == "f" and set(d["items"]) <= set("relcq") and r.w >= 0.25 * self.W and r.h >= 3 and box_outline(d, r)):
                continue
            frame = self.frame_of(r, d["fill"], [o for o in out if o is not d and o["id"] not in self.frame_ids])
            if frame:
                self.frames[d["id"]] = frame
                self.frame_ids |= set(frame["ids"])
        self.bare_frames = self.rule_frames([d for d in out if d["id"] not in self.frame_ids], spans)
        return [d for d in out if d["id"] not in self.frame_ids]

    def rule_frames(self, drawings: list[dict], spans: list[tuple[Rect, dict]]) -> list[dict]:
        """Rules closing a rectangle around code with nothing filled under them (listings'
        frame=single without a background: a rule piece per line on each side): the frames,
        their drawings taken into `self.frame_ids`. The sides become rule shapes and the frame
        a listing's panel; as figures, the side strips were pictures whose ends did not meet
        the top and bottom rules, which stayed in the background as page-wide hairlines."""
        rules = [(d, self.thin_rule(d)) for d in drawings]
        rules = [(d, rule[2]) for d, rule in rules if rule]
        if len(rules) < 4 or len(rules) > 400:
            return []  # (a chart's hundreds of ticks frame no code)
        parent = list(range(len(rules)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        boxes = [Rect.of(d["bbox"]).expand(0.6) for d, _ in rules]
        for i in range(len(rules)):
            for j in range(i + 1, len(rules)):
                if rules[i][1] == rules[j][1] and boxes[i].intersects(boxes[j]):
                    parent[find(i)] = find(j)
        rules = [d for d, _ in rules]
        groups: dict[int, list[dict]] = {}
        for i, d in enumerate(rules):
            groups.setdefault(find(i), []).append(d)
        out = []
        for group in groups.values():
            box = union_all(Rect.of(d["bbox"]) for d in group)
            if len(group) < 4 or box.w < 0.25 * self.W or box.h < 6:
                continue
            inside = [s for r, s in spans if box.contains(r.cx, r.cy)]
            letters = lambda ss: sum(len(s["text"].strip()) for s in ss)
            if not inside or letters(s for s in inside if font_info(s["font"]).family == "mono") < 0.9 * letters(inside):
                continue  # (an \fbox around prose is a table's cell, see table_from)
            frame = self.frame_of(box, None, group)
            if frame is None:
                continue
            out.append({"bbox": box, "frame": frame, "id": group[0]["id"]})
            self.frame_ids |= set(frame["ids"])
        return out

    def analyse_graphics(self) -> None:
        graphics = []
        rules: dict[tuple[int, int], list[Rect]] = {}
        self.decorations: list[Rect] = []
        self.graphic_drawings: dict[str, Rect] = {}  # drawing id -> box, for the drawings among the graphics
        bar_ids: list[tuple[str, Rect]] = []
        self.graphic_paths: dict[tuple, dict] = {}  # graphic box -> its drawing
        table_rules = self.table_hairlines()
        for d in self.panel_frames():
            r = Rect.of(d["bbox"])
            if r.w * r.h >= 0.95 * self.W * self.H:
                continue  # page background
            fill_only = d["type"] == "f" and set(d["items"]) <= set("relcq")
            panel = fill_only and r.w >= 0.25 * self.W and r.h >= 3
            if self.is_decoration(r) and not (panel and box_outline(d, r)) and d["id"] not in table_rules:
                self.decorations.append(r)
                continue
            if panel and box_outline(d, r):
                self.panels.append({"bbox": r, "fill": d["fill"], "id": d["id"],
                                    "rounded": "c" in d["items"], "corners": d.get("corners", {}),
                                    "opacity": d.get("fill_opacity", 1.0), "image": False,
                                    "frame": self.frames.get(d["id"]), "tiles": d.get("tiles", [])})
            elif d["type"] == "s" and r.h <= 1.0 and set(d["items"]) <= {"l"} and (r.w <= 3 * self.body or any(
                    abs(s["bbox"][2] - r.x0) <= 1 and s["bbox"][1] - 1 <= r.y0 <= s["bbox"][3]
                    and (extension_font(s["font"]) or "√" in s["text"])
                    for s in self.page["spans"]) or self.typeset_fraction(r)):
                self.bars.append(r)  # fraction bars, radical overbars (long ones start at their radical sign)
                bar_ids.append((d["id"], r))
            else:
                graphics.append(r)
                self.graphic_drawings[d["id"]] = r
                self.graphic_paths.setdefault(tuple(r.as_list()), d)
                stroke_rule = d["type"] == "s" and r.h <= 1.0 and set(d["items"]) <= {"l"}
                fill_rule = fill_only and r.h <= 1.5 and r.w >= 20  # booktabs rules are thin filled boxes
                if stroke_rule or fill_rule:
                    rules.setdefault((round(r.x0), round(r.x1)), []).append({
                        "rect": r, "color": (d["fill"] if fill_rule else d["stroke"]) or "#000000",
                        "weight": r.h if fill_rule else (d["width"] or 0.4)})
        for f in self.bare_frames:  # a listing's frame of rules: a panel with nothing filled
            self.panels.append({"bbox": Rect.of(f["frame"]["box"]), "fill": None, "id": f["id"], "rounded": False,
                                "corners": {}, "opacity": 1.0, "image": False, "frame": f["frame"]})
        for p in [p for p in self.panels if self.legend_box(p["bbox"], graphics)]:
            # A chart's legend box is part of the chart: as a panel, its swatches became bullets
            # of labels set on a native box, one per line, and the others were lost.
            self.panels.remove(p)
            graphics.append(p["bbox"])
            self.graphic_drawings[p["id"]] = p["bbox"]
            self.graphic_paths.setdefault(tuple(p["bbox"].as_list()), next(d for d in self.page["drawings"] if d["id"] == p["id"]))
        # Strips thinner than a line of text, with no words on them, starting at one x, as thick
        # as each other and three or more (or two abutting), touching a graphic, are the bars of
        # a chart (xbar: one \addplot per series, a rectangle each): as panels they were cut off
        # the chart's picture and set on top of it. (Not a listing frame's top and bottom strip.)
        words = [Rect.of(s["bbox"]) for s in self.page["spans"] if s["text"].strip()]
        thin = [p for p in self.panels if not p["image"] and p["bbox"].h < 0.6 * self.body
                and not any(p["bbox"].contains(w.cx, w.cy) for w in words)]
        bars = []
        for p in thin:
            r = p["bbox"]
            mates = [q["bbox"] for q in thin if q is not p and abs(q["bbox"].x0 - r.x0) <= 0.5 and abs(q["bbox"].h - r.h) <= 0.3]
            series = len(mates) >= 2 or any(min(abs(q.y0 - r.y1), abs(r.y0 - q.y1)) <= r.h for q in mates)
            if series and any(r.expand(1.0).intersects(g) for g in graphics):
                bars.append(p)
        for p in bars:
            self.panels.remove(p)
            graphics.append(p["bbox"])
            self.graphic_drawings[p["id"]] = p["bbox"]
            self.graphic_paths.setdefault(tuple(p["bbox"].as_list()), next(d for d in self.page["drawings"] if d["id"] == p["id"]))
        # A box of a footline or headline made of boxes side by side (author | title | date |
        # page): on the page's edge, as tall as the band and abutting a piece of it. Too narrow
        # to be theme artwork alone, it was a figure, and the date in it a label baked into the
        # background while the words of the boxes beside it were theme text.
        grown = True
        while grown:
            grown = False
            for i, r in [(i, r) for i, r in self.graphic_drawings.items() if any(r is g for g in graphics)]:
                edge = r.y0 <= 1 or r.y1 >= self.H - 1
                if edge and r.h <= 0.1 * self.H and any(
                        abs(d.y0 - r.y0) <= 1 and abs(d.y1 - r.y1) <= 1 and (abs(d.x0 - r.x1) <= 1 or abs(r.x0 - d.x1) <= 1)
                        for d in self.decorations):
                    graphics.remove(r)
                    del self.graphic_drawings[i]
                    self.decorations.append(r)
                    grown = True
        # Two or more horizontal rules of equal extent frame a table: the whole span is one
        # figure (or a native table, see table_from).
        self.table_rules = [g for g in rules.values() if len(g) >= 2]
        for group in self.table_rules:
            graphics.append(union_all(r["rect"] for r in group))
        # A short stroke touching other graphics is an arrow shaft or a tick, not a fraction bar.
        touching = [b for b in self.bars if any(b.expand(1.5).intersects(g) for g in graphics)]
        self.bars = [b for b in self.bars if b not in touching]
        graphics += touching
        self.graphic_drawings.update((i, r) for i, r in bar_ids if any(r is t for t in touching))
        for im in self.page["images"]:
            r = Rect.of(im["bbox"])
            if min(r.w, r.h) < SMALL_IMAGE_PT:
                self.small_images.append((im, r))  # bullets, block shadows
            elif self.is_decoration(r):
                self.decorations.append(r)  # sidebar/header shading
            elif r.w >= 0.6 * self.W:
                self.panels.append({"bbox": r, "fill": None, "id": im["id"], "rounded": False,
                                    "corners": {}, "opacity": 1.0, "image": True})
            else:
                graphics.append(r)
        # Glyphs of symbol fonts (Creative Commons badges, FontAwesome) are artwork.
        graphics += [Rect.of(s["bbox"]) for s in self.page["spans"] if font_info(s["font"]).family == "icon"]
        self.graphics = graphics
        self._artwork = None  # see artwork_of
        self.regions = cluster_rects(graphics, gap=3.0) if graphics else []
        self.title_bridges: list[Rect] = []  # see axis_titles
        self.column_bridges: list[Rect] = []  # see axis_label_column

    def legend_box(self, box: Rect, graphics: list[Rect]) -> bool:
        """A box holding a row of key marks, each right before its label ("[■] EMEA  [■] Americas"):
        a legend. (A list's bullets stand one above the other, and the first items of two
        columns side by side are a column apart.)"""
        spans = [Rect.of(s["bbox"]) for s in self.page["spans"] if s["text"].strip()]
        marks = [g for g in graphics if box.contains_rect(g) and max(g.w, g.h) <= 15 and
                 any(0 <= s.x0 - g.x1 <= 12 and s.y0 < g.cy < s.y1 for s in spans)]
        return any(a is not b and abs(a.cy - b.cy) <= 1 and 0 < b.x0 - a.x1 <= 100 for a in marks for b in marks)

    def artwork_of(self, r: Rect) -> tuple | None:
        """The smallest filled piece of theme artwork (a decoration: corner square, sidebar,
        band; or a small box in the header or footer band, a \\logo's) the rect's centre is on,
        as its box. (Not strokes: a zoomed plot's lines reaching off the page are decorations
        too, and cross the title.) A logo of words in the corner beside the last line of a
        references frame was a hole at that line's end, and the box wider than the entries."""
        if not hasattr(self, "decorations"):
            return None  # (lines built before the graphics were looked at)
        if getattr(self, "_artwork", None) is None:
            decor = {tuple(d.as_list()) for d in self.decorations}
            ornament = lambda d: d["id"] in self.graphic_drawings and self.band_ornament(self.graphic_drawings[d["id"]])
            # (a logo's box holds its words; navigation symbols drawn over a footnote do not)
            self._artwork = [(Rect.of(d["bbox"]), tuple(Rect.of(d["bbox"]).as_list()) not in decor)
                             for d in self.page["drawings"] if "f" in d["type"] and d.get("fill")
                             and (tuple(Rect.of(d["bbox"]).as_list()) in decor or ornament(d))]
        on = [d for d, whole in self._artwork if (d.contains_rect(r, tol=0.5) if whole else d.contains(r.cx, r.cy))]
        return tuple(min(on, key=lambda d: d.w * d.h).as_list()) if on else None

    def on_edge_artwork(self, r: Rect) -> bool:
        edge_panels = [p["bbox"] for p in self.panels
                       if p["bbox"].x0 <= 1 or p["bbox"].y0 <= 1 or p["bbox"].x1 >= self.W - 1 or p["bbox"].y1 >= self.H - 1]
        return any(d.contains(r.cx, r.cy) for d in self.decorations + edge_panels)

    def panel_of(self, r: Rect) -> int | None:
        """Innermost panel containing the rect's centre. A translucent fill is a highlight laid
        over text, not a container."""
        best = None
        for i, p in enumerate(self.panels):
            if p["bbox"].contains(r.cx, r.cy) and p["opacity"] >= 0.99 and (best is None or p["bbox"].w * p["bbox"].h <
                                                    self.panels[best]["bbox"].w * self.panels[best]["bbox"].h):
                best = i
        return best

    # -- lines ----------------------------------------------------------------

    def spans(self) -> list[Span]:
        # External links keep their URL; internal ones become "#page=N" (PDF page index).
        links = [(Rect.of(l["bbox"]), l.get("uri") or f"#page={l['page']}") for l in self.page["links"]]
        out = []
        type3_words = type3_text_page(self.page["spans"])
        for s in self.page["spans"]:
            r = Rect.of(s["bbox"])
            dx, dy = s["dir"]
            color = s["color"]
            if s.get("alpha", 255) < 250:
                # Semi-transparent text (\setbeamercovered{transparent}): Slides text has no
                # opacity, so show the colour it takes over a white page.
                a = s["alpha"] / 255
                color = "#" + "".join(f"{round(int(color[i:i + 2], 16) * a + 255 * (1 - a)):02x}" for i in (1, 3, 5))
            info = font_info(s["font"])
            text = type3_symbol(s["text"], type3_words) if s["font"] == "Type3" else \
                compose_accents(s["text"], info.family == "mono") if info.family not in ("math", "icon") else s["text"]
            # The page draws right-to-left text left to right (bidi.py): put its letters back, on
            # their own for now and with their line around them once lines are known (read_lines).
            visual, text = text, bidi.logical_text(text)
            span = Span(s["id"], text, s["font"].split("+", 1)[-1], s["size"], color, r,
                        s["origin"][1], abs(dy) < 0.01 and dx > 0, info, visual=visual)
            if s.get("smallcaps"):  # OpenType small caps, found from glyph ids (extract.small_caps_spans)
                span.info = replace(span.info, smallcaps=True)
            span.link = next((uri for lr, uri in links if lr.contains(r.cx, r.cy)), None)
            out.append(span)
        return out

    def build_lines(self, spans: list[Span]) -> list[Line]:
        n = len(spans)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        # Words on different panels are different texts (Bergen's label column beside the body).
        panel = [self.panel_of(s.rect) for s in spans]
        # a listing's panel: monospaced text but for its line numbers (a block with a table of
        # \texttt cells is not)
        listing = set()
        for k in set(panel) - {None}:
            words = [s for s, p in zip(spans, panel) if p == k and s.text.strip() and not re.fullmatch(r"\d{1,4}", s.text.strip())]
            mono = sum(len(s.text.strip()) for s in words if s.info.family == "mono")
            if mono and mono >= 0.9 * sum(len(s.text.strip()) for s in words):
                listing.add(k)  # (an escaped arrow or word is fine)
        # Likewise words on different boxes of the theme's artwork: a \logo in a sidebar theme's
        # corner square is not the first word of the frame title in the headline beside it.
        artwork = [self.artwork_of(s.rect) for s in spans]
        for i in range(n):
            a = spans[i]
            for j in range(i + 1, n):
                b = spans[j]
                big = max(a.size, b.size)
                if big > 2.5 * min(a.size, b.size):
                    # very different sizes (a big statistic beside body copy): only words that
                    # share a baseline and nearly touch are one line
                    big = min(a.size, b.size)
                same_row = abs(a.baseline - b.baseline) <= 0.5 * big and \
                    min(a.rect.y1, b.rect.y1) > max(a.rect.y0, b.rect.y0)
                small, large = (a, b) if a.size < b.size else (b, a)
                if not same_row and small.size <= 0.85 * large.size \
                        and 0.5 * large.size < large.baseline - small.baseline <= 0.8 * large.size \
                        and -0.1 * large.size <= small.rect.x0 - large.rect.x1 <= 0.15 * large.size \
                        and min(a.rect.y1, b.rect.y1) > max(a.rect.y0, b.rect.y0):
                    # a superscript stacked over a subscript (S_n^{(k)}) is raised 0.53 em and
                    # starts where its base ends: its line's, a script of it, not a box of its own
                    same_row = True
                if same_row and small.size <= 0.6 * large.size and small.rect.y0 > large.baseline - 0.1 * large.size \
                        and not {"math", "icon"} & {small.info.family, large.info.family} \
                        and sum(c.isalnum() for c in small.text) >= 2:
                    # a word all below the big line's baseline: the next line, touching it (a
                    # subtitle under a heading); a script rises above the baseline it hangs
                    # from, and a limit under a display operator or a wavy underline is the
                    # formula's or the words' (their own holes and pictures)
                    same_row = False
                gap = max(0.0, b.rect.x0 - a.rect.x1, a.rect.x0 - b.rect.x1)
                # (text colour changes with the panel; a dark number on a light box across the
                # edge still belongs to its line)
                if same_row and gap <= 2.0 * big and (panel[i] == panel[j] or a.color == b.color) and artwork[i] == artwork[j] \
                        and not (gap > 0.8 * big and self.gutter(spans, a, b, big)) \
                        and not self.figure_label_apart(spans, a, b, gap, big):
                    parent[find(i)] = find(j)
                elif same_row and panel[i] in listing and panel[i] == panel[j] and gap <= 0.6 * self.panels[panel[i]]["bbox"].w \
                        and not any(s.info.family != "mono" and re.fullmatch(r"\d{1,4}", s.text.strip()) for s in (a, b)):
                    parent[find(i)] = find(j)  # a listing's line: a comment far right of its code is spaces
        groups: dict[int, list[Span]] = {}
        for i, s in enumerate(spans):
            groups.setdefault(find(i), []).append(s)
        lines = sorted((Line(g) for g in groups.values()), key=lambda l: (l.baseline, l.rect.x0))
        return self.join_line_labels(lines)

    @staticmethod
    def drop_hanging_glyphs(lines: list[Line]) -> list[Line]:
        """A glyph hanging from its origin - a \\displaystyle\\int from CMEX, a radical sign -
        has its box between two lines, nearer the upper one, and line building put it there,
        over a word of that line ("graphs" under a √ of the line below; "µ-a.e." over the ∫):
        the upper line's hole picture took it, reaching into the lower line, and the lower
        line's formula lost its sign. It belongs to the line below when it lies over a word of
        its line and in a gap of the line below, words right beside it on both sides."""
        out = list(lines)
        for a in lines:
            if a not in out or len(a.spans) < 2:
                continue  # (a line that took a glyph of the line above is done)
            for g in [s for s in a.spans if extension_font(s.font) or s.text.strip() == "√"]:
                size = g.size
                over = lambda w: min(w.rect.x1, g.rect.x1) - max(w.rect.x0, g.rect.x0) > 1.0
                if not any(w is not g and w.size >= 0.8 * size and over(w) for w in a.spans):
                    continue
                for b in out:
                    if b is a or not (a.baseline < b.baseline <= a.baseline + 2.5 * size) or any(map(over, b.spans)):
                        continue
                    left = any(g.rect.x0 - 1.5 * size <= w.rect.x1 <= g.rect.x0 + 1 for w in b.spans)
                    right = any(g.rect.x1 - 1 <= w.rect.x0 <= g.rect.x1 + 1.5 * size for w in b.spans)
                    if left and right:
                        i, j = out.index(a), out.index(b)
                        rest, joined = Line([s for s in a.spans if s is not g]), Line(b.spans + [g])
                        rest.tab, joined.tab = a.tab, b.tab
                        out[i], out[j] = rest, joined
                        a = rest
                        break
        # A \displaystyle glyph between two lines of prose, alone on a line of its own (its box
        # touched neither): it hangs into the lower line, where the words leave a gap for it
        # with prose right beside it ("the total mass ∫X ρ dµ of the body").
        for g_line in list(out):
            ext = [s for s in g_line.spans if extension_font(s.font)]
            if len(ext) != 1 or any(s is not ext[0] and s.size > 0.8 * ext[0].size for s in g_line.spans):
                continue
            g, size = ext[0], ext[0].size
            over = lambda w: min(w.rect.x1, g.rect.x1) - max(w.rect.x0, g.rect.x0) > 1.0
            for b in out:
                if b is g_line or not (g.baseline + 0.3 * size < b.baseline <= g.baseline + 2.5 * size) \
                        or g.rect.y1 < b.rect.y0 - 0.5 * size or any(map(over, b.spans)):
                    continue
                before = [w for w in b.spans if g.rect.x0 - 1.5 * size <= w.rect.x1 <= g.rect.x0 + 1]
                after = [w for w in b.spans if g.rect.x1 - 1 <= w.rect.x0 <= g.rect.x1 + 1.5 * size]
                if before and after and any(PageClassifier.prose_word(w) for w in before + after):
                    out.remove(g_line)
                    joined = Line(b.spans + g_line.spans)
                    joined.tab = b.tab
                    out[out.index(b)] = joined
                    break
        # A radical sign alone on a line of its own (its box ends above the radicand's baseline):
        # it goes with the radicand that starts at its right edge, or the hole picture was the
        # radicand's while the sign stood left of it, over the space before.
        for g_line in list(out):
            signs = [s for s in g_line.spans if s.text.strip() == "√"]
            if len(signs) != 1 or any(s is not signs[0] and s.size > 0.8 * signs[0].size for s in g_line.spans):
                continue
            g = signs[0]
            for host in out:
                if host is not g_line and any(
                        abs(w.rect.x0 - g.rect.x1) <= 1 and g.baseline < w.baseline <= g.rect.y1 + g.size
                        and w.size >= 0.6 * g.size for w in host.spans):
                    out.remove(g_line)
                    joined = Line(host.spans + g_line.spans)
                    joined.tab = host.tab
                    out[out.index(host)] = joined
                    break
        return out

    @staticmethod
    def split_line_numbers(lines: list[Line]) -> list[Line]:
        """A listing's line numbers (listings/minted `numbers=left`, fancyvrb `numbers`): a
        column of numbers ending at one x, counting up line by line, beside monospaced code.
        Each becomes a line of its own (`code_number`), the column one right-aligned box, and
        the code lines stay code. Joined to its line a number made the line prose: joined by an
        em space or a tab, whichever the gap made it, the code moved off its column, and the
        numbers of lines whose code starts further right were left alone, pictures under the
        listing's panels (r2_code_v1, r2_code_v4)."""
        found = []
        for line in lines:
            content = sorted(line.content, key=lambda s: s.rect.x0)
            if not content or line.bullet or not re.fullmatch(r"\d{1,4}", content[0].text.strip()):
                continue
            num, rest = content[0], content[1:]
            if rest and (not is_code(rest) or rest[0].rect.x0 - num.rect.x1 < 0.25 * num.size):
                continue
            # (its code may be a line of its own: indented code is further off than a gutter)
            beside = rest or [s for o in lines if o is not line and is_code(o.content)
                              and abs(o.baseline - num.baseline) <= 0.3 * num.size for s in o.content if s.rect.x0 > num.rect.x1]
            found.append((line, num, bool(beside)))
        columns: list[list[tuple]] = []
        for item in sorted(found, key=lambda f: f[1].rect.x1):
            col = next((c for c in columns if abs(c[0][1].rect.x1 - item[1].rect.x1) <= 1.0), None)
            (col.append(item) if col else columns.append([item]))
        out = list(lines)
        for col in columns:
            col.sort(key=lambda f: f[1].baseline)
            values = [int(f[1].text.strip()) for f in col]
            if len(col) < 3 or sum(code for _, _, code in col) < max(2, len(col) / 2) or any(b <= a for a, b in zip(values, values[1:])) \
                    or len({round(f[1].size, 1) for f in col}) != 1:
                continue
            for line, num, code in col:
                if len(line.content) == 1:
                    line.code_number = True
                    continue
                line.spans.remove(num)
                line.tab = None  # (the number was the label the code was tabbed from)
                own = Line([num])
                own.code_number = True
                out.insert(out.index(line), own)
        return out

    @staticmethod
    def join_hanging_operators(lines: list[Line]) -> list[Line]:
        """A CMEX glyph hangs from its origin - an inline `\\sum`'s is 8 pt above the baseline
        of the words it stands between - so it comes out a line of its own, which went to the
        background while its limits and the words around it stayed text: the sign stood still
        and the words moved. A lone CMEX glyph about as tall as a line's words, centred on them,
        with words of that line on both sides of it, is in that line (and becomes part of a
        formula hole there). Not a display operator at the start of its line, nor a radical
        taller than the line, nor a brace piece under it.

        The glyph may carry its limits (an \\int's small upper bound joins its line); a \\bigl|
        delimiter may stand up to twice the words' height; and the glyph may start its line when
        that line is in a paragraph's flow - a line of prose above or below starts where it
        does ("... pointwise, but / ∫₀¹ fn dx = 1 for all n")."""
        out = PageClassifier.drop_hanging_glyphs(list(lines))
        for g_line in list(out):
            cmex = [s for s in g_line.spans if extension_font(s.font)]
            if len(cmex) != 1 or any(s is not cmex[0] and s.size > 0.8 * cmex[0].size for s in g_line.spans):
                continue
            g = cmex[0]
            # (pdflatex's CMEX delimiters often come back unmapped, as U+FFFD)
            tall = 2.0 if g.text.strip() and all(c in DELIMITERS or c == "�" for c in g.text.strip()) else 1.5
            for host in out:
                if host is g_line:
                    continue
                words = [w for w in host.spans if not extension_font(w.font)]
                level = [w for w in words if 0.6 * w.rect.h <= g.rect.h <= tall * w.rect.h
                         and abs(g.rect.cy - w.rect.cy) <= 0.35 * w.size]
                before = [w for w in words if 0 <= g.rect.x0 - w.rect.x1 <= 1.5 * w.size]
                after = [w for w in words if 0 <= w.rect.x0 - g.rect.x1 <= 1.5 * w.size]
                # (the words after it may be a line of their own: the operator's box widened the
                # gap past what build_lines joins - "... add up: ∑ d(v) = 2|E|")
                other = None
                if before and not after:
                    other = next((o for o in out if o is not host and o is not g_line
                                  and abs(o.baseline - host.baseline) <= 0.2 * host.size
                                  and any(0 <= w.rect.x0 - g.rect.x1 <= 1.5 * w.size and not extension_font(w.font)
                                          for w in o.spans)), None)
                    if other:
                        after = [w for w in other.spans if 0 <= w.rect.x0 - g.rect.x1 <= 1.5 * w.size]
                prose = PageClassifier.prose_word
                # (next to a word of prose: inside a formula - between a relation and a bracket,
                # or its variables, which beamer's sans math sets in a text italic - it goes with
                # that formula wherever the formula goes; a theorem's italic words are prose)
                inside = before and any(map(prose, before + after))
                starts = not before and words and abs(g.rect.x0 - min(w.rect.x0 for w in words + [g])) <= 0.5 and any(
                    l is not host and l is not g_line and abs(l.x0 - g.rect.x0) <= 1.5
                    and 0.8 * host.size <= abs(l.baseline - host.baseline) <= 2.2 * host.size
                    and prose_share(l.content) >= DISPLAY_WORD_SHARE for l in out)
                if level and after and (inside or starts):
                    out.remove(g_line)
                    if other:
                        out.remove(other)
                    joined = Line(host.spans + g_line.spans + (other.spans if other else []))
                    joined.tab = host.tab
                    out[out.index(host)] = joined
                    break
        return out

    @staticmethod
    def prose_word(w: Span) -> bool:
        """A word of prose, not a formula's: text fonts, and in italic only a word of three or
        more letters (beamer's sans math sets variables in a text italic; a theorem's italic
        words are prose)."""
        return w.info.family != "math" and not w.font.startswith(("CMSY", "CMMI")) and (
            not w.info.italic or re.fullmatch(r"[^\W\d_]{3,}[,.;:]?", w.text.strip()) is not None)

    @staticmethod
    def read_lines(lines: list[Line], spans: list[Span]) -> None:
        """Each line that holds right-to-left letters read as one (`bidi.logical_line`): its spans'
        reading order (`Span.reading`, which `bidi.logical_spans` keeps) and their texts, which a
        span alone cannot say - ':7' in a Hebrew title is '7:', 12-13 is drawn 13-12, a formula
        starting with a number is one island, and a Hebrew line starting with a Latin name still
        reads right to left (the page's own direction, `bidi.page_direction`). A span whose text
        something rewrote since `spans` made it keeps that text."""
        prior = bidi.page_direction(s.visual or "" for s in spans)
        for n, line in enumerate(lines):
            items = sorted(line.spans, key=lambda s: s.rect.x0)
            if not all(s.visual is not None and s.horizontal for s in items) or \
                    not any(bidi.has_rtl(s.visual) for s in items):
                continue
            texts = [s.visual for s in items]
            widths = [max(b.rect.x0 - a.rect.x1, a.rect.x0 - b.rect.x1) for a, b in zip(items, items[1:])]
            joins = [" " if w > 0.15 * b.size and not a.visual.endswith(" ") and not b.visual.startswith(" ")
                     else "" for w, a, b in zip(widths, items, items[1:])]
            base = bidi.line_base(texts, prior)
            spaced = bidi.spaced(texts, base, joins)
            for rank, (i, text) in enumerate(bidi.logical_line(texts, base, joins)):
                s = items[i]
                if s.text == bidi.logical_text(s.visual):
                    s.text = text
                s.reading = (n, rank, base, widths[spaced[i]] if i in spaced else 0.0)

    def figure_label_apart(self, spans: list[Span], a: Span, b: Span, gap: float, size: float) -> bool:
        """A figure's label beside a column of words right of the figure (a pie's pin label
        on the baseline of a list item's second line, 0.8 em from it): the label is next to
        the figure's graphics (its pin), the words well clear of them and starting where the
        column's other lines start. More than a word space apart, they are no line - joined, the
        label opened the list's line and the item split there. The label is a word: an item's
        own label (a dingbat, '1.', '(a)') stands its labelsep (0.5 em) before the same edge."""
        if gap <= 0.6 * size or not getattr(self, "regions", None):
            return False
        left, right = (a, b) if a.rect.x0 < b.rect.x0 else (b, a)
        if sum(c.isalnum() for c in left.text) < 2 or re.fullmatch(r"\(?\w{1,3}[.):]", left.text.strip()):
            return False
        near = lambda s: any(r.distance(s.rect) <= 0.5 * size for r in self.regions)
        if not near(left) or near(right):
            return False
        edge = {round(s.baseline) for s in spans if s is not right and abs(s.rect.x0 - right.rect.x0) <= 1.0
                and abs(s.baseline - right.baseline) > 0.5 * size}
        return len(edge) >= 2

    @staticmethod
    def gutter(spans: list[Span], a: Span, b: Span, size: float) -> bool:
        """The gap between two words on one baseline is the gutter between columns: no text
        just above or below crosses it, and other lines there have words on both sides.

        That is enough after a long word. A short word left of the gap is a label or number
        before its text (a TOC entry, a list label), a tick label or a table cell - or the last
        word of a column's line, which ends on a short word as often as not ("for", "a"; a
        multicol gutter is 1.3-1.8 em, under the 2 em words join at). A column needs more
        evidence, and a long word's gap is a gutter with it too: the gap is not at the start
        of its line; another line has as wide a gap there with one side keeping to the same
        edge - a column starts (or a justified one ends) there; and on this line or such a
        one the words beside the gap run on like prose, wider than a table cell or tick.
        Words of a justified paragraph leave a channel of stretched spaces by chance, never
        edges, and this line's own words are no evidence (a \\framebox wider than its words)."""
        left, right = (a, b) if a.rect.x0 < b.rect.x0 else (b, a)
        mid = (left.rect.x1 + right.rect.x0) / 2
        y0, y1 = min(a.rect.y0, b.rect.y0) - 4 * size, max(a.rect.y1, b.rect.y1) + 4 * size
        band = [s for s in spans if s is not a and s is not b and s.rect.y1 > y0 and s.rect.y0 < y1
                and s.size <= 1.5 * size]  # (a frame title above spans all columns)
        if any(s.rect.x0 < mid < s.rect.x1 for s in band):
            return False
        if len(left.text.strip()) >= 6:
            rows_left = {round(s.baseline) for s in band if s.rect.x1 <= mid}
            rows_right = {round(s.baseline) for s in band if s.rect.x0 >= mid}
            if rows_left & rows_right:
                return True
        # (as build_lines has it: a script, a table cell a little off the paragraph's baseline)
        same_line = lambda s, t: abs(s.baseline - t.baseline) <= 0.5 * size and \
            min(s.rect.y1, t.rect.y1) > max(s.rect.y0, t.rect.y0)
        pool = band + [a, b]
        if not any(s.rect.x1 <= left.rect.x0 + 0.5 and same_line(s, left) for s in band):
            return False  # a label at the start of its line
        if len(left.text.strip()) < 6 and len(right.text.strip()) < 6 and \
                not any(s.rect.x0 >= right.rect.x1 - 0.5 and same_line(s, right) for s in band):
            return False  # (right to left: its number ball, at the end)

        def run(start: Span, step: int) -> float:
            """Width of the words that follow on from `start` away from the gap."""
            words = sorted((s for s in pool if s is start or same_line(s, start)), key=lambda s: s.rect.x0)[::step]
            k = next(i for i, s in enumerate(words) if s is start)
            edge = start
            for s in words[k + 1:]:
                if max(s.rect.x0 - edge.rect.x1, edge.rect.x0 - s.rect.x1) > 0.8 * size:
                    break
                edge = s
            return max(start.rect.x1, edge.rect.x1) - min(start.rect.x0, edge.rect.x0)

        others = [s for s in band if not (same_line(s, left) or same_line(s, right))]
        on_left = [s for s in others if s.rect.x1 <= mid]
        on_right = [s for s in others if s.rect.x0 >= mid]
        pairs = []
        for l in on_left:
            if any(s.rect.x1 > l.rect.x1 for s in on_left if same_line(s, l)):
                continue  # (l: the last word left of the gap on its line)
            across = [r for r in on_right if same_line(r, l)]
            if not across:
                continue
            r = min(across, key=lambda s: s.rect.x0)
            if r.rect.x0 - l.rect.x1 > 0.8 * size and \
                    (abs(r.rect.x0 - right.rect.x0) <= 1 or abs(l.rect.x1 - left.rect.x1) <= 1):
                pairs.append((l, r))
        return bool(pairs) and max(max(run(l, -1), run(r, 1)) for l, r in pairs + [(left, right)]) >= GUTTER_PROSE_EM * size

    @staticmethod
    def join_line_labels(lines: list[Line]) -> list[Line]:
        """A short label ("4:", "[2]") followed, further along the same baseline, by indented
        content (algorithmic, numbered code): one line whose content starts after a tab."""
        def is_label(span: Span) -> bool:
            return span.horizontal and bool(LINE_LABEL_RE.match(span.text.strip()))

        taken: set[int] = set()
        for label in lines:
            if len(label.spans) != 1 or not is_label(label.spans[0]) or id(label) in taken:
                continue
            candidates = [l for l in lines if l is not label and id(l) not in taken and l.spans[0].horizontal
                          and abs(l.baseline - label.baseline) <= 0.2 * max(l.size, label.size)
                          and 0.8 * max(l.size, label.size) <= l.rect.x0 - label.rect.x1 <= 8 * max(l.size, label.size)]
            if candidates:
                content = min(candidates, key=lambda l: l.rect.x0)
                label.spans += content.spans
                label.spans.sort(key=lambda s: s.rect.x0)
                label.tab = content.spans[0]
                taken.add(id(content))
        out = [l for l in lines if id(l) not in taken]
        for line in out:  # label and content close enough to have been joined already
            if line.tab is None and len(line.spans) >= 2 and is_label(line.spans[0]) and \
                    line.spans[1].rect.x0 - line.spans[0].rect.x1 >= 0.3 * line.size:
                line.tab = line.spans[1]

        # Description lists: labels of different widths ending (right-aligned) or starting
        # (left-aligned) at the same x, with the items' text starting at one common x.
        def splits(line: Line) -> dict[int, Span]:
            spans = line.spans
            if len(spans) < 2 or not all(s.horizontal for s in spans) or spans[0].text.strip() in BULLET_GLYPHS \
                    or ENUM_RE.match(spans[0].text.strip()) or is_mono(spans):
                return {}  # (code lines up in columns by itself)
            width = line.rect.w
            # The label is set off by \labelsep (0.5 em), wider than a word space: prose lines whose
            # word edges line up by chance (a paragraph under a list) don't pair.
            return {k: spans[k] for k in range(1, len(spans))
                    if spans[k].rect.x0 - spans[k - 1].rect.x1 >= LABEL_SEP_EM * line.size
                    and spans[k - 1].rect.x1 - spans[0].rect.x0 <= 0.45 * width}

        # (an item without a label may sit between two labelled ones)
        for k, i in [(k, i) for k in (1, 2) for i in range(len(out) - k)]:
            a, b = out[i], out[i + k]
            if a.tab is not None and b.tab is not None:
                continue
            if abs(a.size - b.size) > 0.5 or not 0 < b.baseline - a.baseline <= 2.5 * k * a.size:
                continue
            if k == 2 and out[i + 1].tab is not None:
                continue
            if abs(a.spans[0].rect.x0 - b.spans[0].rect.x0) <= 0.6 and \
                    not (NUMBERING_RE.match(a.spans[0].text.strip()) and NUMBERING_RE.match(b.spans[0].text.strip())):
                # labels start together: ordinary text already lines up the same way (unless
                # they are numbers: a nested enumerate's 2.1, 2.2)
                continue
            if abs(a.rect.cx - b.rect.cx) <= 0.6:
                continue  # centred lines (a quote): word edges line up only by chance
            sa, sb = splits(a), splits(b)
            for ka, span_a in sa.items():
                kb = next((k for k, s in sb.items() if abs(s.rect.x0 - span_a.rect.x0) <= 0.6
                           and abs(b.spans[k - 1].rect.x1 - a.spans[ka - 1].rect.x1) <= 0.6), None)
                if kb is not None and (a.tab is None or a.tab is span_a) and (b.tab is None or b.tab is sb[kb]):
                    a.tab, b.tab = span_a, sb[kb]
                    break
        # The other items of a list found that way: labels starting together (left-aligned, or
        # the widest of right-aligned ones) pair with no neighbour above, but a label ending where
        # an item's label ends, its text starting at that item's text, hangs the same way - and
        # so does a label too wide for the label column, its text \labelsep after it.
        tabbed = [l for l in out if l.tab is not None and any(s.rect.x1 <= l.tab.rect.x0 + 0.5 for s in l.spans)]
        for line in out:
            if line.tab is not None or not tabbed:
                continue
            for k, span in splits(line).items():
                end = line.spans[k - 1].rect.x1
                sep = span.rect.x0 - end
                for o in tabbed:
                    if abs(o.size - line.size) > 0.5 or abs(o.baseline - line.baseline) > 6 * line.size:
                        continue
                    o_end = max(s.rect.x1 for s in o.spans if s.rect.x1 <= o.tab.rect.x0 + 0.5)
                    same = abs(span.rect.x0 - o.tab.rect.x0) <= 0.6 and abs(end - o_end) <= 0.6
                    wide = end > o_end + 0.6 and abs(line.spans[0].rect.x0 - o.spans[0].rect.x0) <= 0.6 and \
                        abs(sep - (o.tab.rect.x0 - o_end)) <= 0.1 * line.size
                    if same or wide:
                        line.tab = span
                        break
                if line.tab is not None:
                    break
        return out

    @staticmethod
    def join_braces(lines: list[Line]) -> list[Line]:
        """\\underbrace / \\overbrace in a line of prose: the brace (big-operator glyphs, a line
        of its own just below or above) and its small label join the line, so the formula
        becomes one hole with them."""
        cmex = lambda s: extension_font(s.font)
        words = lambda l: sum(len(s.text.strip()) >= 2 and s.text.strip().isalpha() and s.info.family not in ("math", "icon")
                              for s in l.spans)
        taken: set[int] = set()
        for host in lines:
            if words(host) < 3 or not all(s.horizontal for s in host.spans):
                continue
            size = host.size
            for brace in lines:
                if id(brace) in taken or brace is host or not all(cmex(s) for s in brace.spans) or \
                        brace.rect.w < 0.8 * size or brace.rect.h > 1.2 * size or \
                        not (host.rect.x0 - 1 <= brace.rect.x0 and brace.rect.x1 <= host.rect.x1 + 1):
                    continue
                shift = brace.baseline - host.baseline
                if not (0 <= shift <= 0.8 * size or -1.2 * size <= shift < 0):
                    continue
                # (over or under a formula of the line, not a big operator below words)
                maths = [s for s in host.spans if s.info.family == "math" or (s.info.italic and len(s.text.strip()) <= 2)
                         or all(ch in MATH_OPERATORS or ch in "()[]" for ch in s.text.strip())]
                if sum(max(0.0, min(s.rect.x1, brace.rect.x1) - max(s.rect.x0, brace.rect.x0)) for s in maths) < 0.7 * brace.rect.w:
                    continue
                labels = [l for l in lines if id(l) not in taken and l is not host and l is not brace
                          and all(s.size <= 0.85 * size and s.horizontal for s in l.spans) and len(l.text) <= 20
                          and brace.rect.x0 - 0.5 * size <= l.rect.x0 and l.rect.x1 <= brace.rect.x1 + 0.5 * size
                          and l.rect.intersects(brace.rect.expand(0.5 * size))
                          and (l.baseline > brace.baseline if shift >= 0 else l.baseline < brace.baseline)]
                for l in [brace] + labels[:1]:
                    host.spans = sorted(host.spans + l.spans, key=lambda s: s.rect.x0)
                    taken.add(id(l))
        return [l for l in lines if id(l) not in taken]

    # -- line reasons -----------------------------------------------------------

    def inside_figure_share(self, line: Line) -> float:
        """Share of the line's characters whose span centre lies in a figure region. A list
        whose number sits in a drawn box has one character inside, not the whole line."""
        total = sum(len(s.text.strip()) for s in line.spans) or 1
        inside = sum(len(s.text.strip()) for s in line.spans
                     if any(reg.expand(1).contains(s.rect.cx, s.rect.cy) for reg in self.regions))
        return inside / total

    def prose_under_graphic(self, line: Line) -> bool:
        """A line of prose that a graphic is drawn over (a tikzmark arrow crossing it, a
        callout's pointer), not a label inside a figure: words reach out of every figure region."""
        words = [s for s in line.spans if sum(ch.isalpha() for ch in s.text) >= 2]
        outside = [s for s in words if not any(reg.expand(1).contains(s.rect.cx, s.rect.cy) for reg in self.regions)]
        return len(words) >= 2 and len(outside) >= 1

    def detect_bullet(self, line: Line) -> None:
        if reads_rtl(line):
            self.detect_rtl_bullet(line)
            return
        spans = line.spans
        if len(spans) < 2:
            first = None
        else:
            first, nxt = spans[0], spans[1]
            gap = nxt.rect.x0 - first.rect.x1
            token = first.text.strip()
            on_ball = any(ir.contains(first.rect.cx, first.rect.cy) for _, ir in self.small_images)
            if first.info.family == "icon" and gap >= 0.25 * line.size and max(first.rect.w, first.rect.h) <= 1.6 * line.size:
                # \item[\ding{43}]: no Slides glyph, and the font's glyph stays in the background.
                # classify turns it into a picture grouped with the item text.
                line.bullet = {"kind": "icon", "text": "", "bbox": first.rect.as_list(), "spans": [first.id]}
                line.bullet_spans, line.tab = [first], None
                return
            lettered =ENUM_RE.match(token) and not any(ch.isdigit() for ch in token)  # a) (b) iv.
            # A dash followed by a word space opens an attribution ("--- Richard Thaler") or a
            # line of dialogue; an \item[--] label is set off by \labelsep, wider than a space.
            dash = token in ("—", "–") and gap < LABEL_SEP_EM * line.size
            if gap >= 0.25 * line.size and ((token in LABEL_GLYPHS - PRESET_GLYPHS) or lettered) and not on_ball \
                    and not dash:
                # \item[--], \item[\checkmark]: Slides has no such bullet preset. The glyph stays
                # literal text, and a tab reaches the item text (hanging indent).
                line.tab = nxt
                return
            if gap >= 0.25 * line.size and (token in BULLET_GLYPHS or ENUM_RE.match(token)) and not on_ball and not dash:
                kind = "glyph" if token in BULLET_GLYPHS else "number"
                line.bullet = {"kind": kind, "text": token, "color": first.color, "bbox": first.rect.as_list(),
                               "label": label_of([first])}
                line.bullet_spans = [first]
                return
            # Numbers drawn on a small box or circle (e.g. Bergen's enumerate): the box is
            # patched out of the background and replaced by a native numbered bullet.
            if gap >= 0.25 * line.size and re.fullmatch(r"[0-9]{1,3}|[a-zA-Z]|[ivxl]{1,5}|[IVXL]{1,5}", token):
                for g in self.graphics:
                    if g.w <= 1.6 * line.size and g.h <= 1.6 * line.size and g.contains(first.rect.cx, first.rect.cy):
                        line.bullet = {"kind": "number", "text": token, "color": first.color,
                                       "bbox": g.as_list(), "patch": True, "label": label_of([first])}
                        line.bullet_spans = [first]
                        return
        # Image bullets (ball themes): a small image just left of the text, level with its
        # x-height. Numbered balls draw the digit as a small text span on top of the image.
        for im, ir in self.small_images:
            # A block shadow's corner piece may just graze a ball at the bottom of a block.
            if not 0.8 <= ir.w / max(ir.h, 0.01) <= 1.25 or \
                    any(o is not im and max(orr.w, orr.h) <= 20 and (overlap(orr, ir) > 0.2 * ir.w * ir.h or ir.contains_rect(orr))
                        for o, orr in self.small_images):
                continue  # icons (beamer's bibliography article, composite images): kept as pictures
            on_image = [s for s in spans if ir.expand(0.5).contains(s.rect.cx, s.rect.cy)]
            rest = [s for s in spans if s not in on_image]
            if not rest:
                continue
            x0 = min(s.rect.x0 for s in rest)
            if ir.x1 <= x0 + 0.5 and x0 - ir.x1 <= 1.5 * line.size and \
                    line.baseline - 0.9 * line.size <= ir.cy <= line.baseline + 0.1 * line.size:
                # A label on the ball (1, (a), iv.): literal_list_numbers centres it on the ball picture.
                token = "".join(s.text.strip() for s in sorted(on_image, key=lambda s: s.rect.x0))
                line.bullet = {"kind": "image", "image": im["id"], "text": token, "bbox": ir.as_list(),
                               "label": label_of(on_image)}
                line.bullet_spans = on_image
                return
        # Vector bullets (shaded balls, squares drawn as paths): a small, roughly square
        # graphic just left of the text at x-height (Bergen hangs subitem squares 1.8 em out).
        x0 = min(s.rect.x0 for s in spans)
        for g in self.graphics:
            if 0.25 * line.size <= g.w <= 1.3 * line.size and 0.25 * line.size <= g.h <= 1.6 * line.size \
                    and 0.5 <= g.w / g.h <= 2.0 \
                    and g.x1 <= x0 + 0.5 and x0 - g.x1 <= 2.0 * line.size \
                    and line.baseline - 0.9 * line.size <= g.cy <= line.baseline + 0.1 * line.size \
                    and self.stands_alone(g, spans[0].rect):
                shape = bullet_shape(self.graphic_paths.get(tuple(g.as_list())))
                # (a mark with parts drawn inside it - a globe's meridians in its disc - is no glyph)
                if shape and not any(g.contains_rect(o) and not o.contains_rect(g) for o in self.graphics if o is not g):
                    line.bullet = {"kind": "shape", "text": "", "bbox": g.as_list(), "patch": True, **shape}
                else:  # no Slides glyph looks like it (beamer's bibliography icon): a picture
                    icon = union_all([g] + [ir for _, ir in self.small_images if ir.intersects(g)])
                    line.bullet = {"kind": "icon", "text": "", "bbox": icon.as_list(), "spans": []}
                return

    def detect_rtl_bullet(self, line: Line) -> None:
        """`detect_bullet` for a line read right to left (a Hebrew or Arabic item): its bullet
        hangs right of its words, where the item starts - a glyph, a ball image (with its
        number drawn on it) or a vector ball - by the same measures mirrored. Nothing at the
        left end of such a line is a bullet: that is where its sentence ends. Left alone, the
        balls stayed in the background, the items of a list ran together, and two columns of
        them were taken for a table's cells."""
        spans = line.spans
        if len(spans) >= 2:
            last, prev = spans[-1], spans[-2]
            token = last.text.strip()
            on_ball = any(ir.contains(last.rect.cx, last.rect.cy) for _, ir in self.small_images)
            if last.rect.x0 - prev.rect.x1 >= 0.25 * line.size and token in BULLET_GLYPHS - {"–"} and not on_ball:
                line.bullet = {"kind": "glyph", "text": token, "color": last.color, "bbox": last.rect.as_list(),
                               "label": label_of([last])}
                line.bullet_spans = [last]
                return
        level = lambda r: line.baseline - 0.9 * line.size <= r.cy <= line.baseline + 0.1 * line.size
        # (a numbered ball comes out 11 or 12 pt as its image's box is rounded: one of 12 pt is no
        # small image, and was taken for a graphic around a word, a hole)
        balls = self.small_images + [(im, r) for im, r in ((im, Rect.of(im["bbox"])) for im in self.page["images"])
                                     if min(r.w, r.h) >= SMALL_IMAGE_PT and max(r.w, r.h) <= 1.25 * line.size]
        for im, ir in balls:
            if not 0.8 <= ir.w / max(ir.h, 0.01) <= 1.25 or \
                    any(o is not im and max(orr.w, orr.h) <= 20 and (overlap(orr, ir) > 0.2 * ir.w * ir.h or ir.contains_rect(orr))
                        for o, orr in self.small_images):
                continue
            on_image = [s for s in spans if ir.expand(0.5).contains(s.rect.cx, s.rect.cy)]
            rest = [s for s in spans if s not in on_image]
            if not rest:
                continue
            x1 = max(s.rect.x1 for s in rest)
            if ir.x0 >= x1 - 0.5 and ir.x0 - x1 <= 1.5 * line.size and level(ir):
                token = "".join(s.text.strip() for s in sorted(on_image, key=lambda s: s.rect.x0))
                line.bullet = {"kind": "image", "image": im["id"], "text": token, "bbox": ir.as_list(),
                               "label": label_of(on_image)}
                line.bullet_spans = on_image
                return
        x1 = max(s.rect.x1 for s in spans)
        for g in self.graphics:
            if 0.25 * line.size <= g.w <= 1.3 * line.size and 0.25 * line.size <= g.h <= 1.6 * line.size \
                    and 0.5 <= g.w / g.h <= 2.0 and g.x0 >= x1 - 0.5 and g.x0 - x1 <= 2.0 * line.size \
                    and level(g) and self.stands_alone(g, spans[-1].rect):
                shape = bullet_shape(self.graphic_paths.get(tuple(g.as_list())))
                if shape and not any(g.contains_rect(o) and not o.contains_rect(g) for o in self.graphics if o is not g):
                    line.bullet = {"kind": "shape", "text": "", "bbox": g.as_list(), "patch": True, **shape}
                else:
                    icon = union_all([g] + [ir for _, ir in self.small_images if ir.intersects(g)])
                    line.bullet = {"kind": "icon", "text": "", "bbox": icon.as_list(), "spans": []}
                return

    def stands_alone(self, g: Rect, word: Rect) -> bool:
        """A graphic that can be an item's bullet: nothing else is drawn at it but its own
        parts and what lies behind the whole item (a box around the list). A dot on a
        timeline's rail, a scatter mark whose label runs out of the plot frame, an arch of a
        logo drawn on its disc beside the wordmark: part of a drawing, which a native bullet
        patched out of the background broke. (A rail running over half the page is taken
        for theme decoration.)"""
        for o in self.graphics + self.decorations:
            if o is g or not o.intersects(g.expand(1.0)) or g.contains_rect(o) or \
                    (o.contains_rect(g) and o.contains_rect(word)):
                continue
            return False
        return True

    @staticmethod
    def label_tabs(lines: list[Line]) -> None:
        """Any short label (\\item[\\textbf{Q:}]) ending where a neighbouring item's hanging label
        ends, with its text starting where that item's text does, hangs the same way."""
        tabbed = [l for l in lines if l.tab is not None and l.reason is None]
        for line in lines:
            if line.reason is not None or line.tab is not None or line.bullet or len(line.spans) < 2:
                continue
            label, text = line.spans[0], line.spans[1]
            if len(label.text.strip()) > 6 or text.rect.x0 - label.rect.x1 < LABEL_SEP_EM * line.size:
                continue
            for other in tabbed:
                before = [s for s in other.spans if s.rect.x1 <= other.tab.rect.x0]
                if before and abs(other.baseline - line.baseline) <= 5 * line.size and abs(other.size - line.size) <= 0.5 \
                        and abs(other.tab.rect.x0 - text.rect.x0) <= 0.6 and abs(before[-1].rect.x1 - label.rect.x1) <= 0.6:
                    line.tab = text
                    break

    def continues_prose(self, line: Line) -> bool:
        """A short all-math line that is really the wrapped end of a text line above it
        (same left edge, one line pitch higher) - not a display equation."""
        for other in self.all_lines:
            if other is line or other.reason is not None:
                continue
            pitch = line.baseline - other.baseline
            # Any word of the line above may start the text column (theorem labels can hang left).
            aligned = any(abs(s.rect.x0 - line.rect.x0) <= 1.5 for s in other.spans)
            if 0 < pitch <= 1.4 * line.size and aligned and len(other.text.replace(" ", "")) >= 20:
                return True
        return False

    def in_prose_flow(self, line: Line) -> bool:
        """One of a paragraph's lines: a line of words of the same size a line pitch or two above
        or below starts where it starts. A display formula is set apart from its paragraph and
        centred (or indented), never flush with the words around it."""
        if line.bullet or line.tab is not None:
            return True
        size = line.size
        for o in self.all_lines:
            if o is line or not o.spans[0].horizontal or o.reason in ("theme", "figure", "rotated") or \
                    abs(o.size - size) > 0.2 * size:
                continue
            # (any of its words: an item's first line starts with its label, "4. Let ...")
            if 0.8 * size <= abs(o.baseline - line.baseline) <= 2.2 * size \
                    and any(abs(s.rect.x0 - line.x0) <= 1.5 for s in o.content) \
                    and prose_share(o.content) >= DISPLAY_WORD_SHARE:
                return True
        return False

    def same_formula(self, line: Line, maths: list[Line]) -> bool:
        """A piece of a display formula whose complex part is a math line: build_lines split
        it where a big operator hangs from its origin (off the baseline) or at a \\qquad.

        - Beside it on its row ("x⊤Lx =" | ∑ | "(xu − xv)2" | "for all x ∈ ℝn"): the row is where
          the math line's glyphs cover the line's x-height, not where its baseline is (a CMEX
          glyph's origin is well above the words'). A formula piece joins up to 4 em away; a few
          words ("#{eigenvalues of ...}") only right next to it, and only out of a paragraph's flow.
        - Under or over it, another row of the same display (align*'s "= 1 − 0.9332"): a formula
          line out of the flow, overlapping it across, no more than half a line apart."""
        size = line.size
        share = prose_share(math_content(line))
        names = WORD_RE.findall(line.text)
        formula = share < DISPLAY_WORD_SHARE and (line.inline_math or any(
            ch in MATH_OPERATORS for ch in line.text) or any(s.info.family == "math" for s in line.spans)
            or bool(names) and all(w in OPERATOR_NAMES for w in names))  # a lone "min" of a display
        few_words = line.inline_math and len(WORD_RE.findall(line.text)) <= 4
        # (the full stop a display ends on, right after a big delimiter that hangs from its
        # origin a line above)
        if line.text.strip() and set(line.text.replace(" ", "")) <= set(",.;:") and any(
                line.rect.x0 - size <= s.rect.x1 <= line.rect.x0 + 0.5 and abs(s.rect.cy - line.rect.cy) <= 2 * size
                for m in maths for s in m.spans if extension_font(s.font)) and not self.in_prose_flow(line):
            return True
        if not (formula or few_words):
            return False
        # (inside a big delimiter: a cases' rows start at its brace, however much they look like
        # a paragraph of two lines to each other)
        if any(s.rect.x1 - 0.5 <= line.rect.x0 <= s.rect.x1 + 0.5 * size and line.rect.y1 > s.rect.y0
               and line.rect.y0 < s.rect.y1 + 1.2 * s.size and s.rect.h >= 0.8 * size
               for m in maths for s in m.spans if extension_font(s.font)):
            return True
        flow = None
        core = (line.baseline - 0.5 * size, line.baseline)
        for m in maths:
            gap = max(0.0, m.rect.x0 - line.rect.x1, line.rect.x0 - m.rect.x1)
            level = abs(m.baseline - line.baseline) <= 0.3 * size
            if level and line.inline_math and gap <= 4 * size:
                return True  # ("L(θ) =" left of its complex part, on one baseline)
            # (a big operator or delimiter's box is an em at its origin; its ink hangs another
            # em below: a display \left( \int of "‖f‖p = (∫ |f|^p dµ)^{1/p}")
            reach = max((s.rect.y1 + 1.2 * s.size for s in m.spans if extension_font(s.font)), default=m.rect.y1)
            on_row = level or min(max(m.rect.y1, reach), core[1]) - max(m.rect.y0, core[0]) >= 0.3 * size
            if on_row:
                # (words go on from the formula, "... #{eigenvalues of H ≤ E}", "... for all n";
                # words before it are the sentence leading into the display)
                if formula and gap <= 4 * size or line.rect.x0 >= m.rect.x1 - 1 and gap <= 1.0 * size:
                    flow = self.in_prose_flow(line) if flow is None else flow
                    if not flow:
                        return True
                continue
            across = min(m.rect.x1, line.rect.x1) - max(m.rect.x0, line.rect.x0)
            # (an align row set on its relation: under one, up to a line apart - align* opens
            # its rows up to 1.5 lines)
            aligned = formula and line.text.lstrip()[:1] in RELATIONS and any(
                abs(s.rect.x0 - line.rect.x0) <= 1.5 and s.text.lstrip()[:1] in RELATIONS for s in m.spans)
            # (a few words in a column of the display: a cases' "otherwise")
            if (formula and across >= 0.3 * min(line.rect.w, m.rect.w) or few_words and any(abs(s.rect.x0 - line.rect.x0) <= 1.5 for s in m.spans)) \
                    and max(line.rect.y0 - m.rect.y1, m.rect.y0 - line.rect.y1) <= (1.0 if aligned else 0.5) * size:
                flow = self.in_prose_flow(line) if flow is None else flow
                if not flow:
                    return True
        return False

    def wrapped_formula(self, line: Line) -> bool:
        """A paragraph's inline formula wrapped onto a line of its own ("(O(√n))" under an item's
        "Separator theorems"): no bullet or label of its own, one pitch below a line of words in
        its size, starting where one of that line's words starts. A display formula is centred
        or indented, never flush with its paragraph. As a display, the item it ended became one
        picture, bullet and words included."""
        if line.bullet or line.tab is not None:
            return False
        size = line.size
        return any(o is not line and o.spans[0].horizontal and o.reason not in ("theme", "figure", "rotated", "math")
                   and abs(o.size - size) <= 0.2 * size
                   and 0.8 * size <= line.baseline - o.baseline <= 1.6 * size
                   and any(abs(s.rect.x0 - line.x0) <= 1.5 for s in o.content)
                   and prose_share(o.content) >= DISPLAY_WORD_SHARE for o in self.all_lines)

    def display_line(self, line: Line) -> bool:
        """A line that is a display formula: mostly formula (few words of prose, see prose_share)
        and set apart from any paragraph's flow."""
        return prose_share(math_content(line)) < DISPLAY_WORD_SHARE and not self.in_prose_flow(line)

    def simple_fraction(self, line: Line, bar: Rect):
        """(bar, numerator spans, denominator spans) for a small inline fraction such as
        \\frac{1}{2}: short text directly above and below a short bar, no radical sign."""
        above = [s for s in line.content if s.rect.x0 >= bar.x0 - 1 and s.rect.x1 <= bar.x1 + 1
                 and s.rect.cy < bar.cy and s.size < 0.85 * line.size]
        below = [s for s in line.content if s.rect.x0 >= bar.x0 - 1 and s.rect.x1 <= bar.x1 + 1
                 and s.rect.cy > bar.cy and s.size < 0.85 * line.size]
        if not above or not below:
            return None
        if len("".join(s.text for s in above + below).replace(" ", "")) > 6:
            return None
        if any("√" in s.text for s in line.content if abs(s.rect.x1 - bar.x0) < 3):
            return None  # radical overbar
        by_x = lambda group: sorted(group, key=lambda s: s.rect.x0)
        return bar, by_x(above), by_x(below)

    def line_bars(self, line: Line) -> list[Rect]:
        """Fraction bars and radical overbars at a line - not the overbar of a radical sign set in
        another line of words (a √ of the line below reaches up into this one; a sign alone on a
        line of its own is this line's, see formula holes)."""
        signs = [s for l in self.all_lines if l is not line and sum(o.size >= 0.8 * line.size for o in l.spans) >= 2
                 for s in l.spans if extension_font(s.font) or "√" in s.text]
        return [b for b in self.bars if b.expand(1).intersects(line.rect) and not any(
            abs(s.rect.x1 - b.x0) <= 1 and s.rect.y0 - 1 <= b.y0 <= s.rect.y1 for s in signs)]

    def formula_holes(self, line: Line, fractions: list) -> list[list[Span]]:
        """Complex formulas inside a line of prose, as groups of spans; [] if there are none or
        if the line is not mostly prose (a display equation stays one picture)."""
        spans = sorted(math_content(line), key=lambda s: s.rect.x0)
        size = line.size
        bars = self.line_bars(line)
        simple_bars = [f[0] for f in fractions]
        in_fraction = {id(s) for f in fractions for s in f[1] + f[2]}

        def mathish(s: Span) -> bool:
            t = s.text.strip()
            return (s.info.family == "math" or bool(script_of(s, line)) or extension_font(s.font)
                    or bool(t) and all(w in OPERATOR_NAMES for w in t.split())  # "lim sup" of a formula
                    or "�" in s.text or (s.info.italic and len(t) <= 2)
                    or any(b.expand(0.5).intersects(s.rect) and s.rect.cy > b.cy for b in bars)
                    or (bool(t) and all(ch in MATH_OPERATORS or ch in "()[]{}|∥,.;:'ˆ˜¯^0123456789 " for ch in t)))

        segments: list[list[Span]] = []
        for s in spans:
            if not mathish(s):
                segments.append([])
                continue
            if segments and segments[-1]:
                segments[-1].append(s)
            else:
                segments.append([s])
        segments = [seg for seg in segments if seg]
        # (operator names are the formula's: "ln Q = α + β Reform" has one word)
        words = [s.text.strip() for s in spans if not any(s in seg for seg in segments) and s.text.strip() not in OPERATOR_NAMES]
        prose_words = [w for w in WORD_RE.findall(" ".join(words)) if len(w) >= 3 and w not in OPERATOR_NAMES]
        if sum(sum(ch.isalpha() for ch in w) >= 2 for w in words) < 2 and sum(map(len, words)) < 8 and not (
                prose_words and self.in_prose_flow(line) and line.tab is None and not line.bullet) and \
                not self.wrapped_formula(line):
            # hardly any words ("f(x) = √x if x ≥ 0"): a display equation, one picture - but a
            # paragraph's line that is mostly formula ("Then / f ∈ L¹(µ) and ∫|fn − f| dµ → 0.")
            # keeps its words
            return []
        if self.display_line(line):
            return []  # a display with a few words in it (\text{for all}, "#{eigenvalues of ...}")

        def complex_segment(seg: list[Span]) -> bool:
            rect = union_all(s.rect for s in seg)
            if any(extension_font(s.font) or "�" in s.text for s in seg):
                return True
            if any(b.expand(1).intersects(rect) and not any(abs(b.x0 - sb.x0) < 0.1 and abs(b.y0 - sb.y0) < 0.1
                                                              for sb in simple_bars) for b in bars):
                return True
            if any(accent_beside(a, b) for a in seg for b in seg if a is not b):
                return True
            scripts = [s for s in seg if script_of(s, line) and id(s) not in in_fraction]
            if any(s.size < 0.6 * size or abs(s.baseline - line.baseline) > 0.6 * size for s in scripts):
                return True
            return any(a is not b and script_of(a, line) != script_of(b, line)
                       and a.rect.x0 < b.rect.x1 - 0.5 and b.rect.x0 < a.rect.x1 - 0.5 for a in scripts for b in scripts)

        holes = []
        for seg in segments:
            if complex_segment(seg):
                # Trailing punctuation is prose again.
                while len(seg) > 1 and seg[-1].text.strip() in (",", ".", ";", ":"):
                    seg = seg[:-1]
                holes.append(seg)
        return holes

    def graphic_holes(self, line: Line) -> bool:
        """Words drawn in or on a small graphic in a line of prose (a TikZ circle or badge, a
        keycap, an \\fbox): the graphic would stay where the PDF has it while Slides sets the
        words at other widths, so words and graphic become one picture over a gap in the text,
        like a formula hole. True if the line got such holes."""
        size = line.size
        spans = [s for s in line.spans if s.text.strip()]
        if len(spans) < 3:
            return False
        if not hasattr(self, "_word_graphics"):
            # (an \fbox's top and bottom rules look like a table of one line)
            frames = [f for f in (union_all(r["rect"] for r in g).expand(1) for g in self.table_rules) if f.h > 2.5 * self.body]
            self._word_graphics = [c for c in cluster_rects(self.graphics, gap=0.5)
                                   if not any(f.contains_rect(c) for f in frames)] if self.graphics else []
        groups = []
        for g in self._word_graphics:
            if g.h > 2.2 * size or not line.baseline - size <= g.cy <= line.baseline + 0.4 * size:
                continue
            touched = [s for s in spans if min(s.rect.x1, g.x1) - max(s.rect.x0, g.x0) > 0.3 * s.rect.w]
            # (a frame closed around its words may be wider: \framebox[2.5cm])
            framed = touched and g.contains_rect(union_all(s.rect for s in touched), tol=0.5) and g.w <= 0.5 * self.W
            if not touched or (g.w > sum(s.rect.w for s in touched) + 2 * size and not framed):
                continue  # nothing on it, or a rule or frame reaching well past the words
            if touched == (spans[-1:] if reads_rtl(line) else spans[:1]):
                continue  # a label on a box at the line start: a list number (detect_bullet)
            if self.cuts_words(g, spans):
                continue  # drawn over the line, not set in it (see overlay)
            groups.append((touched, g))
        # Glyphs set on top of each other on one baseline (\textcircled: a circle glyph over a
        # letter) would come apart as text.
        for i, a in enumerate(spans):
            for b in spans[i + 1:]:
                if not any(t[:1] in ACCENTS or t[-1:] in ACCENTS for t in (a.text.strip(), b.text.strip())) and \
                        abs(a.baseline - b.baseline) <= 0.3 * size and \
                        min(a.rect.x1, b.rect.x1) - max(a.rect.x0, b.rect.x0) > 0.5 * min(a.rect.w, b.rect.w) > 0:
                    groups.append(([a, b], union_all([a.rect, b.rect])))
        outside = [s for s in spans if not any(s in t for t, _ in groups)]
        if not groups or sum(sum(ch.isalpha() for ch in s.text) >= 2 for s in outside) < 2:
            return False
        line.hole_pads = [g for _, g in groups]
        line.add_holes([t for t, _ in groups])
        return True

    @staticmethod
    def cuts_words(g: Rect, spans: list[Span]) -> bool:
        """A graphic reaching into a word's letters: drawn over the text with TikZ's overlay
        (an emphasis ellipse wider than its word), where a box set in the line (\\fbox, a
        circled number) keeps clear of its neighbours."""
        return any(0.15 * s.rect.w < min(s.rect.x1, g.x1) - max(s.rect.x0, g.x0) < 0.85 * s.rect.w
                   and g.y0 < s.rect.y1 and s.rect.y0 < g.y1 for s in spans)

    def math_kind(self, line: Line) -> str | None:
        """None for plain text, 'inline' for math that Slides text can carry (symbols,
        single-level sub/superscripts), 'complex' for anything that must stay a picture."""
        spans = math_content(line)
        line_bars = self.line_bars(line)  # fractions, radicals
        fractions = [f for f in (self.simple_fraction(line, b) for b in line_bars) if f]
        if len(fractions) == len(line_bars):
            line.fractions = fractions  # all bars are simple a/b fractions: text can carry them
            bars = False
        else:
            bars = True
        in_fraction = {id(s) for _, num, den in fractions for s in num + den}
        scripts = [s for s in spans if script_of(s, line) and id(s) not in in_fraction]
        math_font = any(s.info.family == "math" for s in spans)
        chars = "".join(s.text for s in spans).replace(" ", "")
        mathy = sum(len(s.text.strip()) for s in spans if s.info.italic and len(s.text.strip()) <= 2)
        mathy += sum(ch in MATH_OPERATORS for ch in chars)
        # (a display equation, not prose; nor code: '>>> a + b' is a REPL line, r2_code_v4 s6)
        formula_like = len(chars) > 0 and mathy / len(chars) >= 0.4 and not is_code(line.content)
        # (glyphs with no Unicode in what the line says: its icon bullet is no formula - an item
        # with a \faCheck bullet became one picture of its words, bullet included)
        tofu = "�" in chars

        if not (math_font or scripts or bars or fractions or formula_like or tofu):
            return None
        holes = self.formula_holes(line, fractions)
        if holes:
            # Prose with a few complex formulas: the words stay text, each formula becomes a
            # picture placed over a gap left in the text.
            line.add_holes(holes)
            hole_ids = {id(s) for h in line.holes for s in h}
            line.fractions = [f for f in fractions if not any(id(s) in hole_ids for s in f[1] + f[2])]
            return "inline"
        if bars or tofu:
            return "complex"
        if formula_like and not (self.continues_prose(line) or self.wrapped_formula(line)):
            return "complex"
        if any(extension_font(s.font) for s in spans):
            return "complex"  # big operators, large delimiters
        if any(s.size < 0.6 * line.size or abs(s.baseline - line.baseline) > 0.6 * line.size for s in scripts):
            return "complex"  # second-level scripts, limits
        for a in scripts:
            for b in scripts:
                if a is not b and script_of(a, line) != script_of(b, line) and \
                        a.rect.x0 < b.rect.x1 - 0.5 and b.rect.x0 < a.rect.x1 - 0.5:
                    return "complex"  # sub and superscript stacked (a_1^2)
        return "inline"

    def assign_reasons(self, lines: list[Line]) -> None:
        self.all_lines = lines
        for line in lines:
            if not all(s.horizontal for s in line.spans):
                line.reason = "rotated"
            elif self.graphic_holes(line):
                pass  # prose with boxed or circled words
            elif self.inside_figure_share(line) >= 0.5 and not self.prose_under_graphic(line):
                line.reason = "figure"
            elif line.size <= 0.7 * self.body and (line.rect.y1 <= 0.13 * self.H or line.rect.y0 >= 0.87 * self.H):
                line.reason = "theme"
            elif line.size < 0.78 * self.body and self.on_edge_artwork(line.rect):
                line.reason = "theme"  # sidebar navigation, header/footer info

        # Bullets first: a short list item next to its icon bullet is not a figure label.
        for line in lines:
            if line.reason is None:
                self.detect_bullet(line)
        self.label_tabs(lines)

        # Short labels next to figures (axis ticks, axis labels) belong to the figure, and so does
        # a row of widely spaced short pieces however long it is: an axis's tick labels
        # ("200  400  600  800  1,000"). Left as text, such a row turned the chart into an overlay
        # anchored to it, and emit moved and stretched the chart after Slides' words.
        # (An ornament of the header/footer band - a title's accent bar, navigation symbols -
        # becomes no picture, and a bare vertical rule - a column separator, a listing's frame,
        # a quote bar - labels no words, only numbers: a listing's line numbers go with its
        # gutter, a scale's with its axis, and they are no figure for the code beside them.)
        regions = [r for r in self.regions if not self.band_ornament(r)] + \
            [l.rect for l in self.axis_label_column(lines)]
        bare_rule = lambda r: r.w <= 4 and r.h >= 25
        joined: list[Line] = []
        changed = True
        while changed:
            changed = False
            for line in lines:
                if line.reason is not None or line.bullet or line.tab is not None or line.size > 1.15 * self.body or \
                        not (len(line.text.replace(" ", "")) <= 12 or self.tick_row(line)) or self.wrapped_end(line, lines):
                    continue
                words = [s for s in line.content if s.text.strip() and not TICK_NUMBER_RE.fullmatch(s.text.strip())]
                near = [reg for reg in regions if reg.distance(line.rect) <= 0.8 * line.size]
                if any(not bare_rule(reg) for reg in near) or (near and not words):
                    line.reason = "figure"
                    if any(not bare_rule(reg) for reg in near):
                        regions.append(line.rect)
                    joined.append(line)
                    changed = True
        self.release_stranded_labels(lines, joined)
        self.axis_titles(lines)

        for line in lines:
            if line.reason is None:
                kind = self.math_kind(line)
                if kind == "complex":
                    line.reason = "math"
                line.inline_math = kind == "inline"

        # The limits of a big operator in a line's formula hole (an inline \sum\limits, a
        # \displaystyle one) are lines of their own above and below the line: they go into that
        # hole's picture, or the lower one stayed text and the upper one in the background.
        for host in lines:
            size = host.size
            for g in (s for h in host.holes for s in h if extension_font(s.font)):
                for l in lines:
                    if l is host or l.bullet or l.reason not in (None, "math") or l.size >= 0.85 * size or \
                            len(l.text.replace(" ", "")) > 24 or any(l in o.limits for o in lines):
                        continue
                    # (a lower limit under the host line's scripts too: the glyph's box hangs high)
                    if g.rect.x0 - 0.5 * size <= l.rect.cx <= g.rect.x1 + 0.5 * size and (
                            -size <= g.rect.y0 - l.rect.y1 <= 0.5 * size or
                            -size <= l.rect.y0 - max(g.rect.y1, host.rect.y1) <= 0.75 * size):
                        l.reason = "math"
                        host.limits.append(l)

        # Pieces of display math: limits, equation numbers, small italic fragments next to math.
        changed = True
        while changed:
            changed = False
            maths = [l for l in lines if l.reason == "math"]
            for line in lines:
                if line.reason is not None or line.bullet:
                    continue
                txt = line.text.replace(" ", "")
                # Big operators (CMEX) reach further than their glyph boxes: limits sit below them.
                near = any(m.rect.expand(1.0 * max(m.size, line.size)
                                         if any(extension_font(s.font) for s in m.spans)
                                         else 0.6 * line.size).intersects(line.rect) for m in maths)
                # Numerator or denominator: a short line right at a fraction bar next to a display
                # formula (a fraction inside prose stays with its line, see formula_holes).
                # (a line of words beside the bar vetoes: the fraction is inline, in its prose -
                # words in the next column at the bar's height do not)
                fraction_part = len(txt) <= 6 and any(
                    b.x0 - 1 <= line.rect.cx <= b.x1 + 1 and min(abs(line.rect.y1 - b.y0), abs(line.rect.y0 - b.y1)) <= 0.6 * line.size
                    and any(m.rect.expand(line.size).intersects(b) for m in maths)
                    and not any(o.reason != "math" and len(o.text.split()) >= 3 and o.rect.y0 - 1 <= b.y0 <= o.rect.y1 + 1
                                and o.rect.x0 - line.size <= b.x1 and b.x0 <= o.rect.x1 + line.size
                                for o in lines)
                    for b in self.bars)
                small = line.size < 0.9 * self.body or all(s.info.italic for s in line.content)
                eqno = EQ_NUMBER_RE.match(txt) and any(abs(m.baseline - line.baseline) <= 3 for m in maths)
                # A big operator's limits, however long ("{u,v}∈E", "|y−x|=1"): small, right under
                # or over the operator's glyph.
                limit = small and len(txt) <= 24 and any(
                    g.rect.x0 - 0.5 * line.size <= line.rect.cx <= g.rect.x1 + 0.5 * line.size
                    and max(line.rect.y0 - m.rect.y1, m.rect.y0 - line.rect.y1) <= 1.0 * max(m.size, line.size)
                    for m in maths for g in m.spans if extension_font(g.font))
                if (near and small and len(txt) <= 6) or eqno or limit or self.same_formula(line, maths) or fraction_part:
                    line.reason = "math"
                    line.inline_math = False
                    line.holes = []
                    changed = True

    def axis_label_column(self, lines: list[Line]) -> list[Line]:
        """A bar chart's category labels on its y axis ("Carrier handover delayed at hub"):
        three or more lines set flush right against a plot, one above the other, ragged on the
        left - too long for a tick label, but as text boxes they re-wrapped in a wider font and
        grew over the next label, and a chart drawn at them became an overlay stretched after
        their words. A value printed inside the first bar that line building joined to its
        label ends the line inside the plot; the label still ends at the axis. A paragraph
        beside a figure starts its lines together."""
        def inside(s: Span) -> bool:
            return any(reg.expand(0.5).contains(s.rect.cx, s.rect.cy) for reg in self.regions)

        rows = []
        for line in lines:
            outside = [s for s in line.content if s.text.strip() and not inside(s)]
            if line.reason is not None or line.bullet or line.tab is not None or not outside or \
                    len(line.text.split()) > 8 or line.size > 1.15 * self.body:
                continue
            x1 = max(s.rect.x1 for s in outside)
            plot = [reg for reg in self.regions if x1 - 1 <= reg.x0 <= x1 + line.size and
                    reg.y0 - line.size <= line.rect.cy <= reg.y1 + line.size]
            if plot:
                rows.append((line, x1, plot))
        out = []
        self.column_bridges = []  # a label up to an em off its plot: what `figures` clusters by
        for line, x1, plot in rows:
            column = [(l, e) for l, e, _ in rows if abs(e - x1) <= 0.6]
            starts = [min(s.rect.x0 for s in l.content) for l, _ in column]
            # (short labels only - "Q1" "Q2" beside a timeline - are left to the tick-label rule)
            if len({round(l.baseline) for l, _ in column}) >= 3 and max(starts) - min(starts) >= 2 and \
                    any(len(l.text.replace(" ", "")) > 12 for l, _ in column):
                line.reason = "figure"
                out.append(line)
                self.column_bridges.append(union_all([line.rect] + plot))
        return out

    @staticmethod
    def tick_row(line: Line) -> bool:
        """Short pieces far more than a word space apart: tick labels. Three or more, most of
        them apart by 0.8 em (not all: centred ticks close up where a label is wider, "800
        1,000"); or four or more at one pitch, centre to centre, wider apart than a word space
        (month names, years: "Jan Feb Mar" sit half an em apart under their bars); or two a
        whole em apart (the part of a date axis that joined up, "01/2026   03/2026"). A number
        is one label however long ("1,0001,0501,100": labels that touch)."""
        pieces: list[list] = []  # [text, x0, x1]: spans that touch are one label ("1" "," "000")
        for s in (s for s in line.content if s.text.strip()):
            if pieces and s.rect.x0 - pieces[-1][2] < 0.25 * line.size:
                pieces[-1][0] += s.text.strip()
                pieces[-1][2] = max(pieces[-1][2], s.rect.x1)
            else:
                pieces.append([s.text.strip(), s.rect.x0, s.rect.x1])
        if len(pieces) < 2 or not all(len(p[0]) <= 10 or TICK_NUMBER_RE.fullmatch(p[0]) for p in pieces):
            return False
        gaps = [b[1] - a[2] for a, b in zip(pieces, pieces[1:])]
        wide = sum(g >= 0.8 * line.size for g in gaps)
        if len(pieces) == 2:
            return gaps[0] >= 1.0 * line.size
        if wide >= 2 and wide >= 0.6 * len(gaps):
            return True
        # (labels that ran together at the end of a row, "1,0001,0501,100", break the pitch
        # once: four in five pitches on the beat is a row)
        pitches = [(b[1] + b[2] - a[1] - a[2]) / 2 for a, b in zip(pieces, pieces[1:])]
        middle = statistics.median(pitches)
        beat = [g for p, g in zip(pitches, gaps) if abs(p - middle) <= max(0.75, 0.04 * middle)]
        return len(pieces) >= 4 and len(beat) >= 3 and len(beat) >= 0.8 * len(pitches) and \
            min(beat) >= 0.45 * line.size

    def axis_titles(self, lines: list[Line]) -> None:
        """A plot's title and axis titles ("Month of the year 2026", "Temperature") stand further
        off than a tick label, often pushed away on purpose, and are longer than one: short lines
        centred on a *plot* - a drawing that carries at least three tick labels - belong to its
        picture. As text boxes they are set left-aligned in a wider font and lean off the axis
        they name, and they would stay behind when the chart is moved. A caption ("Figure 2:")
        and a sentence stay text, and so does anything by a drawing no labels mark as a plot."""
        # What joins each title to its plot, for `figures` to cluster by: a title stands further
        # off than the clustering gap, and a label no picture holds would stay in the background.
        self.title_bridges = []
        labels = [l for l in lines if l.reason in ("figure", "rotated")]
        plots = []
        for reg in self.regions:
            box, marks, grown = reg, 0, True
            members = set()
            while grown:
                grown = False
                for l in labels:
                    if id(l) not in members and box.distance(l.rect) <= 0.8 * l.size:
                        members.add(id(l))
                        box = union_all([box, l.rect])
                        marks += max(1, len([s for s in l.content if s.text.strip()]))
                        grown = True
            if marks >= 3:
                plots.append((reg, box))
        if not plots:
            return
        for line in lines:
            text = line.text.strip()
            turned = line.reason == "rotated"  # a y axis title, read bottom to top
            if (line.reason is not None and not turned) or line.bullet or line.tab is not None or not text or \
                    len(text.split()) > 6 or line.size > 1.3 * self.body or CAPTION_RE.match(text) or \
                    (text.endswith(".") and len(text.split()) >= 4):
                continue
            r = line.rect
            for reg, box in plots:
                if turned:
                    centred = abs(reg.cy - r.cy) <= max(2.0, 0.03 * reg.h) and r.h <= reg.h
                    gap = max(box.x0 - r.x1, r.x0 - box.x1)
                else:
                    centred = abs(reg.cx - r.cx) <= max(2.0, 0.03 * reg.w) and r.w <= reg.w
                    gap = max(box.y0 - r.y1, r.y0 - box.y1)
                if centred and -0.5 * line.size <= gap <= 2.5 * line.size:
                    line.reason = "figure"
                    self.title_bridges.append(union_all([r, box]))
                    break

    # -- paragraphs -------------------------------------------------------------

    def has_side_content(self, a: Line, b: Line) -> bool:
        """Is there text or graphics right of these two lines (a column, a picture next to
        text)? Content on the left (icons, the left column) does not narrow the text's column."""
        y0, y1 = min(a.rect.y0, b.rect.y0), max(a.rect.y1, b.rect.y1)
        x1 = max(a.rect.x1, b.rect.x1)
        others = [l.rect for l in self.all_lines if l is not a and l is not b] + list(self.regions)
        return any(r.y0 < y1 and y0 < r.y1 and r.x0 > x1 + 5 for r in others)

    def free_width(self, par: Paragraph) -> float:
        """How wide a line of this centred (or right-aligned) paragraph could have been: the room
        between the text margins (or the panel it is on) and whatever stands beside it - taken
        evenly about its centre for centred text, up to its right edge for right-aligned text."""
        r = par.rect
        lb, rb = self.text_margin, self.W - self.text_margin
        panel = self.panel_of(r)
        if panel is not None:
            box = self.panels[panel]["bbox"]
            lb, rb = max(lb, box.x0), min(rb, box.x1)
        mine = {id(l) for l in par.lines}
        beside = [l.rect for l in self.all_lines if id(l) not in mine and l.reason != "theme"] + list(self.regions)
        for o in beside:
            if o.y0 < r.y1 and r.y0 < o.y1:
                if o.x1 <= r.x0 + 0.5:
                    lb = max(lb, o.x1)
                elif o.x0 >= r.x1 - 0.5:
                    rb = min(rb, o.x0)
        if par.align == "center":
            return max(0.0, 2 * min(r.cx - lb, rb - r.cx))
        return max(0.0, r.x1 - lb)

    @staticmethod
    def links_apart(a: Line, b: Line) -> bool:
        """Two lines each linked as a whole to a different page: two entries of a table of
        contents (a section and its subsection, two subsections), not one wrapped title."""
        target = lambda l: {s.link for s in l.content if s.text.strip()}
        ta, tb = target(a), target(b)
        return len(ta) == 1 and len(tb) == 1 and ta != tb and None not in ta | tb and \
            all(t.startswith("#page=") for t in ta | tb)

    def find_hfill_pieces(self, lines: list[Line]) -> None:
        """`hfill_pieces`: id of a line -> the piece an \\hfill pushed to the right end of its row
        (an example's source, an item's reference, an attribution), and `hfill_hosts` the other
        way round. A piece is another line on the same baseline, more than an em further right,
        short, and ending at the right edge of the text area (the page's text margin, or the
        panel it is in). TeX ended the row there: nothing on the next line continues the words
        before the gap - where the piece itself wrapped, its tail opens the next line."""
        self.hfill_pieces, self.hfill_hosts = {}, {}
        native = [l for l in lines if l.reason is None and all(s.horizontal for s in l.spans)]
        for a in native:
            right = self.W - self.text_margin
            panel = self.panel_of(a.rect)
            if panel is not None:
                box = self.panels[panel]["bbox"]
                right = min(right, box.x1 - min(max(0.0, a.x0 - box.x0), 1.5 * a.size))
            # (or, in a quotation set in on both sides, where its left indent mirrors)
            # (a piece is short: two columns' lines side by side are no host and piece)
            rights = [(right, 0.4 * (right - self.text_margin))] + \
                ([(self.W - a.x0, 0.6 * (self.W - 2 * a.x0))] if panel is None and a.x0 > self.text_margin + a.size else [])
            pieces = [b for b in native if b is not a and not b.bullet and abs(b.baseline - a.baseline) <= 0.25 * a.size
                      and abs(b.size - a.size) <= 1 and b.x0 >= a.x1 + a.size
                      and any(abs(b.x1 - r) <= 2 and b.rect.w <= most for r, most in rights)
                      and self.panel_of(b.rect) == panel]
            if pieces and id(a) not in self.hfill_hosts:
                piece = min(pieces, key=lambda b: b.x0)
                self.hfill_pieces[id(a)] = piece
                self.hfill_hosts[id(piece)] = a

    def stacked_above(self, par: Paragraph, line: Line) -> list[Line]:
        """The lines stacked above `line` at the paragraph's left edge, one after another (a
        list's items, the paragraphs of one column). A line whose words after a tab-wide gap
        start at that edge counts too: a verse's number cell, an example's number before it."""
        def at_edge(l: Line) -> bool:
            if abs(l.x0 - par.x0) <= 1.5:
                return True
            words = l.content
            return any(abs(s.rect.x0 - par.x0) <= 1.5 and k and s.rect.x0 - words[k - 1].rect.x1 >= 0.8 * par.size
                       for k, s in enumerate(words))
        above = sorted((l for l in self.all_lines if l.baseline < line.baseline and at_edge(l)
                        and abs(l.size - par.size) <= 0.5 and l.reason is None),  # (not a figure's label)
                       key=lambda l: -l.baseline)
        stack, at = [], line.baseline
        for l in above:
            if at - l.baseline > 3 * par.size:
                break
            stack.append(l)
            at = l.baseline
        return stack

    def column_edge(self, par: Paragraph, line: Line) -> float | None:
        """The right edge of the column `line` is in - the widest of the lines stacked above it at
        the paragraph's left edge (a list's items one after another) - when text or a picture
        stands right of that edge beside one of them; else None: nothing says the text is narrow.
        (A table's next cells beside its first column start left of the table's wide rows.)"""
        stack = self.stacked_above(par, line)
        if not stack:
            return None
        edge = max(l.x1 for l in stack)
        mine = {id(l) for l in stack} | {id(line)}
        others = [l.rect for l in self.all_lines if id(l) not in mine] + list(self.regions)
        if any(r.x0 > edge + 5 and r.y0 < l.rect.y1 and l.rect.y0 < r.y1 for l in stack for r in others):
            return edge
        return None

    def flush_with_others(self, line: Line, last: Line) -> bool:
        """Does `line` start where other text near it starts (the column's left edge)?"""
        return any(abs(l.x0 - line.x0) <= 1 for l in self.all_lines if l is not line and l is not last
                   and l.reason is None and abs(l.baseline - line.baseline) <= 8 * line.size)

    @staticmethod
    def attribution(last: Line, line: Line) -> bool:
        """A line opening with a spaced dash after a closing quotation mark: the quote's source
        ("— Asquith, Mast & Reed"), set on a line of its own."""
        words = [s.text.strip() for s in line.content if s.text.strip()]
        return bool(words) and (words[0] in ("—", "–", "---") or words[0][:2] in ("— ", "– ")) and \
            last.text.rstrip()[-1:] in ("”", "\"", "»", "’")

    def continues(self, par: Paragraph, line: Line) -> str | None:
        """How `line` continues `par` ('left' | 'center' | 'right'), or None. `join_indent`
        says how far a first line was set in, when that is how it continues."""
        self.join_indent = 0.0
        last = par.last
        if line.bullet or abs(line.size - par.size) > 0.5:
            return None
        if is_mono(line.content) or is_mono(last.content) or line.code_number or last.code_number:
            return None  # code (and its line numbers): every line is its own paragraph
        pitch = line.baseline - last.baseline
        if not 0 < pitch <= 1.45 * par.size:  # (Google themes use line spacing 1.15: 1.38 em)
            return None
        if self.panel_of(line.rect) != self.panel_of(par.rect):
            return None
        if self.links_apart(last, line) or self.attribution(last, line):
            return None
        if id(last) in self.hfill_pieces or id(last) in self.hfill_hosts or id(line) in self.hfill_hosts:
            # An \hfill pushed words to the end of the row: TeX ended the line there (a \\ or the
            # item's end), so the next line starts a paragraph of its own; and flushed pieces on
            # consecutive rows (an example's sources) are each their own paragraph.
            return None
        left = abs(line.x0 - (par.x0 - par.indent)) <= 1.5
        right = abs(line.x1 - last.x1) <= 1.5
        if par.first.tab is not None:  # hanging label: wrapped lines start under the text
            if line.tab is not None:
                return None
            left = abs(line.x0 - par.first.tab.rect.x0) <= 1.5
        elif len(par.lines) == 1 and not par.bullet and right and par.x0 - line.x0 <= 3 * par.size and \
                ((par.x0 - line.x0 >= 0.25 * par.size and stretched([last, line])) or
                 (par.x0 - line.x0 >= 0.5 * par.size and self.flush_with_others(line, last))):
            # A first line indented by \parindent (quotation, a table's notes), the next one
            # flush left, both ending at the justified edge: build_paragraphs records the
            # indent. (Right-aligned lines are ragged on the left: their starts meet no other
            # text's edge, and their word spaces are the font's own.)
            left = True
            right = True
            self.join_indent = par.x0 - line.x0
        if par.bullet and right and reads_rtl(par.first) and reads_rtl(line):
            # A right-to-left item's next line starts under its words at their right end, as a
            # left-to-right one's starts at their left (the "left" case below, mirrored): unless
            # its first word - the rightmost - would have fitted at the end of the line above.
            col_left = min(l.x0 for l in par.lines + [line])
            head = max(line.content, key=lambda s: s.rect.x1)
            if last.x0 - 0.3 * par.size - first_word_width(head) > col_left + 0.5:
                return None
            return "right"
        center = abs((line.x0 + line.x1) / 2 - (last.x0 + last.x1) / 2) <= 1.5
        if center and any(abs(s.rect.x0 - last.x0) <= 1.5 and s.rect.x0 - p.rect.x1 >= 0.8 * par.size
                          for p, s in zip(line.content, line.content[1:])):
            # Words after a number cell start under the line above (a verse's line number, an
            # example's number): stacked at one edge, centred only by the lengths of the lines.
            center = False
        if left:
            # TeX would have pulled the next word up if it fitted: then this is a new paragraph.
            col_right = max([l.x1 for l in par.lines] + [line.x1])
            edges = [l.x1 for l in par.lines]
            # Lines already justified to one edge say where the column ends (a \parbox or
            # minipage narrower than the frame, with nothing beside it).
            justified = len(edges) >= 2 and max(edges) - min(edges) <= 1.5
            if justified:
                pass
            elif not self.has_side_content(last, line):
                # Full-width text: beamer's margins are symmetric, so the text block ends
                # where the left margin mirrors. Short paragraphs never reach col_right.
                # Unless the lines stacked above at this left edge sat beside a picture or
                # another column: then this is the column's text below it, as narrow.
                # A quotation is set in on both sides alike: a line of it ending where its left
                # indent mirrors says that is the measure.
                column = self.column_edge(par, line)
                quote = par.x0 > self.text_margin + par.size and abs(col_right - (self.W - par.x0)) <= 1.5
                if not quote and (column is None or column < col_right - 4 * par.size):  # (a column's lines end a word or so apart)
                    col_right = max(col_right, self.W - self.text_margin)
            else:
                # Something stands to the right (a column, a picture, the navigation): the column
                # is at least as wide as the lines stacked above at this edge - a footnote's first
                # line over the next ones, a verse's first line over the next.
                col_right = max([col_right] + [l.x1 for l in self.stacked_above(par, line)])
            # (a span's first word; where its spaces are thin, the whole span: "48 000 EUR")
            head = line.content[0]
            first_word = head.rect.w if thin_span(head, line, par.lines + [line]) else line_word_width(line)
            if not right and last.x1 + 0.3 * par.size + first_word < col_right - 0.5:
                return None
            if not right and not par.bullet and par.first.tab is None and col_right - last.x1 > 1.5 and \
                    self.hand_broken(par, pitch, col_right):
                return None
            return "left"
        if center and par.align in ("left", "center") and len(par.lines) == 1 and line.main.color == last.main.color \
                or par.align == "center" and center:
            # (a line alone in another colour is another entry: a TOC section and its first
            # subsection may be centred on each other by chance)
            return "center"
        if right and (len(par.lines) > 1 or not par.bullet and line.main.color == last.main.color):
            return "right"  # (a TOC section and its first subsection may end together by chance)
        return None

    def hand_broken(self, par: Paragraph, pitch: float, col_right: float) -> bool:
        """Is `par` in a run of lines broken by hand (a verse, a tabular's `l` column, an
        address), so that its last line - short of the measure yet too long for the next word -
        ended at a \\\\ too? The lines stacked above it at its left edge, one pitch apart, reach
        up to a long line (half the measure or more) that TeX ended with room to spare for the
        next line's first word: a forced break. Prose TeX wraps in such a run would be justified
        to the measure (beamer's default), and none of the lines stacked above is: a paragraph
        of wrapped prose under a heading ended by \\\\ is not a run (a heading is short). A
        verse's lines joined as one wrapped paragraph ran together in Slides, whose words run
        longer ("deeds of / valour.Often Scyld", visual hunt r8, r1_lang_v3 s5)."""
        size = par.size
        below = par.first
        for _ in range(12):
            above = [l for l in self.all_lines if l.reason is None and not l.bullet and l.tab is None and l.content
                     and abs(l.size - size) <= 0.5 and abs(l.x0 - below.x0) <= 1.5
                     and abs(below.baseline - l.baseline - pitch) <= 0.1 * size]
            if not above:
                return False
            u = above[0]
            if col_right - u.x1 <= 1.5:
                return False  # (flush with the measure: justified prose, wrapped)
            if any(len(p.lines) > 1 and u in p.lines for p in self.built_paragraphs):
                return False  # (the end of a wrapped paragraph above: prose, paragraph after paragraph)
            if u.x1 - u.x0 >= 0.5 * (col_right - u.x0) and u.x1 + 0.3 * size + line_word_width(below) < col_right - 0.5:
                return True
            below = u
        return False

    def single_line_align(self, line: Line, margin: float, neighbours: list["Paragraph"] = ()) -> str:
        """A line alone is centred when it is centred on the page, right-aligned when it ends at
        the right margin - unless it starts where text next to it starts (the other items of
        its list, the block title above it, the column it is in): then it is left-aligned and
        its centre or end is a coincidence of its length (a centred item puts its bullet
        against its words and leaves its siblings' edge)."""
        if line.bullet and reads_rtl(line):
            # A right-to-left item starts at its right end, beside its bullet: its siblings'
            # edge is there, whatever its length makes of its middle.
            return "right"
        # (a neighbour as long, or centred itself, says nothing: stacked centred lines of a
        # title page, equation numbers; nor does a formula's limit under it)
        near = [l for p in neighbours if p.first is not line and abs(p.size - line.size) <= 1 for l in p.lines
                if abs(l.baseline - line.baseline) <= 4 * line.size and abs(l.x0 - line.x0) <= 1
                and abs(l.x1 - line.x1) > 1 and abs(l.rect.cx - self.W / 2) > 2]
        if near:
            return "left"
        # (ending where a right-aligned paragraph next to it ends: flushed right with it - a quote's
        # attribution under the quote, set right past the margin the page's other text keeps; left,
        # its words ran out past the text area in Slides, r1_lang_v2 s3)
        if line.x0 > margin + line.size and any(
                p.align == "right" and len(p.lines) > 1 and abs(p.size - line.size) <= 1 and
                any(0 < abs(l.baseline - line.baseline) <= 2 * line.size and abs(l.x1 - line.x1) <= 1 for l in p.lines)
                for p in neighbours if p.first is not line):
            return "right"
        if abs(line.rect.cx - self.W / 2) <= 2 and line.x0 > 0.12 * self.W:
            return "center"
        if abs(line.x1 - (self.W - margin)) <= 2 and line.x0 > self.W / 2:
            return "right"
        return "left"

    @staticmethod
    def is_justified(par: Paragraph) -> bool:
        """Justified prose (beamer's default outside lists, \\justifying, a \\parbox): the lines
        but the last all end together, and their word spaces were stretched to get there. A
        left-aligned paragraph whose lines end together by chance keeps natural spaces.

        With two lines only one line ends at the measure, so the shape says little: the last
        line of justified text is never longer than the full ones (a centred pair's may be), and
        every line starts at the measure's left edge (the first may be set in by \\parindent;
        a reference's or a description's hanging label starts left of its wrapped lines)."""
        if par.align != "left" or len(par.lines) < 2 or par.reason is not None or is_mono(par.spans) \
                or par.direction == "rtl":
            return False
        ends = [l.x1 for l in par.lines[:-1]]
        if max(ends) - min(ends) > 0.75 or par.last.x1 > min(ends) + 0.75:
            return False
        starts = [l.x0 for l in par.lines[1:]]
        indent = par.first.x0 - starts[0]
        if max(starts) - min(starts) > 0.75 or not (abs(indent) <= 0.75 or indent >= 0.2 * par.size):
            return False
        return stretched(par.lines)

    def build_paragraphs(self, lines: list[Line]) -> list[Paragraph]:
        self.find_hfill_pieces(lines)
        paragraphs: list[Paragraph] = []
        self.built_paragraphs = paragraphs  # (so far: hand_broken asks how the lines above were joined)
        for line in lines:
            if line.reason not in (None, "math"):
                continue
            best = None
            for par in reversed(paragraphs):
                how = self.continues(par, line)
                if how:
                    best = (par, how)
                    break
            if best:
                par, how = best
                if self.join_indent:
                    par.indent = self.join_indent  # (continues: a \parindent)
                par.lines.append(line)
                par.align = how
                if line.reason == "math":
                    par.reason = "math"
            else:
                paragraphs.append(Paragraph([line], reason=line.reason))
        if not paragraphs:
            return paragraphs

        # (a paragraph's own left edge: its first line may be set in by \parindent)
        margin = min((p.first.x0 - p.indent for p in paragraphs if abs(p.size - self.body) < 1), default=0.08 * self.W)
        for par in paragraphs:
            if len(par.lines) == 1:
                par.align = self.single_line_align(par.first, margin, paragraphs)
                if id(par.first) in self.hfill_hosts:
                    par.align = "right"  # pushed to the measure's end by an \hfill
            par.justified = self.is_justified(par)
            if any(l.reason == "math" for l in par.lines):
                par.reason = "math"
            if par.size >= 1.15 * self.body and par.rect.y0 < 0.2 * self.H:
                par.role = "title"
        titles = [p for p in paragraphs if p.role == "title"]
        for par in paragraphs:
            # A frame subtitle sits right under the title, aligned with it, and is no list item.
            if par.role == "body" and not par.bullet and par.rect.y0 < 0.2 * self.H and par.size < self.body + 0.5 and \
                    any(0 < par.first.baseline - t.last.baseline <= 2 * t.size and abs(par.x0 - t.x0) <= 2
                        for t in titles):
                par.role = "subtitle"
        return paragraphs

    # -- boxes ------------------------------------------------------------------

    def joins_box(self, box: list[Paragraph], par: Paragraph, blockers: list[Paragraph]) -> bool:
        last = box[-1]
        head = box[0]
        if par.role != head.role or par.align != head.align:
            return False
        gap = par.first.baseline - last.last.baseline
        if par.first.code_number or head.first.code_number:
            # A listing's line numbers: one right-aligned column (a blank line has none), whose
            # wider numbers may reach out of the listing's panel.
            return par.first.code_number and head.first.code_number and 0 < gap <= 2.6 * par.size \
                and abs(par.rect.x1 - max(p.rect.x1 for p in box)) <= 1.0
        panel = self.panel_of(par.rect)
        if panel != self.panel_of(head.rect):
            return False
        box_rect = union_all([p.rect for p in box] + [Rect.of(p.bullet["bbox"]) for p in box if p.bullet])
        code = is_code(par.spans) and not par.bullet
        if code != all(is_code(p.spans) and not p.bullet for p in box):
            return False  # code and the prose around it are different boxes (a code box keeps its columns)
        if code and panel is not None and gap > 0 and par.rect.x0 < box_rect.x1 and box_rect.x0 < par.rect.x1:
            # A listing on its panel is one box, blank lines and dedents too: its indentation
            # is spaces from one left edge (as boxes cut at its blank lines, the same indent
            # was spaces in one and the box's own position in the next).
            between = Rect(box_rect.x0, last.last.baseline + 0.1, box_rect.x1, par.first.baseline - par.size)
            return not any(b.rect.intersects(between) for b in blockers)
        if not 0 < gap <= 2.6 * max(par.size, last.size):
            return False
        if code:
            # Code block: indentation varies freely, lines follow at normal pitch.
            return par.rect.x0 >= box_rect.x0 - 1.5 and gap <= 1.35 * par.size
        if par.align == "center":
            if abs(par.rect.cx - box_rect.cx) > 2:
                return False
        elif par.bullet and reads_rtl(par.first) and all(reads_rtl(p.first) for p in box):
            # (right to left: a nested item's bullet ends left of its parent's words' start - their
            # right end - but not much further, and the list keeps to its right edge)
            x = max(par.rect.x1, par.bullet["bbox"][2])
            if not (min(p.first.x1 for p in box) - 2 * par.size <= x <= box_rect.x1 + 1.5):
                return False
            if not (par.rect.x0 < box_rect.x1 and box_rect.x0 < par.rect.x1):
                return False
        else:
            # A nested item's bullet starts after its parent's text start, but not much further.
            x = min(par.rect.x0, par.bullet["bbox"][0]) if par.bullet else par.rect.x0
            tabbed = par.first.tab is not None and any(abs(p.first.tab.rect.x0 - par.first.tab.rect.x0) <= 0.6
                                                        for p in box if p.first.tab is not None)
            if not tabbed and not (box_rect.x0 - 1.5 <= x <= max(p.x0 for p in box) + 2 * par.size):
                return False
            if not (par.rect.x0 < box_rect.x1 and box_rect.x0 < par.rect.x1):
                return False
        between = Rect(box_rect.x0, last.last.baseline + 0.1, box_rect.x1, par.first.baseline - par.size)
        return not any(b.rect.intersects(between) for b in blockers)

    def build_boxes(self, paragraphs: list[Paragraph]) -> list[list[Paragraph]]:
        native = [p for p in paragraphs if p.reason is None]
        blockers = [p for p in paragraphs if p.reason is not None]
        boxes: list[list[Paragraph]] = []
        for par in sorted(native, key=lambda p: (p.first.baseline, p.x0)):
            for box in reversed(boxes):
                if self.joins_box(box, par, blockers):
                    box.append(par)
                    break
            else:
                boxes.append([par])
        for box in boxes:
            # (a right-to-left list nests leftwards: its level is how far its bullet's right
            # edge stands in from the list's)
            start = (lambda b: -Rect.of(b["bbox"]).x1) if all(reads_rtl(p.first) for p in box) else \
                (lambda b: Rect.of(b["bbox"]).x0)
            xs = sorted({round(start(p.bullet), 0) for p in box if p.bullet})
            levels: list[float] = []
            for x in xs:
                if not levels or x - levels[-1] > 2:
                    levels.append(x)
            for p in box:
                if p.bullet:
                    bx = start(p.bullet)
                    p.level = min(range(len(levels)), key=lambda i: abs(levels[i] - bx))
        return boxes

    # -- output -----------------------------------------------------------------

    @staticmethod
    def runs(par: Paragraph, indent: str = "", soft_breaks: bool = False, pitch: float | None = None,
             x_ref: float = 0.0) -> list[dict]:
        """`pitch`, `x_ref`: a code block's column grid (`code_pitch`), which its spaces keep."""
        runs: list[dict] = []
        prev: Span | None = None
        hole_x1 = 0.0
        for li, line in enumerate(par.lines):
            words = [s for s in line.content if s.text.strip()]
            gaps = [gap_between(a, b) / line.size for a, b in zip(words, words[1:])]
            # (a justified line's own word space, stretched; a \quad does not stretch)
            # (its last line keeps natural spaces)
            word_space = min(gaps) if par.justified and gaps and li < len(par.lines) - 1 else 0.33
            order = reading_order(line)
            accent = ""  # an accent at the end of a span, for the letter under it in the next one
            for si, (span, forced) in enumerate(order):
                if span == FRACTION_SLASH:
                    main = line.main
                    runs.append({"text": FRACTION_SLASH, "font": main.font, "family": main.info.family,
                                 "size": round(line.size, 2), "bold": False, "italic": False, "smallcaps": False,
                                 "color": main.color, "link": main.link, "script": None,
                                 "underline": False, "highlight": None})
                    continue
                hole = next((h for h in line.holes if span in h), None)
                if span.info.family == "icon" and hole is None:
                    continue  # symbol-font glyphs stay in the background picture
                if hole is not None:
                    if span is not min(hole, key=lambda s: s.rect.x0):
                        continue
                    # A gap as wide as the formula; emit fills it with no-break spaces.
                    extent = line.hole_rect(hole)
                    # (a big operator's limits are in its picture, and may be wider than its sign)
                    extent = union_all([extent] + [lim.rect for lim in line.limits
                                                   if extent.x0 - line.size <= lim.rect.cx <= extent.x1 + line.size])
                    x0, x1 = extent.x0, extent.x1
                    if prev is not None and runs:
                        gap = x0 - prev.rect.x1
                        if (si == 0 or gap > 0.15 * line.size) and not runs[-1]["text"].endswith(" "):
                            runs[-1]["text"] += " "
                        if si and gap >= max(0.9, word_space + 0.4) * line.size and not runs[-1].get("hole"):
                            # a \quad before the graphic keeps its em spaces, as between words
                            runs[-1]["text"] += EM_SPACE * max(1, round((gap - 0.33 * line.size) / line.size))
                    main = line.main
                    # What precedes the formula on its line, for emit to predict where Slides
                    # will actually leave the gap (substitute fonts are not exactly as wide).
                    before = [[round(s.rect.w, 2), s.font, s.info.family, s.info.bold, s.info.italic,
                               # math spacing is part of the span (" ≥"): Slides sets a plain space there
                               math_text(s.font, s.text)[0] if s.info.family == "math" else s.text, round(s.rect.x0, 2)]
                              for s in line.content if s.rect.x1 <= x0 + 0.5 and s.info.family != "icon"
                              and not any(s in h for h in line.holes)]
                    after = [s.rect.x0 for s in line.content if s.rect.x0 >= x1 - 0.5 and s not in hole and s.info.family != "icon"]
                    runs.append({"text": " ", "font": main.font, "family": main.info.family,
                                 "size": round(line.size, 2), "bold": False, "italic": False, "smallcaps": False,
                                 "color": main.color, "link": None, "script": None, "underline": False,
                                 # (the picture is cropped with HOLE_PAD on both sides: room for that too)
                                 "highlight": None, "hole": round(x1 - x0 + 2 * HOLE_PAD, 2), "hole_x0": round(x0, 2),
                                 "before": before, "next_x0": round(min(after), 2) if after else None})
                    prev, hole_x1 = max(hole, key=lambda s: s.rect.x1), x1
                    continue
                text = span.text
                if prev is not None and runs and not runs[-1].get("hole") and text.strip() and \
                        span.rect.x0 < prev.rect.x1 - 0.2 and prev.rect.x0 < span.rect.x1 and \
                        (runs[-1]["text"][-1:], text.strip()[0]) in COMPOSED:
                    # a composed symbol's pieces in two fonts, overlapped (CMSY's ∼ over CMR's =)
                    tail = runs[-1]["text"]
                    runs[-1]["text"] = tail[:-1] + COMPOSED[(tail[-1], text.strip()[0])]
                    text = text.strip()[1:] + text[len(text.rstrip()):]
                    if not text.strip():
                        prev = max(prev, span, key=lambda s: s.rect.x1)
                        continue
                if text.strip() in ACCENTS and prev is not None and runs and not runs[-1].get("hole") and \
                        span.rect.x0 < prev.rect.x1 - 0.2 and prev.rect.x0 < span.rect.x1:
                    tail = runs[-1]["text"]  # over the letter before it
                    runs[-1]["text"] = tail[:-1] + with_accent(tail[-1], ACCENTS[text.strip()]) \
                        if tail[-1:].isalpha() else tail + ACCENTS[text.strip()]
                    continue
                if accent:
                    lead = len(text) - len(text.lstrip())
                    letter = with_accent(text[lead], accent) if text[lead:lead + 1].isalpha() else text[lead:lead + 1] + accent
                    text, accent = text[:lead] + letter + text[lead + 1:], ""
                body = text.rstrip()
                nxt = order[si + 1][0] if si + 1 < len(order) else None
                if len(body) >= 2 and body[-1] in ACCENTS and isinstance(nxt, Span) and nxt.rect.x0 < span.rect.x1 - 0.2:
                    # "Var(¯" then "X": PDFium reads \bar{X}'s bar with the text before the letter
                    text, accent = body[:-1] + text[len(body):], ACCENTS[body[-1]]
                if forced == "sub":  # denominator: follows the slash directly
                    pass
                elif prev is not None:
                    if si == 0:
                        tail = runs[-1]["text"]
                        if len(tail) >= 2 and tail.endswith("-") and tail[-2].isalpha() and text[:1].islower() \
                                and not explicit_hyphen(tail):
                            runs[-1]["text"] = tail[:-1]  # TeX hyphenation at a line break
                            sep = ""
                        elif len(tail) >= 2 and tail.endswith(("-", "–", "—")) and not tail[-2].isspace() \
                                and text[:1].isalnum():
                            # a compound's hyphen or a range's dash: the words stay joined - but
                            # Slides breaks no line at a hyphen: a compound longer than every line
                            # TeX set it among keeps its break, or Slides cut it inside a word
                            # ("Datenschutz-Folgenabsch / ätzung" in a narrow column's heading)
                            longer = last_word_width(prev) + first_word_width(span) > \
                                max(l.x1 - l.x0 for l in par.lines) + 0.5
                            sep = chr(11) if par.role == "title" or soft_breaks or longer else ""
                        elif par.role == "title" or soft_breaks:
                            sep = chr(11)  # titles keep their line breaks (a soft break in Slides)
                        elif cjk(tail.rstrip()[-1:]) and cjk(text.lstrip()[:1]):
                            sep = ""  # Chinese and Japanese break lines between characters, no space
                        else:
                            sep = " "
                    elif span is line.tab and not pitch:
                        sep = "\t"
                    else:
                        # (after a hole, from the end of its graphic: a frame wider than its words)
                        gap = (span.rect.x0 - hole_x1) if runs[-1].get("hole") else gap_between(prev, span)
                        sep = " " if gap > 0.15 * line.size else ""
                        mono = prev.info.family == "mono" and span.info.family == "mono" and span.text.strip()
                        if (mono or span is line.tab) and pitch and not runs[-1].get("hole") and not runs[-1]["script"]:
                            # (a diff's "+" before its code is no hanging label: spaces too)
                            # In a monospaced face every space is one advance (Roboto Mono: 0.600 em,
                            # an em space too), so a \quad is so many plain spaces, as in code_indent;
                            # em spaces there were half of CMTT's \quad each (text_fit, slide 18).
                            # In a code block the spaces are the columns between where the span
                            # before ends and this one starts (listings' columns=fixed sets
                            # tokens a few tenths of a column apart with no space between).
                            col = lambda s: round((s.rect.x0 - x_ref) / pitch)
                            runs[-1]["text"] += " " * max(0, col(span) - col(prev) - len(prev.text))
                            sep = ""
                        elif sep and mono:
                            # (a one-letter span's own box is no measure of the advance)
                            advance = max(prev, span, key=lambda s: len(s.text))
                            sep = " " * max(1, round(gap / (advance.rect.w / max(1, len(advance.text)))))
                        elif gap >= max(0.9, word_space + 0.4) * line.size:
                            # \quad and wider (\and between authors): em spaces keep the gap
                            # (a \quad measures 0.999 em between the advance boxes; a justified
                            # line's own stretched spaces stay spaces)
                            sep += EM_SPACE * max(1, round((gap - 0.33 * line.size) / line.size))
                    if si and sep == " " and runs[-1].get("hole"):
                        # The space after a formula becomes part of its gap: TeX's space there
                        # is wider than a Slides space would be.
                        runs[-1]["hole"] = round(runs[-1]["hole"] + gap, 2)
                        sep = ""
                        text = text.lstrip()
                    if sep and not runs[-1]["text"].endswith(" ") and not text.startswith(" "):
                        if runs[-1]["script"] or runs[-1].get("hole") or prev.size < 0.85 * span.size:
                            # keep the space out of the raised/lowered run and the gap, and out of
                            # smaller words (TeX's space after them is the surrounding text's)
                            text = sep + text
                        else:
                            runs[-1]["text"] += sep
                if runs and not runs[-1].get("hole") and runs[-1]["text"].rstrip().endswith(NEGATION) and text.strip():
                    # \not in one font, its relation in another (CMSY's slash, CMR's =): one ≠
                    tail = runs[-1]["text"].rstrip()
                    runs[-1]["text"] = tail[:-1] + runs[-1]["text"][len(tail):]
                    text = negate(NEGATION + text)
                if thin_span(span, line, par.lines):
                    text = re.sub(r"(?<=\S) (?=\S)", " ", text)  # "48 000 EUR" breaks nowhere
                script = forced or script_of(span, line)
                # Slides shrinks sub/superscripts itself: give them the line's size.
                size = script_size(span, line) if script else span.size
                family, italic = span.info.family, span.info.italic
                pieces = [(text, italic)]
                if family == "math":
                    # Math fonts carry symbols and variables; show them in the text family -
                    # but CM's (Times', Palatino's) math letters are a serif italic among any words.
                    family = "serif" if serif_math_letters(span.font) else math_family(line, par)
                    pieces = math_pieces(span.font, text)
                tail = TRAILING_PUNCT.search(text) if span.decor_to is not None and family != "math" else None
                if script == "super" and not forced and text.strip() in RAISED_MARKS:
                    pieces, script, size, tail = [(raised_mark(text), False)], None, line.size, None
                if tail:  # (its decoration ends before this punctuation: Span.decor_to)
                    pieces = [(text[:tail.start()], italic), (text[tail.start():], italic)]
                for k, (text, italic) in enumerate(pieces):
                    plain = bool(tail) and k == 1
                    style = {
                        "font": span.font, "family": family,
                        "size": round(size, 2),
                        "bold": span.info.bold, "italic": italic, "smallcaps": span.info.smallcaps,
                        "color": span.color, "link": span.link, "script": script,
                        "underline": span.underline and not plain, "strike": span.strike and not plain,
                        "highlight": None if plain else span.highlight,
                    }
                    marks = lambda r: (r["underline"], r.get("strike", False), r["highlight"])
                    if runs and runs[-1]["text"].endswith(" ") and marks(runs[-1]) != marks(style) and any(marks(runs[-1])):
                        runs[-1]["text"] = runs[-1]["text"][:-1]  # an underline, strike or highlight ends at the word
                        if any(marks(style)):  # and the next one starts at its word: the space between is plain
                            runs.append({**runs[-1], "text": " ", "underline": False, "strike": False, "highlight": None})
                        else:
                            text = " " + text
                    if style["highlight"] and span.pad_left and k == 0:
                        lead = len(text) - len(text.lstrip(" "))
                        text = text[:lead] + BOX_PAD + text[lead:]
                    if style["highlight"] and span.pad_right and k == len(pieces) - 1 - bool(tail):
                        text = text + BOX_PAD
                    if runs and not runs[-1].get("hole") and all(runs[-1].get(k) == v for k, v in style.items()):
                        runs[-1]["text"] += text
                    else:
                        runs.append({"text": text, **style})
                prev = span
        prose_spaces(runs)
        if not indent:
            runs = [r for r in runs if r["text"]]
        if runs:
            runs[0]["text"] = indent + runs[0]["text"].lstrip()
            runs[-1]["text"] = runs[-1]["text"].rstrip()
        return runs

    @staticmethod
    def display_pieces_apart(paragraphs: list[Paragraph]) -> list[Paragraph]:
        """A big delimiter or operator alone on a line is a piece of a display formula, never a
        line of a paragraph's words - whatever edge it happens to share with them (a \\left(
        ending under "The structural similarity is" continued that line right-aligned, and the
        paragraph, math now, became a picture of the words). It goes to a paragraph of its own."""
        out = []
        for par in paragraphs:
            piece = lambda l: l.reason == "math" and l.content and all(extension_font(s.font) for s in l.content)
            pieces = [l for l in par.lines if piece(l)]
            if not pieces or len(pieces) == len(par.lines) or any(l.reason == "math" for l in par.lines if not piece(l)):
                out.append(par)
                continue
            rest = [l for l in par.lines if not piece(l)]
            out.append(replace(par, lines=rest, reason=None))
            out += [Paragraph([l], reason="math") for l in pieces]
        return out

    def math_pictures(self, lines: list[Line], paragraphs: list[Paragraph], elements: list[dict]) -> list[dict]:
        """Math that cannot be text (display equations, fractions, and paragraphs containing
        them) becomes movable pictures instead of staying baked into the background."""
        used = {sid for e in elements for sid in e["spans"]}
        para_reason = {id(l): p.reason for p in paragraphs for l in p.lines}
        spans = [s for l in lines for s in l.spans if s.id not in used
                 and (l.reason == "math" or (l.reason is None and para_reason.get(id(l)) == "math"))]
        if not spans:
            return []
        # Collisions are checked against the actual text lines, not text box outlines: a math
        # item inside a bullet list lies within the list's box but touches none of its lines.
        blocked = [Rect.of(e["bbox"]) for e in elements if e["kind"] != "text" and not e.get("overlay")]
        for e in elements:
            if e["kind"] == "text":
                for p in e["paragraphs"]:
                    blocked += [Rect(l["x0"], l["baseline"] - 0.8 * p["size"], l["x1"], l["baseline"] + 0.25 * p["size"])
                                for l in p["lines"]]
                    if p["bullet"]:  # glyph boxes include ascender space; use their visible core
                        b = Rect.of(p["bullet"]["bbox"])
                        blocked.append(Rect(b.x0, b.cy - 0.25 * b.h, b.x1, b.cy + 0.25 * b.h))
        out = []
        taken: set[str] = set()
        clusters = cluster_rects([s.rect for s in spans] + list(self.bars), gap=0.6 * self.body)
        # (clusters whose boxes overlap are one formula: a display \sum between the words of its
        # row is a cluster of its own inside theirs, and became a picture of its glyph's font
        # box, a chevron, under the picture of the rest)
        while len(merged := cluster_rects(clusters, gap=3.0)) < len(clusters):
            clusters = merged
        # (columns of one display on the same rows - a cases' conditions, an aligned
        # right-hand side - a little further apart than glyphs; not an equation number)
        eqno = lambda c: all(EQ_NUMBER_RE.match(s.text.strip()) for s in spans if c.expand(1.5).contains_rect(s.rect))
        merged = True
        while merged:
            merged = False
            for a in clusters:
                for b in clusters:
                    if a is not b and not eqno(a) and not eqno(b) and \
                            min(a.y1, b.y1) - max(a.y0, b.y0) >= 0.5 * min(a.h, b.h) and \
                            max(a.x0, b.x0) - min(a.x1, b.x1) <= 1.5 * self.body:
                        clusters = [c for c in clusters if c is not a and c is not b] + [union_all([a, b])]
                        merged = True
                        break
                if merged:
                    break
        for c in clusters:
            box = c.expand(1.5)
            members = [s for s in spans if box.contains_rect(s.rect) and s.id not in taken]
            taken |= {s.id for s in members}
            # (the glyphs themselves, not the picture's anti-aliasing margin: a display's big
            # operator box starts a hair under the descenders of the paragraph above)
            if not members or any(b.intersects(c.expand(0.3)) for b in blocked):
                continue  # only a stray bar, or tangled with native content: leave it in the background
            if len(members) == 1 and EQ_NUMBER_RE.match(members[0].text.strip()) and members[0].info.family != "math":
                # An equation number beside its equation is plain text.
                par = Paragraph([Line(members)], align="right")
                out.append(self.text_element([par], f"p{self.page['index']}eq{len(out)}"))
                continue
            out.append({"id": f"p{self.page['index']}m{len(out)}", "kind": "image", "role": "math",
                        "bbox": box.as_list(), "spans": [s.id for s in members]})
        return out

    def figures(self, lines: list[Line], text_elements: list[dict]) -> list[dict]:
        """Figure regions (graphics, images and their labels) that can become separate pictures.

        Skipped, so they stay in the background: specks (shadow corners, QED boxes),
        near-full-page artwork, and anything overlapping an editable text box."""
        label_spans = [s for l in lines if l.reason in ("figure", "rotated") for s in l.spans]
        if not self.regions:
            return []
        def ink_rect(e: dict) -> Rect:
            # Big text without descenders (a statistic: "92%") ends at its baseline, not a
            # quarter em below it where a card under it may start.
            r = Rect.of(e["bbox"])
            last = e["paragraphs"][-1] if e.get("paragraphs") else None
            if last and last["lines"]:
                text = "".join(run["text"] for run in last["runs"])
                depth = 0.25 if any(c in "gjpqyQ,;()[]{}|/@$_" for c in text) else 0.05
                r = Rect(r.x0, r.y0, r.x1, min(r.y1, last["lines"][-1]["baseline"] + depth * last["size"]))
            return r

        text_rects = [ink_rect(e) for e in text_elements]

        def grazed(e: dict, t: Rect, c: Rect) -> bool:
            """A figure reaching less than an em into a text box's outline, clear of its lines
            and bullets (a pie's pin label in the margin of the list beside it, between two
            bullets): no text under it. (Deeper in, a figure is over the text.)"""
            if min(t.x1, c.x1) - max(t.x0, c.x0) >= self.body and min(t.y1, c.y1) - max(t.y0, c.y0) >= self.body:
                return False
            ink = [Rect(l["x0"], l["baseline"] - 0.8 * p["size"], l["x1"], l["baseline"] + 0.25 * p["size"])
                   for p in e.get("paragraphs", []) for l in p["lines"]]
            ink += [Rect.of(p["bullet"]["bbox"]) for p in e.get("paragraphs", []) if p["bullet"] and p["bullet"].get("bbox")]
            # (the figure's own pieces there, not its outline: the label reaches under the
            # bullets' column between two of them)
            pieces = [r for r in rects if c.expand(0.1).contains_rect(r) and r.intersects(t)]
            return bool(ink) and not any(r.intersects(q) for r in ink for q in pieces)

        self.bullet_boxes = [Rect.of(p["bullet"]["bbox"]).expand(1) for e in text_elements for p in e["paragraphs"]
                             if p["bullet"] and p["bullet"].get("bbox")]
        out = []
        # Tables first, from their rules: clustering could merge a table with a picture beside it.
        for group in self.table_rules:
            frame = union_all(r["rect"] for r in group).expand(1)
            table = self.table_from(frame, label_spans, text_rects, len(out))
            if table:
                out.append(table)
                taken = set(table["spans"])
                label_spans = [s for s in label_spans if s.id not in taken]
        table_frames = [Rect.of(t["bbox"]) for t in out]
        regions = [r for r in self.regions if not any(f.expand(0.5).contains_rect(r) for f in table_frames)]
        if not regions:
            return out
        # Cluster word by word: one "line" of labels can span two neighbouring figures.
        # Navigation symbols and ornaments in the header/footer band bridge nothing (they would
        # join a \logo above them into one picture).
        regions = [r for r in regions if not self.band_ornament(r)]
        if not regions:
            return out
        rects = regions + [s.rect for s in label_spans] + self.title_bridges + self.column_bridges
        for c in cluster_rects(rects, gap=0.8 * self.body):
            if max(c.w, c.h) < 25 or c.w * c.h > 0.8 * self.W * self.H:
                continue
            if (c.y1 <= 0.15 * self.H or c.y0 >= 0.88 * self.H) and c.h <= 0.1 * self.H:
                continue  # navigation dots and ornaments in the header/footer band
            if not any(r.intersects(c.expand(0.1)) for r in regions):
                continue  # only stray rotated text, no graphics
            if any(h.expand(0.5).contains_rect(c) for h in self.hole_boxes):
                continue  # the graphic of a hole (a frame around words): in that picture already
            over_text = any(t.intersects(c) and not grazed(e, t, c) for e, t in zip(text_elements, text_rects))
            table = None if over_text or not self.fill_grid(c) else self.table_from(c, label_spans, text_rects, len(out))
            if table:
                out.append(table)  # shaded cells edge to edge: a table, not a diagram of boxes
                continue
            diagram = None if over_text else self.diagram_from(c, label_spans, len(out))
            if diagram and self.splits_cells(diagram, label_spans):
                diagram = None  # a ruled grid table_from could not read: a picture, not merged rows
            if diagram and any(n.get("text") for n in diagram["nodes"]):
                out.append(diagram)  # frames around their own text (a framed paragraph), not marks on prose
                continue
            overlay = self.overlay(c, label_spans, lines, len(out), over_text)
            if overlay:
                out.append(overlay)
                continue
            if over_text:
                continue
            table = self.table_from(c, label_spans, text_rects, len(out))
            if table:
                out.append(table)
                continue
            if diagram:
                out.append(diagram)
                continue
            bars = self.plain_rectangles(c, label_spans, len(out))
            if bars:
                out += bars
                continue
            spans = [s.id for s in label_spans if c.expand(0.5).contains_rect(s.rect)]
            under_words = any(t.x0 < c.x1 and c.x0 < t.x1 and t.y0 < c.cy <= t.y1 + 0.3 * self.body for t in text_rects)
            if not spans and c.h <= 1.5 and under_words and \
                    not any(c.expand(0.5).intersects(Rect.of(im["bbox"])) for im in self.page["images"]):
                # Only a hairline (the pieces of an underline under words left in the background):
                # a picture adds nothing, and its box took the words above it off the background
                # while the crop showed only the rule. (A rule with no words on it - a footnote's,
                # above its text - stays a picture: in the background it is ink by native words.)
                continue
            el = {"id": f"p{self.page['index']}f{len(out)}", "kind": "image", "role": "figure",
                  "bbox": self.clip_to_bands(c, c.expand(1.0)).as_list(), "spans": spans}
            bare = None if spans else self.bare_image(c)
            if bare is not None:
                # One `\includegraphics` and nothing else: the picture is the image itself, on
                # its own box (the margin around a drawing is for strokes reaching out of it,
                # which an image has none of). render.image_file may then write the author's
                # file instead of a render of the page.
                el["bbox"], el["image"] = list(bare["bbox"]), bare["id"]
            out.append(el)
        return out

    def bare_image(self, c: Rect) -> dict | None:
        """The raster image a figure region consists of, if it consists of nothing else: one
        graphic, one image covering it, no text drawn in the region."""
        members = [g for g in self.graphics if c.expand(0.5).contains_rect(g)]
        images = [im for im in self.page["images"] if Rect.of(im["bbox"]).intersects(c.expand(0.5))]
        if len(members) != 1 or len(images) != 1:
            return None
        r = Rect.of(images[0]["bbox"])
        if not (r.contains_rect(members[0]) and members[0].contains_rect(r)):
            return None
        if any(Rect.of(s["bbox"]).intersects(c.expand(0.5)) for s in self.page["spans"]):
            return None
        return images[0]

    def overlay(self, c: Rect, label_spans: list[Span], lines: list[Line], index: int, over_text: bool) -> dict | None:
        """A figure cluster drawn over native text or right at its words (a tikzmark arrow, a
        brace under a phrase, an emphasis ellipse, a callout): a picture of only its own
        drawings and labels on a transparent ground (`drawings`), grouped with the text it
        meets (`anchor`). `marks` are the word edges it meets, with what precedes them on their
        line, so that emit moves and stretches it with the words Slides sets at other widths."""
        box = c.expand(1)
        # (not the frames of holes, nor bullets drawn on the page: those are pictures of their own)
        taken = [h.expand(0.5) for h in self.hole_boxes] + self.bullet_boxes
        drawings = [d for d in self.page["drawings"] if d["id"] in self.graphic_drawings
                    and box.contains_rect(self.graphic_drawings[d["id"]])
                    and not any(t.contains_rect(self.graphic_drawings[d["id"]]) for t in taken)]
        if not drawings or any(box.intersects(Rect.of(im["bbox"])) for im in self.page["images"]):
            return None
        def on_node(s: Span, l: Line) -> bool:
            # Words well inside a filled shape of the cluster are that node's label (a "?" on
            # a disc), not words the drawing is made for: stretched after them as Slides sets
            # them, the disc came out an ellipse. (A highlight box is tight around its words.)
            for d in drawings:
                g = self.graphic_drawings[d["id"]]
                if "f" in d["type"] and d.get("fill") and g.contains_rect(s.rect, tol=0):
                    label = union_all([o.rect for o in l.content if o.text.strip() and g.contains_rect(o.rect, tol=0)])
                    if g.w >= 2 * label.w and g.h >= 1.5 * label.h:
                        return True
            return False

        words = [(s, l) for l in lines if id(l) in self.line_owner for s in l.content
                 if s.text.strip() and s.info.family != "icon" and not any(s in h for h in l.holes) and not on_node(s, l)]
        if not over_text and any(box.contains_rect(s.rect) and s not in label_spans for l in lines
                                 if id(l) not in self.line_owner for s in l.spans):
            return None  # math set inside a figure: one picture of everything in its box

        def points(d: dict) -> list:
            out = []
            for op, pts in d.get("path") or []:
                if op == "re":
                    (x0, y0), (x1, y1) = pts
                    out += [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                else:
                    out += [pts[0], pts[-1]] if op == "c" else pts
            return out

        def near(s: Span, x: float, y: float) -> bool:
            return s.rect.x0 - 0.1 * s.size <= x <= s.rect.x1 + 0.1 * s.size and \
                s.rect.y0 - 0.35 * s.size <= y <= s.rect.y1 + 0.35 * s.size

        # The words its line ends, corners and tips touch; else those it is drawn across. A
        # closed curve has no ends: it meets the words it circles, not those its rim passes (a
        # Venn circle under the title of its set).
        def rim_only(d: dict) -> bool:
            path = d.get("path") or []
            return len(path) >= 2 and all(op == "c" for op, _ in path) and math.dist(path[0][1][0], path[-1][1][-1]) <= 0.5
        ends = [(p, d) for d in drawings for p in points(d)]
        met = [(s, l) for s, l in words if any(near(s, x, y) and (not rim_only(d) or self.graphic_drawings[d["id"]].contains(s.rect.cx, s.rect.cy))
                                                for (x, y), d in ends)]
        if not met and over_text:
            met = [(s, l) for s, l in words if any(s.rect.intersects(self.graphic_drawings[d["id"]]) for d in drawings)]
        if not over_text:
            # Beside text, it points at words of prose; a line of widely spaced words next to a
            # figure is its axis labels.
            spaced = lambda l: any(b.rect.x0 - a.rect.x1 >= l.size for a, b in zip(l.content, l.content[1:]))
            met = [(s, l) for s, l in met if not spaced(l)]
            if not met:
                return None
        labels = [s for s in label_spans if c.expand(0.5).contains_rect(s.rect)]
        el = {"id": f"p{self.page['index']}f{index}", "kind": "image", "role": "figure", "overlay": True,
              "bbox": union_all([self.graphic_drawings[d["id"]] for d in drawings] + [s.rect for s in labels]).expand(1.0).as_list(),
              "spans": [s.id for s in labels], "drawings": [d["id"] for d in drawings]}
        if met:
            anchor = Counter(self.line_owner[id(l)][0] for _, l in met).most_common(1)[0][0]
            el["anchor"] = anchor
            el["marks"] = [self.mark(l, x) for s, l in dict.fromkeys(met) if self.line_owner[id(l)] == (anchor, "left")
                           for x in (s.rect.x0, s.rect.x1)]
        return el

    def trim_overlays(self, elements: list[dict]) -> None:
        """Drawings of an overlay that lie within a formula picture (a highlight behind a line
        that became one picture) belong to that picture: the overlay gives them up."""
        boxes = [Rect.of(e["bbox"]) for e in elements if e["kind"] == "image" and e.get("role") == "math"]
        by_id = self.spans_by_id
        for el in elements:
            if not el.get("overlay"):
                continue
            el["drawings"] = [i for i in el["drawings"] if not any(b.contains_rect(self.graphic_drawings[i]) for b in boxes)]
            if el["drawings"]:
                el["bbox"] = union_all([self.graphic_drawings[i] for i in el["drawings"]] +
                                       [by_id[i].rect for i in el["spans"]]).expand(1.0).as_list()

    @staticmethod
    def mark(line: Line, x: float) -> dict:
        """Where a graphic meets a line of text at `x`: the line's style and the words before x,
        as a formula hole run carries them (emit.formula_shifts predicts where x lands in Slides)."""
        main = line.main
        words = [s for s in line.content if s.rect.x1 <= x + 0.5 and s.info.family != "icon"
                 and not any(s in h for h in line.holes)]
        # Holes and em-space gaps before x keep their width in Slides: close them up, or
        # formula_shifts would read each as one huge word space.
        cuts, holes = [], [line.hole_rect(h) for h in line.holes]
        for r in holes:
            if r.x1 <= x + 0.5:
                cuts.append((r.x0, min([s.rect.x0 for s in words if s.rect.x0 >= r.x1 - 0.5] + [x]) - r.x0))
        stops = sorted([(s.rect.x0, s.rect.x1) for s in words] + [(x, x)])
        for (_, a), (b, _) in zip(stops, stops[1:]):
            if b - a >= line.size and not any(a - 0.5 <= r.x0 < b for r in holes):
                cuts.append((a, max(1, round((b - a - 0.33 * line.size) / line.size)) * line.size))  # (as runs() writes it)
        def closed(v):
            return v - sum(w for c, w in cuts if c < v - 0.5)
        before = [[round(s.rect.w, 2), s.font, s.info.family, s.info.bold, s.info.italic,
                   math_text(s.font, s.text)[0] if s.info.family == "math" else s.text, round(closed(s.rect.x0), 2)]
                  for s in words]
        # (a hole's gap in Slides is HOLE_PAD wider on each side)
        pads = 2 * HOLE_PAD * sum(r.x1 <= x + 0.5 for r in holes)
        return {"x": round(x, 2), "hole_x0": round(closed(x), 2), "pads": pads, "font": main.font, "family": main.info.family, "size": round(line.size, 2),
                "bold": main.info.bold, "italic": main.info.italic, "before": before}

    def icons(self, text_elements: list[dict]) -> list[dict]:
        """Small raster images next to text that are not bullets (bibliography icons, inline
        logos): movable pictures. Overlapping parts of one icon are cropped together."""
        bullets = {p["bullet"].get("image") for e in text_elements for p in e["paragraphs"] if p["bullet"]}
        starts = [Rect(l["x0"], l["baseline"] - p["size"], l["x1"], l["baseline"])
                  for e in text_elements for p in e["paragraphs"] for l in p["lines"]]

        def beside_text(r: Rect) -> bool:
            # right of the icon, or starting on it (a label drawn on a ball)
            return any(-r.w <= s.x0 - r.x1 <= 15 and s.y0 < r.y1 and r.y0 < s.y1 for s in starts)

        rects = [ir for im, ir in self.small_images
                 if im["id"] not in bullets and max(ir.w, ir.h) <= 20 and min(ir.w, ir.h) >= 3
                 and not any(b.contains_rect(ir) for b in self.icon_bullets)
                 and 0.15 * self.H < ir.cy < 0.88 * self.H and not self.on_edge_artwork(ir)
                 and not any(p["bbox"].expand(1).intersects(ir) for p in self.panels)]  # block shadow pieces
        rects = [c for c in cluster_rects(rects, gap=0.0) if beside_text(c)]
        out = []
        for c in rects:
            out.append({"id": f"p{self.page['index']}ic{len(out)}", "kind": "image", "role": "icon",
                        "bbox": c.expand(0.5).as_list(), "spans": []})
            # A picture as an item label (\item[\includegraphics...]) moves with its item.
            item = next((e["id"] for e in text_elements for p in e["paragraphs"] for l in p["lines"][:1]
                         if 0 <= l["x0"] - c.x1 <= 1.5 * p["size"] and l["baseline"] - p["size"] < c.cy < l["baseline"]), None)
            if item:
                out[-1]["anchor"] = item
        return out

    def specks_on_panels(self, spans: list[Span], elements: list[dict]) -> list[dict]:
        """Small graphics on a block panel that no other element took (a proof's QED box, a
        TikZ mark): the panel becomes a native shape over the background, so they become
        pictures above it (grouped with the block) instead of staying hidden in the background."""
        taken = [Rect.of(e["bbox"]) for e in elements if e["kind"] in ("image", "table", "diagram")]
        taken += [Rect.of(p["bullet"]["bbox"]) for e in elements if e["kind"] == "text"
                  for p in e["paragraphs"] if p["bullet"] and p["bullet"].get("bbox")]
        panels = [p["bbox"] for p in self.panels if p["fill"] and not p["image"]
                  and p["bbox"].x0 > 1 and p["bbox"].y0 > 1 and p["bbox"].x1 < self.W - 1 and p["bbox"].y1 < self.H - 1]
        out = []
        for c in (cluster_rects(self.graphics, gap=0.5) if self.graphics and panels else []):
            if max(c.w, c.h) >= 25 or any(t.expand(0.5).intersects(c) for t in taken) or \
                    not any(p.expand(-0.5).contains_rect(c) for p in panels) or \
                    any(s.rect.intersects(c) for s in spans if s.text.strip()):
                continue
            out.append({"id": f"p{self.page['index']}k{len(out)}", "kind": "image", "role": "icon",
                        "bbox": c.expand(1.0).as_list(), "spans": []})
        return out

    def release_stranded_labels(self, lines: list[Line], joined: list[Line]) -> None:
        """Labels joined to graphics that `figures` will make no picture of - a cluster under
        25 pt (a timeline's tick mark and its year), or one in the header/footer band - stay
        text: as a figure's they were left in the background, where no one can edit them."""
        if not joined:
            return
        labels = [s.rect for l in lines if l.reason in ("figure", "rotated") for s in l.spans]
        regions = [r for r in self.regions if not self.band_ornament(r)]
        for c in cluster_rects(regions + labels + self.column_bridges, gap=0.8 * self.body):
            if max(c.w, c.h) < 25 or ((c.y1 <= 0.15 * self.H or c.y0 >= 0.88 * self.H) and c.h <= 0.1 * self.H):
                for l in joined:
                    if c.expand(0.1).contains_rect(l.rect):
                        l.reason = None

    @staticmethod
    def wrapped_end(line: Line, lines: list[Line]) -> bool:
        """The last words of a paragraph wrapped onto a line of their own ("(no defiers).",
        "cycles"): one pitch below a line of prose in the same size, starting where one of its
        words does. Near a list's drawn label they were taken for a figure's label and left in
        the background, and the native paragraph ended a line early."""
        return any(o is not line and o.reason is None and len(o.text) >= 20 and abs(o.size - line.size) <= 0.05 * line.size
                   and 0.9 * line.size <= line.baseline - o.baseline <= 1.6 * line.size
                   and any(abs(s.rect.x0 - line.rect.x0) <= 1.0 for s in o.content) for o in lines)

    def clip_to_bands(self, c: Rect, box: Rect) -> Rect:
        """A figure's box ends where a headline or footline band drawn after it begins: the band
        hides what of the figure reaches under it (tick labels of a chart set just above the
        footline), and a picture reaching into it showed the band's colours over the layout's
        footline texts, cut in half. (A band drawn first is under the figure on the page too.)"""
        order = {d["id"]: i for i, d in enumerate(self.page["drawings"])}
        members = [order[i] for i, r in self.graphic_drawings.items() if c.expand(0.5).contains_rect(r)]
        if not members:
            return box
        last = max(members)
        # (a band of boxes side by side is panels on the edge, see on_edge_artwork)
        decor = {tuple(r.as_list()) for r in self.decorations}
        panels = {p["id"] for p in self.panels if not p["image"]}
        x0, y0, x1, y1 = box.as_list()
        for i, d in enumerate(self.page["drawings"]):
            r = Rect.of(d["bbox"])
            if i < last or r.h > 0.15 * self.H or r.x1 <= box.x0 or r.x0 >= box.x1 or not (
                    d["id"] in self.decor_ids or d["id"] in panels or tuple(r.as_list()) in decor):
                continue
            if r.y1 >= self.H - 1 and box.y0 + 0.5 * box.h < r.y0 < y1:
                y1 = r.y0
            elif r.y0 <= 1 and y0 < r.y1 < box.y1 - 0.5 * box.h:
                y0 = r.y1
        return Rect(x0, y0, x1, y1)

    def band_ornament(self, r: Rect) -> bool:
        """A small graphic in the header or footer band (navigation symbols, a title's accent
        bar): theme furniture that stays in the background and joins nothing into a figure."""
        return (r.y1 <= 0.15 * self.H or r.y0 >= 0.88 * self.H) and max(r.w, r.h) < 25

    def holds_other_text(self, c: Rect, label_spans: list[Span]) -> bool:
        """Text drawn inside a figure cluster that is none of its labels stays in the
        background (a section number in a filled node, read with its heading as one formula):
        native shapes rebuilt from the cluster would be drawn over it and hide it."""
        labels = {s.id for s in label_spans}
        box = c.expand(0.5)
        centre = lambda r: (r.cx, r.cy)
        return any(s["id"] not in labels and s["text"].strip() and box.contains(*centre(Rect.of(s["bbox"])))
                   for s in self.page["spans"])

    def plain_rectangles(self, c: Rect, label_spans: list[Span], index: int) -> list[dict]:
        """A figure cluster that is only opaque filled rectangles without text (progress bars,
        colour swatches, \\rule): native rectangle shapes."""
        box = c.expand(0.5)
        if any(box.contains_rect(s.rect) for s in label_spans) or self.holds_other_text(c, label_spans) or \
                any(box.intersects(Rect.of(im["bbox"])) for im in self.page["images"]):
            return []
        out = []
        for d in self.page["drawings"]:
            r = Rect.of(d["bbox"])
            if not box.intersects(r) or d["id"] in self.decor_ids or self.is_decoration(r) or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            points = [p for _, pts in (d.get("path") or []) for p in pts]
            axis_aligned = d["items"] == "re" or (set(d["items"]) == {"l"} and points and all(
                min(abs(x - r.x0), abs(x - r.x1)) < 0.05 and min(abs(y - r.y0), abs(y - r.y1)) < 0.05 for x, y in points))
            if not box.contains_rect(r) or d["type"] != "f" or not axis_aligned or not d["fill"] \
                    or d.get("fill_opacity", 1.0) < 0.99:
                return []
            out.append({"id": f"p{self.page['index']}r{index + len(out)}", "kind": "shape", "role": "rule",
                        "bbox": r.as_list(), "fill": d["fill"], "shape": "RECTANGLE", "flip": False,
                        "radius": 0.0, "drawing": d["id"], "spans": []})
        if len(out) > MAX_PLAIN_RECTANGLES:
            # A QR code or a pixel grid (426 modules): a picture, not hundreds of shapes nobody
            # would edit one by one, each its own request.
            return []
        # The track of a progress bar runs across the page like a decoration hairline: it goes
        # along (below the bar) when it has exactly the bar's height and contains it.
        for d in self.page["drawings"]:
            r = Rect.of(d["bbox"])
            if d["type"] == "f" and d["fill"] and d.get("fill_opacity", 1.0) >= 0.99 and d["id"] not in self.decor_ids \
                    and r.x0 > 1 and r.x1 < self.W - 1 and all(o["drawing"] != d["id"] for o in out) \
                    and any(abs(r.y0 - o["bbox"][1]) < 0.1 and abs(r.y1 - o["bbox"][3]) < 0.1
                            and r.x0 <= o["bbox"][0] + 0.1 and r.x1 >= o["bbox"][2] - 0.1 for o in out):
                out.insert(0, {"id": f"p{self.page['index']}r{index + len(out)}", "kind": "shape", "role": "rule",
                               "bbox": r.as_list(), "fill": d["fill"], "shape": "RECTANGLE", "flip": False,
                               "radius": 0.0, "drawing": d["id"], "spans": []})
        return out

    @staticmethod
    def closed_frames(nodes: list[dict], lines: list[dict]) -> None:
        """Four stroked lines closing a rectangle (\\fbox and \\fcolorbox draw their frame side by
        side) become one rectangle node; a filled rectangle right inside the frame (the
        \\fcolorbox background) takes it as its outline."""
        extent = lambda ln, k: sorted((ln["from"][k], ln["to"][k]))
        horizontal = [l for l in lines if "via" not in l and abs(l["from"][1] - l["to"][1]) < 0.05]
        vertical = [l for l in lines if "via" not in l and abs(l["from"][0] - l["to"][0]) < 0.05]
        near = lambda a, b, tol: all(abs(p - q) <= tol for p, q in zip(a, b))
        for top in horizontal:
            for bottom in horizontal:
                y0, y1, tol = top["from"][1], bottom["from"][1], top["width"] + 0.5
                if top not in lines or bottom not in lines or y1 - y0 <= 3 or bottom["stroke"] != top["stroke"] \
                        or not near(extent(bottom, 0), extent(top, 0), tol):
                    continue
                x0, x1 = extent(top, 0)
                sides = [v for v in vertical if v in lines and v["stroke"] == top["stroke"] and near(extent(v, 1), (y0, y1), tol)]
                left = next((v for v in sides if abs(v["from"][0] - x0) <= tol), None)
                right = next((v for v in sides if abs(v["from"][0] - x1) <= tol), None)
                if left is None or right is None or left is right:
                    continue
                rect = Rect(left["from"][0], y0, right["from"][0], y1)
                for ln in (top, bottom, left, right):
                    lines.remove(ln)
                inner = next((n for n in nodes if n["shape"] == "RECTANGLE" and n["stroke"] is None and
                              rect.expand(tol).contains_rect(n["rect"]) and n["rect"].w >= rect.w - 2 * tol
                              and n["rect"].h >= rect.h - 2 * tol), None)
                if inner:
                    inner.update(rect=rect, stroke=top["stroke"], width=top["width"])
                else:
                    nodes.append({"rect": rect, "shape": "RECTANGLE", "spans": [], "fill": None,
                                  "stroke": top["stroke"], "width": top["width"]})

    def diagram_from(self, c: Rect, label_spans: list[Span], index: int) -> dict | None:
        """A figure cluster made only of simple nodes (rectangles, rounded rectangles, ellipses)
        with their text inside, straight lines and arrow tips: rebuilt from native Slides
        shapes and lines. Anything else (curves, images, math, loose labels) keeps it a picture."""
        box = c.expand(0.5)
        if any(box.contains_rect(Rect.of(im["bbox"])) for im in self.page["images"]) or self.holds_other_text(c, label_spans):
            return None
        nodes, lines, tips = [], [], []
        for d in self.page["drawings"]:
            r = Rect.of(d["bbox"])
            if not box.contains_rect(r) or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            path = d.get("path")
            if path is None:
                return None
            if d.get("soft_mask") or d.get("fill_opacity", 1.0) < 0.99 or d.get("stroke_opacity", 1.0) < 0.99:
                # A see-through node or line (opacity=0.3 on the steps still to come, a
                # multiplied fill): native shapes came out opaque, the dimmed step drawn in full.
                return None
            ops = "".join(op for op, _ in path)
            shape = {"re": "RECTANGLE", "lclclclc": "ROUND_RECTANGLE", "clclclcl": "ROUND_RECTANGLE",
                     "cccc": "ELLIPSE" if upright_ellipse(path, r) else None}.get(ops)
            points = [p for _, pts in path for p in pts]
            if shape is None and max(r.w, r.h) > 6 and ops in ("llll", "lll") and "f" in d["type"] + "f":
                shape = polygon_shape(points, r)  # decision diamonds, triangles
            if shape and r.w > 3 and r.h > 3:
                nodes.append({"rect": r, "shape": shape, "spans": [],
                              "fill": d["fill"] if "f" in d["type"] else None,
                              "stroke": d["stroke"] if "s" in d["type"] else None, "width": d["width"],
                              # rounded corners=3pt: without it the node got Slides' default rounding
                              **({"radius": max(d["corners"].values())}
                                 if shape == "ROUND_RECTANGLE" and d.get("corners") else {})})
            elif d["type"] == "s" and set(ops) == {"l"} and max(r.w, r.h) > 6:
                segments = [(tuple(a), tuple(b)) for _, (a, b) in path]
                style = {"stroke": d["stroke"] or "#000000", "width": d["width"] or 0.4, "arrow_from": None, "arrow_to": None}
                (p0, p1), (p1b, p2) = segments if len(segments) == 2 else ((None, None), (None, None))
                if p0 and math.dist(p1, p1b) < 0.05 and (abs(p0[0] - p1[0]) < 0.05) != (abs(p0[1] - p1[1]) < 0.05) \
                        and (abs(p1[0] - p2[0]) < 0.05) != (abs(p1[1] - p2[1]) < 0.05) \
                        and (abs(p0[0] - p1[0]) < 0.05) != (abs(p1[0] - p2[0]) < 0.05):
                    # An orthogonal connector (|- or -|): one elbow line, vertical first. A -| one
                    # is written from its other end: Slides drew bentConnector3 at adj 1 (turn at
                    # the end) with its turn halfway, three segments; adj 0 draws |- right.
                    if abs(p0[0] - p1[0]) >= 0.05:
                        p0, p2 = p2, p0
                    lines.append({"from": list(p0), "via": list(p1), "to": list(p2), "bend": "vh", **style})
                else:  # straight lines, and other polylines one segment at a time
                    for (x1, y1), (x2, y2) in segments:
                        lines.append({"from": [x1, y1], "to": [x2, y2], **style})
            elif max(r.w, r.h) <= 6 and set(ops) <= {"c", "l"}:
                # Arrow heads are small separate paths: stroked (->), filled triangles (latex)
                # or filled concave quadrilaterals (stealth).
                if d["type"] == "s":
                    style = "OPEN_ARROW"
                else:
                    style = "STEALTH_ARROW" if ops == "llll" else "FILL_ARROW"
                points = [p for _, pts in path for p in pts]
                tips.append((r, style, points, d["width"] if "s" in d["type"] and d["width"] else 0.0))
            else:
                return None
        self.closed_frames(nodes, lines)
        if not nodes:
            return None
        # Shapes that cross each other (a Venn diagram): its words are placed by region - the
        # lens, one circle's own part - and a node's text is set centred in all of it.
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                ra, rb = a["rect"], b["rect"]
                if overlap(ra, rb) > 0.05 * min(ra.w * ra.h, rb.w * rb.h) and \
                        not ra.contains_rect(rb) and not rb.contains_rect(ra):
                    return None
        for tip, style, points, outline in tips:
            ends = [(ln, end) for ln in lines for end in ("from", "to") if tip.expand(1).contains(*ln[end])]
            if not ends:
                return None
            ln, end = ends[0]
            ln["arrow_" + end] = style
            if style != "OPEN_ARROW":
                # TikZ stops the line where a filled head begins; Slides draws the head at the
                # line's end, so extend the line to the tip.
                other = ln.get("via") or ln["to" if end == "from" else "from"]
                ux, uy = ln[end][0] - other[0], ln[end][1] - other[1]
                length = (ux * ux + uy * uy) ** 0.5 or 1.0
                ux, uy = ux / length, uy / length
                reach = max((px - ln[end][0]) * ux + (py - ln[end][1]) * uy for px, py in points)
                # A head filled and stroked (ultra thick ->) reaches past its path by its outline's
                # mitred point: the line stopped that short of the node, the black edge drawn
                # under it showing as a stub at the tip.
                reach += miter_reach(points, (ux, uy), outline)
                if reach > 0:
                    ln[end] = [round(ln[end][0] + reach * ux, 2), round(ln[end][1] + reach * uy, 2)]

        spans = [s for s in label_spans if box.contains_rect(s.rect)]
        # A label with a script (R_s, C_dl in a circuit) is a formula: set as one run of plain
        # text it read "Rs", and a free label ran into the next ("ct Z").
        if any(b is not a and -0.05 * a.size <= b.rect.x0 - a.rect.x1 <= 0.15 * a.size and b.size < 0.85 * a.size
               and 0.1 * a.size < abs(b.baseline - a.baseline) < 0.6 * a.size for a in spans for b in spans):
            return None
        free: list[Span] = []
        area = lambda n: n["rect"].w * n["rect"].h
        seen: list[Span] = []
        for s in spans:
            if s.info.family in ("math", "icon") or "�" in s.text or not s.horizontal:
                # (an icon font's glyph has no Unicode: as a node's label it read U+FFFD, a
                # diamond with a question mark, where the picture shows the icon)
                return None
            # A node drawn again on a later overlay step (\node<2->[fill=yellow] at (a) {A})
            # paints its label a second time on the same spot: one label, and it goes to the
            # copy on top - the last drawn of the smallest nodes around it. Given to the first,
            # it came out doubled ("LexerLexer") under an empty filled box.
            if any(o.text == s.text and abs(o.rect.x0 - s.rect.x0) <= 0.1 and abs(o.baseline - s.baseline) <= 0.1
                   and abs(o.size - s.size) <= 0.1 for o in seen):
                continue
            seen.append(s)
            owners = [n for n in nodes if n["rect"].contains(s.rect.cx, s.rect.cy)]
            if owners:
                smallest = min(map(area, owners))
                [n for n in owners if area(n) <= 1.02 * smallest + 0.01][-1]["spans"].append(s)
            else:
                free.append(s)  # edge labels and captions: a text box in the group
        if sum(n["shape"] is not None and not n["spans"] for n in nodes) > MAX_PLAIN_RECTANGLES:
            return None  # a QR code, a pixel grid: modules, not nodes
        # Free labels on one baseline and close together are one label. (Close on both sides:
        # two edge labels whose baselines round apart sort right to left, and the one-sided gap
        # joined them across the node between - "connect SYN+ACK" over two arrows.)
        for s in sorted(free, key=lambda s: (round(s.baseline), s.rect.x0)):
            last = nodes[-1] if nodes and nodes[-1]["shape"] is None else None
            if last and abs(last["spans"][-1].baseline - s.baseline) <= 0.3 * s.size and \
                    -0.3 * s.size <= s.rect.x0 - last["spans"][-1].rect.x1 <= 0.5 * s.size:
                last["spans"].append(s)
                last["rect"] = last["rect"].union(s.rect)
            else:
                nodes.append({"rect": s.rect, "shape": None, "spans": [s], "fill": None, "stroke": None, "width": None})

        out_nodes = []
        for n in nodes:
            rows: list[list[Span]] = []
            for s in sorted(n["spans"], key=lambda s: (s.baseline, s.rect.x0)):
                if rows and abs(s.baseline - rows[-1][0].baseline) <= 0.5 * s.size:
                    rows[-1].append(s)
                else:
                    rows.append([s])
            out_nodes.append({
                "bbox": n["rect"].as_list(), "shape": n["shape"], "fill": n["fill"], "stroke": n["stroke"],
                "width": n["width"], "paragraphs": [span_runs(sorted(row, key=lambda s: s.rect.x0)) for row in rows],
                "baselines": [round(row[0].baseline, 2) for row in rows],
                "label_w": round(max((max(s.rect.x1 for s in row) - min(s.rect.x0 for s in row) for row in rows), default=0.0), 2),
                "text": card_text(n["rect"], rows) if n["shape"] else None,
                **({"radius": n["radius"]} if "radius" in n else {}),
            })
        return {"id": f"p{self.page['index']}dg{index}", "kind": "diagram", "role": "figure",
                "bbox": c.expand(1.0).as_list(), "nodes": out_nodes, "lines": lines,
                "spans": [s.id for s in spans]}

    def fill_grid(self, c: Rect) -> list[Rect]:
        """Opaque rectangles tiling a grid inside c, edge to edge: the \\cellcolor / \\rowcolor
        shading of a table that has fewer than two full rules (a heatmap, \\rowcolors stripes).
        Two or more rows and columns of them; empty when there is no such grid."""
        box = c.expand(0.5)
        fills = [Rect.of(d["bbox"]) for d in self.page["drawings"]
                 if d["type"] == "f" and d["items"] == "re" and d.get("fill_opacity", 1.0) >= 0.99
                 and d["id"] not in self.decor_ids and box.contains_rect(Rect.of(d["bbox"]))
                 and Rect.of(d["bbox"]).w * Rect.of(d["bbox"]).h < 0.95 * self.W * self.H
                 and min(Rect.of(d["bbox"]).w, Rect.of(d["bbox"]).h) > 3]
        if len(fills) < 4:
            return []

        def beside(a: Rect, b: Rect) -> bool:
            side = (abs(a.x1 - b.x0) <= 0.6 or abs(b.x1 - a.x0) <= 0.6) and min(a.y1, b.y1) - max(a.y0, b.y0) > 0.5 * min(a.h, b.h)
            above = (abs(a.y1 - b.y0) <= 0.6 or abs(b.y1 - a.y0) <= 0.6) and min(a.x1, b.x1) - max(a.x0, b.x0) > 0.5 * min(a.w, b.w)
            return side or above
        if not all(any(beside(a, b) for b in fills if b is not a) for a in fills):
            return []
        xs = sorted({round(v) for f in fills for v in (f.x0, f.x1)})
        ys = sorted({round(v) for f in fills for v in (f.y0, f.y1)})
        return fills if len(xs) >= 3 and len(ys) >= 3 else []

    @staticmethod
    def splits_cells(diagram: dict, label_spans: list[Span]) -> bool:
        """A diagram node holding words on both sides of a vertical line that crosses it: the
        cells of a ruled table row (or the fields of a record node), which one node sets as one
        paragraph, the words run together across the rules."""
        verticals = [ln for ln in diagram["lines"] if "via" not in ln and abs(ln["from"][0] - ln["to"][0]) < 0.05]
        for n in diagram["nodes"]:
            if not n["shape"]:
                continue
            r = Rect.of(n["bbox"])
            words = [s for s in label_spans if r.contains_rect(s.rect)]
            for ln in verticals:
                x, (y0, y1) = ln["from"][0], sorted((ln["from"][1], ln["to"][1]))
                if r.x0 + 1 < x < r.x1 - 1 and any(y0 <= s.rect.cy <= y1 and s.rect.x1 <= x for s in words) and \
                        any(y0 <= s.rect.cy <= y1 and s.rect.x0 >= x for s in words):
                    return True
        return False

    def table_from(self, c: Rect, label_spans: list[Span], text_rects: list[Rect], index: int) -> dict | None:
        """A figure cluster that is really a plain table: text framed by horizontal rules of
        equal extent and nothing else. Returns a native table element, or None."""
        groups = [g for g in self.table_rules if c.expand(1).contains_rect(union_all(r["rect"] for r in g))]
        # Fewer than two full rules, but cell shading edge to edge (a heatmap, \rowcolors): the
        # shading and the rules there are frame the table.
        grid = [] if groups else self.fill_grid(c)
        if grid:
            frame = union_all(grid)
        elif groups:
            # \cline{2-3} twice over the same columns is a group of equal-extent rules of its own:
            # inside the widest group's frame it is one of that table's partial rules, not a second
            # table (refused, the grid became a diagram with each row's words in one box).
            outer = max(groups, key=lambda g: (union_all(r["rect"] for r in g).w, len(g)))
            frame = union_all(r["rect"] for r in outer)
            if any(g is not outer and not frame.expand(1).contains_rect(union_all(r["rect"] for r in g)) for g in groups):
                return None
            if any(g not in groups and union_all(r["rect"] for r in g).expand(1).contains_rect(frame.expand(1))
                   for g in self.table_rules):
                return None  # the \cline pieces of a larger table: that table's, whole
        else:
            return None
        box = c.expand(0.5)
        if any(box.contains_rect(Rect.of(im["bbox"])) for im in self.page["images"]):
            return None
        # Every drawing must be a horizontal or vertical rule (\hline, \cline, |, booktabs);
        # cell shading and anything else keep the table a picture.
        horizontal, vertical, fills = [], [], []
        for d in self.page["drawings"]:
            r = Rect.of(d["bbox"])
            if d["id"] in self.decor_ids or not box.contains_rect(r) or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            color = (d["fill"] if d["type"] == "f" else d["stroke"]) or "#000000"
            stroke = d["type"] == "s" and d["items"] == "l"
            fill = d["type"] == "f" and d["items"] == "re"
            if (stroke and r.h <= 1.0) or (fill and r.h <= 1.5 and r.w >= 3):
                horizontal.append({"rect": r, "color": color, "weight": r.h if fill else (d["width"] or 0.4)})
            elif (stroke and r.w <= 1.0) or (fill and r.w <= 1.5 and r.h >= 3):
                vertical.append({"rect": r, "color": color, "weight": r.w if fill else (d["width"] or 0.4)})
            elif fill and d.get("fill_opacity", 1.0) >= 0.99:
                fills.append({"rect": r, "color": d["fill"]})  # \rowcolor, \cellcolor
            else:
                return None
        if vertical or grid:
            frame = union_all([frame] + [v["rect"] for v in vertical + (horizontal if grid else [])])
        spans = sorted((s for s in label_spans if box.contains_rect(s.rect)), key=lambda s: s.baseline)
        if not spans or any(not s.horizontal or s.font.upper().startswith("CMEX") or "�" in s.text for s in spans) or \
                any(box.contains_rect(b) for b in self.bars):  # big operators, fractions: keep the picture
            return None
        size = max(s.size for s in spans)

        # Rows by baseline. A row sitting halfway between its neighbours is a \multirow cell
        # spanning both of them.
        rows: list[list[Span]] = []
        for s in spans:
            anchor = max(rows[-1], key=lambda x: x.size) if rows else None  # the row's normal-size text
            if rows and abs(s.baseline - anchor.baseline) <= 0.5 * max(s.size, anchor.size):
                rows[-1].append(s)
            else:
                rows.append([s])
        base = [statistics.fmean(s.baseline for s in row) for row in rows]

        def halfway(i: int) -> bool:
            """Row i sits in the middle of its neighbours, which are one row pitch of this
            table apart (\\arraystretch and \\hline gaps make that pitch well over an em, so
            the half pitches of a \\multirow are too)."""
            gaps = (base[i] - base[i - 1], base[i + 1] - base[i])
            if max(gaps) < 0.75 * size:
                return True
            others = [b - a for k, (a, b) in enumerate(zip(base, base[1:])) if k not in (i - 1, i)]
            return bool(others) and abs(gaps[0] - gaps[1]) <= 0.4 * size and max(gaps) < 1.2 * size and \
                sum(gaps) <= 1.15 * statistics.median(others)
        between = {i for i in range(1, len(rows) - 1)
                   if halfway(i) and not any(a.rect.x0 < b.rect.x1 and b.rect.x0 < a.rect.x1
                                             for a in rows[i] for b in rows[i - 1] + rows[i + 1])}
        if any(i - 1 in between for i in between):
            return None
        grid_rows = [row for i, row in enumerate(rows) if i not in between]

        # A cell set in a paragraph column (p{3cm}) wraps: its next lines are rows of their own
        # holding nothing but words in that column, starting where the cell starts, and its lines
        # are justified, their word spaces stretched past the half em that parts two cells. Each
        # such line is one chunk from the cell's left edge to its right: its words cut into
        # chunks tangled the columns and the table was refused, and as text boxes the cell's
        # first line joined the numbers beside it and reflowed across their column in Slides.
        wrapped: dict[int, list[list[float]]] = {}  # row index -> its wrapped cells' [x0, x1]
        continued: set[int] = set()  # rows holding nothing but the next lines of cells above

        def ruled(a: Span, b: Span) -> bool:
            """A vertical rule between two words of a row (not one of another row: a
            \\multicolumn's words run across the rule the rows above and below have there)."""
            y = b.baseline - 0.3 * b.size
            return any(a.rect.x1 < v["rect"].cx < b.rect.x0 and v["rect"].y0 <= y <= v["rect"].y1 for v in vertical)

        def phrase(spans: list[Span], x0: float) -> list[Span]:
            out = []
            for s in sorted(spans, key=lambda s: s.rect.x0):
                if not out and abs(s.rect.x0 - x0) <= 0.5 or out and s.rect.x0 - out[-1].rect.x1 <= s.size and not ruled(out[-1], s):
                    out.append(s)
                elif out:
                    break
            return out

        def phrases(spans: list[Span]) -> list[list[Span]]:
            out: list[list[Span]] = []
            for s in sorted(spans, key=lambda s: s.rect.x0):
                if out and s.rect.x0 - out[-1][-1].rect.x1 <= s.size and not ruled(out[-1][-1], s):
                    out[-1].append(s)
                else:
                    out.append([s])
            return out

        def words(spans: list[Span]) -> str:
            return " ".join(s.text for s in spans).strip()

        def right_end(x0: float) -> float:
            """Where the longest line starting at x0 ends: a justified cell's full lines."""
            return max((p[-1].rect.x1 for row in rows for p in [phrase(row, x0)] if p), default=x0)

        for i in range(len(rows) - 1):
            j = i + 1
            pitch = base[j] - base[i]
            if i in between or j in between or pitch > 1.6 * size:
                continue
            parts = phrases(rows[j])
            if pitch > 1.35 * size:
                # Rows set as far apart as the lines of a cell (\arraystretch with \linespread):
                # a line of words running on in lower case from a full (justified) line above.
                if not all(words(p)[:1].islower() and (f := phrase(rows[i], p[0].rect.x0)) and
                           f[-1].rect.x1 >= right_end(p[0].rect.x0) - 0.5 for p in parts):
                    continue
            if len(parts) > 1:
                # Several paragraph columns wrapping in one row (|l|X|X|): the next row holds
                # the next line of each, and nothing in the columns to their left. Each line
                # runs on from a phrase of the row above (lower case, after two or more words),
                # which a row of cells in left-aligned columns does not.
                if min(s.rect.x0 for s in rows[j]) <= min(s.rect.x0 for s in rows[i]) + 1:
                    continue
                firsts = [phrase(rows[i], p[0].rect.x0) for p in parts]
                if not all(f and len(words(f).split()) >= 2 and words(p)[:1].islower() for f, p in zip(firsts, parts)):
                    continue
            else:
                firsts = [phrase(rows[i], parts[0][0].rect.x0)]
                if not firsts[0]:
                    continue
            cells: list[list[float]] = []
            for p, first in zip(parts, firsts):
                x0 = p[0].rect.x0
                prev = next((w for w in wrapped.get(i, []) if abs(w[0] - x0) <= 0.5), None)
                x1 = max(first[-1].rect.x1, prev[1] if prev else 0)
                if x1 < p[-1].rect.x1 - 0.5:
                    break  # a paragraph's first line is full; this one ends short of the next
                cells.append([x0, max(x1, p[-1].rect.x1)])
            else:
                for x0, x1 in cells:
                    for r in (i, j):
                        at = next((w for w in wrapped.setdefault(r, []) if abs(w[0] - x0) <= 0.5), None)
                        if at:
                            at[1] = max(at[1], x1)
                        else:
                            wrapped[r].append([x0, x1])
                continued.add(j)

        def chunks_of(row: list[Span], i: int) -> list[list[Span]]:
            chunks: list[list[Span]] = []
            cells = []
            for x0, x1 in wrapped.get(i, []):
                cell = sorted((s for s in row if s.rect.x0 >= x0 - 0.5 and s.rect.x1 <= x1 + 0.5), key=lambda s: s.rect.x0)
                row = [s for s in row if s not in cell]
                cells += [cell] if cell else []
            for s in sorted(row, key=lambda s: s.rect.x0):
                if chunks and s.rect.x0 - chunks[-1][-1].rect.x1 <= 0.5 * size and not ruled(chunks[-1][-1], s):
                    chunks[-1].append(s)
                else:
                    chunks.append([s])
            return sorted(chunks + cells, key=lambda ch: ch[0].rect.x0) if cells else chunks

        # (row index in grid_rows, row span, chunk)
        items = []
        for i, row in enumerate(rows):
            r = sum(1 for j in range(i) if j not in between)
            for ch in chunks_of(row, i):
                items.append((r - 1, 2, ch) if i in between else (r, 1, ch))

        def extent(ch):
            return ch[0].rect.x0, ch[-1].rect.x1

        # A chunk overlapping two separate chunks of another row (\multicolumn), or crossing a
        # vertical rule, spans several columns; columns come from the other chunks.
        def spanning(item) -> bool:
            r, _, ch = item
            x0, x1 = extent(ch)
            if any(x0 + 1 < v["rect"].cx < x1 - 1 for v in vertical):
                return True
            for r2 in {it[0] for it in items if it[0] != r}:
                under = sorted(extent(it[2]) for it in items if it[0] == r2 and extent(it[2])[0] < x1 and x0 < extent(it[2])[1])
                if any(b[0] > a[1] for a, b in zip(under, under[1:])):
                    return True
            return False

        # Columns set closer than half an em (\tabcolsep cut down, @{\hspace{4pt}}) join a row's
        # cells into one chunk as if they were words of one phrase, and that chunk spans the
        # table - in Slides one merged cell, too narrow for its words, which wraps. Cut such a
        # chunk between two words where no other row has anything, and every other row reaching
        # both sides has a gap there, when every piece lines up with the cell under it in every
        # other row (left, right or centre edge: a \multicolumn header centred over two columns has
        # its word gap on the column gap too, and its words line up with nothing - or with one
        # cell somewhere by chance, which is why it is every row) and no piece spans anything.
        def cut(item) -> list:
            r, rs, ch = item
            others = [extent(it[2]) for it in items if it[0] != r]
            rows_of = [[extent(it[2]) for it in items if it[0] == r2] for r2 in {it[0] for it in items if it[0] != r}]
            pieces, start = [], 0
            for k in range(1, len(ch)):
                a, b = ch[k - 1].rect.x1, ch[k].rect.x0
                x = (a + b) / 2
                if not any(x0 - 0.5 < x < x1 + 0.5 for x0, x1 in others) and \
                        any(x1 <= a for x0, x1 in others) and any(x0 >= b for x0, x1 in others):
                    pieces.append(ch[start:k])
                    start = k
            pieces.append(ch[start:])

            def lined_up(p) -> bool:
                x0, x1 = extent(p)
                under = [(o0, o1) for row in rows_of for o0, o1 in row if o0 < x1 and x0 < o1]
                return bool(under) and all(abs(x0 - o0) <= 0.5 or abs(x1 - o1) <= 0.5 or abs(x0 + x1 - o0 - o1) <= 1
                                           for o0, o1 in under)
            return [(r, rs, p) for p in pieces] if all(map(lined_up, pieces)) else [item]

        for it in [it for it in items if len(it[2]) > 1 and spanning(it)]:
            parts = cut(it)
            if len(parts) > 1:
                at = items.index(it)
                items[at:at + 1] = parts
                if any(spanning(p) for p in parts):
                    items[at:at + len(parts)] = [it]
        wide = [it for it in items if spanning(it)]
        intervals = sorted(extent(it[2]) for it in items if it not in wide)
        columns: list[list[float]] = []
        for x0, x1 in intervals:
            if columns and x0 < columns[-1][1] + 1 and not any(columns[-1][1] - 1 < v["rect"].cx < x0 + 1 for v in vertical):
                columns[-1][1] = max(columns[-1][1], x1)
            else:
                columns.append([x0, x1])
        if not columns:
            return None
        if any(f["rect"].x0 < frame.x0 - 1.5 or f["rect"].x1 > frame.x1 + 1.5 for f in fills):
            return None  # shading beyond the table: a coloured box around it
        bounds = [frame.x0]
        # Cell shading starts exactly at TeX's column edges.
        fill_edges = sorted({round(f["rect"].x0, 2) for f in fills})
        for a, b in zip(columns, columns[1:]):
            rule = [v["rect"].cx for v in vertical if a[1] - 1 <= v["rect"].cx <= b[0] + 1] or \
                [x for x in fill_edges if a[1] - 1 <= x <= b[0] + 1]
            bounds.append(rule[0] if rule else (a[1] + b[0]) / 2)
        bounds.append(frame.x1)

        n_rows, n_cols = len(grid_rows), len(columns)
        cells = [[[] for _ in columns] for _ in grid_rows]
        placed: list[list[list[Span]]] = [[] for _ in columns]
        heads: list[list[Span] | None] = [None] * len(columns)  # (each column's cell in the first row)
        extent_of: dict[int, tuple[float, float]] = {}  # (a merged cell's words, by its index in merges)
        merges, covered = [], {}
        for it in items:
            r, rs, ch = it
            x0, x1 = extent(ch)
            cols = [i for i in range(n_cols) if bounds[i] < x1 - 0.5 and x0 + 0.5 < bounds[i + 1]]
            if not cols:
                return None
            c0, cs = cols[0], len(cols)
            for rr in range(r, r + rs):
                for cc in range(c0, c0 + cs):
                    if covered.get((rr, cc), it) is not it:
                        return None  # overlapping cells: not a grid we understand
                    covered[(rr, cc)] = it
            cells[r][c0].extend(ch)
            if rs > 1 or cs > 1:
                mid = (bounds[c0] + bounds[c0 + cs]) / 2
                align = "center" if abs((x0 + x1) / 2 - mid) <= 2 else "left" if x0 - bounds[c0] < bounds[c0 + cs] - x1 else "right"
                merges.append({"row": r, "col": c0, "rows": rs, "cols": cs, "align": align})
                extent_of[len(merges) - 1] = (x0, x1)
            else:
                placed[c0].append(ch)
                if r == 0 and rs == 1:
                    heads[c0] = ch
        col_info = []

        def aligned(chunks: list[list[Span]], x0: float, x1: float) -> str:
            left = all(abs(ch[0].rect.x0 - x0) <= 1 for ch in chunks)
            right = all(abs(ch[-1].rect.x1 - x1) <= 1 for ch in chunks)
            digits = sum(c.isdigit() for ch in chunks for s in ch for c in s.text)
            letters = sum(c.isalpha() for ch in chunks for s in ch for c in s.text)
            if left and right and digits > letters:
                return "right"  # equally wide numbers: right-aligned, like a number column
            return "left" if left else "right" if right else "center"

        for (x0, x1), chunks, head in zip(columns, placed, heads):
            info = {"x0": round(x0, 2), "x1": round(x1, 2), "align": aligned(chunks, x0, x1)}
            # A column head set otherwise than its body (\thead centred over a left column, an S
            # column's head centred over numbers set flush right): the head row keeps its own
            # alignment (`head`) and the body its own, to the body's edges (`body`). As one, a
            # centred head took the body's left edge, or every number was centred.
            body = [ch for ch in chunks if ch is not head]
            if head is not None and len(body) >= 2:
                bx0, bx1 = min(ch[0].rect.x0 for ch in body), max(ch[-1].rect.x1 for ch in body)
                hx0, hx1 = head[0].rect.x0, head[-1].rect.x1
                if abs((hx1 - hx0) - (bx1 - bx0)) <= 2 or \
                        all(abs(ch[0].rect.x0 - bx0) <= 1 and abs(ch[-1].rect.x1 - bx1) <= 1 for ch in body):
                    col_info.append(info)  # (a head as wide as its body, or a body of equally wide
                    continue               # cells - ticks, numbers: set alike every way)
                how = aligned(body, bx0, bx1)
                own = "left" if abs(hx0 - bx0) <= 1 else "right" if abs(hx1 - bx1) <= 1 else \
                    "center" if abs((hx0 + hx1) - (bx0 + bx1)) <= 3 else None
                if own is not None and (own != how or how != info["align"]):
                    info.update(align=how, head=own, body=[round(bx0, 2), round(bx1, 2)])
            col_info.append(info)
        # A \multirow cell in one column is flush with the column's words or centred among them
        # (a \thead over a left column): its column says which, not the cell's bounds - a short
        # word flush left in a narrow column sits near the middle of those either way.
        for k, (x0, x1) in extent_of.items():
            m = merges[k]
            if m["cols"] == 1:
                cx0, cx1 = col_info[m["col"]].get("body") or (col_info[m["col"]]["x0"], col_info[m["col"]]["x1"])
                m["align"] = "left" if abs(x0 - cx0) <= 1 else "right" if abs(x1 - cx1) <= 1 else \
                    "center" if abs((x0 + x1) - (cx0 + cx1)) <= 3 else m["align"]

        def line_base(row: list[Span]) -> float:
            return statistics.fmean(s.baseline for s in row if s.size >= 0.9 * max(x.size for x in row))

        # A wrapped cell's next lines were rows of their own up to here (columns, merges and
        # alignments are found line by line); now they join the cell they continue, one cell of
        # several lines that Slides wraps in its column as TeX did - and wraps again when a person
        # types into it. Not across a merged cell: that grid is not one we understand line by line.
        row_lines = [1] * n_rows
        last_line = [line_base(row) for row in grid_rows]  # a row's last baseline
        cell_lines = [[[cell] if cell else [] for cell in row] for row in cells]
        cont = {sum(1 for j in range(i) if j not in between) for i in continued}
        merged_rows = {g for m in merges for g in range(m["row"], m["row"] + m["rows"])}
        if cont and not cont & merged_rows and 0 not in cont:
            keep, head_of = [], {}
            for g in range(n_rows):
                if g in cont:
                    head_of[g] = keep[-1]
                else:
                    keep.append(g)
            for g, h in sorted(head_of.items()):
                row_lines[h] += 1
                last_line[h] = last_line[g]
                for cc in range(n_cols):
                    if cells[g][cc]:
                        cell_lines[h][cc].append(cells[g][cc])
            at = {g: k for k, g in enumerate(keep)}
            merges = [{**m, "row": at[m["row"]]} for m in merges]
            grid_rows, cell_lines = [grid_rows[g] for g in keep], [cell_lines[g] for g in keep]
            row_lines, last_line = [row_lines[g] for g in keep], [last_line[g] for g in keep]
            n_rows = len(keep)
        rows = grid_rows

        baselines = [line_base(row) for row in rows]

        # Borders: rules across the whole table stay row rules; partial rules (\cline,
        # \cmidrule) and vertical rules become the borders of the cells they run along.
        def row_boundary(y: float) -> int:
            return sum(b < y for b in baselines)

        borders = []
        full = [h for h in horizontal if h["rect"].x0 <= frame.x0 + 1.5 and h["rect"].x1 >= frame.x1 - 1.5]
        rules = full
        for h in horizontal:
            if h in full:
                continue
            k = row_boundary(h["rect"].cy)
            for cc in range(n_cols):
                # (\cmidrule(l) is trimmed by half an em at its left end: it still underlines
                # every word of the column)
                if (h["rect"].x0 <= bounds[cc] + 2.5 or h["rect"].x0 <= columns[cc][0] + 1) and \
                        (h["rect"].x1 >= bounds[cc + 1] - 2.5 or h["rect"].x1 >= columns[cc][1] - 1):
                    borders.append({"row": min(k, n_rows - 1), "col": cc, "position": "TOP" if k < n_rows else "BOTTOM",
                                    "color": h["color"], "weight": round(h["weight"], 2), "y": round(h["rect"].cy, 2)})
        for v in vertical:
            k = min(range(len(bounds)), key=lambda i: abs(bounds[i] - v["rect"].cx))
            if abs(bounds[k] - v["rect"].cx) > 1.5:
                # The second stroke of a double rule (||, \doublerulesep 2 pt beside the first):
                # a Slides border is one line, the one on the bound is written.
                if any(u is not v and abs(bounds[k] - u["rect"].cx) <= 1.5 and abs(u["rect"].cx - v["rect"].cx) <= 4
                       and u["rect"].y0 < v["rect"].y1 and v["rect"].y0 < u["rect"].y1 for u in vertical):
                    continue
                return None  # a rule inside a column
            for rr, b in enumerate(baselines):
                if v["rect"].y0 <= b - 0.5 * size and v["rect"].y1 >= b:
                    borders.append({"row": rr, "col": min(k, n_cols - 1), "position": "LEFT" if k < n_cols else "RIGHT",
                                    "color": v["color"], "weight": round(v["weight"], 2)})
        pitches = [b - a for a, b in zip(baselines, baselines[1:])] or [1.4 * size]
        # The last row: as tall as the one before it (as a line of a wrapped cell, when that one
        # wraps), plus its own further lines.
        lead = min([(e - b) / (k - 1) for b, e, k in zip(baselines, last_line, row_lines) if k > 1] or pitches)
        last = (pitches[-1] if n_rows < 2 or row_lines[-2] == 1 else lead) + last_line[-1] - baselines[-1]
        heights = pitches + [last] if n_rows > 1 else [last]
        # A Slides row is at least its lines, 1.195 em x lineSpacing for the first and 1.2 em for
        # each further one: emit's tables come with the .pptx, without the 7.2 pt of padding above
        # and below an API-made table has (emit.table_rows). Refuse if the table would grow into
        # content below all the same.
        scale = 720.0 / self.W
        z = size * scale / 1.02
        body = [(1.195 + (k - 1) * 1.2) * z for k in row_lines]
        ratio = min(1.0, max(0.5, min(h * scale / b for h, b in zip(heights, body))))
        row_h = [max(h * scale, b * ratio) / scale for h, b in zip(heights, body)]
        top = baselines[0] - (0.968 * z - 0.72 - (1 - ratio) * 0.9 * z) / scale
        bottom = top + sum(row_h)
        grown = Rect(frame.x0, frame.y1, frame.x1, bottom)
        if bottom > self.H - 2 or any(t.intersects(grown) for t in text_rects) or \
                any(reg.intersects(grown) and not c.expand(0.5).contains_rect(reg) for reg in self.regions):
            return None

        cell_text = [[cell_runs(lines) for lines in row] for row in cell_lines]
        page_color = next((d["fill"].lower() for d in self.page["drawings"] if d["type"] == "f" and d["fill"]
                           and Rect.of(d["bbox"]).w * Rect.of(d["bbox"]).h >= 0.95 * self.W * self.H), "#ffffff")
        # [row, col, where each line after the first starts in the cell's text]: emit makes the
        # column wide enough for every line of the PDF, a hyphenated word whole.
        wrapped_cells = [[r, cc, starts] for r, row in enumerate(cell_text) for cc, (_, starts) in enumerate(row) if starts]
        set_justified = {cc for cc in range(n_cols) if col_info[cc]["align"] == "left" and justified_cells(
            [cell_lines[r][c2] for r, c2, _ in wrapped_cells if c2 == cc], columns[cc][1])}
        justified = [[r, cc] for r, cc, _ in wrapped_cells if cc in set_justified]
        return {
            "id": f"p{self.page['index']}tab{index}", "kind": "table", "role": "table",
            "bbox": c.expand(1.0).as_list(), "frame": frame.as_list(), "size": round(size, 2),
            "row_baselines": [round(b, 2) for b in baselines],
            "row_heights": [round(p, 2) for p in heights],
            **({"row_lines": row_lines, "wrapped": wrapped_cells} if wrapped_cells else {}),
            # [row, col] of wrapped cells set justified (a tabularx X, a p{} column): emit writes
            # them JUSTIFIED, their lines out to the PDF's edge.
            **({"justified": justified} if justified else {}),
            "columns": col_info,
            "bounds": [round(b, 2) for b in bounds],
            "cells": [[runs for runs, _ in row] for row in cell_text],
            "merges": merges,
            # Where each merged cell's words run (by its index in merges): emit indents a flush
            # cell spanning columns as the PDF does. (Kept out of the merges themselves: sync
            # compares those with the base's to refill a table in place.)
            **({"merge_x": [[round(extent_of[k][0], 2), round(extent_of[k][1], 2)] for k in range(len(merges))]}
               if merges else {}),
            "rules": [{"row": min(k, len(rows) - 1), "position": "TOP" if k < len(rows) else "BOTTOM",
                       "color": r["color"], "weight": round(r["weight"], 2), "y": round(r["rect"].cy, 2)}
                      for r in rules for k in [row_boundary(r["rect"].cy)]],
            "borders": borders,
            # Shading per cell: the rows whose baseline and the columns whose middle it covers.
            # (white \rowcolors stripes on an off-white page show: only the page's own colour
            # is left out)
            "fills": [{"row": rr, "col": cc, "color": f["color"]}
                      for f in fills if f["color"].lower() != page_color
                      for rr, b in enumerate(baselines) if f["rect"].y0 <= b <= f["rect"].y1
                      for cc in range(n_cols) if f["rect"].x0 <= (bounds[cc] + bounds[cc + 1]) / 2 <= f["rect"].x1],
            "spans": [s.id for s in spans],
        }

    def shapes(self, lines: list[Line], elements: list[dict]) -> list[dict]:
        """Filled panels (beamer blocks and the like) that can become native shapes.

        Only panels that are pure content containers qualify: not touching the page edge
        (those are theme bars, better left in the background like a layout), opaque, and
        with nothing on top that stays in the background (it would be hidden under the
        shape). The render stage additionally checks the panel really shows its fill colour."""
        used = {sid for e in elements for sid in e["spans"]}
        leftovers = [s.rect for l in lines for s in l.spans if s.id not in used]
        figures = [Rect.of(e["bbox"]) for e in elements if e["kind"] in ("image", "table", "diagram") and not e.get("overlay")]
        figures += [self.graphic_drawings[i] for e in elements for i in e.get("drawings", [])]
        figures += [Rect.of(p["bullet"]["bbox"]) for e in elements if e["kind"] == "text"
                    for p in e["paragraphs"] if p["bullet"] and p["bullet"].get("patch")]
        loose = [g for g in self.graphics if not any(f.expand(0.5).contains_rect(g) for f in figures)]
        # Strokes drawn after a panel that reach onto it (a matrix's quadrant dividers on the
        # panels' shared edges, an axis along their bottom): they stay in the background, and a
        # native panel over it hid them, halfway or whole.
        # (Hairlines that long count as theme decoration: any stroke not in a picture counts, but
        # not one that leaves the background with the text - an underline, a fraction bar.)
        order = {d["id"]: i for i, d in enumerate(self.page["drawings"])}
        with_text = {tuple(s) for e in elements for s in e.get("strokes", [])}
        strokes = [(i, Rect.of(d["bbox"]).expand((d["width"] or 0.4) / 2)) for i, d in enumerate(self.page["drawings"])
                   if d["type"] == "s" and d["id"] not in self.decor_ids and d["id"] not in self.frame_ids
                   and tuple(Rect.of(d["bbox"]).as_list()) not in with_text
                   and not any(f.expand(0.5).contains_rect(Rect.of(d["bbox"])) for f in figures)]
        bullet_images = {p["bullet"]["image"] for e in elements if e["kind"] == "text"
                         for p in e["paragraphs"] if p["bullet"] and p["bullet"]["kind"] == "image"}
        out = []
        for p in sorted(self.panels, key=lambda p: -p["bbox"].w * p["bbox"].h):
            r = p["bbox"]
            # A translucent fill over text (a highlight behind list items) is a translucent
            # shape under the text it covers, grouped with it.
            covered = Counter(self.line_owner[id(l)][0] for l in lines if id(l) in self.line_owner and l.rect.intersects(r))
            if not p["image"] and not p["fill"] and p.get("frame") and not any(
                    Rect.of(e["bbox"]).expand(0.5).contains_rect(r) for e in elements if e["kind"] in ("image", "table", "diagram")):
                frame, k = p["frame"], len(out)
                out += [{"id": f"p{self.page['index']}s{k}r{i}", "kind": "shape", "role": "rule", "bbox": rule,
                         "fill": frame["color"], "shape": "RECTANGLE", "flip": False, "radius": 0.0,
                         "drawing": p["id"], "spans": []} for i, rule in enumerate(frame["rules"])]
                continue
            if p["image"] or not p["fill"] or (p["opacity"] < 0.99 and not covered):
                continue
            if r.x0 <= 1 or r.y0 <= 1 or r.x1 >= self.W - 1 or r.y1 >= self.H - 1:
                continue
            if any(Rect.of(e["bbox"]).expand(0.5).contains_rect(r) for e in elements
                   if e["kind"] in ("image", "table", "diagram") and not e.get("overlay")):
                continue  # already part of a picture
            inner = r.expand(-0.5)
            if any(inner.intersects(x) for x in leftovers):
                continue
            if any(overlap(inner, g) > 0.9 * max(g.w * g.h, 1e-6) for g in loose):
                continue  # graphics mostly on the panel (edge decorations like shadows are fine)
            if p["id"] in order and any(k > order[p["id"]] and overlap(r, band) > 0.01 for k, band in strokes):
                continue
            if any(inner.contains_rect(ir, tol=0) and im["id"] not in bullet_images for im, ir in self.small_images):
                continue
            corners = set(p["corners"])
            if not corners:
                kind, flip = "RECTANGLE", False
            elif corners == {"tl", "tr"}:
                kind, flip = "ROUND_2_SAME_RECTANGLE", False
            elif corners == {"bl", "br"}:
                kind, flip = "ROUND_2_SAME_RECTANGLE", True  # same shape rotated 180°
            else:
                kind, flip = "ROUND_RECTANGLE", False
            if any(o["bbox"] == r.as_list() and o["fill"] == p["fill"] for o in out):
                continue  # the same panel painted twice (a tcolorbox's title tab)
            out.append({"id": f"p{self.page['index']}s{len(out)}", "kind": "shape", "role": "panel",
                        "bbox": r.as_list(), "fill": p["fill"], "shape": kind, "flip": flip,
                        "radius": max(p["corners"].values(), default=0.0), "drawing": p["id"], "spans": []})
            if len(p.get("tiles", [])) > 1:  # (a listing's per-line bands, painted as this one fill)
                out[-1]["tiles"] = p["tiles"]
            frame = p.get("frame")
            if frame and len(frame["sides"]) == 4:
                out[-1].update(bbox=frame["box"], outline={"color": frame["color"], "width": frame["width"]},
                               frame_drawings=frame["ids"])
            elif frame:  # listings' frame=lines, leftline...: the sides drawn are rules over the panel
                panel = out[-1]
                panel["frame_drawings"] = frame["ids"]
                out += [{"id": f"{panel['id']}r{k}", "kind": "shape", "role": "rule", "bbox": rule,
                         "fill": frame["color"], "shape": "RECTANGLE", "flip": False, "radius": 0.0,
                         "drawing": panel["drawing"], "spans": []} for k, rule in enumerate(frame["rules"])]
            if p["opacity"] < 0.99:
                out[-1].update(role="highlight", opacity=round(p["opacity"], 3), anchor=covered.most_common(1)[0][0])
        return self.framed_panels(out)

    @staticmethod
    def framed_panels(shapes: list[dict]) -> list[dict]:
        """A panel painted over a slightly larger one of another colour, inset by the same few
        tenths of a point on every side, is framed by it: tcolorbox (and themes like it) paints
        the frame colour's box and the interior over it. The inner panel becomes one shape with
        that outline, on the frame's centre line; the outer one, which shows only as that ring,
        is no shape of its own (as one it failed render's fill check and the frame was lost)."""
        out = list(shapes)
        for inner in shapes:
            if inner.get("outline") or inner.get("opacity") or inner not in out:
                continue
            ix0, iy0, ix1, iy1 = inner["bbox"]
            for outer in out:
                if outer is inner or outer.get("outline") or outer.get("opacity") or outer["fill"] == inner["fill"]:
                    continue
                ox0, oy0, ox1, oy1 = outer["bbox"]
                insets = (ix0 - ox0, iy0 - oy0, ox1 - ix1, oy1 - iy1)
                if not (0.1 <= min(insets) and max(insets) <= FRAME_RULE_PT and max(insets) - min(insets) <= 0.3):
                    continue
                w = sum(insets) / 4
                inner.update(bbox=[round(ox0 + w / 2, 2), round(oy0 + w / 2, 2), round(ox1 - w / 2, 2), round(oy1 - w / 2, 2)],
                             outline={"color": outer["fill"], "width": round(w, 2)},
                             radius=round(max(outer["radius"] - w / 2, inner["radius"]) if outer["radius"] else inner["radius"], 2),
                             shape=outer["shape"] if outer["shape"] != "RECTANGLE" else inner["shape"],
                             frame_drawings=[outer["drawing"]])
                out.remove(outer)
                break
        return out

    def blocks(self, shapes: list[dict]) -> None:
        """Beamer blocks: a title bar panel directly above a body panel of the same width get a
        common `block` number; the body gets the title bar's box (`title_bar`), and emit lays
        it under the whole block so that no gap can open between the two when the block is
        resized. Theme pictures that belong to the block are listed for the render stage to
        paint out of the background: the gradient strip between title bar and body (`strips`)
        and the soft shadow pieces right of and below the block (`shadow`, which emit turns
        into a native drop shadow)."""
        k = 0
        for head in sorted(shapes, key=lambda s: s["bbox"][1]):
            for body in shapes:
                if body is head or "block" in body or "block" in head or "opacity" in body or "opacity" in head \
                        or "rule" in (body.get("role"), head.get("role")):
                    continue
                hx0, hy0, hx1, hy1 = head["bbox"]
                bx0, by0, bx1, by1 = body["bbox"]
                # Title page boxes overlap the two parts by a few points.
                if abs(hx0 - bx0) <= 1.5 and abs(hx1 - bx1) <= 1.5 and by0 - hy1 <= 3.5 and by0 >= max(hy0 + 1, hy1 - 4):
                    head["block"] = body["block"] = k
                    body["title_bar"] = head["bbox"]
                    body["strips"] = [im["bbox"] for im in self.page["images"]
                                      if im["bbox"][3] - im["bbox"][1] <= 6 and hy1 - 3 <= (im["bbox"][1] + im["bbox"][3]) / 2 <= by0 + 3
                                      and im["bbox"][2] - im["bbox"][0] >= 0.9 * (bx1 - bx0)
                                      and im["bbox"][0] >= bx0 - 2 and im["bbox"][2] <= bx1 + 2]
                    k += 1
                    break
        for el in shapes:
            if el.get("block") is not None and "title_bar" not in el:
                continue  # title bars: the body carries the block's shadow
            x0, y0, x1, y1 = el["bbox"]
            if el.get("title_bar"):
                y0 = el["title_bar"][1]
            outer = Rect(x0 - 1.5, y0 - 1.5, x1 + 8, y1 + 8)
            inner = Rect(x0 + 1, y0 + 1, x1 - 1, y1 - 1)
            pieces = [Rect.of(im["bbox"]) for im in self.page["images"]
                      if outer.contains_rect(Rect.of(im["bbox"]), tol=0) and not inner.contains_rect(Rect.of(im["bbox"]), tol=0)
                      and im["bbox"] not in el.get("strips", [])]
            right = [r.x1 - x1 for r in pieces if r.x1 > x1 + 2]
            below = [r.y1 - y1 for r in pieces if r.y1 > y1 + 2]
            under = Rect(x0 + 2, y1 + 0.5, x1 + 2, y1 + 3)  # a shadow never falls onto another panel
            if right and below and all(r.x0 >= x0 - 1.5 and r.y0 >= y0 - 1.5 for r in pieces) \
                    and not any(o is not el and Rect.of(o["bbox"]).intersects(under)
                                and not outer.contains_rect(Rect.of(o["bbox"]))  # the shadow's own black geometry
                                for o in shapes):
                # Image boxes are rounded outwards to whole points: 4.07 ... 4.51 for a 4 pt shadow.
                el["shadow"] = {"size": float(max(1, math.floor(min(max(right), max(below)) + 0.25))),
                                "pieces": [r.as_list() for r in pieces]}

    def plain_tables(self, lines: list[Line]) -> list[dict]:
        """Tabulars without rules, used to align short texts in columns: three or more rows at a
        regular pitch whose cells, separated by wide gaps, keep to the same columns. They
        become borderless native tables; their lines are taken out of the text flow."""
        # The next lines of a list item start where its text starts, a line below: short words
        # side by side in four item columns looked like rows of cells, and were cut out of their
        # items into a table.
        run_on: set[int] = set()
        for b in (l for l in lines if l.bullet and l.content):
            prev = b
            for l in sorted((l for l in lines if l.baseline > b.baseline and not l.bullet and l.content), key=lambda l: l.baseline):
                if l.baseline - prev.baseline > 1.6 * prev.size:
                    break
                if abs(l.x0 - b.x0) <= 1:
                    run_on.add(id(l))
                    prev = l
        # (not code: a listing keeps its columns as spaces, and its line numbers are a column of
        # their own - split_line_numbers - not the first column of a table)
        candidates = [l for l in lines if l.reason is None and not l.bullet and l.tab is None
                      and l.rect.y0 > 0.15 * self.H and all(s.horizontal for s in l.spans)
                      and not l.code_number and not is_mono(l.content)]
        rows: list[list[Line]] = []
        for l in sorted(candidates, key=lambda l: l.baseline):
            if rows and abs(rows[-1][0].baseline - l.baseline) <= 0.3 * l.size:
                rows[-1].append(l)
            else:
                rows.append([l])

        def cells(row: list[Line]) -> list[list[Span]]:
            spans = sorted((s for l in row for s in l.spans), key=lambda s: s.rect.x0)
            out: list[list[Span]] = []
            for s in spans:
                if out and s.rect.x0 - out[-1][-1].rect.x1 < 0.9 * s.size:
                    out[-1].append(s)
                else:
                    out.append([s])
            return out

        def extent(chunk):
            return chunk[0].rect.x0, chunk[-1].rect.x1

        # (a tabular set inside an item has cells that start elsewhere too: its rows stay)
        rows = [row for row in rows if not all(id(l) in run_on and len(cells([l])) == 1 for l in row)]
        split = [(row, cells(row)) for row in rows]
        tables, i = [], 0
        while i < len(split):
            j = i
            k = len(split[i][1])
            size = split[i][0][0].size
            ok = lambda r: len(r[1]) == k >= 2 and all(len("".join(s.text for s in c).strip()) <= 30 for c in r[1]) \
                and abs(r[0][0].size - size) <= 0.5
            while j + 1 < len(split) and ok(split[i]) and ok(split[j + 1]) and \
                    split[j + 1][0][0].baseline - split[j][0][0].baseline <= 2.0 * size:
                j += 1
            group = split[i:j + 1]
            i = j + 1
            if len(group) < 3:
                continue
            pitches = [b[0][0].baseline - a[0][0].baseline for a, b in zip(group, group[1:])]
            if max(pitches) > 1.25 * min(pitches):
                continue
            columns = [[min(extent(r[1][c])[0] for r in group), max(extent(r[1][c])[1] for r in group)] for c in range(k)]
            if any(a[1] + 0.5 * size > b[0] for a, b in zip(columns, columns[1:])):
                continue  # cells of neighbouring columns overlap: not a grid

            def wraps(upper: list[Span], lower: list[Span]) -> bool:
                """A sentence running on from one row to the next in the same column."""
                a, b = " ".join(s.text for s in upper).strip(), " ".join(s.text for s in lower).strip()
                return len(a.split()) >= 3 and b[:1].islower() and (a[-1:].isalnum() or a[-1:] == ",")
            if sum(any(wraps(a[1][c], b[1][c]) for a, b in zip(group, group[1:])) for c in range(k)) >= min(2, k):
                continue  # columns of wrapped prose side by side, not a table
            col_info = []
            for c, (x0, x1) in enumerate(columns):
                chunks = [r[1][c] for r in group]
                left = all(abs(extent(ch)[0] - x0) <= 1 for ch in chunks)
                right = all(abs(extent(ch)[1] - x1) <= 1 for ch in chunks)
                digits = sum(ch_.isdigit() for ch in chunks for s in ch for ch_ in s.text)
                letters = sum(ch_.isalpha() for ch in chunks for s in ch for ch_ in s.text)
                align = "right" if right and (not left or digits > letters) else "left" if left else "center"
                col_info.append({"x0": round(x0, 2), "x1": round(x1, 2), "align": align})
            pad = 0.55 * size  # \tabcolsep
            bounds = [columns[0][0] - pad] + [(a[1] + b[0]) / 2 for a, b in zip(columns, columns[1:])] + [columns[-1][1] + pad]
            spans = [s for r in group for ch in r[1] for s in ch]
            rect = union_all(s.rect for s in spans)
            baselines = [r[0][0].baseline for r in group]
            for r in group:
                for l in r[0]:
                    l.reason = "table"
            tables.append({
                "id": f"p{self.page['index']}pt{len(tables)}", "kind": "table", "role": "table",
                "bbox": rect.expand(1.0).as_list(), "frame": [bounds[0], rect.y0, bounds[-1], rect.y1],
                "size": round(size, 2), "row_baselines": [round(b, 2) for b in baselines],
                "row_heights": [round(p, 2) for p in pitches + [pitches[-1]]], "columns": col_info,
                "bounds": [round(b, 2) for b in bounds],
                "cells": [[span_runs(ch) for ch in r[1]] for r in group],
                "merges": [], "rules": [], "borders": [], "spans": [s.id for s in spans],
            })
        return tables

    def rotated_texts(self, lines: list[Line], used: set[str]) -> list[dict]:
        """Text turned by 90° (\\rotatebox{90}, a label beside a table) that no figure took:
        native text boxes turned the same way (`rotation`: -90 reads upwards, 90 downwards).
        Their paragraphs are laid out in the text's own frame (x along the reading direction,
        y across it), which emit turns back onto the page."""
        spans = sorted((s for l in lines if l.reason == "rotated" for s in l.spans
                        if s.id not in used and s.text.strip() and abs(self.page_dir(s)[0]) < 0.01),
                       key=lambda s: (self.page_dir(s)[1], round(s.rect.cx), -s.baseline * self.page_dir(s)[1]))
        rows: list[list[Span]] = []
        for s in spans:
            last = rows[-1][-1] if rows else None
            if last and self.page_dir(last) == self.page_dir(s) and abs(last.rect.cx - s.rect.cx) <= 0.3 * s.size and \
                    abs(last.size - s.size) <= 0.5 and min(abs(last.rect.y0 - s.rect.y1), abs(s.rect.y0 - last.rect.y1)) <= 2 * s.size:
                rows[-1].append(s)
            else:
                rows.append([s])
        out = []
        for row in rows:
            up = self.page_dir(row[0])[1] < 0
            # page -> text frame: reading upwards, x = -y and y = x; downwards, x = y and y = -x
            turned = [replace(s, rect=Rect(-s.rect.y1, s.rect.x0, -s.rect.y0, s.rect.x1) if up else
                              Rect(s.rect.y0, -s.rect.x1, s.rect.y1, -s.rect.x0),
                              baseline=self.page_origin(s)[0] * (1 if up else -1), horizontal=True) for s in row]
            el = self.text_element([Paragraph([Line(turned)])], f"p{self.page['index']}rt{len(out)}")
            out.append({**el, "bbox": union_all(s.rect for s in row).as_list(), "panel": self.panel_of(union_all(s.rect for s in row)),
                        "rotation": -90 if up else 90})
        return out

    def page_dir(self, s: Span) -> tuple[float, float]:
        return tuple(self._raw_spans[s.id]["dir"])

    def page_origin(self, s: Span) -> tuple[float, float]:
        return tuple(self._raw_spans[s.id]["origin"])

    def text_element(self, box: list[Paragraph], element_id: str) -> dict:
        rect = union_all([p.rect for p in box] + [Rect.of(p.bullet["bbox"]) for p in box if p.bullet])
        code = all(is_code(p.spans) and not p.bullet for p in box)
        pitch =code_pitch([s for p in box for s in p.spans], rect.x0) if code else None

        def unbalanced(p: Paragraph) -> bool:
            """Centred (or right-aligned) lines broken where a greedy wrap would not break, or
            nearly would (TeX balances them, or they were broken by hand): no box width
            reproduces the breaks reliably, so they become soft breaks. So do lines broken
            where the room they are centred in would have taken the next word: a title page's
            \\institute or \\date broken with \\\\ (Slides re-wrapped them mid-affiliation)."""
            if p.align == "left" or len(p.lines) < 2 or not all(l.content for l in p.lines):
                return False
            widest = max(l.x1 - l.x0 for l in p.lines)
            room = max(widest + 0.5 * p.size, self.free_width(p) - 0.5 * p.size)
            return any(a.x1 - a.x0 + 0.2 * p.size + first_word_width(b.content[0]) <= room
                       for a, b in zip(p.lines, p.lines[1:]))
        runs = [self.runs(p, code_indent(p, rect.x0, pitch) if code else "", soft_breaks=unbalanced(p),
                          pitch=pitch, x_ref=rect.x0) for p in box]
        # (said only where it is known, on a left-aligned wrapped paragraph: line_starts)
        starts = [line_starts(p.lines, r) if p.align == "left" and not code and not p.direction else None
                  for p, r in zip(box, runs)]
        return {
            "id": element_id, "kind": "text", "role": box[0].role, "bbox": rect.as_list(),
            "panel": self.panel_of(rect),
            "paragraphs": [{
                "align": p.align, "level": p.level, "bullet": p.bullet, "size": round(p.size, 2),
                # Said only where it is true, as `deck_ir` says it of a deck that is read
                # back: a left-to-right paragraph is every deck this project had until now.
                **({"direction": p.direction} if p.direction else {}),
                # (as `deck_ir` reads Slides' JUSTIFIED: a left paragraph, justified)
                **({"justified": True} if p.justified else {}),
                # (a first line set in by \parindent starts at lines[0].x0, emit indents it)
                "text_x0": round(p.x0 - p.indent, 2),
                "tab_x0": round(p.first.tab.rect.x0, 2) if p.first.tab else None,
                "lines": [{"baseline": round(l.baseline, 2), "x0": round(l.x0, 2), "x1": round(l.x1, 2)}
                          for l in p.lines],
                # Right edge a wrapped line could grow to before TeX would have pulled up the
                # next line's first word: a text box narrower than this wraps the same way.
                # (as a width from the paragraph's left edge, so centred lines count too)
                "wrap_limit": round(min(l.x0 for l in p.lines) + min(a.x1 - a.x0 + 0.25 * p.size + line_word_width(b)
                                                                     for a, b in zip(p.lines, p.lines[1:])), 2)
                              if len(p.lines) > 1 and all(l.content for l in p.lines) else None,
                **({"line_starts": s} if s else {}),
                "runs": r,
            } for p, r, s in zip(box, runs, starts)],
            "code": code,
            "spans": [s.id for p in box for s in p.spans if s.info.family != "icon" and not s.drawn
                      and not any(s in h for l in p.lines for h in l.holes)],
            # Fraction bars now written as text, underlines and highlight boxes now text
            # styles: they leave the background with the glyphs.
            "strokes": [f[0].as_list() for p in box for l in p.lines for f in l.fractions] +
                       list({tuple(r.as_list()): r.as_list() for p in box for s in p.spans
                             for r in self.decor_rects.get(s.id, [])}.values()),
        }

    def classify(self) -> dict:
        spans = self.spans()
        self.text_decorations(spans)
        self.underscores(spans)
        self.spans_by_id = {s.id: s for s in spans}
        self.analyse_graphics()
        # (after the braces: a brace's CMEX pieces go with their label, see join_braces)
        lines = self.split_line_numbers(self.join_hanging_operators(self.join_braces(self.build_lines(spans))))
        self.read_lines(lines, spans)
        self.assign_reasons(lines)
        plain_tables = self.plain_tables(lines)
        body_lines = [l for l in lines if l.reason is None and abs(l.size - self.body) < 1]
        self.text_margin = min((l.rect.x0 for l in body_lines), default=0.08 * self.W)
        if self.text_margin > 0.2 * self.W:
            # Nothing at the body size starts on the left (a frame of smaller items, and the
            # slide number is what is left): the left edge of what text there is.
            self.text_margin = min([l.rect.x0 for l in lines if l.reason is None] + [0.2 * self.W])
        paragraphs = self.display_pieces_apart(self.build_paragraphs(lines))
        for par in paragraphs:
            if par.first.code_number:
                par.align = "right"  # listings sets its numbers flush right against the code
        boxes = self.build_boxes(paragraphs)

        n = self.page["index"]
        elements = [self.text_element(box, f"p{n}t{bi}") for bi, box in enumerate(boxes)]
        self.line_owner = {id(l): (f"p{n}t{bi}", p.align) for bi, box in enumerate(boxes) for p in box for l in p.lines}
        holes = [(f"p{n}t{bi}", l, h) for bi, box in enumerate(boxes) for p in box for l in p.lines for h in l.holes]
        hole_pictures = []
        for anchor, line, h in holes:
            rect = line.hole_rect(h)
            # Radical signs and big-operator parts sit off the baseline, in lines of their own.
            h = h + [s for l in lines if l.reason == "math" for s in l.spans if s.rect.intersects(rect.expand(1))]
            h = h + [s for lim in line.limits for s in lim.spans if s not in h
                     and rect.x0 - line.size <= lim.rect.cx <= rect.x1 + line.size]  # a big operator's limits
            rect = union_all([rect] + [s.rect for s in h] + [b for b in self.bars if b.expand(1).intersects(rect)])
            hole_pictures.append({"id": f"p{n}h{len(hole_pictures)}", "kind": "image", "role": "math",
                                  "bbox": rect.expand(HOLE_PAD).as_list(), "spans": [s.id for s in h],
                                  "anchor": anchor})  # grouped with this text element
        self.icon_bullets = [Rect.of(p["bullet"]["bbox"]) for e in elements for p in e["paragraphs"]
                             if p["bullet"] and p["bullet"]["kind"] == "icon"]
        for e in elements:  # icon bullets: pictures grouped with their item
            for p in e["paragraphs"]:
                if p["bullet"] and p["bullet"]["kind"] == "icon":
                    hole_pictures.append({"id": f"p{n}u{len(hole_pictures)}", "kind": "image", "role": "icon",
                                          "bbox": Rect.of(p["bullet"]["bbox"]).expand(0.5).as_list(),
                                          "spans": p["bullet"]["spans"], "anchor": e["id"]})
                    p["bullet"] = None

        text_spans = {sid for e in elements for sid in e["spans"]}
        self.hole_boxes = [Rect.of(h["bbox"]) for h in hole_pictures]
        elements = self.figures(lines, elements) + self.icons(elements) + hole_pictures + plain_tables + elements  # pictures below text
        elements += self.rotated_texts(lines, {sid for e in elements for sid in e["spans"]})
        text_spans |= {sid for e in elements if e["kind"] == "table" for sid in e["spans"]}
        elements = self.math_pictures(lines, paragraphs, elements) + elements
        self.trim_overlays(elements)
        elements = [e for e in elements if not e.get("overlay") or e["drawings"]]
        text_spans |={sid for e in elements if e["kind"] == "text" for sid in e["spans"]}  # equation numbers
        elements = self.specks_on_panels(spans, elements) + elements
        shapes = self.shapes(lines, elements)
        self.blocks(shapes)
        elements = shapes + elements   # shapes below pictures

        used = {sid for e in elements for sid in e["spans"]}
        paragraph_reason = {id(l): p.reason for p in paragraphs for l in p.lines}
        by_reason: dict[str, list[Span]] = {}
        for line in lines:
            for s in line.spans:
                if s.id not in used:
                    reason = line.reason or paragraph_reason.get(id(line)) or "unsure"
                    by_reason.setdefault(reason, []).append(s)
        left = [{"reason": r, "spans": [s.id for s in ss], "bboxes": [s.rect.as_list() for s in ss]}
                for r, ss in by_reason.items()]

        # Theme text as ready-made text elements: text that is the same on every slide moves to
        # the slide layout (see promote_theme_text), the rest stays in the background.
        theme_texts = []
        for line in lines:
            if line.reason == "theme" and all(s.id not in used for s in line.spans):
                par = Paragraph([line], align="left")
                theme_texts.append({
                    "kind": "text", "role": "layout", "bbox": line.rect.as_list(), "panel": None, "code": False,
                    # Colour is part of the identity: section navigation highlights the current
                    # section by colour, which must not be frozen onto the layout.
                    "key": [line.text, round(line.rect.x0), round(line.baseline), "".join(s.color for s in line.spans)],
                    "chars": sum(len(s.text.strip()) for s in line.spans),
                    "paragraphs": [{"align": "left", "level": 0, "bullet": None, "size": round(line.size, 2),
                                    "text_x0": round(line.x0, 2),
                                    "lines": [{"baseline": round(line.baseline, 2), "x0": round(line.x0, 2),
                                               "x1": round(line.x1, 2)}],
                                    "runs": self.runs(par),
                                    **({"direction": par.direction} if par.direction else {})}],
                    "spans": [s.id for s in line.spans],
                })

        # Pictures describe themselves with the text they show (alt text in Slides).
        by_id = {s.id: s for s in spans}
        for e in elements:
            if e["kind"] == "image" and e["spans"]:
                # formulas left to right (scripts follow their base); figure labels by rows
                order = (lambda s: s.rect.x0) if e["role"] == "math" else (lambda s: (round(s.baseline / 4), s.rect.x0))
                shown = sorted((by_id[i] for i in e["spans"] if i in by_id), key=order)
                words = [math_text(s.font, s.text)[0] if s.info.family == "math" else s.text for s in shown]
                e["alt"] = " ".join(w.strip() for w in words if w.strip() and "�" not in w)[:500]

        chars_total = sum(len(s["text"].strip()) for s in self.page["spans"])
        chars_native = sum(len(s["text"].strip()) for s in self.page["spans"] if s["id"] in text_spans)
        # Span ids name page objects (render switches them off, checks look them up): a character
        # the page draws as a rule has none, and is in the text through its runs alone.
        drawn = {s.id for s in spans if s.drawn}
        if drawn:
            for holder in elements + left + theme_texts + [e["bullet"] for e in elements if isinstance(e.get("bullet"), dict)]:
                if isinstance(holder.get("spans"), list):
                    holder["spans"] = [i for i in holder["spans"] if i not in drawn]
        return {
            "page": n, "frame": self.page["label"], "label": self.page.get("frame_label"), "size": self.page["size"],
            "notes": self.page.get("notes"),
            "elements": elements, "left_in_background": left, "theme_texts": theme_texts,
            "panels": [{"bbox": p["bbox"].as_list(), "fill": p["fill"], "rounded": p["rounded"]} for p in self.panels],
            "figure_regions": [r.as_list() for r in self.regions],
            "stats": {"chars": chars_total, "chars_native": chars_native},
        }


def ball_number(bullet: dict, label: dict) -> str:
    """The number a ball shows. A ball template draws the whole label on a ball sized for a
    letter or two: \\begin{enumerate}[(a)] puts white parentheses at the ball's edges, over its
    white rim and the page, where nobody sees them (r3_dense_v2 s7). In Slides' wider font
    they came out as white crescents cutting the ball. Only parentheses at the edge are unseen:
    a label narrower than the ball keeps them on its dark face, where they show ('(i)', '(ii)'
    on a 10 pt ball, r3_dense_v3 s7, while '(iii)' reaches the rim). The label is centred on
    the ball, so its half width is the ball's centre less its left edge."""
    text = bullet["text"]
    white = all(int(label.get("color", "#000000")[i:i + 2], 16) >= 0xE0 for i in (1, 3, 5))
    if bullet["kind"] == "image" and white and len(text) > 2 and text[0] == "(" and text[-1] == ")":
        x0, _, x1, _ = bullet["bbox"]
        half = (x0 + x1) / 2 - label.get("x0", x0)
        if half >= BALL_RIM * (x1 - x0) / 2:
            return text[1:-1]
    return text


BALL_RIM = 0.95  # of a ball's radius: the bright rim of beamer's ball, where white ink is unseen


def literal_list_numbers(slides: list[dict]) -> None:
    """Slides numbers each list from 1, and the API cannot set a start number. A numbered
    item whose number Slides would get wrong (a table of contents split into one box per
    section, a list continued after a paragraph) keeps its number as literal text with a tab.
    A ball or box under the number becomes a picture grouped with the text, so it moves along,
    and the number goes on the picture (`number`) as its own centred text box: on the item's
    line it would sit on the text baseline, off the middle of the ball."""
    def numbered(p: dict) -> bool:
        b = p["bullet"]
        return bool(b) and (b["kind"] == "number" or (b["kind"] == "image" and bool(b["text"])))

    def on_graphic(p: dict) -> bool:
        return numbered(p) and (p["bullet"]["kind"] == "image" or bool(p["bullet"].get("patch")))

    def kind(p: dict) -> str | None:
        """What emit bullets a paragraph with: one createParagraphBullets per run of paragraphs
        of one preset (emit.bullet_preset), each run a list of its own that Slides numbers
        from 1. Numbers with a parenthesis have a preset of their own; every glyph another."""
        b = p["bullet"]
        if not b:
            return None
        return ("number)" if ")" in b.get("text", "") else "number") if numbered(p) else "glyph"

    def misnumbered(e: dict) -> bool:
        expected: dict[int, int] = {}
        previous = None
        for p in e["paragraphs"]:
            if kind(p) != previous:
                # The next list in Slides starts again at 1: after a paragraph, and after the
                # glyph items nested in a numbered one ('1. a . b 2.' came out 1, 1, 2, r1_ml_v1 s3).
                expected.clear()
                previous = kind(p)
            if not numbered(p):
                continue
            for deeper in [k for k in expected if k > p["level"]]:
                del expected[deeper]
            label = p["bullet"]["text"]
            digits = re.sub(r"\D", "", label)
            # The preset numbers level 0 with digits, level 1 with letters, level 2 in roman.
            if (p["level"] == 0) != bool(digits):
                return True
            want = expected.get(p["level"], 1)
            if digits and int(digits) != want:
                return True
            expected[p["level"]] = want + 1
        return False

    for slide in slides:
        texts = [e for e in slide["elements"] if e["kind"] == "text"]
        # Slides has no numbers on balls or boxes: those are always drawn.
        if not any(misnumbered(e) or any(on_graphic(p) for p in e["paragraphs"]) for e in texts):
            continue
        # All numbers on the slide the same way, so the items still look alike.
        pictures = []
        for e, p in ((e, p) for e in texts for p in e["paragraphs"] if numbered(p)):
            b, label = p["bullet"], p["bullet"].get("label")
            if not label or not p["runs"]:
                continue
            if on_graphic(p):
                x0, y0, x1, y1 = b["bbox"]
                pictures.append({"id": f"{e['id']}b{len(pictures)}", "kind": "image", "role": "icon",
                                 "bbox": [x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5], "spans": [], "anchor": e["id"],
                                 "number": {"text": ball_number(b, label), "center": [(x0 + x1) / 2, (y0 + y1) / 2],
                                            "height": y1 - y0, **label}})
                p["bullet"] = None
                continue
            p["runs"].insert(0, {
                "text": b["text"] + "\t", "font": label["font"], "family": label["family"], "size": label["size"],
                "bold": label["bold"], "italic": label["italic"], "smallcaps": False, "color": label["color"],
                "link": None, "script": None, "underline": False, "highlight": None})
            p["tab_x0"], p["text_x0"], p["bullet"] = p["text_x0"], label["x0"], None
        if pictures:  # below the text
            first_text = next(i for i, e in enumerate(slide["elements"]) if e["kind"] == "text")
            slide["elements"][first_text:first_text] = pictures


def mark_title_page(slides: list[dict], doc_title: str) -> None:
    """On the title page, the box showing the document title (from the PDF metadata, which
    beamer fills as "Title - Subtitle") becomes the slide's title."""
    norm = lambda s: " ".join(s.casefold().split())
    wanted = norm(doc_title)
    if len(wanted) < 3:
        return
    for slide in slides[:2]:
        texts = [e for e in slide["elements"] if e["kind"] == "text"]
        if any(e["role"] == "title" for e in texts):
            continue
        for e in texts:
            first = norm("".join(r["text"] for r in e["paragraphs"][0]["runs"]))
            if len(first) >= 3 and wanted.startswith(first):
                e["role"] = "title"
                slide["title_page"] = True
                return


def promote_theme_text(slides: list[dict]) -> list[dict]:
    """Theme text (header/footer lines) identical in content and position on every slide
    - author, short title, institute, date - becomes text on the slide layouts, edited once
    for the whole deck. Slide numbers and section navigation differ per slide and stay put."""
    if len(slides) < 2:
        for s in slides:
            s["on_layout"] = []
        return []
    keys = [{tuple(t["key"]): t for t in s["theme_texts"]} for s in slides]
    common = set(keys[0]).intersection(*keys[1:])
    for slide, by_key in zip(slides, keys):
        moved = {sid for k in common for sid in by_key[k]["spans"]}
        slide["on_layout"] = sorted(moved)
        # Frame counters ("3 / 9") differ per slide: a small text box on the slide. The rest
        # of the theme then often renders identically on every slide (one shared background).
        counters = [t for k, t in by_key.items() if k not in common and FRAME_COUNTER_RE.match(t["key"][0])]
        for j, t in enumerate(counters):
            slide["elements"].append({**{k: v for k, v in t.items() if k not in ("key", "chars")},
                                      "id": f"p{slide['page']}n{j}", "role": "footer", "strokes": []})
            moved |= set(t["spans"])
        for left in slide["left_in_background"]:
            keep = [i for i, sid in enumerate(left["spans"]) if sid not in moved]
            left["spans"] = [left["spans"][i] for i in keep]
            left["bboxes"] = [left["bboxes"][i] for i in keep]
        slide["left_in_background"] = [l for l in slide["left_in_background"] if l["spans"]]
        slide["stats"]["chars_native"] += sum(by_key[k]["chars"] for k in common) + sum(t["chars"] for t in counters)
    return [keys[0][k] for k in sorted(common, key=lambda k: (k[2], k[1]))]


def mark_big_headings(slides: list[dict], body: float) -> None:
    """Slides without a frame title (section pages, "Thank you!") use their single, clearly
    largest heading as the title, so it shows up in Slides' outline and navigation."""
    for slide in slides:
        texts = [e for e in slide["elements"] if e["kind"] == "text"]
        if not texts or any(e["role"] == "title" for e in texts):
            continue
        sizes = sorted((max(p["size"] for p in e["paragraphs"]), i) for i, e in enumerate(texts))
        size, i = sizes[-1]
        runner_up = sizes[-2][0] if len(sizes) > 1 else 0.0
        # (a quotation set large on a frame of its own is no heading)
        words = "".join(r["text"] for r in texts[i]["paragraphs"][0]["runs"]).strip()
        quote = words[:1] in "“„«‘\"" and len(words.split()) >= 4
        if size >= 1.3 * body and size >= 1.15 * runner_up and texts[i]["paragraphs"][0]["size"] == size and not quote:
            texts[i]["role"] = "title"


def classify_page(page: dict, body: float) -> dict:
    """One page's slide; a page the classifier trips over stays a picture as a whole."""
    try:
        return PageClassifier(page, body).classify()
    except Exception as e:  # never lose a whole deck to one odd page
        print(f"warning: page {page['index'] + 1}: classification failed ({type(e).__name__}: {e}); "
              f"kept as a picture")
        spans = page["spans"]
        return {
            "page": page["index"], "frame": page["label"], "label": page.get("frame_label"), "size": page["size"],
            "notes": page.get("notes"),
            "elements": [], "theme_texts": [], "panels": [], "figure_regions": [],
            "left_in_background": [{"reason": "error", "spans": [s["id"] for s in spans],
                                    "bboxes": [s["bbox"] for s in spans]}] if spans else [],
            "stats": {"chars": sum(len(s["text"].strip()) for s in spans), "chars_native": 0},
        }


def classify(raw: dict) -> dict:
    body = body_size(raw)
    slides = [classify_page(page, body) for page in raw["pages"]]
    literal_list_numbers(slides)
    mark_title_page(slides, raw["source"].get("title", ""))
    mark_big_headings(slides, body)
    layout_texts = promote_theme_text(slides)
    chars = sum(s["stats"]["chars"] for s in slides)
    native = sum(s["stats"]["chars_native"] for s in slides)
    return {
        "version": 1, "source": raw["source"], "body_size": body,
        "stats": {"chars": chars, "chars_native": native, "native_share": round(native / chars, 3) if chars else 0},
        "layout_texts": layout_texts,
        "slides": slides,
    }
