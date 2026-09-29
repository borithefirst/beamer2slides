r"""Every stage that makes deck.json elements, against every stage that reads them.

Each stage has tests of its own, over the decks its author had in mind. What those tests miss is
the pair: an element one producer makes, and a consumer that never saw that producer's elements.
An adopted source's freeform went through classify as a marked shape and raised KeyError 'custom'
in the .pptx's template shapes, because every convert test fed it only beamer's own shapes. So
here every real (producer, consumer) pair runs offline on real decks, and the assertion is the
same everywhere: nothing raises, and no element goes missing on the way (each is written as its
own object, or is a picture the plan knows and has a file for; element counts and ids are kept).

The pairs, traced from the call paths (`ir_sources.py` says which code each name runs):

    producer (stage)            | convert | emission | convert_base | adopt_base | resync | compare | round0 | adopt_read
    ----------------------------+---------+----------+--------------+------------+--------+---------+--------+-----------
    convert   (rendered)        |    x    |    x     |      x       |            |   x    |         |        |
    adopted   (rendered)        |    x    |    x     |              |            |        |         |        |
    folded    (rendered)        |         |    x     |              |     x      |   x    |         |        |
    deck_json (rendered)        |    x    |    x     |              |            |        |         |        |
    fallback  (rendered)        |    x    |          |              |            |        |         |        |
    candidate (classified)      |         |          |              |            |        |         |   x    |
    target    (read: deck_ir)   |         |          |              |            |        |         |        |     x
    read_back (read: deck_ir)   |         |          |              |            |        |    x    |        |

Producers: `convert` = extract/classify/render of a test deck (convert, `deck_prepare`,
`sync.build_ours`); `adopted` = the same of the source adopt writes for a showcase deck
(`marked.classify_marked`, `marked.pictured_shapes`); `folded` = that folded against the deck's
boxes (`adopt_sync.fold_slides`: `convert_source`, `sync.build_ours` on an adopted base);
`deck_json` = deck.json read back (`deck_upload`, a base's `ir`); `fallback` = every non-picture
element refused and cropped (`emit.fallback_pictures`, emit's rebuild); `candidate` = the pull
loop's classify (`inverse.Workspace.build`); `target` = `deck_ir` of a person's deck (the showcase
fixtures); `read_back` = `deck_ir` of the deck convert makes (`slides_sim.simulate`).

Consumers: `convert` = `emit.build_deck` offline (plan, theme, the .pptx with every page's real
pictures, tables and template shapes, phase 1 copies against that .pptx read back, layout styles
and texts, phase 2 per slide, table margins); `emission` = `sync.emitted_elements` /
`emit.slide_emission` at the deck's width, also on the slide read back from JSON (a base's IR);
`convert_base` = `snapshot.build_base` over the simulated read-back; `adopt_base` =
`adopt_sync.labels_match` + `build_base` (pairing, layout, tables, drawn-from); `resync` =
`sync.build_ours` against that base, `mark_emitted(fast=False)`, `theme_sync.ours_side`,
`Sync.slide_requests` for every element (titles in place, tables refilled) and the staging .pptx;
`compare` = `compare.compare` with its source; `round0` = `picture_hashes`, `compare`,
`FrameGuard.observe`, `Planner.plan`; `adopt_read` = `pictures_missing`, `pictures_from_thumbnail`,
`deck_folds` (the bootstrap runs in every adopted producer).

Cases: six built test decks for the beamer side; for the adopted side the six showcase decks
(`tests/decks/foreign/showcase`) and one-slide variants of them (`ir_sources.VARIANTS`), each
holding one thing a deck of the adopt corpus had that crashed a pair there (the corpus itself
cannot be a fixture: no licence).

Real pairs not here, and why:
  * render of a classified deck: every rendered producer above runs it (the pair is the producer).
  * classify -> `emit.plan_offline` before render: `test_emit_requests.py`, every built deck.
  * `fuzz_world` decks -> identity / `merge.plan_merge`: `test_sync_fuzz.py` (they never reach emit;
    their slides carry `bg`, not a background, so `snapshot.slide_entries` is not theirs).
  * classified test deck -> `deck_ir(simulate)` -> compare: `test_inverse.py` (five decks).
  * the pull loop's Planner on a *converted* test deck: it needs that deck's .tex compiled with
    SyncTeX (MiKTeX fetching packages); only adopted sources are compiled here.
  * Google's side of each consumer: the import itself, `measure_places` (thumbnails),
    `write_layouts` / `style_layout_placeholders` (layout reads), `snapshot_after_convert`'s tags
    and signatures, a live `presentations.get` for `adopt_sync.build_base` (no recorded one exists
    in tests: `ir_sources.pres_of` makes it from the target's object ids).

Extending: a producer is one entry of PRODUCERS (how to make it from a case, its cases, the
consumers that read it); a consumer is a function in `ir_sources.py` named in CONSUMERS. A pair
that crashes or drops an element today goes into KNOWN with a one-line reason: it runs as
xfail(strict=True), so fixing it fails the test until the entry goes.

Runs in about 25 s at -n 4 on an idle machine, 45 s on a loaded one (the showcase compiles and
.pptx builds dominate; each case is one xdist group so its compile and conversion are made once).
"""

from collections import namedtuple
from pathlib import Path

import pytest

from . import ir_sources as src

NORMAL = ["04_theme_blocks", "11_research_talk", "19_labels_on_graphics", "20_marks_edge_cases",
          "22_overlays_on_text", "28_frames_code"]
SHOWCASE = ["bees", "hashing", "portfolio", "review", "talk", "water"]
# the showcase decks and their one-slide variants (`ir_sources.VARIANTS`): what adopt reads
ADOPTED = SHOWCASE + list(src.VARIANTS)

CONSUMERS = {
    "convert": src.convert,
    "emission": src.emission,
    "convert_base": src.convert_base,
    "adopt_base": src.adopt_base,
    "resync": src.resync,
    "compare": src.compare_read_back,
    "round0": src.round0,
    "adopt_read": src.adopt_read,
}

# make(case, home, get): get(producer, case) is another producer's (cached) Made.
Producer = namedtuple("Producer", "make cases consumers")


def parent(case: str) -> str:
    return "adopted" if case in ADOPTED else "convert"


PRODUCERS = {
    "convert": Producer(lambda case, home, get: src.converted(case, home), NORMAL,
                        ("convert", "emission", "convert_base", "resync")),
    "adopted": Producer(lambda case, home, get: src.adopted(case, home, get("source", case)), ADOPTED,
                        ("convert", "emission")),
    "folded": Producer(lambda case, home, get: src.folded(get("adopted", case)), ADOPTED,
                       ("emission", "adopt_base", "resync")),
    "deck_json": Producer(lambda case, home, get: src.json_round_trip(get(parent(case), case)), NORMAL + ADOPTED,
                          ("convert", "emission")),
    "fallback": Producer(lambda case, home, get: src.with_fallbacks(get(parent(case), case)),
                         ["04_theme_blocks", "19_labels_on_graphics", "review", "water"], ("convert",)),
    "candidate": Producer(lambda case, home, get: src.candidate(get("adopted", case), home), ADOPTED, ("round0",)),
    "target": Producer(lambda case, home, get: src.target(case, home), ADOPTED, ("adopt_read",)),
    "read_back": Producer(lambda case, home, get: src.read_back(get("convert", case)), NORMAL, ("compare",)),
}

# (producer, case, consumer) -> why it fails today. Strict: a fixed pair fails until it goes.
# Empty: the three variants' failures (an empty slidetable, an ellipsis before a word, a note
# opening on a soft break) were fixed as the matrix found them.
KNOWN: dict[tuple[str, str, str], str] = {}


def needs(case: str):
    if case in ADOPTED:
        return pytest.mark.needs_decks(f"foreign/showcase/{src.showcase_of(case)}/target.json")
    return pytest.mark.needs_decks(f"out/{case}.pdf")


def params():
    for name, p in PRODUCERS.items():
        for case in p.cases:
            for consumer in p.consumers:
                marks = [needs(case), pytest.mark.xdist_group(f"ir-matrix-{case}")]
                why = KNOWN.get((name, case, consumer))
                if why:
                    marks.append(pytest.mark.xfail(strict=True, reason=why))
                yield pytest.param(name, case, consumer, marks=marks, id=f"{name}-{case}-{consumer}")


_made: dict[tuple[str, str], object] = {}


@pytest.fixture(scope="session")
def made(tmp_path_factory):
    """get(producer, case): made once per session (per xdist worker), under the session's tmp."""
    def get(name: str, case: str):
        key = (name, case)
        if key not in _made:
            home = tmp_path_factory.mktemp(f"{name}-{case}")
            if name == "source":
                _made[key] = src.adopted_source(case, home)
            else:
                _made[key] = PRODUCERS[name].make(case, home, get)
        return _made[key]
    return get


@pytest.mark.parametrize("producer, case, consumer", list(params()))
def test_pair(producer, case, consumer, made, tmp_path):
    m = made(producer, case)
    assert isinstance(m, src.Made)
    CONSUMERS[consumer](m, tmp_path)


def test_the_matrix_names_real_things():
    """Every consumer a producer names exists, and every consumer and producer is used."""
    named = {c for p in PRODUCERS.values() for c in p.consumers}
    assert named <= set(CONSUMERS), named - set(CONSUMERS)
    assert set(CONSUMERS) <= named, set(CONSUMERS) - named
    for (name, case, consumer) in KNOWN:
        assert name in PRODUCERS and case in PRODUCERS[name].cases and consumer in PRODUCERS[name].consumers
    doc = __doc__.split("Producers:")[0]
    for name, p in PRODUCERS.items():
        row = next(line for line in doc.splitlines() if line.strip().startswith(name + " "))
        cells = [c.strip() for c in row.split("|")[1:]]
        assert [c for c, x in zip(CONSUMERS, cells) if x == "x"] == [c for c in CONSUMERS if c in p.consumers], \
            f"the docstring's row for {name} is not what PRODUCERS says"
    assert Path(src.__file__).exists()
