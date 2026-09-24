"""The journeys that write LaTeX: pull, its offline twin, and adopt.

These three are the expensive end of the agent layer. Each one runs a **compile loop** - write
an edit, run pdflatex, classify the PDF, compare it with the target, write the next edit - so a
call is seconds per round at best and minutes for a real deck, and `deck_adopt` adds one LARGE
thumbnail per slide on top. Nothing here writes to Google: `deck_pull` and `deck_adopt` read a
deck and write files, `tex_converge` does not even read.

What comes back is the same shape for all three, because an agent's next move is the same
question every time: did it converge, what is left, and which file says what to do about it.
That file is `edits.md`; the residual counts in `data` are there so a benchmark can score a run
without reading prose.

Annotations are deliberately *not* postponed (`from __future__ import annotations` is absent):
`@tool` hands back a wrapper defined in `context.py`, whose `__globals__` are that module's, so
string annotations would not resolve there. Real `Annotated` objects are copied onto the wrapper
by `functools.wraps` and `typing.get_type_hints(fn, include_extras=True)` reads them anywhere.
"""

import json
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Callable

from .context import Job, tool
from .types import READS, READS_GOOGLE, WRITES, Refused

__all__ = ["deck_pull", "tex_converge", "deck_adopt", "SOURCE_TOOLS"]

#: `-file-line-error` puts the failing place first: `./main.tex:112: Undefined control sequence`.
_FILE_LINE = re.compile(r"^(?P<file>[^\s:][^:]*):(?P<line>\d+): ", re.MULTILINE)
_URL = re.compile(r"^https?://", re.IGNORECASE)
_SLIDES_READ = re.compile(r"deck: (\d+) slides read")


# ---------------------------------------------------------------- the tools

@tool("deck_pull", needs=(READS, WRITES, READS_GOOGLE))
def deck_pull(
    j: Job,
    deck: Annotated[str, "The deck to pull from: a Slides URL, a presentation id, or a workspace "
                         "ref to the out/<deck> folder a conversion wrote. Read only, never written."],
    tex: Annotated[str, "Workspace ref of the main .tex the deck was converted from. It must exist; "
                        "pull refines a source, it does not write one (that is deck_adopt)."],
    work: Annotated[str | None, "Workspace ref of the loop's scratch folder (compiles, target.json, "
                                "the reports). Default: <deck folder>/pull when `deck` is a folder, "
                                "else <tex parent>/out/pull."] = None,
    apply: Annotated[bool, "Write the converged source over the real files, keeping what was there "
                           "as .bak/.bak2. False (the default) leaves the tree alone and only "
                           "reports the patch."] = False,
    out: Annotated[str | None, "Workspace ref of a folder to copy the whole edited source tree into "
                               "instead of applying it in place. Mutually useful with apply=False."] = None,
    max_iter: Annotated[int, "How many edit rounds the loop may run before giving up (each round is "
                             "one LaTeX compile plus a classify). 10 is the CLI default."] = 10,
    handout: Annotated[bool, "Compile the source in handout mode, for a deck converted from a "
                             "handout PDF (no overlay steps)."] = False,
    engine: Annotated[str | None, "Force a LaTeX engine (pdflatex, xelatex, lualatex). Default: "
                                  "whatever the source itself asks for."] = None,
) -> None:
    """Pull a live Slides deck back into the LaTeX source it was converted from.

    Reads the deck (presentations.get and the pictures; the deck is never written), then loops
    compile -> classify -> compare -> edit the .tex until a fresh conversion of the source would
    match the deck. **This compiles LaTeX once per round**, so it costs seconds per round and
    minutes for a large deck - not a call to make speculatively.
    Use it when someone edited the deck in Slides and the source has to catch up. What the loop
    could not express in LaTeX is left in `edits.md`, which is written for an AI to act on next.
    """
    tex_path = _existing(j, tex, "tex")
    target_ref, folder = _deck_argument(j, deck)
    work_path = _work_dir(j, work, folder / "pull" if folder else tex_path.parent / "out" / "pull")
    out_path = j.path(out, write=True) if out else None

    from ..deck_ir import read_deck
    from ..inverse import Later, run_pull

    log = _logger(j)
    j.data["deck"] = target_ref

    def read():
        # On a thread of its own while the source first compiles (`inverse.converge`): the read
        # needs no PDF and the compile needs no deck.
        target = read_deck(target_ref, images=work_path / "target-images")
        log(f"deck: {len(target['slides'])} slides read")
        j.data["slides"] = len(target["slides"])
        return target

    result = _loop(lambda: run_pull(Later(read), tex_path, work_path, apply, out_path, max_iter,
                                    handout, engine, log=log))
    _finish(j, result, work_path, out_path, apply, max_iter, "deck_pull")


@tool("tex_converge", needs=(READS, WRITES))
def tex_converge(
    j: Job,
    target: Annotated[str, "Workspace ref of the deck.json-shaped file to converge onto: classify's "
                           "deck.json, or a deck_ir read of a live deck saved earlier."],
    tex: Annotated[str, "Workspace ref of the main .tex to edit until its conversion matches the "
                        "target."],
    work: Annotated[str | None, "Workspace ref of the loop's scratch folder. Default: "
                                "<target parent>/pull."] = None,
    apply: Annotated[bool, "Write the converged source over the real files, keeping what was there "
                           "as .bak/.bak2. False (the default) only reports the patch."] = False,
    out: Annotated[str | None, "Workspace ref of a folder to copy the whole edited source tree "
                               "into instead of applying it in place."] = None,
    max_iter: Annotated[int, "How many edit rounds the loop may run before giving up (one LaTeX "
                             "compile each)."] = 10,
    handout: Annotated[bool, "Compile the source in handout mode (no overlay steps)."] = False,
    engine: Annotated[str | None, "Force a LaTeX engine (pdflatex, xelatex, lualatex). Default: "
                                  "whatever the source asks for."] = None,
) -> None:
    """Converge a LaTeX source onto a local deck.json target: pull with no Google in it.

    The offline twin of `deck_pull` - same loop, same reports, same `edits.md` - with the target
    read from a file instead of from Slides. **It compiles LaTeX once per edit round**, so it is
    seconds to minutes, not milliseconds.
    Use it to test or benchmark the pull loop, to replay a deck someone saved, or to make a
    source match a classification without spending an account's quota. `data` carries the
    residual counts before and after and one row per iteration, for scoring.
    """
    target_path = _existing(j, target, "target")
    tex_path = _existing(j, tex, "tex")
    work_path = _work_dir(j, work, target_path.parent / "pull")
    out_path = j.path(out, write=True) if out else None

    from ..inverse import run_pull

    log = _logger(j)
    try:
        doc = json.loads(target_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Refused("bad_request", f"{target} is not JSON: {exc}", target=target) from None
    if not isinstance(doc, dict) or "slides" not in doc:
        raise Refused("bad_request", f"{target} is not a deck.json (no 'slides' key).", target=target)
    j.data["slides"] = len(doc["slides"])
    j.data["target"] = j.ctx.workspace.ref(target_path)

    result = _loop(lambda: run_pull(doc, tex_path, work_path, apply, out_path, max_iter,
                                    handout, engine, log=log))
    _finish(j, result, work_path, out_path, apply, max_iter, "tex_converge")


@tool("deck_adopt", needs=(READS, WRITES, READS_GOOGLE))
def deck_adopt(
    j: Job,
    deck: Annotated[str, "The deck to adopt: a Slides URL, a presentation id, or a workspace ref to "
                         "a deck.json-shaped file (ending .json), which is read locally with no "
                         "Google call at all."],
    tex: Annotated[str, "Workspace ref of the main .tex to write. It must NOT exist: adopt writes a "
                        "new source tree beside it (slides.sty, the recovered theme, figures/)."],
    work: Annotated[str | None, "Workspace ref of the loop's scratch folder. Default: "
                                "<tex parent>/out/adopt."] = None,
    apply: Annotated[bool, "Write the converged source over the bootstrapped files. The bootstrap "
                           "itself is always written; this governs the loop's own edits."] = False,
    out: Annotated[str | None, "Workspace ref of a folder to copy the whole edited source tree into "
                               "instead of applying it in place."] = None,
    max_iter: Annotated[int, "How many edit rounds the convergence loop may run (one LaTeX compile "
                             "each). 6 is adopt's own default, lower than pull's."] = 6,
    engine: Annotated[str | None, "Force a LaTeX engine (pdflatex, xelatex, lualatex). Default: "
                                  "whatever the bootstrapped source asks for."] = None,
    flow: Annotated[bool, "Write the readable version - text that flows in lists and paragraphs - "
                          "instead of the absolute geometry a foreign deck's dragged boxes are. "
                          "Costs fidelity, gains a source a person can edit."] = False,
    fonts: Annotated[list[str] | None, "Font files the deck is written in (.ttf, .otf, .ttc, .woff, "
                                       ".woff2), each a workspace ref (a folder: every font in it) or "
                                       "the file itself as content. Preferred to any other copy. "
                                       "Give the ones a previous adopt listed in data['fonts_missing']."] = None,
) -> None:
    """Write the LaTeX source a foreign deck never had, then converge it onto that deck.

    For a deck a person built in Slides, which `deck_pull` cannot refine because there is no
    source to refine. It reads every slide, its layouts and masters and **one LARGE thumbnail
    per slide** (60 such reads a minute, a 429 sleeps 20-60 s: minutes for a big deck), writes a
    source tree, then **compiles it in a loop** like pull. `data["readability"]`: how keepable
    that source is (sources people wrote: 0.6-1.0). A `.json` deck is read locally, but credentials
    are fetched anyway. Fonts it had no file for are in `data["fonts_missing"]` - ask for them.
    """
    from ..adopt import written_already

    font_paths = [_existing(j, ref, "fonts") for ref in fonts or []]
    tex_path = j.path(tex, write=True)
    if written_already(tex_path):
        # adopt's own refusal, made before the minutes of thumbnails rather than after them.
        j.suggest("deck_pull to refine an existing source instead")
        raise Refused("source_exists",
                      f"{j.ctx.workspace.ref(tex_path)} is already there and adopt writes a new "
                      f"source tree. Use deck_pull to refine the source you have, or name a path "
                      f"nothing is at.", tex=j.ctx.workspace.ref(tex_path))
    target_ref, folder = _deck_argument(j, deck, allow_json=True)
    target_path = Path(target_ref) if target_ref.lower().endswith(".json") else None
    work_path = _work_dir(j, work, tex_path.parent / "out" / "adopt")
    out_path = j.path(out, write=True) if out else None
    j.data["deck"] = target_ref
    j.data["local_target"] = target_path is not None

    from ..adopt import cmd_adopt

    log = _logger(j)

    def watch(line: str = "") -> None:
        # The slide count is only said out loud, and it is said before the loop that may die:
        # read it as it goes past, so a run that broke mid-compile still reports what it read.
        if m := _SLIDES_READ.search(str(line)):
            j.data["slides"] = int(m.group(1))
        log(line)

    from ..fontfiles import ForeignFolder

    found: dict = {}
    try:
        result = _loop(lambda: cmd_adopt(target_ref, tex_path, work_path, apply, out_path,
                                         max_iter, engine, flow, target_path, log=watch,
                                         fonts=font_paths, found=found))
    except ForeignFolder as exc:
        raise Refused("bad_request", str(exc), work=j.ctx.workspace.ref(work_path)) from None
    _fonts_report(j, found)
    if tex_path.exists():
        j.artifact(tex_path, "tex", "the bootstrapped source adopt wrote")
        _readability(j, tex_path)
    # Scoring the pages the way devtools/adopt_bench.py does needs both sides rendered on one pixel
    # grid (its `score_page` wants numpy arrays of the slide thumbnails and of the compiled PDF),
    # which is well past thirty lines of wiring here: agent_bench is where that belongs.
    j.data["page_scores"] = None
    _finish(j, result, work_path, out_path, apply, max_iter, "deck_adopt")
    _sync_base(j, work_path)


SOURCE_TOOLS = (deck_pull, tex_converge, deck_adopt)


# ---------------------------------------------------------------- arguments

def _fonts_report(j: Job, found: dict) -> None:
    """What adopt made of the fonts given, and which of the deck's fonts it had to stand in for.

    A font set in a stand-in breaks lines in other places than the deck does, and every such
    line is a residual the loop then spends rounds on; the one thing that fixes it is the font's
    file, which only the caller can hand over. So it is data to act on, not a line in the log."""
    supplied = found.get("supplied")
    if supplied is not None:
        j.data["fonts_supplied"] = {f: got["styles"] for f, got in supplied.get("families", {}).items()}
        for s in supplied.get("skipped", []):
            j.warn(f"{s['file']} was not used: {s['reason']}", where="fonts")
    missing = found.get("missing") or []
    j.data["fonts_missing"] = [dict(m) for m in missing]
    for m in missing:
        j.warn(f"{m['font']} ({m['letters']} letters) is not here and was set in {m['set_in']}, so its "
               f"lines break elsewhere than the deck's", where="fonts")
    if missing:
        j.suggest("ask the person for the files of the fonts in data['fonts_missing'] and adopt again "
                  "with fonts=[...] (into a new tex path), if the deck's line breaks matter")


def _existing(j: Job, ref: str, what: str) -> Path:
    path = j.path(ref)
    if not path.exists():
        raise Refused("not_found", f"No {what} at {ref} (looked in {path}).", **{what: ref})
    return path


def _work_dir(j: Job, ref: str | None, default: Path) -> Path:
    path = j.path(ref, write=True) if ref else j.ctx.workspace.resolve(str(default), write=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _deck_argument(j: Job, deck: str, allow_json: bool = False) -> tuple[str, Path | None]:
    """What `read_deck`/`cmd_adopt` get, and the out folder if the ref named one.

    A deck is a URL, a bare presentation id, or a path in the workspace - a conversion's out
    folder (which carries deck.json and the sync base), or, for adopt, a saved deck.json.
    """
    if _URL.match(deck):
        return deck, None
    try:
        path = j.path(deck)
    except Refused:
        raise
    except OSError:                       # a presentation id Windows will not make a path of
        return deck, None
    if path.exists():
        if path.is_dir():
            return str(path), path
        if path.suffix.lower() == ".json" and allow_json:
            return str(path), None
        if path.suffix.lower() == ".json":
            raise Refused("bad_request",
                          f"{deck} is a file; deck_pull wants a live deck (URL or id) or the "
                          f"out/<deck> folder of a conversion. Use tex_converge for a local "
                          f"deck.json target.", deck=deck)
        raise Refused("bad_request", f"{deck} is not a deck folder.", deck=deck)
    if "/" in deck or "\\" in deck or deck.lower().endswith(".json"):
        raise Refused("not_found", f"No deck at {deck} (looked in {path}).", deck=deck)
    return deck, None                     # a bare presentation id


# ---------------------------------------------------------------- the loop and its report

def _logger(j: Job) -> Callable[[str], None]:
    """What the loop prints its progress through: the job's log, not stdout."""

    def log(line: str = "") -> None:
        for one in str(line).split("\n"):
            j.log.append(one)
            if j.ctx.progress:
                try:
                    j.ctx.progress(one)
                except Exception:                                  # a harness sink is not our problem
                    pass

    return log


def _loop(run: Callable[[], Any]) -> Any:
    """Run the convergence loop, turning its one fatal failure into a refusal with the place in it."""
    try:
        return run()
    except RuntimeError as exc:
        text = str(exc)
        if "does not compile" not in text:
            raise
        data: dict[str, Any] = {"latex_error": text}
        if m := _FILE_LINE.search(text):
            data["file"] = m.group("file")
            data["line"] = int(m.group("line"))
            data["where"] = f"{m.group('file')}:{m.group('line')}"
        raise Refused("compile_failed", text.strip(), **data) from None


def _finish(j: Job, result: Any, work: Path, out: Path | None, apply: bool, max_iter: int,
            tool_name: str) -> None:
    """Everything the three journeys say the same way: data, artifacts, warnings, summary."""
    rounds = max(len(result.iterations) - 1, 0)
    left = _by_kind(result.residuals)
    first = result.iterations[0]["by_kind"] if result.iterations else {}
    changed = [j.ctx.workspace.ref(p) for p in sorted(result.files)]
    j.data.update({
        "converged": bool(result.converged),
        "rounds": rounds,
        "max_iter": max_iter,
        "residuals_before": dict(first),
        "residuals_left": left,
        "residuals_total": sum(left.values()),
        "unresolved": [_unresolved(u) for u in result.unresolved],
        "iterations": [dict(it) for it in result.iterations],
        "files_changed": changed,
        "applied": bool(apply),
        "work": j.ctx.workspace.ref(work),
    })
    if result.notes:
        j.data["pictures"] = list(result.notes)
    if result.theme:
        j.data["theme_differences"] = len(result.theme)
        j.note("note", f"{len(result.theme)} difference(s) the beamer theme owns were left alone: "
                       f"they belong in the theme, not in a frame.")

    report = work / "edits.md"
    if report.exists():
        j.artifact(report, "report", "what the loop could not express in LaTeX, written for an AI "
                                     "to act on: the residuals left, with frame file:lines")
    if (data_file := work / "edits.json").exists():
        j.artifact(data_file, "json", "the same report as data: iterations, unresolved residuals, "
                                      "changed files, picture notes")
    if (patch := work / "pull.patch").exists():
        j.artifact(patch, "report", "unified diff of every source change the loop made")
    if out is not None and out.exists():
        j.artifact(out, "folder", "the edited source tree")

    skipped = _not_applied(data_file)
    for path in skipped:
        # A real collision with a human: they wrote to the file while the loop was compiling, so
        # their version is still there and the loop's is beside it.
        j.conflict(f"{path} changed while the pull was running, so it was left alone; this run's "
                   f"version is {path}.b2s-new", where=path)
    if skipped:
        j.data["not_applied"] = skipped
        j.suggest("diff each <file>.b2s-new against the file and merge by hand")

    for u in result.unresolved[:20]:
        j.warn(_residual_text(u), where=str(u.get("where") or ""))
    if len(result.unresolved) > 20:
        j.warn(f"{len(result.unresolved) - 20} further unresolved residual(s) are in edits.md.")

    if not result.converged and _stalled(result):
        # A loop that ran its rounds and got *somewhere* has written a source that compiles and a
        # report worth acting on, so that is ok=True with a loud warning: refusing would throw the
        # work away. `not_converged` is kept for the case where nothing moved at all - the first
        # round's residual count was never beaten - because then there is nothing but the report.
        j.suggest(f"read {j.ctx.workspace.ref(report)} and edit the source by hand",
                  f"run {tool_name} again with a larger max_iter once the source moved")
        raise Refused("not_converged",
                      f"{rounds} edit round(s) left {sum(left.values())} residual(s) and never beat "
                      f"the {sum(first.values())} the loop started with: the translators have "
                      f"nothing more to offer here. {j.ctx.workspace.ref(report)} says what is "
                      f"left.", rounds=rounds)

    j.summary = _summary(j, result, rounds, left, changed, apply, out, report)
    if not result.converged:
        j.suggest(f"act on {j.ctx.workspace.ref(report)}: it lists what the loop could not write")
    if not apply and out is None and changed:
        j.suggest(f"re-run with apply=True to write the {len(changed)} changed file(s) in place")


def _sync_base(j: Job, work: Path) -> None:
    """What the base `adopt` recorded says about the sync that comes after it.

    An agent that has just adopted a deck is about to tell a person they may edit this .tex and
    merge it back, and how much of that is true is a number only the base knows. Nothing on a
    person's slide says which part of a source it came from, so the base is a *pairing* by place
    and words (`adopt_sync.pair_elements`) and what it could not tie is kept as the deck has it,
    at every sync, for ever - on the corpus that is between 20% and 90% of the elements. It
    cannot be read off the source, the deck or the fidelity score, and a run that does not say it
    leaves an agent to promise a merge the tool will not make.
    """
    from .. import adopt_sync, snapshot

    path = snapshot.local_path(work)
    if not path.exists():
        j.warn("no sync base was recorded, so the source this wrote cannot be merged back into the "
               "deck it came from: deck_sync will say there is none, and deck_convert would make a "
               "second deck and leave the person's behind.")
        return
    info = (adopt_sync.load(path).get("adopt") or {})
    tied, loose = info.get("paired", 0), len(info.get("unpaired") or [])
    drawn = len(info.get("from_layout") or [])
    total = tied + loose + drawn
    j.data["sync_base"] = {"path": j.ctx.workspace.ref(path), "slides": info.get("slides"),
                           "elements": total, "paired": tied, "unpaired": loose, "from_layout": drawn}
    if loose:
        j.warn(f"{loose} of {total} element(s) are tied to no object of this deck, and no sync ever "
               f"gives one an object: a source edit to one of them is kept as the deck has it and "
               f"reported. Change those in Slides rather than in the source.")
    if drawn:
        j.warn(f"{drawn} of {total} element(s) are drawn by the deck's own layouts or master and "
               f"not by a slide. Change those in Slides under Slide > Edit theme.")
    if total:
        j.summary += (f" {tied} of {total} element(s) are tied to an object of the deck, which is "
                      f"what a later sync can write; the base is at {j.ctx.workspace.ref(path)}.")
    j.suggest(f"deck_sync(deck={j.ctx.workspace.ref(work)}, dry_run=True) after editing the source, "
              f"to see what the merge would write before anything is written")


def _summary(j: Job, result: Any, rounds: int, left: dict, changed: list[str], apply: bool,
             out: Path | None, report: Path) -> str:
    where = ("written in place (what was there is kept as .bak)" if apply
             else f"copied to {j.ctx.workspace.ref(out)}" if out is not None
             else "not written: the patch is in pull.patch")
    head = (f"Converged after {rounds} edit round(s): a fresh conversion of the source now matches "
            f"the target." if result.converged else
            f"{rounds} edit round(s) left {sum(left.values())} residual(s) "
            f"({_phrase(left)}) the loop could not write into LaTeX.")
    files = (f"{len(changed)} source file(s) changed, {where}." if changed
             else "No source file needed changing.")
    tail = (f"{len(result.unresolved)} residual(s) are left for an author or an AI to resolve; "
            f"{j.ctx.workspace.ref(report)} names each one with its frame's file:lines."
            if result.unresolved else
            f"Nothing was left unresolved; {j.ctx.workspace.ref(report)} has the full report.")
    return f"{head} {files} {tail}"


def _by_kind(residuals: list[dict]) -> dict[str, int]:
    return dict(sorted(Counter(r.get("kind", "?") for r in residuals).items()))


def _phrase(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {k}" for k, n in counts.items()) or "none"


def _unresolved(u: dict) -> dict:
    out = {"kind": u.get("kind"), "why": u.get("why"), "text": _residual_text(u)}
    for key in ("where", "frame_label", "target_slide", "slide"):
        if u.get(key) is not None:
            out[key] = u[key]
    return out


def _residual_text(u: dict) -> str:
    from ..compare import residual_line
    try:
        line = residual_line(u)
    except Exception:                                              # an entry with no residual shape
        line = f"{u.get('kind', 'residual')}"
    why = u.get("why")
    return f"{line} — {why}" if why else line


def _not_applied(data_file: Path) -> list[str]:
    """Files `pull --apply` left alone because someone edited them while the loop was running."""
    if not data_file.exists():
        return []
    try:
        return list(json.loads(data_file.read_text(encoding="utf-8")).get("not_applied", []))
    except (json.JSONDecodeError, OSError):
        return []


def _stalled(result: Any) -> bool:
    """Whether the loop never beat the residual count it started with."""
    opens = [it["open"] for it in result.iterations]
    return bool(opens) and min(opens) >= opens[0] and opens[-1] > 0


def _readability(j: Job, tex_path: Path) -> None:
    """How keepable the written source is (devtools/readability.py): sources people wrote score 0.6-1.0."""
    from ..devtools.readability import measure, score, tree_theme, tree_vocabulary

    tree = tex_path.parent
    try:
        text = tex_path.read_text(encoding="utf-8", errors="replace")
        m = measure(text, tree_vocabulary(tree), tree_theme(tree))
    except Exception as exc:                                       # never lose a whole adopt to this
        j.note("note", f"the source could not be measured for readability ({type(exc).__name__}).")
        return
    if not m:
        return
    j.data["readability"] = round(score(m), 3)
    j.data["readability_detail"] = {k: m[k] for k in ("frames", "words", "lines", "numbers",
                                                      "plumbing", "bloat", "author", "repeat")
                                    if k in m}
