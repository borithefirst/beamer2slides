"""PageClassifier's line reasons: which lines are bullets, formulas, figure labels, chart ticks or
theme, and which stay text.
"""

import re
import statistics

from .classify_model import (
    ACCENTS, SMALL_IMAGE_PT, Fraction, Line, Rect, Span, cluster_rects, extension_font, overlap, reads_rtl, union_all,
)
from .classify_text import (
    BULLET_GLYPHS, DISPLAY_WORD_SHARE, ENUM_RE, EQ_NUMBER_RE, LABEL_SEP_EM, MATH_OPERATORS, OPERATOR_NAMES,
    WORD_RE, accent_beside, bullet_shape, is_code, label_of, long_arrow_groups, math_content, prose_share,
    script_of, unmeasured_symbols,
)
from .classify_figures import FiguresMixin
from .classify_lines import is_leader


PRESET_GLYPHS = set("▶►▸‣•●★⋆")  # glyphs with a close Slides bullet preset (see emit.bullet_preset)
# Symbol-font (pifont/Zapf Dingbats) item labels that a Slides bullet draws closely enough, by the
# bullet glyph emit knows them as (`emit_metrics.GLYPH_SHAPES`): \item[\ding{86}] is a native
# star bullet that moves with its item. (As a picture of the glyph beside the item, it stayed
# where the PDF drew it while Slides set the item's lines at other heights, beside the wrong line:
# real_africa-remote-sens-30 slides 39 and 41.) Filled stars of any point count are Slides' star;
# arrowheads its triangle; outlined or other dingbats (✓, ☞, ❄) stay pictures.
ICON_BULLET_GLYPHS: dict[str, str] = {ch: glyph for chars, glyph in (
    ("★✦✴✵✶✷✸✹", "★"), ("▶►➢➣➤", "▶"), ("●", "●"), ("■", "■"), ("❏❐❑❒", "□"), ("◆❖", "◆")) for ch in chars}
LABEL_GLYPHS = BULLET_GLYPHS | set("+✗✘→⇒—♦◆⋄")
# A caption's label: "Figure:", "Figure 3:", "Fig. 2.", "Table IV:" (beamer's caption templates).
CAPTION_RE = re.compile(r"^\S+\.?(\s+[\dIVXivx]+(\.\d+)*)?\s*[:.](\s|$)")
RELATIONS = set("=<>≤≥≈≠≡∼≃≅∝⇒⇔→")
# A tick label that is a number, or several touching ("1,0001,0501,100"; "10%", "−0.5").
TICK_NUMBER_RE = re.compile(r"[-−+]?\d[\d.,−%]*")
# A smaller mark this far (in ems of the words after it) above their baseline is a superscript.
FOOTNOTE_RAISE_EM = 0.3


def footnote_mark(first: Span, nxt: Span) -> bool:
    """A line opening on a superscript mark (the `$^*$` of a footnote under a formula, '* plus
    complicated models'): the mark of a note, no item label. As a hanging label it was written
    `*<TAB>text`, and in a centred paragraph Slides took the tab to its own stop, the mark far
    left of its words. (beamer raises its own item triangle 1.25 pt at \\scriptsize: 0.11 em of
    an 11 pt item.)"""
    return first.size < nxt.size and nxt.baseline - first.baseline >= FOOTNOTE_RAISE_EM * nxt.size


class ReasonsMixin(FiguresMixin):
    """Methods of classify.PageClassifier; the state they share is `classify_state.PageState`."""

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
            if first.info.family == "icon" and gap >= 0.25 * line.size and max(first.rect.w, first.rect.h) <= 1.6 * line.size \
                    and token in ICON_BULLET_GLYPHS:
                line.bullet = {"kind": "glyph", "text": ICON_BULLET_GLYPHS[token], "color": first.color,
                               "bbox": first.rect.as_list(), "label": label_of([first])}
                line.bullet_spans = [first]
                return
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
            mark = footnote_mark(first, nxt) and not on_ball
            if gap >= 0.25 * line.size and ((token in LABEL_GLYPHS - PRESET_GLYPHS) or lettered) and not on_ball \
                    and not dash and not mark:
                # \item[--], \item[\checkmark]: Slides has no such bullet preset. The glyph stays
                # literal text, and a tab reaches the item text (hanging indent).
                line.tab = nxt
                return
            # (a bullet glyph opening a leader stands right against its dots: `leader_item`)
            apart = gap >= 0.25 * line.size or token in BULLET_GLYPHS and is_leader(nxt.text)
            if apart and (token in BULLET_GLYPHS or ENUM_RE.match(token)) and not on_ball and not dash and not mark:
                if token in BULLET_GLYPHS:
                    line.bullet = {"kind": "glyph", "text": token, "color": first.color, "bbox": first.rect.as_list(),
                                   "label": label_of([first])}
                else:
                    line.bullet = {"kind": "number", "text": token, "color": first.color, "bbox": first.rect.as_list(),
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
                    any(o is not im and max(orr.w, orr.h) <= 20 and (overlap(orr, ir) > 0.2 * ir.w * ir.h or ir.contains_rect(orr, tol=0.5))
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
                look = bullet_shape(self.graphic_paths.get(tuple(g.as_list())))
                # (a mark with parts drawn inside it - a globe's meridians in its disc - is no glyph)
                if "shape" in look and "color" in look and \
                        not any(g.contains_rect(o, tol=0.5) and not o.contains_rect(g, tol=0.5) for o in self.graphics if o is not g):
                    line.bullet = {"kind": "shape", "text": "", "bbox": g.as_list(), "patch": True,
                                   "shape": look["shape"], "color": look["color"]}
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
        def level(r: Rect) -> bool:
            return line.baseline - 0.9 * line.size <= r.cy <= line.baseline + 0.1 * line.size
        # (a numbered ball comes out 11 or 12 pt as its image's box is rounded: one of 12 pt is no
        # small image, and was taken for a graphic around a word, a hole)
        balls = self.small_images + [(im, r) for im, r in ((im, Rect.of(im["bbox"])) for im in self.raw["images"])
                                     if min(r.w, r.h) >= SMALL_IMAGE_PT and max(r.w, r.h) <= 1.25 * line.size]
        for im, ir in balls:
            if not 0.8 <= ir.w / max(ir.h, 0.01) <= 1.25 or \
                    any(o is not im and max(orr.w, orr.h) <= 20 and (overlap(orr, ir) > 0.2 * ir.w * ir.h or ir.contains_rect(orr, tol=0.5))
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
                look = bullet_shape(self.graphic_paths.get(tuple(g.as_list())))
                if "shape" in look and "color" in look and \
                        not any(g.contains_rect(o, tol=0.5) and not o.contains_rect(g, tol=0.5) for o in self.graphics if o is not g):
                    line.bullet = {"kind": "shape", "text": "", "bbox": g.as_list(), "patch": True,
                                   "shape": look["shape"], "color": look["color"]}
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
            if o is g or not o.intersects(g.expand(1.0)) or g.contains_rect(o, tol=0.5) or \
                    (o.contains_rect(g, tol=0.5) and o.contains_rect(word, tol=0.5)):
                continue
            return False
        return True

    @staticmethod
    def label_tabs(lines: list[Line]) -> None:
        """Any short label (\\item[\\textbf{Q:}]) ending where a neighbouring item's hanging label
        ends, with its text starting where that item's text does, hangs the same way."""
        tabbed = [(l, tab) for l in lines if (tab := l.tab) is not None and l.reason is None]
        for line in lines:
            if line.reason is not None or line.tab is not None or line.bullet or len(line.spans) < 2:
                continue
            label, text = line.spans[0], line.spans[1]
            if len(label.text.strip()) > 6 or text.rect.x0 - label.rect.x1 < LABEL_SEP_EM * line.size:
                continue
            for other, tab in tabbed:
                before = [s for s in other.spans if s.rect.x1 <= tab.rect.x0]
                if before and abs(other.baseline - line.baseline) <= 5 * line.size and abs(other.size - line.size) <= 0.5 \
                        and abs(tab.rect.x0 - text.rect.x0) <= 0.6 and abs(before[-1].rect.x1 - label.rect.x1) <= 0.6:
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
        its size, starting where that line's words start (the paragraph's left edge). A display
        formula is centred or indented, never flush with its paragraph. As a display, the item
        it ended became one picture, bullet and words included.

        Not a delimiter's piece alone (a matrix's upper parenthesis hangs on a line of its own
        under the "For the path P3 on three vertices," above it, and a word of that line started
        where it does: as the item's formula it became a hole, the parenthesis split from its
        lower half, r1_math_v1 s2), nor a line starting under a word inside the line above."""
        if line.bullet or line.tab is not None:
            return False
        if all(extension_font(s.font) for s in line.content if s.text.strip()):
            return False
        size = line.size
        # (where the line's words start: its first, or the one after its hanging label)
        def starts(o: Line) -> list[float]:
            return [min((s.rect.x0 for s in o.content if s.text.strip()), default=o.rect.x0)] + \
                ([o.tab.rect.x0] if o.tab is not None else [])
        return any(o is not line and o.spans[0].horizontal and o.reason not in ("theme", "figure", "rotated", "math")
                   and abs(o.size - size) <= 0.2 * size
                   and 0.8 * size <= line.baseline - o.baseline <= 1.6 * size
                   and any(abs(x - line.x0) <= 1.5 for x in starts(o))
                   and prose_share(o.content) >= DISPLAY_WORD_SHARE for o in self.all_lines)

    def display_line(self, line: Line) -> bool:
        """A line that is a display formula: mostly formula (few words of prose, see prose_share)
        and set apart from any paragraph's flow."""
        return prose_share(math_content(line)) < DISPLAY_WORD_SHARE and not self.in_prose_flow(line)

    def simple_fraction(self, line: Line, bar: Rect) -> Fraction | None:
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

        def by_x(group: list[Span]) -> list[Span]:
            return sorted(group, key=lambda s: s.rect.x0)
        return bar, by_x(above), by_x(below)

    def line_bars(self, line: Line) -> list[Rect]:
        """Fraction bars and radical overbars at a line - not the overbar of a radical sign set in
        another line of words (a √ of the line below reaches up into this one; a sign alone on a
        line of its own is this line's, see formula holes)."""
        bars = [b for b in self.bars if b.expand(1).intersects(line.rect)]
        if not bars:  # (the signs are looked for over every line: only where a bar asks)
            return []
        size = line.size
        signs = [s for l in self.all_lines if l is not line and sum(o.size >= 0.8 * size for o in l.spans) >= 2
                 for s in l.spans if extension_font(s.font) or "√" in s.text]
        return [b for b in bars if not any(
            abs(s.rect.x1 - b.x0) <= 1 and s.rect.y0 - 1 <= b.y0 <= s.rect.y1 for s in signs)]

    def formula_holes(self, line: Line, fractions: list[Fraction]) -> list[list[Span]]:
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
                    # (a text italic's letters with spaces between, "b N" of helvet's math: the
                    # formula went on past them, r2_fonts_helvet s3 held "− " in its hole and
                    # set " b N)" as words, gaps on both sides)
                    or (s.info.italic and bool(t) and all(len(w) == 1 and w.isalpha() for w in t.split()))
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

        holes: list[list[Span]] = []
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
        if self._word_graphics is None:
            # (an \fbox's top and bottom rules look like a table of one line)
            frames = [f for f in (union_all(r.rect for r in g).expand(1) for g in self.table_rules) if f.h > 2.5 * self.body]
            self._word_graphics = [c for c in cluster_rects(self.graphics, gap=0.5)
                                   if not any(f.contains_rect(c, tol=0.5) for f in frames)] if self.graphics else []
        groups: list[tuple[list[Span], Rect]] = []
        for g in self._word_graphics:
            if g.h > 2.2 * size or not line.baseline - size <= g.cy <= line.baseline + 0.4 * size:
                continue
            touched = [s for s in spans if min(s.rect.x1, g.x1) - max(s.rect.x0, g.x0) > 0.3 * s.rect.w]
            # (a frame closed around its words may be wider: \framebox[2.5cm]; one hugging them
            # as wide as it needs, a \boxed formula over half the page: real_beamer-monodromy s12)
            held = union_all(s.rect for s in touched) if touched else None
            framed = held is not None and g.contains_rect(held, tol=0.5) and (g.w <= 0.5 * self.W or g.w <= held.w + size)
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
        # Words of prose beside it, not spans: one span can carry the whole rest of the line ("by
        # Galois correspondence." after a framed formula stayed text beside a frame of PDF width).
        if not groups or sum(sum(ch.isalpha() for ch in w) >= 2 for s in outside for w in s.text.split()) < 2:
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
        if (holes or bars or unmeasured_symbols(spans)) and prose_share(spans) == 0.0 \
                and self.wrapped_formula(line):
            # A paragraph's formula wrapped onto a line of its own and not flat text ("h(G) =
            # min" then a picture of its limits and fraction, r1_math_v1 s6; "q₀ x₁ ··· xₙ ⊔"
            # with Slides' subscripts low and ⊔ from a fallback face, r1_math_v3 s6) is one
            # hole: its pieces set natively beside pictures of the rest came out in two faces
            # and at two sizes. It stays in its paragraph (wrapped_formula), as one picture.
            # Not when a word of prose follows it (", with β = 1 for the plain", r2_fonts_firamath
            # s3): the words stay text.
            whole = sorted((s for s in spans if s.text.strip()), key=lambda s: s.rect.x0)
            while len(whole) > 1 and whole[-1].text.strip() in (",", ".", ";", ":"):
                whole = whole[:-1]  # (trailing punctuation is prose again)
            line.add_holes([whole])
            line.fractions = []
            return "inline"
        if holes:
            # Prose with a few complex formulas: the words stay text, each formula becomes a
            # picture placed over a gap left in the text.
            line.add_holes(holes + long_arrow_groups(line.content))
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
        # A long arrow in a line of text Slides carries ("x ∈ A ⟺ f(x) ∈ B", "O₂ ⟶ GFP*") is a
        # hole of its own: its picture keeps the PDF's length and the thick spaces around it.
        arrows = long_arrow_groups(line.content)
        if arrows:
            line.add_holes(arrows)
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
            elif line.text.strip() and all(s.id in self.furniture for s in line.spans if s.text.strip()):
                line.reason = "theme"  # the deck's furniture (classify.furniture), whatever its size
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

        def bare_rule(r: Rect) -> bool:
            return r.w <= 4 and r.h >= 25
        joined: list[Line] = []
        changed = True
        while changed:
            changed = False
            for line in lines:
                if line.reason is not None or line.bullet or line.tab is not None or line.size > 1.15 * self.body or \
                        not (len(line.text.replace(" ", "")) <= 12 or self.tick_row(line)) or self.wrapped_end(line, lines):
                    continue
                words = [s for s in line.content if s.text.strip() and not TICK_NUMBER_RE.fullmatch(s.text.strip())]
                nearby = [reg for reg in regions if reg.distance(line.rect) <= 0.8 * line.size]
                if any(not bare_rule(reg) for reg in nearby) or (nearby and not words):
                    line.reason = "figure"
                    if any(not bare_rule(reg) for reg in nearby):
                        regions.append(line.rect)
                    joined.append(line)
                    changed = True
        self.release_stranded_labels(lines, joined)
        self.axis_titles(lines)
        self.release_held_captions(lines)

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
        # An \xrightarrow's or mhchem arrow's labels (120 °C over it, "in vacuo" under it) are
        # small lines of their own inside the arrow's length: its picture's, or they stayed text
        # printed over the formula Slides sets at other widths (r1_sci_v3 s3). A label between
        # two arrows is the nearer one's (r1_math_v2 s7: Lp over the third line's arrow is also
        # just under the second's).
        near: dict[int, tuple[float, Line, Line]] = {}
        for host in lines:
            size, held = host.size, {id(s) for h in host.holes for s in h}
            for g in long_arrow_groups(host.content):
                if not any(id(s) in held for s in g):
                    continue
                arrow = union_all(s.rect for s in g)
                for l in lines:
                    if l is host or l.bullet or l.reason not in (None, "math") or l.size >= 0.85 * size or \
                            len(l.text.replace(" ", "")) > 24 or any(l in o.limits for o in lines):
                        continue
                    if arrow.x0 - 0.3 * size <= l.rect.x0 and l.rect.x1 <= arrow.x1 + 0.3 * size and (
                            -0.5 * size <= arrow.y0 - l.rect.y1 <= 0.5 * size or
                            -0.5 * size <= l.rect.y0 - arrow.y1 <= 0.5 * size):
                        gap = abs(l.rect.cy - arrow.cy)
                        if id(l) not in near or gap < near[id(l)][0]:
                            near[id(l)] = (gap, l, host)
        for _, l, host in near.values():
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
                touching = any(m.rect.expand(1.0 * max(m.size, line.size)
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
                if (touching and small and len(txt) <= 6) or eqno or limit or self.same_formula(line, maths) or fraction_part:
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

        rows: list[tuple[Line, float, list[Rect]]] = []
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
        out: list[Line] = []
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
        pieces: list[tuple[str, float, float]] = []  # (text, x0, x1): spans that touch are one label ("1" "," "000")
        for s in (s for s in line.content if s.text.strip()):
            if pieces and s.rect.x0 - pieces[-1][2] < 0.25 * line.size:
                text, x0, x1 = pieces[-1]
                pieces[-1] = (text + s.text.strip(), x0, max(x1, s.rect.x1))
            else:
                pieces.append((s.text.strip(), s.rect.x0, s.rect.x1))
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
        plots: list[tuple[Rect, Rect]] = []
        for reg in self.regions:
            box, marks, grown = reg, 0, True
            members: set[int] = set()
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
                    (text.endswith(".") and len(text.split()) >= 4) or self.in_label_row(line, lines):
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

    @staticmethod
    def in_label_row(line: Line, lines: list[Line]) -> bool:
        """One of a row of like short labels two or more of which stay text ('Week 0  Week 1
        Week 2-3  Week 4' over a timeline): the row's, not the title of a drawing it happens to
        be centred over (r1_design_v1 s5, the label above a callout box alone went into the
        callout's picture, its row left text)."""
        face = (line.spans[0].font, round(line.size, 1))
        row = [o for o in lines if o is not line and o.reason is None and not o.bullet and o.spans and
               (o.spans[0].font, round(o.size, 1)) == face and abs(o.baseline - line.baseline) <= 0.25 * line.size
               and len(o.text.replace(" ", "")) <= 12]
        return len(row) >= 2
