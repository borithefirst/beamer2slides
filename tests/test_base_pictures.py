"""Where a sync base's picture files are (`snapshot.BasePictures`).

A base names each picture by its file (`ir.file`) and records the first 12 hex of that file's sha1
(`fields.image`). Two readers need the file back: `refresh_pictures` (a picture whose file changed
but that looks the same is no source change) and `rehash_base` (a base in an older form, hashed
again). They looked it up by name, in folders each chose for itself - and a name does not say which
picture: every sync renders the new PDF into `<out>/sync/ours` under the same names, over the files
of the sync before, while the out folder `convert` wrote keeps the oldest picture of each name.

So a figure the source changed and then changed back was read as "the same picture, written
differently": `refresh_pictures` compared the figure `convert` had made with the one the source now
draws, found them alike, and gave the base the new hash - the deck kept the changed figure, the
report said it had converged, and no later sync would put it right. Now a base picture is its bytes
(`find_base_pictures`), the files of the last sync are held out of the next render's way
(`hold_base_pictures`), and one function says which folders a deck's bases use
(`picture_folders`)."""

import copy
from pathlib import Path

import pytest

from beamer2slides import identity, merge, snapshot, sync
from beamer2slides.emit import SLIDE_W

from .json_reads import jobj, jstr
from .test_sync import _picture_deck, _picture_files, merged_view, unit
from .test_sync_containment import SYNC_DECKS, talk_base

FIGURE = ("convergence", "image/figure/0")   # (the sync talk's plot, which the `figure` variant redraws)


def converted_for_sync(pdf: Path, out: Path, base: dict) -> tuple[sync.Built, list[snapshot.Refreshed]]:
    """What `sync.sync` does before it plans: the base's pictures held, the new PDF converted into
    the deck folder's `sync_work`, and the pictures written differently taken into the base."""
    pictures = snapshot.hold_base_pictures(base, out)
    ours = sync.build_ours_of(pdf, snapshot.sync_work(out), base, "last", SLIDE_W, pictures)
    return ours, snapshot.refresh_pictures(base, ours.slides, ours.pairs, ours.out, pictures)


def written_by_a_sync(base: dict, ours: sync.Built) -> dict:
    """The base a sync that recreated every unit the source changed leaves behind, as
    `Sync.new_base` records one: the new conversion's element, with the object the deck now holds
    for it (read back as the base had it: the person has not touched it)."""
    after = copy.deepcopy(base)
    for j, i in ours.pairs.items():
        now = {e["key"]: e for e in ours.slides[j]["elements"]}
        elements = after["slides"][i]["elements"]
        for k, e in enumerate(elements):
            o = now.get(e["key"])
            if o is not None and o["ir_hash"] != e["ir_hash"]:
                elements[k] = {**o, "objects": e["objects"], "main": e["main"], "readback": e["readback"]}
    return after


@pytest.mark.needs_decks("sync/out/v1.pdf", "sync/out/figure.pdf")
def test_a_figure_the_source_changed_back_is_written_back(tmp_path: Path) -> None:
    """convert v1, sync the redrawn plot (`figure`), then sync v1 again. The second sync must put
    v1's plot back. It found the base's picture by name in the out folder - v1's own plot, the
    same as the one it had just rendered - so it said "the same picture, written differently",
    took the new hash into the base and wrote nothing: the deck kept the redrawn plot for good."""
    base, pres = talk_base(tmp_path)
    out, theirs = tmp_path / "v1", snapshot.read_presentation(pres)

    ours, refreshed = converted_for_sync(SYNC_DECKS / "figure.pdf", out, base)
    assert refreshed == []
    assert unit(merge.plan_merge(copy.deepcopy(base), sync.ours_json(ours), theirs), *FIGURE)["action"] == "recreate"
    base = written_by_a_sync(base, ours)

    ours, refreshed = converted_for_sync(SYNC_DECKS / "v1.pdf", out, base)
    assert refreshed == [], "v1's plot is not the redrawn one written differently"
    assert unit(merge.plan_merge(copy.deepcopy(base), sync.ours_json(ours), theirs), *FIGURE)["action"] == "recreate"
    # ... and the redrawn plot the base records was held before the render wrote v1's over it
    [held] = [snapshot.find_base_pictures(base, snapshot.picture_folders(out)).folder(e)
              for s in base["slides"] if s["key"] == FIGURE[0] for e in s["elements"] if e["key"] == FIGURE[1]]
    assert held is not None and held.parent == snapshot.held_pictures(out)


def test_the_last_syncs_picture_is_held_before_the_next_render(tmp_path: Path) -> None:
    """A base a sync wrote names the files that sync rendered, in the folder the next sync renders
    into. Held first, the base's picture is still there to compare: the same picture written on a
    clear ground is no source change. Read by name after the render, the base's side would be the
    new file itself - which is why nothing is ever read by name alone: not found, the picture reads
    as changed and is written again, never the other way round."""
    out = tmp_path / "deck"
    work = snapshot.sync_work(out)
    _picture_files(work, transparent=False)                 # what the last sync rendered
    base, _, theirs = _picture_deck(work, work)             # ... and the base it wrote names
    unheld = copy.deepcopy(base)
    pictures = snapshot.hold_base_pictures(base, out)
    _picture_files(work, transparent=True)                  # this sync's render, over it
    _, ours, _ = _picture_deck(work, work)

    el = jobj(base, "slides", 0, "elements", 1)
    assert pictures.folder(el) == snapshot.held_pictures(out) / jstr(el, "fields", "image")
    assert snapshot.refresh_pictures(base, ours["slides"], ours["pairs"], ours["out"], pictures) == [
        snapshot.Refreshed(slide="figs", element="image/math/0")]
    assert unit(merge.plan_merge(base, merged_view(ours), theirs), "figs", "image/math/0")["action"] == "keep"

    # (by name, the file under it now is the new render: not the base's picture, so not found)
    by_name = snapshot.find_base_pictures(unheld, snapshot.PictureFolders(kept=(), rendered=work, held=None))
    assert by_name.folder(jobj(unheld, "slides", 0, "elements", 1)) is None
    assert snapshot.refresh_pictures(unheld, ours["slides"], ours["pairs"], ours["out"], by_name) == []
    assert unit(merge.plan_merge(unheld, merged_view(ours), theirs), "figs", "image/math/0")["action"] == "recreate"


def test_held_pictures_no_base_names_go(tmp_path: Path) -> None:
    out = tmp_path / "deck"
    _picture_files(snapshot.sync_work(out), transparent=False)
    base, _, _ = _picture_deck(snapshot.sync_work(out), snapshot.sync_work(out))
    snapshot.hold_base_pictures(base, out)
    assert [p.name for p in snapshot.held_pictures(out).iterdir()] == [jstr(base, "slides", 0, "elements", 1, "fields", "image")]
    snapshot.hold_base_pictures({"slides": []}, out)
    assert list(snapshot.held_pictures(out).iterdir()) == []


def test_an_adopt_bases_pictures_are_where_adopt_converted_the_source(tmp_path: Path) -> None:
    """`adopt` records its base in its work folder and converts the source into `sync-base/ours`
    there; neither the out folder nor the sync's work folder held those files, so every picture of
    an adopt base read as gone (`rehash_base`) and was never found the same (`refresh_pictures`)."""
    out = tmp_path / "adopted"
    folder = _picture_files(out / "sync-base" / "ours", transparent=False)
    base, _, _ = _picture_deck(folder, folder)
    el = jobj(base, "slides", 0, "elements", 1)
    assert snapshot.find_base_pictures(base, snapshot.picture_folders(out)).folder(el) == folder
    assert identity.ir_fields(jobj(el, "ir"), folder)[0] == el["ir_hash"]
