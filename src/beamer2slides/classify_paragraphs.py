"""PageClassifier's paragraphs and text boxes: continuation, alignment and justification, and the
runs a paragraph is written as.
"""

import re
from dataclasses import replace

from .classify_model import (
    ACCENTS, HOLE_PAD, Line, Paragraph, Rect, Span, extension_font, ir_bullet, new_paragraph, reads_rtl, union_all,
)
from .classify_text import (
    COMPOSED, FRACTION_SLASH, NBSP, NEGATION, RAISED_MARKS, cjk, code_indent, code_pitch, explicit_hyphen,
    family_of, first_word_width, formula_groups, gap_between, glued, is_code, is_mono, last_word_width,
    line_starts, line_word_width, look, math_family, math_pieces, math_text, negate, prose_spaces, raised_mark,
    reading_order, script_of, script_size, stretched, thin_span, with_accent, with_text,
)
from .classify_lines import LinesMixin
from .fonts import serif_math_letters
from .ir import Align, BeforeWord, Run, TextElement
from .ir import Paragraph as ParagraphJson


EM_SPACE = chr(0x2003)
TRAILING_PUNCT = re.compile(r"(?<=\w)[,.;:!?)\]]+\s*$")
BOX_PAD = " "   # a padded \colorbox's \fboxsep, highlighted (a no-break space: the box never breaks)


class ParagraphsMixin(LinesMixin):
    """Methods of classify.PageClassifier; the state they share is `classify_state.PageState`."""

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
            box = self.panels[panel].bbox
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
        def target(l: Line) -> set[str | None]:
            return {s.link for s in l.content if s.text.strip()}
        ta, tb = target(a), target(b)
        return len(ta) == 1 and len(tb) == 1 and ta != tb and \
            all(t is not None and t.startswith("#page=") for t in ta | tb)

    def find_hfill_pieces(self, lines: list[Line]) -> None:
        """`hfill_pieces`: id of a line -> the piece an \\hfill pushed to the right end of its row
        (an example's source, an item's reference, an attribution), and `hfill_hosts` the other
        way round. A piece is another line on the same baseline, more than an em further right,
        short, and ending at the right edge of the text area (the page's text margin, or the
        panel it is in). TeX ended the row there: nothing on the next line continues the words
        before the gap - where the piece itself wrapped, its tail opens the next line."""
        self.hfill_pieces = {}
        self.hfill_hosts = set()
        native = [l for l in lines if l.reason is None and all(s.horizontal for s in l.spans)]
        for a in native:
            right = self.W - self.text_margin
            panel = self.panel_of(a.rect)
            if panel is not None:
                box = self.panels[panel].bbox
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
                self.hfill_hosts.add(id(piece))

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
        stack: list[Line] = []
        at = line.baseline
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

    def continues(self, par: Paragraph, line: Line) -> Align | None:
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
            if last.x0 - 0.3 * par.size - first_word_width(head, False) > col_left + 0.5:
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
            first_word = head.rect.w if thin_span(head, line, par.lines + [line]) else line_word_width(line, False)
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
            if u.x1 - u.x0 >= 0.5 * (col_right - u.x0) and u.x1 + 0.3 * size + line_word_width(below, False) < col_right - 0.5:
                return True
            below = u
        return False

    def single_line_align(self, line: Line, margin: float, neighbours: list[Paragraph]) -> Align:
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
            best: tuple[Paragraph, Align] | None = None
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
                paragraphs.append(new_paragraph([line], align="left", reason=line.reason))
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
            rtl = all(reads_rtl(p.first) for p in box)

            def start(bbox: list[float]) -> float:
                return -Rect.of(bbox).x1 if rtl else Rect.of(bbox).x0
            xs = sorted({round(start(p.bullet["bbox"]), 0) for p in box if p.bullet})
            levels: list[float] = []
            for x in xs:
                if not levels or x - levels[-1] > 2:
                    levels.append(x)
            for p in box:
                if p.bullet:
                    bx = start(p.bullet["bbox"])
                    p.level = min(range(len(levels)), key=lambda i: abs(levels[i] - bx))
        return boxes

    def text_element(self, box: list[Paragraph], element_id: str) -> TextElement:
        """A text box of paragraphs, as deck.json says it. (An icon bullet is no bullet here:
        `PageClassifier.classify` makes it a picture of the item.)"""
        rect = union_all([p.rect for p in box] + [Rect.of(b["bbox"]) for b in (p.bullet for p in box) if b])
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
            return any(a.x1 - a.x0 + 0.2 * p.size + first_word_width(b.content[0], False) <= room
                       for a, b in zip(p.lines, p.lines[1:]))
        runs = [self.runs(p, code_indent(p, rect.x0, pitch) if code else "", unbalanced(p), pitch, rect.x0) for p in box]
        # (said only where it is known, on a left-aligned wrapped paragraph: line_starts)
        starts = [line_starts(p.lines, r) if p.align == "left" and not code and not p.direction else None
                  for p, r in zip(box, runs)]

        def paragraph(p: Paragraph, r: list[Run], s: list[int] | None) -> ParagraphJson:
            direction = p.direction
            return {
                "align": p.align, "level": p.level, "bullet": ir_bullet(p.bullet), "size": round(p.size, 2),
                # Said only where it is true, as `deck_ir` says it of a deck that is read
                # back: a left-to-right paragraph is every deck this project had until now.
                **({"direction": direction} if direction else {}),
                # (as `deck_ir` reads Slides' JUSTIFIED: a left paragraph, justified)
                **({"justified": True} if p.justified else {}),
                # (a first line set in by \parindent starts at lines[0].x0, emit indents it)
                "text_x0": round(p.x0 - p.indent, 2),
                "tab_x0": round(p.first.tab.rect.x0, 2) if p.first.tab else None,
                "lines": [{"baseline": round(l.baseline, 2), "x0": round(l.x0, 2), "x1": round(l.x1, 2)}
                          for l in p.lines],
                # Right edge a wrapped line could grow to before TeX would have pulled up the
                # next line's first word: a text box narrower than this wraps the same way.
                # (as a width from the paragraph's left edge, so centred lines count too; the next
                # word up to where Slides may break it, after a hyphen: 'Санкт-' of
                # 'Санкт-Петербургский' was pulled up, r3_scripts_ruxe s1)
                "wrap_limit": round(min(l.x0 for l in p.lines) + min(a.x1 - a.x0 + 0.25 * p.size + line_word_width(b, True)
                                                                     for a, b in zip(p.lines, p.lines[1:])), 2)
                              if len(p.lines) > 1 and all(l.content for l in p.lines) else None,
                **({"line_starts": s} if s else {}),
                "runs": r,
            }
        return {
            "id": element_id, "kind": "text", "role": box[0].role, "bbox": rect.as_list(),
            "panel": self.panel_of(rect),
            "paragraphs": [paragraph(p, r, s) for p, r, s in zip(box, runs, starts)],
            "code": code,
            "spans": [s.id for p in box for s in p.spans if s.info.family != "icon" and not s.drawn
                      and not any(s in h for l in p.lines for h in l.holes)],
            # Fraction bars now written as text, underlines and highlight boxes now text
            # styles: they leave the background with the glyphs.
            "strokes": [f[0].as_list() for p in box for l in p.lines for f in l.fractions] +
                       list({tuple(r.as_list()): r.as_list() for p in box for s in p.spans
                             for r in self.decor_rects.get(s.id, [])}.values()),
        }

    @staticmethod
    def runs(par: Paragraph, indent: str, soft_breaks: bool, pitch: float | None, x_ref: float) -> list[Run]:
        """`indent`: spaces a code line starts with (`code_indent`, else ""); `soft_breaks`: its
        lines keep their breaks (a title's); `pitch`, `x_ref`: a code block's column grid
        (`code_pitch`), which its spaces keep, else None and 0."""
        runs: list[Run] = []
        prev: Span | None = None
        hole_x1 = 0.0
        for li, line in enumerate(par.lines):
            words = [s for s in line.content if s.text.strip()]
            gaps = [gap_between(a, b) / line.size for a, b in zip(words, words[1:])]
            # (a justified line's own word space, stretched; a \quad does not stretch)
            # (its last line keeps natural spaces)
            word_space = min(gaps) if par.justified and gaps and li < len(par.lines) - 1 else 0.33
            order = reading_order(line)
            # (not in code, whose spaces are columns)
            formulas: dict[int, int] = {} if pitch else formula_groups(line, max(l.x1 - l.x0 for l in par.lines))
            accent = ""  # an accent at the end of a span, for the letter under it in the next one
            for si, (span, forced) in enumerate(order):
                if isinstance(span, str):  # (FRACTION_SLASH, the only string)
                    main = line.main
                    runs.append({"text": FRACTION_SLASH, "font": main.font, "family": family_of(main),
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
                    before: list[BeforeWord] = [(round(s.rect.w, 2), s.font, family_of(s), s.info.bold, s.info.italic,
                               # math spacing is part of the span (" ≥"): Slides sets a plain space there
                               math_text(s.font, s.text)[0] if s.info.family == "math" else s.text, round(s.rect.x0, 2))
                              for s in line.content if s.rect.x1 <= x0 + 0.5 and s.info.family != "icon"
                              and not any(s in h for h in line.holes)]
                    after = [s.rect.x0 for s in line.content if s.rect.x0 >= x1 - 0.5 and s not in hole and s.info.family != "icon"]
                    runs.append({"text": " ", "font": main.font, "family": family_of(main),
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
                    gap = 0.0
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
                            longer = last_word_width(prev) + first_word_width(span, False) > \
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
                            cols = round((span.rect.x0 - x_ref) / pitch) - round((prev.rect.x0 - x_ref) / pitch)
                            runs[-1]["text"] += " " * max(0, cols - len(prev.text))
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
                    hole_w = runs[-1].get("hole")
                    if si and sep == " " and hole_w:
                        # The space after a formula becomes part of its gap: TeX's space there
                        # is wider than a Slides space would be.
                        runs[-1]["hole"] = round(hole_w + gap, 2)
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
                family, italic = family_of(span), span.info.italic
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
                if id(span) in formulas:
                    # Inside a short inline formula: no line break (formula_groups).
                    glue = si > 0 and prev is not None and formulas.get(id(prev)) == formulas[id(span)] \
                        and bool(runs) and not runs[-1].get("hole")
                    if glue:
                        ahead = runs[-1]["text"]
                        runs[-1]["text"] = ahead.rstrip(" ") + NBSP * (len(ahead) - len(ahead.rstrip(" ")))
                    last = len(pieces) - 1
                    pieces = [(glued(t, glue if k == 0 else True, k < last), it) for k, (t, it) in enumerate(pieces)]
                for k, (text, italic) in enumerate(pieces):
                    plain = bool(tail) and k == 1
                    style: Run = {
                        "text": "", "font": span.font, "family": family,
                        "size": round(size, 2),
                        "bold": span.info.bold, "italic": italic, "smallcaps": span.info.smallcaps,
                        "color": span.color, "link": span.link, "script": script,
                        "underline": span.underline and not plain, "strike": span.strike and not plain,
                        "highlight": None if plain else span.highlight,
                    }
                    def marks(r: Run) -> tuple[bool, bool, str | None]:
                        return r.get("underline", False), r.get("strike", False), r.get("highlight")

                    if runs and runs[-1]["text"].endswith(" ") and marks(runs[-1]) != marks(style) and any(marks(runs[-1])):
                        runs[-1]["text"] = runs[-1]["text"][:-1]  # an underline, strike or highlight ends at the word
                        if any(marks(style)):  # and the next one starts at its word: the space between is plain
                            plain_space: Run = {**runs[-1], "text": " ", "underline": False, "strike": False,
                                                "highlight": None}
                            runs.append(plain_space)
                        else:
                            text = " " + text
                    if style.get("highlight") and span.pad_left and k == 0:
                        lead = len(text) - len(text.lstrip(" "))
                        text = text[:lead] + BOX_PAD + text[lead:]
                    if style.get("highlight") and span.pad_right and k == len(pieces) - 1 - bool(tail):
                        text = text + BOX_PAD
                    if runs and not runs[-1].get("hole") and look(runs[-1]) == look(style):
                        runs[-1]["text"] += text
                    else:
                        runs.append(with_text(style, text))
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
        out: list[Paragraph] = []

        def piece(l: Line) -> bool:
            return l.reason == "math" and bool(l.content) and all(extension_font(s.font) for s in l.content)
        for par in paragraphs:
            pieces = [l for l in par.lines if piece(l)]
            if not pieces or len(pieces) == len(par.lines) or any(l.reason == "math" for l in par.lines if not piece(l)):
                out.append(par)
                continue
            rest = [l for l in par.lines if not piece(l)]
            out.append(replace(par, lines=rest, reason=None))
            out += [new_paragraph([l], align="left", reason="math") for l in pieces]
        return ParagraphsMixin.wrapped_formulas_apart(out)

    @staticmethod
    def wrapped_formulas_apart(paragraphs: list[Paragraph]) -> list[Paragraph]:
        """A paragraph's last line that is nothing but a formula hole (`wrapped_formula`: an
        item's "(O(√n))" TeX wrapped under its "Separator theorems") goes to a paragraph of its
        own, where TeX broke. In the item, Slides' wider words wrapped "theorems" onto the
        formula's line, and a paragraph holding a hole is not measured, so its box did not grow
        to keep them on one line: the formula's picture printed over "theorems" (r3_dense_v4 s3).
        Apart, the words are measured and the formula starts its own line, as in the PDF."""
        out: list[Paragraph] = []
        for par in paragraphs:
            last = par.lines[-1]
            words = [s for s in last.content if s.text.strip()]
            held = {id(s) for h in last.holes for s in h}
            if len(par.lines) < 2 or par.reason or not held or \
                    not all(id(s) in held or s.text.strip() in (",", ".", ";", ":") for s in words) or \
                    abs(last.x0 - par.lines[-2].x0) > 1.5 and abs(last.x0 - (par.lines[-2].tab.rect.x0 if par.lines[-2].tab else -1e9)) > 1.5:
                out.append(par)
                continue
            out.append(replace(par, lines=par.lines[:-1]))
            out.append(Paragraph(lines=[last], align=par.align, reason=None, role=par.role, level=par.level, indent=0.0,
                                 justified=False))
        return out
