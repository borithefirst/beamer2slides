"""The deck journeys as tools: look at a PDF, build the deck, merge into it, label the source.

These are the same four journeys the command line offers (`classify`, `convert`, `sync`,
`label`), with the differences the agent layer exists for. Three are worth naming here, because
they are the places where a faithful copy of `__main__.py` would have been the wrong thing:

* **A refusal is a code, not an exit.** `guard.RebuildRefused` is the library's most important
  sentence - a rebuild must never destroy what someone did in Slides - and at a terminal it is
  a paragraph of prose ending in three suggested commands. Here it comes back as
  `deck_edited` with the named edits in `data` and the three ways forward in `next_steps`, so
  an agent branches instead of parsing. (A backup that could not be made is the same exception
  with a different reason, and gets its own code, `no_way_back`.)
* **What the CLI does around a library call is part of the journey.** `sync.sync` does not
  record the deck's revision or keep a .pptx before its first write; `__main__.record_sync_point`
  does, and a caller who skips it silently gives up the only way back there is. `deck_sync`
  does it in the same order the CLI does.
* **Cost is a parameter.** `classify` always renders debug images and never runs the local
  invariants; an agent wants the opposite defaults, so `debug_images` is off and `checks` is
  on. Everything else about the sequence - the stale `slides.pdf`, the label survey, the order
  of preflight and work - is copied exactly, because those are decisions, not habits.

Every path in and out is a workspace ref (`Job.path`), and no tool here fetches credentials:
the wrapper has already installed them for the length of the call.
"""

import json
import time
from pathlib import Path
from typing import Annotated, Any, NoReturn

from .context import Job, tool
from .types import READS, READS_GOOGLE, WRITES, WRITES_GOOGLE, Refused

__all__ = ["deck_inspect", "deck_convert", "deck_sync", "tex_label"]


# ---------------------------------------------------------------- shared pieces


def _cli():
    """`__main__` as a module: `BACKUP_MODES` and the two steps that live in the CLI, not in
    `sync.sync`. Imported when a tool runs rather than at import, both because it pulls in the
    whole pipeline and because `python -m beamer2slides` has it loaded under another name."""
    from .. import __main__ as cli
    return cli


def _pdf(j: Job, ref: str) -> Path:
    path = j.path(ref)
    if not path.is_file():
        raise Refused("not_found", f"{ref}: no such PDF in the workspace.", ref=ref)
    if path.suffix.lower() != ".pdf":
        raise Refused("bad_request", f"{ref} is not a PDF; this tool reads the compiled PDF, "
                                     f"not the .tex.", ref=ref)
    return path


def _out_dir(j: Job, out: str | None, pdf: Path) -> Path:
    """`out/<pdf stem>/` in the workspace unless the caller named a folder."""
    folder = j.path(out, write=True) if out else j.ctx.workspace.out_dir(pdf.stem)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _check_backup(mode: str) -> str:
    modes = _cli().BACKUP_MODES
    if mode not in modes:
        raise Refused("bad_request", f"backup={mode!r} is not one of {', '.join(modes)}.",
                      backup=mode, allowed=list(modes))
    return mode


def _classify_into(j: Job, source: Path, out: Path, overlays: str, debug_images: bool) -> tuple:
    """`__main__.cmd_classify`, without the printing and with the debug render made optional.

    Returns (pdf without note pages, raw, deck, facts). `raw.json` and `deck.json` land in `out`
    exactly as the CLI writes them, so a folder an agent inspected is a folder `convert` and
    `fidelity` can go on using.
    """
    from ..classify import classify
    from ..extract import extract, select_overlays
    from ..notes import prepare as prepare_notes

    prepared = prepare_notes(source, out)
    pdf = prepared.pdf
    if not prepared.mode and (out / "slides.pdf").exists():
        (out / "slides.pdf").unlink()  # stale from an earlier run of a PDF that had notes
    raw = extract(pdf, prepared.labels)
    for page in raw["pages"]:
        page["notes"] = prepared.notes.get(page["index"])
    raw = select_overlays(raw, overlays)
    (out / "raw.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    deck = classify(raw)
    (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    if debug_images:
        from ..debug import render_debug
        render_debug(pdf, deck, out / "debug")
        j.artifact(out / "debug", "folder", "one PNG per slide with the classified boxes drawn on it")
    j.artifact(out / "raw.json", "json", "spans, images and drawings as the PDF gives them")
    j.artifact(out / "deck.json", "json", "the intermediate representation the deck is built from")
    facts = {
        "pages": raw["source"]["pages"],
        "slides": len(deck["slides"]),
        "notes": {"mode": prepared.mode, "pages": len(prepared.notes)} if prepared.mode else None,
        "overlays": {"mode": overlays, "dropped": raw.get("overlays", {}).get("dropped", 0)},
        "native_share": deck["stats"].get("native_share", 0),
        "chars": deck["stats"]["chars"],
        "chars_native": deck["stats"]["chars_native"],
        "fonts": sorted({s["font"] for page in raw["pages"] for s in page["spans"] if s.get("font")}),
    }
    return pdf, raw, deck, facts


def _slide_rows(deck: dict) -> list[dict]:
    """One row per slide: what it is made of, and what stays in the background picture."""
    from .. import identity

    rows = []
    for slide in deck["slides"]:
        kinds: dict[str, int] = {}
        for el in slide["elements"]:
            kinds[el["kind"]] = kinds.get(el["kind"], 0) + 1
        rows.append({
            "slide": slide["page"] + 1,
            "label": slide.get("label"),
            "title": identity.slide_title(slide),
            "elements": kinds,
            "background": [l["reason"] for l in slide.get("left_in_background", [])],
        })
    return rows


def _label_survey(j: Job, deck: dict) -> dict:
    """The one thing that decides whether a later sync can follow this deck (docs/labels.md).

    A frame with no `label=` falls back to its title and its position, which a reordered or
    retitled source breaks; a label written on two frames reaches the PDF once, so the second
    frame arrives here indistinguishable from an unlabelled one. Both are warnings, because both
    are cheap to fix now and impossible to fix after someone has edited the deck.
    """
    from .. import identity, labels

    found = labels.survey([identity.slide_info(s) for s in deck["slides"]])
    for line in labels.problems(found):
        j.warn(line, where="frame labels")
    return found


# ---------------------------------------------------------------- the tools


@tool("deck_inspect", needs=(READS, WRITES))
def deck_inspect(
    j: Job,
    pdf: Annotated[str, "Workspace ref of the compiled beamer PDF to look at (not the .tex)."],
    out: Annotated[str | None, "Folder for raw.json, deck.json and the debug images; default "
                               "out/<pdf stem>/."] = None,
    overlays: Annotated[str, "'last' keeps the final step of each frame (default), 'all' keeps "
                             "every overlay page."] = "last",
    checks: Annotated[bool, "Also run the local invariants (stray ink, lost ink, structure, junk "
                            "text). Roughly doubles the time."] = True,
    debug_images: Annotated[bool, "Also write debug/slide-NNN.png: the page with the classified "
                                  "boxes drawn on it."] = False,
) -> None:
    """Classify a beamer PDF and report what the deck would be made of, without touching Google.

    Extracts the PDF, groups spans into text, tables, diagrams, shapes and pictures, and writes
    raw.json and deck.json - the same files `deck_convert` builds from, so a conversion after
    this one costs nothing extra. Reports the frame labels, which decide whether a later sync
    can follow this deck, and (with checks=True) the local invariants that say whether anything
    would be lost or left behind.
    Costs 2-6 s for a 30-page deck, roughly double that with checks. No Google calls, no deck
    is created. Reach for it first, before converting, and whenever a conversion looked wrong.
    """
    started = time.time()
    source = _pdf(j, pdf)
    out_dir = _out_dir(j, out, source)
    _, raw, deck, facts = _classify_into(j, source, out_dir, overlays, debug_images)

    slides = _slide_rows(deck)
    totals: dict[str, int] = {}
    for row in slides:
        for kind, n in row["elements"].items():
            totals[kind] = totals.get(kind, 0) + n
    j.data.update(facts)
    j.data["out"] = j.ctx.workspace.ref(out_dir)
    j.data["title"] = raw["source"].get("title")
    j.data["elements"] = totals
    j.data["slides_detail"] = slides
    j.data["titles"] = [row["title"] for row in slides]

    survey = _label_survey(j, deck)
    j.data["labels"] = survey

    findings: list[dict] = []
    if checks:
        from .. import checks as invariants
        rendered = invariants.convert_locally(source, overlays)
        findings = invariants.run_checks(rendered)
        for f in findings:
            j.warn(f"{f['check']}: {f.get('detail', '')}".strip(),
                   where=f"slide {f['page'] + 1}" + (f" / {f['element']}" if f.get("element") else ""))
    j.data["checks"] = {"ran": checks,
                        "findings": len(findings),
                        "by_check": {c: sum(1 for f in findings if f["check"] == c)
                                     for c in sorted({f["check"] for f in findings})}}

    unlabelled = len(survey["unlabelled"])
    parts = [f"{facts['slides']} slide(s) from {source.name}, "
             f"{facts['native_share']:.0%} of the characters native "
             f"({', '.join(f'{n} {k}' for k, n in sorted(totals.items())) or 'nothing classified'})."]
    if facts["overlays"]["dropped"]:
        parts.append(f"{facts['overlays']['dropped']} overlay page(s) were dropped, keeping the "
                     f"last step of each frame.")
    if facts["notes"]:
        parts.append(f"Speaker notes were found on {facts['notes']['pages']} page(s) "
                     f"({facts['notes']['mode']}).")
    parts.append(f"{survey['frames'] - unlabelled} of {survey['frames']} frames carry a label of "
                 f"their own" + ("." if not unlabelled else
                                 f"; the other {unlabelled} would have to be identified by title and "
                                 f"position, which a later sync can lose track of."))
    if checks:
        parts.append("The local invariants found nothing." if not findings else
                     f"The invariants found {len(findings)} problem(s) - see the warnings before "
                     f"trusting the conversion.")
    j.summary = " ".join(parts)

    if unlabelled or survey["duplicates"]:
        j.suggest("tex_label on the .tex to give every frame a label of its own, then recompile")
    if not findings:
        j.suggest("deck_convert to build the Google Slides deck from this PDF")
    j.data["seconds"] = round(time.time() - started, 2)


@tool("deck_convert", needs=(READS, WRITES, WRITES_GOOGLE))
def deck_convert(
    j: Job,
    pdf: Annotated[str, "Workspace ref of the compiled beamer PDF to convert."],
    out: Annotated[str | None, "Folder holding this deck's state (emit.json, backgrounds, sync "
                               "base); default out/<pdf stem>/."] = None,
    title: Annotated[str | None, "Name for the presentation in Drive; default the PDF's title, "
                                 "else its file name."] = None,
    new_deck: Annotated[bool, "Create a new presentation instead of rebuilding the one this "
                              "folder already has."] = False,
    overlays: Annotated[str, "'last' keeps the final step of each frame (default), 'all' keeps "
                             "every overlay page."] = "last",
    measure: Annotated[bool, "Place formula and overlay pictures by measuring them on scratch "
                             "slides (2-3 s, much more accurate) rather than predicting."] = True,
    force_rebuild: Annotated[bool, "Rebuild although the deck was edited in Slides: its content "
                                   "is replaced, after a backup. Ask the person first."] = False,
    backup: Annotated[str, "What to keep before replacing a deck: auto, none, file (.pptx), "
                           "drive (a copy of the presentation), both."] = "auto",
) -> None:
    """Convert a beamer PDF into an editable Google Slides deck, creating or rebuilding it.

    Classifies the PDF, renders a background picture per slide, uploads one .pptx and fills in
    the native text, tables, diagrams and pictures, then records the sync base a later merge
    needs. Rebuilds the folder's previous deck in place at the same URL unless new_deck.
    Costs 12-30 s for a 30-page deck and several hundred Google calls; it creates or replaces a
    real presentation. If someone edited that deck in Slides it refuses with code `deck_edited`
    rather than destroying their work - use deck_sync then, not force_rebuild.
    """
    from ..emit import emit, preflight_rebuild
    from ..guard import RebuildRefused
    from ..render import render_backgrounds
    from ..snapshot import snapshot_after_convert

    started = time.time()
    _check_backup(backup)
    if overlays not in ("last", "all"):
        raise Refused("bad_request", f"overlays={overlays!r} is not 'last' or 'all'.", overlays=overlays)
    source = _pdf(j, pdf)
    out_dir = _out_dir(j, out, source)

    try:
        # Asked before any work, so a deck that must not be replaced costs a second, not a
        # conversion. `emit` asks again immediately before the write.
        preflight_rebuild(out_dir, source, new_deck, force_rebuild)
    except RebuildRefused as refused:
        _refuse_rebuild(j, refused, source, out_dir)

    pdf_path, raw, deck, facts = _classify_into(j, source, out_dir, overlays, debug_images=False)
    j.data.update(facts)
    j.data["out"] = j.ctx.workspace.ref(out_dir)
    render_backgrounds(pdf_path, raw, deck, out_dir)
    (out_dir / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    j.artifact(out_dir / "backgrounds", "folder", "one background picture per slide")
    survey = _label_survey(j, deck)
    j.data["labels"] = survey

    name = title or raw["source"]["title"] or source.stem
    try:
        state = emit(deck, out_dir, name, new_deck, measure, force_rebuild, backup, source)
    except RebuildRefused as refused:
        # Asked again immediately before the write, in case the deck was edited in between.
        _refuse_rebuild(j, refused, source, out_dir)
    j.artifact(out_dir / "emit.json", "json", "what was built: deck id, url, per-slide objects")

    previous = state.get("previous") or {}
    rebuilt = previous.get("action") == "rebuilt in place"
    j.data.update({
        "url": state["url"],
        "presentationId": state["presentationId"],
        "title": name,
        "rebuilt": rebuilt,
        "new_deck": not rebuilt,
        # What this rebuild replaced, and the .pptx it was kept as: the only way back there is,
        # because every Drive revision of a Slides file exports its *current* content.
        "backup": (previous.get("backup") or {}) if rebuilt else {},
        "replaced_revision": previous.get("revisionId") if rebuilt else None,
        "measured": measure,
    })

    base_slides = None
    try:
        base = snapshot_after_convert(state["deck"], out_dir, state, source, overlays)
        base_slides = len(base["slides"])
        j.artifact(out_dir / "sync" / "base.json", "json",
                   "the sync base: what this conversion put in the deck")
    except Exception as exc:  # noqa: BLE001 - the deck is complete; only a later sync needs the base
        j.warn(f"could not record the sync base ({type(exc).__name__}: {exc}); a later deck_sync "
               f"will refuse with no_base until this deck is converted again.", where="sync base")
    j.data["base_slides"] = base_slides
    j.data["seconds"] = round(time.time() - started, 2)

    j.summary = (f"{'Rebuilt' if rebuilt else 'Created'} a {facts['slides']}-slide deck "
                 f"\"{name}\" from {source.name} in {j.data['seconds']:.0f} s: {state['url']}. "
                 f"{facts['native_share']:.0%} of the characters are native text; the rest is in "
                 f"the per-slide background pictures."
                 + (f" The sync base records {base_slides} slide(s), so a later source change can "
                    f"be merged into this deck without losing edits." if base_slides else ""))
    j.suggest("deck_sync when the source changes, to merge into this deck instead of rebuilding it",
              "deck_inspect with checks=True if anything on the slides looks wrong")


def _refuse_rebuild(j: Job, refused: Any, source: Path, out_dir: Path) -> NoReturn:
    """Turn `guard.RebuildRefused` into the code an agent branches on. Always raises.

    The library's message names what was edited and offers three ways forward; those become
    `next_steps`, and the named edits become `data` so the agent can say *which* slides someone
    worked on rather than quoting a paragraph back at the user. The same exception is raised
    when a forced rebuild could not keep a backup, which is a different decision entirely
    (`no_way_back`: there is nothing to go back to, not somebody's work to protect).
    """
    survey = dict(getattr(refused, "survey", {}) or {})
    reason = survey.get("reason", "edited")
    code = "no_way_back" if reason == "backup-failed" else "deck_edited"
    ref = j.ctx.workspace.ref(out_dir)
    if code == "deck_edited":
        j.suggest(f"deck_sync(pdf={j.ctx.workspace.ref(source)!r}, deck={ref!r}) to merge this PDF "
                  f"into the deck, keeping the edits",
                  "deck_convert with new_deck=True to leave that deck alone and make a new one",
                  "deck_convert with force_rebuild=True to replace its content anyway (a backup is "
                  "kept first) - only after the person who edited it has said so")
    else:
        j.suggest("deck_convert with force_rebuild=True and backup='drive' to keep a Drive copy instead",
                  "deck_convert with force_rebuild=True and backup='none' to rebuild with no way back")
    pid = survey.get("presentationId")
    raise Refused(code, _rebuild_message(refused, survey, reason, code),
                  url=f"https://docs.google.com/presentation/d/{pid}/edit" if pid else None,
                  presentationId=pid,
                  reason=reason,
                  examples=list(survey.get("examples") or []),
                  counts=survey.get("counts") or {},
                  slides_added=survey.get("slides_added", 0),
                  slides_deleted=survey.get("slides_deleted", 0),
                  reordered=bool(survey.get("reordered")),
                  revisionId=survey.get("revisionId"),
                  out=ref)


def _rebuild_message(refused: Any, survey: dict, reason: str, code: str) -> str:
    """Say what the guard found, in this layer's vocabulary rather than the CLI's.

    The library's own message ends with three shell commands - `python -m beamer2slides
    convert ... --force-rebuild` among them - because it is written for a person at a
    terminal. Handed to a model it is worse than useless: prose is the first thing read,
    so the refusal would be teaching the one move it exists to prevent, and offering a
    shell as the way round a tool that just said no. The facts are all in `data` and the
    ways forward are all in `next_steps`, both in terms of calls that can actually be
    made; this says only what happened.
    """
    if code == "no_way_back":
        return ("The rebuild was forced, but no backup could be made, so there would be no way "
                "back to what is in the deck now. Every Drive revision of a Slides file exports "
                "its current content, so a .pptx export is the only way back there is. "
                "Nothing was written.")
    counts = survey.get("counts") or {}
    what = ", ".join(f"{n} {kind.replace('_', ' ')} change(s)" for kind, n in counts.items())
    added, deleted = survey.get("slides_added", 0), survey.get("slides_deleted", 0)
    for n, word in ((added, "added"), (deleted, "deleted")):
        if n:
            what += f", {n} slide(s) {word}"
    if survey.get("reordered"):
        what += ", slides reordered"
    if reason != "edited":
        # `no-base`, `other-pdf`: the deck may be untouched and still unsafe to replace.
        return (f"Refusing to rebuild this deck: {str(refused).splitlines()[0].strip() or reason}. "
                f"Rebuilding replaces the whole deck, and nothing here can say what would be "
                f"lost. Nothing was written.")
    examples = survey.get("examples") or []
    shown = "".join(f"\n  - {line}" for line in examples[:3])
    return (f"Refusing to rebuild: somebody edited this deck in Google Slides after it was last "
            f"written ({what or 'changes found'}). A rebuild replaces the whole deck, so their "
            f"work would be gone. Nothing was written.{shown}\n"
            f"The ways forward are in next_steps; forcing is one of them and is a decision for "
            f"the person who made those edits, not for you.")


@tool("deck_sync", needs=(READS, WRITES, READS_GOOGLE))
def deck_sync(
    j: Job,
    pdf: Annotated[str, "Workspace ref of the recompiled PDF: the new version of the source."],
    deck: Annotated[str, "The deck to merge into: presentation URL, presentation id, or the out "
                         "folder of its conversion."],
    out: Annotated[str | None, "Where the new conversion, base and reports go; default the "
                               "deck's own folder."] = None,
    dry_run: Annotated[bool, "Plan and report without writing anything to the deck; needs no "
                             "permission to write Google."] = False,
    overlays: Annotated[str | None, "'last' or 'all'; default the mode the deck was converted "
                                    "with."] = None,
    measure: Annotated[bool, "Measure formula and overlay picture placement on scratch slides "
                             "rather than predicting it."] = True,
    backup: Annotated[str, "Way back kept before the first write: auto (a .pptx), none, file, "
                           "drive, both."] = "auto",
    follow_labels: Annotated[bool, "Write to a slide even where a frame label may have moved onto "
                                   "another frame. By default such a slide is held back and nothing "
                                   "is written to it; only a person who has read the .tex and knows "
                                   "the labels are right can say this."] = False,
) -> None:
    """Merge a changed PDF into a deck someone has edited, three ways, keeping their edits.

    Reads the live deck, compares it with the sync base and the new conversion, and writes only
    what the source changed; where both sides changed the same thing the deck wins and the
    clash is reported as a conflict. Keeps a .pptx backup and records the deck's revision before
    the first write, which is the only way back there is.
    Costs 20-60 s and many Google calls. dry_run=True plans the whole merge and writes nothing,
    which is the right first call on any deck you did not just convert.
    """
    from ..sync import sync as run_sync

    started = time.time()
    _check_backup(backup)
    if overlays not in (None, "last", "all"):
        raise Refused("bad_request", f"overlays={overlays!r} is not 'last', 'all' or unset.", overlays=overlays)
    if not dry_run:
        # The gate let this journey in on READS_GOOGLE so a read-only context can still plan a
        # sync; a real write says so here - before the first request, and before a folder is made.
        j.require(WRITES_GOOGLE)
    source = _pdf(j, pdf)
    target, folder = _deck_arg(j, deck)
    out_dir = j.path(out, write=True) if out else folder
    if out_dir is None:
        # `sync.sync` would fall back to `paths.out_root()`, which answers a different folder
        # depending on how beamer2slides was installed. The workspace answers one.
        out_dir = j.ctx.workspace.out_dir(source.stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Lives in the CLI, not in `sync.sync`: without it there is no .pptx backup and no recovery
    # block, and Drive's revision history cannot give the old content back (docs/sync.md).
    note = None if dry_run else _cli().record_sync_point(source, target, out_dir, backup)

    try:
        info = run_sync(source, target, out_dir, dry_run, overlays, measure,
                        follow_labels=follow_labels)
    except SystemExit as exc:
        # `sync.sync` says no by exiting. Without a base there is nothing to merge against: the
        # deck's own edits cannot be told apart from what the last conversion put there.
        j.suggest("deck_convert to create the deck and its base, then sync from the next change on",
                  "check that `deck` names the folder or URL of a deck this workspace converted")
        raise Refused("no_base", str(exc.code) if exc.code not in (0, None) else
                      f"there is no sync base for {deck}, so a three-way merge is impossible",
                      deck=deck, out=j.ctx.workspace.ref(out_dir)) from None
    if note:
        _cli().add_recovery(note, info)

    report = info["report"]
    for clash in report["conflicts"]:
        j.conflict(f"{clash.get('element')}: {clash.get('field')} ({clash.get('resolution')})",
                   where=str(clash.get("slide")))
    for warning in report["warnings"]:
        j.warn(warning, where="sync")

    for name, kind in (("sync-report.md", "report"), ("sync-report.json", "json")):
        path = out_dir / "sync" / name
        if path.exists():
            j.artifact(path, kind, "what this sync applied, kept and could not decide")
    if info.get("recovery"):
        j.data["recovery"] = info["recovery"]

    # `sync.sync` counts requests per phase ({"text": 12, "pictures": 3}), and a dry run sends
    # none at all: what it planned is in `applied`, `overrides` and `conflicts`.
    sent = info.get("requests") or {}
    sent = sent if isinstance(sent, dict) else {"all": int(sent)}
    requests = sum(sent.values())
    wrote = bool(requests) and not dry_run
    j.data.update({
        "dry_run": dry_run,
        "wrote": wrote,
        "url": info["url"],
        "presentationId": info["presentationId"],
        "applied": len(report["applied"]),
        "kept": len(report["overrides"]),
        "conflicts": len(report["conflicts"]),
        "held": [h["slide"] for h in report["slides"].get("held") or []],
        "warnings": len(report["warnings"]),
        "requests": requests,
        "requests_by_phase": sent,
        "actions": info.get("actions", []),
        "overlays": info.get("overlays"),
        "base_from": info.get("base_from"),
        "generation": info.get("generation"),
        "report": j.ctx.workspace.ref(out_dir / "sync" / "sync-report.json"),
        "seconds": info.get("seconds", round(time.time() - started, 2)),
    })

    j.summary = (f"Sync{' (dry run)' if dry_run else ''} of {source.name} into {info['url']}: "
                 f"{len(report['applied'])} source change(s) "
                 f"{'would be applied' if dry_run else 'applied'}, "
                 f"{len(report['overrides'])} deck edit(s) kept, "
                 f"{len(report['conflicts'])} conflict(s). "
                 + ("Nothing was written to the deck." if not wrote else
                    f"{requests} request(s) were sent; the revision before them is in the "
                    f"recovery block.")
                 + (" Every conflict is a place both sides changed, where the deck won - read them "
                    "before deciding the source is right." if report["conflicts"] else "")
                 + (f" {len(j.data['held'])} slide(s) were held back with nothing written: a frame label "
                    f"may have moved onto another frame, so which frame those slides belong to is in "
                    f"doubt. That is a question for the person, not for you - ask them to check the "
                    f"`.tex`." if j.data.get("held") else ""))
    if dry_run and not report["conflicts"]:
        j.suggest("run deck_sync again with dry_run=False")
    elif dry_run:
        j.suggest("read the conflicts in sync-report.md, then run deck_sync again with dry_run=False",
                  "change the source where a conflict shows the deck is right")


def _deck_arg(j: Job, deck: str) -> tuple[str, Path | None]:
    """A deck given as a workspace folder, or as a URL/id the workspace knows nothing about.

    A folder is resolved (and confined) like any other ref; anything else is passed through, so
    a presentation URL is not mistaken for a path climbing out of the workspace.
    """
    if "/" in deck and "docs.google.com" in deck:
        return deck, None
    try:
        path = j.path(deck)
    except Refused:
        return deck, None
    if path.is_dir():
        return str(path), path
    return deck, None


@tool("tex_label", needs=(READS, WRITES))
def tex_label(
    j: Job,
    tex: Annotated[str, "Workspace ref of the document's main .tex (its \\input files are read "
                        "and labelled too)."],
    apply: Annotated[bool, "Write the labels into the source. Without it nothing is changed and "
                           "the plan is reported."] = False,
) -> None:
    """Give every frame that has none a `label=`, which is the identity a later sync follows.

    Reads the .tex (never the PDF), derives a unique label from each unlabelled frame's title
    and writes it into the frame's options. Existing labels are never touched or renamed: each
    one is a promise to a deck already converted from it. Duplicates are reported, never
    resolved - which of two frames a slide came from is a question only the author can answer.
    Costs under a second, no Google calls. apply=True edits the files, keeping what was there
    as <file>.bak. Do this before the first conversion, and recompile afterwards.
    """
    from .. import labels, texmap
    from ..inverse import keep_backup, replace_file

    path = j.path(tex)
    if not path.is_file():
        raise Refused("not_found", f"{tex}: no such file (this tool reads the .tex, not the PDF).",
                      ref=tex)
    source = texmap.Source(path)
    if not source.frames:
        raise Refused("bad_request",
                      rf"{tex}: no \begin{{frame}} found - is this the main file of a beamer "
                      rf"document?", ref=tex)

    edits = labels.plan(source)
    have = sum(1 for f in source.frames if f.label)

    # A label written on two frames reaches the PDF once (hyperref keeps the first destination of
    # a name), so the second frame arrives at every later stage looking unlabelled. Reading the
    # .tex is the only place this is visible at all.
    seen: dict[str, str] = {}
    duplicates: list[dict] = []
    for frame in source.frames:
        if not frame.label:
            continue
        where = f"{frame.file.name}:{frame.begin_line}"
        if frame.label in seen:
            duplicates.append({"label": frame.label, "first": seen[frame.label], "again": where,
                               "title": frame.title})
            j.conflict(f"label={frame.label} is on more than one frame ({seen[frame.label]} and "
                       f"{where}). hyperref keeps only the first, so the second frame reaches the "
                       f"PDF with no label at all and a sync cannot tell which of them a slide "
                       f"came from. Give each its own; nothing but this tool can see it.",
                       where=where)
        else:
            seen[frame.label] = where

    planned = [{"label": e["label"], "title": e["title"],
                "where": f"{e['file'].name}:{e['line']}",
                "file": j.ctx.workspace.ref(e["file"])} for e in edits]
    j.data.update({
        "tex": j.ctx.workspace.ref(path),
        "frames": len(source.frames),
        "labelled": have,
        "planned": len(edits),
        "edits": planned,
        "refs": sorted({p["file"] for p in planned}),
        "duplicates": duplicates,
        "applied": False,
    })

    if not edits:
        j.summary = (f"Every one of the {len(source.frames)} frame(s) in {path.name} already "
                     f"carries a label of its own; nothing to write."
                     + (f" {len(duplicates)} label(s) are on more than one frame, though, which "
                        f"makes those frames unidentifiable - see the conflicts."
                        if duplicates else ""))
        if duplicates:
            j.suggest("rename one frame of each duplicated label in the source, by hand")
        return

    if not apply:
        j.summary = (f"{len(source.frames)} frame(s) in {path.name}: {have} already labelled, "
                     f"{len(edits)} without. The labels that would be written are in "
                     f"data['edits']; nothing was changed. A frame without a label is identified "
                     f"by its title and position, which a reordered or retitled source breaks.")
        j.suggest("tex_label with apply=True to write them, then recompile and convert")
        return

    written = []
    for file_path, text in labels.apply(source, edits).items():
        j.path(str(file_path), write=True)  # an \input outside the workspace is refused, not written
        bak = keep_backup(file_path, text)
        replace_file(file_path, text)
        written.append({"file": j.ctx.workspace.ref(file_path),
                        "backup": j.ctx.workspace.ref(bak) if bak else None})
        j.artifact(file_path, "tex", "labelled source")
        if bak:
            j.artifact(bak, "tex", "the file as it was before the labels were written")
    j.data["applied"] = True
    j.data["written"] = written
    j.summary = (f"Wrote {len(edits)} label(s) into {len(written)} file(s) of {path.name}; "
                 f"every frame now carries one of its own and what was there is kept as .bak. "
                 f"Existing labels were left untouched. Recompile the document before converting "
                 f"or syncing, or the PDF still has the old identities.")
    j.suggest("recompile the document, then deck_inspect to confirm every frame is labelled")
