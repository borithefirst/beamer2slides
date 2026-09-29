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

What it answers is deck.json as `ir.py` types it (`ir.Deck`, `ir.Slide`): a producer writing an
element emit cannot take fails the checker. `ir.deck_json` is the same deck as JSON.
"""

import re
from dataclasses import replace

from . import ir
from .classify_model import HOLE_PAD, Line, Paragraph, Rect, Span, union_all
from .classify_model import box_outline, upright_ellipse  # noqa: F401 (callers take these from here)
from .classify_reasons import ReasonsMixin
from .classify_text import body_size, math_text
from .classify_text import (  # noqa: F401 (callers take these from here)
    bullet_shape, card_text, compose_accents, first_word_width, gap_between, hyphen_cut, label_of, math_family,
    math_pieces, negate, span_runs, type3_symbol,
)
from .raw_types import RawDoc, RawPage, RawSpan

FRAME_COUNTER_RE =re.compile(r"^\d{1,4}( ?/ ?\d{1,4})?$")


class PageClassifier(ReasonsMixin):
    """One page's classification. Its state (the page, its panels, regions and bars, what
    decorates which span) is declared in `classify_state.PageState` and found in `classify`; the
    mixins' methods, inherited one from the next, share it."""

    def __init__(self, page: RawPage, body: float) -> None:
        # (a page extract made, or one `raw_types.parse_page` read; a test's hand-built page holds
        # only what its method reads)
        super().__init__(page, body)
        self.raw_spans: dict[str, RawSpan] = {s["id"]: s for s in self.raw["spans"]}

    # -- output -----------------------------------------------------------------

    def rotated_texts(self, lines: list[Line], used: set[str]) -> list[ir.TextElement]:
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
        out: list[ir.TextElement] = []
        for row in rows:
            up = self.page_dir(row[0])[1] < 0
            # page -> text frame: reading upwards, x = -y and y = x; downwards, x = y and y = -x
            turned = [replace(s, rect=Rect(-s.rect.y1, s.rect.x0, -s.rect.y0, s.rect.x1) if up else
                              Rect(s.rect.y0, -s.rect.x1, s.rect.y1, -s.rect.x0),
                              baseline=self.page_origin(s)[0] * (1 if up else -1), horizontal=True) for s in row]
            el = self.text_element([Paragraph([Line(turned)])], f"p{self.raw['index']}rt{len(out)}")
            rotated: ir.TextElement = {**el, "bbox": union_all(s.rect for s in row).as_list(),
                                       "panel": self.panel_of(union_all(s.rect for s in row)),
                                       "rotation": -90 if up else 90}
            out.append(rotated)
        return out

    def page_dir(self, s: Span) -> tuple[float, float]:
        dx, dy = self.raw_spans[s.id]["dir"]
        return dx, dy

    def page_origin(self, s: Span) -> tuple[float, float]:
        x, y = self.raw_spans[s.id]["origin"]
        return x, y

    def classify(self) -> ir.Slide:
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
        texts = [self.text_element(box, f"p{n}t{bi}") for bi, box in enumerate(boxes)]
        self.line_owner = {id(l): (f"p{n}t{bi}", p.align) for bi, box in enumerate(boxes) for p in box for l in p.lines}
        holes = [(f"p{n}t{bi}", l, h) for bi, box in enumerate(boxes) for p in box for l in p.lines for h in l.holes]
        hole_pictures: list[ir.ImageElement] = []
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
        # Icon bullets: pictures grouped with their item (`text_element` wrote no bullet for them).
        icon_bullets = [(f"p{n}t{bi}", b) for bi, box in enumerate(boxes) for p in box
                        for b in [p.bullet] if b is not None and b["kind"] == "icon"]
        self.icon_bullets = [Rect.of(b["bbox"]) for _, b in icon_bullets]
        for anchor, b in icon_bullets:
            bbox = Rect.of(b["bbox"]).expand(0.5).as_list()
            hole_boxes.append(Rect.of(bbox))
            hole_pictures.append({"id": f"p{n}u{len(hole_pictures)}", "kind": "image", "role": "icon",
                                  "bbox": bbox, "spans": b["spans"], "anchor": anchor})

        text_spans = {sid for e in texts for sid in e["spans"]}
        self.hole_boxes = hole_boxes
        # (pictures below text)
        elements: list[ir.Element] = [*self.figures(lines, texts), *self.icons(texts), *hole_pictures, *plain_tables,
                                      *texts]
        elements += self.rotated_texts(lines, {sid for e in elements for sid in e["spans"]})
        text_spans |= {sid for e in elements if e["kind"] == "table" for sid in e["spans"]}
        elements = [*self.math_pictures(lines, paragraphs, elements), *elements]
        self.trim_overlays(elements)
        # (an overlay whose drawings all went to formula pictures is no picture)
        elements = [e for e in elements if not (e["kind"] == "image" and e.get("overlay") and not e.get("drawings"))]
        text_spans |={sid for e in elements if e["kind"] == "text" for sid in e["spans"]}  # equation numbers
        elements = [*self.specks_on_panels(spans, elements), *elements]
        shapes = self.shapes(lines, elements)
        self.blocks(shapes)
        elements = [*shapes, *elements]   # shapes below pictures

        used = {sid for e in elements for sid in e["spans"]}
        paragraph_reason = {id(l): p.reason for p in paragraphs for l in p.lines}
        by_reason: dict[str, list[Span]] = {}
        for line in lines:
            for s in line.spans:
                if s.id not in used:
                    reason = line.reason or paragraph_reason.get(id(line)) or "unsure"
                    by_reason.setdefault(reason, []).append(s)
        left: list[ir.LeftInBackground] = [{"reason": r, "spans": [s.id for s in ss], "bboxes": [s.rect.as_list() for s in ss]}
                                           for r, ss in by_reason.items()]

        # Theme text as ready-made text elements: text that is the same on every slide moves to
        # the slide layout (see promote_theme_text), the rest stays in the background.
        theme_texts: list[ir.ThemeText] = []
        for line in lines:
            if line.reason == "theme" and all(s.id not in used for s in line.spans):
                par = Paragraph([line], align="left")
                direction = par.direction
                theme_texts.append({
                    "kind": "text", "role": "layout", "bbox": line.rect.as_list(), "panel": None, "code": False,
                    # Colour is part of the identity: section navigation highlights the current
                    # section by colour, which must not be frozen onto the layout.
                    "key": (line.text, round(line.rect.x0), round(line.baseline), "".join(s.color for s in line.spans)),
                    "chars": sum(len(s.text.strip()) for s in line.spans),
                    "paragraphs": [{"align": "left", "level": 0, "bullet": None, "size": round(line.size, 2),
                                    "text_x0": round(line.x0, 2),
                                    "lines": [{"baseline": round(line.baseline, 2), "x0": round(line.x0, 2),
                                               "x1": round(line.x1, 2)}],
                                    "runs": self.runs(par, "", False, None, 0.0),
                                    **({"direction": direction} if direction else {})}],
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
            for e in elements:
                e["spans"] = [i for i in e["spans"] if i not in drawn]
            for kept in left:
                kept["spans"] = [i for i in kept["spans"] if i not in drawn]
            for t in theme_texts:
                t["spans"] = [i for i in t["spans"] if i not in drawn]
        return {
            "page": n, "frame": self.raw["label"], "label": self.raw.get("frame_label"), "size": self.raw["size"],
            "notes": self.raw.get("notes"),
            "elements": elements, "left_in_background": left, "theme_texts": theme_texts,
            "panels": [{"bbox": p.bbox.as_list(), "fill": p.fill, "rounded": p.rounded} for p in self.panels],
            "figure_regions": [r.as_list() for r in self.regions],
            "stats": {"chars": chars_total, "chars_native": chars_native},
        }


def ball_number(bullet: ir.Bullet, label: ir.Label) -> str:
    """The number a ball shows. A ball template draws the whole label on a ball sized for a
    letter or two: \\begin{enumerate}[(a)] puts white parentheses at the ball's edges, over its
    white rim and the page, where nobody sees them (r3_dense_v2 s7). In Slides' wider font
    they came out as white crescents cutting the ball. Only parentheses at the edge are unseen:
    a label narrower than the ball keeps them on its dark face, where they show ('(i)', '(ii)'
    on a 10 pt ball, r3_dense_v3 s7, while '(iii)' reaches the rim). The label is centred on
    the ball, so its half width is the ball's centre less its left edge."""
    text = bullet["text"]
    white = all(int(label["color"][i:i + 2], 16) >= 0xE0 for i in (1, 3, 5))
    if bullet["kind"] == "image" and white and len(text) > 2 and text[0] == "(" and text[-1] == ")":
        x0, _, x1, _ = bullet["bbox"]
        half = (x0 + x1) / 2 - label["x0"]
        if half >= BALL_RIM * (x1 - x0) / 2:
            return text[1:-1]
    return text


BALL_RIM = 0.95  # of a ball's radius: the bright rim of beamer's ball, where white ink is unseen


def literal_list_numbers(slides: list[ir.Slide]) -> None:
    """Slides numbers each list from 1, and the API cannot set a start number. A numbered
    item whose number Slides would get wrong (a table of contents split into one box per
    section, a list continued after a paragraph) keeps its number as literal text with a tab.
    A ball or box under the number becomes a picture grouped with the text, so it moves along,
    and the number goes on the picture (`number`) as its own centred text box: on the item's
    line it would sit on the text baseline, off the middle of the ball."""
    def numbered(p: ir.Paragraph) -> bool:
        b = p["bullet"]
        return b is not None and (b["kind"] == "number" or (b["kind"] == "image" and bool(b["text"])))

    def on_graphic(p: ir.Paragraph) -> bool:
        b = p["bullet"]
        return numbered(p) and b is not None and (b["kind"] == "image" or bool(b.get("patch")))

    def kind(p: ir.Paragraph) -> str | None:
        """What emit bullets a paragraph with: one createParagraphBullets per run of paragraphs
        of one preset (emit.bullet_preset), each run a list of its own that Slides numbers
        from 1. Numbers with a parenthesis have a preset of their own; every glyph another."""
        b = p["bullet"]
        if b is None:
            return None
        return ("number)" if ")" in b["text"] else "number") if numbered(p) else "glyph"

    def misnumbered(e: ir.TextElement) -> bool:
        expected: dict[int, int] = {}
        previous = None
        for p in e["paragraphs"]:
            if kind(p) != previous:
                # The next list in Slides starts again at 1: after a paragraph, and after the
                # glyph items nested in a numbered one ('1. a . b 2.' came out 1, 1, 2, r1_ml_v1 s3).
                expected.clear()
                previous = kind(p)
            b = p["bullet"]
            if b is None or not numbered(p):
                continue
            for deeper in [k for k in expected if k > p["level"]]:
                del expected[deeper]
            label = b["text"]
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
        pictures: list[ir.ImageElement] = []
        for e, p in ((t, q) for t in texts for q in t["paragraphs"] if numbered(q)):
            b = p["bullet"]
            label = None if b is None else ir.bullet_label(b)
            if b is None or not label or not p["runs"]:
                continue
            if on_graphic(p):
                x0, y0, x1, y1 = b["bbox"]
                number: ir.Number = {"text": ball_number(b, label), "center": [(x0 + x1) / 2, (y0 + y1) / 2],
                                     "height": y1 - y0, "x0": label["x0"], "baseline": label["baseline"],
                                     "font": label["font"], "family": label["family"], "size": label["size"],
                                     "bold": label["bold"], "italic": label["italic"], "color": label["color"]}
                pictures.append({"id": f"{e['id']}b{len(pictures)}", "kind": "image", "role": "icon",
                                 "bbox": [x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5], "spans": [], "anchor": e["id"],
                                 "number": number})
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


def mark_title_page(slides: list[ir.Slide], doc_title: str) -> None:
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


def promote_theme_text(slides: list[ir.Slide]) -> list[ir.ThemeText]:
    """Theme text (header/footer lines) identical in content and position on every slide
    - author, short title, institute, date - becomes text on the slide layouts, edited once
    for the whole deck. Slide numbers and section navigation differ per slide and stay put."""
    if len(slides) < 2:
        for s in slides:
            s["on_layout"] = []
        return []
    keys = [{t["key"]: t for t in s["theme_texts"]} for s in slides]
    common = set(keys[0]).intersection(*keys[1:])
    for slide, by_key in zip(slides, keys):
        moved = {sid for k in common for sid in by_key[k]["spans"]}
        slide["on_layout"] = sorted(moved)
        # Frame counters ("3 / 9") differ per slide: a small text box on the slide. The rest
        # of the theme then often renders identically on every slide (one shared background).
        counters = [t for k, t in by_key.items() if k not in common and FRAME_COUNTER_RE.match(t["key"][0])]
        for j, t in enumerate(counters):
            counter: ir.TextElement = {"kind": "text", "role": "footer", "bbox": t["bbox"], "panel": t["panel"],
                                       "code": t["code"], "paragraphs": t["paragraphs"], "spans": t["spans"],
                                       "id": f"p{slide['page']}n{j}", "strokes": []}
            slide["elements"].append(counter)
            moved |= set(t["spans"])
        for left in slide["left_in_background"]:
            keep = [i for i, sid in enumerate(left["spans"]) if sid not in moved]
            left["spans"] = [left["spans"][i] for i in keep]
            left["bboxes"] = [left["bboxes"][i] for i in keep]
        slide["left_in_background"] = [l for l in slide["left_in_background"] if l["spans"]]
        slide["stats"]["chars_native"] += sum(by_key[k]["chars"] for k in common) + sum(t["chars"] for t in counters)
    return [keys[0][k] for k in sorted(common, key=lambda k: (k[2], k[1]))]


def mark_big_headings(slides: list[ir.Slide], body: float) -> None:
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


def classify_page(page: RawPage, body: float) -> ir.Slide:
    """One page's slide; a page the classifier trips over stays a picture as a whole. A page whose
    objects say what they are (adopt's slides.sty marks) is read from its marks (`marked.py`),
    taken as a slide once it holds what `ir.Slide` says (`ir.slide_of`)."""
    from . import marked
    if marked.has_marks(page):
        try:
            return ir.slide_of(marked.classify_marked(page, body))
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


def classify(raw: RawDoc) -> ir.Deck:
    """deck.json for a raw.json, as `ir.Deck` (`ir.deck_json` writes it)."""
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
