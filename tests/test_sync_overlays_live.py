"""Opt-in (marker `sync`): a deck converted with `--overlays all` keeps every step.

Every overlay step of a labelled frame carries that frame's label, so such a deck holds several
slides under one label. They all paired with the same base slide, which left the others unpaired -
and an unpaired base slide is one the source dropped: syncing the deck against its own unchanged
PDF planned to delete a step, then crashed on the slide order (found live on 2026-09-18,
`identity.align_slides`). The base also records the mode now, so a sync without `--overlays` uses
the one the deck was made with (`sync.overlay_mode`).

  python -m pytest -m sync tests/test_sync_overlays_live.py

Folder `out/sync-tests/overlays-all` of the main checkout, one deck rebuilt in place.
"""

import json

import pytest

from test_slides_alignment import google_unavailable  # noqa: E402
from test_sync_live import OUT, Run, build, cli_missing, pdflatex_missing  # noqa: E402

pytestmark = pytest.mark.sync

STEPS = 11   # v1.pdf with every overlay step
LAST = 10    # v1.pdf with the last step of each frame


@pytest.fixture(scope="module")
def run():
    for reason in (cli_missing("sync"), pdflatex_missing(), google_unavailable()):
        if reason:
            pytest.skip(reason)
    OUT.mkdir(parents=True, exist_ok=True)
    r = Run("overlays-all")
    from deck_edits import LiveDeck
    r.cli("convert", build("v1"), "--out", r.out, "--overlays", "all", "--force-rebuild")
    r.deck = LiveDeck(json.loads((r.out / "emit.json").read_text(encoding="utf-8"))["presentationId"])
    yield r
    r.log.close()


def test_the_deck_has_a_slide_per_step(run):
    assert len(run.deck.read().slides) == STEPS > LAST
    base = json.loads((run.out / "sync" / "base.json").read_text(encoding="utf-8"))
    assert base["overlays"] == "all", "the base must record the mode, or sync will use the other one"
    labels = [s.get("label") for s in base["slides"]]
    assert len(labels) != len(set(labels)), "this deck is only a test of anything while a label repeats"


def test_syncing_the_same_source_writes_nothing(run):
    import sync_check as sc
    revision = run.revision()
    report = run.sync(build("v1"))
    assert report["overlays"] == "all", "sync must keep the steps the deck was converted with"
    assert sc.changes(report) == 0, f"a sync of the same source changed {sc.changes(report)} things"
    assert run.revision() == revision
    assert len(run.deck.read().slides) == STEPS


def test_a_source_change_lands_and_every_step_stays(run):
    import sync_check as sc
    pdf = build("reword")
    report = run.sync(pdf)
    model = run.deck.read()
    assert len(model.slides) == STEPS, "a step was lost"
    # count 2: the reworded bullet shows on both steps of that frame, and the change must land on both
    assert not sc.check_all(model, [{"check": "text", "slide": {"contains": "Why decks and sources diverge"},
                                     "text": "by an AI assistant and converted once", "count": 2}])
    assert sc.integrity(model, base_ids=run.base_ids()) == []
    assert sc.changes(report) >= 1
    run.cli("convert", build("v1"), "--out", run.out, "--overlays", "all", "--force-rebuild")  # (leave it at v1)
