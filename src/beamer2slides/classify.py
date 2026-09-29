"""Stage 2: turn raw page content into native text boxes plus background leftovers (deck.json).

Pipeline per page:
  drawings/images -> panels (theme bars, blocks), figure regions, short bars, small images
  spans           -> lines (baseline clustering, scripts attached)
  lines           -> reasons: rotated | figure | theme | math, and bullets
  lines           -> paragraphs (continuation, alignment, TeX paragraph-break test)
  paragraphs      -> text boxes (same panel, same column, close vertically)
Every span ends up in exactly one text element or in `left_in_background`.

This module is the stage's face: `classify`, `classify_page`, `PageClassifier` and the deck-wide
passes after it. The page's model is `classify_model` (Rect, Span, Line, Paragraph), what is
read off its text `classify_text`, and PageClassifier's methods live by topic in
`classify_graphics`, `classify_lines`, `classify_reasons`, `classify_paragraphs`,
`classify_figures` and `classify_tables`. Names callers have always taken from here still are.
"""

import re
from dataclasses import replace

from .classify_model import HOLE_PAD, Line, Paragraph, Rect, Span, union_all
from .classify_model import box_outline, upright_ellipse  # noqa: F401 (callers take these from here)
from .classify_reasons import ReasonsMixin
from .classify_text import (
    body_size, code_indent, code_pitch, first_word_width, is_code, line_starts, line_word_width, math_text,
)
from .classify_text import (  # noqa: F401 (callers take these from here)
    bullet_shape, card_text, compose_accents, gap_between, hyphen_cut, label_of, math_family, math_pieces,
    negate, span_runs, type3_symbol,
)
from .raw_types import RawPage, RawSpan

FRAME_COUNTER_RE =re.compile(r"^\d{1,4}( ?/ ?\d{1,4})?$")


class PageClassifier(ReasonsMixin):
    """One page's classification. Its state (the page, its panels, regions and bars, what
    decorates which span) is declared in `classify_state.PageState` and found in `classify`; the
    mixins' methods, inherited one from the next, share it."""

    def __init__(self, page: dict, body: float) -> None:
        # The page is trusted to be what extract writes (`raw_types.RawPage`): a page read from
        # raw.json is not checked key by key yet. (A test's hand-built page holds only what its
        # method reads.)
        super().__init__(RawPage(**page), body)
        self.page = page  # (marked.py reads the spans of the page it built through this)
        self.raw_spans: dict[str, RawSpan] = {s["id"]: s for s in self.raw["spans"]}
        # marked.py's view of the same spans, untyped: its `params` takes a plain dict, which a
        # RawSpan is not. (Goes once `params` takes the raw types.)
        self._raw_spans = {s["id"]: s for s in page["spans"]}

    # -- output -----------------------------------------------------------------

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
            el = self.text_element([Paragraph([Line(turned)])], f"p{self.raw['index']}rt{len(out)}")
            out.append({**el, "bbox": union_all(s.rect for s in row).as_list(), "panel": self.panel_of(union_all(s.rect for s in row)),
                        "rotation": -90 if up else 90})
        return out

    def page_dir(self, s: Span) -> tuple[float, float]:
        dx, dy = self.raw_spans[s.id]["dir"]
        return dx, dy

    def page_origin(self, s: Span) -> tuple[float, float]:
        x, y = self.raw_spans[s.id]["origin"]
        return x, y

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
            return any(a.x1 - a.x0 + 0.2 * p.size + first_word_width(b.content[0], False) <= room
                       for a, b in zip(p.lines, p.lines[1:]))
        runs = [self.runs(p, code_indent(p, rect.x0, pitch) if code else "", unbalanced(p), pitch, rect.x0) for p in box]
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
                # (as a width from the paragraph's left edge, so centred lines count too; the next
                # word up to where Slides may break it, after a hyphen: 'Санкт-' of
                # 'Санкт-Петербургский' was pulled up, r3_scripts_ruxe s1)
                "wrap_limit": round(min(l.x0 for l in p.lines) + min(a.x1 - a.x0 + 0.25 * p.size + line_word_width(b, True)
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

        n = self.raw["index"]
        elements = [self.text_element(box, f"p{n}t{bi}") for bi, box in enumerate(boxes)]
        self.line_owner = {id(l): (f"p{n}t{bi}", p.align) for bi, box in enumerate(boxes) for p in box for l in p.lines}
        holes = [(f"p{n}t{bi}", l, h) for bi, box in enumerate(boxes) for p in box for l in p.lines for h in l.holes]
        hole_pictures = []
        hole_boxes: list[Rect] = []  # (each picture's box, as written)
        for anchor, line, h in holes:
            rect = line.hole_rect(h)
            # Radical signs and big-operator parts sit off the baseline, in lines of their own.
            h = h + [s for l in lines if l.reason == "math" for s in l.spans if s.rect.intersects(rect.expand(1))]
            h = h + [s for lim in line.limits for s in lim.spans if s not in h
                     and rect.x0 - line.size <= lim.rect.cx <= rect.x1 + line.size]  # a big operator's limits
            rect = union_all([rect] + [s.rect for s in h] + [b for b in self.bars if b.expand(1).intersects(rect)])
            bbox = rect.expand(HOLE_PAD).as_list()
            hole_boxes.append(Rect.of(bbox))
            hole_pictures.append({"id": f"p{n}h{len(hole_pictures)}", "kind": "image", "role": "math",
                                  "bbox": bbox, "spans": [s.id for s in h],
                                  "anchor": anchor})  # grouped with this text element
        self.icon_bullets = [Rect.of(p["bullet"]["bbox"]) for e in elements for p in e["paragraphs"]
                             if p["bullet"] and p["bullet"]["kind"] == "icon"]
        for e in elements:  # icon bullets: pictures grouped with their item
            for p in e["paragraphs"]:
                if p["bullet"] and p["bullet"]["kind"] == "icon":
                    bbox = Rect.of(p["bullet"]["bbox"]).expand(0.5).as_list()
                    hole_boxes.append(Rect.of(bbox))
                    hole_pictures.append({"id": f"p{n}u{len(hole_pictures)}", "kind": "image", "role": "icon",
                                          "bbox": bbox, "spans": p["bullet"]["spans"], "anchor": e["id"]})
                    p["bullet"] = None

        text_spans = {sid for e in elements for sid in e["spans"]}
        self.hole_boxes = hole_boxes
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
                                    "runs": self.runs(par, "", False, None, 0.0),
                                    **({"direction": par.direction} if par.direction else {})}],
                    "spans": [s.id for s in line.spans],
                })

        # Pictures describe themselves with the text they show (alt text in Slides).
        by_id = {s.id: s for s in spans}
        for e in elements:
            if e["kind"] == "image" and e["spans"]:
                # formulas left to right (scripts follow their base); figure labels by rows
                math = e["role"] == "math"

                def order(s: Span) -> tuple[float, float]:
                    return (0, s.rect.x0) if math else (round(s.baseline / 4), s.rect.x0)
                shown = sorted((by_id[i] for i in e["spans"] if i in by_id), key=order)
                words = [math_text(s.font, s.text)[0] if s.info.family == "math" else s.text for s in shown]
                e["alt"] = " ".join(w.strip() for w in words if w.strip() and "�" not in w)[:500]

        chars_total = sum(len(s["text"].strip()) for s in self.raw["spans"])
        chars_native = sum(len(s["text"].strip()) for s in self.raw["spans"] if s["id"] in text_spans)
        # Span ids name page objects (render switches them off, checks look them up): a character
        # the page draws as a rule has none, and is in the text through its runs alone.
        drawn = {s.id for s in spans if s.drawn}
        if drawn:
            for holder in elements + left + theme_texts + [e["bullet"] for e in elements if isinstance(e.get("bullet"), dict)]:
                if isinstance(holder.get("spans"), list):
                    holder["spans"] = [i for i in holder["spans"] if i not in drawn]
        return {
            "page": n, "frame": self.raw["label"], "label": self.raw.get("frame_label"), "size": self.raw["size"],
            "notes": self.raw.get("notes"),
            "elements": elements, "left_in_background": left, "theme_texts": theme_texts,
            "panels": [{"bbox": p.bbox.as_list(), "fill": p.fill, "rounded": p.rounded} for p in self.panels],
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
        for e, p in ((t, q) for t in texts for q in t["paragraphs"] if numbered(q)):
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
    def norm(s: str) -> str:
        return " ".join(s.casefold().split())
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
    """One page's slide; a page the classifier trips over stays a picture as a whole. A page whose
    objects say what they are (adopt's slides.sty marks) is read from its marks (`marked.py`)."""
    from . import marked
    if marked.has_marks(page):
        try:
            return marked.classify_marked(page, body)
        except Exception as e:  # the marks are a hint: the page still reads as before
            print(f"warning: page {page['index'] + 1}: marked read-back failed ({type(e).__name__}: {e}); "
                  f"classified without marks")
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
