"""PageClassifier's figures: picture regions, formula pictures, overlays and icons, and the
drawings that become native diagrams.
"""

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from .classify_model import (
    HOLE_PAD, Line, Paragraph, Rect, Span, cluster_rects, miter_reach, new_line, new_paragraph, overlap,
    polygon_shape, union_all, upright_ellipse,
)
from .classify_paragraphs import ParagraphsMixin
from .classify_text import EQ_NUMBER_RE, card_text, family_of, math_text, span_runs
from .ir import (
    Arrow, BeforeWord, DiagramElement, DiagramLine, Element, ImageElement, Mark, Node, ShapeElement, TemplateKind,
    TextElement,
)
from .raw_types import RawDrawing, RawImage

MAX_PLAIN_RECTANGLES = 32  # more rectangles in one cluster (a QR code, a pixel grid) are a picture

End = Literal["from", "to"]
ENDS: tuple[End, End] = ("from", "to")


@dataclass(frozen=True, kw_only=True)
class DraftNode:
    """A diagram node while `diagram_from` gathers it: its box, the words inside it, how it is
    drawn (`shape` None: a free label), and its corner radius when the drawing says one."""
    rect: Rect
    shape: TemplateKind | None
    spans: list[Span]
    fill: str | None
    stroke: str | None
    width: float | None
    radius: float | None


class FiguresMixin(ParagraphsMixin):
    """Methods of classify.PageClassifier; the state they share is `classify_state.PageState`."""

    def math_pictures(self, lines: list[Line], paragraphs: list[Paragraph], elements: list[Element]) -> list[Element]:
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
                    bullet = p["bullet"]
                    if bullet:  # glyph boxes include ascender space; use their visible core
                        b = Rect.of(bullet["bbox"])
                        blocked.append(Rect(b.x0, b.cy - 0.25 * b.h, b.x1, b.cy + 0.25 * b.h))
        out: list[Element] = []
        taken: set[str] = set()
        clusters = cluster_rects([s.rect for s in spans] + list(self.bars), gap=0.6 * self.body)
        # (clusters whose boxes overlap are one formula: a display \sum between the words of its
        # row is a cluster of its own inside theirs, and became a picture of its glyph's font
        # box, a chevron, under the picture of the rest)
        while len(merged := cluster_rects(clusters, gap=3.0)) < len(clusters):
            clusters = merged
        # (columns of one display on the same rows - a cases' conditions, an aligned
        # right-hand side - a little further apart than glyphs; not an equation number)
        def eqno(c: Rect) -> bool:
            return all(EQ_NUMBER_RE.match(s.text.strip()) for s in spans if c.expand(1.5).contains_rect(s.rect, tol=0.5))
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
            members = [s for s in spans if box.contains_rect(s.rect, tol=0.5) and s.id not in taken]
            taken |= {s.id for s in members}
            # (the glyphs themselves, not the picture's anti-aliasing margin: a display's big
            # operator box starts a hair under the descenders of the paragraph above)
            if not members or any(b.intersects(c.expand(0.3)) for b in blocked):
                continue  # only a stray bar, or tangled with native content: leave it in the background
            if len(members) == 1 and EQ_NUMBER_RE.match(members[0].text.strip()) and members[0].info.family != "math":
                # An equation number beside its equation is plain text.
                par = new_paragraph([new_line(members)], align="right", reason=None)
                out.append(self.text_element([par], f"p{self.raw['index']}eq{len(out)}"))
                continue
            picture: ImageElement = {"id": f"p{self.raw['index']}m{len(out)}", "kind": "image", "role": "math",
                                     "bbox": box.as_list(), "spans": [s.id for s in members]}
            out.append(picture)
        return out

    def figures(self, lines: list[Line], text_elements: list[TextElement]) -> list[Element]:
        """Figure regions (graphics, images and their labels) that can become separate pictures.

        Skipped, so they stay in the background: specks (shadow corners, QED boxes),
        near-full-page artwork, and anything overlapping an editable text box."""
        label_spans = [s for l in lines if l.reason in ("figure", "rotated") for s in l.spans]
        if not self.regions:
            return []
        def ink_rect(e: TextElement) -> Rect:
            # Big text without descenders (a statistic: "92%") ends at its baseline, not a
            # quarter em below it where a card under it may start.
            r = Rect.of(e["bbox"])
            last = e["paragraphs"][-1] if e["paragraphs"] else None
            if last and last["lines"]:
                text = "".join(run["text"] for run in last["runs"])
                depth = 0.25 if any(c in "gjpqyQ,;()[]{}|/@$_" for c in text) else 0.05
                r = Rect(r.x0, r.y0, r.x1, min(r.y1, last["lines"][-1]["baseline"] + depth * last["size"]))
            return r

        text_rects = [ink_rect(e) for e in text_elements]

        def grazed(e: TextElement, t: Rect, c: Rect) -> bool:
            """A figure reaching less than an em into a text box's outline, clear of its lines
            and bullets (a pie's pin label in the margin of the list beside it, between two
            bullets): no text under it. (Deeper in, a figure is over the text.)"""
            if min(t.x1, c.x1) - max(t.x0, c.x0) >= self.body and min(t.y1, c.y1) - max(t.y0, c.y0) >= self.body:
                return False
            ink = [Rect(l["x0"], l["baseline"] - 0.8 * p["size"], l["x1"], l["baseline"] + 0.25 * p["size"])
                   for p in e["paragraphs"] for l in p["lines"]]
            ink += [Rect.of(b["bbox"]) for p in e["paragraphs"] for b in [p["bullet"]] if b and b["bbox"]]
            # (the figure's own pieces there, not its outline: the label reaches under the
            # bullets' column between two of them)
            pieces = [r for r in rects if c.expand(0.1).contains_rect(r, tol=0.5) and r.intersects(t)]
            return bool(ink) and not any(r.intersects(q) for r in ink for q in pieces)

        self.bullet_boxes = [Rect.of(b["bbox"]).expand(1) for e in text_elements for p in e["paragraphs"]
                             for b in [p["bullet"]] if b and b["bbox"]]
        out: list[Element] = []
        # Tables first, from their rules: clustering could merge a table with a picture beside it.
        for group in self.table_rules:
            frame = union_all(r.rect for r in group).expand(1)
            table = self.table_from(frame, label_spans, text_rects, len(out))
            if table:
                out.append(table)
                taken = set(table["spans"])
                label_spans = [s for s in label_spans if s.id not in taken]
        table_frames = [Rect.of(t["bbox"]) for t in out]
        regions = [r for r in self.regions if not any(f.expand(0.5).contains_rect(r, tol=0.5) for f in table_frames)]
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
            if any(h.expand(0.5).contains_rect(c, tol=0.5) for h in self.hole_boxes):
                continue  # the graphic of a hole (a frame around words): in that picture already
            over_text = any(t.intersects(c) and not grazed(e, t, c) for e, t in zip(text_elements, text_rects))
            table = None if over_text or not self.fill_grid(c) else self.table_from(c, label_spans, text_rects, len(out))
            if table:
                out.append(table)  # shaded cells edge to edge: a table, not a diagram of boxes
                continue
            diagram = None if over_text else self.diagram_from(c, label_spans, len(out))
            if diagram and self.splits_cells(diagram, label_spans):
                diagram = None  # a ruled grid table_from could not read: a picture, not merged rows
            if diagram and any(n["text"] for n in diagram["nodes"]):
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
            spans = [s.id for s in label_spans if c.expand(0.5).contains_rect(s.rect, tol=0.5)]
            under_words = any(t.x0 < c.x1 and c.x0 < t.x1 and t.y0 < c.cy <= t.y1 + 0.3 * self.body for t in text_rects)
            if not spans and c.h <= 1.5 and under_words and \
                    not any(c.expand(0.5).intersects(Rect.of(im["bbox"])) for im in self.raw["images"]):
                # Only a hairline (the pieces of an underline under words left in the background):
                # a picture adds nothing, and its box took the words above it off the background
                # while the crop showed only the rule. (A rule with no words on it - a footnote's,
                # above its text - stays a picture: in the background it is ink by native words.)
                continue
            el: ImageElement = {"id": f"p{self.raw['index']}f{len(out)}", "kind": "image", "role": "figure",
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

    def bare_image(self, c: Rect) -> RawImage | None:
        """The raster image a figure region consists of, if it consists of nothing else: one
        graphic, one image covering it, no text drawn in the region."""
        members = [g for g in self.graphics if c.expand(0.5).contains_rect(g, tol=0.5)]
        images = [im for im in self.raw["images"] if Rect.of(im["bbox"]).intersects(c.expand(0.5))]
        if len(members) != 1 or len(images) != 1:
            return None
        r = Rect.of(images[0]["bbox"])
        if not (r.contains_rect(members[0], tol=0.5) and members[0].contains_rect(r, tol=0.5)):
            return None
        if any(Rect.of(s["bbox"]).intersects(c.expand(0.5)) for s in self.raw["spans"]):
            return None
        return images[0]

    def overlay(self, c: Rect, label_spans: list[Span], lines: list[Line], index: int, over_text: bool) -> ImageElement | None:
        """A figure cluster drawn over native text or right at its words (a tikzmark arrow, a
        brace under a phrase, an emphasis ellipse, a callout): a picture of only its own
        drawings and labels on a transparent ground (`drawings`), grouped with the text it
        meets (`anchor`). `marks` are the word edges it meets, with what precedes them on their
        line, so that emit moves and stretches it with the words Slides sets at other widths."""
        box = c.expand(1)
        # (not the frames of holes, nor bullets drawn on the page: those are pictures of their own)
        taken = [h.expand(0.5) for h in self.hole_boxes] + self.bullet_boxes
        drawings = [d for d in self.raw["drawings"] if d["id"] in self.graphic_drawings
                    and box.contains_rect(self.graphic_drawings[d["id"]], tol=0.5)
                    and not any(t.contains_rect(self.graphic_drawings[d["id"]], tol=0.5) for t in taken)]
        if not drawings or any(box.intersects(Rect.of(im["bbox"])) for im in self.raw["images"]):
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
        if not over_text and any(box.contains_rect(s.rect, tol=0.5) and s not in label_spans for l in lines
                                 if id(l) not in self.line_owner for s in l.spans):
            return None  # math set inside a figure: one picture of everything in its box

        def points(d: RawDrawing) -> list[Sequence[float]]:
            out: list[Sequence[float]] = []
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
        def rim_only(d: RawDrawing) -> bool:
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
            def spaced(l: Line) -> bool:
                return any(b.rect.x0 - a.rect.x1 >= l.size for a, b in zip(l.content, l.content[1:]))

            met = [(s, l) for s, l in met if not spaced(l)]
            if not met:
                return None
        labels = [s for s in label_spans if c.expand(0.5).contains_rect(s.rect, tol=0.5)]
        el: ImageElement = {"id": f"p{self.raw['index']}f{index}", "kind": "image", "role": "figure", "overlay": True,
              "bbox": union_all([self.graphic_drawings[d["id"]] for d in drawings] + [s.rect for s in labels]).expand(1.0).as_list(),
              "spans": [s.id for s in labels], "drawings": [d["id"] for d in drawings]}
        if met:
            anchor = Counter(self.line_owner[id(l)][0] for _, l in met).most_common(1)[0][0]
            el["anchor"] = anchor
            el["marks"] = [self.mark(l, x) for s, l in dict.fromkeys(met) if self.line_owner[id(l)] == (anchor, "left")
                           for x in (s.rect.x0, s.rect.x1)]
        return el

    def trim_overlays(self, elements: list[Element]) -> None:
        """Drawings of an overlay that lie within a formula picture (a highlight behind a line
        that became one picture) belong to that picture: the overlay gives them up."""
        boxes = [Rect.of(e["bbox"]) for e in elements if e["kind"] == "image" and e["role"] == "math"]
        by_id = self.spans_by_id
        for el in elements:
            if el["kind"] != "image" or not el.get("overlay"):
                continue
            drawings = [i for i in el.get("drawings", []) if not any(b.contains_rect(self.graphic_drawings[i], tol=0.5) for b in boxes)]
            el["drawings"] = drawings
            if drawings:
                el["bbox"] = union_all([self.graphic_drawings[i] for i in drawings] +
                                       [by_id[i].rect for i in el["spans"]]).expand(1.0).as_list()

    @staticmethod
    def mark(line: Line, x: float) -> Mark:
        """Where a graphic meets a line of text at `x`: the line's style and the words before x,
        as a formula hole run carries them (emit.formula_shifts predicts where x lands in Slides)."""
        main = line.main
        words = [s for s in line.content if s.rect.x1 <= x + 0.5 and s.info.family != "icon"
                 and not any(s in h for h in line.holes)]
        # Holes and em-space gaps before x keep their width in Slides: close them up, or
        # formula_shifts would read each as one huge word space.
        cuts: list[tuple[float, float]] = []
        holes = [line.hole_rect(h) for h in line.holes]
        for r in holes:
            if r.x1 <= x + 0.5:
                cuts.append((r.x0, min([s.rect.x0 for s in words if s.rect.x0 >= r.x1 - 0.5] + [x]) - r.x0))
        stops = sorted([(s.rect.x0, s.rect.x1) for s in words] + [(x, x)])
        for (_, a), (b, _) in zip(stops, stops[1:]):
            if b - a >= line.size and not any(a - 0.5 <= r.x0 < b for r in holes):
                cuts.append((a, max(1, round((b - a - 0.33 * line.size) / line.size)) * line.size))  # (as runs() writes it)
        def closed(v: float) -> float:
            return v - sum(w for c, w in cuts if c < v - 0.5)
        before: list[BeforeWord] = [
            (round(s.rect.w, 2), s.font, family_of(s), s.info.bold, s.info.italic,
             math_text(s.font, s.text)[0] if s.info.family == "math" else s.text, round(closed(s.rect.x0), 2))
            for s in words]
        # (a hole's gap in Slides is HOLE_PAD wider on each side)
        pads = 2 * HOLE_PAD * sum(r.x1 <= x + 0.5 for r in holes)
        return {"x": round(x, 2), "hole_x0": round(closed(x), 2), "pads": pads, "font": main.font, "family": family_of(main), "size": round(line.size, 2),
                "bold": main.info.bold, "italic": main.info.italic, "before": before}

    def icons(self, text_elements: list[TextElement]) -> list[ImageElement]:
        """Small raster images next to text that are not bullets (bibliography icons, inline
        logos): movable pictures. Overlapping parts of one icon are cropped together."""
        bullets = {b.get("image") for e in text_elements for p in e["paragraphs"] for b in [p["bullet"]] if b}
        starts = [Rect(l["x0"], l["baseline"] - p["size"], l["x1"], l["baseline"])
                  for e in text_elements for p in e["paragraphs"] for l in p["lines"]]

        def beside_text(r: Rect) -> bool:
            # right of the icon, or starting on it (a label drawn on a ball)
            return any(-r.w <= s.x0 - r.x1 <= 15 and s.y0 < r.y1 and r.y0 < s.y1 for s in starts)

        rects = [ir for im, ir in self.small_images
                 if im["id"] not in bullets and max(ir.w, ir.h) <= 20 and min(ir.w, ir.h) >= 3
                 and not any(b.contains_rect(ir, tol=0.5) for b in self.icon_bullets)
                 and 0.15 * self.H < ir.cy < 0.88 * self.H and not self.on_edge_artwork(ir)
                 and not any(p.bbox.expand(1).intersects(ir) for p in self.panels)]  # block shadow pieces
        rects = [c for c in cluster_rects(rects, gap=0.0) if beside_text(c)]
        out: list[ImageElement] = []
        for c in rects:
            out.append({"id": f"p{self.raw['index']}ic{len(out)}", "kind": "image", "role": "icon",
                        "bbox": c.expand(0.5).as_list(), "spans": []})
            # A picture as an item label (\item[\includegraphics...]) moves with its item.
            item = next((e["id"] for e in text_elements for p in e["paragraphs"] for l in p["lines"][:1]
                         if 0 <= l["x0"] - c.x1 <= 1.5 * p["size"] and l["baseline"] - p["size"] < c.cy < l["baseline"]), None)
            if item:
                out[-1]["anchor"] = item
        return out

    def specks_on_panels(self, spans: list[Span], elements: list[Element]) -> list[ImageElement]:
        """Small graphics on a block panel that no other element took (a proof's QED box, a
        TikZ mark): the panel becomes a native shape over the background, so they become
        pictures above it (grouped with the block) instead of staying hidden in the background."""
        taken = [Rect.of(e["bbox"]) for e in elements if e["kind"] in ("image", "table", "diagram")]
        taken += [Rect.of(b["bbox"]) for e in elements if e["kind"] == "text"
                  for p in e["paragraphs"] for b in [p["bullet"]] if b and b["bbox"]]
        panels = [p.bbox for p in self.panels if p.fill and not p.image
                  and p.bbox.x0 > 1 and p.bbox.y0 > 1 and p.bbox.x1 < self.W - 1 and p.bbox.y1 < self.H - 1]
        out: list[ImageElement] = []
        for c in (cluster_rects(self.graphics, gap=0.5) if self.graphics and panels else []):
            if max(c.w, c.h) >= 25 or any(t.expand(0.5).intersects(c) for t in taken) or \
                    not any(p.expand(-0.5).contains_rect(c, tol=0.5) for p in panels) or \
                    any(s.rect.intersects(c) for s in spans if s.text.strip()):
                continue
            out.append({"id": f"p{self.raw['index']}k{len(out)}", "kind": "image", "role": "icon",
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
                    if c.expand(0.1).contains_rect(l.rect, tol=0.5):
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
        order = {d["id"]: i for i, d in enumerate(self.raw["drawings"])}
        members = [order[i] for i, r in self.graphic_drawings.items() if c.expand(0.5).contains_rect(r, tol=0.5)]
        if not members:
            return box
        last = max(members)
        # (a band of boxes side by side is panels on the edge, see on_edge_artwork)
        decor = {tuple(r.as_list()) for r in self.decorations}
        panels = {p.id for p in self.panels if not p.image}
        x0, y0, x1, y1 = box.as_list()
        for i, d in enumerate(self.raw["drawings"]):
            r = Rect.of(d["bbox"])
            if i < last or r.h > 0.15 * self.H or r.x1 <= box.x0 or r.x0 >= box.x1 or not (
                    d["id"] in self.decor_ids or d["id"] in panels or tuple(r.as_list()) in decor):
                continue
            if r.y1 >= self.H - 1 and box.y0 + 0.5 * box.h < r.y0 < y1:
                y1 = r.y0
            elif r.y0 <= 1 and y0 < r.y1 < box.y1 - 0.5 * box.h:
                y0 = r.y1
        return Rect(x0, y0, x1, y1)

    def holds_other_text(self, c: Rect, label_spans: list[Span]) -> bool:
        """Text drawn inside a figure cluster that is none of its labels stays in the
        background (a section number in a filled node, read with its heading as one formula):
        native shapes rebuilt from the cluster would be drawn over it and hide it."""
        labels = {s.id for s in label_spans}
        box = c.expand(0.5)

        def centre(r: Rect) -> tuple[float, float]:
            return r.cx, r.cy
        return any(s["id"] not in labels and s["text"].strip() and box.contains(*centre(Rect.of(s["bbox"])))
                   for s in self.raw["spans"])

    def plain_rectangles(self, c: Rect, label_spans: list[Span], index: int) -> list[ShapeElement]:
        """A figure cluster that is only opaque filled rectangles without text (progress bars,
        colour swatches, \\rule): native rectangle shapes."""
        box = c.expand(0.5)
        if any(box.contains_rect(s.rect, tol=0.5) for s in label_spans) or self.holds_other_text(c, label_spans) or \
                any(box.intersects(Rect.of(im["bbox"])) for im in self.raw["images"]):
            return []
        out: list[ShapeElement] = []
        for d in self.raw["drawings"]:
            r = Rect.of(d["bbox"])
            if not box.intersects(r) or d["id"] in self.decor_ids or self.is_decoration(r) or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            points = [p for _, pts in (d.get("path") or []) for p in pts]
            axis_aligned = d["items"] == "re" or (set(d["items"]) == {"l"} and points and all(
                min(abs(x - r.x0), abs(x - r.x1)) < 0.05 and min(abs(y - r.y0), abs(y - r.y1)) < 0.05 for x, y in points))
            if not box.contains_rect(r, tol=0.5) or d["type"] != "f" or not axis_aligned or not d["fill"] \
                    or d.get("fill_opacity", 1.0) < 0.99:
                return []
            out.append({"id": f"p{self.raw['index']}r{index + len(out)}", "kind": "shape", "role": "rule",
                        "bbox": r.as_list(), "fill": d["fill"], "shape": "RECTANGLE", "flip": False,
                        "radius": 0.0, "drawing": d["id"], "spans": []})
        if len(out) > MAX_PLAIN_RECTANGLES:
            # A QR code or a pixel grid (426 modules): a picture, not hundreds of shapes nobody
            # would edit one by one, each its own request.
            return []
        # The track of a progress bar runs across the page like a decoration hairline: it goes
        # along (below the bar) when it has exactly the bar's height and contains it.
        for d in self.raw["drawings"]:
            r = Rect.of(d["bbox"])
            if d["type"] == "f" and d["fill"] and d.get("fill_opacity", 1.0) >= 0.99 and d["id"] not in self.decor_ids \
                    and r.x0 > 1 and r.x1 < self.W - 1 and all(o.get("drawing") != d["id"] for o in out) \
                    and any(abs(r.y0 - o["bbox"][1]) < 0.1 and abs(r.y1 - o["bbox"][3]) < 0.1
                            and r.x0 <= o["bbox"][0] + 0.1 and r.x1 >= o["bbox"][2] - 0.1 for o in out):
                out.insert(0, {"id": f"p{self.raw['index']}r{index + len(out)}", "kind": "shape", "role": "rule",
                               "bbox": r.as_list(), "fill": d["fill"], "shape": "RECTANGLE", "flip": False,
                               "radius": 0.0, "drawing": d["id"], "spans": []})
        return out

    @staticmethod
    def closed_frames(nodes: list[DraftNode], lines: list[DiagramLine]) -> None:
        """Four stroked lines closing a rectangle (\\fbox and \\fcolorbox draw their frame side by
        side) become one rectangle node; a filled rectangle right inside the frame (the
        \\fcolorbox background) takes it as its outline."""
        def extent(ln: DiagramLine, k: int) -> list[float]:
            return sorted((ln["from"][k], ln["to"][k]))
        horizontal = [l for l in lines if "via" not in l and abs(l["from"][1] - l["to"][1]) < 0.05]
        vertical = [l for l in lines if "via" not in l and abs(l["from"][0] - l["to"][0]) < 0.05]

        def near(a: Sequence[float], b: Sequence[float], tol: float) -> bool:
            return all(abs(p - q) <= tol for p, q in zip(a, b))
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
                inner = next((i for i, n in enumerate(nodes) if n.shape == "RECTANGLE" and n.stroke is None and
                              rect.expand(tol).contains_rect(n.rect, tol=0.5) and n.rect.w >= rect.w - 2 * tol
                              and n.rect.h >= rect.h - 2 * tol), None)
                if inner is not None:
                    nodes[inner] = replace(nodes[inner], rect=rect, stroke=top["stroke"], width=top["width"])
                else:
                    nodes.append(DraftNode(rect=rect, shape="RECTANGLE", spans=[], fill=None,
                                           stroke=top["stroke"], width=top["width"], radius=None))

    def diagram_from(self, c: Rect, label_spans: list[Span], index: int) -> DiagramElement | None:
        """A figure cluster made only of simple nodes (rectangles, rounded rectangles, ellipses)
        with their text inside, straight lines and arrow tips: rebuilt from native Slides
        shapes and lines. Anything else (curves, images, math, loose labels) keeps it a picture."""
        box = c.expand(0.5)
        if any(box.contains_rect(Rect.of(im["bbox"]), tol=0.5) for im in self.raw["images"]) or self.holds_other_text(c, label_spans):
            return None
        nodes: list[DraftNode] = []
        lines: list[DiagramLine] = []
        tips: list[tuple[Rect, Arrow, list[list[float]], float]] = []
        for d in self.raw["drawings"]:
            r = Rect.of(d["bbox"])
            if not box.contains_rect(r, tol=0.5) or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            path = d.get("path")
            if path is None:
                return None
            if d.get("soft_mask") or d.get("fill_opacity", 1.0) < 0.99 or d.get("stroke_opacity", 1.0) < 0.99:
                # A see-through node or line (opacity=0.3 on the steps still to come, a
                # multiplied fill): native shapes came out opaque, the dimmed step drawn in full.
                return None
            ops = "".join(op for op, _ in path)
            shapes: dict[str, TemplateKind | None] = {
                "re": "RECTANGLE", "lclclclc": "ROUND_RECTANGLE", "clclclcl": "ROUND_RECTANGLE",
                "cccc": "ELLIPSE" if upright_ellipse(path, r) else None}
            shape = shapes.get(ops)
            points = [p for _, pts in path for p in pts]
            if shape is None and max(r.w, r.h) > 6 and ops in ("llll", "lll") and "f" in d["type"] + "f":
                shape = polygon_shape(points, r)  # decision diamonds, triangles
            if shape and r.w > 3 and r.h > 3:
                corners = d.get("corners")
                nodes.append(DraftNode(rect=r, shape=shape, spans=[],
                                       fill=d["fill"] if "f" in d["type"] else None,
                                       stroke=d["stroke"] if "s" in d["type"] else None, width=d["width"],
                                       # rounded corners=3pt: without it the node got Slides' default rounding
                                       radius=max(corners.values()) if shape == "ROUND_RECTANGLE" and corners else None))
            elif d["type"] == "s" and set(ops) == {"l"} and max(r.w, r.h) > 6:
                segments = [(tuple(a), tuple(b)) for _, (a, b) in path]
                stroke, width = d["stroke"] or "#000000", d["width"] or 0.4
                (p0, p1), (p1b, p2) = segments[0], segments[-1]
                if len(segments) == 2 and math.dist(p1, p1b) < 0.05 and (abs(p0[0] - p1[0]) < 0.05) != (abs(p0[1] - p1[1]) < 0.05) \
                        and (abs(p1[0] - p2[0]) < 0.05) != (abs(p1[1] - p2[1]) < 0.05) \
                        and (abs(p0[0] - p1[0]) < 0.05) != (abs(p1[0] - p2[0]) < 0.05):
                    # An orthogonal connector (|- or -|): one elbow line, vertical first. A -| one
                    # is written from its other end: Slides drew bentConnector3 at adj 1 (turn at
                    # the end) with its turn halfway, three segments; adj 0 draws |- right.
                    if abs(p0[0] - p1[0]) >= 0.05:
                        p0, p2 = p2, p0
                    lines.append({"from": list(p0), "via": list(p1), "to": list(p2), "bend": "vh",
                                  "stroke": stroke, "width": width, "arrow_from": None, "arrow_to": None})
                else:  # straight lines, and other polylines one segment at a time
                    for (x1, y1), (x2, y2) in segments:
                        lines.append({"from": [x1, y1], "to": [x2, y2],
                                      "stroke": stroke, "width": width, "arrow_from": None, "arrow_to": None})
            elif max(r.w, r.h) <= 6 and set(ops) <= {"c", "l"}:
                # Arrow heads are small separate paths: stroked (->), filled triangles (latex)
                # or filled concave quadrilaterals (stealth).
                head: Arrow
                if d["type"] == "s":
                    head = "OPEN_ARROW"
                else:
                    head = "STEALTH_ARROW" if ops == "llll" else "FILL_ARROW"
                points = [p for _, pts in path for p in pts]
                tips.append((r, head, points, d["width"] if "s" in d["type"] and d["width"] else 0.0))
            else:
                return None
        self.closed_frames(nodes, lines)
        if not nodes:
            return None
        # Shapes that cross each other (a Venn diagram): its words are placed by region - the
        # lens, one circle's own part - and a node's text is set centred in all of it.
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                ra, rb = a.rect, b.rect
                if overlap(ra, rb) > 0.05 * min(ra.w * ra.h, rb.w * rb.h) and \
                        not ra.contains_rect(rb, tol=0.5) and not rb.contains_rect(ra, tol=0.5):
                    return None
        for tip, head, points, outline in tips:
            ends = [(ln, end) for ln in lines for end in ENDS if tip.expand(1).contains(*ln[end])]
            if not ends:
                return None
            ln, end = ends[0]
            if end == "from":
                ln["arrow_from"] = head
            else:
                ln["arrow_to"] = head
            if head != "OPEN_ARROW":
                # TikZ stops the line where a filled head begins; Slides draws the head at the
                # line's end, so extend the line to the tip.
                other = ln.get("via") or (ln["to"] if end == "from" else ln["from"])
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

        spans = [s for s in label_spans if box.contains_rect(s.rect, tol=0.5)]
        # A label with a script (R_s, C_dl in a circuit) is a formula: set as one run of plain
        # text it read "Rs", and a free label ran into the next ("ct Z").
        if any(b is not a and -0.05 * a.size <= b.rect.x0 - a.rect.x1 <= 0.15 * a.size and b.size < 0.85 * a.size
               and 0.1 * a.size < abs(b.baseline - a.baseline) < 0.6 * a.size for a in spans for b in spans):
            return None
        free: list[Span] = []

        def area(n: DraftNode) -> float:
            return n.rect.w * n.rect.h
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
            owners = [n for n in nodes if n.rect.contains(s.rect.cx, s.rect.cy)]
            if owners:
                smallest = min(map(area, owners))
                [n for n in owners if area(n) <= 1.02 * smallest + 0.01][-1].spans.append(s)
            else:
                free.append(s)  # edge labels and captions: a text box in the group
        if sum(n.shape is not None and not n.spans for n in nodes) > MAX_PLAIN_RECTANGLES:
            return None  # a QR code, a pixel grid: modules, not nodes
        # Free labels on one baseline and close together are one label. (Close on both sides:
        # two edge labels whose baselines round apart sort right to left, and the one-sided gap
        # joined them across the node between - "connect SYN+ACK" over two arrows.)
        for s in sorted(free, key=lambda s: (round(s.baseline), s.rect.x0)):
            last = nodes[-1] if nodes and nodes[-1].shape is None else None
            if last and abs(last.spans[-1].baseline - s.baseline) <= 0.3 * s.size and \
                    -0.3 * s.size <= s.rect.x0 - last.spans[-1].rect.x1 <= 0.5 * s.size:
                last.spans.append(s)
                nodes[-1] = replace(last, rect=last.rect.union(s.rect))
            else:
                nodes.append(DraftNode(rect=s.rect, shape=None, spans=[s], fill=None, stroke=None, width=None,
                                       radius=None))

        out_nodes: list[Node] = []
        for n in nodes:
            rows: list[list[Span]] = []
            for s in sorted(n.spans, key=lambda s: (s.baseline, s.rect.x0)):
                if rows and abs(s.baseline - rows[-1][0].baseline) <= 0.5 * s.size:
                    rows[-1].append(s)
                else:
                    rows.append([s])
            out_nodes.append({
                "bbox": n.rect.as_list(), "shape": n.shape, "fill": n.fill, "stroke": n.stroke,
                "width": n.width, "paragraphs": [span_runs(sorted(row, key=lambda s: s.rect.x0)) for row in rows],
                "baselines": [round(row[0].baseline, 2) for row in rows],
                "label_w": round(max((max(s.rect.x1 for s in row) - min(s.rect.x0 for s in row) for row in rows), default=0.0), 2),
                "text": card_text(n.rect, rows) if n.shape else None,
                **({"radius": n.radius} if n.radius is not None else {}),
            })
        return {"id": f"p{self.raw['index']}dg{index}", "kind": "diagram", "role": "figure",
                "bbox": c.expand(1.0).as_list(), "nodes": out_nodes, "lines": lines,
                "spans": [s.id for s in spans]}
