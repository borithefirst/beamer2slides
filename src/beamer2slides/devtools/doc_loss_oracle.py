"""Did anything a person put in the document disappear without being accounted for?

That is the whole question this module answers about one `docs sync`, and it is worth
saying exactly what each word of it means, because the value of a fuzz campaign is
never better than the judgement at the end of it.

**A person** is the reader: whoever typed in the Google Doc between the last sync and
this one. Not the source. The file in git is a *request*; the document is *work*. The
merge is allowed to refuse a request (it reports a conflict and moves on) and is never
allowed to throw work away in silence.

**Put in the document** is anything the read-back before the sync shows and the base
does not, or shows differently from the base: words typed, a block added, a block
dragged somewhere else, a word bolded, a row typed into, a chip or an equation
inserted, a picture dropped in, a list renumbered. The reader's own deletions are not
work to be preserved — they are work already done, and the oracle never asks for
deleted text to come back.

**Disappear** is judged on the read-back *after* the sync has settled, which is the
read that becomes the new file and the new base. Not on the plan, not on the requests:
on what the document says when the command has finished. A block is gone when its key
is gone and nothing in the document says what it said; a word is gone when it is in no
block of that tab; a frozen run (chip, equation, picture) is gone when no run of that
tab has its kind and its value. Moving something is not losing it — except for order
itself, where putting a block back where the reader took it from *is* the loss, and it
is reported as `undo`.

**Without being accounted for** is the escape hatch, and it is deliberately generous:
the sync may do any of it as long as the report says so. A conflict naming the block's
key, a note, a line under "Left alone" — any of them turns a `loss` into silence. The
merge's contract is not "never overwrite the reader", it is "never overwrite the reader
without telling them", and a report the person can read is how they get their work back
out of the file's history. A finding therefore means one of two things: the sync lost
something, or the sync lost something *and did not say so*. Both are bugs; the second
is the worse one.

What this module does **not** judge: whether the source's changes arrived (that is the
campaign's job — a sync that writes nothing loses nothing and is still useless), and
whether the document looks right. Severities follow `loss_oracle.py`: `loss` (work
gone), `undo` (work put back the way it was), `report` (the sync's own account of
itself is wrong), `note` (worth printing, not a failure).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

from .. import doc_ir, doc_merge

SEVERITIES = ("loss", "undo", "report", "note")
FAIL = ("loss", "undo", "report")

WORD = re.compile(r"\S+")
# The run marks, which are the only styling a named style can put on a word here.
MARK_KEYS = {key for key, _ in doc_ir.MARK_FIELDS}


def finding(kind: str, severity: str, detail: str, tab=None, key=None, block=None) -> dict:
    return {"kind": kind, "severity": severity, "detail": detail,
            "tab": tab, "key": key, "block": block}


# ---------------------------------------------------------------- reading an IR

def parts_by_tab(ir: dict | None) -> dict:
    """{tab id (None for the first): part} for a whole IR.

    A `<section>` with no `data-tab` is a tab the *file* is asking for and the
    document has never had (`_fresh_asks`), so it has no id — and keyed by
    `part.get("tab")` it landed on `None`, where the first tab is, and the last one
    written won. Every question asked of the body then got another tab's part
    instead: the file side of it in `check`, and all four sides in
    `fuzz_docs._arrived`, which is how the judges came to be silent about the body of
    two rounds in three (the campaign's `add_tab` draws). The body keeps `None` and a
    tab nobody can name gets a slot of its own, which matches nothing on the other
    sides — the honest answer, since nothing on the other sides can name it either.
    """
    if not ir:
        return {}
    out = {}
    for at, part in enumerate(doc_ir.parts(ir)):
        tab = None if part is ir else part.get("tab") or f"?{at}"
        out.setdefault(tab, part)
    return out


def keyed(part: dict | None) -> dict:
    return {b["key"]: b for b in (part or {}).get("blocks", []) if b.get("key")}


def unkeyed(part: dict | None) -> list[dict]:
    return [b for b in (part or {}).get("blocks", []) if not b.get("key")]


def text_of(block: dict) -> str:
    """Everything a block says, cells included.

    A frozen run gets a space each side: a chip's own words sit right against the text
    beside it (`doc_ir.runs_text` gives "plain tailgrace"), and a chip the source adds
    would otherwise look as if it had eaten the word in front of it. Runs the reader's
    styling split, on the other hand, are joined as they are, or "follows." would come
    apart into two words the moment somebody bolded the full stop.
    """
    if block.get("kind") == "table":
        return " ".join(text_of(b) for row in block.get("rows", [])
                        for cell in row for b in cell)
    return "".join(f" {r['text']} " if r.get("frozen") else r["text"]
                   for r in block.get("runs", []))


def own_words(block: dict) -> str:
    """What a block says in its own right: the frozen runs left out.

    A chip's **face is the document's to draw** — the file says `Grace`, Docs renders
    `grace` off the address, and a date chip re-inserted from its value comes back in
    whatever form the document spells a date in. So the face is not words anybody
    typed, and a chip that goes is caught by `frozen_marks`, which counts it by what
    identifies it (kind and value) and not by what it reads as. Counting the face
    made a block the source merely *moved* — a delete and a write, so every chip in it
    is inserted again — read as losing the words `Sep 20, 2026` that the reader's own
    Backspace had brought into it (chain-8 seed 280398). `_says` in `fuzz_docs` is the
    same subtraction, asked from the other side.
    """
    if block.get("kind") == "table":
        return " ".join(own_words(b) for row in block.get("rows", [])
                        for cell in row for b in cell)
    return "".join(" " if r.get("frozen") else r["text"]
                   for r in block.get("runs", []))


def part_text(part: dict | None) -> str:
    return " ".join(text_of(b) for b in (part or {}).get("blocks", []))


def words(text: str) -> Counter:
    return Counter(WORD.findall(text))


PIECE = re.compile(r"\w+")


def _split(token: str) -> list[str]:
    return [p for p in PIECE.findall(token) if len(p) > 1]


def stands_elsewhere(text: str, whole: str) -> bool:
    r"""Whether every word of `text` is still somewhere in `whole`.

    What both resurrection checks ask before calling something deleted, and the same
    forgiveness `joined_differently` grants a token, granted to a whole block. The
    words are looked for *inside* `whole`, not among its tokens, because `WORD` is
    `\S+` and a reader's drag glues the text it carries to whatever it lands in: a
    paragraph dropped inside the full stop of the one before it says "section.."
    where the base said "section." (chain-8 seeds 64057, 64084), and one that held a
    chip comes down without it, closing the gap it left ("harbourgrace" for "harbour
    grace", seed 64166 — no `insertText` retypes a chip). Neither is a block that
    went away. Erring this way costs a finding; the other way cries wolf on every
    move.
    """
    return all(piece in whole for token in WORD.findall(text)
               for piece in _split(token))


def joined_differently(token: str, after: Counter, was: Counter,
                       tab_was: Counter | None = None,
                       theirs: Counter | None = None) -> bool:
    r"""A token the reader typed is not lost when the words *they* put in it are still
    there, joined to something else.

    `WORD` is `\S+`, so a soft hyphen, a slash or an em dash makes one token out of two
    words, and the two sides can each rewrite one half of it. Then the merge is right
    to produce a token neither side ever typed, and the reader's work is inside it:
    chain-8 seed 5130, shape `astral` — the base said `soft\xadhyphen`, the reader
    typed `quartz\xadhyphen`, the source `soft\xadzephyr`, and the document ended up
    saying `quartz\xadzephyr`. Both edits arrived. Calling that a loss is the oracle's
    mistake, not the sync's.

    What must survive is only what the reader added, measured against the base
    (`was`): the other half is the source's to change. Nothing real gets through — a
    token of one word must still survive whole, and a word the sync really dropped is
    in no token of the tab at all.

    A reader may also *delete* one of the joined words, and then the token left over
    reads as one they typed — `theirs - was` compares whole tokens, and `\xadhyphen`
    is not `soft\xadhyphen`. It holds nothing of theirs: its words are the base's, and
    the source is entitled to rewrite them, which is what happened at fresh-seed 91197
    (the reader deleted `soft`, the source made `hyphen` into `willow`, and the merge
    said `\xadwillow` — both edits arrived). `_pared_down` asks it exactly: this token
    is a token of the base with one of its words taken out, nothing else.

    There were two more of these, `_dressed_up` and `_undressed`, for a token dressed
    in or stripped of punctuation of the reader's — and both were written for damage
    the *harness* was doing. `read_move_block` read its drop index off the document
    before the cut, so a drag landed some way past where the reader let go: inside a
    word, which is how a full stop came to stand against the `1` in a cell and how one
    came off the end of `section.`. The drop is a paragraph mark now and nothing it
    inserts can land inside a token at all, so neither excuse fires any more — 0 of 800
    rounds at chain 4, 0 of 500 at chain 8, 0 of 400 at chain 12 and 0 of the 60
    regression seeds need them — and they are gone. A forgiveness for something that no
    longer happens is a blind spot waiting, which is what `_twin_unmarks` was.
    """
    pieces = _split(token)
    if len(pieces) < 2 and not _pared_down(token, was, theirs) \
            and not _welded(token, after, tab_was if tab_was is not None else was):
        return False
    base = {piece for other in was for piece in _split(other)}
    there = {piece for other in after for piece in _split(other)}
    return all(piece in there for piece in pieces if piece not in base)


def _pared_down(token: str, was: Counter, theirs: Counter | None = None) -> bool:
    """Whether this token is a token of the base with one of its joined words deleted.

    Exactly that, and nothing looser: the base token with the span of one of its word
    pieces cut out has to *equal* the token. A word of the base the reader typed again
    somewhere new is then still their own work and still has to survive.

    The cut may take the joiner with it, which is the half this missed. Deleting
    `soft` out of `soft\xadhyphen` leaves `\xadhyphen` if the reader stopped at the
    word and plain `hyphen` if they swept the soft hyphen up too — the same deletion
    either way, and only the first was recognised, so the second read as a word they
    had typed and the source's rewriting of the other half looked like a loss
    (chain-6 seed 94030: the reader deleted `a soft\xad`, `collide` made `hyphen`
    into `kestrel`, the merge said `kestrel` and both edits were in it). Only the
    joiners *between* two of the token's words are cut with it; punctuation at the
    ends is not cut at all.

    And only when the narrow leftover is not standing there as well: a base token
    pared down once leaves one token, so if `\xadhyphen` is in the reader's text
    then that is the paring and a bare `hyphen` beside it is a word they typed
    (`theirs`, the guard the test of this function has always asserted).
    """
    for other in was:
        spans = [m.span() for m in PIECE.finditer(other)]
        if len(spans) < 2:
            continue
        for i, (start, end) in enumerate(spans):
            narrow = other[:start] + other[end:]
            if narrow == token:
                return True
            if theirs is not None and theirs.get(narrow):
                continue
            lefts = (start, spans[i - 1][1]) if i else (start,)
            rights = (end, spans[i + 1][0]) if i + 1 < len(spans) else (end,)
            if any(other[:a] + other[b:] == token for a in lefts for b in rights):
                return True
    return False


def _welded(token: str, after: Counter, was: Counter) -> bool:
    """Whether this token is two tokens of the base pushed together, one of which the
    source has since rewritten.

    A reader who joins two paragraphs welds the last token of the first to the first
    token of the second with nothing between them: "And prose after that." and "A line
    the source can move." become "…after that.A line…", and `that.A` is a token the
    base never had, so `theirs - was` reads it as a word the reader typed. It holds
    nothing of theirs — both its words are the base's — and the source may rewrite
    either. That is chain-8 seed 93212, where `collide` made `that.` into `vellum.`,
    the merge said `vellum.A`, and the reader's join and the source's wording were
    both in it.

    `joined_differently`'s general rule covers this already whenever both halves carry
    a word of two letters or more; `_split` drops a one-letter piece, so a paragraph
    beginning "A" falls straight through it. Exact, like the others, and the same
    condition: one of the two base tokens has to be gone from the tab as well, since
    that is what made the welded token disappear.
    """
    for other in was:
        for rest in (token[:-len(other)] if token.endswith(other) else None,
                     token[len(other):] if token.startswith(other) else None):
            if rest and rest in was and not (after.get(other) and after.get(rest)):
                return True
    return False


def runs_of(block: dict):
    if block.get("kind") == "table":
        for row in block.get("rows", []):
            for cell in row:
                for inner in cell:
                    yield from runs_of(inner)
        return
    yield from block.get("runs", [])


def frozen_key(run: dict) -> tuple:
    """What a frozen run is, to a reader.

    A picture is the file it shows, not the object id Docs gave it: a block the sync
    rewrites is deleted and written again, and the picture that comes back is the same
    picture under a new id. Asking for the id back would make every legitimate rewrite
    look like a loss. Everything else is its kind and its value (an equation's LaTeX,
    a person chip's address).
    """
    if run.get("chip") == "image":
        return ("image", run.get("sha") or run.get("src") or run.get("uri")
                or run.get("value", ""))
    return (run.get("chip", "object"), run.get("value") or run.get("text", ""))


def image_names(run: dict) -> set:
    """Every name that says which picture this is — and it takes all of them.

    Neither half of `frozen_key`'s rule holds on its own across one sync. The object
    id goes when the block is rewritten, which is why the key is the file. But the
    *file* goes too: a picture the reader inserted in the browser has only a `uri`
    before the sync, and the settle saves it and gives it a `src` and a digest
    (`doc_sync.fetch_pictures`) — the document did not change, the name we know it by
    did. Judged on one name the oracle called every such picture lost and had done
    since it was written, which was thirteen of the twenty findings left in a chain-8
    run: a false accusation hides the true ones behind it.
    """
    return {("image", value) for value in (run.get("sha"), run.get("src"),
                                           run.get("uri"), run.get("value")) if value}


def images_of(part: dict | None) -> list[dict]:
    return [run for block in (part or {}).get("blocks", []) for run in runs_of(block)
            if run.get("frozen") and run.get("chip") == "image"]


def telling_names(runs: list[dict]) -> list[set]:
    """Of each picture's names, the ones that say *which* picture it is: the names no
    other picture standing beside it carries.

    A reader who pastes the same image twice gives two pictures one `uri`, and that
    uri then names neither of them. Matched on it, one insertion could stand in for
    the other: the picture that went was paired with the one that stayed, the one
    that stayed was left over, and the oracle named the survivor as lost (chain-8
    seed 7049 and six of its neighbours). What is left may be empty - two copies of
    one file really are indistinguishable - and those are matched by count instead.
    """
    seen = Counter(name for run in runs for name in image_names(run))
    return [{name for name in image_names(run) if seen[name] == 1} for run in runs]


def pair_images(now: list[dict], then: list[dict], ids: bool = False) -> list[int | None]:
    """For each picture the document held, the one it has now, or None.

    Two passes: a telling name first, then any name at all, so a picture keeps its
    own counterpart where one exists and two copies of one file still pair off one
    for one. Each survivor is claimed once.

    `ids` refuses a pairing between two object ids that differ, which is only ever
    true of the *file*: the settle regenerates it from the document it wrote, so a
    picture the file names by id is that very object, while a picture it names by
    file alone is one the source has just added and the document has never held. The
    document's own pictures may not be asked for their ids across a sync (a rewrite
    deletes and inserts, and the picture comes back under a new one), which is why
    the other pairing goes by name.
    """
    telling = telling_names(now)
    every = [image_names(run) for run in then]
    hit: list[int | None] = [None] * len(now)
    free = set(range(len(then)))

    def may(i: int, j: int) -> bool:
        mine, theirs = now[i].get("value"), then[j].get("value")
        return not (ids and mine and theirs and mine != theirs)

    for names in (telling, [image_names(run) for run in now]):
        for i in range(len(now)):
            if hit[i] is not None or not names[i]:
                continue
            at = next((j for j in sorted(free) if names[i] & every[j] and may(i, j)), None)
            if at is not None:
                hit[i] = at
                free.discard(at)
    return hit


def frozen_marks(block: dict) -> Counter:
    """What a block holds that a plain paragraph cannot say, by what identifies it.
    Pictures are left out: they are matched by name, not counted (`image_names`)."""
    out: Counter = Counter()
    if block.get("kind") == "toc":
        out[("toc", "")] += 1
    for run in runs_of(block):
        if run.get("frozen") and run.get("chip") != "image":
            out[frozen_key(run)] += 1
    return out


def frozen_runs(part: dict | None) -> dict:
    """One representative run per frozen mark, so `doc_merge.writable` can be asked
    whether a request could ever put it back."""
    out = {}
    for block in (part or {}).get("blocks", []):
        if block.get("kind") == "toc":
            out.setdefault(("toc", ""), {"chip": "toc", "frozen": True})
        for run in runs_of(block):
            if run.get("frozen"):
                out.setdefault(frozen_key(run), run)
    return out


def core(word: str) -> str:
    """A word without the punctuation clinging to either end.

    What a reader selects when they bold a word is the word, and `WORD` is `\\S+`, so
    the full stop the *source* then writes after it comes along in the token. The
    mark is on every character of `zephyr` and on none of `zephyr.`, so the rule
    below — a word wears what every character of it wears — read the bold as gone
    while it sat there in the document (chain-8 seed 870308, shape `two_tables`: the
    reader bolded `zephyr`, the source's `collide` reworded the sentence around it
    and the merge wrote `lantern zephyr` bold and `.` plain).

    Punctuation only (Unicode `P*`), which leaves a soft hyphen or a slash where it
    is: those join two words rather than dressing one, and `joined_differently` is
    the forgiveness written for them. A token that is all punctuation is its own
    core, or an em dash would have none.
    """
    start, end = _core_span(word)
    return word[start:end]


def _core_span(word: str) -> tuple[int, int]:
    start, end = 0, len(word)
    while start < end and unicodedata.category(word[start]).startswith("P"):
        start += 1
    while end > start and unicodedata.category(word[end - 1]).startswith("P"):
        end -= 1
    return (start, end) if start < end else (0, len(word))


def _under_words(block: dict):
    r"""Each word of a block with the runs under its characters.

    A word is what a reader sees, so it is read off the block's whole text and not
    run by run — the same rule as `text_of`, and for the same reason. Asked run by
    run, a run boundary was a word boundary: the merge writing the source's strike on
    the words the file has left the reader's freshly typed word in a run of its own,
    and the token `\xadvellum` the reader had coloured came apart into `\xad` and
    `vellum`, so the colour read as lost although every character still wore it
    (fresh seed 970528, chain 6). A frozen run is a gap with a space each side, as
    `text_of` has it and as skipping the run used to make it.

    And it is the word, not the punctuation the source parks against it (`core`).
    """
    text, under = _chars_under(block)
    for match in WORD.finditer(text):
        start, end = _core_span(match.group())
        runs = [r for r in under[match.start() + start:match.start() + end]
                if r is not None]
        if runs:
            yield match.group()[start:end], runs


def _chars_under(block: dict) -> tuple[str, list]:
    """A block's text and, per character, the run it came from (None where frozen)."""
    if block.get("kind") == "table":
        text, under = "", []
        for row in block.get("rows", []):
            for cell in row:
                for inner in cell:
                    body, fields = _chars_under(inner)
                    if text:
                        text, under = text + " ", under + [None]
                    text, under = text + body, under + fields
        return text, under
    text, under = "", []
    for run in block.get("runs", []):
        body = run.get("text", "")
        if run.get("frozen"):
            text, under = text + f" {body} ", under + [None] * (len(body) + 2)
        else:
            text, under = text + body, under + [run] * len(body)
    return text, under


def _wears(block: dict, theme: dict | None) -> set:
    """The marks this block's named style puts on, which is what its runs inherit."""
    if not block.get("kind") or block.get("kind") == "table":
        return set()
    return MARK_KEYS & set((theme or {}).get(doc_merge.named_style(block), ()))


def marks_on(block: dict, theme: dict | None = None) -> Counter:
    """What each of a block's words *wears*, by (mark, word): the marks chosen for it
    and the ones its named style puts on, less the ones a run says False to.

    This is the reader's view rather than the file's, so it is what answers "is the
    bold back on?" — a run that says nothing under a bold theme is bold again. Asked
    without a theme it is what somebody *chose* for a word, which is the other half
    of the styling question.

    One mark at a time, and the value rather than the key: a run saying `bold: False`
    is a reader who took the bold off (`doc_ir.MARK_FIELDS`), and counting the key
    would read that as a reader who put bold on — and then call the un-bolding a loss
    the moment it was honoured. Counting whole *sets* of marks was the same mistake
    one turn out: the source adding its strike to a word made the tuple the reader's
    colour was in disappear, so the colour read as lost while it sat there (fresh
    seed 970528). A word split across runs wears what every character of it wears
    (`_under_words`).

    What a word inherits is deliberately not in here when no theme is given. A
    theme's bold belongs to the named style, so a block that becomes a heading (Docs
    hands the paragraph after a deleted one the style of the one that went) would
    otherwise read as the reader bolding its every word, and any later restyle as
    losing that (themed seed 283).
    """
    wears = _wears(block, theme)
    out: Counter = Counter()
    for word, under in _under_words(block):
        marks = set.intersection(*({k for k in set(run) | wears
                                    if k not in ("text", "width")
                                    and (run[k] if k in run else True)}
                                   for run in under))
        for mark in marks:
            out[(mark, word)] += 1
    return out


def unmarked_of(block: dict, theme: dict | None = None) -> Counter:
    """The marks a reader deliberately took *off* a word, by (mark, word).

    Only an explicit False counts, and only against a named style that puts the mark
    on: that is a reader pressing Ctrl+B on a bold heading, and it is the one thing
    the absence of a mark can never be told from. A block that merely stopped being a
    heading — which a move in the document can do — lost the mark and chose nothing.
    """
    wears = _wears(block, theme)
    out: Counter = Counter()
    for word, under in _under_words(block):
        for mark in set.intersection(*({key for key in wears if run.get(key) is False}
                                       for run in under)):
            out[(mark, word)] += 1
    return out


def cells_of(block: dict) -> dict:
    return {(r, c): " ".join(text_of(b) for b in cell)
            for r, row in enumerate(block.get("rows", []))
            for c, cell in enumerate(row)}


# ---------------------------------------------------------------- the report

def accounted(report: dict) -> str:
    """What the report says *about the reader's work*, as one haystack to look a key
    up in: the conflicts and the notes.

    Not "applied" and not "kept": `doc_sync._summary` writes one of those per block, so
    taking them as an account of a loss would excuse every block in the document and
    leave the oracle with nothing to say. "`x` rewritten" is the sync telling us it
    merged — if the reader's sentence came out of that merge missing, that is exactly
    the bug, and the report has not named it.
    """
    info = report or {}
    lines = [json.dumps(c, ensure_ascii=False) for c in info.get("conflicts", [])]
    lines += [str(x) for x in info.get("notes", [])]
    return "\n".join(lines)


def _named(said: str, *what) -> bool:
    return any(str(x) and str(x) in said for x in what)


# ---------------------------------------------------------------- the checks

def check(base: dict, before: dict, after: dict, report: dict,
          ours: dict | None = None, allow=(), theme: dict | None = None) -> list[dict]:
    """Judge one sync. `before` is the document as the reader left it, `after` the
    settled read, `base` what both sides last agreed on, `ours` the file that was
    synced. `allow` names kinds to keep out of the verdict.

    `theme` is what the document's named styles say, as
    `{namedStyleType: {the IR fields that style sets}}`. Given it, the verdict also
    covers what a theme is made of — a paragraph wearing a named style and made to
    stop (`_inherited_findings`). Nothing in a `documents.get` answer says a
    paragraph inherits, only that it sets nothing, so the caller has to say."""
    said = accounted(report)
    out: list[dict] = []
    was, now, then = parts_by_tab(base), parts_by_tab(before), parts_by_tab(after)
    file_tabs = parts_by_tab(ours) if ours is not None else {}
    for tab, part in now.items():
        if tab is not None and tab not in then:
            # A tab the source dropped goes, exactly as a block it dropped does, as
            # long as the reader left it as the base had it. Anything else — a tab the
            # reader wrote in, or one the file still asks for — is a loss the report
            # has to name.
            dropped = ours is not None and tab not in file_tabs
            kept = was.get(tab) is not None \
                and part_text(was.get(tab)) == part_text(part) \
                and (was[tab].get("title") == part.get("title"))
            if not (dropped and kept) and not _named(said, tab, part.get("title")):
                out.append(finding("tab_gone", "loss",
                                   f"the tab {part.get('title')!r} is not in the document any "
                                   f"more and the report does not say why", tab=tab))
            continue
        out += _tab_findings(was.get(tab), part, then.get(tab), said, tab,
                             doc_ir.tab_part(ours, tab) if tab else ours, theme)
        out += _title_findings(was.get(tab), part, then[tab], said, tab)
    out += _resurrection_findings(was, now, then, file_tabs, said, ours)
    return [f for f in out if f["kind"] not in allow]


def _fresh_asks(ours: dict | None) -> Counter:
    """How many tabs of each title the *source* is asking for outright.

    A `<section>` with no `data-tab` is a tab the file wants and the document has
    never had (`doc_ir.parts`), so the sync creating one is the merge doing its job.
    They cannot be read off `parts_by_tab`, which keys by tab id and puts every one
    of them on `None` beside the first tab.
    """
    return Counter(part.get("title") for part in doc_ir.parts(ours)[1:]
                   if not part.get("tab")) if ours else Counter()


def _resurrection_findings(was: dict, now: dict, then: dict, file_tabs: dict,
                           said: str, ours: dict | None = None) -> list[dict]:
    """A tab the reader deleted that the sync put back.

    Nothing of the reader's *disappears* here, so every other question in this file
    passes it: the content is all present, the report converges, and a tab that comes
    back with a new id slips past anything that asks for the old one. What is undone
    is the reader's own deletion, which is a decision they made in the browser and
    the whole bargain says the document wins on.

    Only the shape the merge could actually produce is accused: the file still asks
    for that tab (`<section data-tab>`), so the source never dropped it either, and a
    tab now stands in the document, unknown to the base, saying the same thing under
    the same name. A tab the source freshly asks for *is* word for word that shape —
    two new empty tabs say exactly as much as each other — so each such ask answers
    for one of the new tabs before any of them is called a resurrection (`_fresh_asks`;
    chain-8 seed 65370, two `add_tab`s drawing the same name).
    """
    asked, out = _fresh_asks(ours), []
    for tab, old in was.items():
        if tab is None or tab in now or tab not in file_tabs:
            continue
        title = old.get("title")
        twins = [p for t, p in then.items()
                 if t is not None and t not in was and t not in now
                 and p.get("title") == title and part_text(p) == part_text(old)]
        if not twins:
            continue
        if asked[title] >= len(twins):
            asked[title] -= len(twins)
            continue
        if not _named(said, title):
            out.append(finding("tab_resurrected", "loss",
                               f"the reader deleted the tab {title!r} and it is "
                               f"back after the sync; the report does not say why", tab=tab))
    return out


def _tab_name(part: dict | None, tab) -> str:
    """What a tab calls itself. The first tab's `title` is the *document's* name, so
    its own is `tab_title` (`doc_ir.TAB_META`)."""
    return ((part or {}).get("title") if tab else (part or {}).get("tab_title")) or ""


def _title_findings(was: dict | None, now: dict, then: dict, said: str, tab) -> list[dict]:
    """A tab the reader renamed and the sync renamed back.

    Only the reader's own rename is theirs to lose: a source rename over a title the
    document left as the base had it is the merge doing its job, and it lands in
    `applied`, which `accounted` deliberately does not read. So the question is the
    narrow one — the reader renamed it, and it is called something else now — which is
    the both-sides case, and the merge answers that with a note.
    """
    mine, after = _tab_name(now, tab), _tab_name(then, tab)
    if mine == _tab_name(was, tab) or after == mine or _named(said, mine):
        return []
    return [finding("tab_renamed", "loss",
                    f"the tab the reader named {mine!r} is called {after!r} now and the "
                    f"report does not say why", tab=tab)]


def _tab_findings(was: dict | None, now: dict, then: dict | None, said: str,
                  tab, mine: dict | None, theme: dict | None = None) -> list[dict]:
    out: list[dict] = []
    if was:
        # A table the reader beheaded is not a table the reader *made*. Deleting a
        # table's first row takes its named range with it (`doc_ir.anchor_span`), so
        # the read-back has it unkeyed — and `unkeyed` below is the oracle's word for
        # "the reader added this", whose whole content then has to survive. It is the
        # base's table, and the merge knows it again (`doc_merge.recover_tables`), so
        # the oracle asks the same question and judges it by its key: the source's own
        # edit to a cell the reader kept is then a change and not a loss.
        doc_merge.recover_tables(was, now)
    old, new = keyed(was), keyed(then)
    live = keyed(now)
    after_text = part_text(then)
    after_words = words(after_text)
    after_frozen: Counter = Counter()
    for block in (then or {}).get("blocks", []):
        after_frozen += frozen_marks(block)
    after_styles: Counter = Counter()
    for block in (then or {}).get("blocks", []):
        after_styles += marks_on(block)
    source_keys = {b["key"] for b in (mine or {}).get("blocks", []) if b.get("key")}
    file_blocks = keyed(mine) if mine else {}

    for key, block in live.items():
        theirs_text = text_of(block)
        base_block = old.get(key)
        touched = base_block is None or text_of(base_block) != theirs_text
        if key not in new:
            # The block is gone. Fair when the reader left it exactly as the base had
            # it and the source deleted it; otherwise the reader's work went with it.
            excused = (not touched and key in old and key not in source_keys) \
                or _named(said, key)
            if excused or not theirs_text.strip():
                continue
            mine_words = words(theirs_text)
            typed = mine_words - (words(text_of(base_block)) if base_block else Counter())
            if not (typed - after_words) and key in source_keys and _stands(key, block, mine, then):
                # Nothing the reader typed is missing and the block still stands whole
                # somewhere — one paragraph's words are not a sentence the sync split up
                # and scattered. What went is the identity the file carries (its `id=`),
                # so the next diff renames a paragraph nobody touched, and a
                # hand-written id is gone for good.
                out.append(finding(
                    "identity_lost", "report",
                    f"the block {key} said {theirs_text[:40]!r} and has lost its key, "
                    f"though neither side dropped the block",
                    tab=tab, key=key))
            else:
                out.append(finding(
                    "block_gone", "loss",
                    f"the block {key} said {theirs_text[:60]!r} and is not in the document "
                    f"any more; the report does not account for it"
                    + (", though the file still names it" if key in source_keys else ""),
                    tab=tab, key=key))
            continue
        out += _words_findings(key, block, base_block, new[key], after_words, said, tab,
                               words(part_text(was)))
        out += _style_findings(key, block, base_block, after_styles, new[key],
                               after_words, said, tab, theme, file_blocks.get(key),
                               dropped=mine is not None and not touched
                               and key in old and key not in source_keys)
        out += _inherited_findings(key, block, new[key], (mine or {}), said, tab, theme,
                                   now)
        out += _shape_findings(key, block, base_block, new[key], said, tab,
                               behind_dropped=_behind(was, key) is not None
                               and _behind(was, key) not in source_keys)
        if block.get("kind") == "table":
            out += _cell_findings(key, block, base_block, new[key], after_words, said, tab,
                                  words(part_text(was)))
            out += _row_resurrection_findings(key, block, base_block, new[key], said, tab,
                                              file_blocks.get(key))

    before_text = part_text(now)
    for key, was_block in old.items():
        # The mirror of `block_gone`, and the same shape as `tab_resurrected`: the
        # reader deleted this block in the browser, the file still asks for it, and it
        # stands in the document again after the sync. Nothing of theirs disappeared,
        # so every question above passes it — what was undone is the deletion itself,
        # which the document is supposed to win. A block the reader merely *emptied*
        # is not one they deleted (its range, and so its key, is still there), and a
        # key the merge handed to another block is `identity_lost`, not this.
        if key in live or key not in new or key not in file_blocks:
            continue
        # A key missing from the read-back does not mean the block is: a reader who
        # *moves* a block deletes its text and types it again, which destroys the
        # named range, and the settle then keys the block from its own words as
        # before. So the question is asked of the words, not of the key — and of the
        # block that carries the key *after* the sync, not of the one the base
        # remembers: if what stands there now was standing there before, nothing came
        # back at all. Asked of the base's text instead, everything the reader's own
        # hand took out of that block on the way read as a resurrection — a chip no
        # retype carries, a word they went on to delete (chain-4 seed 66195, where a
        # dragged paragraph came down without the chip the source had put in it).
        if stands_elsewhere(text_of(new[key]), before_text):
            continue
        if _named(said, key, text_of(was_block)[:40]):
            continue
        out.append(finding("block_resurrected", "loss",
                           f"the reader deleted the block {key}, {text_of(was_block)[:60]!r}, "
                           f"and it is back after the sync; the report does not say why",
                           tab=tab, key=key))

    for block in unkeyed(now):
        text = text_of(block).strip()
        lost = words(text) - after_words
        if not text or not lost or _named(said, text[:40]):
            continue
        out.append(finding("block_gone", "loss",
                           f"a {'table' if block.get('kind') == 'table' else 'block'} "
                           f"the reader added, {text[:60]!r}, is not in the "
                           f"document any more", tab=tab))

    before_frozen: Counter = Counter()
    for block in now.get("blocks", []):
        if _source_dropped(block, was, mine):
            continue
        before_frozen += frozen_marks(block)
    mine_frozen: Counter = Counter()
    for block in (mine or {}).get("blocks", []):
        mine_frozen += frozen_marks(block)
    runs = frozen_runs(now)
    for mark, count in before_frozen.items():
        kind, value = mark
        # A picture or a chip the source itself took out of the file is meant to go;
        # an equation, a dropdown or a table of contents can never come back, so it
        # only goes when the report says it did. By *count*, because the same email or
        # the same file may stand in two places and the source may drop one of them
        # (chain-4 seed 309: two `grace@example.com` chips, one block dropped).
        remakeable = doc_merge.writable(runs.get(mark, {"chip": kind, "frozen": True}))
        want = min(count, mine_frozen.get(mark, 0)) if remakeable and mine is not None \
            else count
        if after_frozen.get(mark, 0) >= want or _named(said, value, kind):
            continue
        out.append(finding("frozen_gone", "loss",
                           f"the {kind} {value!r} the document held is not in it any more"
                           + ("" if remakeable else ", and no request can make one"),
                           tab=tab))

    out += _picture_findings(now, mine, then, said, tab)
    out += _order_findings(was, now, then, said, tab)
    return out


def _twin_already(now_part: dict, block: dict, after_block: dict, field, value) -> bool:
    """Whether the block this key names *after* the sync is another one that already
    stood in the document saying these words with this field set this way.

    A key is not a block. The reader pastes a copy of a paragraph beside it — which
    lands in the style of what it was dropped into, as Docs' own rule has it — the
    source drops the one the key was on, and the settle keys the copy by its words to
    the name that went. Nothing was written to it and nothing of anybody's was lost:
    the field is "of its own" because it always was. Asked of the words the key names
    afterwards, so a twin that says something else answers for nothing (themed seed
    2030066, chain 6).
    """
    words = text_of(after_block)
    return any(other is not block and text_of(other) == words
               and other.get(field) == value
               for other in (now_part or {}).get("blocks", []))


def _inherited_findings(key, block, after_block, mine: dict, said: str, tab,
                        theme: dict | None, now_part: dict | None = None) -> list[dict]:
    """A paragraph that wore the document's named style and stopped.

    A document's look lives in its named styles, and a paragraph that sets nothing of
    its own wears them. Write a value onto such a paragraph and it stops wearing them
    — for good, and invisibly: the file is regenerated from the document afterwards,
    so it now says what was written, the next sync writes nothing, and the convergence
    check is perfectly happy with a document whose theme is gone. Nothing else here
    can see it, because nothing was deleted and no word moved.

    So: a property the theme sets for this paragraph's named style, that the paragraph
    did not have before, has after, and the file never asked for. The merge does not
    invent one, which is what makes this a defect rather than a judgement call —
    `doc_merge.paragraph_style` gave `alignment` a value always until it was found
    this way.

    `theme` is `{namedStyleType: {the IR fields that style sets}}` and is the whole of
    what makes this check safe to run. Without it the same question fires on Docs' own
    merge-on-delete rule, where a paragraph really does take the style of the one
    deleted in front of it: that is the document's doing, not ours.
    """
    named = doc_merge.named_style(block) if block.get("kind") != "table" else None
    fields = (theme or {}).get(named) or ()
    if not fields:
        return []
    mine_block = next((b for b in mine.get("blocks", []) if b.get("key") == key), None)
    out = []
    for field in fields:
        now, then = block.get(field), after_block.get(field)
        if then is None or then == now or (mine_block or {}).get(field) == then:
            continue
        if _twin_already(now_part, block, after_block, field, then):
            continue
        out.append(finding(
            "theme_undone", "loss",
            f"the block {key} inherited its {field} and now says {then!r} of its own, "
            f"which neither the reader nor the file asked for: it has stopped following "
            f"the document's named style", tab=tab, key=key))
    return [f for f in out if not _named(said, key)]


# What a reader can set about a paragraph in the browser that is a choice of theirs.
# `ordered` is left out: a list's ordered-ness is the one thing an imported document
# cannot report (`doc_merge` ignores it for identity and the settle hands back the
# document's own bullets), so the two sides disagree about it by construction.
SHAPE_FIELDS = tuple(k for k in doc_merge.SHAPE_KEYS if k != "ordered")


def _behind(was: dict | None, key) -> str | None:
    """The key of the block standing in front of this one in the base."""
    blocks = (was or {}).get("blocks", [])
    for at, block in enumerate(blocks):
        if block.get("key") == key:
            return blocks[at - 1].get("key") if at else None
    return None


def _shape_findings(key, block, base_block, after_block, said, tab,
                    behind_dropped: bool) -> list[dict]:
    """How the reader set a paragraph, put back the way the file has it.

    The mirror of `_style_findings` one level up: a mark is a choice about a word and
    this is a choice about the paragraph — its named style, its alignment, its
    indents, its spacing, its shading, its rules, its page break. A reader who centres
    a quotation or makes a line a heading has done something as deliberate as bolding
    a word, and until this was written **nothing asked whether it survived**. The
    convergence check cannot: the file is regenerated from the document afterwards, so
    whatever was written reads back as what both sides wanted and the next sync writes
    nothing. The campaign's own judge cannot either: it asks whether the *source's*
    changes arrived, and here the source's change arrived perfectly.

    `behind_dropped` is Docs' own rule and the one thing that has to be forgiven: a
    paragraph the source deletes is merged into the one behind it, which takes the
    *deleted* one's style. That is the document's doing, not the sync's — the same
    reason `_inherited_findings` has to be told what the theme sets.
    """
    if base_block is None or block.get("kind") == "table" or behind_dropped:
        return []
    for field in SHAPE_FIELDS:
        theirs, then, now = (block.get(field), base_block.get(field),
                             after_block.get(field))
        if theirs == then or now == theirs:
            continue
        return [] if _named(said, key) else [finding(
            "shape_undone", "loss",
            f"the block {key} had its {field} set to {theirs!r} by the reader and says "
            f"{now!r} after the sync, which is what the file asks for", tab=tab, key=key)]
    return []


def _source_dropped(block: dict, was: dict | None, mine: dict | None) -> bool:
    """Whether this block is one the source dropped and the reader left alone, so
    that what it holds goes with it.

    Chips are counted by value over the whole tab, and the excuse is that the file
    still holds one of that value (`mine_frozen`) — which credits a chip the source
    *added somewhere else* against the one it is deleting here. A source that drops
    the block its person chip is in and adds a chip of the same address to another
    block read as no change at all, and then as a loss when the second block turned
    out to be one no request can write (chain-8 seed 9109, chain-4 seed 3286).

    Only a block the reader left exactly as the base has it: a chip the reader put
    in is a chip the merge keeps the block for (`doc_merge._edited`), and if it ever
    stopped doing that this must still say so.
    """
    key = block.get("key")
    if mine is None or key is None or key in {b.get("key") for b in mine.get("blocks", [])}:
        return False
    base_block = keyed(was).get(key)
    return base_block is not None and frozen_marks(base_block) == frozen_marks(block) \
        and text_of(base_block) == text_of(block)


def _picture_findings(now: dict, mine: dict | None, then: dict | None,
                      said: str, tab) -> list[dict]:
    """The pictures the document held and does not hold any more.

    Paired by name (`pair_images`), because a picture may keep its object id or keep
    its file and need not keep both. A picture the *source* itself took out of the
    file is meant to go — but only when the file has that tab at all: where it does
    not, the whole tab is the reader's and everything in it has to survive.

    What the file asks for is claimed one for one as well (`pair_images` again), and
    counted rather than matched picture by picture: two copies of one file are told
    apart by their object ids, which the survivor may not keep, so *which* of them
    survived and *which* the file still asks for can land on different copies and
    mean nothing. The document has to keep as many as the file still asks for; a
    source that adds a second copy and then drops the first left the file holding
    that name, and matching by name alone accused the copy that went (fresh seed
    980193, chain 6).

    That pairing does ask for the ids (`ids=True`), because the file's are the
    document's own: two pictures of one file, one deleted by the reader and the
    other dropped by the source, left the survivor paired with the file's entry for
    the one the reader had already taken away, so nothing was excused and the
    picture the source itself gave up was named as lost (fresh seed 994410, chain 8).
    """
    out: list[dict] = []
    held = images_of(now)
    hits = pair_images(held, images_of(then))
    telling = telling_names(held)
    left = [i for i in range(len(held)) if hits[i] is None]
    if mine is not None:
        asked = pair_images(held, images_of(mine), ids=True)
        left = left[:max(0, len(left) - sum(1 for at in asked if at is None))]
    for i in left:
        run = held[i]
        names = telling[i] or image_names(run)
        if _named(said, *(value for _, value in names)):
            continue
        out.append(finding("frozen_gone", "loss",
                           f"the image {frozen_key(run)[1]!r} the document held is not "
                           f"in it any more", tab=tab))
    return out


def _stands(key, block, mine: dict | None, then: dict | None) -> bool:
    """Whether the block whose key is gone is still there, whole, under another key.

    A key lost is a key lost, and nothing else; a paragraph whose words the sync
    scattered over the document is a paragraph gone, however many of its words turn
    up somewhere. So one block after the sync must hold *all* of its words — either
    as the document had them, or as the file rewords them, since the sync is entitled
    to write the source's version of that block.
    """
    wanted = [words(text_of(block))]
    for other in (mine or {}).get("blocks", []):
        if other.get("key") == key:
            wanted.append(words(text_of(other)))
    return any(w and not (w - words(text_of(after)))
               for after in (then or {}).get("blocks", []) for w in wanted)


def _words_findings(key, block, base_block, after_block, after_words, said, tab,
                    tab_was: Counter | None = None):
    """Words the reader typed that the document does not say any more."""
    theirs = words(own_words(block))
    was = words(own_words(base_block)) if base_block else Counter()
    typed = theirs - was                                  # what the reader added
    if not typed:
        return []
    survived = words(text_of(after_block))
    lost = typed - survived - (after_words - survived)     # nowhere in the tab
    # `tab_was` for `_welded` alone: a join is the one reader edit that makes a token
    # out of two blocks, so the other half of it is a word of the tab's base and not
    # of this block's. Everything else is measured against the block, as it must be.
    lost = Counter({w: n for w, n in lost.items()
                    if len(w) > 1
                    and not joined_differently(w, after_words, was, tab_was, theirs)})
    if not lost or _named(said, key):
        return []
    return [finding("words_lost", "loss",
                    f"the block {key} lost words the reader typed: "
                    f"{' '.join(sorted(lost))[:80]}", tab=tab, key=key)]


def _cored(counted: Counter) -> Counter:
    """Word counts by `core`: the spelling a styled word is known by."""
    out: Counter = Counter()
    for token, times in counted.items():
        out[core(token)] += times
    return out


def _style_findings(key, block, base_block, after_styles, after_block, after_words,
                    said, tab, theme=None, file_block=None, dropped=False):
    """Styling the reader put on words that are still there — or took off them.

    Taking a mark off is as much a choice as putting one on, and the only way to
    make it is against a theme that puts it on (`styles_of`): a reader who un-bolds
    a word of a heading has done something the file has to carry, or the first
    source edit that writes that block again hands the word back to the theme.

    The two questions are not asked at the same width. A mark the reader *put on* is
    looked for anywhere in the tab (`after_styles`), because a block the sync rewrote
    and re-keyed still carries it and nothing is lost. A mark the reader *took off* is
    asked of this block alone: the word is one the theme bolds, so the same word in
    the heading next door wears it too, and a tab-wide answer would call every
    un-bolding a loss (themed seeds 9, 32, 40).

    And inside the block it is asked by *occurrence*: how many of this word the reader
    un-marked, against how many are still un-marked afterwards. Asking instead whether
    the word wears the mark anywhere in the block counted a second occurrence the
    source had just added — "and willow" plus " and harbour" — as the reader's own
    coming back bold, while the word they pressed Ctrl+B on stood untouched (themed
    seed 40254). The mark must also really be on in the end: a block that stopped
    being a heading took the bold off every word of it, which is `theme_undone`'s
    question and not this one.

    One unmarking is not the reader's news: one the *base* already records, on a word
    the file itself now marks. The reader pressed Ctrl+B a sync ago, the settle wrote
    that into the file and the base, and the source has since asked for the mark on
    that very word — a source restyle of a block the document has not restyled since,
    which the merge's own rule gives to the source. `marks_on` without the theme,
    because what excuses it is the file *saying* the mark: a file that says nothing
    there (a block the source merely moved, written again from nothing) leaves the
    finding exactly where it was (fresh seed 970228, chain 6).

    A twin once had to be forgiven here too — the reader pastes a copy, the key lands
    on whichever paragraph comes first, and their own un-bolding is right beside it in
    plain sight (chain-5 seed 90736). That was a symptom: `doc_merge._adopt_in_order`
    keeps the key on the block the plan meant, and with it the excuse became dead
    weight (3,750 rounds at chains 4 to 8 without it, nothing found). An oracle that
    forgives what no longer happens is a blind spot waiting for the next defect that
    looks like it, so it is gone.

    `dropped` is the one twin that is not that: the block the mark was taken off is
    one the *source* dropped and the reader left as the base has it, so it goes and
    the mark goes with it — the bargain the `block_gone` branch above states in the
    same words. What makes it visible at all is that the key then lands on a block
    the reader's own paste put there, saying the same words in the theme's own bold
    (chain-10 seed 1180145, shape `themed`). Only the taken-off half: a mark the
    reader *put on* is looked for tab-wide and would have kept the block alive
    (`doc_merge._styled`), so the question cannot arise there. And only where there
    is a file to say it: asked of a check given none, "the file no longer names this
    key" is true of every key there is, and the whole half falls silent.
    """
    theirs = marks_on(block)
    was = marks_on(base_block) if base_block else Counter()
    # A styled word is its `core`, so the question "is the word still there?" has to
    # be asked in the same words: `after_words` counts the tab's `\S+` tokens.
    standing = _cored(after_words)
    lost = Counter({m: n for m, n in ((theirs - was) - after_styles).items()
                    if standing.get(m[1])})
    if lost and not _named(said, key):
        mark, word = next(iter(lost))
        return [finding("styling_lost", "loss",
                        f"the block {key} lost the {mark} the reader put on "
                        f"{word!r}", tab=tab, key=key)]
    agreed = unmarked_of(base_block, theme) if base_block else Counter()
    asked = marks_on(file_block) if file_block else Counter()
    undone = (unmarked_of(block, theme) - unmarked_of(after_block, theme)
              - (agreed & asked))
    # An un-mark is undone only on a word that is still *that* word. The question is
    # asked by occurrence and a block may say one twice, so where the source's own
    # rewording takes an occurrence away, the mark it carried went with it: the block
    # said `thicket` twice, the reader un-bolded one of them, the source reworded that
    # one to `vellum` — which came out un-bold, exactly as asked — and the plain
    # `thicket` left over answered for it (chain-10 seed 1180151, shape `themed`).
    # Counted against the *file*, because that says whose doing it was: an occurrence
    # the source has just added is the mirror case and must still be no excuse
    # (themed seed 40254).
    here, asks = _cored(words(text_of(block))), _cored(words(text_of(file_block or {})))
    back = Counter()
    for (mark, word), times in (undone & marks_on(after_block, theme)).items():
        reworded = max(0, here[word] - asks[word]) if file_block else 0
        if standing.get(word) and times > reworded:
            back[(mark, word)] = times - reworded
    if back and not dropped and not _named(said, key):
        mark, word = next(iter(back))
        return [finding("styling_restored", "loss",
                        f"the block {key} has the {mark} back on {word!r}, which the "
                        f"reader had taken off", tab=tab, key=key)]
    return []


def _cell_findings(key, block, base_block, after_block, after_words, said, tab,
                   tab_was: Counter | None = None):
    """Words the reader typed into a cell, cell by cell where the grid allows it.

    A cell is known by its place, and a row the reader inserted shifts every cell
    below it — so a word that stands anywhere in the base's table is none of the
    reader's, wherever it has ended up. Without that a source edit two rows down
    reads as a loss (offline seed 181: the reader adds a row, the source rewrites a
    cell, and the cell at that place now holds the word the row above it had).

    `tab_was` is `_words_findings`' parameter for `_welded` and it is wanted here for
    the same reason and more sharply: a cell is the one place a reader's drag can weld
    text from *anywhere else in the tab* onto a word of the table. A paragraph dropped
    into a cell against the word standing there makes one token of the two — `it.x`
    for "…after it." and `x` — which is a token of neither the base's table nor the
    base's paragraph, so it read as a word the reader typed, and the source rewriting
    its own half of it read as a loss (chain-4 seed 600784: `edit_cell` made the `x`
    into `meadow-83192`, the merge said `it.meadow-83192`, and both edits were in it).
    Only `_welded` sees the tab; everything else is the table's question, since a
    regrid is what moves words about inside one.
    """
    theirs, was = cells_of(block), cells_of(base_block) if base_block else {}
    now = cells_of(after_block)
    elsewhere = words(text_of(base_block)) if base_block else Counter()
    out = []
    for at, text in theirs.items():
        typed = words(text) - words(was.get(at, ""))
        typed = Counter({w: n for w, n in typed.items()
                         if len(w) > 1 and not elsewhere.get(w)})
        if not typed:
            continue
        # By place when the grid still has that cell, else anywhere in the table.
        survived = words(now[at]) if at in now else Counter()
        after_all = words(text_of(after_block))
        lost = typed - survived - (after_all - survived)
        lost = Counter({w: n for w, n in lost.items()
                        if not joined_differently(w, after_all, elsewhere, tab_was)})
        if lost and not _named(said, key):
            out.append(finding("cell_words_lost", "loss",
                               f"the cell {at} of {key} lost {' '.join(sorted(lost))[:60]}",
                               tab=tab, key=key))
    return out


def _row_texts(block: dict | None) -> list[str]:
    """One string per row, its cells joined: what a row *says*, which is the only
    handle on a row there is (a row carries no key of its own)."""
    return [" | ".join(text_of(inner) for cell in row for inner in cell).strip()
            for row in (block or {}).get("rows", [])]


def _row_resurrection_findings(key, block, base_block, after_block, said, tab,
                               file_block=None):
    """A row the reader deleted that the sync put back.

    `block_resurrected` at the third size. A row has no key: the merge knows it by
    what it says (`doc_merge._table_lines`), and so does this. A row the reader
    reordered or reworded is not one they deleted — its words are still in the table
    before the sync — so the question is asked of the words, as it is of a block, and
    is just as forgiving on purpose.

    Words are evidence and not the thing itself, and the source can spend them: it may
    write into a row the reader *kept* exactly what a row they deleted used to say, and
    then a row saying that after the sync is the source's own asking and no
    resurrection at all (chain-6 seed 260208: the base's rows say `thicket` and
    `meadow`, the reader deletes `thicket`, the source rewrites `meadow` to `thicket`,
    and the one row left rightly says `thicket`). So they are counted rather than
    looked up: the reader took the count to what the document shows, the source has
    raised it by `file - base` of its own accord, and a row over that sum is one that
    came back. With the source leaving those words alone the sum is what the document
    shows, which is the question as it was.
    """
    if not base_block or block.get("kind") != "table" or not after_block:
        return []
    live_rows = _row_texts(block)
    live_text = " ".join(live_rows)
    base_rows = _row_texts(base_block)
    after_rows = Counter(_row_texts(after_block))
    live_count = Counter(live_rows)
    asked = Counter(_row_texts(file_block)) if file_block else Counter(base_rows)
    out = []
    for row in dict.fromkeys(base_rows):
        allowed = live_count[row] + max(0, asked[row] - Counter(base_rows)[row])
        if not row.strip() or after_rows[row] <= allowed:
            continue
        if stands_elsewhere(row, live_text) or _named(said, key, row[:40]):
            continue
        out.append(finding("row_resurrected", "loss",
                           f"the reader deleted the row {row[:60]!r} of {key} and it is "
                           f"back after the sync; the report does not say why",
                           tab=tab, key=key))
    return out


def _order_findings(was, now, then, said, tab):
    """A block the reader moved must stay where they put it (docs/google-docs.md: the
    merged order is the document's)."""
    order_was = [b["key"] for b in (was or {}).get("blocks", []) if b.get("key")]
    order_now = [b["key"] for b in (now or {}).get("blocks", []) if b.get("key")]
    order_then = [b["key"] for b in (then or {}).get("blocks", []) if b.get("key")]
    shared = set(order_was) & set(order_now) & set(order_then)
    place_was = {k: i for i, k in enumerate(k for k in order_was if k in shared)}
    place_now = {k: i for i, k in enumerate(k for k in order_now if k in shared)}
    place_then = {k: i for i, k in enumerate(k for k in order_then if k in shared)}
    out = []
    for x in shared:
        for y in shared:
            if x >= y:
                continue
            base_order = place_was[x] < place_was[y]
            read_order = place_now[x] < place_now[y]
            done_order = place_then[x] < place_then[y]
            if read_order != base_order and done_order == base_order \
                    and not _named(said, x, y):
                out.append(finding("order_undone", "undo",
                                   f"the reader moved {x} and {y} apart and the sync put "
                                   f"them back the way the base had them", tab=tab, key=x))
                return out   # one is enough: a move shows up in every pair it crosses
    return out


# ---------------------------------------------------------------- saying it

def failures(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] in FAIL]


def describe(findings: list[dict]) -> str:
    if not findings:
        return "nothing lost"
    lines = []
    for f in sorted(findings, key=lambda f: SEVERITIES.index(f["severity"])):
        where = " ".join(str(x) for x in (f.get("tab"), f.get("key")) if x)
        lines.append(f"  [{f['severity']}] {f['kind']}{(' ' + where) if where else ''}: "
                     f"{f['detail']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundle", type=Path,
                    help="JSON with base / before / after / report / ours")
    args = ap.parse_args(argv)
    data = json.loads(args.bundle.read_text(encoding="utf-8"))
    found = check(data.get("base", {}), data["before"], data["after"],
                  data.get("report", {}), data.get("ours"))
    print(describe(found))
    return 1 if failures(found) else 0


if __name__ == "__main__":
    sys.exit(main())
