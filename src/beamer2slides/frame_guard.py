"""Pull's convergence loop never leaves a frame worse than it found it.

`inverse.converge` edits the source by what the read-back says: classify of the compiled PDF against
the deck. That view can be wrong about a right page - a wrapped paragraph read as several, stacked
boxes read as one, shapes read as background - and then the loop "fixes" a slide that was already
right: on the adopt micro-corpus (2026-09-26) plain-layouts:6 went from ink 0.993 to 0.501 in two
rounds, ds-lecture:13 from 0.988 to 0.439, and plain-fonts:5 once reached 0 residuals ("converged")
while its ink fell from 0.979 to 0.267. So the residual count alone cannot say whether a round
helped a slide.

The guard watches every round, frame by frame: it remembers each frame's text and a score of the
page it made, and when the loop stops, every frame ends at its best round - best by the score below,
then by weighted residuals, and the earliest of rounds that tie. A round must earn its edits: one
that the scores cannot tell from an earlier one is not kept (sc-functions:8 read the same 66
residuals after round 1 while its ink fell from 0.984 to 0.912; arabic-training:6 read 29 and 29
while it fell from 0.960 to 0.805). An edit only the residuals see (notes, a style on the same
glyphs) scores the same ink and fewer residuals, and stays. The preamble keeps what the loop added:
colours and packages a frame no longer uses are harmless. A frame is one unit whatever it holds: its target slides are the ones
compare paired with its pages, and it is known across rounds by its label, else by those slides.

The score, higher is better:
  ink        with Google's own picture of each target slide (a foreign read keeps its thumbnail's
             path on the slide, `deck_ir`; `converge(thumbnails=...)` can hand others): the page's
             `boxes` score against it (`page_score`), the mean over the frame's slides. This does not
             depend on the read-back at all. Rounds within `INK_MARGIN` (float noise) tie on it.
  residuals  without a picture (pull reads none), and the tie-break with one: the frame's open
             residuals, weighted by kind (`RESIDUAL_WEIGHT`: words count more than places), plus
             `WORD_WEIGHT` per word the page shows that the slide does not have or lacks one it has -
             the page's words as extract reads them, which no grouping of lines into paragraphs can
             misread. Lower is better; any gain counts (the counts are exact, not noisy).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# boxes score a frame may lose before it is put back: float noise only. At 0.005 five micro-corpus
# slides kept rounds that cost 0.001-0.002 of ink and bought no residual (apps-edu-zh:9 13 -> 13,
# creandum-board:22 22 -> 22); a page that inks less than the deck's picture is worse. An edit the
# ink cannot see (notes, a style on the same glyphs) scores the same and stays.
INK_MARGIN = 0.0005
PENALTY_EPS = 1e-6          # weighted residuals are sums of halves: equal means equal
WORD_WEIGHT = 1.0           # per word the page has wrong, missing or extra
RESIDUAL_WEIGHT = {
    "text": 3.0, "paragraph_missing": 3.0, "paragraph_extra": 3.0, "paragraph_order": 2.0,
    "element_missing": 2.0, "element_extra": 2.0, "table": 2.0, "diagram": 2.0, "slide_missing": 10.0,
    "image": 1.0, "shape": 1.0, "style": 1.0, "bullet": 1.0, "align": 1.0, "notes": 1.0, "background": 1.0,
    "geometry": 0.5,
}
PUNCT = ".,;:!?()[]{}\"'«»“”‘’–—-•·"


def words_of(text: str) -> list[str]:
    from .compare import norm_text
    return [w for w in (w.strip(PUNCT).lower() for w in norm_text(text).split()) if w]


def slide_words(slide: dict) -> list[str]:
    """The words a slide's IR says it shows: text boxes, tables and diagram labels (a formula or icon
    picture has none)."""
    from .compare import diagram_text, table_text
    out: list[str] = []
    for el in slide.get("elements", []):
        if el.get("role") in ("math", "icon"):
            continue
        if el.get("kind") == "table":
            out += [w for row in table_text(el) for c in row for w in words_of(c)]
        elif el.get("kind") == "diagram":
            out += [w for t in diagram_text(el) for w in words_of(t)]
        else:
            for p in el.get("paragraphs") or []:
                out += words_of("".join(r.get("text", "") for r in p.get("runs", []) if not r.get("hole")))
    return out


def word_error(want: list[str], got: list[str]) -> int:
    """Words one side has more of than the other, both ways."""
    a, b = Counter(want), Counter(got)
    return sum((a - b).values()) + sum((b - a).values())


def target_thumbnails(target: dict):
    """The thumbnails a target carries (`deck_ir` keeps a foreign read's paths as `thumbnail`)."""
    slides = target.get("slides", [])
    if not any(s.get("thumbnail") for s in slides):
        return None
    return lambda j: slides[j].get("thumbnail") if 0 <= j < len(slides) else None


@dataclass
class Seen:
    """One frame in one round."""
    round: int
    file: Path
    span: tuple[int, int]          # the frame's offsets in that round's text of `file`
    text: str
    slides: tuple[int, ...]        # target slides it stands for
    mode: str                      # "ink" or "residuals"
    score: float                   # higher is better: ink, or minus the penalty
    label: str | None
    penalty: float = 0.0           # weighted residuals and wrong words, lower is better (both modes)


class FrameGuard:
    def __init__(self, target: dict, thumbnails=None, log=print):
        self.target = target
        self.thumbnails = thumbnails
        self.log = log
        self.history: dict[tuple, list[Seen]] = {}
        self.slides_of: dict[tuple, tuple[int, ...]] = {}
        self.cache: dict[tuple, float] = {}
        self._has_thumb: dict[int, bool] = {}
        self._words: dict[int, list[str]] = {}
        self.last_round = -1

    # ------------------------------------------------------------------ scores

    def has_thumbnail(self, j: int) -> bool:
        if j not in self._has_thumb:
            ok = False
            if self.thumbnails:
                src = self.thumbnails(j)
                ok = src is not None and (not isinstance(src, (str, Path)) or Path(src).is_file())
            self._has_thumb[j] = ok
        return self._has_thumb[j]

    def target_words(self, j: int) -> list[str]:
        if j not in self._words:
            self._words[j] = slide_words(self.target["slides"][j])
        return self._words[j]

    def ink(self, doc, cand, ci: int | None, j: int, text: str, context: str) -> float:
        """`boxes` of candidate slide `ci`'s page against target slide j's thumbnail (0: no page)."""
        if ci is None:
            return 0.0
        key = (j, text, context)
        if key not in self.cache:
            from .page_score import boxes_score, load_thumbnail
            ref = load_thumbnail(self.thumbnails(j))
            if ref is None:
                self._has_thumb[j] = False
                return 0.0
            if doc[0] is None:
                from .pdf import Document
                doc[0] = Document(cand.pdf)
            self.cache[key] = boxes_score(doc[0][cand.deck["slides"][ci]["page"]], ref, self.target["slides"][j])
        return self.cache[key]

    def penalty(self, cand, comp, cis: list[int], slides: tuple[int, ...]) -> float:
        wanted = set(slides)
        res = sum(RESIDUAL_WEIGHT.get(r["kind"], 1.0) for r in comp.open() if r.get("target_slide") in wanted)
        got = [w for ci in cis for w in words_of(" ".join(cand.words.get(cand.deck["slides"][ci]["page"], [])))]
        want = [w for j in slides for w in self.target_words(j)]
        return res + WORD_WEIGHT * word_error(want, got)

    # ------------------------------------------------------------------ rounds

    def observe(self, it: int, cand, comp) -> float | None:
        """Remember every frame of round `it` with its score; the mean ink score of the frames scored
        by ink (None: none were)."""
        self.last_round = it
        pairs = {ci: tj for ci, tj in comp.slides if ci is not None and tj is not None}
        by_frame: dict[int, list[int]] = {}
        frames = {}
        for ci, f in enumerate(cand.frames):
            if f is not None:
                by_frame.setdefault(f.index, []).append(ci)
                frames[f.index] = f
        context = context_hash(cand)
        doc = [None]
        seen_keys: dict[tuple, int] = {}
        entries: list[tuple[tuple, Seen]] = []
        inks = []
        try:
            for fi, cis in by_frame.items():
                f = frames[fi]
                paired = tuple(sorted({pairs[ci] for ci in cis if ci in pairs}))
                key = ("label", f.label) if f.label else ("slides", paired)
                if paired:
                    self.slides_of[key] = paired
                slides = self.slides_of.get(key, ())
                if not slides:
                    continue
                seen_keys[key] = seen_keys.get(key, 0) + 1
                text = cand.source.text(f.file)[f.start:f.end]
                pen = self.penalty(cand, comp, cis, slides)
                mode, score = "residuals", -pen
                if all(self.has_thumbnail(j) for j in slides):
                    of = {pairs[ci]: ci for ci in cis if ci in pairs}
                    if not of and len(cis) == len(slides):      # its pages paired with nothing: by order
                        of = dict(zip(slides, cis))
                    ink = sum(self.ink(doc, cand, of.get(j), j, text, context) for j in slides) / len(slides)
                    if all(self._has_thumb[j] for j in slides):  # every picture loaded after all
                        mode, score = "ink", ink
                        inks.append(ink)
                entries.append((key, Seen(it, f.file, (f.start, f.end), text, slides, mode, score, f.label, pen)))
        finally:
            if doc[0] is not None:
                doc[0].close()
        for key, seen in entries:
            if seen_keys[key] == 1:            # two frames under one key: neither can be told apart
                self.history.setdefault(key, []).append(seen)
        return sum(inks) / len(inks) if inks else None

    def plan(self) -> list[tuple[Seen, Seen]]:
        """(the frame as the loop left it, its best round) for every frame to put back."""
        out = []
        for key, seen in self.history.items():
            final = seen[-1]
            if final.round != self.last_round:
                continue
            best = best_round([s for s in seen if s.slides == final.slides and s.mode == final.mode])
            if best.text != final.text:
                out.append((final, best))
        return sorted(out, key=lambda fb: (str(fb[0].file), fb[0].span[0]))

    def score_now(self, final: Seen) -> float | None:
        """The score the frame of `final` has in the round observed last (after it was put back)."""
        for seen in self.history.values():
            if seen[-1].round == self.last_round and seen[-1].slides == final.slides and seen[-1].label == final.label:
                return seen[-1].score
        return None


def best_round(seen: list[Seen]) -> Seen:
    """The best of one frame's rounds: by ink within `INK_MARGIN` (a residuals-mode score is minus the
    penalty, so this step keeps the least penalised), then by the least penalty, then the earliest -
    a later round has to earn its edits."""
    top = max(s.score for s in seen)
    good = [s for s in seen if s.score >= top - (INK_MARGIN if s.mode == "ink" else PENALTY_EPS)]
    least = min(s.penalty for s in good)
    return min((s for s in good if s.penalty <= least + PENALTY_EPS), key=lambda s: s.round)


def context_hash(cand) -> str:
    """Everything of the source outside its frames (the preamble above all): a frame's page is the same
    page while its own text and this stay the same."""
    h = hashlib.sha1()
    spans: dict[Path, list[tuple[int, int]]] = {}
    for f in cand.source.frames:
        spans.setdefault(f.file, []).append((f.start, f.end))
    for p in cand.source.order:
        text, pos = cand.source.text(p), 0
        h.update(str(p).encode("utf-8", "replace") + b"\0")
        for a, b in sorted(spans.get(p, [])):
            h.update(text[pos:a].encode("utf-8", "replace"))
            pos = b
        h.update(text[pos:].encode("utf-8", "replace"))
    return h.hexdigest()


def why(final: Seen, best: Seen) -> str:
    when = "the first draft" if best.round == 0 else f"its text of iteration {best.round}"
    if final.mode == "ink" and final.score < best.score - INK_MARGIN:
        return (f"the loop's edits made its page match the deck's own picture less (ink {final.score:.3f}, "
                f"{best.score:.3f} at iteration {best.round}): put back to {when}")
    if final.penalty > best.penalty + PENALTY_EPS:
        return (f"the loop's edits left it further from the deck ({final.penalty:g} weighted residuals and "
                f"wrong words, {best.penalty:g} at iteration {best.round}): put back to {when}")
    seen = f"ink {final.score:.3f} and " if final.mode == "ink" else ""
    return (f"the loop's edits made nothing better that can be measured ({seen}{final.penalty:g} weighted "
            f"residuals and wrong words, as at iteration {best.round}): put back to {when}")
