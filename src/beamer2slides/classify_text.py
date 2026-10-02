"""What classify reads in text: scripts, accents and math letters, runs, word widths, code columns."""

import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Iterable
from typing import Literal, Protocol, TypedDict

from . import bidi
from .classify_model import ACCENTS, OUTLINE_MIN, Line, Paragraph, Rect, Span
from .fonts import MATH_ITALIC_RE, font_info
from .ir import BulletShape, CardBox, Family, Label, Script
from .ir import Paragraph as ParagraphJson
from .ir import Run
from .raw_types import RawDoc, RawDrawing, RawSpan


# Operator names set upright inside formulas (\min, \lim, \log, \operatorname{Var}): words of a
# formula, not of prose around it.
OPERATOR_NAMES = {
    "min", "max", "sup", "inf", "lim", "liminf", "limsup", "log", "ln", "lg", "exp", "sin", "cos", "tan",
    "cot", "sec", "csc", "sinh", "cosh", "tanh", "coth", "arcsin", "arccos", "arctan", "det", "dim", "ker",
    "deg", "arg", "gcd", "lcm", "Pr", "hom", "tr", "Tr", "rank", "diag", "sgn", "sign", "span", "ess", "argmin",
    "argmax", "mod", "Var", "Cov", "Corr", "softmax", "erf", "Re", "Im", "id", "supp", "vol", "dist", "div",
    "grad", "curl", "rot", "Hom", "End", "Aut", "Ker", "KL",
}
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


def type3_text_page(spans: list[RawSpan]) -> bool:
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


# Slides draws a script run at 2/3 of its size (docs/calibration.md: 0.665): a script smaller
# than 2/3 of its line (a script's script, 0.55) is given the size that draws it as small.
SLIDES_SCRIPT = 0.665


class Row(Protocol):
    """What a script is measured against: its line, or in a cell or label the largest span,
    which stands for the line (`span_runs`)."""

    @property
    def size(self) -> float: ...

    @property
    def baseline(self) -> float: ...


def script_of(span: Span, line: Row) -> Script | None:
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


def script_size(span: Span, line: Row) -> float:
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


def compose_accents(text: str, mono: bool) -> str:
    """Spacing accents built with their letter (OT1: accent glyph, then the letter under it;
    a cedilla after a tall letter) as the accented letter: "Schr¨odinger" -> "Schrödinger",
    "Garc´ıa" -> "García", "S¸." -> "Ş.". In a monospaced face (`mono`) ` is a backquote, not
    an accent."""
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


# Long arrows (\longrightarrow, \implies, \iff, mhchem's and \xrightarrow's stretched arrows). No
# Slides face has them at TeX's length: its fallback draws ⟶ about 0.75 em long where TeX's is
# 1.64 em and mhchem's 2-3.3 em, so as one glyph the arrow came out short and tight, and an
# \xrightarrow's labels printed over the formula (r1_sci_v3 s3, r1_math_v3 s6).
LONG_ARROWS = set("⟶⟵⟷⟹⟸⟺⟼")


def long_arrow_groups(spans: list["Span"]) -> list[list["Span"]]:
    """The spans of each long arrow among `spans`: a span whose pieces compose one (CMSY's
    "−−→", "⇐⇒") or that is one (unicode-math's ⟶), and pieces in two fonts overlapped into one
    (CMR's = kerned under CMSY's ⇒: \\implies)."""
    spans = sorted((s for s in spans if s.text.strip()), key=lambda s: s.rect.x0)
    groups = [[s] for s in spans if LONG_ARROWS & set(compose_symbols(s.text))]
    for a, b in zip(spans, spans[1:]):
        if b.rect.x0 < a.rect.x1 - 0.2 and a.rect.x0 < b.rect.x1 and \
                LONG_ARROWS & set(compose_symbols(a.text.strip()[-1:] + b.text.strip()[:1])):
            groups.append([a, b])
    merged: list[list[Span]] = []
    for g in groups:
        into = next((m for m in merged if any(s in m for s in g)), None)
        if into is None:
            merged.append(list(g))
        else:
            into += [s for s in g if s not in into]
    return merged


def unmeasured_symbols(spans: list["Span"]) -> set[str]:
    """Symbols of math spans Slides was never seen to set in line with its text faces
    (`emit.SYMBOL_ADVANCE_EM`, tools/probe_symbols.py): a fallback face draws them at its own
    size and height (\\sqcup's ⊔ half as tall and raised, r1_math_v3 s6). A raised ring or
    asterisk is not one: it is written ° or * at the line's size (300 °C, r3_scripts_ruxe s4)."""
    from .emit import SYMBOL_ADVANCE_EM  # (emit imports this module)

    out = set()
    for s in spans:
        if s.info.family != "math":
            continue
        for c in math_text(s.font, s.text)[0]:
            if not (c.isascii() or c.isalnum() or c.isspace() or c in SYMBOL_ADVANCE_EM or unicodedata.combining(c)
                    or c in "◦∘∗°"):
                out.add(c)
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


def math_family(line: "Line", par: "Paragraph | None") -> Literal["sans", "serif"]:
    """The text family math is shown in: the family most of the words around it are set in -
    on its line, else in its paragraph (None: none around it) - never a monospaced one (a
    formula after \\texttt{x} is not code), and serif when there are no words (TeX's math is
    Computer Modern's)."""
    around: list[list[Span]] = [l.content for l in par.lines] if par else []
    for spans in ([line.content], around):
        weight: Counter[str] = Counter()
        for s in (s for group in spans for s in group):
            if s.info.family in ("sans", "serif"):
                weight[s.info.family] += len(s.text.strip())
        if weight:
            return "sans" if weight.most_common(1)[0][0] == "sans" else "serif"
    return "serif"


FRACTION_SLASH = "⁄"


def reading_order(line: "Line") -> list[tuple[Span | str, Script | None]]:
    """The line's content spans in reading order - left to right, or right to left where that is
    how the line reads (`bidi.logical_spans`) - except that each simple fraction becomes
    numerator (superscript), fraction slash, denominator (subscript). The slash is the only
    string, and each span's script the one its fraction forces on it."""
    owner = {id(s): f for f in line.fractions for s in f[1] + f[2]}
    out: list[tuple[Span | str, Script | None]] = []
    emitted: set[int] = set()
    for s in bidi.logical_spans(line.content):
        f = owner.get(id(s))
        if f is None:
            out.append((s, None))
        elif id(f) not in emitted:
            emitted.add(id(f))
            out.extend((x, "super") for x in f[1])
            out.append((FRACTION_SLASH, None))
            out.extend((x, "sub") for x in f[2])
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


_FAMILIES: dict[str, Family] = {"sans": "sans", "serif": "serif", "mono": "mono", "math": "math", "icon": "icon"}


def family_of(s: Span) -> Family:
    """A span's font family (`FontInfo.family`, said as a string) as deck.json says it."""
    return _FAMILIES[s.info.family]


def look(r: Run) -> tuple[object, ...]:
    """A run's style, every key but its text: two runs of one look are one run (`span_runs`,
    `PageClassifier.runs`). A decoration a run does not say is None."""
    return (r["font"], r["family"], r["size"], r["bold"], r["italic"], r["smallcaps"], r["color"], r["link"],
            r["script"], r.get("underline"), r.get("strike"), r.get("highlight"))


def span_runs(spans: list[Span]) -> list[Run]:
    """Runs for a short piece of text given as spans left to right (cells, node labels), put into
    reading order first. Simple math works as in text lines: symbols from math fonts,
    sub/superscripts."""
    runs: list[Run] = []
    if not spans:
        return runs
    main = max(spans, key=lambda s: s.size)
    base_family: Family = next((family_of(s) for s in spans if s.info.family not in ("math", "icon")), "sans")
    spans = list(bidi.logical_spans(spans))
    lead = bidi.lead_mark(spans)

    def over(a: Span, b: Span) -> bool:
        return b.rect.x0 < a.rect.x1 - 0.2 and a.rect.x0 < b.rect.x1 - 0.2
    for i in range(len(spans) - 1):  # (an accent reaching left of its letter follows it, as in Line)
        a, b = spans[i], spans[i + 1]
        if a.text.strip() in ACCENTS and b.text.strip() not in ACCENTS and \
                min(a.rect.x1, b.rect.x1) - max(a.rect.x0, b.rect.x0) > 0.5 * a.rect.w:
            spans[i], spans[i + 1] = b, a
    accent = ""
    for i, s in enumerate(spans):
        text = s.text
        if accent:  # (carried from the span before: see below)
            # (`pad`: this was `lead` too, the direction mark below, which then crashed or was lost)
            pad = len(text) - len(text.lstrip())
            if text[pad:pad + 1].isalpha():
                text = text[:pad] + with_accent(text[pad], accent) + text[pad + 1:]
            accent = ""
        body = text.rstrip()
        if len(body) >= 2 and body[-1] in ACCENTS and i + 1 < len(spans) and over(s, spans[i + 1]) and \
                spans[i + 1].text.strip()[:1].isalpha():
            # "(ˆ" then "β": the accent read with the text before its letter (r1_econ_v3 s7)
            text, accent = body[:-1] + text[len(body):], ACCENTS[body[-1]]
        if i and runs and text.strip() in ACCENTS and over(spans[i - 1], s) and runs[-1]["text"].strip():
            # An accent over the letter before it (a table header's \hat\beta, \bar y: the accent
            # a span of the text face of its own): the accented letter, not the letter and a
            # spacing accent beside it ("βˆ", "y¯", r1_econ_v4 s2).
            tail = runs[-1]["text"].rstrip()
            runs[-1]["text"] = tail[:-1] + with_accent(tail[-1], ACCENTS[text.strip()]) + runs[-1]["text"][len(tail):]
            continue
        if i and gap_between(spans[i - 1], s) > 0.15 * s.size and not text.startswith(" "):
            if runs and not runs[-1]["script"] and s.info.family == "mono" and spans[i - 1].info.family != "mono" \
                    and runs[-1]["family"] != "mono" and not runs[-1]["text"].endswith(" "):
                runs[-1]["text"] += " "  # the word space before \texttt is the prose's, not a monospaced one
            else:
                text = " " + text
        family, italic = family_of(s), s.info.italic
        pieces = [(text, italic)]
        if family == "math":
            family = base_family
            pieces = math_pieces(s.font, text)
        script = script_of(s, main)  # (the largest span stands for the line)
        size = script_size(s, main) if script else s.size
        if script == "super" and text.strip() in RAISED_MARKS:
            pieces, script, size = [(raised_mark(text), False)], None, main.size
        for text, italic in pieces:
            run: Run = {"text": text, "font": s.font, "family": family, "size": round(size, 2),
                        "bold": s.info.bold, "italic": italic, "smallcaps": s.info.smallcaps, "color": s.color,
                        "link": s.link, "script": script, "underline": s.underline, "strike": s.strike,
                        "highlight": s.highlight}
            if runs and look(runs[-1]) == look(run):
                runs[-1]["text"] += text
            else:
                runs.append(run)
    prose_spaces(runs)
    runs = [r for r in runs if r["text"]]
    if lead and runs:  # (a right-to-left cell starting with a Latin word says which way it reads)
        runs[0]["text"] = lead + runs[0]["text"]
    return runs


# An inline formula no wider than this share of its paragraph's widest line is set with no-break
# spaces inside it (formula_groups): TeX kept it on one line, and Slides, whose substitute runs a
# little narrower or wider, broke 'W_q = C/(cμ' from '− λ).' (r2_fonts_segoe s3). A longer one
# keeps its spaces, or Slides would have to cut it inside a word.
FORMULA_GLUE_SHARE = 0.5
NBSP = " "


def formula_groups(line: "Line", measure: float) -> dict[int, int]:
    """The line's short inline formulas, as {id(span): formula}: runs of neighbouring spans in
    reading order that are math (or hold no letter: CMR's '=', '(', digits), at least one of them
    math, less than a \\quad apart, none in a hole, the whole no wider than FORMULA_GLUE_SHARE of
    `measure`. The spaces between and inside their spans are written as no-break spaces
    (PageClassifier.runs)."""
    held = {id(s) for h in line.holes for s in h}
    groups: list[list[Span]] = []
    cur: list[Span] = []
    for s, _ in reading_order(line):
        if not isinstance(s, Span) or not s.text.strip() or s.info.family == "icon":
            continue
        formulaic = id(s) not in held and (s.info.family == "math" or not any(c.isalpha() for c in s.text))
        if formulaic and cur and s.rect.x0 - cur[-1].rect.x1 < 0.9 * line.size:
            cur.append(s)
            continue
        if cur:
            groups.append(cur)
        cur = [s] if formulaic else []
    if cur:
        groups.append(cur)
    out = {}
    for i, g in enumerate(groups):
        if any(s.info.family == "math" for s in g) and \
                max(s.rect.x1 for s in g) - min(s.rect.x0 for s in g) <= FORMULA_GLUE_SHARE * measure:
            out.update((id(s), i) for s in g)
    return out


def glued(text: str, lead: bool, trail: bool) -> str:
    """A formula's text with its spaces no-break: those between its characters, with `lead` its
    leading ones (the space between it and the formula's span or piece before) and with `trail`
    its trailing ones (a piece the formula's next piece follows)."""
    text = re.sub(r"(?<=\S) +(?=\S)", lambda m: NBSP * len(m.group()), text)
    if lead:
        body = text.lstrip(" ")
        text = NBSP * (len(text) - len(body)) + body
    if trail:
        body = text.rstrip(" ")
        text = body + NBSP * (len(text) - len(body))
    return text


def prose_spaces(runs: list[Run]) -> None:
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


def cell_runs(lines: list[list[Span]]) -> tuple[list[Run], list[int]]:
    """Runs of a table cell given as its lines of spans (a paragraph column wraps): one paragraph,
    the lines joined by a space, or by nothing where TeX hyphenated a word at the line's end.
    And where in the runs' text each line after the first starts."""
    runs: list[Run] = []
    starts: list[int] = []
    for spans in lines:
        more = span_runs(spans)
        if runs and more:
            tail, head = runs[-1]["text"], more[0]["text"].lstrip()
            if len(tail) >= 2 and tail.endswith("-") and tail[-2].isalpha() and head[:1].islower():
                runs[-1] = with_text(runs[-1], tail[:-1])
                more[0] = with_text(more[0], head)
            else:
                more[0] = with_text(more[0], " " + head)
            starts.append(sum(len(r["text"]) for r in runs) + len(more[0]["text"]) - len(head))
            if look(runs[-1]) == look(more[0]):
                runs[-1] = with_text(runs[-1], runs[-1]["text"] + more[0]["text"])
                more = more[1:]
        runs += more
    return runs, starts


def with_text(r: Run, text: str) -> Run:
    """A copy of a run with other text."""
    return {**r, "text": text}


def label_of(spans: list[Span]) -> Label | None:
    """Where and how a list number is drawn, to write it as literal text if Slides can't number it."""
    if not spans:
        return None
    s = min(spans, key=lambda s: s.rect.x0)
    return {"x0": round(s.rect.x0, 2), "baseline": round(s.baseline, 2), "font": s.font, "family": family_of(s),
            "size": round(s.size, 2), "bold": s.info.bold, "italic": s.info.italic, "color": s.color}


class BulletLook(TypedDict, total=False):
    """`bullet_shape`'s answer: both keys, or none for a mark with no Slides glyph. A shape
    bullet takes them as they are (`**look`)."""
    shape: BulletShape
    color: str


def bullet_shape(d: RawDrawing | None) -> BulletLook:
    """Shape and colour of a bullet drawn as a path (emit picks the Slides glyph): a filled
    rectangle is a square, curves are a disc (a circle when only stroked), three corners a
    triangle. A filled mark outlined in another colour (a legend's swatch) has no Slides glyph:
    none (it becomes a picture beside its text, r3_charts_v1 s9)."""
    if not d:
        return {}
    fill = d["fill"] if d["type"] in ("f", "fs") else None
    stroke = d["stroke"]
    if fill and d["type"] == "fs" and stroke and stroke != fill and \
            (d["width"] or 0.0) >= OUTLINE_MIN and d["stroke_opacity"] > 0.5:
        return {}
    ops = set(d["items"])
    # (a drawing of more than 20 pieces has no path: it crashed here, and its page became a picture)
    points = {(round(x, 1), round(y, 1)) for _, pts in d["path"] or [] for x, y in pts}
    shape: BulletShape
    if ops <= {"r", "e", "q", "u"}:
        shape = "square" if fill else "open_square"
    elif "c" in ops:
        shape = "disc" if fill else "circle"
    elif ops == {"l"} and len(points) == 3:
        shape = "triangle"
    else:
        return {}
    return {"shape": shape, "color": fill or stroke or "#000000"}


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
_WIDTHS: dict[str, int] = dict(zip("abcdefghijklmnopqrstuvwxyz", (556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833,
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


def hyphen_cut(word: str) -> int | None:
    """Where Slides may break inside `word`: just after its first hyphen that has a letter before
    it and no digit after it (UAX #14 LB25, as `text_layout.wrap` and `emit.first_break`)."""
    for i in range(1, len(word) - 1):
        if word[i] == "-" and not word[i + 1].isdigit():
            return i + 1
    return None


def text_weight(text: str) -> int:
    """`text`'s width in `_WIDTHS` units (an ideograph 1000, an unknown letter 556)."""
    return sum(1000 if cjk(c) else _WIDTHS.get(c, 556) for c in text)


def first_word_width(span: Span, hyphens: bool) -> float:
    """Width of a span's first word: a span can hold one word or a whole line of them. In Chinese
    or Japanese a line can break after any character, so a word there is one character. With
    `hyphens`, the word ends after a hyphen inside it, where Slides may break a line
    (`hyphen_cut`)."""
    text = span.text.strip()
    if not text:
        return span.rect.w
    word = text.split()[0]
    cut = next((i for i, c in enumerate(word) if cjk(c)), None)
    if cut is not None:
        word = word[:max(cut, 1)]  # up to the first ideograph, or that one alone
    elif hyphens:
        word = word[:hyphen_cut(word)]  # (None: the whole word)
    return span.rect.w * text_weight(word) / text_weight(text)


def line_word_width(line: "Line", hyphens: bool) -> float:
    """Width of a line's first word, across the spans it is set in: a small-caps word is its
    capital in one span and its small letters in the next ('G' + 'oldbach', r2_fonts_pazo s5),
    and the capital alone looked short enough to have ended the line above - the paragraph was
    cut in two there (V-fonts-8). With `hyphens`, up to where Slides may first break it
    (`first_word_width`): a text element's `wrap_limit`, which Slides is held under."""
    spans = line.content
    head = spans[0]
    x1 = head.rect.x0 + first_word_width(head, hyphens)
    for a, b in zip(spans, spans[1:]):
        if len(a.text.split()) != 1 or a.text != a.text.rstrip() or b.text[:1].isspace() or \
                b.rect.x0 - a.rect.x1 > 0.1 * line.size or cjk(a.text[-1:]) or \
                (hyphens and hyphen_cut(a.text.strip()) is not None):
            break
        x1 = b.rect.x0 + first_word_width(b, hyphens)
    return x1 - head.rect.x0


def last_word_width(span: Span) -> float:
    """Width of a span's last word (first_word_width's other end)."""
    text = span.text.strip()
    if not text:
        return span.rect.w
    return span.rect.w * text_weight(text.split()[-1]) / text_weight(text)


def line_starts(lines: list["Line"], runs: list[Run]) -> list[int] | None:
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
    starts: list[int] = []
    at = 0
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
    def whole(l: "Line") -> bool:
        return not font_gaps(l, font) and any(
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


def card_text(node: Rect, rows: list[list[Span]]) -> list[CardBox] | None:
    """The text on a node that is more than a centred label (a card: a big number over a
    caption, a heading over wrapped body copy) as its text boxes ({"paragraphs": [...]}, each
    placed on its baselines), or None for a simple label (one size, centred on the node)."""
    if not rows:
        return None
    rows = [sorted(row, key=lambda s: s.rect.x0) for row in rows]
    # (a row's baseline is its normal-size text's, not a script's)
    info = [{"x0": row[0].rect.x0, "x1": row[-1].rect.x1, "baseline": max(row, key=lambda s: s.size).baseline,
             "size": max(s.size for s in row)} for row in rows]
    sizes = [r["size"] for r in info]
    top, bottom = min(s.rect.y0 for s in rows[0]), max(s.rect.y1 for s in rows[-1])
    # (lines of a justified paragraph start together and end apart, even when nearly centred)
    def flush_left(ls: list[dict[str, float]]) -> bool:
        return len(ls) > 1 and all(abs(l["x0"] - ls[0]["x0"]) <= 0.5 for l in ls) and \
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
    boxes: list[list[ParagraphJson]] = []
    for idx in paragraphs:
        lines = [info[i] for i in idx]
        on_centre = all(abs((l["x0"] + l["x1"]) / 2 - node.cx) <= 2 for l in lines) and not flush_left(lines)
        left = not on_centre and (len(lines) == 1 or all(abs(l["x0"] - lines[0]["x0"]) <= 1 for l in lines))
        runs: list[Run] = []
        for i in idx:
            row_runs = span_runs(rows[i])
            if runs and row_runs:
                # a centred caption keeps its breaks (see text_element.unbalanced)
                runs[-1] = with_text(runs[-1], runs[-1]["text"].rstrip() + (" " if left else chr(11)))
            runs += row_runs
        par: ParagraphJson = {
            "align": "left" if left else "center", "level": 0, "bullet": None, "size": round(lines[0]["size"], 2),
            "text_x0": round(min(l["x0"] for l in lines), 2), "tab_x0": None,
            "lines": [{"baseline": round(l["baseline"], 2), "x0": round(l["x0"], 2), "x1": round(l["x1"], 2)} for l in lines],
            "wrap_limit": round(min(l["x0"] for l in lines) + min(
                a["x1"] - a["x0"] + 0.25 * a["size"] + first_word_width(rows[i + 1][0], True)
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
    def letters(ss: Iterable[Span]) -> int:
        return sum(len(s.text.strip()) for s in ss)
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


def code_indent(par: Paragraph, box_x0: float, pitch: float | None) -> str:
    """Leading spaces that reproduce a code line's indentation: the block's column `pitch`
    (`code_pitch`) per space, else (None) the line's monospace advance per char."""
    return " " * max(0, round((par.x0 - box_x0) / (pitch or mono_advance(par.first.content))))


def body_size(raw: RawDoc) -> float:
    """The deck's most common text size, not counting theme furniture: a piece of text drawn at
    the same place on at least half the frames (and three of them) is a footline or headline
    ("Author (Inst.)  Short title  date"). In a Madrid/Boadilla deck with little prose - a deck
    of charts - the \\tiny footline outweighed the words, the body came out 6 pt, and every
    size gate measured against it (tick labels belong to their chart) failed on 11 pt ticks."""
    def key(s: RawSpan) -> tuple[str, int, int, float]:
        return s["text"].strip(), round(s["bbox"][0]), round(s["bbox"][1]), round(s["size"], 1)
    frames_of: dict[tuple[str, int, int, float], set[str | None]] = {}
    for page in raw["pages"]:
        for s in page["spans"]:
            frames_of.setdefault(key(s), set()).add(page.get("label"))
    frames = len({page.get("label") for page in raw["pages"]})
    counts: Counter[float] = Counter()
    furniture: Counter[float] = Counter()
    for page in raw["pages"]:
        for s in page["spans"]:
            if font_info(s["font"]).family != "math":
                repeated = frames >= 3 and len(frames_of[key(s)]) >= max(3, 0.5 * frames)
                (furniture if repeated else counts)[round(s["size"], 1)] += len(s["text"].strip())
    counts = counts or furniture
    return counts.most_common(1)[0][0] if counts else 10.0


BULLET_GLYPHS = set("▶►▸‣•◦▪■□○●★⋆✓∗–")

ENUM_RE = re.compile(r"^(\(?\d{1,2}[.)]|\(?[a-z][.)]|\([a-z]\)|\(?[ivx]{1,4}[.)])$")

LABEL_SEP_EM = 0.4  # gap after a description label (beamer: 0.5 em; word spaces are about 0.33 em)

EQ_NUMBER_RE = re.compile(r"^\(\d+(\.\d+)*[a-z]?\)$")

MATH_OPERATORS = set("=+−<>≤≥×·/∑∏∫∈∉⊂⊆∪∩→←⇒⇔≈≠±∞")

DISPLAY_WORD_SHARE = 0.35  # a line with fewer of its characters in words of prose is a formula
