"""Proof on a live deck that `convert` can no longer destroy someone's edits, and that a forced
rebuild can be undone (docs/sync.md, "Never lose deck edits").

    python tools/rebuild_guard_proof.py [--pdf tests/decks/sync/out/v1.pdf] [--out out/agent-guard]

It converts a deck, then:
  1. converts again           -> rebuilt in place, no false alarm;
  2. edits it like a person (tools/deck_edits.py) and converts       -> refused, exit code != 0,
     and the live deck is byte-for-byte the revision it was before;
  3. converts with --force-rebuild -> rebuilt, a .pptx backup and the old revisionId recorded;
  4. recovers: the .pptx backup goes back into a new presentation that still shows the edits
     (Drive's version history is only recorded as evidence: it gives the deck's current content
     back for every revision, see tools/probe_revision_history.py);
  4b. recovers at the same URL (`restore --in-place`) and compares the deck with what it showed
     before the rebuild, word for word, notes included: nothing may be missing;
  4c. syncs the recovered deck against the same source, because a recovery that can't be worked
     with afterwards is only half a way back: the recovered edits must still be there;
  5. --new-deck leaves the old deck alone, and a trashed deck is never resurrected.
The new decks this makes (the restored copy, the --new-deck one) are trashed again at the end.
Evidence: <out>/proof.json, and every command's output in <out>/proof.log.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.gslides import execute

ROOT = Path(__file__).resolve().parents[1]


def cli(log, *args) -> subprocess.CompletedProcess:
    log.write(f"\n$ beamer2slides {' '.join(map(str, args))}\n")
    log.flush()
    done = subprocess.run([sys.executable, "-m", "beamer2slides", *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    log.write(done.stdout + done.stderr)
    log.flush()
    return done


def tool(log, *args) -> subprocess.CompletedProcess:
    log.write(f"\n$ python tools/{' '.join(map(str, args))}\n")
    log.flush()
    done = subprocess.run([sys.executable, str(ROOT / "tools" / str(args[0])), *map(str, args[1:])], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    log.write(done.stdout + done.stderr)
    log.flush()
    return done


def revision(slides, pid: str) -> str:
    return execute(slides.presentations().get(presentationId=pid, fields="revisionId"))["revisionId"]


def pptx_holds(path: Path, text: str) -> bool:
    import zipfile
    with zipfile.ZipFile(path) as z:
        return any(text.encode() in z.read(n) for n in z.namelist() if n.endswith(".xml"))


def deck_text(slides, pid: str) -> str:
    from beamer2slides.devtools import sync_check as sc
    return " ".join(s.all_text for s in sc.read(pid).slides)


def lost_words(before, after) -> dict:
    """Words the deck showed before and doesn't show any more, per slide (a multiset difference:
    a word written twice and shown once is a loss too). Slides are compared by position, and a
    missing slide loses all of its words, so this counts every way the content can shrink."""
    from collections import Counter
    lost = {}
    for i, slide in enumerate(before.slides):
        had = Counter((slide.all_text + " " + slide.notes).split())
        now = Counter((after.slides[i].all_text + " " + after.slides[i].notes).split()) \
            if i < len(after.slides) else Counter()
        gone = had - now
        if gone:
            lost[str(i)] = sorted(gone.elements())[:20]
    return lost


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", type=Path, default=ROOT / "tests" / "decks" / "sync" / "out" / "v1.pdf")
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "agent-guard")
    args = ap.parse_args()
    out = args.out / args.pdf.stem
    out.mkdir(parents=True, exist_ok=True)
    slides, drive = slides_service(), drive_service()
    problems, notes = [], {}
    started = time.time()
    with open(args.out / "proof.log", "w", encoding="utf-8") as log:
        from beamer2slides.devtools import deck_edits  # (tools/<name>.py is a shim that runs one)
        from beamer2slides.devtools import sync_check as sc

        # 1. convert, then convert again: a deck nobody touched is rebuilt without a word of warning.
        if cli(log, "convert", args.pdf, "--out", out).returncode:
            print(f"the first conversion failed, see {args.out / 'proof.log'}")
            return 1
        pid = json.loads((out / "emit.json").read_text(encoding="utf-8"))["presentationId"]
        notes["presentationId"] = pid
        again = cli(log, "convert", args.pdf, "--out", out)
        if again.returncode:
            problems.append("converting an untouched deck a second time was refused (false alarm)")
        notes["rebuild_untouched"] = {"returncode": again.returncode,
                                      "said": [l for l in again.stdout.splitlines() if "existing deck" in l]}

        # 2. a person edits the deck, then someone re-runs convert.
        deck = deck_edits.LiveDeck(pid, slides)
        target = max((e for e in deck.model.slides[1].elements if e.kind == "shape" and len(e.text.split()) > 3),
                     key=lambda e: len(e.text))
        phrase = " ".join(target.text.split()[:6])
        word = max(phrase.split(), key=len)
        deck_edits.replace_word(deck, {"index": 1}, phrase, word, "HANDWRITTEN")
        deck_edits.add_text_box(deck, {"index": 1}, "a box the person added", [400, 330, 220, 40])
        edited_revision = revision(slides, pid)
        edited_model = sc.read(pid)                       # what the person's deck showed, word for word
        notes["edits"] = {"slide": deck.model.slides[1].title, "phrase": phrase, "word": word,
                          "revision": edited_revision}
        refused = cli(log, "convert", args.pdf, "--out", out)
        notes["refusal"] = {"returncode": refused.returncode,
                            "message": (refused.stdout + refused.stderr).strip().splitlines()[-9:]}
        if refused.returncode == 0:
            problems.append("convert rebuilt a deck that had been edited")
        if "refusing to rebuild" not in refused.stdout + refused.stderr:
            problems.append("the refusal doesn't say it is refusing to rebuild")
        if revision(slides, pid) != edited_revision:
            problems.append("the refused convert changed the deck (its revision moved)")
        if "HANDWRITTEN" not in deck_text(slides, pid):
            problems.append("the refused convert lost the person's edit")

        # 3. the same conversion, forced: the deck is replaced, but a way back is kept.
        forced = cli(log, "convert", args.pdf, "--out", out, "--force-rebuild")
        if forced.returncode:
            problems.append("--force-rebuild failed")
        log_entries = json.loads((out / "backups" / "backups.json").read_text(encoding="utf-8"))
        entry = next((e for e in reversed(log_entries) if e.get("reason") == "edited"), None)  # this run's
        notes["forced"] = {"returncode": forced.returncode, "entry": entry,
                           "said": [l for l in forced.stdout.splitlines() if "WARNING" in l or "backup" in l
                                    or "revision" in l]}
        if entry is None:
            problems.append("the forced rebuild recorded no backup entry")
        else:
            if entry["revisionId"] != edited_revision:
                problems.append("the recorded revision is not the one the deck had when it was edited")
            backup_file = Path(entry["backup"].get("file", ""))
            if not backup_file.exists():
                problems.append("the forced rebuild kept no .pptx backup")
            notes["backup_file"] = {"path": str(backup_file), "bytes": backup_file.stat().st_size
                                    if backup_file.exists() else None}
        if "HANDWRITTEN" in deck_text(slides, pid):
            problems.append("the forced rebuild did not actually rebuild the deck")

        # 4. recovery, both ways: the local .pptx, and Drive's version history.
        restored = []
        listed = tool(log, "deck_backup.py", "list", "--deck", out)
        notes["history"] = {"returncode": listed.returncode, "lines": listed.stdout.splitlines()[:12]}
        if entry and Path(entry["backup"].get("file", "")).exists():
            done = tool(log, "deck_backup.py", "restore", "--deck", out, "--from", entry["backup"]["file"])
            rid = done.stdout.strip().rsplit("/d/", 1)[-1].split("/")[0] if "/d/" in done.stdout else None
            notes["restore_from_file"] = {"returncode": done.returncode, "presentationId": rid}
            if rid:
                restored.append(rid)
                if "HANDWRITTEN" not in deck_text(slides, rid):
                    problems.append("the deck restored from the .pptx backup doesn't show the edits")
            else:
                problems.append("restoring from the .pptx backup produced no deck")
        # Drive's version history is not a recovery path for a converted deck: it keeps a revision
        # row per editing session, but every revision exports the file's *current* content
        # (tools/probe_revision_history.py). Recorded here as evidence, not as a way back.
        if entry and entry.get("modifiedTime"):
            done = tool(log, "deck_backup.py", "export", "--deck", out,
                        "--revision", f"before:{entry['modifiedTime']}",
                        "--to", out / "backups" / "from-history.pptx")
            path = out / "backups" / "from-history.pptx"
            holds = pptx_holds(path, "HANDWRITTEN") if path.exists() else None
            notes["drive_history"] = {"returncode": done.returncode, "exported": path.exists(),
                                      "holds_the_edit": holds,
                                      "note": "a revision's export gives the deck's current content"}

        # 4b. the way back people actually want: the same URL, holding what it held before the
        # rebuild. `restore --in-place` uploads the backup over the deck, so every link, embed and
        # bookmark keeps working. What came back is compared word for word with what was lost.
        recovered = None
        if entry and Path(entry["backup"].get("file", "")).exists():
            done = tool(log, "deck_backup.py", "restore", "--deck", out, "--from", entry["backup"]["file"],
                        "--in-place")
            recovered = sc.read(pid)
            lost = lost_words(edited_model, recovered)
            notes["restore_in_place"] = {
                "returncode": done.returncode, "presentationId": pid,
                "slides": [len(edited_model.slides), len(recovered.slides)],
                "holds_the_edit": "HANDWRITTEN" in " ".join(s.all_text for s in recovered.slides),
                "lost_words": lost}
            if done.returncode:
                problems.append("restoring the backup into the deck itself failed")
            if len(recovered.slides) != len(edited_model.slides):
                problems.append(f"the deck restored in place has {len(recovered.slides)} slides, "
                                f"not the {len(edited_model.slides)} it had")
            if lost:
                problems.append(f"the deck restored in place lost words on {len(lost)} slide(s): "
                                f"{json.dumps(lost, ensure_ascii=False)[:300]}")

        # 4c. recovery is not a dead end: the recovered deck goes back into the normal workflow.
        # Its content is older than the base the forced rebuild left behind, so sync sees the
        # recovered edits as deck edits - and deck edits win, so nothing of them may go again.
        if recovered is not None:
            synced = cli(log, "sync", args.pdf, "--deck", out)
            after = sc.read(pid)
            report = json.loads((out / "sync" / "sync-report.json").read_text(encoding="utf-8"))
            notes["sync_after_recovery"] = {
                "returncode": synced.returncode, "changes": sc.changes(report),
                "conflicts": len(sc.section(report, "conflicts")),
                "slides": len(after.slides),
                "holds_the_edit": "HANDWRITTEN" in " ".join(s.all_text for s in after.slides),
                "lost_words": lost_words(recovered, after),
                "integrity": sc.integrity(after)}
            if synced.returncode:
                problems.append("syncing the recovered deck failed")
            elif not notes["sync_after_recovery"]["holds_the_edit"]:
                problems.append("the sync after the recovery undid the recovered edit")
            elif notes["sync_after_recovery"]["lost_words"]:
                problems.append("the sync after the recovery lost words: "
                                f"{json.dumps(notes['sync_after_recovery']['lost_words'], ensure_ascii=False)[:300]}")

        # 5. --new-deck leaves the old deck alone; a trashed deck is never written to again.
        before, state_file = revision(slides, pid), (out / "emit.json").read_text(encoding="utf-8")
        new = cli(log, "convert", args.pdf, "--out", out, "--new-deck")
        fresh = json.loads((out / "emit.json").read_text(encoding="utf-8"))["presentationId"]
        notes["new_deck"] = {"returncode": new.returncode, "presentationId": fresh,
                             "said": [l for l in new.stdout.splitlines() if "left as it is" in l]}
        if fresh == pid:
            problems.append("--new-deck rebuilt the old deck")
        elif revision(slides, pid) != before:
            problems.append("--new-deck changed the old deck")
        else:
            restored.append(fresh)
        execute(drive.files().update(fileId=fresh, body={"trashed": True}))
        from beamer2slides.guard import previous_deck
        state = previous_deck(drive, out)
        notes["trashed"] = state
        if state["state"] != "trashed":
            problems.append(f"a trashed deck reads back as {state['state']}")

        # tidy up: only the decks this proof created.
        for rid in restored:
            try:
                execute(drive.files().delete(fileId=rid))
            except Exception as e:  # noqa: BLE001
                log.write(f"could not delete {rid}: {e}\n")
        (out / "emit.json").write_text(state_file, encoding="utf-8")  # the folder tracks the proof's deck again

    notes["problems"] = problems
    notes["seconds"] = round(time.time() - started, 1)
    (args.out / "proof.json").write_text(json.dumps(notes, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(notes, indent=1, ensure_ascii=False)[:4000])
    print(f"\n{'PROBLEMS: ' + '; '.join(problems) if problems else 'all checks passed'} "
          f"({notes['seconds']} s, evidence in {args.out})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
