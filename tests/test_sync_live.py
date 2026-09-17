"""Opt-in: sync and pull on real Google Slides decks with simulated human edits (marker `sync`).

  python -m pytest -m sync                          # everything below
  python -m pytest -m sync -k variants              # offline: each source version classifies as intended
  python -m pytest -m sync -k edit_catalogue        # every deck edit kind, verified by read-back
  python -m pytest -m sync -k "scenario and slides" # one scenario

A scenario is `convert v1 -> deck edits (tools/deck_edits.py) -> build vN (tests/decks/sync) ->
sync -> check (tools/sync_check.py)`: deck edits survive exactly once, source changes are applied,
conflicts are reported with both versions, no orphans or duplicates, groups intact, slides nobody
edited match a fresh conversion of vN (elements, thumbnails, alignment), and a second sync writes
nothing (same revision). Scenario folders are out/sync-tests/<scenario> of the main checkout
(convert rebuilds the same deck each run), fresh conversions out/sync-tests/_fresh/<variant>.
Scenarios run PARALLEL at a time. Skipped while `beamer2slides sync` (or `pull`) doesn't exist,
pdflatex isn't found, or the Google token needs a browser consent.
Sync hook used by the concurrency scenario: sync runs the command in B2S_SYNC_BEFORE_WRITE once,
after planning and before its first write.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from test_slides_alignment import MAIN, google_unavailable  # noqa: E402

_spec = importlib.util.spec_from_file_location("sync_build", ROOT / "tests" / "decks" / "sync" / "build.py")
sync_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_build)

pytestmark = pytest.mark.sync

OUT = Path(os.environ.get("B2S_SYNC_TESTS_OUT", MAIN / "out" / "sync-tests"))
PARALLEL = 3
MIKTEX = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
       "PATH": os.pathsep.join([os.environ.get("PATH", "")] + ([str(MIKTEX)] if MIKTEX.exists() else []))}
os.environ["PATH"] = ENV["PATH"]  # (sync_build compiles with the inherited environment)
PDFIUM = threading.Lock()  # PDFium is not thread-safe (alignment measurement)

TITLE, ALGO, MERGING, CONV = "Keeping Slides and Source in Sync", "The sync algorithm", "Merging text", "Convergence"
POLICY, RESULTS, VERSIONS = "Merge policy", "Results", "Three versions"
IDENTITY, CONCL = "Finding the same slide", "Conclusions"
WHY = {"contains": "Later the source changes again"}  # the motivation slide, whatever its title


def E(edit: str, **args) -> dict:
    return {"edit": edit, "args": args}


def pdflatex_missing() -> str | None:
    return None if shutil.which("pdflatex", path=ENV["PATH"]) else "pdflatex not found (MiKTeX)"


def cli_missing(command: str) -> str | None:
    done = subprocess.run([sys.executable, "-m", "beamer2slides", command, "--help"], env=ENV, cwd=ROOT,
                          capture_output=True, text=True)
    return None if done.returncode == 0 else f"`beamer2slides {command}` is not available yet"


def build(variant: str) -> Path:
    return sync_build.build(variant)


# ---------------------------------------------------------------- offline: source versions

@pytest.mark.parametrize("variant", [v for v in sync_build.VARIANTS if v != "v1"])
def test_variants_classify_as_intended(variant):
    """Each source version differs from v1 in classification exactly as its flags intend."""
    if reason := pdflatex_missing():
        pytest.skip(reason)
    folders = {}
    for v in ("v1", variant):
        pdf = build(v)
        folders[v] = sync_build.OUT / "classified" / v
        if not (folders[v] / "deck.json").exists() or (folders[v] / "deck.json").stat().st_mtime < pdf.stat().st_mtime:
            subprocess.run([sys.executable, "-m", "beamer2slides", "classify", str(pdf), "--out", str(folders[v])],
                           env=ENV, cwd=ROOT, check=True, capture_output=True)
    got = set(sync_build.classification_diff(sync_build.summary(folders["v1"]), sync_build.summary(folders[variant])))
    want = sync_build.intended_diff(sync_build.VARIANTS[variant])
    assert got == want, f"unintended: {sorted(got - want)}\nmissing: {sorted(want - got)}"


# ---------------------------------------------------------------- live helpers

class Run:
    """One scenario folder: its deck, the CLI calls (logged) and the problems found."""

    def __init__(self, name: str):
        self.name, self.out = name, OUT / name
        self.out.mkdir(parents=True, exist_ok=True)
        self.log = open(OUT / f"{name}.log", "w", encoding="utf-8")
        self.problems: list[str] = []
        self.deck = None

    def cli(self, *args, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
        self.log.write(f"\n$ beamer2slides {' '.join(map(str, args))}\n")
        self.log.flush()
        done = subprocess.run([sys.executable, "-m", "beamer2slides", *map(str, args)], env={**ENV, **(env or {})},
                              cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        if check and done.returncode:
            raise RuntimeError(f"{self.name}: beamer2slides {args[0]} failed, see {OUT / f'{self.name}.log'}")
        return done

    def convert(self, pdf: Path) -> None:
        from deck_edits import LiveDeck
        self.cli("convert", pdf, "--out", self.out)
        self.deck = LiveDeck(json.loads((self.out / "emit.json").read_text(encoding="utf-8"))["presentationId"])

    def edit(self, *specs: dict) -> list[dict]:
        from deck_edits import verified
        self.deck.read()
        out = []
        for spec in specs:
            exp, bad = verified(self.deck, spec)
            if bad:
                raise RuntimeError(f"{self.name}: the edit itself failed: {bad}")
            out.append(exp)
            self.log.write(f"edit {json.dumps(spec, ensure_ascii=False)}\n")
        return out

    def report(self) -> dict:
        found = sorted((p for p in (self.out / "sync-report.json", self.out / "sync" / "sync-report.json") if p.exists()),
                       key=lambda p: p.stat().st_mtime)
        if not found:
            raise RuntimeError(f"{self.name}: sync wrote no sync-report.json")
        return json.loads(found[-1].read_text(encoding="utf-8"))

    def sync(self, pdf: Path, env: dict | None = None) -> dict:
        self.before = self.deck.read()
        started = time.time()
        self.cli("sync", pdf, "--deck", self.out, env=env)
        report = self.report()
        if max(p.stat().st_mtime for p in self.out.rglob("sync-report.json")) < started - 1:
            raise RuntimeError(f"{self.name}: sync-report.json is stale")
        return report

    def revision(self) -> str:
        from beamer2slides.gslides import execute
        return execute(self.deck.api.presentations().get(presentationId=self.deck.pid, fields="revisionId"))["revisionId"]

    def base_ids(self) -> set[str] | None:
        from sync_check import ids_in
        base = next((p for p in (self.out / "sync" / "base.json", self.out / "base.json") if p.exists()), None)
        return ids_in(json.loads(base.read_text(encoding="utf-8"))) if base else None

    def check(self, variant: str, pdf: Path, report: dict, expectations: list[dict], *, drop: tuple[str, ...] = (),
              checks: list[dict] = (), skip_source: tuple[str, ...] = (), order: list | None = None,
              conflicts: list[list[str]] = (), any_conflicts: bool = False, mentions: tuple[str, ...] = (),
              converged: list[list[str]] = (), no_writes_since: str | None = None, allow_ungrouped: set[str] = frozenset(),
              idempotent: bool = True) -> None:
        """Everything a sync must leave behind. `drop`: edits whose own checks the source legitimately
        changed (the merged outcome is in `checks`); `skip_source`: source checks on texts the deck
        overrides; `order`: the slide titles in order (default: the variant's); `conflicts`: entries
        the report must list (else none, unless `any_conflicts`); `mentions`: words the report must hold;
        `converged`: converged entries the report must list; `no_writes_since`: the revision the sync
        must have left alone (and listed no changes); `allow_ungrouped`: slides whose formula pictures
        the deck ungrouped on purpose."""
        import sync_check as sc
        flags = sync_build.VARIANTS[variant]
        model = self.deck.read()
        kept = [c for e in expectations if e["edit"] not in drop for c in e["checks"]]
        source = [c for c in sync_build.checks(flags) if not any(s in json.dumps(c, ensure_ascii=False) for s in skip_source)]
        order = order or sync_build.titles(flags)
        all_checks = kept + list(checks) + source + [{"check": "slides", "order": order}]
        self.problems += [f"after sync to {variant}: {p}" for p in sc.check_all(model, all_checks)]
        self.problems += sc.check_report(report, conflicts=conflicts, converged=converged,
                                         no_conflicts=not conflicts and not any_conflicts)
        text = json.dumps(report, ensure_ascii=False).lower()
        self.problems += [f"report doesn't mention {m!r}" for m in mentions if m.lower() not in text]
        if no_writes_since is not None:
            if sc.changes(report):
                self.problems.append(f"sync to {variant} lists {sc.changes(report)} changes, expected none")
            if model.revision != no_writes_since:
                self.problems.append(f"sync to {variant} changed the presentation revision")
        self.problems += sc.integrity(model, before=self.before, base_ids=self.base_ids(), allow_ungrouped=allow_ungrouped)

        # Slides nobody edited in the deck: like a fresh conversion of the same source.
        edited = {s.id for e in expectations for sel in e["slides"] for s in model.find(sel)}
        fresh_folder, fresh = fresh_conversion(variant)
        titles = [t for t in sync_build.titles(flags)
                  if len(model.find(t)) == 1 and model.one(t).id not in edited and len(fresh.find(t)) == 1]
        self.problems += sc.compare_fresh(model, fresh, titles)
        for t in titles:
            problem = sc.thumbnail_diff(self.deck.api, (self.deck.pid, model.one(t).id), (fresh.pres["presentationId"], fresh.one(t).id),
                                        self.out / "check" / f"thumb-{sync_build.titles(flags).index(t) + 1:02}.png")
            if problem:
                self.problems.append(f"fresh {t}: {problem}")
        with PDFIUM:
            self.problems += sc.alignment_compare(fresh_folder, model, fresh, self.deck.pid, titles, self.out / "check")

        if idempotent:
            revision = self.revision()
            again = self.sync(pdf)
            if sc.changes(again):
                self.problems.append(f"second sync to {variant} still lists {sc.changes(again)} changes")
            if self.revision() != revision:
                self.problems.append(f"second sync to {variant} changed the presentation revision")


_fresh_locks: dict[str, threading.Lock] = {}
_fresh_guard = threading.Lock()


def fresh_conversion(variant: str):
    """(folder, model) of a fresh conversion of a source version, converted once per session."""
    import sync_check as sc
    with _fresh_guard:
        lock = _fresh_locks.setdefault(variant, threading.Lock())
    with lock:
        folder = OUT / "_fresh" / variant
        pdf = build(variant)
        done = folder / ".converted"
        if not done.exists() or done.read_text(encoding="utf-8") != str(pdf.stat().st_mtime):
            folder.mkdir(parents=True, exist_ok=True)
            with open(OUT / "_fresh" / f"{variant}.log", "w", encoding="utf-8") as log:
                subprocess.run([sys.executable, "-m", "beamer2slides", "convert", str(pdf), "--out", str(folder)],
                               env=ENV, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
            done.write_text(str(pdf.stat().st_mtime), encoding="utf-8")
        pid = json.loads((folder / "emit.json").read_text(encoding="utf-8"))["presentationId"]
        return folder, sc.read(pid)


# ---------------------------------------------------------------- scenarios

SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__.removeprefix("scenario_").replace("_", "-")] = fn
    return fn


@scenario
def scenario_untouched(run: Run):
    """No deck edits: sync to the long chain of source changes equals a fresh conversion."""
    run.convert(build("v1"))
    pdf = build("chain")
    run.check("chain", pdf, run.sync(pdf), [])


@scenario
def scenario_disjoint(run: Run):
    """Source and deck change different elements (some on the same slides)."""
    run.convert(build("v1"))
    exps = run.edit(
        E("replace_word", slide=WHY, text="People polish the converted deck by hand", old="polish", new="refine"),
        E("move", slide=CONV, target={"text": "Conflicts disappear once"}, dx=0, dy=40),
        E("bold", slide=CONCL, word="survive", context="Deck edits survive every sync"),
        E("add_text_box", slide=RESULTS, text="Measured on the test decks", box=[460, 60, 220, 30]),
        E("add_shape", slide=VERSIONS, shape_type="STAR_5", box=[620, 300, 50, 50], color="#ffc000"),
        E("set_background", slide=IDENTITY, color="#fff2cc"),
        E("set_notes", slide=ALGO, text="Walk through the steps slowly."))
    pdf = build("disjoint")
    run.check("disjoint", pdf, run.sync(pdf), exps)


@scenario
def scenario_same_element(run: Run):
    """Source and deck change different fields of the same elements: text vs geometry, style,
    picture size, another paragraph, another table cell."""
    run.convert(build("v1"))
    exps = run.edit(
        E("move", slide=WHY, target={"text": "People polish the converted deck by hand"}, dx=0, dy=20),
        E("resize_font", slide=POLICY, text="Deck edits win", size=16),
        E("resize", slide=CONV, target={"image": "largest"}, sx=0.8),
        E("append_sentence", slide=MERGING, text="writes the merged paragraph back into the deck.", sentence="Nothing is lost."),
        E("bold", slide=RESULTS, word="Disjoint"))
    resized = next(e for e in exps if e["edit"] == "resize")["checks"][0]
    pdf = build("same-element")
    run.check("same-element", pdf, run.sync(pdf), exps, drop=("resize_font",), checks=[
        {"check": "style", "slide": POLICY, "text": "Deck edits come first", "size": 16},
        {"check": "image", "slide": CONV, "near": [resized["origin"][0] + resized["size"][0] / 2,
                                                   resized["origin"][1] + resized["size"][1] / 2], "colour": "#cc0000"},
        {"check": "image", "slide": MERGING, "count": 2}])


@scenario
def scenario_diff3(run: Run):
    """Both sides change words of one bullet without overlapping: a clean word-level merge."""
    run.convert(build("v1"))
    exps = run.edit(E("replace_word", slide=WHY, text="by an author and converted once", old="converted", new="exported"))
    pdf = build("reword")
    run.check("reword", pdf, run.sync(pdf), exps, drop=("replace_word",),
              skip_source=("by an AI assistant and converted once",),
              checks=[{"check": "text", "slide": WHY, "text": "by an AI assistant and exported once", "count": 1}])


@scenario
def scenario_conflict(run: Run):
    """Both sides rewrite the same words: the deck wins, the report has both versions."""
    run.convert(build("v1"))
    exps = run.edit(E("replace_word", slide=WHY, text="by an author and converted once", old="author", new="editor"),
                    E("replace_word", slide=POLICY, text="is reported as a conflict", old="reported", new="flagged"))
    pdf = build("conflict")
    run.check("conflict", pdf, run.sync(pdf), exps,
              skip_source=("by an AI assistant and converted once", "by an author and converted once",
                           "kept aside and listed as a conflict"),
              conflicts=[["editor", "AI assistant"], ["flagged", "kept aside"]])


@scenario
def scenario_deletions(run: Run):
    """Deleted on one side, edited on the other: a frame the deck edited, a table the deck
    deleted, a bullet the deck reworded."""
    run.convert(build("v1"))
    fresh = fresh_conversion("v1")[1]
    from deck_edits import donor_image_url
    exps = run.edit(
        E("add_image", slide=VERSIONS, url=donor_image_url(run.deck.api, fresh.pres["presentationId"]), box=[540, 250, 150, 100]),
        E("delete_element", slide=RESULTS, target={"text": "Same element"}),
        E("replace_word", slide=WHY, text="moving pictures around", old="pictures", new="figures"))
    pdf = build("deletions")
    order = sync_build.titles(["tablecell"])  # the deleted frame stays, edited in the deck
    run.check("deletions", pdf, run.sync(pdf), exps, order=order,
              skip_source=("Three versions", '"Merged"', "4.7 s", "moving pictures around"),
              checks=[{"check": "text", "slide": RESULTS, "text": "4.7 s", "count": 0},
                      {"check": "slide_count", "slide": VERSIONS, "count": 1}],
              any_conflicts=True, mentions=("versions", "results", "moving figures around"))


@scenario
def scenario_slides(run: Run):
    """Slides added, deleted, duplicated and moved in the deck while the source adds, swaps and
    renames frames."""
    run.convert(build("v1"))
    exps = run.edit(
        E("add_slide", after=CONV, title="Reviewer questions", body="What happens to comments?"),
        E("delete_slide", slide=IDENTITY),
        E("move_slide", slide=CONCL, after=ALGO),
        E("duplicate_slide", slide=MERGING, new_title="Merging text (copy)"))
    pdf = build("slides")
    # The deck reordered slides, so its order stays (the source's swap of Results and Merge policy
    # isn't applied); the new frame follows its source predecessor, Merge policy.
    order = [TITLE, "Why decks and sources diverge", ALGO, "Takeaways", MERGING, "Merging text (copy)", CONV,
             "Reviewer questions", POLICY, "Pulling edits back", RESULTS, VERSIONS]
    run.check("slides", pdf, run.sync(pdf), exps, drop=("move_slide",), order=order,
              checks=[{"check": "slide_count", "slide": IDENTITY, "count": 0}])


@scenario
def scenario_reorder_both(run: Run):
    """The deck moved a slide the source reorders too: the deck's order stays."""
    run.convert(build("v1"))
    exps = run.edit(E("move_slide", slide=POLICY, after=VERSIONS))
    pdf = build("reorder")
    run.check("reorder", pdf, run.sync(pdf), exps,
              order=[TITLE, "Why decks and sources diverge", ALGO, MERGING, CONV, RESULTS, VERSIONS, POLICY, IDENTITY, CONCL])


@scenario
def scenario_chain(run: Run):
    """Two syncs with more deck edits in between, including edits of synced content."""
    run.convert(build("v1"))
    first = run.edit(
        E("replace_word", slide=WHY, text="People polish the converted deck by hand", old="polish", new="refine"),
        E("move", slide=CONV, target={"text": "Conflicts disappear once"}, dx=0, dy=40),
        E("add_slide", after=CONCL, title="Backup: timings", body="Sync takes seconds."))
    pdf = build("mixed")
    run.check("mixed", pdf, run.sync(pdf), first, order=sync_build.titles(sync_build.MIXED) + ["Backup: timings"],
              idempotent=False)
    second = run.edit(
        E("replace_word", slide="Pulling edits back", text="patched into the source", old="patched", new="written"),
        E("bold", slide=WHY, word="AI", context="by an AI assistant"),
        E("set_notes", slide=MERGING, text="Mention diff3."),
        E("delete_slide", slide="Backup: timings"))
    pdf = build("chain")
    run.check("chain", pdf, run.sync(pdf), [e for e in first if e["edit"] != "add_slide"] + second,
              skip_source=("Plain wording edits are patched into the source",))


@scenario
def scenario_concurrent(run: Run):
    """A person edits the deck while sync plans: sync re-plans instead of overwriting."""
    run.convert(build("v1"))
    exps = run.edit(E("replace_word", slide=WHY, text="People polish the converted deck by hand", old="polish", new="refine"))
    specs = [E("add_text_box", slide=RESULTS, text="Added during the sync", box=[460, 60, 220, 30]),
             E("replace_word", slide=CONCL, text="Deck edits survive every sync", old="survive", new="outlive")]
    spec_file, exp_file = run.out / "hook-edits.json", run.out / "hook-expectations.json"
    spec_file.write_text(json.dumps(specs), encoding="utf-8")
    exp_file.unlink(missing_ok=True)
    hook = subprocess.list2cmdline([sys.executable, str(ROOT / "tools" / "deck_edits.py"), run.deck.pid, "apply",
                                    f"@{spec_file}", "--out", str(exp_file)])
    pdf = build("tablecell")
    report = run.sync(pdf, env={"B2S_SYNC_BEFORE_WRITE": hook})
    if not exp_file.exists():
        pytest.skip("sync doesn't run the B2S_SYNC_BEFORE_WRITE hook")
    run.check("tablecell", pdf, report, exps + json.loads(exp_file.read_text(encoding="utf-8")))


@scenario
def scenario_converged(run: Run):
    """The source now says what the deck says (as after a pull into the .tex): the deck edits
    converge, the report says so, and sync writes nothing (a bullet and a table cell)."""
    run.convert(build("v1"))
    exps = run.edit(E("replace_word", slide=WHY, text="by an author and converted once", old="author", new="AI assistant"),
                    E("replace_word", slide=RESULTS, text="3.9 s", old="3.9", new="4.7"))
    pdf = build("converged")
    revision = run.revision()
    run.check("converged", pdf, run.sync(pdf), exps, converged=[["AI assistant"], ["4.7"]], no_writes_since=revision)


@scenario
def scenario_many_edits(run: Run):
    """One slide edited every way in the deck (words, word styles, a move, added objects, notes,
    background) while the source retitles it, rewords, adds and removes bullets and changes its notes."""
    run.convert(build("v1"))
    exps = run.edit(
        E("replace_word", slide=WHY, text="People polish the converted deck by hand", old="polish", new="refine"),
        E("bold", slide=WHY, word="typos", context="fixing typos and wording"),
        E("recolour", slide=WHY, word="Later", context="Later the source changes again", color="#c00000"),
        E("move", slide=WHY, target={"text": "Later the source changes again"}, dx=0, dy=15),
        E("add_text_box", slide=WHY, text="Ask the audience first", box=[470, 300, 220, 30]),
        E("add_shape", slide=WHY, shape_type="STAR_5", box=[640, 60, 40, 40], color="#ffc000"),
        E("set_background", slide=WHY, color="#eef5ff"),
        E("set_notes", slide=WHY, text="Keep this slide short."))
    pdf = build("many-edits")
    run.check("many-edits", pdf, run.sync(pdf), exps, skip_source=("show of hands",),
              conflicts=[["Keep this slide short", "show of hands"]])


@scenario
def scenario_groups(run: Run):
    """Groups in the deck around changed elements: a user group holding a figure the source
    redraws, a converter group taken apart (steps the source renumbers), and a user group deleted
    with the block the source edits."""
    run.convert(build("v1"))
    exps = run.edit(
        E("group", slide=CONV, targets=[{"image": "largest"}, {"text": "Conflicts disappear once"}]),
        E("ungroup", slide=ALGO, target={"text": "Read the base snapshot"}),
        E("group", slide=POLICY, targets=[{"text": "Both versions go into the report."}, {"text": "Deck edits win"}]),
        E("delete_group", slide=POLICY, target={"text": "Deck edits win"}))
    pdf = build("groups")
    run.check("groups", pdf, run.sync(pdf), exps, drop=("group", "ungroup"),
              skip_source=("Deck edits come first", "kept aside and listed"),
              checks=[{"check": "grouped", "slide": CONV, "members": [{"image": "largest"}, {"text": "Conflicts disappear once"}],
                       "grouped": True},
                      {"check": "grouped", "slide": ALGO, "members": [{"text": "Read the base snapshot"}, {"image_near": [35.0, 147.7]}],
                       "grouped": False},
                      {"check": "text", "slide": POLICY, "text": "Deck edits come first", "count": 0}],
              any_conflicts=True, mentions=("policy",), allow_ungrouped={ALGO})


def repaint_pictures(out: Path) -> int:
    """Put the deck's anchored pictures back the way the converter wrote them before they got a
    transparent ground (the page colour painted in), base hashes included, so the next conversion's
    files differ from the base exactly as they do on the first sync of a deck converted then."""
    import numpy as np
    from PIL import Image

    from beamer2slides import identity, snapshot
    from beamer2slides.google_auth import drive_service
    base, _ = snapshot.load_base(json.loads((out / "emit.json").read_text(encoding="utf-8"))["presentationId"],
                                 out, drive_service())
    page_key = snapshot.page_keys({"slides": [{"page": s["page"]} for s in base["slides"]]},
                                  [s["key"] for s in base["slides"]])
    painted = 0
    for s in base["slides"]:
        for el in s["elements"]:
            file = el["ir"].get("file")
            if el["kind"] != "image" or not file or not (out / file).exists():
                continue
            with Image.open(out / file) as img:
                if img.mode != "RGBA":
                    continue
                rgba = np.asarray(img).astype(float)
            if (rgba[..., 3] == 0).sum() < 20:
                continue
            alpha = rgba[..., 3:] / 255
            flat = rgba[..., :3] * alpha + 255 * (1 - alpha)  # the pages are white under these
            Image.fromarray(flat.round().astype("uint8"), "RGB").save(out / file)
            h, fields = identity.ir_fields(el["ir"], out, el.get("anchor"), page_key)
            assert {k: v for k, v in fields.items() if k != "image"} == {k: v for k, v in el["fields"].items() if k != "image"}
            el["ir_hash"], el["fields"] = h, fields
            painted += 1
    snapshot.save_local(base, out)
    snapshot.save_drive(drive_service(), base)
    return painted


@scenario
def scenario_repainted_pictures(run: Run):
    """A converter change that rewrites the picture files without changing what they show (the
    transparent ground anchored pictures got): the base takes the new hashes, the deck is left
    alone instead of every formula and ball being rewritten."""
    run.convert(build("v1"))
    painted = repaint_pictures(run.out)
    if painted < 3:
        run.problems.append(f"only {painted} anchored picture(s) to repaint: the scenario tests nothing")
    pdf = build("v1")
    revision = run.revision()
    run.check("v1", pdf, run.sync(pdf), [], no_writes_since=revision,
              converged=[["image", "the same picture"]])


XFAIL = {}  # scenario -> why it can't pass yet (docs/sync.md, Not supported yet)


@scenario
def scenario_table_words(run: Run):
    """Different cells of one table edited in the deck and in the source."""
    run.convert(build("v1"))
    exps = run.edit(E("replace_word", slide=RESULTS, text="5.1 s", old="5.1", new="5.2"))
    pdf = build("tablecell")
    run.check("tablecell", pdf, run.sync(pdf), exps)


XFAIL["table-words"] = "no word merge inside tables: a text edit on both sides keeps the deck's table (a conflict)"


@scenario
def scenario_nested_group(run: Run):
    """A user group around a block (a group in a group) whose title the source changes."""
    run.convert(build("v1"))
    exps = run.edit(E("group", slide=POLICY, targets=[{"text": "Both versions go into the report."}, {"text": "Deck edits win"}]))
    pdf = build("blockedit")
    report = run.sync(pdf)
    run.check("blockedit", pdf, report, exps, drop=("group",), checks=[
        {"check": "grouped", "slide": POLICY, "grouped": True,
         "members": [{"text": "Both versions go into the report."}, {"text": "Deck edits come first"}]}])
    run.problems += [f"report warns: {w}" for w in report.get("warnings", []) if "group" in w]


@scenario
def scenario_pull_wording(run: Run):
    """Deck wording edits pulled into the .tex, rebuilt and synced: the deck stays as it is and
    the report calls the overrides converged."""
    if reason := cli_missing("pull"):
        pytest.skip(reason)
    src = run.out / "src"
    src.mkdir(exist_ok=True)
    tex = src / "talk.tex"
    tex.write_text(sync_build.render([]), encoding="utf-8")
    run.convert(sync_build.compile_tex(tex))
    exps = run.edit(
        E("replace_word", slide=WHY, text="fixing typos and wording", old="typos", new="mistakes"),
        E("replace_word", slide=POLICY, text="Both versions go into the report.", old="report", new="sync report"),
        E("move", slide=CONV, target={"text": "Conflicts disappear once"}, dx=0, dy=30))
    run.cli("pull", "--deck", run.out, "--tex", tex, "--apply")
    source = tex.read_text(encoding="utf-8")
    run.problems += [f"pull didn't write {w!r} into the source" for w in ("fixing mistakes and wording", "the sync report")
                     if w not in source]
    pdf = sync_build.compile_tex(tex)
    revision = run.revision()
    report = run.sync(pdf)
    import sync_check as sc
    run.problems += sc.check_all(run.deck.read(), [c for e in exps for c in e["checks"]])
    run.problems += sc.check_report(report, converged=[["mistakes"], ["sync report"]], no_conflicts=True)
    if sc.changes(report):
        run.problems.append(f"sync after pull lists {sc.changes(report)} changes")
    if run.revision() != revision:
        run.problems.append("sync after pull changed the presentation revision")


@scenario
def scenario_pull_picture(run: Run):
    """A picture the person added to the deck, pulled into the .tex: the next sync keeps their
    object (the source draws that picture now) instead of putting a second one next to it."""
    if reason := cli_missing("pull"):
        pytest.skip(reason)
    import sync_check as sc
    from deck_edits import donor_image_url
    src = run.out / "src"
    src.mkdir(exist_ok=True)
    tex = src / "talk.tex"
    tex.write_text(sync_build.render([]), encoding="utf-8")
    run.convert(sync_build.compile_tex(tex))
    donor = donor_image_url(run.deck.api, fresh_conversion("v1")[1].pres["presentationId"])
    pictures_of = lambda model: {e.id for e in model.one(CONCL).elements if e.kind == "image"}
    before = pictures_of(run.deck.read())
    exps = run.edit(E("add_image", slide=CONCL, url=donor, box=[500, 270, 160, 100]))
    after = pictures_of(run.deck.read())
    added = after - before
    run.cli("pull", "--deck", run.out, "--tex", tex, "--apply")
    if "includegraphics" not in tex.read_text(encoding="utf-8"):
        run.problems.append("pull didn't put the picture into the source")
        return
    report = run.sync(sync_build.compile_tex(tex))
    model = run.deck.read()
    run.problems += sc.check_all(model, [c for e in exps for c in e["checks"]])
    run.problems += sc.check_report(report, converged=[["image"]], no_conflicts=True)
    run.problems += sc.integrity(model, before=run.before, base_ids=run.base_ids())
    pictures = pictures_of(model)
    if len(pictures) != len(after):
        run.problems.append(f"{len(after)} picture(s) on {CONCL} before the sync, {len(pictures)} after: "
                            "the pulled picture was duplicated or deleted")
    if not added <= pictures:
        run.problems.append("sync replaced the picture the person added instead of adopting their object")
    elif run.base_ids() is not None and not added <= run.base_ids():
        run.problems.append("the new base doesn't own the adopted picture")


@pytest.fixture(scope="module")
def outcomes(request):
    """scenario -> problems (or the exception, or a skip reason) for the scenarios selected."""
    names = [n for n in SCENARIOS if any(getattr(i, "callspec", None) and i.callspec.params.get("name") == n
                                         for i in request.session.items)]
    for reason in (cli_missing("sync"), pdflatex_missing(), google_unavailable()):
        if reason:
            pytest.skip(reason)
    OUT.mkdir(parents=True, exist_ok=True)

    def run(name):
        r = Run(name)
        try:
            SCENARIOS[name](r)
            return r.problems
        except pytest.skip.Exception as e:
            return e
        except Exception as e:  # reported by that scenario's test
            return e
        finally:
            r.log.close()
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        return dict(zip(names, pool.map(run, names)))


@pytest.mark.parametrize("name", [pytest.param(n, marks=pytest.mark.xfail(reason=XFAIL[n], strict=True)) if n in XFAIL else n
                                  for n in SCENARIOS])
def test_scenario(name, outcomes):
    result = outcomes[name]
    if isinstance(result, pytest.skip.Exception):
        pytest.skip(str(result))
    if isinstance(result, Exception):
        raise result
    if result:
        pytest.fail(f"{name} ({OUT / name}):\n  " + "\n  ".join(result), pytrace=False)


def test_checker_controls():
    """The checker on known answers: two conversions of v1 compare clean (elements, thumbnails,
    alignment); broken decks are caught (moved text, a moved formula picture, a duplicate, an
    ungrouped formula, a group taken apart, a report without the conflict)."""
    for reason in (pdflatex_missing(), google_unavailable()):
        if reason:
            pytest.skip(reason)
    import sync_check as sc
    from sync_check import EMU_PER_PT
    run = Run("checker-controls")
    try:
        run.convert(build("v1"))
        fresh_folder, fresh = fresh_conversion("v1")
        titles = sync_build.titles([])
        model = run.deck.read()
        problems = [f"clean: {p}" for p in sc.compare_fresh(model, fresh, titles) + sc.integrity(model)]
        for t in titles:
            if p := sc.thumbnail_diff(run.deck.api, (run.deck.pid, model.one(t).id), (fresh.pres["presentationId"], fresh.one(t).id),
                                      run.out / "check" / f"clean-{titles.index(t) + 1:02}.png"):
                problems.append(f"clean: {t}: {p}")
        with PDFIUM:
            problems += [f"clean: {p}" for p in sc.alignment_compare(fresh_folder, model, fresh, run.deck.pid, titles, run.out / "check")]

        merging = model.one(MERGING)
        formula = next(e for e in merging.elements if e.kind == "image" and sc.is_formula_picture(e))
        text = next(e for e in model.one(CONCL).elements if "Deck edits survive" in e.text)
        diagram = next(e for e in model.one(VERSIONS).elements if e.kind == "group")
        run.deck.batch([
            {"updatePageElementTransform": {"objectId": formula.id, "applyMode": "RELATIVE", "transform": {
                "scaleX": 1, "scaleY": 1, "translateX": 4 * EMU_PER_PT, "translateY": 0, "unit": "EMU"}}},
            {"updatePageElementTransform": {"objectId": text.id, "applyMode": "RELATIVE", "transform": {
                "scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 30 * EMU_PER_PT, "unit": "EMU"}}},
            {"duplicateObject": {"objectId": model.one(RESULTS).elements[0].top}},
            {"ungroupObjects": {"objectIds": [diagram.id]}}])
        broken = run.deck.read()
        caught = {
            "moved text": sc.compare_fresh(broken, fresh, [CONCL]),
            "moved text thumbnail": [sc.thumbnail_diff(run.deck.api, (run.deck.pid, broken.one(CONCL).id),
                                                       (fresh.pres["presentationId"], fresh.one(CONCL).id),
                                                       run.out / "check" / "broken-concl.png")],
            "duplicate": [p for p in sc.integrity(broken) if "duplicate" in p],
            "group taken apart": [p for p in sc.integrity(broken, before=model) if "came apart" in p],
            "report without the conflict": sc.check_report({"conflicts": [{"ours": "a", "theirs": "b"}]},
                                                           conflicts=[["editor", "AI assistant"]]),
        }
        with PDFIUM:
            caught["moved formula picture"] = sc.alignment_compare(fresh_folder, broken, fresh, run.deck.pid, [MERGING],
                                                                   run.out / "check")
        run.deck.batch([{"ungroupObjects": {"objectIds": [formula.parent]}}])
        caught["ungrouped formula"] = [p for p in sc.integrity(run.deck.read()) if "not grouped" in p]
        problems += [f"not caught: {k}" for k, found in caught.items() if not any(found)]
    finally:
        run.log.close()
    assert not problems, "\n".join(problems)


def test_edit_catalogue():
    """Every edit kind of tools/deck_edits.py on a converted v1 deck does what its expectation
    claims (checks fail before, hold after, all still hold at the end), and leaves no duplicates."""
    for reason in (pdflatex_missing(), google_unavailable()):
        if reason:
            pytest.skip(reason)
    import sync_check as sc
    from deck_edits import catalogue, donor_image_url
    run = Run("edit-catalogue")
    try:
        run.convert(build("v1"))
        donor = donor_image_url(run.deck.api, fresh_conversion("v1")[1].pres["presentationId"])
        problems, expectations = [], []
        from deck_edits import verified
        for spec in catalogue(donor):
            exp, bad = verified(run.deck, spec)
            problems += bad
            expectations.append(exp)
        model = run.deck.read()
        problems += [f"at the end: {p}" for p in sc.check_all(model, [c for e in expectations for c in e["checks"]])]
        problems += sc.integrity(model, allow_ungrouped={e["args"]["slide"] for e in expectations if e["edit"] == "ungroup"})
        from deck_edits import EDITS
        problems += [f"edit kind not in the catalogue: {k}" for k in set(EDITS) - {e["edit"] for e in expectations}]
    finally:
        run.log.close()
    assert not problems, "\n".join(problems)
