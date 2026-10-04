"""PageClassifier's tables: ruled, shaded and rule-less grids of text that become native tables."""

import re
import statistics
from dataclasses import dataclass, replace
from typing import Literal

from .classify_model import Line, Rect, Span, union_all
from .classify_state import Fill, PageState, Rule
from .classify_text import cell_runs, is_mono, justified_cells, span_runs
from .ir import Align, Border, Column, DiagramElement, Merge, TableElement, element_json
from .raw_types import RawSpan
from .typing_compat import assert_never

Chunk = tuple[int, int, list[Span]]
"""A table's chunk: (row index in the grid's rows, rows it spans, its words)."""


@dataclass(frozen=True, kw_only=True)
class Ruling:
    """What makes a figure cluster a table (`TablesMixin.table_ruling`): the region its words
    are taken from (the cluster, grown along side rules that run on past its rules:
    `sides_run_on`), its frame, its horizontal and vertical rules and its cell shading."""
    reach: Rect
    frame: Rect
    horizontal: list[Rule]
    vertical: list[Rule]
    fills: list[Fill]


class TablesMixin(PageState):
    """PageClassifier's tables (the first of its mixins: they call no other)."""

    def table_hairlines(self) -> set[str]:
        """Rules of a table wider than half the page, which `is_decoration` would take for theme
        hairlines: two or more rules of one extent, touching no page edge, with rows of text
        between them (or between side rules running on past them: `sides_run_on`) - at least two
        rows, one of them cells set more than an em apart - and no text running out past their
        ends. (A \\centering booktabs table in a 4:3 frame is often
        0.55-0.7 of the page wide; left as decoration its rules stayed in the background and its
        cells became text boxes that overlapped and reflowed across columns.)"""
        rules: dict[tuple[int, int], list[tuple[str, Rect]]] = {}
        for d in self.raw["drawings"]:
            r = Rect.of(d["bbox"])
            # (an overfull table's rules start at its margin and run off the page's right edge,
            # where TeX lets an overfull line go: r2_tables_v2 slide 6, r2_tables_v3 slide 2 fell
            # apart into free text; theme hairlines touch the left edge or both)
            if d["id"] in self.decor_ids or r.w < 0.5 * self.W or \
                    r.x0 <= 1 or r.y0 <= 1 or r.y1 >= self.H - 1:
                continue
            if (d["type"] == "s" and d["items"] == "l" and r.h <= 1.0) or \
                    (d["type"] == "f" and d["items"] == "re" and r.h <= 1.5):
                rules.setdefault((round(r.x0), round(r.x1)), []).append((d["id"], r))
        out: set[str] = set()
        cells = [Rect.of(d["bbox"]) for d in self.raw["drawings"] if d["type"] == "f" and d["items"] == "re"
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
            # (a longtable's ruled head over rows between side rules running on: its rows count)
            run_on = self.sides_run_on(Rect(x0, y0, x1, y1))
            y0, y1 = run_on.y0, run_on.y1
            inside = [s for s in self.raw["spans"] if s["text"].strip() and y0 < (s["bbox"][1] + s["bbox"][3]) / 2 < y1
                      and s["bbox"][0] < x1 and x0 < s["bbox"][2]]
            if not inside or any(s["bbox"][0] < x0 - 1 or s["bbox"][2] > x1 + 1 for s in inside):
                continue
            rows: list[list[RawSpan]] = []
            for s in sorted(inside, key=lambda s: s["origin"][1]):
                if rows and abs(rows[-1][0]["origin"][1] - s["origin"][1]) <= 0.3 * s["size"]:
                    rows[-1].append(s)
                else:
                    rows.append([s])
            def cells_apart(row: list[RawSpan]) -> bool:
                row = sorted(row, key=lambda s: s["bbox"][0])
                return any(b["bbox"][0] - a["bbox"][2] >= max(a["size"], b["size"]) for a, b in zip(row, row[1:]))
            if len(rows) >= 2 and any(map(cells_apart, rows)):
                out |= {i for i, _ in group}
        return out

    def fill_grid(self, c: Rect) -> list[Rect]:
        """Opaque rectangles tiling a grid inside c, edge to edge: the \\cellcolor / \\rowcolor
        shading of a table that has fewer than two full rules (a heatmap, \\rowcolors stripes).
        Two or more rows and columns of them; empty when there is no such grid."""
        box = c.expand(0.5)
        fills = [Rect.of(d["bbox"]) for d in self.raw["drawings"]
                 if d["type"] == "f" and d["items"] == "re" and d.get("fill_opacity", 1.0) >= 0.99
                 and d["id"] not in self.decor_ids and box.contains_rect(Rect.of(d["bbox"]), tol=0.5)
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
    def splits_cells(diagram: DiagramElement, label_spans: list[Span]) -> bool:
        """A diagram node holding words on both sides of a vertical line that crosses it: the
        cells of a ruled table row (or the fields of a record node), which one node sets as one
        paragraph, the words run together across the rules."""
        verticals = [ln for ln in diagram["lines"] if "via" not in ln and abs(ln["from"][0] - ln["to"][0]) < 0.05]
        for n in diagram["nodes"]:
            if not n["shape"]:
                continue
            r = Rect.of(n["bbox"])
            words = [s for s in label_spans if r.contains_rect(s.rect, tol=0.5)]
            for ln in verticals:
                x, (y0, y1) = ln["from"][0], sorted((ln["from"][1], ln["to"][1]))
                if r.x0 + 1 < x < r.x1 - 1 and any(y0 <= s.rect.cy <= y1 and s.rect.x1 <= x for s in words) and \
                        any(y0 <= s.rect.cy <= y1 and s.rect.x0 >= x for s in words):
                    return True
        return False

    def table_from(self, c: Rect, label_spans: list[Span], text_rects: list[Rect], index: int) -> TableElement | None:
        """A figure cluster that is really a plain table: text framed by horizontal rules of
        equal extent and nothing else. Returns a native table element, or None.

        In steps: the rules and shading that frame it (`table_ruling`); its words by row, with
        its \\multirow rows and wrapped cells (`TableRows`), and the columns their chunks fall
        into; cells and merges (`place_cells`) and each column's alignment (`column_info`); a
        wrapped cell's lines joined into one cell (`TableCells.join_wrapped`); borders
        (`table_borders`) and row heights (`row_heights`); and last whether Slides can set the
        table where it stands."""
        ruling = self.table_ruling(c)
        if ruling is None:
            return None
        c, frame, horizontal, vertical, fills = ruling.reach, ruling.frame, ruling.horizontal, ruling.vertical, ruling.fills
        box = c.expand(0.5)
        spans = sorted((s for s in label_spans if box.contains_rect(s.rect, tol=0.5)), key=lambda s: s.baseline)
        if not spans or any(not s.horizontal or s.font.upper().startswith("CMEX") or "�" in s.text for s in spans) or \
                any(box.contains_rect(b, tol=0.5) for b in self.bars):  # big operators, fractions: keep the picture
            return None
        rows = TableRows(spans, vertical)
        if any(i - 1 in rows.between for i in rows.between) or numbered_listing(rows.rows):
            return None
        size = rows.size
        rows.find_wrapped()
        items, columns = rows.chunks()
        if not columns:
            return None
        if any(f.rect.x0 < frame.x0 - 1.5 or f.rect.x1 > frame.x1 + 1.5 for f in fills):
            return None  # shading beyond the table: a coloured box around it
        bounds = column_bounds(frame, columns, vertical, fills)
        placed = place_cells(items, bounds, rows.grid_rows())
        if placed is None:
            return None
        n_cols = len(columns)
        col_info = column_info(columns, placed)
        align_merges(placed, col_info)
        cells = placed.join_wrapped({sum(1 for j in range(i) if j not in rows.between) for i in rows.continued})
        n_rows = len(cells.rows)
        baselines = [line_base(row) for row in cells.rows]
        ruled = table_borders(horizontal, vertical, frame, bounds, columns, baselines, size)
        if ruled is None:
            return None
        rules, borders = ruled
        heights = row_heights(baselines, cells.last_line, cells.row_lines, size)
        bottom = slides_bottom(baselines, heights, cells.row_lines, size, self.W)
        grown = Rect(frame.x0, frame.y1, frame.x1, bottom)
        if bottom > self.H - 2 or any(t.intersects(grown) for t in text_rects) or \
                any(reg.intersects(grown) and not c.expand(0.5).contains_rect(reg, tol=0.5) for reg in self.regions):
            return None

        cell_text = [[cell_runs(lines) for lines in row] for row in cells.cell_lines]
        page_color = next((d["fill"].lower() for d in self.raw["drawings"] if d["type"] == "f" and d["fill"]
                           and Rect.of(d["bbox"]).w * Rect.of(d["bbox"]).h >= 0.95 * self.W * self.H), "#ffffff")
        # [row, col, where each line after the first starts in the cell's text]: emit makes the
        # column wide enough for every line of the PDF, a hyphenated word whole.
        wrapped = [(r, cc, starts) for r, row in enumerate(cell_text) for cc, (_, starts) in enumerate(row) if starts]
        set_justified = {cc for cc in range(n_cols) if col_info[cc]["align"] == "left" and justified_cells(
            [cells.cell_lines[r][c2] for r, c2, _ in wrapped if c2 == cc], columns[cc][1])}
        justified = [(r, cc) for r, cc, _ in wrapped if cc in set_justified]
        bands = row_bands([f for f in fills if f.color.lower() != page_color], baselines)
        merges, extent_of = cells.merges, cells.extent_of
        table: TableElement = {
            "id": f"p{self.raw['index']}tab{index}", "kind": "table", "role": "table",
            "bbox": c.expand(1.0).as_list(), "frame": frame.as_list(), "size": round(size, 2),
            "row_baselines": [round(b, 2) for b in baselines],
            "row_heights": [round(p, 2) for p in heights],
            **({"row_lines": cells.row_lines, "wrapped": wrapped} if wrapped else {}),
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
            **({"merge_x": [(round(extent_of[k][0], 2), round(extent_of[k][1], 2)) for k in range(len(merges))]}
               if merges else {}),
            "rules": [{"row": min(k, n_rows - 1), "position": "TOP" if k < n_rows else "BOTTOM",
                       "color": r.color, "weight": round(r.weight, 2), "y": round(r.rect.cy, 2)}
                      for r in rules for k in [row_boundary(baselines, r.rect.cy)]],
            "borders": borders,
            # Shading per cell: the rows whose baseline and the columns whose middle it covers.
            # (white \rowcolors stripes on an off-white page show: only the page's own colour
            # is left out)
            "fills": [{"row": rr, "col": cc, "color": f.color}
                      for f in fills if f.color.lower() != page_color
                      for rr, b in enumerate(baselines) if f.rect.y0 <= b <= f.rect.y1
                      for cc in range(n_cols) if f.rect.x0 <= (bounds[cc] + bounds[cc + 1]) / 2 <= f.rect.x1],
            **({"bands": bands} if bands else {}),
            "spans": [s.id for s in spans],
        }
        # A table Slides cannot set on the page, its columns closed up to their words and its
        # text at emit.TABLE_MIN_SHRINK (an overfull table far wider than the page), stays a
        # picture: native, it ran off the slide's edge and lost its last columns.
        from .emit import table_fits  # (emit imports this module)
        return table if table_fits(element_json(table), self.W) else None

    def sides_run_on(self, frame: Rect) -> Rect:
        """frame, the extent of a group of equal rules, grown along vertical rules that run on
        from both its ends past its first or last rule: a longtable broken across frames, whose
        head is ruled above and below and whose rows hang between the side rules with no rule
        under them (real_africa-remote-sens-30 slide 8: the head alone was taken for a table,
        the side rules for two pictures, and the rows set as column text boxes off their rows).
        frame itself when no rule runs on from both ends."""
        pieces = [r for d in self.raw["drawings"] for r in [Rect.of(d["bbox"])]
                  if d["id"] not in self.decor_ids and r.h >= 3 and
                  ((d["type"] == "s" and d["items"] == "l" and r.w <= 1.0) or (d["type"] == "f" and d["items"] == "re" and r.w <= 1.5))]

        def reach(x: float, y: float, step: Literal["down", "up"]) -> float:
            side = [r for r in pieces if abs(r.cx - x) <= 1.0]
            moved = True
            while moved:
                moved = False
                for r in side:
                    match step:
                        case "down":
                            if r.y0 <= y + 1.0 and r.y1 > y + 0.01:
                                y, moved = r.y1, True
                        case "up":
                            if r.y1 >= y - 1.0 and r.y0 < y - 0.01:
                                y, moved = r.y0, True
                        case _:
                            assert_never(step)
            return y
        y1 = min(reach(frame.x0, frame.y1, "down"), reach(frame.x1, frame.y1, "down"))
        y0 = max(reach(frame.x0, frame.y0, "up"), reach(frame.x1, frame.y0, "up"))
        # (a ruled table's side rules end half a rule past its first and last: no run on)
        return Rect(frame.x0, y0 if y0 < frame.y0 - 1 else frame.y0, frame.x1, y1 if y1 > frame.y1 + 1 else frame.y1)

    def table_ruling(self, c: Rect) -> Ruling | None:
        """The rules and shading that make the cluster c a table (`Ruling`). None when there are
        no such rules, or c holds an image or any other drawing."""
        groups = [g for g in self.table_rules if c.expand(1).contains_rect(union_all(r.rect for r in g), tol=0.5)]
        # Fewer than two full rules, but cell shading edge to edge (a heatmap, \rowcolors): the
        # shading and the rules there are frame the table.
        grid: list[Rect] = [] if groups else self.fill_grid(c)
        if grid:
            frame = union_all(grid)
        elif groups:
            # \cline{2-3} twice over the same columns is a group of equal-extent rules of its own:
            # inside the widest group's frame it is one of that table's partial rules, not a second
            # table (refused, the grid became a diagram with each row's words in one box).
            outer = max(groups, key=lambda g: (union_all(r.rect for r in g).w, len(g)))
            frame = union_all(r.rect for r in outer)
            if any(g is not outer and not frame.expand(1).contains_rect(union_all(r.rect for r in g), tol=0.5) for g in groups):
                return None
            if any(g not in groups and union_all(r.rect for r in g).expand(1).contains_rect(frame.expand(1), tol=0.5)
                   for g in self.table_rules):
                return None  # the \cline pieces of a larger table: that table's, whole
            run_on = self.sides_run_on(frame)
            if run_on != frame:
                c = union_all([c, run_on.expand(1)])
        else:
            return None
        box = c.expand(0.5)
        if any(box.contains_rect(Rect.of(im["bbox"]), tol=0.5) for im in self.raw["images"]):
            return None
        # Every drawing must be a horizontal or vertical rule (\hline, \cline, |, booktabs);
        # cell shading and anything else keep the table a picture.
        horizontal: list[Rule] = []
        vertical: list[Rule] = []
        fills: list[Fill] = []
        for d in self.raw["drawings"]:
            r = Rect.of(d["bbox"])
            if d["id"] in self.decor_ids or not box.contains_rect(r, tol=0.5) or r.w * r.h >= 0.95 * self.W * self.H:
                continue
            color = (d["fill"] if d["type"] == "f" else d["stroke"]) or "#000000"
            stroke = d["type"] == "s" and d["items"] == "l"
            fill = d["type"] == "f" and d["items"] == "re"
            if (stroke and r.h <= 1.0) or (fill and r.h <= 1.5 and r.w >= 3):
                horizontal.append(Rule(rect=r, color=color, weight=r.h if fill else (d["width"] or 0.4)))
            elif (stroke and r.w <= 1.0) or (fill and r.w <= 1.5 and r.h >= 3):
                vertical.append(Rule(rect=r, color=color, weight=r.w if fill else (d["width"] or 0.4)))
            elif fill and d.get("fill_opacity", 1.0) >= 0.99:
                shade = d["fill"]
                if shade is None:
                    # (a fill of no colour extract could read, a shading: as a cell's colour
                    # it failed the whole page)
                    return None
                fills.append(Fill(rect=r, color=shade))  # \rowcolor, \cellcolor
            else:
                return None
        if vertical or grid:
            frame = union_all([frame] + [v.rect for v in vertical + (horizontal if grid else [])])
        return Ruling(reach=c, frame=frame, horizontal=horizontal, vertical=vertical, fills=fills)

    def plain_tables(self, lines: list[Line]) -> list[TableElement]:
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

        # (a tabular set inside an item has cells that start elsewhere too: its rows stay)
        rows = [row for row in rows if not all(id(l) in run_on and len(cells([l])) == 1 for l in row)]
        split = [(row, cells(row)) for row in rows]
        tables: list[TableElement] = []
        i = 0
        while i < len(split):
            j = i
            k = len(split[i][1])
            size = split[i][0][0].size

            def ok(r: tuple[list[Line], list[list[Span]]]) -> bool:
                return len(r[1]) == k >= 2 and all(len("".join(s.text for s in c).strip()) <= 30 for c in r[1]) \
                    and abs(r[0][0].size - size) <= 0.5 and one_baseline(r[1])
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
            col_info: list[Column] = []
            for c, (x0, x1) in enumerate(columns):
                chunks = [r[1][c] for r in group]
                left = all(abs(extent(ch)[0] - x0) <= 1 for ch in chunks)
                right = all(abs(extent(ch)[1] - x1) <= 1 for ch in chunks)
                digits = sum(ch_.isdigit() for ch in chunks for s in ch for ch_ in s.text)
                letters = sum(ch_.isalpha() for ch in chunks for s in ch for ch_ in s.text)
                align: Align = "right" if right and (not left or digits > letters) else "left" if left else "center"
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
                "id": f"p{self.raw['index']}pt{len(tables)}", "kind": "table", "role": "table",
                "bbox": rect.expand(1.0).as_list(), "frame": [bounds[0], rect.y0, bounds[-1], rect.y1],
                "size": round(size, 2), "row_baselines": [round(b, 2) for b in baselines],
                "row_heights": [round(p, 2) for p in pitches + [pitches[-1]]], "columns": col_info,
                "bounds": [round(b, 2) for b in bounds],
                "cells": [[span_runs(ch) for ch in r[1]] for r in group],
                "merges": [], "rules": [], "borders": [], "spans": [s.id for s in spans],
            })
        return tables


ROW_BASELINE_SLACK = 0.5
"""How far apart (pt) the cells of a rule-less table's row may stand: TeX sets a tabular row's
cells on one baseline."""


def one_baseline(cells: list[list[Span]]) -> bool:
    """The cells of a row stand on one baseline (each cell's by its largest words, not its
    scripts), as a tabular sets them. Side-by-side columns (beamer's columns, two minipages)
    are centred on their own and their lines rarely line up: real_dstalk-datascience-ta slide
    17's two centred contact columns, 2.2 pt apart line for line, were a plain table whose
    columns emit re-fitted off their centres, while each column's name above stayed a text box
    of its own."""
    bases = [statistics.fmean(s.baseline for s in c if s.size >= 0.9 * max(x.size for x in c)) for c in cells]
    return max(bases) - min(bases) <= ROW_BASELINE_SLACK


LISTING_NUMBER = re.compile(r"\d{1,3}:")


def numbered_listing(rows: list[list[Span]]) -> bool:
    """An algorithm between rules (algpseudocode's line numbers "1:", "2:", ... left of every
    line, the lines indented by nesting) is no table: its cells came out centred, the numbers a
    column of their own and the nesting lost (defense, slide 35). It stays a picture."""
    numbers = [int(text[:-1]) for row in rows
               for text in [min(row, key=lambda s: s.rect.x0).text.strip()] if LISTING_NUMBER.fullmatch(text)]
    return len(numbers) >= 3 and numbers == list(range(numbers[0], numbers[0] + len(numbers)))


class TableRows:
    """A table's words by row, the rows of its \\multirow cells (`between`) and of its wrapped
    cells' further lines (`wrapped`, `continued`), and the chunks the rows fall into."""

    def __init__(self, spans: list[Span], vertical: list[Rule]) -> None:
        self.size: float = max(s.size for s in spans)
        self.vertical: list[Rule] = vertical
        # Rows by baseline. A row sitting halfway between its neighbours is a \multirow cell
        # spanning both of them.
        rows: list[list[Span]] = []
        for s in spans:
            anchor = max(rows[-1], key=lambda x: x.size) if rows else None  # the row's normal-size text
            if anchor is not None and abs(s.baseline - anchor.baseline) <= 0.5 * max(s.size, anchor.size):
                rows[-1].append(s)
            else:
                rows.append([s])
        self.rows: list[list[Span]] = rows
        self.base: list[float] = [statistics.fmean(s.baseline for s in row) for row in rows]
        self.between: set[int] = {i for i in range(1, len(rows) - 1)
                        if self.halfway(i) and not any(a.rect.x0 < b.rect.x1 and b.rect.x0 < a.rect.x1
                                                       for a in rows[i] for b in rows[i - 1] + rows[i + 1])}
        self.wrapped: dict[int, list[list[float]]] = {}  # row index -> its wrapped cells' [x0, x1]
        self.continued: set[int] = set()  # rows holding nothing but the next lines of cells above
        # Where three or more rows start a cell after a gap wider than a word space: a column's
        # left edge. Phrases (a wrapped cell's lines) part there even when the columns are set
        # closer than an em (real_africa-remote-sens-30 slide 8: a citation column 0.6 em from
        # the DOI column; the DOI joined the citation's phrase, and the cell lines under it were
        # read as one wrapped cell across both columns).
        starts: dict[float, set[int]] = {}
        for i, row in enumerate(rows):
            line = sorted(row, key=lambda s: s.rect.x0)
            for a, b in zip(line, line[1:]):
                if b.rect.x0 - a.rect.x1 > 0.5 * self.size:
                    starts.setdefault(round(b.rect.x0 * 2) / 2, set()).add(i)
        self.column_starts: list[float] = [x for x, at in starts.items() if len(at) >= 3]

    def halfway(self, i: int) -> bool:
        """Row i sits in the middle of its neighbours, which are one row pitch of this
        table apart (\\arraystretch and \\hline gaps make that pitch well over an em, so
        the half pitches of a \\multirow are too)."""
        base, size = self.base, self.size
        gaps = (base[i] - base[i - 1], base[i + 1] - base[i])
        if max(gaps) < 0.75 * size:
            return True
        others = [b - a for k, (a, b) in enumerate(zip(base, base[1:])) if k not in (i - 1, i)]
        return bool(others) and abs(gaps[0] - gaps[1]) <= 0.4 * size and max(gaps) < 1.2 * size and \
            sum(gaps) <= 1.15 * statistics.median(others)

    def grid_rows(self) -> list[list[Span]]:
        """The rows of the grid: every row but the \\multirow cells between two."""
        return [row for i, row in enumerate(self.rows) if i not in self.between]

    def ruled(self, a: Span, b: Span) -> bool:
        """A vertical rule between two words of a row (not one of another row: a
        \\multicolumn's words run across the rule the rows above and below have there)."""
        y = b.baseline - 0.3 * b.size
        return any(a.rect.x1 < v.rect.cx < b.rect.x0 and v.rect.y0 <= y <= v.rect.y1 for v in self.vertical)

    def runs_on(self, a: Span, b: Span) -> bool:
        """b is the next word of a's phrase: less than an em after it, with no vertical rule and
        no column's left edge (`column_starts`) in between."""
        gap = b.rect.x0 - a.rect.x1
        return gap <= b.size and not self.ruled(a, b) and \
            not (gap > 0.5 * self.size and any(abs(b.rect.x0 - x) <= 0.5 for x in self.column_starts))

    def phrase(self, spans: list[Span], x0: float) -> list[Span]:
        out: list[Span] = []
        for s in sorted(spans, key=lambda s: s.rect.x0):
            if not out and abs(s.rect.x0 - x0) <= 0.5 or out and self.runs_on(out[-1], s):
                out.append(s)
            elif out:
                break
        return out

    def phrases(self, spans: list[Span]) -> list[list[Span]]:
        out: list[list[Span]] = []
        for s in sorted(spans, key=lambda s: s.rect.x0):
            if out and self.runs_on(out[-1][-1], s):
                out[-1].append(s)
            else:
                out.append([s])
        return out

    @staticmethod
    def words(spans: list[Span]) -> str:
        return " ".join(s.text for s in spans).strip()

    def right_end(self, x0: float) -> float:
        """Where the longest line starting at x0 ends: a justified cell's full lines."""
        return max((p[-1].rect.x1 for row in self.rows for p in [self.phrase(row, x0)] if p), default=x0)

    def runs_on_below(self, first: list[Span], line: list[Span]) -> bool:
        """line, alone in its row under first, is first's cell wrapping (a p{} cell's next line)
        and not a row of its own whose other cells are blank: an `l` column listing several
        entries beside one label (real_pnuc-intro-pnuc slide 6: "Traditional no use" and "No use
        episodes" under "No-use" were joined into one cell, which Slides then wrapped by itself).
        The pitch says nothing (a tabular's rows stand \\baselineskip apart, as a p{} cell's lines
        do), so the words must. line's first word must not fit on first's line within the column
        (the widest line starting where first does: a p{} column is no narrower), and line must
        run on: first ends on a hyphen, or line starts in lower case, or first is full - it ends
        where another line of the column ends, a justified cell's edge. A wrap this cannot tell
        stays a row per line, which looks as the PDF does."""
        x0, end = first[0].rect.x0, first[-1].rect.x1
        if self.words(first).endswith("-"):
            return True
        head = line[0]
        word = (head.text.split() or [""])[0]
        width = head.rect.w * len(word) / max(len(head.text), 1)
        right = self.right_end(x0)
        if end + width + 0.5 * self.size < right:
            return False  # its first word would have fit on the line above
        if self.words(line)[:1].islower():
            return True
        ends = [p[-1].rect.x1 for row in self.rows for p in [self.phrase(row, x0)] if p]
        return end >= right - 0.5 and sum(abs(e - end) <= 0.5 for e in ends) >= 2

    def find_wrapped(self) -> None:
        """A cell set in a paragraph column (p{3cm}) wraps: its next lines are rows of their own
        holding nothing but words in that column, starting where the cell starts, and its lines
        are justified, their word spaces stretched past the half em that parts two cells. Each
        such line is one chunk from the cell's left edge to its right: its words cut into
        chunks tangled the columns and the table was refused, and as text boxes the cell's
        first line joined the numbers beside it and reflowed across their column in Slides."""
        rows, base, size, between = self.rows, self.base, self.size, self.between
        phrase, phrases, words, wrapped = self.phrase, self.phrases, self.words, self.wrapped
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
                           f[-1].rect.x1 >= self.right_end(p[0].rect.x0) - 0.5 for p in parts):
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
                if not firsts[0] or not self.runs_on_below(firsts[0], parts[0]):
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
                self.continued.add(j)

    def chunks_of(self, row: list[Span], i: int) -> list[list[Span]]:
        chunks: list[list[Span]] = []
        cells: list[list[Span]] = []
        for x0, x1 in self.wrapped.get(i, []):
            cell = sorted((s for s in row if s.rect.x0 >= x0 - 0.5 and s.rect.x1 <= x1 + 0.5), key=lambda s: s.rect.x0)
            row = [s for s in row if s not in cell]
            cells += [cell] if cell else []
        for s in sorted(row, key=lambda s: s.rect.x0):
            if chunks and s.rect.x0 - chunks[-1][-1].rect.x1 <= 0.5 * self.size and not self.ruled(chunks[-1][-1], s):
                chunks[-1].append(s)
            else:
                chunks.append([s])
        return sorted(chunks + cells, key=lambda ch: ch[0].rect.x0) if cells else chunks

    def chunks(self) -> tuple[list[Chunk], list[list[float]]]:
        """The rows' chunks as (row index in grid_rows, row span, chunk), and the columns
        [x0, x1] the chunks that span no other fall into."""
        rows, between, vertical = self.rows, self.between, self.vertical
        items: list[Chunk] = []
        for i, row in enumerate(rows):
            r = sum(1 for j in range(i) if j not in between)
            for ch in self.chunks_of(row, i):
                items.append((r - 1, 2, ch) if i in between else (r, 1, ch))

        # A chunk overlapping two separate chunks of another row (\multicolumn), or crossing a
        # vertical rule, spans several columns; columns come from the other chunks.
        def spanning(item: Chunk) -> bool:
            r, _, ch = item
            x0, x1 = extent(ch)
            if any(x0 + 1 < v.rect.cx < x1 - 1 for v in vertical):
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
        def cut(item: Chunk) -> list[Chunk]:
            r, rs, ch = item
            others = [extent(it[2]) for it in items if it[0] != r]
            rows_of = [[extent(it[2]) for it in items if it[0] == r2] for r2 in {it[0] for it in items if it[0] != r}]
            pieces: list[list[Span]] = []
            start = 0
            for k in range(1, len(ch)):
                a, b = ch[k - 1].rect.x1, ch[k].rect.x0
                x = (a + b) / 2
                if not any(x0 - 0.5 < x < x1 + 0.5 for x0, x1 in others) and \
                        any(x1 <= a for x0, x1 in others) and any(x0 >= b for x0, x1 in others):
                    pieces.append(ch[start:k])
                    start = k
            pieces.append(ch[start:])

            def lined_up(p: list[Span]) -> bool:
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
            if columns and x0 < columns[-1][1] + 1 and not any(columns[-1][1] - 1 < v.rect.cx < x0 + 1 for v in vertical):
                columns[-1][1] = max(columns[-1][1], x1)
            else:
                columns.append([x0, x1])
        return items, columns


def extent(ch: list[Span]) -> tuple[float, float]:
    return ch[0].rect.x0, ch[-1].rect.x1


def column_bounds(frame: Rect, columns: list[list[float]], vertical: list[Rule], fills: list[Fill]) -> list[float]:
    """Where the table's columns part: on a vertical rule between them, else where cell
    shading starts (exactly at TeX's column edges), else halfway; the frame at both ends."""
    bounds = [frame.x0]
    fill_edges = sorted({round(f.rect.x0, 2) for f in fills})
    for a, b in zip(columns, columns[1:]):
        rule = [v.rect.cx for v in vertical if a[1] - 1 <= v.rect.cx <= b[0] + 1] or \
            [x for x in fill_edges if a[1] - 1 <= x <= b[0] + 1]
        bounds.append(rule[0] if rule else (a[1] + b[0]) / 2)
    bounds.append(frame.x1)
    return bounds


@dataclass(frozen=True, kw_only=True)
class TableCells:
    """A table's chunks set in its grid: `cells[row][col]` the words of the cell starting there,
    `merges` the cells spanning several rows or columns (`extent_of`: where their words run, by
    index in merges), `placed` each column's single cells, `heads` its cell in the first row and
    `row_of` a single cell's row (by id). `rows` are the grid's rows, `cell_lines` each cell's
    lines, `row_lines` and `last_line` each row's number of lines and last baseline: one line a
    row as `place_cells` sets them, a wrapped cell's lines one cell after `join_wrapped`."""
    rows: list[list[Span]]
    cells: list[list[list[Span]]]
    placed: list[list[list[Span]]]
    heads: list[list[Span] | None]
    merges: list[Merge]
    extent_of: dict[int, tuple[float, float]]
    row_of: dict[int, int]
    row_lines: list[int]
    last_line: list[float]
    cell_lines: list[list[list[list[Span]]]]

    def join_wrapped(self, cont: set[int]) -> "TableCells":
        """A wrapped cell's next lines were rows of their own up to here (columns, merges and
        alignments are found line by line); now they join the cell they continue, one cell of
        several lines that Slides wraps in its column as TeX did - and wraps again when a person
        types into it. Not across a merged cell: that grid is not one we understand line by
        line. cont: the grid rows holding a wrapped cell's further lines."""
        n_rows, n_cols = len(self.rows), len(self.placed)
        cells = self.cells
        merged_rows = {g for m in self.merges for g in range(m["row"], m["row"] + m["rows"])}
        if not cont or cont & merged_rows or 0 in cont:
            return self
        row_lines = list(self.row_lines)
        last_line = list(self.last_line)
        cell_lines = [[list(lines) for lines in row] for row in self.cell_lines]
        keep: list[int] = []
        head_of: dict[int, int] = {}
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
        return replace(self, merges=[Merge(row=at[m["row"]], col=m["col"], rows=m["rows"], cols=m["cols"], align=m["align"])
                                     for m in self.merges],
                       rows=[self.rows[g] for g in keep], cell_lines=[cell_lines[g] for g in keep],
                       row_lines=[row_lines[g] for g in keep], last_line=[last_line[g] for g in keep])


def place_cells(items: list[Chunk], bounds: list[float], grid_rows: list[list[Span]]) -> TableCells | None:
    """Each chunk (row, row span, words) in the cells its words cover, or None when a chunk
    covers no column or two chunks the same cell."""
    n_cols = len(bounds) - 1
    cells: list[list[list[Span]]] = [[[] for _ in range(n_cols)] for _ in grid_rows]
    placed: list[list[list[Span]]] = [[] for _ in range(n_cols)]
    heads: list[list[Span] | None] = [None] * n_cols  # (each column's cell in the first row)
    extent_of: dict[int, tuple[float, float]] = {}  # (a merged cell's words, by its index in merges)
    merges: list[Merge] = []
    covered: dict[tuple[int, int], Chunk] = {}
    row_of: dict[int, int] = {}  # (a single cell's words, by id: its row)
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
            align: Align = "center" if abs((x0 + x1) / 2 - mid) <= 2 else "left" if x0 - bounds[c0] < bounds[c0 + cs] - x1 \
                else "right"
            merges.append(Merge(row=r, col=c0, rows=rs, cols=cs, align=align))
            extent_of[len(merges) - 1] = (x0, x1)
        else:
            placed[c0].append(ch)
            row_of[id(ch)] = r
            if r == 0 and rs == 1:
                heads[c0] = ch
    # (one line a row until join_wrapped: a row's last baseline is its baseline)
    return TableCells(rows=grid_rows, cells=cells, placed=placed, heads=heads, merges=merges, extent_of=extent_of,
                      row_of=row_of, row_lines=[1] * len(grid_rows), last_line=[line_base(row) for row in grid_rows],
                      cell_lines=[[[cell] if cell else [] for cell in row] for row in cells])


def aligned(chunks: list[list[Span]], x0: float, x1: float) -> Align:
    left = all(abs(ch[0].rect.x0 - x0) <= 1 for ch in chunks)
    right = all(abs(ch[-1].rect.x1 - x1) <= 1 for ch in chunks)
    digits = sum(c.isdigit() for ch in chunks for s in ch for c in s.text)
    letters = sum(c.isalpha() for ch in chunks for s in ch for c in s.text)
    if left and right and digits > letters:
        return "right"  # equally wide numbers: right-aligned, like a number column
    return "left" if left else "right" if right else "center"


def column_info(columns: list[list[float]], cells: TableCells) -> list[Column]:
    """Each column's extent and alignment, its head's and body's when they differ."""
    col_info: list[Column] = []
    for (x0, x1), chunks, head in zip(columns, cells.placed, cells.heads):
        # (`body` is a pair, written as a list: `ir.deck_json` is the classified deck as JSON)
        info: Column = {"x0": round(x0, 2), "x1": round(x1, 2), "align": aligned(chunks, x0, x1)}
        # A column head set otherwise than its body (\thead centred over a left column, an S
        # column's head centred over numbers set flush right): the head row keeps its own
        # alignment (`head`) and the body its own, to the body's edges (`body`). As one, a
        # centred head took the body's left edge, or every number was centred.
        body = [ch for ch in chunks if ch is not head]
        # siunitx centres what is no number (a dash for a missing value) on the column, the head's
        # centre, while its numbers keep their decimal places, off the middle (r2_tables_v2 slide
        # 4): such a cell is set centred over the column like the head (`centred`, its rows), the
        # body's alignment is its numbers'.
        if head is not None:
            hc = (head[0].rect.x0 + head[-1].rect.x1) / 2

            def numeric(ch: list[Span]) -> bool:
                return any(c.isdigit() for s in ch for c in s.text)

            def on_axis(ch: list[Span]) -> bool:
                return abs((ch[0].rect.x0 + ch[-1].rect.x1) / 2 - hc) <= 1
            odd = [ch for ch in body if not numeric(ch) and on_axis(ch)]
            rest = [ch for ch in body if not any(ch is o for o in odd)]
            if odd and len(rest) >= 2 and all(numeric(ch) and not on_axis(ch) for ch in rest):
                bx0, bx1 = min(ch[0].rect.x0 for ch in rest), max(ch[-1].rect.x1 for ch in rest)
                info["align"] = aligned(rest, bx0, bx1)
                info["head"] = "center"
                info["body"] = (round(bx0, 2), round(bx1, 2))
                info["centred"] = sorted(cells.row_of[id(ch)] for ch in odd)
                col_info.append(info)
                continue
        if head is not None and len(body) >= 2:
            bx0, bx1 = min(ch[0].rect.x0 for ch in body), max(ch[-1].rect.x1 for ch in body)
            hx0, hx1 = head[0].rect.x0, head[-1].rect.x1
            if abs((hx1 - hx0) - (bx1 - bx0)) <= 2 or \
                    all(abs(ch[0].rect.x0 - bx0) <= 1 and abs(ch[-1].rect.x1 - bx1) <= 1 for ch in body):
                col_info.append(info)  # (a head as wide as its body, or a body of equally wide
                continue               # cells - ticks, numbers: set alike every way)
            how = aligned(body, bx0, bx1)
            own: Align | None = "left" if abs(hx0 - bx0) <= 1 else "right" if abs(hx1 - bx1) <= 1 else \
                "center" if abs((hx0 + hx1) - (bx0 + bx1)) <= 3 else None
            if own is not None and (own != how or how != info["align"]):
                info["align"] = how
                info["head"] = own
                info["body"] = (round(bx0, 2), round(bx1, 2))
        col_info.append(info)
    return col_info


def align_merges(cells: TableCells, col_info: list[Column]) -> None:
    """A \\multirow cell in one column is flush with the column's words or centred among them
    (a \\thead over a left column): its column says which, not the cell's bounds - a short
    word flush left in a narrow column sits near the middle of those either way."""
    for k, (x0, x1) in cells.extent_of.items():
        m = cells.merges[k]
        if m["cols"] == 1:
            cx0, cx1 = col_info[m["col"]].get("body") or (col_info[m["col"]]["x0"], col_info[m["col"]]["x1"])
            m["align"] = "left" if abs(x0 - cx0) <= 1 else "right" if abs(x1 - cx1) <= 1 else \
                "center" if abs((x0 + x1) - (cx0 + cx1)) <= 3 else m["align"]


def line_base(row: list[Span]) -> float:
    return statistics.fmean(s.baseline for s in row if s.size >= 0.9 * max(x.size for x in row))


def row_boundary(baselines: list[float], y: float) -> int:
    return sum(b < y for b in baselines)


def table_borders(horizontal: list[Rule], vertical: list[Rule], frame: Rect, bounds: list[float],
                  columns: list[list[float]], baselines: list[float], size: float) -> tuple[list[Rule], list[Border]] | None:
    """Borders: rules across the whole table stay row rules; partial rules (\\cline,
    \\cmidrule) and vertical rules become the borders of the cells they run along. Returns
    (row rules, cell borders), or None for a vertical rule inside a column."""
    n_rows, n_cols = len(baselines), len(columns)
    borders: list[Border] = []
    full = [h for h in horizontal if h.rect.x0 <= frame.x0 + 1.5 and h.rect.x1 >= frame.x1 - 1.5]
    for h in horizontal:
        if h in full:
            continue
        k = row_boundary(baselines, h.rect.cy)
        for cc in range(n_cols):
            # (\cmidrule(l) is trimmed by half an em at its left end: it still underlines
            # every word of the column)
            if (h.rect.x0 <= bounds[cc] + 2.5 or h.rect.x0 <= columns[cc][0] + 1) and \
                    (h.rect.x1 >= bounds[cc + 1] - 2.5 or h.rect.x1 >= columns[cc][1] - 1):
                borders.append({"row": min(k, n_rows - 1), "col": cc, "position": "TOP" if k < n_rows else "BOTTOM",
                                "color": h.color, "weight": round(h.weight, 2), "y": round(h.rect.cy, 2)})
    for v in vertical:
        k = min(range(len(bounds)), key=lambda i: abs(bounds[i] - v.rect.cx))
        if abs(bounds[k] - v.rect.cx) > 1.5:
            # The second stroke of a double rule (||, \doublerulesep 2 pt beside the first):
            # a Slides border is one line, the one on the bound is written.
            if any(u is not v and abs(bounds[k] - u.rect.cx) <= 1.5 and abs(u.rect.cx - v.rect.cx) <= 4
                   and u.rect.y0 < v.rect.y1 and v.rect.y0 < u.rect.y1 for u in vertical):
                continue
            return None  # a rule inside a column
        for rr, b in enumerate(baselines):
            if v.rect.y0 <= b - 0.5 * size and v.rect.y1 >= b:
                borders.append({"row": rr, "col": min(k, n_cols - 1), "position": "LEFT" if k < n_cols else "RIGHT",
                                "color": v.color, "weight": round(v.weight, 2)})
    return full, borders


def row_heights(baselines: list[float], last_line: list[float], row_lines: list[int], size: float) -> list[float]:
    """Each row's height: the pitch to the next row's baseline. The last row: as tall as the
    one before it (as a line of a wrapped cell, when that one wraps), plus its own further
    lines."""
    n_rows = len(baselines)
    pitches = [b - a for a, b in zip(baselines, baselines[1:])] or [1.4 * size]
    lead = min([(e - b) / (k - 1) for b, e, k in zip(baselines, last_line, row_lines) if k > 1] or pitches)
    last = (pitches[-1] if n_rows < 2 or row_lines[-2] == 1 else lead) + last_line[-1] - baselines[-1]
    return pitches + [last] if n_rows > 1 else [last]


def slides_bottom(baselines: list[float], heights: list[float], row_lines: list[int], size: float,
                  page_w: float) -> float:
    """Where the table ends in Slides. A Slides row is at least its lines, 1.195 em x
    lineSpacing for the first and 1.2 em for each further one: emit's tables come with the
    .pptx, without the 7.2 pt of padding above and below an API-made table has
    (emit.table_rows)."""
    scale = 720.0 / page_w
    z = size * scale / 1.02
    body = [(1.195 + (k - 1) * 1.2) * z for k in row_lines]
    ratio = min(1.0, max(0.5, min(h * scale / b for h, b in zip(heights, body))))
    row_h = [max(h * scale, b * ratio) / scale for h, b in zip(heights, body)]
    top = baselines[0] - (0.968 * z - 0.72 - (1 - ratio) * 0.9 * z) / scale
    return top + sum(row_h)


def row_bands(shown: list[Fill], baselines: list[float]) -> list[tuple[int, float, float]]:
    """[row, top, bottom] of a row shaded by a band of its own (\\rowcolor, \\rowcolors: fills
    that hold its baseline and no other row's): emit puts the Slides row's edges there, as on
    a rule. Set just above its words instead, a shaded row began ~3 pt below its band and
    the words sat at the top of their fill (r2_tables_v1 slide 4)."""
    bands: list[tuple[int, float, float]] = []
    for rr, b in enumerate(baselines):
        own = [f.rect for f in shown if f.rect.y0 <= b <= f.rect.y1
               and sum(f.rect.y0 <= b2 <= f.rect.y1 for b2 in baselines) == 1]
        if own:
            bands.append((rr, round(min(r.y0 for r in own), 2), round(max(r.y1 for r in own), 2)))
    return bands
