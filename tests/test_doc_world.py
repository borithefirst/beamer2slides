"""`doc_world`, the reference applier, on requests the campaign reaches only through a
sync — so each rule Docs has is pinned here on its own, the way the sync will rely on it.
"""

import pytest

from beamer2slides.devtools import doc_world, fuzz_docs


def _world() -> doc_world.World:
    """The `tabs` corpus shape with one range called `b2s:x` on the body and one on the
    second tab: the shape of a key both tabs happen to give a block."""
    world = fuzz_docs.corpus("tabs")
    other = world.tabs[1].id
    world.apply([
        {"createNamedRange": {"name": "b2s:x", "range": {"startIndex": 1, "endIndex": 2}}},
        {"createNamedRange": {"name": "b2s:x",
                              "range": {"startIndex": 1, "endIndex": 2, "tabId": other}}},
    ])
    return world


def _ranges(world: doc_world.World, name: str = "b2s:x") -> list[tuple[str, str]]:
    return [(t.id, r["id"]) for t in world.tabs for r in t.named if r["name"] == name]


def test_a_named_range_on_another_tab_is_deleted_only_where_the_request_says_so():
    """A range id names one range and one tab's, and a `deleteNamedRange` carries no
    Range to say which tab — it takes `tabsCriteria` instead. The reference says an
    omitted one applies to every tab; the live API answers "No named range with ID" for
    a range on a second tab (measured, `doc_merge.on_tab`), and a refusal throws out the
    whole batch — which for a plant batch is every anchor that tab was to be given.
    """
    world = _world()
    (body, first), (other, second) = _ranges(world)
    with pytest.raises(doc_world.Refused):
        world.apply([{"deleteNamedRange": {"namedRangeId": second}}])
    assert _ranges(world) == [(body, first), (other, second)]   # nothing was deleted
    world.apply([{"deleteNamedRange": {"namedRangeId": second,
                                       "tabsCriteria": {"tabIds": [other]}}}])
    assert _ranges(world) == [(body, first)]
    # The first tab's own needs no criteria: a request without one goes there.
    world.apply([{"deleteNamedRange": {"namedRangeId": first}}])
    assert _ranges(world) == []


def test_deleting_a_name_takes_every_tab_unless_told_which():
    world = _world()
    (body, _), (other, _) = _ranges(world)
    world.apply([{"deleteNamedRange": {"name": "b2s:x",
                                       "tabsCriteria": {"tabIds": [other]}}}])
    assert [tab for tab, _ in _ranges(world)] == [body]
    world = _world()
    world.apply([{"deleteNamedRange": {"name": "b2s:x"}}])
    assert _ranges(world) == []


def test_a_delete_of_nothing_is_refused_and_takes_the_batch_with_it():
    """Google refuses the request, and a refused request throws out the whole batch:
    the range created just before it in the same batch is not there afterwards."""
    world = _world()
    before = [list(t.named) for t in world.tabs]
    for bad in ({"namedRangeId": "nr.none"}, {"name": "b2s:nobody"}, {}):
        with pytest.raises(doc_world.Refused):
            world.apply([
                {"createNamedRange": {"name": "b2s:y",
                                      "range": {"startIndex": 1, "endIndex": 2}}},
                {"deleteNamedRange": bad},
            ])
        assert [list(t.named) for t in world.tabs] == before


def test_a_key_planted_again_on_an_empty_paragraph_stays_on_it():
    """Text written *at* a range's first index pushes the range along (Docs' rule, and the
    way an empty paragraph's key slid onto the block the sync wrote at its mark): the
    sync's answer is to delete the range and plant it on the mark again after the write,
    in the same batch. Pinned here so the applier keeps letting that batch through."""
    world = doc_world.World()
    world.apply([{"createNamedRange": {"name": "b2s:paragraph:empty",
                                       "range": {"startIndex": 1, "endIndex": 2}}}])
    [old] = [r["id"] for r in world.tabs[0].named]
    world.apply([
        {"insertText": {"location": {"index": 1}, "text": "\nwritten here"}},
        {"deleteNamedRange": {"namedRangeId": old}},
        {"createNamedRange": {"name": "b2s:paragraph:empty",
                              "range": {"startIndex": 1, "endIndex": 2}}},
    ])
    [kept] = world.tabs[0].named
    assert (kept["start"], kept["end"]) == (1, 2)
    content = world.read()["tabs"][0]["documentTab"]["body"]["content"]
    paragraphs = [c for c in content if "paragraph" in c]
    assert [c["startIndex"] for c in paragraphs] == [1, 2]
    assert paragraphs[0]["paragraph"]["elements"][0]["textRun"]["content"] == "\n"


def test_a_paragraph_split_off_another_has_measurements_of_its_own():
    """The world's own defect, and the reason it was invisible: `dict(para)` is a
    shallow copy, so the paragraph a split makes went on sharing the very dict its
    measures live in. Appending a block writes "\\ntext", which is a split, so one
    `updateParagraphStyle` set the line spacing of every paragraph descended from the
    same ancestor — and the next request to clear a measure cleared it everywhere too,
    which left the document self-consistent and the base agreeing with it. Only a judge
    asking "did the source's restyle arrive *here*?" could see it (offline seed 110149).
    """
    world = doc_world.World()
    world.apply([{"insertText": {"location": {"index": 1}, "text": "one\ntwo"}}])
    world.apply([{"updateParagraphStyle": {
        "range": {"startIndex": 1, "endIndex": 4},
        "paragraphStyle": {"lineSpacing": 150.0}, "fields": "lineSpacing"}}])
    styles = [c["paragraph"]["paragraphStyle"]
              for c in world.read()["tabs"][0]["documentTab"]["body"]["content"]
              if "paragraph" in c]
    assert [s.get("lineSpacing") for s in styles] == [150.0, None]

