"""Opt-in: sync and pull on the 48-frame stress deck (marker `sync`, deselected by default).

  python -m pytest -m sync tests/test_stress_live.py
  python -m pytest -m sync tests/test_stress_live.py -k "variants or selectors or budget"   # offline
  python -m pytest -m sync tests/test_stress_live.py -k "scenario and ambiguous"

`tests/decks/stress/talk.tex` is built to be ambiguous: three frames titled Results, two frames
with no title at all, twins one word apart, one paragraph on three slides, the same picture twice
on one slide and again on another, a table whose every row says the same, two frames with
identical notes. Nothing in here may be identified by its title, so every selector below finds a
slide by a phrase that appears on exactly one of them (`test_selectors_are_unique` proves it
offline), and the order after a sync is checked against the labels of the variant, not its titles.

A scenario is `convert v1 -> deck edits (tools/deck_edits.py) -> build a variant
(tests/decks/stress/build.py) -> sync -> check (tools/sync_check.py)`. What it must show is always
the same: every deck edit survived exactly once and on the slide it was made on, every source
change landed, the slide order follows the source, no orphans or duplicates, untouched slides
still equal a fresh conversion, and a second sync writes nothing.

Folders: out/stress-tests/<scenario> of the main checkout (the same decks are rebuilt every run),
fresh conversions out/stress-tests/_fresh/<variant>. Timings land in out/stress-tests/perf.json.
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

_spec = importlib.util.spec_from_file_location("stress_build", ROOT / "tests" / "decks" / "stress" / "build.py")
stress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stress)

pytestmark = pytest.mark.sync

OUT = Path(os.environ.get("B2S_STRESS_TESTS_OUT", MAIN / "out" / "stress-tests"))
PARALLEL = 2  # a 48 frame conversion is heavy; two at a time keeps Drive and the CPU sane
MIKTEX = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
       "PATH": os.pathsep.join([os.environ.get("PATH", "")] + ([str(MIKTEX)] if MIKTEX.exists() else []))}
os.environ["PATH"] = ENV["PATH"]  # (stress.build compiles with the inherited environment)
TIMINGS: dict[str, dict] = {}
TIMINGS_LOCK = threading.Lock()


# ---------------------------------------------------------------- finding slides that look alike

# frame label (stress.FRAMES) -> a phrase that appears on that slide and on no other. Three
# frames are left out because nothing on them is unique: the two without any text (fullpage,
# onlypic) and the diagram, whose only words are Base, Source and Deck. The frames around them
# pin their place in the order.
SEL = {
    "title": "Laboratory of Hard Cases",
    "agenda": "Tables nobody wants to merge",
    "twin-a": "Only one word below is different",
    "twin-b": "Only one word above is different",
    "between": "A frame added exactly where identity is hardest",
    "echo-one": "Context for the first echo",
    "echo-two": "Context for the second echo",
    "echo-three": "Context for the third echo",
    "astral": "count as two code units each",
    "scripts": "A soft hyphen hides inside",
    "longline": "four hundred characters long",
    "gaps": "A list with an empty item follows",
    "bigtable": "Twenty rows, one verdict",
    "widetable": "C9",
    "merged": "Planning",
    "columns": "The left column argues",
    "blockcol": "A block that lives in a column",
    "boxes": "Box seven",
    "formulas": "three pictures have to share the gaps",
    "steps": "Convert the source",
    "alternatives": "the one that survives",
    "figcaption": "A caption that names the picture",
    "twinpics": "The same file twice",
    "picagain": "And once more, on another slide",
    "results-a": "Disjoint",
    "results-b": "Every deck edit survived",
    "#27": "no label at all",
    "notes-a": "A slide whose speaker notes are shared",
    "notes-b": "word for word the same",
    "displaymath": "The equation stays a picture",
    "code": "return diff3",
    "links": "the sync documentation",
    "numbers": "Read the base snapshot",
    "description": "what the converter produced last time",
    "quote": "The sentence above is quoted twice",
    "emphasis": "Coloured",
    "deep": "Fourth level",
    "mobile": "The label on this frame moves",
    "arriving": "This frame is where that label arrives",
    "vanishing": "This frame loses its label",
    "footnotes": "A claim that needs a footnote",
    "repeatcells": "Geometry",
    "whitespace": "kept as a witness",
    "quote-again": "The same sentence as before",
    "summary": "Labels decide identity",
}
# `backup` is the frame whose every bullet is rewritten, so it has no phrase that survives the
# rewrite: it is selected by whichever version the variant carries.
BACKUP_V1 = "Timings are measured on the stress deck itself"
BACKUP_V2 = "Every timing below comes from this deck and no other"


def S(name: str) -> dict:
    """The selector for a frame, by its v1 label."""
    return {"contains": SEL[name]}


def backup(flags: list[str]) -> dict:
    return {"contains": BACKUP_V2 if "rewritebullets" in flags else BACKUP_V1}


def order_check(flags: list[str], gone: tuple[str, ...] = ()) -> dict:
    """The slides of this variant in source order, named by phrases, not by titles. `gone`: frames
    the scenario deleted in the deck - the source still has them and deck edits win, so they are
    not there to be in any order."""
    return {"check": "slides", "order": [S(n) for n in stress.names(flags) if n in SEL and n not in gone]}


# Unique titles of frames no scenario edits: what an untouched slide is compared with.
FRESH_TITLES = ["Agenda", "One very long line", "Description list", "Footnotes and small print"]


def fresh_titles(flags: list[str]) -> list[str]:
    return [f"{t} v2" if "retitleall" in flags else t for t in FRESH_TITLES]


def E(edit: str, **args) -> dict:
    return {"edit": edit, "args": args}


def display_maths_noise(problem: str) -> bool:
    """A known false positive of `sync_check.integrity` (see the xfail test below): a display
    equation is an `image/math` element with no anchor and no text to be grouped with, and the
    checker asks for the grouping anyway. No deck with display maths can pass without this."""
    return "formula picture" in problem and "not grouped" in problem and "Display mathematics" in problem


# ---------------------------------------------------------------- offline

def latex_missing() -> str | None:
    return None if shutil.which(stress.ENGINE, path=ENV["PATH"]) else f"{stress.ENGINE} not found (MiKTeX)"


def cli_missing(command: str) -> str | None:
    done = subprocess.run([sys.executable, "-m", "beamer2slides", command, "--help"], env=ENV, cwd=ROOT,
                          capture_output=True, text=True)
    return None if done.returncode == 0 else f"`beamer2slides {command}` is not available yet"


def classified(variant: str) -> Path:
    """tests/decks/stress/out/classified/<variant>, classified again when the PDF is newer."""
    pdf = stress.build(variant)
    folder = stress.OUT / "classified" / variant
    deck = folder / "deck.json"
    if not deck.exists() or deck.stat().st_mtime < pdf.stat().st_mtime:
        subprocess.run([sys.executable, "-m", "beamer2slides", "classify", str(pdf), "--out", str(folder)],
                       env=ENV, cwd=ROOT, check=True, capture_output=True)
    return folder


@pytest.mark.parametrize("variant", [v for v in stress.VARIANTS if v != "v1" and v not in stress.DIFF_EXEMPT])
def test_stress_variants_classify_as_intended(variant):
    """Each source version differs from v1 in classification exactly as its flags intend - with
    three frames called Results and two with no title, the slides are paired by content, so an
    unintended difference here means the deck moved text between slides that look alike."""
    if reason := latex_missing():
        pytest.skip(reason)
    base, other = classified("v1"), classified(variant)
    got = set(stress.classification_diff(stress.summary(base), stress.summary(other),
                                         stress.names([]), stress.names(stress.VARIANTS[variant])))
    want = stress.intended_diff(stress.VARIANTS[variant])
    assert got == want, f"unintended: {sorted(got - want)}\nmissing: {sorted(want - got)}"


@pytest.mark.parametrize("variant", sorted(stress.DIFF_EXEMPT))
def test_exempt_variants_still_build_and_classify(variant):
    """4:3 is a page size, not an edit: every paragraph rewraps, so there is no diff to pin down.
    It still has to compile and classify into the same frames."""
    if reason := latex_missing():
        pytest.skip(reason)
    slides = stress.summary(classified(variant))
    assert len(slides) == len(stress.names(stress.VARIANTS[variant]))


def test_selectors_are_unique():
    """Every phrase this file finds a slide by is on exactly one slide of v1 (and of the variants
    that add or change text). Without this the live scenarios would edit the wrong slide."""
    if reason := latex_missing():
        pytest.skip(reason)
    problems = []
    for variant in ("v1", "insertframe", "rewritebullets", "everyrow"):
        flags = stress.VARIANTS[variant]
        slides = stress.summary(classified(variant))
        wanted = {n: p for n, p in SEL.items() if n in stress.names(flags)}
        wanted["backup"] = backup(flags)["contains"]
        for name, phrase in wanted.items():
            hits = [i for i, s in enumerate(slides)
                    if phrase.lower() in " ".join([s["title"] or ""] + s["texts"]).lower()]
            if len(hits) != 1:
                problems.append(f"{variant}: {name!r} phrase {phrase!r} is on {len(hits)} slides {hits}")
    assert not problems, "\n".join(problems)


def test_request_budget():
    """What a 48 frame deck costs: elements, planned API requests and the scratch slides the
    picture measurement needs. A data point, with bounds wide enough to only catch a blow-up."""
    if reason := latex_missing():
        pytest.skip(reason)
    from beamer2slides.emit import plan_offline
    deck = json.loads((classified("v1") / "deck.json").read_text(encoding="utf-8"))
    started = time.time()
    plan = plan_offline(deck)
    planned = time.time() - started
    requests = sum(len(part) for _, _, parts, _ in plan["slides"] for part in parts)
    elements = sum(len(s["elements"]) for s in deck["slides"])
    stats = {"slides": len(deck["slides"]), "elements": elements, "phase1_requests": len(plan["copies"]),
             "content_requests": requests, "measure_requests": len(plan["measure"]),
             "measure_slides": sum("createSlide" in r for r in plan["measure"]),
             "batches": -(-requests // 400), "plan_seconds": round(planned, 2)}
    with TIMINGS_LOCK:
        TIMINGS["plan"] = stats
        save_timings()
    print("\nstress deck request budget: " + json.dumps(stats))
    assert stats["slides"] == len(stress.names([]))
    assert requests < 40 * len(deck["slides"]), f"{requests} content requests for {len(deck['slides'])} slides"


@pytest.mark.xfail(strict=True, reason="sync_check.is_formula_picture also matches a display equation, "
                                       "which has no anchor and no text to be grouped with")
def test_integrity_does_not_ask_display_maths_to_be_grouped():
    """A display equation becomes a picture of its own (`classify`: role math, anchor None), tagged
    `.../image/math/N` like an inline formula. `sync_check.integrity` asks every such picture to be
    grouped with its text, so every deck holding display maths reports a problem that is not one.
    A fix could ask only for pictures that lie inside a text box's line (an inline formula does,
    a display equation does not), or emit could tag the two apart."""
    import sync_check as sc
    model = sc.Model({"slides": [{"objectId": "s1", "pageElements": [
        {"objectId": "b2s_s034_f0", "title": "b2s:displaymath/image/math/0",
         "size": {"width": {"magnitude": 1900000, "unit": "EMU"}, "height": {"magnitude": 410000, "unit": "EMU"}},
         "transform": {"scaleX": 1, "scaleY": 1, "translateX": 1310000, "translateY": 980000, "unit": "EMU"},
         "image": {"contentUrl": "https://example.invalid/equation.png"}}]}]})
    assert sc.integrity(model) == []


def save_timings() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "perf.json").write_text(json.dumps(TIMINGS, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- live helpers

class Run:
    """One scenario folder: its deck, the CLI calls (logged), timings and the problems found."""

    def __init__(self, name: str):
        self.name, self.out = name, OUT / name
        self.out.mkdir(parents=True, exist_ok=True)
        self.log = open(OUT / f"{name}.log", "w", encoding="utf-8")
        self.problems: list[str] = []
        self.deck = None
        self.before = None
        self.times: dict[str, float] = {}

    def cli(self, *args, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
        self.log.write(f"\n$ beamer2slides {' '.join(map(str, args))}\n")
        self.log.flush()
        done = subprocess.run([sys.executable, "-m", "beamer2slides", *map(str, args)], env={**ENV, **(env or {})},
                              cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        if check and done.returncode:
            raise RuntimeError(f"{self.name}: beamer2slides {args[0]} failed, see {OUT / f'{self.name}.log'}")
        return done

    def timed(self, what: str, fn):
        started = time.time()
        try:
            return fn()
        finally:
            self.times[what] = round(time.time() - started, 1)
            with TIMINGS_LOCK:
                TIMINGS.setdefault(self.name, {}).update(self.times)
                save_timings()

    def convert(self, pdf: Path) -> None:
        from deck_edits import LiveDeck
        # --force-rebuild: the folder holds the deck of the previous run with that run's deck
        # edits still on it, and the rebuild guard would refuse to replace it (guard.py).
        self.timed("convert", lambda: self.cli("convert", pdf, "--out", self.out, "--force-rebuild"))
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

    def sync(self, pdf: Path, what: str = "sync") -> dict:
        self.before = self.deck.read()
        started = time.time()
        self.timed(what, lambda: self.cli("sync", pdf, "--deck", self.out))
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
              checks: list[dict] = (), conflicts: list[list[str]] = (), any_conflicts: bool = False,
              converged: list[list[str]] = (), no_writes_since: str | None = None, edited: list[str] = (),
              gone: tuple[str, ...] = (), overridden: tuple[str, ...] = (), idempotent: bool = True) -> None:
        """Everything a sync of this deck must leave behind: the deck edits (minus `drop`, whose
        own checks the source legitimately changed), the source's own checks, the slide order of
        the variant, the report, integrity, and untouched slides against a fresh conversion.
        `edited`: unique titles this scenario touched, kept out of the fresh comparison.

        The source's checks say what a deck *nobody edited* would show. Where this scenario edited
        the same thing the source did, the deck wins and the source's check cannot hold: `gone`
        names frames deleted in the deck, `overridden` the texts the deck kept instead. Both are
        the scenario declaring which side of a conflict it arranged."""
        import sync_check as sc
        flags = stress.VARIANTS[variant]
        model = self.deck.read()
        kept = [c for e in expectations if e["edit"] not in drop for c in e["checks"]]
        source_checks = [c for c in stress.checks(flags) if c.get("text") not in overridden]
        all_checks = kept + list(checks) + source_checks + [order_check(flags, gone)]
        self.problems += [f"after sync to {variant}: {p}" for p in sc.check_all(model, all_checks)]
        self.problems += sc.check_report(report, conflicts=conflicts, converged=converged,
                                         no_conflicts=not conflicts and not any_conflicts)
        if no_writes_since is not None:
            if sc.changes(report):
                self.problems.append(f"sync to {variant} lists {sc.changes(report)} changes, expected none")
            if model.revision != no_writes_since:
                self.problems.append(f"sync to {variant} changed the presentation revision")
        self.problems += [p for p in sc.integrity(model, before=self.before, base_ids=self.base_ids())
                          if not display_maths_noise(p)]

        # Slides nobody edited: still element for element a fresh conversion of the same source.
        # (Sub-pixel placement is the sync suite's job; this deck is about identity and merging,
        # so only a few untouched slides are compared, and two of them pixel by pixel.)
        fresh_folder, fresh = fresh_conversion(variant)
        titles = [t for t in fresh_titles(flags) if t not in edited
                  and len(model.find(t)) == 1 and len(fresh.find(t)) == 1]
        self.problems += sc.compare_fresh(model, fresh, titles)
        for t in titles[:2]:
            problem = sc.thumbnail_diff(self.deck.api, (self.deck.pid, model.one(t).id),
                                        (fresh.pres["presentationId"], fresh.one(t).id),
                                        self.out / "check" / f"thumb-{titles.index(t) + 1:02}.png")
            if problem:
                self.problems.append(f"fresh {t}: {problem}")

        if idempotent:
            revision = self.revision()
            again = self.sync(pdf, "sync_again")
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
        pdf = stress.build(variant)
        done = folder / ".converted"
        if not done.exists() or done.read_text(encoding="utf-8") != str(pdf.stat().st_mtime):
            folder.mkdir(parents=True, exist_ok=True)
            started = time.time()
            with open(OUT / "_fresh" / f"{variant}.log", "w", encoding="utf-8") as log:
                subprocess.run([sys.executable, "-m", "beamer2slides", "convert", str(pdf), "--out", str(folder)],
                               env=ENV, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
            done.write_text(str(pdf.stat().st_mtime), encoding="utf-8")
            with TIMINGS_LOCK:
                TIMINGS.setdefault("convert", {})[variant] = round(time.time() - started, 1)
                save_timings()
        pid = json.loads((folder / "emit.json").read_text(encoding="utf-8"))["presentationId"]
        return folder, sc.read(pid)


# ---------------------------------------------------------------- scenarios

SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__.removeprefix("scenario_").replace("_", "-")] = fn
    return fn


@scenario
def scenario_ambiguous(run: Run):
    """The hardest identity case: two frames one word apart are swapped, a third frame is
    inserted between them, and a cell changes in every row of a table whose rows all say the same
    - while the deck has edits on exactly those slides and on one of three identical paragraphs."""
    run.convert(stress.build("v1"))
    exps = run.edit(
        # The same sentence is on both twins: this edit must stay on the one it was made on.
        E("replace_word", slide=S("twin-a"), text="This bullet is about identity, not about content",
          old="identity", new="sameness"),
        # One of three copies of the same paragraph (the sentence lands at the end of it).
        E("append_sentence", slide=S("echo-two"),
          text="which is the whole point of repeating it.", sentence="Only the second echo says this."),
        # A cell of the table whose every row repeats a value (the source changes another column).
        E("replace_word", slide=S("repeatcells"), text="Three", old="Three", new="Third"),
        E("add_text_box", slide=S("results-b"), text="Reviewed", box=[560, 60, 130, 28]),
        E("set_notes", slide=S("notes-b"), text="Only the second of the two notes slides says this."))
    pdf = stress.build("ambiguous")
    run.check("ambiguous", pdf, run.sync(pdf), exps, checks=[
        {"check": "text", "slide": None, "text": "sameness", "count": 1},
        {"check": "text", "slide": S("twin-a"), "text": "sameness", "count": 1},
        {"check": "text", "slide": None, "text": "Only the second echo says this.", "count": 1},
        {"check": "text", "slide": None, "text": "Nothing in this paragraph says which slide it is on", "count": 3},
        {"check": "notes", "slide": S("notes-a"), "text": "Remember to slow down here; the audience needs a moment."}])


@scenario
def scenario_identity(run: Run):
    """Identity itself moves: a label moves to the next frame, another disappears, and every
    title in the deck is renamed at once. Nothing the person did may follow the labels.

    `mobile` moving onto the next frame is reported as a conflict and the content decides instead
    (docs/sync.md, "When a label moved"): the bold stays on the frame whose sentence the person
    emboldened, and the frame that lost the label does not come back beside itself. `vanishing`
    losing its label outright is only a warning - the words still recognise that frame."""
    run.convert(stress.build("v1"))
    exps = run.edit(
        E("bold", slide=S("mobile"), word="moves", context="The label on this frame moves"),
        E("replace_word", slide=S("vanishing"), text="so its key falls back to its title", old="key", new="identity"),
        E("set_background", slide=S("arriving"), color="#fff2cc"),
        E("recolour", slide=S("#27"), word="Results", context="This third slide called Results", color="#c00000"),
        E("add_text_box", slide=S("astral"), text="Two code units", box=[540, 300, 150, 28]))
    pdf = stress.build("identity")
    run.check("identity", pdf, run.sync(pdf), exps, checks=[
        {"check": "title", "slide": S("agenda"), "text": "Agenda v2"},
        {"check": "title", "slide": S("mobile"), "text": "Moving labels v2"},
        {"check": "title", "slide": S("vanishing"), "text": "Disappearing label v2"},
        {"check": "text", "slide": S("vanishing"), "text": "so its identity falls back to its title", "count": 1}],
        conflicts=[["label", "mobile", "Arriving labels v2", "identity taken from the content"]],
        edited=["Characters outside the BMP"])


@scenario
def scenario_churn(run: Run):
    """Ten frames reordered, the first and the last deleted, every bullet of one frame and the
    whole of one block rewritten - on a deck that has edits on the moved frames and on the very
    bullets the source rewrites."""
    run.convert(stress.build("v1"))
    exps = run.edit(
        E("add_text_box", slide=S("results-a"), text="Moved but mine", box=[560, 60, 130, 28]),
        E("set_notes", slide=S("displaymath"), text="Do not read the equation out loud."),
        E("move", slide=S("code"), target={"text": "return diff3"}, dx=0, dy=25),
        # The source rewrites this very bullet: a conflict the deck must win. (The slide is found
        # by another bullet, because this one's words are about to change on both sides.)
        E("replace_word", slide={"contains": BACKUP_V1}, text="The numbers are rounded to whole seconds",
          old="whole", new="full"),
        E("delete_slide", slide=S("echo-three")))
    pdf = stress.build("churn")
    run.check("churn", pdf, run.sync(pdf), exps, drop=("replace_word",), checks=[
        {"check": "slide_count", "slide": S("summary"), "count": 0},
        {"check": "slide_count", "slide": S("title"), "count": 0},
        {"check": "slide_count", "slide": S("echo-three"), "count": 0},
        {"check": "text", "slide": None, "text": "The numbers are rounded to full seconds", "count": 1},
        {"check": "text", "slide": S("blockcol"), "text": "The deck always wins, and the report explains why.",
         "count": 1}],
        any_conflicts=True, edited=["A code block"],
        # The deck deleted echo-three and rewrote the third backup bullet; both are arranged here
        # so that the deck wins, so neither of the source's own checks for them can hold.
        gone=("echo-three",), overridden=("Seconds are rounded, milliseconds are dropped",))


@scenario
def scenario_pictures(run: Run):
    """One of two identical pictures is replaced in the source while the person has moved the
    other one and resized the same file on another slide; identical notes change on one frame."""
    run.convert(stress.build("v1"))
    model = run.deck.read()
    left = min((e for e in model.one(S("twinpics")).elements if e.kind == "image"), key=lambda e: e.box[0])
    exps = run.edit(
        E("move", slide=S("twinpics"), target={"id": left.id}, dx=0, dy=-18),
        E("resize", slide=S("picagain"), target={"image": "largest"}, sx=0.8),
        E("set_notes", slide=S("notes-b"), text="This one keeps the deck's notes."))
    pdf = stress.build("pictures")
    run.check("pictures", pdf, run.sync(pdf), exps, checks=[
        {"check": "image", "slide": S("twinpics"), "count": 2},
        {"check": "notes", "slide": S("notes-a"),
         "text": "Slow right down here; give the audience a moment to catch up."},
        {"check": "notes", "slide": S("notes-b"), "text": "This one keeps the deck's notes."}])


@scenario
def scenario_kitchen(run: Run):
    """Everything at once: twins swapped, a frame inserted between them, a label moved, every
    title renamed, ten frames reordered, a frame's bullets rewritten, a cell changed in every
    row, a picture replaced and notes changed - against a deck edited in every way."""
    run.convert(stress.build("v1"))
    exps = run.edit(
        E("replace_word", slide=S("twin-b"), text="This bullet is about identity, not about content",
          old="content", new="wording"),
        E("bold", slide=S("longline"), word="impossible", context="is impossible to see by eye"),
        E("recolour", slide=S("echo-one"), word="first", context="Context for the first echo", color="#c00000"),
        E("move", slide=S("results-a"), target={"text": "Disjoint"}, dx=0, dy=20),
        E("add_shape", slide=S("boxes"), shape_type="STAR_5", box=[620, 300, 44, 44], color="#ffc000"),
        E("add_text_box", slide=S("summary"), text="Ends here", box=[560, 60, 130, 28]),
        E("set_background", slide=S("quote"), color="#eef5ff"),
        E("set_notes", slide=S("gaps"), text="Mention the empty item."),
        E("add_slide", after=S("figcaption"), title="Reviewer questions", body="Does the caption survive?"),
        E("move_slide", slide=S("footnotes"), after=S("description")))
    pdf = stress.build("kitchen")
    run.check("kitchen", pdf, run.sync(pdf), exps, drop=("move_slide",), checks=[
        {"check": "title", "slide": S("summary"), "text": "Takeaways v2"},
        {"check": "text", "slide": None, "text": "A frame added exactly where identity is hardest", "count": 1},
        {"check": "text", "slide": S("twin-b"), "text": "This bullet is about sameness, not about wording", "count": 0},
        {"check": "text", "slide": S("twin-b"), "text": "This bullet is about identity, not about wording", "count": 1}],
        edited=["A figure with a caption v2", "Footnotes and small print v2"])


@scenario
def scenario_pull(run: Run):
    """Wording edits on the ambiguous slides pulled back into the source: the .tex has to receive
    them on the right frames (three slides carry the same paragraph, two the same notes), and the
    sync that follows must call them converged and write nothing."""
    if reason := cli_missing("pull"):
        pytest.skip(reason)
    import sync_check as sc
    src = run.out / "src"
    src.mkdir(exist_ok=True)
    stress.write_figures(src / "figures")
    tex = src / "talk.tex"
    tex.write_text(stress.render([]), encoding="utf-8")
    run.convert(run.timed("compile", lambda: stress.compile_tex(tex)))
    # None of these touches the phrase its own slide is found by: one of three identical
    # paragraphs, one of two twins, and the long line.
    exps = run.edit(
        E("replace_word", slide=S("echo-two"), text="the deck was edited by hand", old="edited", new="polished"),
        E("replace_word", slide=S("twin-b"), text="The merge has to tell these two frames apart",
          old="frames", new="slides"),
        E("replace_word", slide=S("longline"), text="is impossible to see by eye", old="impossible", new="hopeless"))
    run.timed("pull", lambda: run.cli("pull", "--deck", run.out, "--tex", tex, "--apply"))
    source = tex.read_text(encoding="utf-8")
    run.problems += [f"pull didn't write {w!r} into the source"
                     for w in ("the deck was polished by hand", "tell these two slides apart",
                               "is hopeless to see by eye") if w not in source]
    # The same sentence stands on the other twin and the same paragraph on two more slides: an
    # edit that landed on them too would be data changed where nobody asked for it.
    run.problems += [f"pull changed {w!r} on another frame too" for w in
                     ("the deck was converted once", "the source changed again") if f"{w}" not in source]
    if source.count("tell these two slides apart") != 1:
        run.problems.append("pull rewrote the sentence on both twins")
    pdf = run.timed("compile_again", lambda: stress.compile_tex(tex))
    revision = run.revision()
    report = run.sync(pdf)
    model = run.deck.read()
    run.problems += sc.check_all(model, [c for e in exps for c in e["checks"]] + [
        {"check": "text", "slide": None, "text": "the deck was polished by hand", "count": 1},
        {"check": "text", "slide": None, "text": "the deck was converted once", "count": 1},
        {"check": "text", "slide": None, "text": "the source changed again", "count": 1},
        {"check": "text", "slide": None, "text": "tell these two slides apart", "count": 1},
        {"check": "text", "slide": None, "text": "tell these two frames apart", "count": 1}])
    run.problems += sc.check_report(report, converged=[["polished"], ["slides"], ["hopeless"]], no_conflicts=True)
    if sc.changes(report):
        run.problems.append(f"sync after pull lists {sc.changes(report)} changes")
    if run.revision() != revision:
        run.problems.append("sync after pull changed the presentation revision")
    run.problems += sc.integrity(model, before=run.before, base_ids=run.base_ids())


XFAIL = {}  # scenario -> why it cannot pass yet


@pytest.fixture(scope="module")
def outcomes(request):
    """scenario -> problems (or the exception, or a skip reason) for the scenarios selected."""
    names = [n for n in SCENARIOS if any(getattr(i, "callspec", None) and i.callspec.params.get("name") == n
                                         for i in request.session.items)]
    for reason in (cli_missing("sync"), latex_missing(), google_unavailable()):
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
        except Exception as e:  # noqa: BLE001 - reported by that scenario's test
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
