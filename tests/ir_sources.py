"""Producers and consumers of deck.json elements for `test_ir_matrix.py`.

A **producer** is a function `(case, home) -> Made`: a deck (the IR every stage passes on) and the
stage it is at. A **consumer** is a function `(Made, home) -> None` that runs the real code a
journey runs on such a deck, offline, and raises when an element crashes it or goes missing on the
way. Consumers work on a deep copy: several of them read one cached producer's deck, and render,
`fit_holes` and `pictured_shapes` write into what they are handed.

Nothing here talks to Google. What only Google could answer is stood in for the way the other
offline tests do: the imported .pptx is read back with python-pptx (its slides are what the Drive
import copies from), a deck's read-back is `slides_sim.simulate`, and an adopted deck's
`presentations.get` is made from the target's own object ids (`pres_of`).
"""

import copy
import json
import os
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
DECKS = TESTS / "decks" / "out"


@dataclass
class Made:
    """What a producer made: `deck` at `stage` ("classified", "rendered", "planned", "read"), the
    folder its files (backgrounds, figures) are relative to, the PDF it came from, the width in
    slide pt of the deck it is written into, and what else a consumer of it needs (`extra`:
    "target", the deck_ir of the deck adopt read; "tex", "workspace", "compiled" for an adopted
    source)."""
    deck: dict
    stage: str
    out: Path | None = None
    pdf: Path | None = None
    page_width: float | None = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- producers

def _convert_pdf(pdf: Path, out: Path) -> tuple[dict, dict, Path]:
    """convert's local half (`__main__.cmd_convert`, `agent.deck_tools._prepare`,
    `sync.build_ours`): notes taken out, extract, the last overlay steps, classify, render."""
    from beamer2slides.classify import classify
    from beamer2slides.extract import extract, select_overlays
    from beamer2slides.notes import prepare
    from beamer2slides.render import render_backgrounds

    out.mkdir(parents=True, exist_ok=True)
    prepared = prepare(pdf, out)
    raw = extract(prepared.pdf, prepared.labels)
    for page in raw["pages"]:
        page["notes"] = prepared.notes.get(page["index"])
    raw = select_overlays(raw, "last")
    deck = classify(raw)
    render_backgrounds(prepared.pdf, raw, deck, out)
    return deck, raw, prepared.pdf


def built_pdf(name: str) -> Path:
    pdf = DECKS / f"{name}.pdf"
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not built (python tests/decks/build.py)")
    return pdf


def converted(case: str, home: Path) -> Made:
    """A beamer PDF of the test decks, classified and rendered as `convert` does."""
    pdf = built_pdf(case)
    deck, raw, used = _convert_pdf(pdf, home / "out")
    return Made(deck, "rendered", home / "out", used, extra={"raw": raw})


def adopted_source(case: str, home: Path, target: dict | None = None) -> dict:
    """A showcase deck (or `target`, any deck_ir read) adopted (`adopt.bootstrap`, fonts none as
    in test_adopt_compiles, nothing downloaded) and its source compiled in an inverse `Workspace`,
    as `adopt_sync.convert_source` and the pull loop compile it. {"target", "tex", "workspace",
    "pdf"}."""
    from beamer2slides import adopt
    from beamer2slides.inverse import Workspace, tex_env

    from .test_adopt_compiles import engine

    if target is None:
        target = showcase_target(case)
    tex = home / "tree" / "main.tex"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("B2S_FONTS", str(home / "no-fonts-here"))
        mp.delenv("B2S_FONT_FETCH", raising=False)
        mp.setenv("B2S_NO_DOWNLOADS", "1")  # (offline: no font or picture fetch whatever the deck names)
        adopt._FAMILIES.clear()
        try:
            text = adopt.bootstrap(target, tex)
        finally:
            adopt._FAMILIES.clear()
    if shutil.which(engine(text), path=tex_env()["PATH"]) is None:
        (pytest.fail if os.environ.get("B2S_REQUIRE_TEX") else pytest.skip)(f"{engine(text)} not found")
    ws = Workspace(tex, home / "work")
    pdf, err = ws.compile()
    assert pdf is not None, err
    # (a copy: the workspace compiles again into its build folder when the pull loop wants notes)
    kept = home / "compiled.pdf"
    shutil.copyfile(pdf, kept)
    return {"target": target, "tex": tex, "workspace": ws, "pdf": kept}


def adopted(case: str, home: Path, source: dict) -> Made:
    """The source adopt wrote for a showcase deck, compiled and converted (classify reads its
    marks: `marked.classify_marked`; render pictures what Slides cannot draw)."""
    deck, raw, used = _convert_pdf(source["pdf"], home / "out")
    target = source["target"]
    return Made(deck, "rendered", home / "out", used, deck_width(target), {**source, "raw": raw})


def deck_width(target: dict) -> float:
    """The width in slide pt of the deck adopt read (its PDF pt times the read's scale)."""
    return target["slides"][0]["size"][0] * target["scale"]


def json_round_trip(made: Made) -> Made:
    """deck.json as the next process reads it: `deck_prepare` writes it and `deck_upload` reads it
    back (agent/deck_tools.py), and a base's `ir` comes back from Drive the same way. Tuples are
    lists and every key a string."""
    deck = json.loads(json.dumps(made.deck, ensure_ascii=False))
    return Made(deck, made.stage, made.out, made.pdf, made.page_width, made.extra)


def folded(made: Made) -> Made:
    """An adopted source's conversion folded against the deck's own boxes, as `adopt_sync.record`
    (via `convert_source`) and a later `sync.build_ours` fold it."""
    from beamer2slides.adopt_sync import deck_folds, fold_slides

    deck = copy.deepcopy(made.deck)
    folds = deck_folds(made.extra["target"])
    fold_slides(deck, folds)
    return Made(deck, made.stage, made.out, made.pdf, made.page_width, {**made.extra, "folds": folds})


def with_fallbacks(made: Made) -> Made:
    """What `emit.emit` builds the deck from again when the API refused elements: every element
    but the pictures refused, each replaced by a crop of its region (`emit.fallback_pictures`)."""
    from beamer2slides.emit import fallback_pictures, merge_blocks

    deck = copy.deepcopy(made.deck)
    deck = {**deck, "slides": [{**s, "elements": merge_blocks(s["elements"])} for s in deck["slides"]]}
    refused = [(s["page"], e["id"]) for s in deck["slides"] for e in s["elements"] if e["kind"] != "image"]
    return Made(fallback_pictures(deck, refused, made.out), made.stage, made.out, made.pdf, made.page_width,
                {**made.extra, "refused": refused})


def candidate(made: Made, home: Path) -> Made:
    """The pull loop's own reading of an adopted source (`inverse.Workspace.build`: classify and
    `keep_visible_shapes`, no render), from the compile the producer already made."""
    ws = made.extra["workspace"]
    cand = ws.build(home / "classify", any(s.get("notes") for s in made.extra["target"]["slides"]),
                    compiled=(made.extra["pdf"], ""))
    assert not isinstance(cand, str), cand
    return Made(cand.deck, "classified", None, cand.pdf, made.page_width, {**made.extra, "candidate": cand})


def target(case: str, home: Path) -> Made:
    """What `deck_ir` read from a deck a person made in Slides (the showcase fixtures)."""
    t = showcase_target(case)
    return Made(t, "read", None, None, deck_width(t), {"target": t})


# ---------------------------------------------------------------- showcase decks and their variants

def blank_tables(t: dict) -> None:
    """Every table's words gone: a grid of (coloured) empty cells, as a person lays one out before
    filling it in (a corpus deck has one)."""
    for s in t["slides"]:
        for e in s["elements"]:
            if e["kind"] == "table":
                e["rows"] = [["" for _ in row] for row in e["rows"]]
                for cell in e["table_cells"]:
                    cell["paragraphs"] = []


def ellipsis_then_letter(t: dict) -> None:
    """The last paragraph of the slide's last text box ends on an ellipsis after its last word,
    and a paragraph of one letter follows it (a corpus deck's labels under a quote)."""
    box = [e for e in t["slides"][0]["elements"] if e["kind"] == "text"][-1]
    last = box["paragraphs"][-1]
    last["runs"][-1]["text"] = last["runs"][-1]["text"].rstrip() + "…"
    letter = copy.deepcopy(last)
    letter["runs"] = [{**last["runs"][-1], "text": "E"}]
    letter["lines"] = last["lines"][-1:]
    box["paragraphs"].append(letter)


def note_opening_on_a_break(t: dict) -> None:
    """Speaker notes whose second paragraph opens on a soft break (Shift+Enter at its start)."""
    t["slides"][0]["notes"] = "Say why first.\n\x0bThen say how."


# case -> (showcase deck, the one slide kept, what is changed on it)
VARIANTS = {
    "hashing-blank_table": ("hashing", 4, blank_tables),
    "hashing-ellipsis": ("hashing", 1, ellipsis_then_letter),
    "hashing-note_break": ("hashing", 1, note_opening_on_a_break),
}


def showcase_of(case: str) -> str:
    return VARIANTS[case][0] if case in VARIANTS else case


def showcase_target(case: str) -> dict:
    """The deck_ir read of a showcase deck (`tests/decks/foreign/showcase`), or of a variant: one
    of its slides with one thing a person's deck can have that no showcase slide has."""
    from .test_adopt_compiles import SHOWCASE, target_of

    name = showcase_of(case)
    if not (SHOWCASE / name / "target.json").exists():
        pytest.skip(f"{name}: no showcase fixture (tools/showcase.py fixture)")
    t = target_of(name)
    if case in VARIANTS:
        _, keep, change = VARIANTS[case]
        t = copy.deepcopy(t)
        t["slides"] = [{**t["slides"][keep], "page": 0}]
        change(t)
    return t


def read_back(made: Made) -> Made:
    """`deck_ir` of the deck convert would make of `made` (`slides_sim.simulate`): what `pull`
    reads from a converted deck nobody edited."""
    from .irs import deck_ir

    from .slides_sim import simulate

    return Made(deck_ir(simulate(copy.deepcopy(made.deck)), made.deck["slides"][0]["size"]), "read",
                None, None, None, {"source": made})


# ---------------------------------------------------------------- what every consumer checks

def ids(deck: dict) -> list[list[str]]:
    return [[e["id"] for e in s["elements"]] for s in deck["slides"]]


def same_elements(before: dict, after: dict, what: str) -> None:
    """No element came or went: `merge_blocks`, `fit_holes` and the folds reshape, they never drop."""
    assert [len(s["elements"]) for s in before["slides"]] == [len(s["elements"]) for s in after["slides"]] \
        and ids(before) == ids(after), f"{what} changed the elements: {ids(before)} -> {ids(after)}"


def merged(deck: dict) -> dict:
    from beamer2slides.emit import merge_blocks

    out = {**deck, "slides": [{**s, "elements": merge_blocks(s["elements"])} for s in deck["slides"]]}
    assert [sorted(i) for i in ids(deck)] == [sorted(i) for i in ids(out)], "merge_blocks dropped an element"
    return out


def written(slide: dict, parts: list, element_ids: list[str], carried: set[str], what: str) -> None:
    """Every element of `slide` has an object: created by its own requests, or carried by what the
    slide was copied from (a picture or a table the .pptx brought, a layout placeholder).
    `parts`: `DeckPlan.slide_parts`' (the first part is the slide's cleanup, then one per element)."""
    from beamer2slides.emit import created_ids

    assert len(element_ids) == len(slide["elements"]) == len(parts[1:1 + len(element_ids)]), \
        f"{what}: {len(slide['elements'])} elements, {len(element_ids)} object ids"
    missing = []
    for el, oid, (part_el, reqs) in zip(slide["elements"], element_ids, parts[1:]):
        assert part_el["id"] == el["id"], f"{what}: parts out of step with the elements"
        made = created_ids(reqs)
        # (a diagram of one object gets no group, Slides grouping two or more: its object is the
        # diagram, `snapshot.attach_readback`)
        if oid not in carried and oid not in made and not (el["kind"] == "diagram" and made):
            missing.append(f"{el['kind']} {el['id']} ({oid})")
        elif not says(el, oid, reqs):
            missing.append(f"{el['kind']} {el['id']} ({oid}) without its words")
    assert not missing, f"{what}: elements with no object: {missing}"


def says(el: dict, oid: str, reqs: list[dict]) -> bool:
    """A text element with words has them inserted into its object (a placeholder the slide was
    copied with exists whether or not anything is written into it)."""
    if el["kind"] != "text" or not any(r["text"].strip() for p in el.get("paragraphs", []) for r in p["runs"]):
        return True
    return any(r.get("insertText", {}).get("objectId") == oid and r["insertText"]["text"].strip() for r in reqs)


# ---------------------------------------------------------------- convert (emit.build_deck, offline)

PLACEHOLDER_TYPES = {"TITLE": "TITLE", "CENTER_TITLE": "CENTERED_TITLE", "SUBTITLE": "SUBTITLE"}


def source_slides(pptx) -> list[dict]:
    """The slides of the .pptx as the Drive import brings them (`presentations.get`'s
    pageElements, as far as `DeckPlan.copy_request` reads them), in the order python-pptx wrote
    them: layout placeholders, pictures, tables, template shapes."""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    def size(shape) -> dict:
        return {"width": {"magnitude": shape.width or 0, "unit": "EMU"},
                "height": {"magnitude": shape.height or 0, "unit": "EMU"}}

    out = []
    for k, slide in enumerate(Presentation(pptx).slides):
        els = []
        for j, shape in enumerate(slide.shapes):
            oid = f"src{k:03}_{j}"
            if shape.is_placeholder:
                kind = shape.placeholder_format.type.name
                els.append({"objectId": oid, "size": size(shape),
                            "shape": {"placeholder": {"type": PLACEHOLDER_TYPES.get(kind, kind)}}})
            elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                els.append({"objectId": oid, "size": size(shape), "image": {}})
            elif getattr(shape, "has_table", False) and shape.has_table:
                els.append({"objectId": oid, "size": size(shape), "table": {}})
            else:
                els.append({"objectId": oid, "size": size(shape), "shape": {}})
        out.append({"objectId": f"src{k:03}", "pageElements": els})
    return out


def convert(made: Made, home: Path) -> None:
    """`emit.build_deck` up to what only Google can answer: the plan, the backgrounds and theme,
    the .pptx with every page's real pictures, tables and template shapes, phase 1's copy of each
    imported slide (against that .pptx read back), the layouts' styles and texts, and phase 2's
    requests for every slide, with the table margins the base records."""
    from beamer2slides import emit

    deck = copy.deepcopy(made.deck)
    out = made.out
    page_w, page_h = deck["slides"][0]["size"]
    plan = emit.DeckPlan(merged(deck), pptx_tables=True)
    same_elements(merged(deck), plan.deck, "DeckPlan")
    deck, scale, fonts = plan.deck, plan.scale, plan.fonts
    bg_key = {s["page"]: emit.background_key(s, out) for s in deck["slides"]}
    bg_file = {bg_key[s["page"]]: out / s["background"] for s in deck["slides"] if not s.get("background_color")}
    counts = Counter(bg_key.values())
    shared = counts.most_common(1)[0][0] if counts and counts.most_common(1)[0][1] >= 2 else None

    def fill(key: tuple) -> dict:
        return {"color": key[1]} if key[0] == "color" else {"picture": bg_file[key]}

    theme = emit.plan_theme(deck, out, bg_key)
    master_fill = fill(shared or ("color", "#ffffff"))
    if theme:
        group = lambda s: "TITLE" if emit.slide_layout(s)[0] == "TITLE" else "*"  # noqa: E731
        if shared is None or all(theme["exact"].get(group(s)) == shared for s in deck["slides"] if bg_key[s["page"]] == shared):
            master_fill = {"color": theme["ground"]}
    pages = [{
        "layout": theme["layouts"][s["page"]] if theme else emit.slide_layout(s)[0],
        "fill": None if bg_key[s["page"]] == shared else fill(bg_key[s["page"]]),
        "pictures": [{"file": out / e["file"], "bbox": bbox, "alt": e.get("alt"),
                      "title": emit.PICTURE_TITLES.get(e.get("role"), "Figure")} for e, bbox in plan.pictures(s)],
        "tables": plan.tables(s),
        "templates": plan.uses_templates[s["page"]],
    } for s in deck["slides"]]
    pictures = {s["page"]: {e["id"] for e, _ in plan.pictures(s)} for s in deck["slides"]}
    for s in deck["slides"]:
        for e in s["elements"]:
            if e["kind"] == "image":
                assert e["id"] in pictures[s["page"]] and (out / e["file"]).is_file(), \
                    f"slide {s['page'] + 1}: picture {e['id']} has no file the .pptx could carry"
    pptx = emit.build_pptx(page_w, page_h, plan.keys, pages, master_fill, theme and theme["decorations"])
    sources = source_slides(pptx)
    assert len(sources) == len(deck["slides"]), "a source slide per deck slide"

    template_sizes: list = []
    page_elements, carried = {}, {}
    for slide, source in zip(deck["slides"], sources):
        request, sizes = plan.copy_request(slide, source)
        template_sizes = template_sizes or sizes
        new = request["duplicateObject"]["objectIds"]
        sid = new[source["objectId"]]
        page_elements[sid] = [{"objectId": new.get(e["objectId"], f"{e['objectId']}_copy"), "size": e["size"]}
                              for e in source["pageElements"]]
        carried[sid] = set(new.values())
    speaker_notes = {sid: f"{sid}_notes" for sid in page_elements}

    ground = emit.master_ground(shared, bg_file, page_w)
    emit.layout_style_spec(deck, scale, fonts, emit.PPTX_TITLE_DY, ground)
    for ti, el in enumerate(deck.get("layout_texts") or []):
        assert emit.text_box_requests(el, "layout", f"{emit.LAYOUT_TEXT_PREFIX}0_{ti}", scale, fonts)

    for slide in deck["slides"]:
        sid = f"b2s_s{slide['page']:03}"
        parts, element_ids = plan.slide_parts(slide, page_elements, speaker_notes, {}, template_sizes)
        written(slide, parts, element_ids, carried[sid], f"slide {slide['page'] + 1}")
        emit.element_objects(parts, element_ids)
        for el in slide["elements"]:
            if el["kind"] == "table":
                emit.pptx_table(el, scale, fonts)["margins"]


# ---------------------------------------------------------------- what sync writes, element by element

def emission(made: Made, home: Path) -> None:
    """`sync.mark_emitted`'s two sides: a slide's emission worked out from that slide alone
    (`sync.emitted_elements` over `emit.slide_emission`), for every slide of the plan a sync makes
    at the deck's own width (`DeckPlan(page_width)`, `sync.build_ours`, `adopt_sync.convert_source`)
    - and for a base's IR, which is exactly such a slide read back from JSON."""
    from beamer2slides.emit import SLIDE_W, DeckPlan
    from beamer2slides.sync import emitted_elements

    deck = copy.deepcopy(made.deck)
    plan = DeckPlan(merged(deck), made.page_width or SLIDE_W)
    same_elements(merged(deck), plan.deck, "DeckPlan")
    for slide in plan.deck["slides"]:
        names = [f"e{i}" for i in range(len(slide["elements"]))]
        got = emitted_elements(slide, names, plan.scale, plan.fonts)
        assert len(got) == len(slide["elements"]), f"slide {slide['page'] + 1}: an element emitted nothing"
        again = json.loads(json.dumps(slide))
        assert emitted_elements(again, names, plan.scale, plan.fonts) == got, \
            f"slide {slide['page'] + 1}: its IR read back from a base emits differently"


# ---------------------------------------------------------------- the base convert records

def convert_state(deck: dict) -> tuple[dict, dict]:
    """(plan_offline, emit.json's state) for a deck, as `build_deck` records it (element objects,
    groups, the .pptx tables' margins)."""
    from beamer2slides.emit import element_objects, plan_offline, pptx_table

    off = plan_offline(deck)
    plan = off["plan"]
    state = {"presentationId": "simulated", "scale": plan.scale, "slides": []}
    for slide, (sid, page, parts, element_ids) in zip(plan.deck["slides"], off["slides"]):
        written(slide, parts, element_ids, set(off["copies"][len(state["slides"])]["duplicateObject"]["objectIds"].values()),
                f"slide {page + 1}")
        objects, groups = element_objects(parts, element_ids)
        state["slides"].append({"page": page, "objectId": sid, "elements": element_ids, "objects": objects,
                                "groups": groups,
                                "table_margins": {str(i): pptx_table(el, plan.scale, plan.fonts)["margins"]
                                                  for i, el in enumerate(slide["elements"]) if el["kind"] == "table"}})
    return off, state


def base_of(made: Made) -> tuple[dict, dict]:
    """The base `convert` records (`snapshot.build_base`), over the simulated read-back, as a
    later sync loads it (JSON); and that read-back."""
    from beamer2slides import snapshot

    from .slides_sim import simulate

    deck = copy.deepcopy(made.deck)
    off, state = convert_state(deck)
    pres = simulate(copy.deepcopy(deck))
    base = snapshot.build_base(off["plan"].deck, made.out, pres, state, made.pdf, 0, False, "last", None)
    for entry, slide in zip(base["slides"], off["plan"].deck["slides"]):
        assert [e["id"] for e in entry["elements"]] == [e["id"] for e in slide["elements"]], \
            f"slide {slide['page'] + 1}: the base lost an element"
        assert len({e["key"] for e in entry["elements"]}) == len(entry["elements"]), \
            f"slide {slide['page'] + 1}: two elements share a key"
    return json.loads(json.dumps(base, ensure_ascii=False)), pres


def convert_base(made: Made, home: Path) -> None:
    base_of(made)


def pres_of(target: dict) -> dict:
    """A `presentations.get` of the deck adopt read, as far as `adopt_sync.build_base` reads it:
    its slides and the objects on them, by the ids the read gave (no test holds a recorded one)."""
    scale = target["scale"]

    def emu(v: float) -> dict:
        return {"magnitude": v * scale * 12700, "unit": "EMU"}

    slides = []
    for s in target["slides"]:
        seen, els = set(), []
        for e in s["elements"]:
            oid = e.get("object")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            x0, y0, x1, y1 = e["bbox"]
            els.append({"objectId": oid, "size": {"width": emu(x1 - x0), "height": emu(y1 - y0)},
                        "transform": {"scaleX": 1, "scaleY": 1, "translateX": x0 * scale * 12700,
                                      "translateY": y0 * scale * 12700, "unit": "EMU"},
                        "shape": {"shapeType": "TEXT_BOX"}})
        slides.append({"objectId": s["objectId"], "pageElements": els})
    w, h = target["slides"][0]["size"]
    return {"presentationId": "adopted", "revisionId": "r1",
            "pageSize": {"width": emu(w), "height": emu(h)}, "slides": slides, "layouts": [], "masters": []}


def adopt_base_of(made: Made) -> tuple[dict, dict]:
    """`adopt_sync.record`'s second half on a folded conversion: the plan at the deck's width,
    the labels checked, the conversion paired with the deck's objects (`pair_elements`,
    `explained_by_layout`, `inside_tables`, `drawn_from`) and the base recorded."""
    from beamer2slides import adopt_sync
    from beamer2slides.emit import DeckPlan

    target = made.extra["target"]
    deck = copy.deepcopy(made.deck)
    plan = DeckPlan(merged(deck), made.page_width)
    assert adopt_sync.labels_match(plan.deck, target) is None, adopt_sync.labels_match(plan.deck, target)
    pres = pres_of(target)
    base = adopt_sync.build_base(plan.deck, made.out, target, pres, made.pdf, folds=made.extra["folds"])
    for entry, slide in zip(base["slides"], plan.deck["slides"]):
        assert [e["id"] for e in entry["elements"]] == [e["id"] for e in slide["elements"]], \
            f"slide {slide['page'] + 1}: the base lost an element"
    return json.loads(json.dumps(base, ensure_ascii=False)), pres


def adopt_base(made: Made, home: Path) -> None:
    adopt_base_of(made)


# ---------------------------------------------------------------- sync (build_ours and the writes)

def resync(made: Made, home: Path) -> None:
    """A sync of the same PDF into the deck the base describes: `sync.build_ours` (which folds
    against an adopted base's boxes, plans at the deck's width, keys and records the new side),
    `mark_emitted` on every paired slide (fast off: both emissions are worked out), the merge
    against the deck as the base read it (`merge.plan_merge`), theme sync's fresh side, and
    `Sync.slide_requests` recreating every element of every slide - a title into its
    placeholder, a table refilled in place as `table_refill` does - with the staging .pptx that
    brings the pictures."""
    from beamer2slides import merge, snapshot, sync, theme_sync
    from beamer2slides.emit import SLIDE_W, build_pptx, title_element

    base, pres = adopt_base_of(made) if made.extra.get("folds") is not None else base_of(made)
    # (the width went in as `overlays` while build_ours had defaults: planned at SLIDE_W whatever the deck)
    pictures = snapshot.find_base_pictures(base, snapshot.PictureFolders(kept=(made.out,), rendered=None, held=None)) \
        if made.out else snapshot.NO_PICTURES
    ours = sync.build_ours(made.pdf, home / "ours", base, "last", made.page_width or SLIDE_W, pictures)
    # a base this converter just wrote is in today's form: nothing to rewrite, nothing it cannot read
    assert snapshot.base_form_json(ours["base_forms"]) == []
    for s, entry in zip(ours["deck"]["slides"], ours["slides"]):
        assert [e["id"] for e in entry["elements"]] == [e["id"] for e in s["elements"]]
    sync.mark_emitted(base, ours["slides"], ours["deck"], ours["pairs"], ours["plan"].scale, ours["plan"].fonts,
                      fast=False, unread=[])
    merge.plan_merge(base, ours, snapshot.read_presentation(pres))
    theme_sync.ours_side(theme_sync.theme_ours(ours))

    s = sync.Sync(None, None, "offline", base, ours, home / "ours", dry_run=True, measure=False,
                  trust_generation=True, check_plan=None, follow_labels=False, take_source=(), facts=None,
                  way_back=None)
    pictures = {}
    for j, slide in enumerate(ours["deck"]["slides"]):
        title = title_element(slide)
        in_place = {}
        if title is not None:  # (a placeholder holding words, emptied first)
            in_place[title] = sync.InPlace(id=f"live{j}_title", size=(sync.STAND_IN, sync.STAND_IN), text="x")
        for i, el in enumerate(slide["elements"]):
            if el["kind"] == "table":
                in_place[i] = sync.TableFill(id=f"live{j}_tab{i}", cells=[], steps=[], shift=(0.0, 0.0), margins=[])
            if el["kind"] == "image":
                pictures[str(ours["out"] / el["file"])] = "picture"
        if slide.get("background") and not slide.get("background_color"):
            pictures[str(ours["out"] / slide["background"])] = "background"
        units = list(range(len(slide["elements"])))
        reqs, objects, new_oid, _ = s.slide_requests(j, units, f"live{j}", in_place, {}, {}, True, frozenset())
        created = {r[k]["objectId"] for r in reqs for k in ("createShape", "createLine", "createTable", "createImage")
                   if k in r} | {v for r in reqs for v in r.get("duplicateObject", {}).get("objectIds", {}).values()} | \
            {r["groupObjects"]["groupObjectId"] for r in reqs if "groupObjects" in r}
        lost = [f"{el['kind']} {el['id']}" for i, el in enumerate(slide["elements"])
                if i not in objects or (new_oid[i] not in created and i not in in_place
                                        and not (el["kind"] == "diagram" and created & set(objects[i])))
                or not says(el, new_oid[i], reqs)]
        assert not lost, f"slide {slide['page'] + 1}: sync would write nothing for {lost}"
    if pictures:
        page_w, page_h = ours["deck"]["slides"][0]["size"]
        still = [f for f, what in pictures.items() if what != "background"]
        pages = [{"layout": "BLANK", "fill": None, "templates": False, "pictures": [
            {"file": f, "bbox": [0, 0, *s._fit(f)], "alt": f"b2s-stage:{k + n}", "title": "stage"}
            for n, f in enumerate(still[k:k + 40])]} for k in range(0, len(still), 40)]
        pages += [{"layout": "BLANK", "fill": {"picture": Path(f)}, "templates": False, "pictures": []}
                  for f, what in pictures.items() if what == "background"]
        assert len(source_slides(build_pptx(page_w, page_h, [], pages, {"color": "#ffffff"}))) == len(pages)


# ---------------------------------------------------------------- pull and adopt (deck_ir's side)

def compare_read_back(made: Made, home: Path) -> None:
    """`compare` of a converted deck with what `deck_ir` reads from it (the pull of a converted,
    unedited deck): it runs, and every element of the source is compared."""
    from beamer2slides.compare import TOL, compare, without_keys
    from beamer2slides.inverse import typed_target

    source = made.extra["source"]
    comp = compare(without_keys(copy.deepcopy(source.deck)), typed_target(copy.deepcopy(made.deck)), TOL, {})
    comp.open()
    comp.summary()


def round0(made: Made, home: Path) -> None:
    """The pull loop's first round on an adopted source (`inverse.converge`, `adopt_replay`):
    picture hashes, `compare` with the deck adopt read, the frame guard's look (no thumbnails:
    residuals and words) and the planner's edits."""
    from beamer2slides.compare import TOL, compare
    from beamer2slides.frame_guard import FrameGuard
    from beamer2slides.inverse import Context, Planner, class_pt_option, picture_hashes, typed_target

    cand, target, ws = made.extra["candidate"], made.extra["target"], made.extra["workspace"]
    typed = typed_target(target)
    hashes = picture_hashes(cand, typed, home / "work")
    comp = compare(cand.current(), typed, TOL, hashes)
    comp.summary()
    FrameGuard(typed, None, lambda *a: None).observe(0, cand, comp)
    Planner(cand, comp, target, Context(pt_option=class_pt_option(ws.source)), ws, set(), {}, hashes).plan()


def adopt_read(made: Made, home: Path) -> None:
    """What `adopt._adopt` asks of the deck it read besides the bootstrap (which every adopted
    producer runs): the pictures it has to leave out or crop, and the fold records a base keeps."""
    from beamer2slides import adopt
    from beamer2slides.adopt_sync import deck_folds

    t = copy.deepcopy(made.deck)
    adopt.pictures_missing(t)
    adopt.pictures_from_thumbnail(t)
    folds = deck_folds(t)
    assert len(folds) == len([s for s in t["slides"]]), "a fold record per slide label"
