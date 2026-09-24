"""Opt-in: sync and pull on the 48-frame stress deck (marker `sync`, deselected by default).

  python -m pytest -m sync tests/test_stress_live.py
  python -m pytest -m sync tests/test_stress_live.py -k "variants or pairs or selectors or budget"  # offline
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
change landed, the slide order follows the source except where the deck moved a slide itself, no
orphans or duplicates, untouched slides still equal a fresh conversion, and a second sync writes
nothing.

Folders: out/stress-tests/<scenario> of the main checkout, fresh conversions
out/stress-tests/_fresh/<variant>, made again only when the PDF or the converter's code changed.
A scenario starts from a Drive copy of the fresh v1 deck (its previous run's deck is deleted), so
v1 is converted once for all of them; pull converts its own. The fresh conversions the checks need
are made while the scenarios run. Timings land in out/stress-tests/perf.json.
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

from .test_slides_alignment import MAIN, google_unavailable

_spec = importlib.util.spec_from_file_location("stress_build", ROOT / "tests" / "decks" / "stress" / "build.py")
stress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stress)
_build, _build_lock = stress.build, threading.Lock()


def _locked_build(variant: str, force: bool = False) -> Path:
    """`stress.build`, one at a time: fresh conversions are made while the scenarios run, and two
    threads compiling one variant would write the same .tex and .pdf (a cache hit costs nothing)."""
    with _build_lock:
        return _build(variant, force)


stress.build = _locked_build

pytestmark = pytest.mark.sync

OUT = Path(os.environ.get("B2S_STRESS_TESTS_OUT", MAIN / "out" / "stress-tests"))
# Scenarios start from a copy of one conversion, so what runs at once is mostly round trips: four
# stays under the write quota (as the live fuzz's --parallel does). The conversions themselves
# (fresh ones, and pull's) are heavy: two at a time.
PARALLEL = 4
FRESH_PARALLEL = 2
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


def order_check(flags: list[str], gone: tuple[str, ...] = (), dragged: tuple[tuple[str, str], ...] = ()) -> dict:
    """The slides of this variant in source order, named by phrases, not by titles. `gone`: frames
    the scenario deleted in the deck - the source still has them and deck edits win, so they are
    not there to be in any order. `dragged`: (frame, the frame it was dropped behind) - the deck
    moved this one, so it stands where the deck put it while the rest follow the source."""
    names = [n for n in stress.names(flags) if n in SEL and n not in gone]
    for name, after in dragged:
        names.remove(name)
        names.insert(names.index(after) + 1, name)
    return {"check": "slides", "order": [S(n) for n in names]}


# Unique titles of frames no scenario edits: what an untouched slide is compared with.
FRESH_TITLES = ["Agenda", "One very long line", "Description list", "Footnotes and small print"]


def fresh_titles(flags: list[str]) -> list[str]:
    return [f"{t} v2" if "retitleall" in flags else t for t in FRESH_TITLES]


def E(edit: str, **args) -> dict:
    return {"edit": edit, "args": args}


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


@pytest.mark.parametrize("variant", [v for v in stress.VARIANTS if v not in ("v1", "recastmoved")])
def test_every_variant_pairs_with_v1_frame_for_frame(variant):
    """Identity itself, against a truth this deck knows: `stress.frames` says which frame of the
    variant is which frame of v1, whatever the source did to it. So the pairing `sync` would use
    (`identity.label_moves` + `align_slides`, on the real classified PDFs, no Google) can be asked
    to be exactly right - on twins one word apart, three frames called Results, an unlabelled frame
    carried across nine others, every title renamed at once, a label moved to the next frame.

    A frame the variant adds must pair with nothing: reading it as one of v1's frames would write
    it over that slide. This is the cheap offline half of what the live scenarios below check."""
    if reason := latex_missing():
        pytest.skip(reason)
    from beamer2slides import identity

    def infos(name):
        deck = json.loads((classified(name) / "deck.json").read_text(encoding="utf-8"))
        return [identity.slide_info(s) for s in deck["slides"]]

    base, ours = infos("v1"), infos(variant)
    base_names, names = stress.names([]), stress.names(stress.VARIANTS[variant])
    assert len(ours) == len(names), f"{len(ours)} slides for {len(names)} frames"
    pairs = identity.align_slides(base, ours, identity.label_moves(base, ours))
    wrong = []
    for j, name in enumerate(names):
        want = base_names.index(name) if name in base_names else None
        if pairs.get(j) != want:
            got = base_names[pairs[j]] if j in pairs else None
            wrong.append(f"frame {name} read as {got}, wanted {name if want is not None else 'a new frame'}")
    assert not wrong, "\n".join(wrong)
    # One slide to one frame, whatever the passes believe: two frames sharing a base slide share
    # its key, and a key is what sync writes by (`identity.align_slides`, offline fuzz seed 5521).
    assert len(set(pairs.values())) == len(pairs), f"two frames of {variant} read as one slide"

    # And the other side of `near_misses`: a report that cries wolf is worse than a quiet one,
    # because an AI author would go labelling frames that were never in doubt. Every one of these
    # variants pairs frame for frame, so it has nothing to say. `strangers` is the one that can
    # get this wrong - a frame the source added, two it dropped, and nothing in common between
    # them - and the only variant besides `recastmoved` where a near miss is possible at all.
    said = [(names[m["ours"]], base_names[m["base"]], m["evidence"])
            for m in identity.near_misses(base, ours, pairs)]
    assert not said, f"near_misses names {said} on a variant whose frames all pair"


def test_the_one_frame_nothing_can_follow_is_named_in_the_report():
    """`recastmoved`, the exception to the test above and the reason it has one: the frame with no
    label gets another title, half its words rewritten *and* a ride across nine other frames, all in
    one version. No label, not enough words, no gap to stand in - so it pairs with nothing, which is
    the right answer (the deck keeps the old slide with its edits and gains a new one beside it) and
    a silent one. `identity.near_misses` is what makes it audible, and this is that on a real deck's
    real words rather than a synthetic one's."""
    if reason := latex_missing():
        pytest.skip(reason)
    from beamer2slides import identity

    def infos(name):
        deck = json.loads((classified(name) / "deck.json").read_text(encoding="utf-8"))
        return [identity.slide_info(s) for s in deck["slides"]]

    base, ours = infos("v1"), infos("recastmoved")
    base_names, names = stress.names([]), stress.names(stress.VARIANTS["recastmoved"])
    j, i = names.index("#27"), base_names.index("#27")
    pairs = identity.align_slides(base, ours, identity.label_moves(base, ours))
    assert j not in pairs, "the passes now follow this frame; the report below is no longer the story"
    assert [(m["ours"], m["base"]) for m in identity.near_misses(base, ours, pairs) if m["ours"] == j] == [(j, i)]
    # And every other frame of this variant still lands exactly where it belongs.
    assert [name for k, name in enumerate(names) if k != j
            and pairs.get(k) != (base_names.index(name) if name in base_names else None)] == []


def test_a_label_written_twice_reaches_the_pdf_as_no_label_at_all():
    """`duplabel` writes `label=mobile` on the `arriving` frame as well, and changes nothing else.
    What comes out is not two slides sharing a label: hyperref refuses the second destination, so
    the PDF has one `mobile` (on the first frame) and no `arriving` at all, and the second frame
    arrives *unlabelled*. Everything downstream of the PDF - `identity`, `sync`, `--check-labels` -
    therefore cannot tell a label written twice from a label never written, and must not pretend
    to: only `beamer2slides label`, which reads the `.tex`, can see it (docs/labels.md).

    This is worth a test of its own because it is the one label mistake the pipeline cannot report
    on its own terms, and because the day the PDF writer stops dropping the duplicate, every pass
    that keys slides by label would suddenly be looking at two slides called `mobile`."""
    if reason := latex_missing():
        pytest.skip(reason)
    from beamer2slides import identity, labels
    from beamer2slides.pdf import Document

    dests = Document(stress.build("duplabel")).named_dests()
    assert sorted(n for n, _ in dests if n.startswith(("mobile", "arriving"))) == ["mobile", "mobile<1>"]

    def infos(name):
        deck = json.loads((classified(name) / "deck.json").read_text(encoding="utf-8"))
        return [identity.slide_info(s) for s in deck["slides"]]

    ours = infos("duplabel")
    j = next(k for k, i in enumerate(ours) if i["title"] == "Arriving labels")
    assert ours[j]["label"] is None and ours[j - 1]["label"] == "mobile"
    # So the survey sees an unlabelled frame, not a duplicate - and says the one it can prove.
    found = labels.survey(ours)
    assert found["duplicates"] == []
    assert [u["title"] for u in found["unlabelled"]] == ["Results", "Arriving labels"]
    assert [u["title"] for u in labels.survey(infos("v1"))["unlabelled"]] == ["Results"]


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


def _page_element(oid: str, tag: str, box: list[float], **kind) -> dict:
    """One `presentations.get` page element at an absolute box, in pt."""
    x0, y0, x1, y1 = box
    return {"objectId": oid, "title": tag, **kind,
            "size": {"width": {"magnitude": x1 - x0, "unit": "PT"}, "height": {"magnitude": y1 - y0, "unit": "PT"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x0, "translateY": y0, "unit": "PT"}}


def _text_box(oid: str, tag: str, box: list[float], text: str) -> dict:
    return _page_element(oid, tag, box, shape={"shapeType": "TEXT_BOX", "text": {
        "textElements": [{"textRun": {"content": text}}]}})


def _picture(oid: str, tag: str, box: list[float]) -> dict:
    return _page_element(oid, tag, box, image={"contentUrl": "https://example.invalid/equation.png"})


def test_integrity_does_not_ask_display_maths_to_be_grouped():
    """A display equation becomes a picture of its own (`classify`: role math, anchor None), tagged
    `.../image/math/N` exactly like an inline formula. The checker used to ask every such picture to
    be grouped with its text, so every deck holding display maths reported a problem that was not
    one - and `Run.integrity` had to filter that sentence out to see the real ones.

    What tells them apart is where the picture stands (`sync_check.on_a_text_line`): an inline
    formula sits between the words of a line, so the text's box holds it top and bottom; a display
    equation stands on its own between paragraphs. Both halves are here, because the fix would be
    worthless if it also stopped asking for the inline one."""
    from beamer2slides.devtools import sync_check as sc

    display = sc.Model({"slides": [{"objectId": "s1", "pageElements": [
        _text_box("b2s_s034_t0", "b2s:displaymath/text/body/0", [60, 100, 660, 130], "The equation below:\n"),
        _picture("b2s_s034_f0", "b2s:displaymath/image/math/0", [260, 160, 460, 200])]}]})
    assert sc.integrity(display) == []

    # The same picture moved up onto the line of that text, and still not grouped with it.
    inline = sc.Model({"slides": [{"objectId": "s1", "pageElements": [
        _text_box("b2s_s034_t0", "b2s:displaymath/text/body/0", [60, 100, 660, 130], "The equation   here:\n"),
        _picture("b2s_s034_f0", "b2s:displaymath/image/math/0", [260, 106, 300, 124])]}]})
    assert [p for p in sc.integrity(inline) if "not grouped with its text" in p]


def test_integrity_excuses_a_slide_by_id_when_the_source_retitled_it():
    """A person who takes a converter group apart owns that slide's grouping from then on, and the
    caller says so by naming the slide in `allow_ungrouped`. Naming it by title alone is not enough
    when the source retitles the frame in the same step - which is exactly what a chained fuzz round
    did (live seed 607: `ungroup` on "Why decks and sources diverge", then a sync to the `retitle`
    variant, which calls that frame something else), and the slide was accused of the very group its
    own edit had dissolved. The objectId is the one name of a slide a sync cannot change."""
    from beamer2slides.devtools import sync_check as sc

    model = sc.Model({"slides": [{"objectId": "b2s_s002", "pageElements": [
        _page_element("b2s_s002_t1", "b2s:diverge/text/title/0", [40, 30, 600, 60],
                      shape={"shapeType": "TEXT_BOX", "placeholder": {"type": "TITLE"},
                             "text": {"textElements": [{"textRun": {"content": "Why decks drift away\n"}}]}}),
        _text_box("b2s_s002_t0", "b2s:diverge/text/body/0", [60, 100, 660, 130], "The source says   this:\n"),
        _picture("b2s_s002_f0", "b2s:diverge/image/math/0", [260, 106, 300, 124])]}]})
    assert [p for p in sc.integrity(model) if "not grouped" in p]
    assert sc.integrity(model, allow_ungrouped={"b2s_s002"}) == []
    assert sc.integrity(model, allow_ungrouped={"Why decks drift away"}) == []   # the title still works


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

    def clear(self) -> None:
        """The previous run's deck (with that run's deck edits on it) and folder, gone. Rebuilding
        it in place took a forced rebuild, and with it a .pptx backup, per scenario."""
        from beamer2slides.gapi import HttpError
        from beamer2slides.google_auth import drive_service
        from beamer2slides.gslides import execute, status_of
        emitted = self.out / "emit.json"
        if emitted.exists():
            try:
                execute(drive_service().files().delete(
                    fileId=json.loads(emitted.read_text(encoding="utf-8"))["presentationId"]))
            except HttpError as e:
                if status_of(e) != 404:
                    raise
        shutil.rmtree(self.out, ignore_errors=True)
        self.out.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        """The v1 deck, as a Drive copy of the fresh v1 conversion: one conversion for every
        scenario instead of one each (`fuzz_sync.Template`, the live fuzz's `--reuse`)."""
        from beamer2slides.devtools.deck_edits import LiveDeck
        from beamer2slides.devtools.fuzz_sync import Template
        folder, _ = fresh_conversion("v1", read=False)
        self.clear()
        pid = self.timed("copy", lambda: Template.at(folder).copy_into(self.out, f"b2s stress: {self.name}"))
        self.deck = LiveDeck(pid)

    def convert(self, pdf: Path) -> None:
        """A conversion of its own (after `clear`: the guard refuses to rebuild an edited deck)."""
        from beamer2slides.devtools.deck_edits import LiveDeck
        self.timed("convert", lambda: self.cli("convert", pdf, "--out", self.out))
        self.deck = LiveDeck(json.loads((self.out / "emit.json").read_text(encoding="utf-8"))["presentationId"])

    def edit(self, *specs: dict) -> list[dict]:
        from beamer2slides.devtools.deck_edits import verified
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
        from beamer2slides.devtools.sync_check import ids_in
        base = next((p for p in (self.out / "sync" / "base.json", self.out / "base.json") if p.exists()), None)
        return ids_in(json.loads(base.read_text(encoding="utf-8"))) if base else None

    def integrity(self, model) -> list[str]:
        """What the checker says about the deck's structure - all of it. It used to have one
        sentence filtered out, the one it said about every deck with display maths; the checker
        now tells a display equation from an inline formula itself (`sync_check.on_a_text_line`)."""
        from beamer2slides.devtools import sync_check as sc
        return sc.integrity(model, before=self.before, base_ids=self.base_ids())

    def check(self, variant: str, pdf: Path, report: dict, expectations: list[dict], *, drop: tuple[str, ...] = (),
              checks: list[dict] = (), conflicts: list[list[str]] = (), any_conflicts: bool = False,
              converged: list[list[str]] = (), no_writes_since: str | None = None, edited: list[str] = (),
              gone: tuple[str, ...] = (), overridden: tuple[str, ...] = (), warnings: list[list[str]] = (),
              dragged: tuple[tuple[str, str], ...] = (), idempotent: bool = True) -> None:
        """Everything a sync of this deck must leave behind: the deck edits (minus `drop`, whose
        own checks the source legitimately changed), the source's own checks, the slide order of
        the variant, the report, integrity, and untouched slides against a fresh conversion.
        `edited`: unique titles this scenario touched, kept out of the fresh comparison.

        The source's checks say what a deck *nobody edited* would show. Where this scenario edited
        the same thing the source did, the deck wins and the source's check cannot hold: `gone`
        names frames deleted in the deck, `dragged` frames moved there, `overridden` the texts the
        deck kept instead. All three are the scenario declaring which side of a conflict it arranged."""
        from beamer2slides.devtools import sync_check as sc
        flags = stress.VARIANTS[variant]
        model = self.deck.read()
        kept = [c for e in expectations if e["edit"] not in drop for c in e["checks"]]
        source_checks = [c for c in stress.checks(flags) if c.get("text") not in overridden]
        all_checks = kept + list(checks) + source_checks + [order_check(flags, gone, dragged)]
        self.problems += [f"after sync to {variant}: {p}" for p in sc.check_all(model, all_checks)]
        self.problems += sc.check_report(report, conflicts=conflicts, converged=converged, warnings=warnings,
                                         no_conflicts=not conflicts and not any_conflicts)
        if no_writes_since is not None:
            if sc.changes(report):
                self.problems.append(f"sync to {variant} lists {sc.changes(report)} changes, expected none")
            if model.revision != no_writes_since:
                self.problems.append(f"sync to {variant} changed the presentation revision")
        self.problems += self.integrity(model)

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


def fresh_conversion(variant: str, read: bool = True):
    """(folder, model) of a fresh conversion of a source version, converted again only when its
    PDF or the converter changed (`sync_check.converter_stamp`). Nobody edits these decks: `v1`'s
    is also every scenario's starting deck, as a copy (`Run.start`)."""
    from beamer2slides.devtools import sync_check as sc
    with _fresh_guard:
        lock = _fresh_locks.setdefault(variant, threading.Lock())
    with lock:
        folder = OUT / "_fresh" / variant
        pdf = stress.build(variant)
        done = folder / ".converted"
        stamp = f"{pdf.stat().st_mtime} {sc.converter_stamp()}"
        if not done.exists() or done.read_text(encoding="utf-8") != stamp:
            folder.mkdir(parents=True, exist_ok=True)
            started = time.time()
            with open(OUT / "_fresh" / f"{variant}.log", "w", encoding="utf-8") as log:
                subprocess.run([sys.executable, "-m", "beamer2slides", "convert", str(pdf), "--out", str(folder)],
                               env=ENV, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
            done.write_text(stamp, encoding="utf-8")
            with TIMINGS_LOCK:
                TIMINGS.setdefault("convert", {})[variant] = round(time.time() - started, 1)
                save_timings()
        pid = json.loads((folder / "emit.json").read_text(encoding="utf-8"))["presentationId"]
        return folder, (sc.read(pid) if read else None)


# ---------------------------------------------------------------- scenarios

SCENARIOS = {}
# scenario -> the variant its check compares with a fresh conversion (`Run.check`)
CHECKED = {"ambiguous": "ambiguous", "identity": "identity", "recast-moved": "recastmoved", "churn": "churn",
           "pictures": "pictures", "kitchen": "kitchen"}


def scenario(fn):
    SCENARIOS[fn.__name__.removeprefix("scenario_").replace("_", "-")] = fn
    return fn


@scenario
def scenario_ambiguous(run: Run):
    """The hardest identity case: two frames one word apart are swapped, a third frame is
    inserted between them, and a cell changes in every row of a table whose rows all say the same
    - while the deck has edits on exactly those slides and on one of three identical paragraphs."""
    run.start()
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
    losing its label outright is only a warning - the words still recognise that frame.

    The third frame called Results has no label to lose: the source gives it another title and
    rewrites half of what it says, which leaves the words too thin for the alignment. It keeps its
    slide - and the person's red word on it - because it is the only slide and the only frame
    between two neighbours that paired (`identity.gap_pairs`)."""
    run.start()
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
        {"check": "text", "slide": S("vanishing"), "text": "so its identity falls back to its title", "count": 1},
        # The recast frame: another title, half its words new, and the person's red word still on it.
        {"check": "title", "slide": S("#27"), "text": "The third table of numbers v2"},
        {"check": "slide_count", "slide": S("#27"), "count": 1}],
        conflicts=[["label", "mobile", "Arriving labels v2", "identity taken from the content"]],
        edited=["Characters outside the BMP"])


@scenario
def scenario_recast_moved(run: Run):
    """The frame nothing can follow, end to end. The unlabelled third Results frame gets another
    title, half its words rewritten *and* a ride across nine other frames, in one version - while
    the person has coloured a word on its slide.

    Sync does the only safe thing: it reads the frame as new, makes a slide for it, and leaves the
    old slide alone with the person's red word on it. What it may not do is pass in silence, and
    that is what this scenario is for: the report names the slide it could not match and the frame
    that looks like it (`identity.near_misses`), so an AI author reading the report can put a label
    on that frame and have a person move the edits over."""
    run.start()
    old = {"contains": "so only its content can identify it"}   # the sentence only the old slide has
    new = {"contains": "the source has now given it"}           # and the one only the new slide has
    exps = run.edit(
        E("recolour", slide=old, word="Results", context="This third slide called Results", color="#c00000"),
        E("add_text_box", slide=S("astral"), text="Mine, on a frame nothing touches", box=[540, 300, 150, 28]))
    pdf = stress.build("recastmoved")
    run.check("recastmoved", pdf, run.sync(pdf), exps, checks=[
        # Both slides say "no label at all", so the source's own checks (which name the frame by
        # that phrase) cannot tell them apart any more: `overridden` drops them and these take over.
        {"check": "slide_count", "slide": old, "count": 1},
        {"check": "slide_count", "slide": new, "count": 1},
        {"check": "text", "slide": new, "count": 1,
         "text": "another title and rewritten the rest of what it says about itself."}],
        # The frame is in the deck twice now - the person's slide and the source's new one - so no
        # single place in the order is the right one for it.
        gone=("#27",),
        overridden=("so only its content can identify it",
                    "another title and rewritten the rest of what it says about itself."),
        warnings=[["no frame this slide could be matched to", "The third table of numbers"]],
        edited=["Characters outside the BMP"])


@scenario
def scenario_churn(run: Run):
    """Ten frames reordered, the first and the last deleted, every bullet of one frame and the
    whole of one block rewritten - on a deck that has edits on the moved frames and on the very
    bullets the source rewrites."""
    run.start()
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
    run.start()
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
    run.start()
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
    run.check("kitchen", pdf, run.sync(pdf), exps, checks=[
        {"check": "title", "slide": S("summary"), "text": "Takeaways v2"},
        {"check": "text", "slide": None, "text": "A frame added exactly where identity is hardest", "count": 1},
        {"check": "text", "slide": S("twin-b"), "text": "This bullet is about sameness, not about wording", "count": 0},
        {"check": "text", "slide": S("twin-b"), "text": "This bullet is about identity, not about wording", "count": 1}],
        # The moved label is the same conflict `identity` declares. The deck dragged `footnotes`
        # behind `description`, so that one slide stands where the deck put it and the eleven the
        # source moved (the twins, the ten reversed) still follow the source.
        conflicts=[["label", "mobile", "Arriving labels v2", "identity taken from the content"]],
        dragged=(("footnotes", "description"),),
        # "One very long line" carries the deck's bold: it cannot be element for element, let alone
        # pixel for pixel, what a fresh conversion of the source makes of that frame.
        edited=["A figure with a caption v2", "Footnotes and small print v2", "One very long line v2"])


@scenario
def scenario_pull(run: Run):
    """Wording edits on the ambiguous slides pulled back into the source: the .tex has to receive
    them on the right frames (three slides carry the same paragraph, two the same notes), and the
    sync that follows must call them converged and write nothing."""
    if reason := cli_missing("pull"):
        pytest.skip(reason)
    from beamer2slides.devtools import sync_check as sc
    run.clear()
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
    run.problems += run.integrity(model)


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
    # The fresh conversions the checks will want are made while the scenarios edit and sync, not
    # one by one when each check first asks (each is a cache hit when nothing changed). v1's
    # goes first: every scenario but pull starts from a copy of it.
    wanted = ["v1"] * any(n != "pull" for n in names) + [CHECKED[n] for n in names if n in CHECKED]
    with ThreadPoolExecutor(max_workers=FRESH_PARALLEL) as fresh, ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        warm = [fresh.submit(fresh_conversion, v, False) for v in wanted]
        if warm:
            warm[0].result()   # (a failed v1 would fail every scenario the same way: say so once)
        found = dict(zip(names, pool.map(run, names)))
        for w in warm:
            w.result()
        return found


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
