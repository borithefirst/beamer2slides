"""beamer2slides command line.

  python -m beamer2slides classify deck.pdf [--out DIR]
      DIR/raw.json, DIR/deck.json and DIR/debug/slide-NNN.png
  python -m beamer2slides convert deck.pdf [--out DIR] [--title TITLE] [--tex main.tex]
      classify + backgrounds + Google Slides deck (DIR/emit.json); --tex: speaker notes from the
      source when the PDF has no note pages (docs/speaker-notes.md)
  python -m beamer2slides pull --deck URL|ID|DIR --tex main.tex [--apply | --out SRC] [--max-iter N]
      edit the source until its conversion matches the (edited) deck: WORK/pull.patch, edits.md/json
  python -m beamer2slides converge --target deck.json --tex main.tex [...]
      the same against a deck.json-shaped target, offline
  python -m beamer2slides deck-files --deck URL|ID --out DIR [--zip]
      everything adopt reads of a deck, saved for `adopt --deck DIR|DIR.zip` with no Google or internet
  python -m beamer2slides docs push|sync doc.html [--dry-run]
      a Google Doc from a canonical HTML file, and the merge that keeps both in step
  python -m beamer2slides docs adopt --doc URL|ID [doc.html]
      the canonical file for a document nobody pushed: keys, anchors, base
"""

import argparse
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from .classify import classify
from .debug import render_debug
from .extract import Reading, extract, extract_read, select_overlays
from .ir import deck_json
from .notes import Prepared, notes_from_source, prepare_read, source_beside
from .paths import out_root
from .raw_types import RawDoc
from .typing_compat import assert_never

if TYPE_CHECKING:  # (the Google side is imported where it is used, as the CLI always has)
    from .google_types import DriveService, SlidesService
    from collections.abc import Mapping

    from .json_types import Json, JsonObject
    from .sync import SyncResult

BACKUP_MODES = ("auto", "none", "file", "drive", "both")  # = guard.BACKUP_MODES (imported lazily)
_V = TypeVar("_V")


def check_labels(deck: "Mapping[str, Json]", mode: str) -> None:
    """Say what the deck's frame labels cost a later sync (docs/labels.md). `error` refuses: a
    person who asked for that would rather fix the source than convert a deck sync cannot follow."""
    if mode == "off":
        return
    from . import identity, labels
    from .json_types import as_objects
    found = labels.problems(labels.survey([identity.slide_info(s) for s in as_objects(deck["slides"], "deck.slides")]))
    for line in found:
        print(f"  labels: {line}")
    if found and mode == "error":
        raise SystemExit("--check-labels error: the frames above need labels of their own")


PACKAGE_HINT = "`python -m beamer2slides notes-package` writes it"


def notes_of(reading: Reading, out: Path, tex: Path | None) -> Prepared:
    """The PDF's speaker notes (docs/speaker-notes.md): its note pages, or, when it has none and
    the person named its source (`--tex`), the source compiled once more with its notes shown,
    paired page by page. Without TeX the deck is converted without notes; a source that does not
    compile or is not this PDF's refuses. A source is never compiled unless named. (`reading`:
    the PDF, open for extract to read on.)"""
    pdf = reading.pdf
    prepared = prepare_read(reading, out)
    if prepared.pdf != out / "slides.pdf" and (out / "slides.pdf").exists():
        (out / "slides.pdf").unlink()  # stale from an earlier run of a PDF that had note pages
    if prepared.mode:
        print(f"speaker notes ({prepared.mode}): found notes for {len(prepared.notes)} pages")
        if tex is not None:
            print(f"  (--tex {tex.name} is not compiled: the PDF carries its notes)")
        return prepared
    if tex is None:
        beside = source_beside(pdf)
        if beside is not None:
            print(f"speaker notes: {pdf.name} has none, but {beside.name} beside it writes \\note: "
                  f"\\usepackage{{b2snotes}} in its preamble carries them in the PDF ({PACKAGE_HINT}), "
                  f"or --tex {beside.name} brings them now")
        return prepared
    found = notes_from_source(tex, pdf, out / "notes-source")
    match found.outcome:
        case "found":
            print(found.message)
            if found.notes:
                print("  a later sync from a PDF without note pages keeps these notes; to bring changed "
                      "notes, sync a PDF compiled with notes shown")
            return replace(prepared, notes=found.notes)
        case "no-engine" | "no-notes":
            print(f"speaker notes: {found.message}")
            return prepared
        case "failed" | "mismatch":
            raise SystemExit(f"speaker notes: {found.message}")
        case _:
            assert_never(found.outcome)


def cmd_classify(pdf: Path, out: Path, overlays: str, check: str) -> "tuple[Path, RawDoc, JsonObject]":
    return classify_pdf(pdf, out, overlays, check, None)


def classify_pdf(pdf: Path, out: Path, overlays: str, check: str,
                 tex: Path | None) -> "tuple[Path, RawDoc, JsonObject]":
    out.mkdir(parents=True, exist_ok=True)
    with Reading(pdf) as reading:
        prepared = notes_of(reading, out, tex)
        # the PDF as given, unless note pages were taken out of it: read on from the notes' pass
        raw = extract_read(reading, prepared.labels) if prepared.pdf == pdf else extract(prepared.pdf, prepared.labels)
    pdf = prepared.pdf
    for page in raw["pages"]:
        page["notes"] = prepared.notes.get(page["index"])
    raw = select_overlays(raw, overlays)
    dropped = raw["overlays"]["dropped"] if "overlays" in raw else 0
    if dropped:
        print(f"overlays: kept the last step of each frame, skipped {dropped} pages")
    (out / "raw.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    classified = classify(raw)
    deck = deck_json(classified)
    (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    render_debug(pdf, classified, out / "debug", 3.0)
    s = classified["stats"]
    print(f"{pdf.name}: {len(classified['slides'])} slides, {s['chars_native']}/{s['chars']} chars native "
          f"({s['native_share']:.0%}) -> {out}")
    for slide in classified["slides"]:
        left = ", ".join(f"{l['reason']} {len(l['spans'])}" for l in slide["left_in_background"])
        kinds = [e["kind"] for e in slide["elements"]]
        print(f"  slide {slide['page'] + 1:>2}: {kinds.count('text')} text boxes, {kinds.count('image')} pictures,"
              f" {kinds.count('shape')} shape candidates, {kinds.count('table')} tables,"
              f" {kinds.count('diagram')} diagrams; background: {left or '-'}")
    check_labels(deck, check)
    return pdf, raw, deck


def cmd_convert(pdf: Path, out: Path, title: str | None, new_deck: bool, overlays: str, measure: bool,
                force_rebuild: bool, backup: str, check: str, tex: Path | None) -> None:
    from .emit import emit, preflight_in_background
    from .render import render_backgrounds

    source = pdf
    # Whether this folder's deck may be replaced is asked while the PDF is converted (three Drive
    # reads and a whole presentations.get, needing nothing the conversion makes) and answered
    # before the first write to Drive. `emit` asks again immediately before that write.
    preflight = preflight_in_background(out, source, new_deck, force_rebuild)
    # (--check-labels error refuses here, before anything is written to Drive)
    pdf, raw, deck = classify_pdf(pdf, out, overlays, check, tex)  # pdf: without note pages, if there were any
    render_backgrounds(pdf, raw, deck, out, frozenset())
    (out / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    checked = preflight()  # RebuildRefused comes out here, with nothing yet written to Drive
    title = title or raw["source"]["title"] or source.stem
    built = emit(deck, out, title, new_deck, measure, force_rebuild, backup, source, checked)
    print(f"Google Slides: {built.state.url}")
    from .json_types import as_array
    from .snapshot import snapshot_after_convert
    try:
        base = snapshot_after_convert(built.deck, out, built.state, source, overlays, None)
        print(f"sync base: {len(as_array(base['slides'], 'base.slides'))} slides recorded "
              f"({out / 'sync' / 'base.json'})")
    except Exception as e:  # the deck is complete; only a later sync needs the base
        print(f"warning: could not record the sync base ({type(e).__name__}: {e})")


def record_sync_point(pdf: Path, deck: str, out: Path | None, backup: str):
    """Before sync's first write: the deck's revision (and a backup, `--backup`) so the state
    sync is about to change can be restored (docs/sync.md). Never fails the sync.

    Returns a `guard.WayBack`, which starts the three round trips on a thread of its own: they
    need nothing of the sync's reading and planning, and `sync` collects them at the one moment
    they are a promise about - before anything in the deck moves."""
    from .guard import WayBack
    return WayBack(lambda slides, drive: sync_point(pdf, deck, out, backup, slides, drive), "b2s-back")


def sync_point(pdf: Path, deck: str, out: Path | None, backup: str, slides: "SlidesService",
               drive: "DriveService") -> "JsonObject | None":
    from .guard import backup_deck, deck_url, record
    from .gslides import execute
    from .sync import resolve_deck
    try:
        pid, folder = resolve_deck(deck)
        out = out or folder or out_root() / pdf.stem
        rev = execute(slides.presentations().get(presentationId=pid, fields="revisionId")).get("revisionId")
        if rev is None:   # (asked for by name: never so; said as the missing key it was)
            raise KeyError("revisionId")
        info = execute(drive.files().get(fileId=pid, fields="modifiedTime"))
        # A sync only ever rewrites the parts the source changed, but the deck as a whole can only
        # be recovered from a file: Drive's version history is not readable back (docs/sync.md).
        entry: JsonObject = {
            "presentationId": pid, "url": deck_url(pid), "action": "synced", "revisionId": rev,
            "modifiedTime": info.get("modifiedTime"), "out": str(out),
            "checked": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": f"sync {pdf.name}",
            "backup": backup_deck(drive, pid, Path(out), "file" if backup == "auto" else backup, "", False, slides)}
        record(Path(out), entry)
        return {"out": str(out), "entry": entry}
    except Exception as e:  # noqa: BLE001 (a missing recovery note is no reason not to sync)
        print(f"warning: could not record the deck's revision before syncing ({type(e).__name__}: {e})")
        return None


def add_recovery(note: "JsonObject", result: "SyncResult") -> "SyncResult":
    """The recovery note (`sync_point`'s) on screen, in sync's result (one it already carries
    stays) and in its report files when the sync wrote them there."""
    from dataclasses import replace

    from .guard import restore_hint
    from .json_types import as_object, as_str
    from .sync import write_reports
    entry = as_object(note["entry"], "the recovery note's entry")
    print("recovery:")
    for line in restore_hint(entry, "sync"):
        print(line)
    noted = result if result.recovery is not None else replace(result, recovery=entry)
    out = Path(as_str(note["out"], "the recovery note's out"))
    if (out / "sync" / "sync-report.json").exists():
        try:
            write_reports(out, replace(result, recovery=entry))
        except OSError:
            pass
    return noted


def cmd_label(tex: Path, apply: bool) -> None:
    """Write a label into every frame that has none. Existing labels are never touched: each one is
    a promise to a deck that was converted from it (docs/labels.md)."""
    from . import labels, texmap
    from .inverse import keep_backup, replace_file
    if not tex.is_file():
        raise SystemExit(f"{tex}: no such file (this command reads the .tex, not the PDF)")
    source = texmap.Source(tex)
    if not source.frames:
        raise SystemExit(f"{tex}: no \\begin{{frame}} found - is this the main file of a beamer document?")
    edits = labels.plan(source)
    have = sum(1 for f in source.frames if f.label)
    print(f"{len(source.frames)} frame(s) in {tex}: {have} already labelled, {len(edits)} without")
    for e in edits:
        print(f"  {'writing' if apply else 'would write'} label={e['label']:<30} "
              f"{e['file'].name}:{e['line']}  {e['title'] or '(untitled)'}")
    seen: dict[str, str] = {}
    for f in source.frames:
        if f.label and f.label in seen:
            print(f"  ! label={f.label} is on more than one frame ({seen[f.label]}, {f.file.name}:{f.begin_line}): "
                  f"a sync cannot tell which of them a slide came from. Please give each its own.")
        elif f.label:
            seen[f.label] = f"{f.file.name}:{f.begin_line}"
    if not edits:
        print("every frame already carries a label of its own")
        return
    if not apply:
        print(f"{len(edits)} frame(s) would be labelled. Add --apply to write them.")
        return
    for path, text in labels.apply(source, edits).items():
        bak = keep_backup(path, text)
        replace_file(path, text)
        print(f"  {path}{f' (was kept as {bak.name})' if bak else ''}")
    print(f"labelled {len(edits)} frame(s). Recompile, then convert or sync as usual.")


@dataclass(frozen=True, kw_only=True)
class DocsPush:
    """`docs push`, as the command line said it."""
    file: Path
    name: str | None
    new_doc: bool


@dataclass(frozen=True, kw_only=True)
class DocsAdopt:
    """`docs adopt`: the document, and where its file goes (None: a slug of its title)."""
    doc: str
    file: Path | None
    force: bool


@dataclass(frozen=True, kw_only=True)
class DocsSync:
    """`docs sync`: the file, the document when the file's own is not the one, and how."""
    file: Path
    doc: str | None
    dry_run: bool
    assume_base: str | None
    backup: bool


def _said(args: argparse.Namespace, name: str) -> object:
    """One of argparse's answers, as a value still to be narrowed."""
    value: object = getattr(args, name)
    return value


def _path(args: argparse.Namespace, name: str) -> Path | None:
    value = _said(args, name)
    if value is None or isinstance(value, Path):
        return value
    raise SystemExit(f"--{name}: a path was expected, found {value!r}")


def _text(args: argparse.Namespace, name: str) -> str | None:
    value = _said(args, name)
    if value is None or isinstance(value, str):
        return value
    raise SystemExit(f"--{name}: a word was expected, found {value!r}")


def _flag(args: argparse.Namespace, name: str) -> bool:
    return _said(args, name) is True


def _given(value: _V | None, name: str) -> _V:
    if value is None:
        raise SystemExit(f"{name} is required")
    return value


def docs_command(args: argparse.Namespace) -> DocsPush | DocsAdopt | DocsSync:
    """The `docs` subcommand argparse read, as the record its journey takes. Every
    default is argparse's, decided here once and passed on whole."""
    command = _text(args, "docs_command")
    if command == "push":
        return DocsPush(file=_given(_path(args, "file"), "file"), name=_text(args, "name"),
                        new_doc=_flag(args, "new_doc"))
    if command == "adopt":
        return DocsAdopt(doc=_given(_text(args, "doc"), "--doc"), file=_path(args, "file"),
                         force=_flag(args, "force"))
    if command == "sync":
        return DocsSync(file=_given(_path(args, "file"), "file"), doc=_text(args, "doc"),
                        dry_run=_flag(args, "dry_run"), assume_base=_text(args, "assume_base"),
                        backup=not _flag(args, "no_backup"))
    raise SystemExit(f"docs: no command {command!r}")


def cmd_docs(args: argparse.Namespace) -> None:
    """Google Docs: the canonical HTML file and the document, kept in step (docs/google-docs.md)."""
    from .doc_sync import adopt, push, sync
    said = docs_command(args)
    if isinstance(said, DocsPush):
        pushed = push(said.file, said.name, said.new_doc)
        for note in pushed["notes"]:
            print(f"  note: {note}")
        print(f"{said.file}: {pushed['blocks']} blocks, {pushed['anchored']} of them anchored")
        print(f"Google Docs: {pushed['url']}")
        return
    if isinstance(said, DocsAdopt):
        # A file named by the document's title lands in the folder the person typed in.
        adopted = adopt(said.doc, said.file, said.force, None)
        for note in adopted["notes"]:
            print(f"  note: {note}")
        print(f"{adopted['file']}: {adopted['blocks']} blocks, {adopted['anchored']} of them "
              f"anchored, {adopted['tabs']} tab(s)")
        print(f"Google Docs: {adopted['url']}")
        print(f"`docs sync {adopted['file']}` from here on.")
        return
    info = sync(said.file, said.doc, said.dry_run, said.assume_base, said.backup)
    for clash in info["conflicts"]:
        print(f"  conflict {clash['key']}: the source said {clash['ours']!r}, "
              f"the document says {clash['theirs']!r} (the document wins)")
    for note in info["notes"]:
        print(f"  note: {note}")
    for comment in info.get("comments", []):
        print(f"  open comment: {comment}")
    print(f"docs sync{' (dry run)' if said.dry_run else ''}: {len(info['applied'])} block(s) from "
          f"the source, {len(info['kept'])} kept from the document, "
          f"{len(info['conflicts'])} conflict(s), {info['requests']} request(s)")
    if info.get("backup"):
        print(f"  the document was exported to {info['backup']} before being written over")
    print(f"report: {info.get('report')}")
    print(f"Google Docs: {info['url']}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="beamer2slides")
    ap.add_argument("--no-downloads", action="store_true",
                    help="never download a URL (pictures, thumbnails, fonts): the deck's pictures are "
                         "signed from this run's files and a Drive export instead; inline formula "
                         "pictures keep their predicted places and `fidelity` cannot run "
                         "(= $B2S_NO_DOWNLOADS=1, net.no_downloads)")
    ap.add_argument("--drive-folder", metavar="auto|none|ID",
                    help="where every file this creates in Drive goes (decks, documents, sync bases, "
                         "backup copies, temporary staging): auto (default) = a 'beamer2slides' folder of "
                         "the app's own; none = new decks in My Drive's root and a base beside its deck; "
                         "or a folder id (= $B2S_DRIVE_FOLDER, drive_folder.py)")
    sub = ap.add_subparsers(dest="command", required=True)
    for name, help_text in (("classify", "extract + classify a PDF, with debug images"),
                            ("convert", "full conversion into a Google Slides deck"),
                            ("fidelity", "compare the emitted deck with the PDF")):
        c = sub.add_parser(name, help=help_text)
        c.add_argument("pdf", type=Path)
        c.add_argument("--out", type=Path)
        if name in ("classify", "convert"):
            c.add_argument("--overlays", choices=["last", "all"], default="last",
                           help="for PDFs with overlay steps: keep the last step of each frame (default) or all pages")
            c.add_argument("--tex", type=Path,
                           help="the PDF's beamer source: when the PDF has no note pages, it is compiled once "
                                "more with its notes shown (a copy in <out>/notes-source) and each \\note goes "
                                "to its slide's speaker notes. Refused when the source's pages are not the "
                                "PDF's; without TeX the deck is converted without notes (docs/speaker-notes.md)")
        if name == "convert":
            c.add_argument("--title")
            c.add_argument("--new-deck", action="store_true",
                           help="create a new presentation instead of rebuilding the previous one")
            c.add_argument("--force-rebuild", action="store_true",
                           help="rebuild the previous deck even though it was edited in Slides "
                                "(its content is replaced; a backup is kept first)")
            c.add_argument("--backup", choices=list(BACKUP_MODES), default="auto",
                           help="what to keep before replacing a deck's content: auto (a .pptx when the "
                                "rebuild is forced), none, file (.pptx in <out>/backups), drive (a copy "
                                "of the presentation), both")
            c.add_argument("--predict-places", "--predict-holes", dest="predict_places", action="store_true",
                           help="place inline formula and overlay pictures by prediction only, without measuring "
                                "their gaps and words on scratch slides")
        if name in ("classify", "convert"):
            c.add_argument("--check-labels", choices=["off", "warn", "error"], default="warn",
                           help="frames whose identity a later sync cannot rely on (docs/labels.md): report them "
                                "(default), refuse the conversion, or say nothing. A PDF shows a frame without a "
                                "`label=` of its own; a label written on two frames reaches it only once, so the "
                                "second frame is reported as unlabelled - `beamer2slides label` names it")
        if name == "fidelity":
            c.add_argument("--refresh", action="store_true", help="re-export slide thumbnails")
    c = sub.add_parser("sync", help="merge a changed PDF into the edited deck (docs/sync.md)")
    c.add_argument("pdf", type=Path)
    c.add_argument("--deck", required=True, help="presentation URL or id, or the output folder of its convert")
    c.add_argument("--out", type=Path, help="where the new conversion, base and reports go (default: the deck's folder)")
    c.add_argument("--dry-run", action="store_true", help="plan and report without writing to the deck")
    c.add_argument("--overlays", choices=["last", "all"], default=None,
                   help="which overlay steps to keep (default: the ones the deck was converted with)")
    c.add_argument("--predict-places", dest="predict_places", action="store_true")
    c.add_argument("--backup", choices=list(BACKUP_MODES), default="auto",
                   help="keep a way back before sync's first write: auto = file (a .pptx in <out>/backups), "
                        "none, drive (a copy of the presentation), both")
    c.add_argument("--follow-labels", dest="follow_labels", action="store_true",
                   help="write to a slide whose label may have moved onto another frame (docs/sync.md, When a "
                        "label moved): by default such a slide is held back and nothing is written to it")
    c.add_argument("--take-source", dest="take_source", action="append", metavar="ID", default=[],
                   help="settle one reported conflict for the source instead of the deck (docs/sync.md, Taking "
                        "the source's version): ID is the id the last report gave that conflict, repeat or "
                        "comma-separate for several. It writes over what the person wrote there, so the report "
                        "keeps their version verbatim; an id whose conflict has since changed matches nothing")
    c.add_argument("--force-adopted-deck", dest="force_adopted", action="store_true",
                   help="write into an adopted deck although sync cannot vouch for what it would write "
                        "(docs/sync.md, Adopt): the objects were made by a person, not by this converter")
    for name, help_text in (("pull", "edit the beamer source until its conversion matches an edited deck"),
                            ("converge", "offline pull: edit the source until its conversion matches a deck.json")):
        c = sub.add_parser(name, help=help_text)
        if name == "pull":
            c.add_argument("--deck", required=True, help="deck URL, presentation id or convert output folder")
        else:
            c.add_argument("--target", required=True, type=Path, help="deck.json-shaped target IR")
        c.add_argument("--tex", required=True, type=Path)
        c.add_argument("--apply", action="store_true", help="patch the source in place (what was there is kept as <file>.bak, .bak2, ...)")
        c.add_argument("--out", type=Path, help="write the edited source tree here instead")
        c.add_argument("--work", type=Path, help="loop folder and reports (default: <deck folder>/pull)")
        c.add_argument("--max-iter", type=int, default=10)
        c.add_argument("--handout", action="store_true", help="compile in handout mode (one page per frame)")
        c.add_argument("--engine", help="pdflatex, xelatex or lualatex (default: from the source)")
    from .deck_files import PARTS
    c = sub.add_parser("deck-files", help="save everything adopt reads of a deck, for an adopt with no Google "
                                          "and no internet (adopt --deck FOLDER|ZIP)")
    c.add_argument("--deck", required=True, help="deck URL or presentation id")
    c.add_argument("--out", required=True, type=Path, help="the folder to write (new or empty): "
                   "presentation.json, thumbnails/, pictures/, google-fonts/, deck-files.json")
    c.add_argument("--zip", action="store_true", help="also pack the folder as <out>.zip: one file to hand over")
    c.add_argument("--pptx", type=Path, metavar="FILE", help="a .pptx of the deck to put in with them: " + PARTS["pptx"])
    c = sub.add_parser("adopt", help="write a beamer source for a deck nobody converted, then converge it")
    c.add_argument("--deck", required=True,
                   help="deck URL or presentation id (read live); or the deck as files, read with no "
                        "Google call: the folder or .zip `deck-files` wrote (as faithful as a live "
                        "read, with no network), a saved presentations.get .json (" + PARTS["presentation"]
                        + "; add the parts below), or a deck.json-shaped target")
    c.add_argument("--thumbnails", type=Path, action="append", default=[], metavar="FILE|FOLDER",
                   help="with a deck read from files: " + PARTS["thumbnails"] + ". Pictures named by slide "
                        "number (001.png) or objectId. Repeatable")
    c.add_argument("--pictures", type=Path, metavar="FOLDER",
                   help="with a deck read from files: " + PARTS["pictures"] + " (deck-files' pictures/)")
    c.add_argument("--google-fonts", type=Path, metavar="FOLDER",
                   help="with a deck read from files: " + PARTS["google_fonts"] + " (deck-files' google-fonts/)")
    c.add_argument("--tex", required=True, type=Path, help="the source to write (it must not exist yet)")
    c.add_argument("--flow", action="store_true",
                   help="write frame titles and body text in the flow instead of a textblock per element: "
                        "readable beamer, further from the deck")
    c.add_argument("--apply", action="store_true", help="keep what the loop edits (default: it is reported only)")
    c.add_argument("--out", type=Path, help="write the edited source tree here instead")
    c.add_argument("--work", type=Path, help="loop folder and reports (default: <tex folder>/out/adopt)")
    c.add_argument("--max-iter", type=int, default=6)
    c.add_argument("--engine", help="pdflatex, xelatex or lualatex (default: from the source)")
    c.add_argument("--no-base", dest="base", action="store_false",
                   help="do not record a sync base for the adopted deck (a later sync then has nothing "
                        "to merge into it)")
    c.add_argument("--base-in-drive", action="store_true",
                   help="also store the base in the deck's own appProperties, as convert does. This WRITES to "
                        "the presentation, which adopt otherwise never does: ask for it only for a deck you own")
    c.add_argument("--fonts", type=Path, action="append", default=[], metavar="FILE|FOLDER",
                   help=PARTS["fonts"] + " (.ttf .otf .ttc .woff .woff2, or a folder of them); "
                        "preferred to this machine's and to google/fonts. Repeatable. A local copy of "
                        "google/fonts is $B2S_FONT_SOURCE instead")
    c.add_argument("--pptx", type=Path, metavar="FILE",
                   help=PARTS["pptx"] + ". Used before any download, so --no-downloads still gets them "
                        "(after --pictures, whose bytes are Google's own). Download it from the deck as it is now")
    c = sub.add_parser("docs", help="a Google Doc from a canonical HTML file, and back (docs/google-docs.md)")
    docs_sub = c.add_subparsers(dest="docs_command", required=True)
    d = docs_sub.add_parser("push", help="create the document from the file and anchor its blocks")
    d.add_argument("file", type=Path, help="the canonical HTML file (it is rewritten with the keys)")
    d.add_argument("--name", help="the document's name in Drive (default: the file's <title>)")
    d.add_argument("--new-doc", action="store_true",
                   help="create a second document although the file already names one")
    d = docs_sub.add_parser("adopt", help="write the canonical file for a document nobody pushed")
    d.add_argument("--doc", required=True, help="document URL or id")
    d.add_argument("file", type=Path, nargs="?",
                   help="where the canonical HTML goes (default: a slug of the document's title)")
    d.add_argument("--force", action="store_true",
                   help="write over a file that is already there and names another document")
    d = docs_sub.add_parser("sync", help="merge file and document three ways, then rewrite the file")
    d.add_argument("file", type=Path)
    d.add_argument("--doc", help="document URL or id (default: the <meta> in the file)")
    d.add_argument("--dry-run", action="store_true", help="plan and report without writing")
    d.add_argument("--assume-base", choices=["document-wins", "source-wins", "file", "document"],
                   metavar="{document-wins,source-wins}",
                   help="when no base of the last sync can be found (neither in Drive nor beside "
                        "the file): whose work is discarded. document-wins = nothing is written "
                        "to the document and the file is rewritten from it (source edits since "
                        "the last sync are lost); source-wins = the file is written over the "
                        "live document (the reader's edits are lost), after exporting it to "
                        ".b2s/backups/. `file` and `document` are the old names for the two, "
                        "and they read backwards")
    d.add_argument("--no-backup", action="store_true",
                   help="do not export the document before --assume-base source-wins writes over "
                        "it: this is how one asks for a write with no way back")
    c = sub.add_parser("label", help="write a `label=` into every frame that has none (docs/labels.md)")
    c.add_argument("tex", type=Path, help="the main .tex (its \\input files are labelled too)")
    c.add_argument("--apply", action="store_true",
                   help="edit the source in place (what was there is kept as <file>.bak, .bak2, ...)")
    c = sub.add_parser("notes-package", help="write b2snotes.sty: \\usepackage{b2snotes} makes the PDF one presents "
                                             "from carry its speaker notes (docs/speaker-notes.md)")
    c.add_argument("--out", type=Path, default=Path("."), help="the folder to write it into (default: here), "
                   "the one holding the deck's .tex")
    c = sub.add_parser("playground", help="a web app that runs the pipeline on a talk typed or uploaded (docs/playground.md)")
    c.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to serve other machines (default: this one only)")
    c.add_argument("--port", type=int, default=7860)
    args = ap.parse_args()
    if args.no_downloads:
        import os
        from .net import NO_DOWNLOADS
        os.environ[NO_DOWNLOADS] = "1"   # (this process only; `google_auth.fetcher_for_threads`)
    if args.drive_folder:
        import os
        from .drive_folder import FOLDER_ENV
        os.environ[FOLDER_ENV] = args.drive_folder   # (this process only; worker threads see it too)
    if args.command == "playground":
        from .playground.server import serve
        return serve(args.host, args.port)
    if args.command == "label":
        return cmd_label(args.tex, args.apply)
    if args.command == "notes-package":
        from .notes import write_package
        print(f"wrote {write_package(args.out)}: put \\usepackage{{b2snotes}} in the deck's preamble")
        return
    if args.command == "docs":
        return cmd_docs(args)
    if args.command == "deck-files":
        from .deck_files import save, zip_folder
        save(args.deck, args.out, args.pptx, print)
        if args.zip:
            print(f"wrote {zip_folder(args.out, args.out.with_suffix('.zip'))}")
        return
    if args.command == "adopt":
        from .adopt import cmd_adopt
        from .deck_files import gather
        work = args.work or args.tex.resolve().parent / "out" / "adopt"
        files = gather(args.deck, work / "deck-files", args.thumbnails, args.pictures, args.google_fonts)
        target = Path(args.deck) if files is None and Path(args.deck).suffix == ".json" else None
        cmd_adopt(args.deck, args.tex, args.work, args.apply, args.out, args.max_iter, args.engine,
                  args.flow, target, base=args.base, base_in_drive=args.base_in_drive, log=print, fonts=args.fonts,
                  found=None, pptx=args.pptx, files=files)
        return
    if args.command in ("pull", "converge"):
        from .inverse import cmd_converge, cmd_pull
        if args.command == "pull":
            cmd_pull(args.deck, args.tex, args.work, args.apply, args.out, args.max_iter, args.handout, args.engine)
        else:
            cmd_converge(args.target, args.tex, args.work, args.apply, args.out, args.max_iter, args.handout, args.engine)
        return
    if args.command == "sync":
        from . import merge
        from .adopt_sync import FirstSyncRefused
        from .sync import sync
        note = None if args.dry_run else record_sync_point(args.pdf, args.deck, args.out, args.backup)
        try:
            # The recovery note carries the backup that was (or was not) kept: an adopted deck has
            # no way back at all unless one was, so the refusal has to be able to ask.
            # One --take-source may name several ids, since a report that lists three of them is
            # read in one go and typed back in one go.
            take = [p.strip() for arg in args.take_source for p in str(arg).split(",") if p.strip()]
            result = sync(args.pdf, args.deck, args.out, args.dry_run, args.overlays, not args.predict_places,
                          note, args.backup, args.force_adopted, args.follow_labels, take)
        except FirstSyncRefused as refused:
            raise SystemExit(str(refused)) from None
        # Only a sync that wrote asked for its way back; one that did not is not kept waiting.
        kept = note.kept() if note else None
        if kept:
            result = add_recovery(kept, result)
        r = result.report
        sent = result.requests                 # Sync.sent counts them per phase, not in total
        held, resolved = r.slides.held, r.resolved
        print(f"sync{' (dry run)' if args.dry_run else ''}: {len(r.applied)} source changes applied, "
              f"{len(r.overrides)} deck edits kept, {len(r.conflicts)} conflicts, "
              + (f"{len(resolved)} settled for the source, " if resolved else "")
              + (f"{len(held)} slide(s) held back, " if held else "") +
              f"requests {sum(sent.values())}")
        for c in r.conflicts:
            print(f"  conflict: {c['slide']} / {c['element']}: {c['field']} ({c['resolution']})"
                  + (f" [--take-source {c['id']}]" if c.get("takeable") and c.get("id")
                     and c["resolution"] != merge.TAKEN_SAYS else ""))
        for wmsg in r.warnings:
            print(f"  warning: {wmsg}")
        print(f"Google Slides: {result.url}")
        return
    out = args.out or out_root() / args.pdf.stem
    if args.command == "classify":
        classify_pdf(args.pdf, out, args.overlays, args.check_labels, args.tex)
    elif args.command == "convert":
        from .guard import RebuildRefused
        try:
            cmd_convert(args.pdf, out, args.title, args.new_deck, args.overlays, not args.predict_places,
                        args.force_rebuild, args.backup, args.check_labels, args.tex)
        except RebuildRefused as refused:
            raise SystemExit(str(refused)) from None
    elif args.command == "fidelity":
        from .fidelity import measure, print_report
        prepared = out / "slides.pdf"  # the notes-free PDF the deck was built from
        print_report(measure(prepared if prepared.exists() else args.pdf, out, args.refresh))


if __name__ == "__main__":
    main()
