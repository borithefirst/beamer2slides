"""The built test decks as convert's local half reads them, made once per test run.

`classified_decks(pdfs)`: each PDF's notes taken out (`notes.prepare`), extracted, its notes put
on its pages, the last overlay step of each frame kept and classified - deck.json, as
`test_ir.read` and `test_emit_requests.emitted` make it. Several files check every built deck
(test_ir, test_ir_types, test_emit_requests, test_emitted_diff), each once per xdist worker it runs
on: ~70 PDFs at ~0.1 s each, the same work three or four times over.

So a deck is made once per run and kept as its pickle, in this process and in the run's folder
(`SHARED`, which conftest makes and removes, and the xdist workers inherit). A worker claims a deck
before making it (`<key>.lock`, created exclusively), and one that finds a deck claimed goes on to
the next and comes back for it, so workers asking for the same decks at once share the work rather
than doing it twice. Every caller gets its own copy (each call unpickles), as each made its own
before; a pickle keeps tuples, float bits and key order, so the copy is the deck as made. The key
is the PDF's bytes: the folder lives for one run, so the code that made a deck is the code running.

Without the folder (a test run on its own, a script) decks are made and kept in this process."""

import hashlib
import os
import pickle
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from beamer2slides.json_types import JsonObject, as_object

SHARED = "B2S_TEST_SHARED"
RECIPE = "classified-last"  # what a kept deck is: change it when `make` changes
STALE = 120.0  # s a claim may stand before a waiting worker makes the deck itself

_kept: dict[str, bytes] = {}


def make(pdf: Path) -> JsonObject:
    # (imported here: conftest imports this module, and a run that never asks for a deck need not
    # import the converter for it)
    from beamer2slides.classify import classify
    from beamer2slides.extract import extract, select_overlays
    from beamer2slides.ir import deck_json
    from beamer2slides.notes import prepare

    with tempfile.TemporaryDirectory() as tmp:
        prepared = prepare(pdf, Path(tmp))
        raw = extract(prepared.pdf, prepared.labels)
        for page in raw["pages"]:
            page["notes"] = prepared.notes.get(page["index"])
        return deck_json(classify(select_overlays(raw, "last")))


def key_of(pdf: Path) -> str:
    return f"{RECIPE}-{hashlib.sha256(pdf.read_bytes()).hexdigest()}"


def deck_of(data: bytes, pdf: Path) -> JsonObject:
    return as_object(pickle.loads(data), f"kept deck of {pdf}")


def shared_folder() -> Path | None:
    folder = os.environ.get(SHARED)
    return Path(folder) if folder and Path(folder).is_dir() else None


def kept_on_disk(folder: Path, key: str) -> bytes | None:
    try:
        return (folder / f"{key}.pickle").read_bytes()
    except (FileNotFoundError, PermissionError):  # (Windows: being renamed into place, or scanned)
        return None


def keep(key: str, data: bytes, folder: Path | None) -> None:
    _kept[key] = data
    if folder is not None and not (folder / f"{key}.pickle").exists():
        part = folder / f"{key}.{os.getpid()}.part"
        part.write_bytes(data)
        try:
            os.replace(part, folder / f"{key}.pickle")
        except PermissionError:  # (Windows: another worker kept it first and is reading it)
            part.unlink()


def made(pdf: Path, key: str, folder: Path | None) -> bytes:
    """Make the deck and keep it; a failure gives the claim up, so the next asker fails as this did."""
    try:
        data = pickle.dumps(make(pdf), protocol=pickle.HIGHEST_PROTOCOL)
        keep(key, data, folder)
        return data
    finally:
        if folder is not None:
            (folder / f"{key}.lock").unlink(missing_ok=True)


def claim(folder: Path, key: str) -> bool:
    try:
        os.close(os.open(folder / f"{key}.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def claimed_since(folder: Path, key: str) -> float:
    try:
        return time.time() - (folder / f"{key}.lock").stat().st_mtime
    except FileNotFoundError:
        return 0.0


def looked_up(pdf: Path, key: str, folder: Path | None) -> bytes | None:
    """The kept deck, or the deck made here; None while another worker is making it."""
    found = _kept.get(key)
    if found is not None:
        return found
    if folder is None:
        return made(pdf, key, None)
    found = kept_on_disk(folder, key)
    if found is not None:
        return found
    if claim(folder, key):
        # (whoever claimed it before may have kept it and let go between that look and this claim)
        found = kept_on_disk(folder, key)
        if found is None:
            return made(pdf, key, folder)
        (folder / f"{key}.lock").unlink(missing_ok=True)
        return found
    return made(pdf, key, folder) if claimed_since(folder, key) > STALE else None


def classified_decks(pdfs: Sequence[Path]) -> list[JsonObject]:
    """deck.json of each PDF, in order; made here only where nobody has made or claimed it."""
    folder = shared_folder()
    keys = [key_of(pdf) for pdf in pdfs]
    data: dict[str, bytes] = {}
    waiting = list(dict.fromkeys(keys))
    by_key = dict(zip(keys, pdfs))
    while waiting:
        for key in list(waiting):
            found = looked_up(by_key[key], key, folder)
            if found is not None:
                _kept[key] = found
                data[key] = found
                waiting.remove(key)
        if waiting:
            time.sleep(0.02)
    return [deck_of(data[key], pdf) for key, pdf in zip(keys, pdfs)]
