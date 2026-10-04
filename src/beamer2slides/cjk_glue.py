"""The glue TeX sets between Chinese or Japanese characters and Latin ones, as Slides is made to
leave it.

Japanese TeX puts a skip of its own between a kana or kanji and a Latin letter, digit or most
punctuation (LuaTeX-ja's and pLaTeX's xkanjiskip: 0.25 zw of the size it was set at; xeCJK's
CJKecglue), with no space in the source ('VPNを使って', 'HVCAN上のIP電話'). It is no character of
the PDF, only a pen move. real_slide-20250221's (luatexja, metropolis) is 2.4 pt whatever the size
it stands among: 0.168 em of its 14.3 pt body text, which extract's JOIN_GAP (0.15 em) read as a
space, but 0.139 em of a 17.2 pt frame title and 0.116 em of the 20.7 pt title page, which it
did not: 'HVCAN上のIP電話...' in Slides, every title with Latin words 1-2.6% short (r2 ledger,
cjk-latin-xkanjiskip-lost).

So a gap of GLUE_MIN em or more (of the Latin side's size) between a CJK character and a
non-CJK one (`boundary`) is written as CJK_GLUE in the Latin side's run (`extract`'s spans, and
classify's joins between spans and lines), up to GLUE_MAX em: a wider gap is a word space, as
before. CJK_GLUE is the six-per-em space, 1/6 em by definition and in Lato 0.169 em (measured,
`mono_edges.EDGE_SPACE_EM`, the same character): 2.4 pt of a 14.35 pt Fira Sans run exactly,
where a plain space in Noto Sans JP (0.224 em) was a third too wide. Written in the Latin run,
the character is drawn by the deck's Latin face (Lato, Fira Sans), whose six-per-em space is
1/6 em; Noto Sans JP's was not measured.

A line TeX broke at the glue left no gap to measure: classify joins such lines with CJK_GLUE
between a CJK letter and a Latin letter or digit (`line_glue`) - TeX would set its glue there.

Readers: `inverse.latex_escape` writes nothing for it (`as_tex`: TeX sets its glue again, the
source said 'VPNを'; elsewhere a space), `compare.NORMALISE` reads it as a space (it is EDGE_SPACE), and
`text_layout` breaks after it as Slides may (UAX #14 BA).
"""

import unicodedata

from .mono_edges import EDGE_SPACE
from .scripts import script_of

CJK_GLUE = EDGE_SPACE  # SIX-PER-EM SPACE: the same character as inline code's edge fill
GLUE_MIN = 0.08        # em of the Latin side's size: a gap nearer a sixth of an em than none
GLUE_MAX = 0.21        # em: a wider gap is nearer a plain space (0.19-0.22 em in Slides' faces)


def chinese_or_japanese(ch: str) -> bool:
    """`classify_text.cjk` (a character of Chinese or Japanese text), without classify."""
    return bool(ch) and script_of(ch) in ("han", "kana")


def _latin(ch: str) -> bool:
    """A character every Latin font has (letters, digits, punctuation), not a space or a mark."""
    return bool(ch) and not ch.isspace() and script_of(ch) is None and not unicodedata.combining(ch)


def boundary(before: str, after: str) -> bool:
    """Whether TeX's CJK glue may stand between the characters `before` and `after`: one is
    Chinese or Japanese, the other Latin. Whether it does is the gap the PDF leaves. (A glyph may
    be several characters, a ligature's: its last and first are the ones beside the gap.)"""
    before, after = before[-1:], after[:1]
    return (chinese_or_japanese(before) and _latin(after)) or (_latin(before) and chinese_or_japanese(after))


def in_glue(gap: float) -> bool:
    """Whether a gap of `gap` em (of the Latin side's size) at a `boundary` is TeX's CJK glue."""
    return GLUE_MIN <= gap < GLUE_MAX


def line_glue(before: str, after: str) -> bool:
    """Whether two lines, the first ending on `before` and the next starting on `after`, join
    with CJK_GLUE: a CJK letter beside a Latin letter or digit, where TeX's glue would stand had it
    not broken the line there (next to punctuation it may not)."""
    def letter(ch: str) -> bool:
        return bool(ch) and unicodedata.category(ch)[0] in "LN"
    return boundary(before, after) and letter(before[-1:]) and letter(after[:1])


def as_tex(text: str) -> str:
    """`text` with each CJK_GLUE at a CJK boundary taken out (TeX sets its glue there itself) and
    any other a space. Call after the edge fill (CJK_GLUE then a space) is read as one space."""
    out: list[str] = []
    for i, ch in enumerate(text):
        if ch == CJK_GLUE:
            if not boundary(text[i - 1:i], text[i + 1:i + 2]):
                out.append(" ")
            continue
        out.append(ch)
    return "".join(out)
