"""A sync base the way `snapshot` writes one, for tests that hand one to the guard without a
conversion (tests/test_guard.py).

Each object of a presentation is the converter element it was written from, under the key the test
names: `identity` hashes and fingerprints it, `snapshot.slide_entries_of` makes its entry, and
`snapshot.attached` ties it to its object and records what the deck read back - the steps
`snapshot.build_base_of` takes around a real IR. So the base parses (`sync_model.base`) as one
convert wrote, and a test changes only the fields its rule reads.

A picture's file is never read: its element hashes its IR alone, as an image whose file is gone
does."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from beamer2slides import identity, snapshot
from beamer2slides.json_types import JsonObject
from beamer2slides.sync_model import Base, ElementKey, ObjectId, SlideKey, base_json

NOWHERE = Path("made-bases-read-no-files")


@dataclass(frozen=True, kw_only=True)
class Made:
    """One object of the deck and the element convert wrote it from."""
    key: str
    object_id: str
    element: JsonObject


@dataclass(frozen=True, kw_only=True)
class MadeSlide:
    """One slide of the deck: its key, its objectId, what it was written from."""
    key: str
    object_id: str
    notes: str
    background_color: str
    elements: Sequence[Made]


def text(eid: str, role: str, box: Sequence[float], words: str) -> JsonObject:
    """A text element as classify writes one, down to what identity reads of it."""
    return {"id": eid, "kind": "text", "role": role, "bbox": list(box), "paragraphs": [{"runs": [{"text": words}]}]}


def picture(eid: str, box: Sequence[float], file: str) -> JsonObject:
    return {"id": eid, "kind": "image", "role": "figure", "bbox": list(box), "file": file}


def made_base(pres: dict, slides: Sequence[MadeSlide], pdf: str, generation: int) -> dict:
    """The base.json `convert` would record after writing `slides` as the deck `pres` (the answer
    of presentations.get), from the PDF at `pdf`."""
    deck: JsonObject = {"slides": [{"page": n, "background_color": s.background_color, "notes": s.notes,
                                    "elements": [m.element for m in s.elements]} for n, s in enumerate(slides)]}
    keys = [SlideKey(s.key) for s in slides]
    element_keys = [[ElementKey(m.key) for m in s.elements] for s in slides]
    fingerprints = [[identity.fingerprint_of(m.element, NOWHERE, None) for m in s.elements] for s in slides]
    entries = snapshot.slide_entries_of(deck, NOWHERE, keys, element_keys, fingerprints)
    read = snapshot.read_presentation_of(pres)
    by_id = {r.object_id: r for r in read.slides}
    attached = [snapshot.attached(entry, by_id.get(ObjectId(s.object_id)),
                                  [[ObjectId(m.object_id)] for m in s.elements], [])
                for entry, s in zip(entries, slides)]
    return base_json(Base(
        version=snapshot.VERSION, generation=generation, presentation_id=read.presentation_id,
        revision_id=read.revision_id, source={"pdf": pdf, "sha1": "abc"}, overlays="last", scale=1.0,
        page_size=(720, 405), deck_page_size=read.page_size, master_background=snapshot.master_key(deck, NOWHERE),
        master_readback=read.master_background, slides=tuple(attached), theme=None, pending=None, cleanup=None,
        origin=None, adopt=None))
