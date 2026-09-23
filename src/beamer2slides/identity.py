"""Identity for sync: slide keys, element keys, fingerprints and IR hashes (docs/sync.md).

Slides are keyed by their beamer frame label, else `title:<normalised title>#<occurrence>`,
else `page:<n>`. Elements within a slide are keyed `kind/role/ordinal`. A new conversion (ours)
inherits the keys of the base it matches: labels first, then a sequence alignment of the
unlabelled slides; elements by key with a similar fingerprint, then by best fingerprint.
"""

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

HOLE_MARK = "□"  # an inline formula picture's place in fingerprint text
SLIDE_MATCH = 0.6     # least similarity of two unlabelled slides to be the same frame
LABEL_SURE = 1.2      # a label pairing this alike (same title, most of the words) needs no second opinion
LABEL_MOVED = 1.0     # a slide elsewhere this alike may be the frame the label used to name
                      # (of what the pair can score at all: `_moved_bar`)
LABEL_MARGIN = 0.5    # ... but only if it beats the label's own pairing by this much
LABEL_EXCHANGE = 0.02  # ... or by more than a tie (`TWIN_TIE`), if the two readings point at each
                       # other (an exchange): there the look back carries the doubt, not the number
CROSS_SURE = 1.15     # a slide this alike, left over by the order-keeping pass, is that frame moved
CROSS_MARGIN = 0.4    # ... unless another leftover comes this close to explaining it too
GAP_SURE = 0.3        # the only leftover between two paired frames needs this much of the same words
NEAR_TELL = 0.35      # leftovers this alike are worth a word in the report, though nothing pairs them
TWIN_TIE = 0.02       # an alignment this close to the best one is a second reading, not a worse one
DROPPED_FRAME = 1e-4  # a tie between a slide the source still describes and one it dropped goes live
KEY_MATCH = 0.5       # least similarity for an element keeping the key it would get anyway
ELEMENT_MATCH = 0.35  # least similarity for an element inheriting another key
# Render output, not source: "picture" says how a bare image reached its file (raw stream or
# PDFium's pixels); the bytes themselves are hashed by image_sha1.
DROP_KEYS = {"id", "spans", "file", "px", "picture", "drawings", "drawing"}
STYLE_KEYS = {"font", "family", "size", "bold", "italic", "smallcaps", "color", "fill", "stroke", "shape", "align",
              "level", "script", "underline", "strike", "highlight", "link", "opacity", "shadow", "radius", "code",
              "flip", "weight", "arrow_from", "arrow_to", "rotation"}
ID_LIKE = re.compile(r"p\d+([a-z]+\d+.*)")
UNIQUE_ROLES = ("title", "footer")  # one per slide: matched by role and place whatever their words


def sha1(data: bytes | str) -> str:
    return hashlib.sha1(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def norm_title(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def run_text(runs: list[dict]) -> str:
    return "".join(HOLE_MARK if r.get("hole") else r["text"] for r in runs)


def plain_text(el: dict) -> str:
    """The words of an element, for fingerprints and text diffs."""
    kind = el["kind"]
    if kind == "text":
        return "\n".join(run_text(p["runs"]) for p in el["paragraphs"])
    if kind == "table":
        return "\n".join("\t".join(run_text(cell).strip() for cell in row) for row in el["cells"])
    if kind == "diagram":
        return "\n".join(" ".join(run_text(runs).strip() for runs in n["paragraphs"]) for n in el["nodes"])
    if kind == "image" and el.get("number"):
        return el["number"]["text"]
    return ""


def slide_title(slide: dict) -> str:
    return next((plain_text(e) for e in slide["elements"] if e["kind"] == "text" and e.get("role") == "title"), "")


def slide_text(slide: dict) -> str:
    return " ".join(plain_text(e) for e in slide["elements"])


def slide_info(slide: dict) -> dict:
    return {"label": slide.get("label"), "title": slide_title(slide), "text": slide_text(slide), "page": slide["page"]}


def fresh_slide_key(info: dict, taken: set[str]) -> str:
    if info.get("label"):
        key = info["label"]
    elif norm_title(info.get("title") or ""):
        stem = f"title:{norm_title(info['title'])}"
        key = next(f"{stem}#{k}" for k in range(1, 10 ** 6) if f"{stem}#{k}" not in taken)
    else:
        key = f"page:{info['page'] + 1}"
    base, k = key, 2
    while key in taken:
        key, k = f"{base}~{k}", k + 1
    return key


def slide_keys(infos: list[dict]) -> list[str]:
    """Keys of a first conversion (nothing to inherit)."""
    taken: set[str] = set()
    out = []
    for info in infos:
        out.append(fresh_slide_key(info, taken))
        taken.add(out[-1])
    return out


def slide_similarity(a: dict, b: dict) -> float:
    ratio = SequenceMatcher(None, a["text"].split(), b["text"].split(), autojunk=False).ratio()
    same_title = bool(norm_title(a["title"])) and norm_title(a["title"]) == norm_title(b["title"])
    return ratio + (0.5 if same_title else 0.0)


def label_pairs(base: list[dict], ours: list[dict]) -> dict[int, int]:
    """ours index -> base index by label alone.

    A label can name several slides - every overlay step of a frame carries the frame's label, so
    `--overlays all` gives one per step - and then the n-th slide of that label pairs with the
    n-th in the base. Pairing them all with one base slide would leave its siblings unpaired, and
    an unpaired base slide is one the source dropped: the deck would lose a step per sync."""
    pairs: dict[int, int] = {}
    base_labels: dict[str, list[int]] = {}
    for i, b in enumerate(base):
        if b.get("label"):
            base_labels.setdefault(b["label"], []).append(i)
    for j, o in enumerate(ours):
        free = base_labels.get(o.get("label") or "")
        if free:
            pairs[j] = free.pop(0)
    return pairs


def _title_alike(a: dict, b: dict) -> float:
    """1 for the same title, less for one edited, 0 for another title. `slide_similarity` asks the
    question as yes or no, which is right for an alignment; here it is not, because a source that
    retitles every frame in the same breath as it moves a label ("Results" -> "Results v2") is
    exactly the edit this has to see through."""
    ta, tb = norm_title(a["title"]), norm_title(b["title"])
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    ratio = SequenceMatcher(None, ta.split(), tb.split(), autojunk=False).ratio()
    return ratio if ratio >= 0.6 else 0.0


def _evidence(a: dict, b: dict) -> float:
    """How much two slides look like the same frame, where there is something to look at. Two
    frames that say almost nothing - a full-page picture, a section divider - are alike by default,
    and that is no reason to believe one of them is the other."""
    alike = _title_alike(a, b)
    if not alike and min(len(a["text"].split()), len(b["text"].split())) < 4:
        return 0.0
    return SequenceMatcher(None, a["text"].split(), b["text"].split(), autojunk=False).ratio() + 0.5 * alike


def _moved_bar(a: dict, b: dict) -> float:
    """`LABEL_MOVED` on the scale this pair is scored on.

    `_evidence` is the word ratio plus half of what the titles agree on, so what it can score at
    all depends on the pair: 1.5 where the titles match, 1.0 for two slides nobody gave a title.
    A flat bar of 1.0 therefore asks for most of the words and the same title on a talk, and for
    *word for word* on a deck `adopt` wrote - where half the slides have no title at all and a
    label, being the only identity there is, is the very thing this check exists to doubt. So the
    bar is the same share of what the pair could say, which is what `_complete` does for exactness
    one line below. Measured (`fuzz_labels --shape adopt`, 1400 rounds chained 4 deep): five of
    the eleven rounds that wrote a frame onto the wrong slide in silence are a label swapped
    between two untitled slides whose rival reading scores 0.88 to 0.96 where the label's own
    scores 0.06 to 0.39 - unmistakable, and unreachable from under 1.0."""
    return LABEL_MOVED * (1.0 + 0.5 * _title_alike(a, b)) / 1.5 - 1e-9


def _scaled(score: float, a: dict, b: dict) -> float:
    """`score` put back on the scale a pair whose titles agree is scored on, which is the one the
    constants are written for - `_moved_bar` the same idea as a bar rather than a score.

    A margin is a *difference* between two readings, and the two are readings of different pairs:
    `here` scores the label's slide against another frame, `own` against the frame carrying the
    label. Where those pairs can say different amounts the difference is measured in two units at
    once - a rival that says word for word what an untitled slide said reaches 1.0 and no further,
    so beating a titled pairing of 0.8 by `LABEL_MARGIN` is arithmetically out of reach whatever
    it says. A deck `adopt` wrote is made of such pairs (half its slides have no title at all), and
    it is the deck where a label is the only identity there is.

    Measured over four adopt-shaped campaigns run twice with only this swapped (1000 rounds four
    deep at label-chance 1 and 0.5, two seeds each): frames written onto the wrong slide 174 ->
    **154** of 61,344, misidentified 421 -> 391, `unsure` verdicts 115 -> 94 and four of the five
    questions the sound rounds asked gone; one campaign's silent rounds went 3 -> 4 and another's
    writes 16 -> 18, which is the same coin - a bar a pair can actually reach is reachable by a
    wrong reading too. Converted decks write the same 26 either way."""
    return score * 1.5 / (1.0 + 0.5 * _title_alike(a, b))


def _complete(a: dict, b: dict, score: float) -> bool:
    """Whether `_evidence` has nothing left to hold against these two: the same words, word for
    word. The most it can say depends on the pair - 1 for the words, plus half of what the titles
    already say - so "nothing left" is a question about this pair, not a number to compare with."""
    return bool(a["text"].split()) and score >= 1.0 + 0.5 * _title_alike(a, b) - 1e-9


def label_moves(base: list[dict], ours: list[dict]) -> list[dict]:
    r"""Labels that look as if they moved to another frame (docs/sync.md, "When a label moved").

    A label is a promise: the frame that carries it is the frame the deck's slide was made from.
    Rename one, or paste `[label=intro]` onto the next frame, and following it would carry a
    person's edits onto a slide they never touched. Nothing in the PDF says this happened - the
    label is simply somewhere else - so the only witness is the content on both sides.

    For each label the base and the source share, the check asks whether the two slides say the
    same thing. Short of `LABEL_SURE`, or short of word for word, they do not quite, and then it
    looks for a better explanation among the slides nothing else accounts for: does the source's
    labelled frame look like some *other* base slide (`frame_is`), and does the base's labelled
    slide look like some *other* source frame (`slide_is`)? An explanation counts only if it is
    good on its own (`LABEL_MOVED`) *and* better than the label's own pairing by `LABEL_MARGIN`,
    or word for word right where the label's own pairing is not, or part of an exchange - two
    readings pointing at each other (`exchanged`) - which is what keeps an overlay step, a
    retitled frame or a frame edited hard from setting this off.

    - both, clearly: `moved`. The label is ignored for pairing and the content decides, which is
      also where the person's edits belong - they edited those words, not that label.
    - one of the two: `unsure`. Something in the deck looks exactly like what this label used to
      name - which is either a label that moved, or a passage the author moved from one frame to
      another. Nothing is re-paired and the report asks.
    - neither: silence. A frame whose text was rewritten from scratch looks exactly like this, and
      that is a plausible edit, not a broken invariant.

    Both verdicts are reported as conflicts: which frame is which is a question with an answer,
    and guessing it wrong is the one mistake in this program that quietly loses somebody's work.
    """
    pairs = label_pairs(base, ours)
    own = {j: _evidence(base[i], ours[j]) for j, i in pairs.items()}
    # `LABEL_SURE` says a pairing needs no second opinion, and on a deck of near-twins a label
    # swapped between two of them clears it on both sides: the frame says most of what the slide
    # says and carries its title, so the pairing scores 1.25 and the check never looks. A pairing
    # that is not *word for word* right still has something left to explain, however alike it is,
    # so it is in doubt too. Nothing below changes for it: the margin cannot be met from up there
    # (`LABEL_SURE` + `LABEL_MARGIN` is above what `_evidence` can score), so the only thing that
    # can speak against such a pairing is exactness - another slide saying word for word what this
    # frame says, or another frame saying word for word what this slide said.
    doubt = sorted(j for j, s in own.items()
                   if s < LABEL_SURE or not _complete(base[pairs[j]], ours[j], s))
    if not doubt:
        return []
    # A pairing in doubt settles nothing, so both of its slides are free to be somebody else's
    # partner - which is what lets a pair of labels swapped between two frames be seen at all.
    settled_base = {i for j, i in pairs.items() if j not in doubt}
    settled_ours = {j for j in pairs if j not in doubt}
    free_base = [i for i in range(len(base)) if i not in settled_base]
    free_ours = [j for j in range(len(ours)) if j not in settled_ours]

    def best(scored):
        top = (0.0, None)
        for score, k in scored:
            if score > top[0]:
                top = (score, k)
        return top

    def among_best(scored, k: int) -> bool:
        """Whether `k` is one of the best readings there are, ties counted.

        `best` keeps the first of several equal scores, which is the lower index and nothing else.
        That is harmless where a number is wanted and wrong where the *identity* is: two slides
        that say word for word the same thing are two readings of one score (`TWIN_TIE`, the same
        tie `align_slides` names as a coin toss), and a look back that refuses because the arbitrary
        one of them came first is refusing on the strength of a slide order."""
        top, mine = 0.0, None
        for score, x in scored:
            top = max(top, score)
            if x == k:
                mine = score
        return mine is not None and mine > 0.0 and top - mine <= TWIN_TIE

    # A slide carrying this frame's own label is another overlay step of this very frame, not
    # another frame: `--overlays all` keeps one slide per step, they say word for word what the
    # step before said plus a bullet, and a step dropped or added leaves the one beside it
    # explaining the pairing exactly. That is the frame itself, so it explains nothing about
    # where its label went (`test_overlay_steps_of_one_frame_are_never_a_move`).
    def apart(a: dict, b: dict) -> bool:
        """Not two overlay steps of one frame. A label names a frame, so the same label on both
        sides is that frame itself; two *unlabelled* slides say nothing of the kind, and reading
        `None == None` as one frame would hide half the deck from the check."""
        return not a.get("label") or a.get("label") != b.get("label")

    def rivals(i: int, j: int) -> tuple[float, int | None, float, int | None]:
        """The best explanation on each side of the pairing (label j, slide i): the frame that
        looks most like this slide, and the slide that looks most like this frame."""
        here, slide_is = best((_evidence(base[i], ours[k]), k) for k in free_ours
                              if k != j and apart(base[i], ours[k]))
        there, frame_is = best((_evidence(base[k], ours[j]), k) for k in free_base
                               if k != i and apart(ours[j], base[k]))
        return here, slide_is, there, frame_is

    def exchanged(j: int) -> bool:
        """Do the two readings this pairing is up against point back at *it*?

        A frame the source merely reworded looks a bit like half the deck, and one loud rival says
        little. Two that agree say something else: the frame explaining this label's slide belongs
        on the slide explaining this frame, each looking there before anywhere else - which is a
        label and a frame that changed places, and nothing else known to produce it. So inside such
        an exchange the bar is `LABEL_EXCHANGE` rather than `LABEL_MARGIN`, which no swap between
        near-twins can ever meet. Innocent twins do *not* clear even that: they tie (the twin
        explains the slide exactly as well as the label's own pairing does), and a tie is 0 - which
        is the whole of what that bar has to say, so it is *a tie* (`TWIN_TIE`) and not a number of
        its own. A swap between two frames a revision has reworded is a hair either way: the
        campaign's silent misidentifications at 0.1 were pairs where the crossing read 1.447 and
        the label's own reading 1.419, and what tells that from an innocent pair of twins is not
        0.03 of evidence but the look back below.

        The other frame carries no label of its own here. `[label=intro]` pasted onto the next
        frame is how a label moves in practice, and the frame it left is then unlabelled, so
        nothing pairs it and there is no second pairing to notice the exchange from.
        """
        i = pairs[j]
        here, slide_is, there, frame_is = look[j]
        if slide_is is None or frame_is is None:
            return False
        if here < _moved_bar(base[i], ours[slide_is]) or there < _moved_bar(base[frame_is], ours[j]):
            return False
        mine = _scaled(own[j], base[i], ours[j])
        if min(_scaled(here, base[i], ours[slide_is]),
               _scaled(there, base[frame_is], ours[j])) - mine < LABEL_EXCHANGE:
            return False
        # Nothing is left out of the look back, though `rivals` leaves out the pairing's own two
        # sides: the frame that best explains the slide this frame would move to may well be the
        # twin standing beside it, and then the two readings are not about each other at all -
        # they are two frames that both look like the deck's other half
        # (`test_a_frame_reworded_on_a_deck_of_twins_is_no_exchange`).
        back_i = among_best(((_evidence(base[s], ours[slide_is]), s) for s in free_base
                             if apart(ours[slide_is], base[s])), i)
        back_j = among_best(((_evidence(base[frame_is], ours[m]), m) for m in free_ours
                             if apart(base[frame_is], ours[m])), j)
        return back_i and back_j

    look = {j: rivals(pairs[j], j) for j in doubt}
    found: dict[str, dict] = {}
    for j in doubt:
        i = pairs[j]
        here, slide_is, there, frame_is = look[j]
        # Two frames that say nearly the same thing cannot be told apart by `LABEL_MARGIN`: the
        # pairing a label swapped between them leaves behind is already 0.85 alike, and nothing
        # can beat that by half. What such a move does do is come out *exact* - word for word -
        # while the label's own pairing is not. A frame edited hard, an overlay step or a retitled
        # frame is exact on neither side, which is why exactness is allowed to stand in for the
        # margin and nothing looser is.
        #
        # The two sides are not worth the same, though, and only one of them may stand alone:
        #  - `there_exact`, the frame carrying the label says word for word what some other,
        #    unclaimed slide said. For that to be innocent the *source* must have rewritten this
        #    frame into a copy of another one - a real edit, and one worth a question either way.
        #  - `here_exact`, some other frame says word for word what this label's slide said. On a
        #    deck of twins that is true without anybody editing anything: the untouched twin
        #    always explains it. So it counts only together with the other side, which is what a
        #    swap looks like (`test_one_twin_edited_is_not_a_swap`).
        own_exact = _complete(base[i], ours[j], own[j])
        here_exact = slide_is is not None and not own_exact and _complete(base[i], ours[slide_is], here)
        there_exact = frame_is is not None and not own_exact and _complete(base[frame_is], ours[j], there)
        trade = exchanged(j)
        # The margin is a difference between two readings of *different* pairs, so both are put on
        # one scale first (`_scaled`): a rival that says word for word what an untitled slide said
        # cannot beat a titled pairing by half a point, whatever it says.
        mine = _scaled(own[j], base[i], ours[j])
        strong = (slide_is is not None and here >= _moved_bar(base[i], ours[slide_is])
                  and (trade or _scaled(here, base[i], ours[slide_is]) - mine >= LABEL_MARGIN
                       or (here_exact and there_exact)),
                  frame_is is not None and there >= _moved_bar(base[frame_is], ours[j])
                  and (trade or _scaled(there, base[frame_is], ours[j]) - mine >= LABEL_MARGIN
                       or there_exact))
        if not any(strong):
            continue
        label = ours[j]["label"]
        found.setdefault(label, {
            "label": label, "verdict": "moved" if all(strong) else "unsure", "ours": j, "base": i,
            "similarity": round(own[j], 2), "base_title": base[i]["title"], "ours_title": ours[j]["title"],
            "slide_is": slide_is if strong[0] else None, "slide_score": round(here, 2) if strong[0] else None,
            "frame_is": frame_is if strong[1] else None, "frame_score": round(there, 2) if strong[1] else None,
        })
    return list(found.values())


def crossed_twins(base: list[dict], ours: list[dict], pairs: dict[int, int]) -> dict[int, str]:
    """Pairings that cross over slides the words cannot tell apart.

    Two slides that say word for word the same thing - a deck `adopt` wrote is full of them - leave
    `label_moves` nothing to work with: the reading where the two labels swapped and the reading
    where they did not score *exactly* alike, so neither can explain the other and the check is
    right to say nothing. What does differ is the order: the labels cross where the slides do not.
    A crossing the words account for is a frame the author moved; one they cannot account for at
    all is a coin toss of the kind `weak["twins"]` already names for an unlabelled frame, and its
    other reading is `[label=q3]` pasted onto the frame below.

    So the label is followed - it is the promise, and nobody's edits move either way - and the
    person is told which two slides it was (`fuzz_labels --shape adopt`, seeds 7100725 and
    7100896: a label swapped between two slides whose base entries are word for word identical,
    where every reading ties and the sync wrote each frame onto the other's slide in silence).

    `crossed` is two labels that changed places; `traded` is a label that crossed a frame carrying
    none, which is the commoner half of it and the one nothing named - `[label=q3]` pasted onto the
    twin below leaves the frame it came from unlabelled, so the *alignment* pairs that one and
    `weak["twins"]` cannot see the tie either, the slide it would have wanted having been taken by
    the label (adopt-shaped seed 1400358 at chain 4: a label moved onto the twin of its own slide,
    each frame written onto the other's slide in silence). Which is why this is asked of the whole
    pairing and not of the labels alone - and why it is asked only where both sides say enough to
    be an explanation at all (`_moved_bar`), or two slides that say nothing would tie as surely as
    two that say the same thing.

    And what the crossing has to be is that it **loses nothing**, not that it ties. Asking for a
    tie was asking the two slides to say word for word the same thing, which one revision rewording
    either of them takes away - and then the crossed reading is a hair better or a hair worse than
    the one the labels took, which says nothing about which is right and everything about which
    words were changed last. `label_moves` has already had its say about a reading that is better
    by enough to act on (`LABEL_MARGIN`, or an exchange); between that and a tie there was nothing
    at all, and the campaign's remaining silent writes lived in the gap, crossing with 0.03 to 0.42
    between the two readings. So each frame need only read at least as well (within `TWIN_TIE`)
    against the other's slide as against its own. It costs a deck nobody reordered nothing, the
    loop never reaching past the order: measured over the four adopt-shaped campaigns (1,000 rounds
    four deep, label-chance 1 and 0.5, two seeds each), run twice over the same seeds with only
    this swapped, frames written onto another frame's slide with nothing in the report naming them
    go **14 -> 7, in 12 -> 7 rounds**, not one frame moves (109 written and 3 either way, 308
    costly, `moved` and `unsure` unchanged), and the price is 22 warnings in the 12,037 broken
    rounds and **none at all** in the 3,963 sound ones."""
    out: dict[int, str] = {}
    order = sorted(pairs)
    for x, j1 in enumerate(order):
        for j2 in order[x + 1:]:
            i1, i2 = pairs[j1], pairs[j2]
            if i1 < i2:                 # the pairings keep the order: nothing to wonder about
                continue
            if not (ours[j1].get("label") or ours[j2].get("label")):
                continue                # two unlabelled frames are the alignment's own business
            if (_evidence(base[i1], ours[j1]) < _moved_bar(base[i1], ours[j1])
                    or _evidence(base[i2], ours[j2]) < _moved_bar(base[i2], ours[j2])):
                continue
            if (_evidence(base[i2], ours[j1]) >= _evidence(base[i1], ours[j1]) - TWIN_TIE
                    and _evidence(base[i1], ours[j2]) >= _evidence(base[i2], ours[j2]) - TWIN_TIE):
                # reading the two frames the other way round loses nothing, so the words did not
                # decide this; that they might decide it the *other* way is `label_moves`' business
                how = "crossed" if ours[j1].get("label") and ours[j2].get("label") else "traded"
                out[j1] = out[j2] = how
    return out


def align_slides(base: list[dict], ours: list[dict], moves: list[dict] | None = None,
                 weak: dict[int, str] | None = None) -> dict[int, int]:
    """ours index -> base index. Labelled frames pair by label wherever they moved; the others by
    an order-keeping alignment on (title, text) similarity, so an inserted frame shifts nothing.
    A label the content says has moved to another frame (`label_moves`) is not followed: its two
    slides go into the alignment with the rest.

    `weak`, if given, is filled with the pairings nothing quite proved and how: "content"
    (`cross_pairs`) or "place" (`gap_pairs`), the two leftover passes, which are inferences the
    alignment itself could not draw, "twins" - an unlabelled frame the alignment could have
    put on another slide for the same score - and "crossed", two labels that changed places over
    slides the words cannot tell apart, or "traded", one label that crossed a frame carrying none
    (`crossed_twins`). A report that says so lets the author put a label there instead, or put one
    back where it was."""
    if moves is None:
        moves = label_moves(base, ours)
    dropped = {m["ours"] for m in moves if m["verdict"] == "moved"}
    pairs = {j: i for j, i in label_pairs(base, ours).items() if j not in dropped}
    freed = {m["base"] for m in moves if m["verdict"] == "moved"}
    bs = [i for i in range(len(base)) if i not in pairs.values()]
    os_ = [j for j in range(len(ours)) if j not in pairs]
    m, n = len(bs), len(os_)
    theirs_labels = {o["label"] for o in ours if o.get("label")}
    base_labels = {b["label"] for b in base if b.get("label")}

    def pairable(i: int, j: int) -> bool:
        """May an unpaired base slide and an unpaired source frame be the same frame? Two labels
        that both exist on both sides belong to two frames that both exist, and pairing across
        them would be reading one as the other. Everything else is allowed to go by the content:
        a label added or removed, the two slides of a label this run found moved, and a label
        renamed - each side's label unknown to the other, so nothing else can claim either slide
        and the words are all there is to go on."""
        bl, ol = base[i].get("label"), ours[j].get("label")
        if not (bl and ol) or bl == ol or j in dropped or i in freed:
            return True
        return bl not in theirs_labels and ol not in base_labels

    allow = [[pairable(bs[a], os_[b]) for b in range(n)] for a in range(m)]
    # A base entry the source dropped, kept alive by the deck's own edits (`sync.new_base`'s
    # `keep_removed`), still says what that frame said and has lost its label, and the rebase puts
    # it back beside the slide the frame really lives on. So it reads exactly as well as that slide
    # for the frame that carries the words - and the traceback, handed two alignments of one score,
    # takes the earlier: the frame flipped onto the dead entry and the next sync planned to *delete*
    # the slide it had just written, with the person's edits on it (converted seed 2100403 at chain
    # 6). Between two readings that tie, the one the source still describes wins; the penalty is a
    # hair, so a frame the source brings back still re-pairs with its kept slide when nothing else
    # explains it, and a real difference in the words decides as it did.
    sim = [[slide_similarity(base[bs[a]], ours[os_[b]])
            - (DROPPED_FRAME if base[bs[a]].get("removed") else 0.0) for b in range(n)]
           for a in range(m)]
    score = [[0.0] * (n + 1) for _ in range(m + 1)]
    for a in range(m - 1, -1, -1):
        for b in range(n - 1, -1, -1):
            best = max(score[a + 1][b], score[a][b + 1])
            if sim[a][b] >= SLIDE_MATCH and allow[a][b]:
                best = max(best, sim[a][b] + score[a + 1][b + 1])
            score[a][b] = best
    a = b = 0
    chosen = {}
    while a < m and b < n:
        s = sim[a][b]
        if s >= SLIDE_MATCH and allow[a][b] and abs(score[a][b] - (s + score[a + 1][b + 1])) < 1e-9:
            pairs[os_[b]] = bs[a]
            chosen[b] = a
            a, b = a + 1, b + 1
        elif score[a + 1][b] >= score[a][b + 1]:
            a += 1
        else:
            b += 1
    # An alignment of equal score is an alignment the traceback could as well have picked, and on a
    # deck of look-alike slides there are several: a frame with no label, between twins, pairs with
    # whichever of them the walk reaches first. Nothing downstream can tell that from a match made
    # on the words, so the person is told instead (fuzz_labels seed 32773 --shape adopt: the source
    # deleted one of four near-identical slides, the unlabelled frame after it took the deleted
    # slide's place, and the sync wrote it over a slide the person had edited, in silence).
    # `pre` is the same alignment read forwards, so a pairing lies on *some* best alignment exactly
    # when what leads to it plus what follows it adds up to the best score there is.
    if weak is not None and chosen:
        pre = [[0.0] * (n + 1) for _ in range(m + 1)]
        for a in range(1, m + 1):
            for b in range(1, n + 1):
                best = max(pre[a - 1][b], pre[a][b - 1])
                if sim[a - 1][b - 1] >= SLIDE_MATCH and allow[a - 1][b - 1]:
                    best = max(best, pre[a - 1][b - 1] + sim[a - 1][b - 1])
                pre[a][b] = best

        def optimal(a: int, b: int) -> bool:
            return (sim[a][b] >= SLIDE_MATCH and allow[a][b]
                    and score[0][0] - (pre[a][b] + sim[a][b] + score[a + 1][b + 1]) <= TWIN_TIE + 1e-9)

        # And the same question asked of the *pair* rather than of the alignment: is there another
        # leftover slide whose words read as well against this frame as the one it got? An
        # order-keeping walk cannot offer the crossing where the source carried a frame past its
        # near-twin - that reading is unrealisable, so it scores worse and `optimal` is silent -
        # while a person looking at the two slides sees exactly the coin toss the other half names.
        # Measured as the union, since a rival on some best alignment need not tie pairwise (four
        # adopt-shaped campaigns, 1,000 rounds four deep, run twice over the same seeds with only
        # this swapped): of the 109 frames written onto another frame's slide, those written with
        # nothing in the report naming them go 21 -> 14, in 17 -> 12 rounds; not one frame moves
        # (109 and 3 either way, no new `moved` or `unsure`), and the price is 7 more warnings in
        # 3,963 sound rounds. The label condition is factored over both readings, not dropped:
        # `merge.plan_merge` prints this warning only `and not o.get("label")` and its sentence
        # opens "this frame has no label", so a `twins` on a frame whose label the source renamed -
        # which is in `os_` carrying one - would be counted as spoken for and said to nobody.
        for b, a in chosen.items():
            if ours[os_[b]].get("label"):
                continue
            alike = any(allow[other][b] and sim[other][b] >= SLIDE_MATCH
                        and sim[other][b] >= sim[a][b] - TWIN_TIE
                        for other in range(m) if other != a)
            if alike or any(optimal(other, b) for other in range(m) if other != a):
                weak[os_[b]] = "twins"
    # One after the other, each seeing what the one before it took: written as one tuple, both
    # passes were handed the *same* leftovers and could claim the same slide - and did (offline
    # fuzz seed 5521: the source swapped two frames, `cross_pairs` recognised one of them by its
    # words, `gap_pairs` gave the other the same base slide because it was the only one left
    # between two paired neighbours). Two frames with one key is the one thing nothing downstream
    # survives: sync wrote both frames onto that one slide, the other slide's picture was gone and
    # the report said no slide had been created. The filter keeps the promise in the open - this
    # mapping is one slide to one frame, whatever a pass believes.
    for how, pass_ in (("content", cross_pairs), ("place", gap_pairs)):
        found = {}
        for j, i in pass_(base, ours, pairs, pairable).items():
            if j not in pairs and i not in pairs.values() and i not in found.values():
                found[j] = i
        pairs.update(found)
        if weak is not None:
            weak.update(dict.fromkeys(found, how))
    # Asked of the finished pairing, not of the labels alone: the frame a label crossed over need
    # carry no label itself, and then it is the *alignment* that pairs it (`crossed_twins`).
    if weak is not None:
        for j, how in crossed_twins(base, ours, pairs).items():
            weak.setdefault(j, how)
    return pairs


def cross_pairs(base: list[dict], ours: list[dict], pairs: dict[int, int], pairable) -> dict[int, int]:
    """The leftovers of the order-keeping pass, paired where one frame explains one slide and
    nothing else comes close.

    The alignment keeps the order, so a frame the source moved across another can't be paired by
    it: of the two, one keeps the chain and the other falls out - read as a new frame, while the
    slide it was made from is read as deleted. Nobody loses a word that way (a deck-edited slide is
    kept), but the person's edits end up on a slide beside the one the source now writes, which is
    the same harm a label pointing at the wrong frame does.

    So the slides left over are matched to the frames left over, by content alone and only when the
    content is sure: the best explanation must be good on its own (`CROSS_SURE`, the same evidence
    the moved-label check weighs) and no other leftover may come within `CROSS_MARGIN` of it, on
    either side. A deck of frames that all say the same thing (the stress deck's three Results)
    pairs nothing here and is left to the order, as before."""
    free_base = [i for i in range(len(base)) if i not in set(pairs.values())]
    free_ours = [j for j in range(len(ours)) if j not in pairs]
    if not free_base or not free_ours:
        return {}
    scores = {(j, i): _evidence(base[i], ours[j]) for j in free_ours for i in free_base if pairable(i, j)}
    out: dict[int, int] = {}
    for (j, i), score in sorted(scores.items(), key=lambda kv: -kv[1]):
        if score < CROSS_SURE or j in out or i in out.values():
            continue
        rivals = [s for (b, a), s in scores.items() if (b == j) != (a == i) and b not in out and a not in out.values()]
        if max(rivals, default=0.0) > score - CROSS_MARGIN:
            continue
        out[j] = i
    return out


def gap_pairs(base: list[dict], ours: list[dict], pairs: dict[int, int], pairable) -> dict[int, int]:
    """One slide left over between two frames that paired, one frame left over between the same
    two, and some of the same words: the place is evidence the words alone don't have.

    An unlabelled frame that was retitled keeps neither its title nor a label, and half its words
    are no longer enough for the alignment (`SLIDE_MATCH`) or for `cross_pairs`, which has to be
    sure of itself because it may pair across the whole talk. Here there is nothing to be unsure
    between: both neighbours are pinned, on both sides, and the gap they leave holds exactly one
    slide and exactly one frame. What it cannot be is a frame deleted and another written in its
    place, and that is what `GAP_SURE` is for - some of the words have to be the same words.

    Where that line sits is measured, not guessed (`tools/fuzz_labels.py`): anything from 0.2 to
    0.45 gives the same tally, under 0.2 the pass starts fusing frames that have nothing to do with
    each other, and the stress deck's own retitled-and-half-rewritten frame scores 0.45 - so the
    value sits in the middle of what the campaign allows rather than at the edge of it."""
    free_base = [i for i in range(len(base)) if i not in set(pairs.values())]
    free_ours = [j for j in range(len(ours)) if j not in pairs]
    if not free_base or not free_ours:
        return {}
    # The ends count as gaps too: the talk before the first frame that paired and after the last
    # one are bounded by the start and the end of both sides, which pin a slide just as well.
    anchors = [(-1, -1), *sorted(pairs.items()), (len(ours), len(base))]   # (ours, base), in ours order
    out: dict[int, int] = {}
    for k in range(len(anchors) - 1):
        (jl, il), (jr, ir) = anchors[k], anchors[k + 1]
        if ir <= il:                                                  # the two crossed: no gap to speak of
            continue
        here = [j for j in free_ours if jl < j < jr]
        there = [i for i in free_base if il < i < ir]
        if len(here) != 1 or len(there) != 1:
            continue
        j, i = here[0], there[0]
        if pairable(i, j) and _evidence(base[i], ours[j]) >= GAP_SURE:
            out[j] = i
    return out


def near_misses(base: list[dict], ours: list[dict], pairs: dict[int, int]) -> list[dict]:
    """A frame the source seems to have written and a slide it seems to have dropped that look like
    each other - not enough for any pass above to pair them, enough that a person would ask.

    Every pass here refuses to guess, and refusing leaves a deck with the old slide (the person's
    edits still on it) beside a new one carrying the frame's new words. Nothing is lost, and that is
    the point of refusing - but if the two really are one frame, the author is the only one who can
    say so, and nobody told them the question was asked. So the leftovers on both sides are paired
    off by content one last time, purely to be reported: `NEAR_TELL` is low on purpose, well under
    every threshold that decides anything, because a warning that costs a sentence should fire
    while the evidence is still weak. What it must not do is fire at a genuinely new frame beside a
    genuinely dropped one, and that is measured (`tools/fuzz_labels.py --chain`): over 2000 chained
    revisions it speaks 4 times, 3 of them about frames the pairing really did lose - and of the 4
    lost frames whose slide was still free to be named, the fourth shares not one word with it, so
    there is nothing to say. Anywhere from 0.3 to 0.5 gives that same tally; below 0.3 it is noise
    only, and above it the stress deck's own retitled-and-half-rewritten frame (0.448) falls out."""
    free_base = [i for i in range(len(base)) if i not in set(pairs.values())]
    out = []
    for j in range(len(ours)):
        if j in pairs or not free_base:
            continue
        score, i = max((_evidence(base[i], ours[j]), i) for i in free_base)
        if score >= NEAR_TELL:
            out.append({"ours": j, "base": i, "evidence": round(score, 3)})
            free_base.remove(i)     # one slide can only be one frame
    return out


def inherit_slide_keys(base: list[dict], base_keys: list[str], ours: list[dict],
                       moves: list[dict] | None = None,
                       weak: dict[int, str] | None = None) -> tuple[list[str], dict[int, int]]:
    """Keys for ours slides (matched ones inherit the base key) and the match (ours -> base index).
    `weak`: see `align_slides`."""
    pairs = align_slides(base, ours, moves, weak)
    taken = set(base_keys)
    keys = []
    for j, info in enumerate(ours):
        if j in pairs:
            keys.append(base_keys[pairs[j]])
        else:
            keys.append(fresh_slide_key(info, taken))
            taken.add(keys[-1])
    return keys, pairs


# ---------------------------------------------------------------- elements

def image_sha1(el: dict, out: Path | None) -> str | None:
    if el["kind"] != "image" or not el.get("file") or out is None:
        return None
    path = out / el["file"]
    return sha1(path.read_bytes()) if path.exists() else None


def fingerprint(el: dict, out: Path | None = None, anchor_key: str | None = None) -> dict:
    return {"text": plain_text(el), "bbox": [round(v, 2) for v in el["bbox"]], "image_sha1": image_sha1(el, out),
            "anchor": anchor_key}


def default_keys(elements: list[dict]) -> list[str]:
    counts: dict[str, int] = {}
    out = []
    for el in elements:
        stem = f"{el['kind']}/{el.get('role') or 'none'}"
        out.append(f"{stem}/{counts.get(stem, 0)}")
        counts[stem] = counts.get(stem, 0) + 1
    return out


def _geometry(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    iou = ix * iy / union if union > 0 else 0.0
    dist = ((a[0] + a[2] - b[0] - b[2]) ** 2 + (a[1] + a[3] - b[1] - b[3]) ** 2) ** 0.5 / 2
    return max(iou, max(0.0, 1 - dist / 60))


def element_similarity(a: dict, b: dict) -> float:
    """a, b: {"kind", "role", "fingerprint"}."""
    if a["kind"] != b["kind"]:
        return 0.0
    fa, fb = a["fingerprint"], b["fingerprint"]
    geom = _geometry(fa["bbox"], fb["bbox"])
    role = 1.0 if a.get("role") == b.get("role") else 0.0
    if fa["text"] or fb["text"]:
        ratio = SequenceMatcher(None, fa["text"], fb["text"], autojunk=False).ratio()
        score = 0.6 * ratio + 0.3 * geom + 0.1 * role
        if role and a.get("role") in UNIQUE_ROLES:  # a renamed title is still the slide's title
            score = max(score, 0.5 + 0.3 * geom + 0.2 * ratio)
        return score
    if a["kind"] == "image":
        same = 1.0 if fa["image_sha1"] and fa["image_sha1"] == fb["image_sha1"] else 0.0
        anchor = 1.0 if fa.get("anchor") == fb.get("anchor") else 0.0
        return 0.4 * same + 0.3 * geom + 0.2 * anchor + 0.1 * role
    return 0.7 * geom + 0.3 * role


def match_elements(base: list[dict], ours: list[dict], reserved: set[str] = frozenset()) -> list[str]:
    """Keys for ours elements ({"kind", "role", "fingerprint"}), given base elements ({"key",
    "kind", "role", "fingerprint"}): most similar pairs first (the key an element would get anyway
    counts slightly more and needs KEY_MATCH, others ELEMENT_MATCH), else a fresh ordinal.
    `reserved` keys are taken already."""
    by_key = {b["key"]: b for b in base if b["key"] not in reserved}
    keys: list[str | None] = [None] * len(ours)
    used: set[str] = set(reserved)
    defaults = default_keys(ours)
    candidates = []
    for j, o in enumerate(ours):
        for b in by_key.values():
            s = element_similarity(o, b)
            if s >= (KEY_MATCH if b["key"] == defaults[j] else ELEMENT_MATCH):
                candidates.append((s + (0.05 if b["key"] == defaults[j] else 0.0), j, b["key"]))
    for s, j, key in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
        if keys[j] is None and key not in used:
            keys[j] = key
            used.add(key)
    ordinals: dict[str, int] = {}
    for key in [b["key"] for b in base] + list(used):
        stem, _, n = key.rpartition("/")
        ordinals[stem] = max(ordinals.get(stem, 0), int(n) + 1 if n.isdigit() else 0)
    for j, o in enumerate(ours):
        if keys[j] is None:
            stem = f"{o['kind']}/{o.get('role') or 'none'}"
            keys[j] = f"{stem}/{ordinals.get(stem, 0)}"
            ordinals[stem] = ordinals.get(stem, 0) + 1
    return keys


def slide_element_keys(elements: list[dict], out: Path | None, base: list[dict] | None = None) -> tuple[list[str], list[dict]]:
    """Keys and fingerprints of a slide's IR elements; with `base` (the matched base slide's
    elements: {"key", "kind", "role", "fingerprint"}) keys are inherited. Elements anchored to
    text are matched after their anchors, with the anchor's key in their fingerprint."""
    ids = {e["id"]: i for i, e in enumerate(elements)}
    fps = [fingerprint(e, out) for e in elements]
    if base is None:
        keys = default_keys(elements)
        for e, fp in zip(elements, fps):
            if e.get("anchor") in ids:
                fp["anchor"] = keys[ids[e["anchor"]]]
        return keys, fps
    items = [{"kind": e["kind"], "role": e.get("role"), "fingerprint": fp} for e, fp in zip(elements, fps)]
    free = [i for i, e in enumerate(elements) if e.get("anchor") not in ids]
    anchored = [i for i, e in enumerate(elements) if e.get("anchor") in ids]
    keys = [""] * len(elements)
    for i, k in zip(free, match_elements([b for b in base if not b["fingerprint"].get("anchor")], [items[i] for i in free])):
        keys[i] = k
    for i in anchored:
        fps[i]["anchor"] = keys[ids[elements[i]["anchor"]]]
    got = match_elements([b for b in base if b["fingerprint"].get("anchor")], [items[i] for i in anchored],
                         {keys[i] for i in free} | {b["key"] for b in base if not b["fingerprint"].get("anchor")})
    for i, k in zip(anchored, got):
        keys[i] = k
    return keys, fps


# ---------------------------------------------------------------- IR hashes

def normalise_ir(value, anchor_key: str | None = None, page_key=None):
    """The element's IR without ids and page-specific numbering (links to pages become slide keys)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in DROP_KEYS:
                continue
            if k == "anchor":
                out[k] = anchor_key
            else:
                out[k] = normalise_ir(v, anchor_key, page_key)
        return out
    if isinstance(value, list):
        return [normalise_ir(v, anchor_key, page_key) for v in value]
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, str):
        if value.startswith("#page=") and value[6:].isdigit() and page_key:
            return f"#slide={page_key(int(value[6:]))}"
        m = ID_LIKE.fullmatch(value)
        return m.group(1) if m else value
    return value


def _styles(value, found: set) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k in STYLE_KEYS and not isinstance(v, (dict, list)):
                found.add((k, json.dumps(v)))
            elif k in STYLE_KEYS:
                found.add((k, json.dumps(v, sort_keys=True)))
            elif k not in ("bbox", "lines", "text"):
                _styles(v, found)
    elif isinstance(value, list):
        for v in value:
            _styles(v, found)


def ir_fields(el: dict, out: Path | None = None, anchor_key: str | None = None, page_key=None) -> tuple[str, dict]:
    """(ir_hash, {"text", "position", "size", "style", "image"} field hashes) of an element. A
    reworded line changes its size, not its position."""
    norm = normalise_ir(el, anchor_key, page_key)
    image = image_sha1(el, out)
    whole = sha1(json.dumps([norm, image], sort_keys=True, ensure_ascii=False))[:16]
    styles: set = set()
    _styles(norm, styles)
    x0, y0, x1, y1 = el["bbox"]
    fields = {
        "text": sha1(plain_text(el))[:12],
        "position": sha1(json.dumps([round(2 * x0) / 2, round(2 * y0) / 2]))[:12],
        "size": sha1(json.dumps([round(2 * (x1 - x0)) / 2, round(2 * (y1 - y0)) / 2]))[:12],
        "style": sha1(json.dumps(sorted(styles)))[:12],
        "image": (image or "")[:12],
    }
    return whole, fields


# Fields `sync.mark_emitted` gives both sides of an element whose own IR the source left alone but
# which emit now writes differently because of what stands around it: "width" (a text box's frame),
# "placed" (a picture's predicted place), "emitted" (anything else of its requests).
CONTEXT_FIELDS = ("width", "placed", "emitted")


def source_changes(base_el: dict, ours_el: dict) -> set[str]:
    """Fields the source changed ({"text", "position", "size", "style", "image"}, or {"layout"} when only
    something else in the IR differs); empty when the IR hash is the same. A `CONTEXT_FIELDS` one
    too when emit writes the element differently because of what stands around it
    (`sync.mark_emitted`), which only counts when both sides carry the mark."""
    marked = {f for f in CONTEXT_FIELDS if base_el["fields"].get(f) is not None
              and ours_el["fields"].get(f) is not None and base_el["fields"][f] != ours_el["fields"][f]}
    if base_el["ir_hash"] == ours_el["ir_hash"]:
        return marked
    changed = {k for k, v in ours_el["fields"].items() if k not in CONTEXT_FIELDS and base_el["fields"].get(k) != v}
    return (changed or {"layout"}) | marked
