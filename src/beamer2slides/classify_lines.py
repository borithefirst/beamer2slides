"""PageClassifier's lines: spans joined by baseline, hanging glyphs and operators, line numbers,
labels and braces, and where side-by-side text splits.
"""

import re
from dataclasses import replace

from . import bidi
from .classify_graphics import GraphicsMixin
from .classify_model import Line, Rect, Span, extension_font, new_line, new_span
from .classify_text import (
    BULLET_GLYPHS, DISPLAY_WORD_SHARE, ENUM_RE, LABEL_SEP_EM, MATH_OPERATORS, compose_accents, is_code,
    is_mono, prose_share, type3_symbol, type3_text_page,
)
from .fonts import font_info


LINE_LABEL_RE = re.compile(r"^(\d{1,3}:|\[\d{1,3}\])$")
NUMBERING_RE = re.compile(r"^\(?(\d{1,2}(\.\d{1,2}){0,3}|[a-z]|[ivx]{1,4})[.)]?$")  # an item's number: 2.1, (b), iv.
GUTTER_PROSE_EM = 8.0  # a column's line beside a gutter is wider than this; ticks and table cells are not
DELIMITERS = set("|‖∣∥()[]{}⟨⟩⌊⌋⌈⌉")


class LinesMixin(GraphicsMixin):
    """PageClassifier's lines (after its graphics: words on different panels or artwork are
    different lines)."""

    def spans(self) -> list[Span]:
        # External links keep their URL; internal ones become "#page=N" (PDF page index).
        links = [(Rect.of(l["bbox"]), l.get("uri") or f"#page={l.get('page')}") for l in self.raw["links"]]
        out: list[Span] = []
        type3_words = type3_text_page(self.raw["spans"])
        for s in self.raw["spans"]:
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
            span = new_span(id=s["id"], text=text, font=s["font"].split("+", 1)[-1], size=s["size"], color=color,
                            rect=r, baseline=s["origin"][1], horizontal=abs(dy) < 0.01 and dx > 0, info=info,
                            link=next((uri for lr, uri in links if lr.contains(r.cx, r.cy)), None), drawn=False,
                            visual=visual)
            if s.get("smallcaps"):  # OpenType small caps, found from glyph ids (extract.small_caps_spans)
                span.info = replace(span.info, smallcaps=True)
            out.append(span)
        return out

    def build_lines(self, spans: list[Span]) -> list[Line]:
        n = len(spans)
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        # Words on different panels are different texts (Bergen's label column beside the body).
        panel = [self.panel_of(s.rect) for s in spans]
        # a listing's panel: monospaced text but for its line numbers (a block with a table of
        # \texttt cells is not)
        listing: set[int] = set()
        for k in {p for p in panel if p is not None}:
            words = [s for s, p in zip(spans, panel) if p == k and s.text.strip() and not re.fullmatch(r"\d{1,4}", s.text.strip())]
            mono = sum(len(s.text.strip()) for s in words if s.info.family == "mono")
            if mono and mono >= 0.9 * sum(len(s.text.strip()) for s in words):
                listing.add(k)  # (an escaped arrow or word is fine)
        # Likewise words on different boxes of the theme's artwork: a \logo in a sidebar theme's
        # corner square is not the first word of the frame title in the headline beside it.
        artwork = [self.artwork_of(s.rect) for s in spans]
        for i in range(n):
            a = spans[i]
            pi = panel[i]
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
                # edge still belongs to its line; so does a piece running over its panel's edge
                # right after a word in it - an overfull listing line's closing comma)
                if same_row and gap <= 2.0 * big and (panel[i] == panel[j] or a.color == b.color
                                                      or self.over_panel_edge(a, b, panel[i], panel[j], gap, big)) \
                        and artwork[i] == artwork[j] \
                        and not (gap > 0.8 * big and self.gutter(spans, a, b, big)) \
                        and not self.figure_label_apart(spans, a, b, gap, big):
                    parent[find(i)] = find(j)
                elif same_row and pi is not None and pi in listing and pi == panel[j] and gap <= 0.6 * self.panels[pi].bbox.w \
                        and not any(s.info.family != "mono" and re.fullmatch(r"\d{1,4}", s.text.strip()) for s in (a, b)):
                    parent[find(i)] = find(j)  # a listing's line: a comment far right of its code is spaces
        groups: dict[int, list[Span]] = {}
        for i, s in enumerate(spans):
            groups.setdefault(find(i), []).append(s)
        lines = sorted((new_line(g) for g in groups.values()), key=lambda l: (l.baseline, l.rect.x0))
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

                def over(w: Span) -> bool:
                    return min(w.rect.x1, g.rect.x1) - max(w.rect.x0, g.rect.x0) > 1.0
                if not any(w is not g and w.size >= 0.8 * size and over(w) for w in a.spans):
                    continue
                for b in out:
                    if b is a or not (a.baseline < b.baseline <= a.baseline + 2.5 * size) or any(map(over, b.spans)):
                        continue
                    left = any(g.rect.x0 - 1.5 * size <= w.rect.x1 <= g.rect.x0 + 1 for w in b.spans)
                    right = any(g.rect.x1 - 1 <= w.rect.x0 <= g.rect.x1 + 1.5 * size for w in b.spans)
                    if left and right:
                        i, j = out.index(a), out.index(b)
                        rest, joined = new_line([s for s in a.spans if s is not g]), new_line(b.spans + [g])
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

            def across(w: Span) -> bool:
                return min(w.rect.x1, g.rect.x1) - max(w.rect.x0, g.rect.x0) > 1.0
            for b in out:
                if b is g_line or not (g.baseline + 0.3 * size < b.baseline <= g.baseline + 2.5 * size) \
                        or g.rect.y1 < b.rect.y0 - 0.5 * size or any(map(across, b.spans)):
                    continue
                before = [w for w in b.spans if g.rect.x0 - 1.5 * size <= w.rect.x1 <= g.rect.x0 + 1]
                after = [w for w in b.spans if g.rect.x1 - 1 <= w.rect.x0 <= g.rect.x1 + 1.5 * size]
                if before and after and any(LinesMixin.prose_word(w) for w in before + after):
                    out.remove(g_line)
                    joined = new_line(b.spans + g_line.spans)
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
                    joined = new_line(host.spans + g_line.spans)
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
        found: list[tuple[Line, Span, bool]] = []
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
        columns: list[list[tuple[Line, Span, bool]]] = []
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
                own = new_line([num])
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
        out = LinesMixin.drop_hanging_glyphs(list(lines))
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
                prose = LinesMixin.prose_word
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
                    joined = new_line(host.spans + g_line.spans + (other.spans if other else []))
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
            texts = [s.visual for s in items if s.visual is not None]
            if len(texts) < len(items) or not all(s.horizontal for s in items) or \
                    not any(bidi.has_rtl(t) for t in texts):
                continue
            widths = [max(b.rect.x0 - a.rect.x1, a.rect.x0 - b.rect.x1) for a, b in zip(items, items[1:])]
            joins = [" " if w > 0.15 * b.size and not ta.endswith(" ") and not tb.startswith(" ")
                     else "" for w, ta, tb, b in zip(widths, texts, texts[1:], items[1:])]
            base = bidi.line_base(texts, prior)
            spaced = bidi.spaced(texts, base, joins)
            for rank, (i, text) in enumerate(bidi.logical_line(texts, base, joins)):
                s = items[i]
                if s.text == bidi.logical_text(texts[i]):
                    s.text = text
                s.reading = (n, rank, base, widths[spaced[i]] if i in spaced else 0.0)

    def over_panel_edge(self, a: Span, b: Span, pa: int | None, pb: int | None, gap: float, size: float) -> bool:
        """Two pieces nearly touching where one's panel ends: the other starts inside that panel
        and runs over its edge (its centre outside). A listing line overfull by a character
        ('"order-service",' against its frame) left the comma a box of its own, set 70 px away."""
        if pa == pb or gap > 0.3 * size:
            return False
        for inner, outer, p in ((a, b, pa), (b, a, pb)):
            if p is None:
                continue
            box = self.panels[p].bbox
            r = outer.rect
            if box.contains(r.x0 + 0.1, r.cy) and not box.contains(r.cx, r.cy) and r.w <= 1.5 * size:
                return True
        return False

    def figure_label_apart(self, spans: list[Span], a: Span, b: Span, gap: float, size: float) -> bool:
        """A figure's label beside a column of words right of the figure (a pie's pin label
        on the baseline of a list item's second line, 0.8 em from it): the label is next to
        the figure's graphics (its pin), the words well clear of them and starting where the
        column's other lines start. More than a word space apart, they are no line - joined, the
        label opened the list's line and the item split there. The label is a word: an item's
        own label (a dingbat, '1.', '(a)') stands its labelsep (0.5 em) before the same edge."""
        if gap <= 0.6 * size or not self.regions:
            return False
        left, right = (a, b) if a.rect.x0 < b.rect.x0 else (b, a)
        if sum(c.isalnum() for c in left.text) < 2 or re.fullmatch(r"\(?\w{1,3}[.):]", left.text.strip()):
            return False
        # The column's piece is words too: a lone number right of a line is the label of an item
        # read right to left, its ball's digit ('1' hanging right of a Hebrew item, the balls
        # below it the same column), and apart from its item the ball lost its number.
        if sum(c.isalnum() for c in right.text) < 2:
            return False
        def near(s: Span) -> bool:
            return any(r.distance(s.rect) <= 0.5 * size for r in self.regions)
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
        def same_line(s: Span, t: Span) -> bool:
            return abs(s.baseline - t.baseline) <= 0.5 * size and min(s.rect.y1, t.rect.y1) > max(s.rect.y0, t.rect.y0)
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
        pairs: list[tuple[Span, Span]] = []
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
        for k, i in [(step, i) for step in (1, 2) for i in range(len(out) - step)]:
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
        tabbed = [(l, tab) for l in out if (tab := l.tab) is not None and any(s.rect.x1 <= tab.rect.x0 + 0.5 for s in l.spans)]
        for line in out:
            if line.tab is not None or not tabbed:
                continue
            for k, span in splits(line).items():
                end = line.spans[k - 1].rect.x1
                sep = span.rect.x0 - end
                for o, o_tab in tabbed:
                    if abs(o.size - line.size) > 0.5 or abs(o.baseline - line.baseline) > 6 * line.size:
                        continue
                    o_end = max(s.rect.x1 for s in o.spans if s.rect.x1 <= o_tab.rect.x0 + 0.5)
                    same = abs(span.rect.x0 - o_tab.rect.x0) <= 0.6 and abs(end - o_end) <= 0.6
                    wide = end > o_end + 0.6 and abs(line.spans[0].rect.x0 - o.spans[0].rect.x0) <= 0.6 and \
                        abs(sep - (o_tab.rect.x0 - o_end)) <= 0.1 * line.size
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
        def cmex(s: Span) -> bool:
            return extension_font(s.font)

        def words(l: Line) -> int:
            return sum(len(s.text.strip()) >= 2 and s.text.strip().isalpha() and s.info.family not in ("math", "icon")
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
