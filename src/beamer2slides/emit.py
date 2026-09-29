"""Stage 4: build the Google Slides deck from deck.json and the background images.

This module is the stage's face: `emit` and `build_deck`, the rebuild guard's preflight, and
`DeckPlan` / `plan_offline` / `slide_emission`, which say what a deck or one slide is written as.
The rest lives by topic: `emit_metrics` (Slides' measures, FontMapper, bullets), `emit_widths`
(measured advances and line breaks), `emit_text` (text boxes), `emit_pptx` (the .pptx upload,
shapes), `emit_tables`, `emit_diagrams`, `emit_holes` and `emit_places` (pictures placed by
prediction, then by measurement) and `emit_theme` (the presentation, master and layouts). Names
callers have always taken from here still are.
"""

import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, TypeVar

from .emit_diagrams import block_groups, diagram_requests, element_template_keys, rule_groups
from .emit_diagrams import (  # noqa: F401 (callers take these from here)
    bend_template_key, connection, label_inside, node_template_key,
)
from .emit_holes import fit_holes, formula_shifts, overlay_boxes, text_right_limit
from .emit_holes import (  # noqa: F401 (callers take these from here)
    OVERLAY_STRETCH, OVERLAY_STRETCH_MEASURED, fit_overlay, hole_offset, slide_holes, space_shift,
)
from .emit_metrics import PPTX_TITLE_DY, SLIDE_W, FontMapper
from .emit_metrics import (  # noqa: F401 (callers take these from here)
    ADVANCES, ASCENT_EM, BASELINE_A, BULLET_GAP, BULLET_SHAPES, CALIBRATION, CALIBRATION_DIR, CM_ADVANCES,
    CM_FACE, DESCENT_EM, DESIGN_WIDTH, DIGITS, DRAWN_BOLD_WEIGHT, FONT_FOR_FAMILY, LINE_EM,
    MIDDLE_BASELINE_EM, NUMBER_CHARS, OPTICAL_WEIGHT, OPTICAL_WEIGHTS_READ, OPTICAL_WIDTH_MAX, PAD_X, PX_PT,
    ROBOTO_MONO_ADVANCE_EM, SHAPE_REFERENCE, SHAPE_TOL, SMALL_CAPS_WIDTH, SOFT_BREAK, STYLE_KEY,
    SYMBOL_ADVANCE_EM, advance_widths, bullet_extent, bullet_level, bullet_preset, bullet_shape, bullet_size,
    cm_face, design_width, rgb, u16, xml_text,
)
from .emit_places import grown_panels, measure_jobs, measure_places, title_bar_under
from .emit_places import (  # noqa: F401 (callers take these from here)
    find_marks, ink_end, mark_alpha, overlay_move, pick_gap, slides_texts,
)
from .emit_pptx import TEMPLATE_LAYOUTS, api_error, batch, build_pptx, shape_requests, template_key
from .emit_pptx import _add_table, _add_template_shapes  # (DeckPlan.contain tries what the .pptx carries)
from .emit_pptx import NO_TABLE_STYLE, NS_A, VARIANT  # noqa: F401 (callers take these from here)
from .emit_tables import pptx_table, table_requests
from .emit_tables import (  # noqa: F401 (callers take these from here)
    TABLE_CELL_PAD, TABLE_MARGIN, TABLE_MIN_SHRINK, TABLE_TEXT_TOP, fit_columns, squeezed_columns,
    table_columns, table_fits, table_layout,
)
from .emit_text import merge_blocks, number_box_requests, text_box_requests
from .emit_text import (  # noqa: F401 (callers take these from here)
    HOLE_BREAK, HOLE_FONT, HOLE_SPACE_EM, LINE_MARGIN, extra_above, extra_below, hole_run, hole_runs,
    in_sentence, inner_pitch, line_pitch, line_size, line_sizes, pitch_between, run_sizes, snap,
    vertical_layout,
)
from .emit_theme import (
    LAYOUT_PLACEHOLDERS, background_key, import_presentation, master_ground, plan_theme, slide_layout,
    subtitle_element, title_element, write_layouts,
)
from .emit_theme import (  # noqa: F401 (callers take these from here)
    LAYOUT_TEXT_PREFIX, PPTX_MIME, layout_placeholder_requests, layout_style_spec, master_plan,
    style_layout_placeholders,
)
from .emit_widths import (  # noqa: F401 (callers take these from here)
    SCRIPT_SIZE, SMALL_CAPS_SIZE, WRAP_MARGIN, ZWSP, first_break, pdf_line_breaks, pdf_width, slides_lines,
    slides_width, wrap_joins, wrap_window, wrapped_width,
)
from .fonts import font_info  # noqa: F401 (callers take these from here)
from .gapi import HttpError
from .google_auth import credentials_for_threads, drive_service, shared_service, slides_service
from .gslides import EMU_PER_PT, emu, execute, per_thread

BATCH_MAX_REQUESTS = 400  # slides are sent together until a batch reaches this size
# A round trip to Google costs about a second whatever it carries, so the wall clock of a
# conversion is round trips and not work (measured: tools/probe_batch_parallelism.py). Several
# batches may be in flight on one presentation at once - Google takes them and loses nothing -
# and four is where the curve flattens: 8 batches of 200 requests take 9.4 s one at a time,
# 6.1 s two at a time, 3.6 s four at a time and 3.1 s eight at a time.
CONTENT_WORKERS = 4
PICTURE_TITLES = {"math": "Formula", "icon": "Icon", "fallback": "Picture"}
# An element emit trips over while planning it is the picture of its region, with a warning, instead
# of the whole conversion failing (`DeckPlan.contain`). Set (to anything but "0"), the failure is
# raised: the offline suite runs so (tests/conftest.py), where a contained failure would hide the bug.
STRICT_ENV = "B2S_EMIT_STRICT"
OBJECT_PREFIX = {"shape": "s", "table": "tab", "diagram": "dg", "image": "f"}  # (text: "t")
_T = TypeVar("_T")


def strict() -> bool:
    """Whether a failure planning one element is raised rather than contained (STRICT_ENV)."""
    return os.environ.get(STRICT_ENV, "") not in ("", "0")


# ---------------------------------------------------------------- main entry

def existing_presentation(drive, out: Path) -> str | None:
    """The deck from a previous run of this output folder, if it still exists (not trashed)."""
    from .guard import previous_deck
    previous = previous_deck(drive, out)
    return previous["presentationId"] if previous and previous["state"] == "live" else None


def size_pt(element: dict) -> tuple[float, float]:
    size = element["size"]
    return tuple(size[k]["magnitude"] / (EMU_PER_PT if size[k]["unit"] == "EMU" else 1) for k in ("width", "height"))


def fallback_element(el: dict) -> dict:
    """What an element becomes when Slides refused it (`fallback_pictures`) or emit could not plan
    it (`DeckPlan.contain`): the picture of its region, 2 pt round its box, which `crop_fallbacks`
    cuts out of the PDF page."""
    x0, y0, x1, y1 = el["bbox"]
    return {"kind": "image", "id": el["id"], "role": "fallback", "bbox": [x0 - 2, y0 - 2, x1 + 2, y1 + 2],
            "file": f"figures/fallback-{el['id']}.png"}


def crop_fallbacks(deck: dict, wanted: list[tuple[int, str]], out: Path, why: str) -> None:
    """Crop the picture of every fallback element ((PDF page, element id) in `wanted`) out of the
    PDF the deck was built from, into `out`. `why`: what made them pictures, for the error when
    that PDF is not there."""
    from .render import crop_region

    if not wanted:
        return
    source_pdf = out / "slides.pdf" if (out / "slides.pdf").exists() else Path(deck["source"]["pdf"])
    if not source_pdf.exists():
        # The one step of a conversion that needs the PDF itself rather than what was classified
        # out of it, and the only reason `agent.deck_tools.deck_upload` asks for one at all. A
        # folder that travelled without its source says so here rather than inside `crop_region`.
        raise FileNotFoundError(
            f"{why} and the region of each has to be cropped from the page, but the PDF this deck "
            f"was built from is not at {source_pdf}. Put it back beside the folder (or pass it in) "
            f"and build the deck again.")
    regions = set(wanted)
    for slide in deck["slides"]:
        for el in slide["elements"]:
            if (slide["page"], el["id"]) in regions and el.get("role") == "fallback":
                (out / el["file"]).parent.mkdir(parents=True, exist_ok=True)
                crop_region(source_pdf, slide["page"], el["bbox"], out / el["file"], 6.0)


def fallback_pictures(deck: dict, refused: list[tuple[int, str]], out: Path) -> dict:
    """The deck with every element the API refused ((PDF page, element id)) replaced by a
    picture of its region, cropped from the PDF the deck was built from."""
    new_slides = []
    for slide in deck["slides"]:
        ids = {eid for page, eid in refused if page == slide["page"]}
        elements = []
        for el in slide["elements"]:
            if el["id"] in ids and el["kind"] != "image":
                ids.discard(el["id"])
                el = fallback_element(el)
            elements.append(el)
        new_slides.append({**slide, "elements": elements})
    new = {**deck, "slides": new_slides}
    crop_fallbacks(new, refused, out, f"the API refused {len(refused)} element(s)")
    return new


def preflight_rebuild(out: Path, source_pdf: Path | None, new_deck: bool = False, force_rebuild: bool = False,
                      slides=None, drive=None) -> dict | None:
    """The guard's question (guard.check_rebuild) before the conversion work starts, so a refusal
    comes in a second instead of after extract, classify and render. `emit` asks again - and backs
    the deck up - immediately before the write, in case the deck is edited in between.

    Returns what it found ({"presentationId", "found"}), which that second ask confirms with one
    field of one read instead of asking the whole question again (`plan_rebuild`'s `checked`);
    None where there was nothing to ask."""
    from . import guard

    if new_deck or force_rebuild or not (out / "emit.json").exists():
        return None
    drive = drive or drive_service()
    previous = guard.previous_deck(drive, out)
    if not previous or previous["state"] != "live":
        return None
    pid = previous["presentationId"]
    return {"presentationId": pid,
            "found": guard.check_rebuild(slides or slides_service(), drive, pid, out, source_pdf, False)}


def preflight_in_background(out: Path, source_pdf: Path | None, new_deck: bool = False,
                            force_rebuild: bool = False):
    """`preflight_rebuild` on a thread of its own. Returns the function that asks for its answer:
    it raises whatever the check raised, and gives back what it found (`emit`'s `checked`).

    The check is three Drive reads and a whole `presentations.get` - three seconds that need
    nothing the conversion produces and answer a question only the first write to Drive really
    asks - so it is made while the PDF is being converted and collected just before `emit`. The
    price is that a refusal now comes after the local conversion instead of in a second, and what
    that writes is the output folder's own files: the deck itself is still never touched.
    """
    if new_deck or force_rebuild or not (out / "emit.json").exists() \
            or shared_service("slides", "v1") or shared_service("drive", "v3"):
        # Nothing to ask, or a caller's own clients, which are that caller's one thread's.
        found = preflight_rebuild(out, source_pdf, new_deck, force_rebuild)
        return lambda: found
    creds = credentials_for_threads()  # here: a worker thread inherits no context (google_auth)
    pool = ThreadPoolExecutor(1, thread_name_prefix="b2s-preflight")
    work = pool.submit(lambda: preflight_rebuild(out, source_pdf, new_deck, force_rebuild,
                                                 slides_service(creds), drive_service(creds)))
    pool.shutdown(wait=False)

    def answer() -> dict | None:
        return work.result()
    return answer


def look_again(slides, drive, out: Path, checked: dict | None) -> tuple[dict | None, dict | None]:
    """What the output folder points at, and the preflight's finding where it still stands.

    Two reads that need nothing of each other - the deck's place in Drive and its revision - so
    they are made at once, one client per thread. `guard.recheck` is the cheap half of the second
    ask; a deck that has moved since the preflight (or one the folder no longer points at, or one
    now in the trash) gets the whole question again, in `plan_rebuild`."""
    from . import guard

    pid = (checked or {}).get("presentationId")
    if not pid or shared_service("slides", "v1"):
        return guard.previous_deck(drive, out), None
    creds = credentials_for_threads()  # here: a worker thread inherits no context (google_auth)
    with ThreadPoolExecutor(1, thread_name_prefix="b2s-recheck") as pool:
        again = pool.submit(lambda: guard.recheck(slides_service(creds), pid, checked["found"]))
        previous = guard.previous_deck(drive, out)
        found = again.result()
    if previous and previous["presentationId"] == pid and previous["state"] == "live":
        return previous, found
    return previous, None


def plan_rebuild(slides, drive, out: Path, new_deck: bool, force_rebuild: bool, backup: str,
                 source_pdf: Path | None, checked: dict | None = None) -> tuple[str | None, dict | None]:
    """Decide what happens to the deck this output folder already has: rebuild it in place (the id
    is returned), or leave it alone and make a new one. Nothing destructive happens before this:
    `guard.check_rebuild` raises `guard.RebuildRefused` when the deck was edited in Slides, and a
    forced rebuild keeps a backup and records the deck's revision first
    (`<out>/backups/backups.json`, printed too) - and is refused in turn when that backup could
    not be kept (`guard.demand_way_back`), because then nothing could bring the deck back. The
    second value goes into emit.json as "previous".

    `checked`: what the preflight found a few seconds ago (`preflight_rebuild`). The question is
    asked again here because the deck may have been edited in between - but only what could have
    changed since is really in question, so where the deck is still at the revision the preflight
    read, that finding stands and the deck and the base are not read again."""
    from . import guard

    previous, found = look_again(slides, drive, out, None if new_deck or force_rebuild else checked)
    if previous is None:
        return None, None
    pid, url = previous["presentationId"], guard.deck_url(previous["presentationId"])
    if previous["state"] != "live":
        where = {"trashed": "is in the Drive trash", "gone": "is gone (deleted, or not this app's file any more)",
                 "other": "is not a presentation any more"}[previous["state"]]
        print(f"the deck of the previous run ({pid}) {where}: making a new one, that deck is left as it is")
        return None, {"presentationId": pid, "state": previous["state"], "action": "new deck", "url": url}
    if new_deck:
        print(f"--new-deck: the previous deck is left as it is at {url}\n"
              f"  (this folder tracks the new deck from now on; the old one is only reachable by that link)")
        return None, {"presentationId": pid, "state": "kept", "action": "new deck", "url": url}
    found = found or guard.check_rebuild(slides, drive, pid, out, source_pdf, force_rebuild)
    mode = backup if backup != "auto" else ("file" if found["reason"] else "none")
    entry = {"presentationId": pid, "url": url, "action": "rebuilt in place", "revisionId": found.get("revisionId"),
             "modifiedTime": previous.get("modifiedTime"), "out": str(out),  # Drive's clock, and where to restore from
             "checked": found.get("checked"), "reason": found.get("reason") or "no deck edits",
             "summary": guard.summary_line(found) if found.get("edited") else "no deck edits",
             "examples": found.get("examples", []), "base_from": found.get("base_from")}
    if found["reason"]:
        print(f"WARNING: rebuilding a deck that {'was edited in Slides' if found['reason'] == 'edited' else found['reason']} "
              f"(--force-rebuild): {entry['summary']}")
    entry["backup"] = guard.backup_deck(drive, pid, out, mode, entry["reason"], slides=slides)
    guard.record(out, entry)  # the attempt belongs in the log even when it failed, and what follows
    if found["reason"]:
        guard.demand_way_back(pid, out, source_pdf, entry, mode)  # no backup, no forced rebuild
    print(f"updating existing deck {pid} (revision {found.get('revisionId')})")
    for line in guard.restore_hint(entry) if found["reason"] else []:
        print(line)
    return pid, entry


def emit(deck: dict, out: Path, title: str, new_deck: bool = False, measure: bool = True,
         force_rebuild: bool = False, backup: str = "auto", source_pdf: Path | None = None,
         checked: dict | None = None) -> dict:
    """Build the deck. An output folder that already has a deck is rebuilt in place unless
    `new_deck`; that replaces the deck's whole content, so `guard.check_rebuild` refuses when
    the deck was edited in Slides (`force_rebuild` goes ahead, after a backup). `checked`: what
    the preflight found (`preflight_rebuild`), which saves the second ask a read of the deck."""
    slides, drive = slides_service(), drive_service()
    # (blocks are merged by the plan, `DeckPlan.contain`, where a block it trips over is a picture)
    existing, previous_entry = plan_rebuild(slides, drive, out, new_deck, force_rebuild, backup, source_pdf, checked)
    state, refused = build_deck(slides, drive, deck, out, title, existing, measure)
    if refused:
        # A picture can only come with the imported .pptx (the API inserts images from public
        # URLs only), so the deck is built once more with the refused elements as pictures.
        print(f"rebuilding the deck with {len(refused)} refused element(s) as pictures")
        # The refused ids are the built deck's (blocks merged, and what emit could not plan made
        # pictures already): the rebuild starts from that deck, so nothing is contained or said twice.
        merged, contained = DeckPlan(deck, pptx_tables=True, contain=True).merged, state.get("contained")
        state, again = build_deck(slides, drive, fallback_pictures(merged, refused, out), out, title,
                                  state["presentationId"], measure)
        if contained:
            state["contained"] = contained
        for page, eid in again:
            print(f"warning: slide {page + 1}: {eid} was refused again and is missing")
    if previous_entry:
        state["previous"] = previous_entry  # what this run replaced, and how to get it back
    (out / "emit.json").write_text(json.dumps({k: v for k, v in state.items() if k != "deck"}, indent=1), encoding="utf-8")
    return state


def upload_plan(deck: dict, out: Path) -> "DeckPlan":
    """build_deck's plan of `deck` (as classify wrote it). What the plan could not make of an
    element (a field its producer never wrote: `DeckPlan.contain`) is the picture of its region, as
    a refused element's is: each is said in a warning, listed in emit.json ("contained"), and its
    picture cut out of the page here, before the .pptx is built."""
    plan = DeckPlan(deck, pptx_tables=True, contain=True)
    for c in plan.contained:
        print(f"warning: slide {c['page'] + 1}: {c['kind']} {c['id']} could not be planned ({c['error']}); "
              f"using a picture of it instead")
    crop_fallbacks(plan.deck, [(c["page"], c["id"]) for c in plan.contained], out,
                   f"emit could not plan {len(plan.contained)} element(s)")
    return plan


def build_deck(slides, drive, deck: dict, out: Path, title: str, existing: str | None,
               measure: bool = True) -> tuple[dict, list[tuple[int, str]]]:
    """Import the .pptx and fill in the content. Returns the state for emit.json and the
    elements the API refused ((PDF page, element id)). `measure`: hole and overlay pictures go
    where a thumbnail shows their gaps and words (measure_places), not only where they are predicted."""
    page_w, page_h = deck["slides"][0]["size"]
    plan = upload_plan(deck, out)
    deck, scale, fonts = plan.deck, plan.scale, plan.fonts

    # Backgrounds: the most common one becomes the master's (the deck's theme): layouts and
    # slides inherit it, and slides added later too. Identical pictures are stored once.
    bg_key = {s["page"]: background_key(s, out) for s in deck["slides"]}
    bg_file = {bg_key[s["page"]]: out / s["background"] for s in deck["slides"] if not s.get("background_color")}
    counts = Counter(bg_key.values())
    shared = counts.most_common(1)[0][0] if counts and counts.most_common(1)[0][1] >= 2 else None

    def fill(key: tuple) -> dict:
        return {"color": key[1]} if key[0] == "color" else {"picture": bg_file[key]}

    theme = plan_theme(deck, out, bg_key)
    master_fill = fill(shared or ("color", "#ffffff"))
    if theme:
        group = lambda s: "TITLE" if slide_layout(s)[0] == "TITLE" else "*"
        # The master's ground colour where the shared background is nothing but ground and decoration.
        if shared is None or all(theme["exact"].get(group(s)) == shared for s in deck["slides"] if bg_key[s["page"]] == shared):
            master_fill = {"color": theme["ground"]}
    pages = [{
        "layout": theme["layouts"][s["page"]] if theme else slide_layout(s)[0],
        "fill": None if bg_key[s["page"]] == shared else fill(bg_key[s["page"]]),
        "pictures": [{"file": out / e["file"], "bbox": bbox, "alt": e.get("alt"),
                      "title": PICTURE_TITLES.get(e.get("role"), "Figure")} for e, bbox in plan.pictures(s)],
        "tables": plan.tables(s),
        "templates": plan.uses_templates[s["page"]],
    } for s in deck["slides"]]
    pptx = build_pptx(page_w, page_h, plan.keys, pages, master_fill, theme and theme["decorations"])
    pres = import_presentation(slides, drive, title, page_w, page_h, pptx, existing)
    pid = pres["presentationId"]
    sources = pres.get("slides", [])
    if len(sources) != len(deck["slides"]):
        raise RuntimeError(f"the import brought {len(sources)} slides, expected {len(deck['slides'])}")

    # Phase 1: every source slide is copied under our object IDs (slide, title and subtitle
    # placeholders, pictures, template shapes); the sources are deleted at the end.
    template_sizes: list[tuple[float, float]] = []
    reqs = []
    for slide, source in zip(deck["slides"], sources):
        request, sizes = plan.copy_request(slide, source)
        template_sizes = template_sizes or sizes
        reqs.append(request)
    batch(slides, pid, reqs)

    # A round trip to Google costs about a second whatever it carries, so what a conversion
    # spends is round trips and not work, and two of them may be in the air at once. The layouts
    # and the master are nobody else's business - neither pass reads the slides being filled in
    # beside them - so they go on a thread of their own, and the content batches below go out
    # CONTENT_WORKERS at a time, each thread with its own client. A caller that handed one ready
    # Slides client over (google_auth.use_services with a mapping) keeps the old serial order:
    # that client is its own, and a service object is not thread-safe.
    threaded = not shared_service("slides", "v1")
    creds = credentials_for_threads() if threaded else None  # here: a worker inherits no context
    client = per_thread(lambda: slides_service(creds)) if threaded else (lambda: slides)
    pool = ThreadPoolExecutor(CONTENT_WORKERS, thread_name_prefix="b2s-content") if threaded else None
    ground = master_ground(shared, bg_file, page_w)
    layout_pool, layout_work = None, None
    if threaded:
        layout_pool = ThreadPoolExecutor(1, thread_name_prefix="b2s-layout")
        layout_work = layout_pool.submit(write_layouts, client, pid, deck, scale, fonts, ground)
    else:
        write_layouts(client, pid, deck, scale, fonts, ground)

    state = {"presentationId": pid, "url": f"https://docs.google.com/presentation/d/{pid}/edit",
             "scale": scale, "slides": []}
    if plan.contained:  # (emit.json: which elements are pictures because emit could not plan them)
        state["contained"] = plan.contained
    if theme:
        state["theme"] = {"ground": theme["ground"], "master": master_fill.get("color"),
                          "decorations": {k: str(p.relative_to(out)).replace("\\", "/") if p else None
                                          for k, p in theme["decorations"].items()},
                          "layouts": {str(k): v for k, v in theme["layouts"].items()}}
    # Placeholder sizes (needed to resize them) and any extra layout placeholders.
    created = execute(slides.presentations().get(
        presentationId=pid,
        fields="slides(objectId,pageElements(objectId,size),slideProperties/notesPage/notesProperties)"))
    page_elements = {s["objectId"]: s.get("pageElements", []) for s in created["slides"]}
    speaker_notes = {s["objectId"]: s.get("slideProperties", {}).get("notesPage", {})
                     .get("notesProperties", {}).get("speakerNotesObjectId") for s in created["slides"]}
    moves, scratch = measure_places(slides, pid, deck, scale, fonts, plan.placed, plan.page_slide, out) \
        if measure else ({}, [])

    # Phase 2: content, batched over slides. Each slide's requests come in parts (one per
    # element) so that a rejected batch can be narrowed down to the element at fault.
    refused: list[tuple[int, str]] = []

    def send(items: list[tuple[str, int, list[tuple[dict | None, list[dict]]]]]) -> None:
        reqs = [r for _, _, parts in items for _, rs in parts for r in rs]
        if not reqs:
            return
        try:
            batch(client(), pid, reqs)
            return
        except HttpError as e:
            if len(items) > 1:
                for item in items:
                    send([item])
                return
            print(f"warning: {items[0][0]}: batch rejected ({api_error(e)}); retrying element by element")
        slide_id, page, parts = items[0]
        for el, rs in parts:
            try:
                if rs:
                    batch(client(), pid, rs)
            except HttpError as e:
                print(f"warning: {slide_id}: {el['kind'] + ' ' + el['id'] if el else 'request'} rejected "
                      f"({api_error(e)})" + ("; using a picture of it instead" if el and el["kind"] != "image" else ""))
                if el and el["kind"] != "image":
                    refused.append((page, el["id"]))

    # A full batch is sent while the next slides are still being planned, several at a time.
    sent = []

    def dispatch(items: list) -> None:
        if pool:
            sent.append(pool.submit(send, items))
        else:
            send(items)

    pending: list[tuple[str, int, list]] = []
    pending_size = 0
    try:
        # The layouts first, and never beside the slides: a slide's title placeholder inherits
        # the layout's box until we give it one of its own, and while the layout batch is in the
        # air together with the content batch that does that, the one Google commits LAST wins -
        # a layout batch landing second takes every title's own box away again and the deck's
        # titles all sit at the layout's, silently. Measured with three conversions at once
        # (`tools/probe_layout_race.py`): the deck whose layout batch landed after its first
        # content batch lost all ten titles, the two that landed first kept theirs. So the
        # layout pass overlaps the read and `measure_places` above it, and nothing below.
        if layout_work is not None:
            layout_work.result()
        for slide in deck["slides"]:
            n = slide["page"]
            slide_id = f"b2s_s{n:03}"
            late: list[tuple[int, Exception]] = []
            parts, element_ids = plan.slide_parts(slide, page_elements, speaker_notes, moves, template_sizes, late)
            for i, e in late:
                # Planned with Google's own sizes, an element the rehearsal passed tripped all the
                # same: it goes the way of a refused one, into the rebuild with pictures (`emit`).
                # (a picture is one already, and stays as the .pptx brought it)
                el = slide["elements"][i]
                print(f"warning: {slide_id}: {el['kind']} {el['id']} could not be planned ({type(e).__name__}: {e})"
                      + ("; using a picture of it instead" if el["kind"] != "image" else ""))
                if el["kind"] != "image":
                    refused.append((n, el["id"]))
            size = sum(len(rs) for _, rs in parts)
            # Several slides per round trip; a slide's requests are never split across batches.
            if pending and pending_size + size > BATCH_MAX_REQUESTS:
                dispatch(pending)
                pending, pending_size = [], 0
            pending.append((slide_id, n, parts))
            pending_size += size
            objects, groups = element_objects(parts, element_ids)
            state["slides"].append({"page": n, "objectId": slide_id, "elements": element_ids, "objects": objects,
                                    "groups": groups})
            if plan.pptx_tables:  # the base records them: a sync refills such a table in place (sync.table_refill)
                state["slides"][-1]["table_margins"] = {str(i): pptx_table(el, plan.scale, plan.fonts)["margins"]
                                                        for i, el in enumerate(slide["elements"]) if el["kind"] == "table"}
            kinds = [el["kind"] for el in slide["elements"]]
            print(f"  slide {n + 1}: {kinds.count('text')} text boxes, {kinds.count('image')} pictures, "
                  f"{kinds.count('shape')} shapes, {kinds.count('table')} tables")
        if pending:
            dispatch(pending)
        for job in sent:             # every content batch has landed
            job.result()
    finally:
        for p in (pool, layout_pool):
            if p:
                p.shutdown()
    refused.sort()                   # several threads appended to it
    batch(slides, pid, [{"deleteObject": {"objectId": oid}} for oid in [s["objectId"] for s in sources] + scratch])
    state["deck"] = deck  # (what was built, for the sync snapshot; not written to emit.json)
    return state, refused


def created_ids(reqs: list[dict]) -> list[str]:
    """Object ids a list of requests creates."""
    out = []
    for r in reqs:
        (kind, body), = r.items()
        if kind in ("createShape", "createLine", "createTable", "createImage", "createSlide"):
            out.append(body["objectId"])
        elif kind == "duplicateObject":
            out += list(body.get("objectIds", {}).values())
        elif kind == "groupObjects":
            out.append(body["groupObjectId"])
    return out


def element_objects(parts: list[tuple[dict | None, list[dict]]], element_ids: list[str]) -> tuple[list[list[str]], list[str]]:
    """Per element (in slide_parts order) every object id created for it: its main object first,
    then what its requests create and its group with anchored pictures ({oid}_g); and the slide's
    other groups (blocks, rules)."""
    objects = [[oid] for oid in element_ids]
    index = {oid: i for i, oid in enumerate(element_ids)}
    groups = []
    k = 0
    for el, reqs in parts:
        if el is not None:
            objects[k] += [o for o in created_ids(reqs) if o != element_ids[k]]
            k += 1
            continue
        for oid in created_ids(reqs):
            owner = index.get(oid[:-2]) if oid.endswith("_g") else None
            if owner is not None:
                objects[owner].append(oid)
            else:
                groups.append(oid)
    return [list(dict.fromkeys(o)) for o in objects], groups


class DeckPlan:
    """The requests build_deck sends, apart from what only Google knows (the imported slides'
    object IDs, placeholder and template sizes, measured hole moves): pure, so tests can check
    them offline (plan_offline)."""

    def __init__(self, deck: dict, page_width: float = SLIDE_W, pptx_tables: bool = False, contain: bool = False):
        # `page_width`: the width of the deck this plan is for, in slide pt. A deck `convert` makes
        # is always SLIDE_W wide (it uploads the .pptx that says so), but `sync` may be writing into
        # a deck a person built at any size (`adopt_sync`), and every box, font size and hole width
        # below is this converter's PDF pt times `scale`.
        # `pptx_tables`: tables come with the imported .pptx, empty and with their cell margins
        # (`tables`, build_pptx), and are filled in; else (sync) they are made by createTable.
        # `contain`: the deck as classify wrote it, whose blocks the plan merges, and an element
        # it cannot plan is the picture of its region (`contain`, listed in `contained`), whose
        # file the caller crops (`crop_fallbacks`). Without it the deck's blocks are merged
        # already and such an element raises.
        self.page_width = page_width
        self.pptx_tables = pptx_tables
        self.scale = scale = page_width / deck["slides"][0]["size"][0]
        self.fonts = fonts = FontMapper()
        # Internal link targets: PDF page -> slide. A skipped overlay step maps to the kept
        # (last) step of its frame, which comes right after it.
        kept = sorted(s["page"] for s in deck["slides"])
        self.page_slide = {}
        for page in range(kept[-1] + 1):
            target = next(k for k in kept if k >= page)
            self.page_slide[page] = f"b2s_s{target:03}"
        self.contained: list[dict] = []  # {"page", "id", "kind", "error"}: elements made pictures
        self._scratch = None
        slides = [self.contain(s) for s in deck["slides"]] if contain else deck["slides"]
        self.merged = {**deck, "slides": slides}  # (blocks merged, holes not yet fitted: `emit`'s rebuild)
        self.deck = deck = {**deck, "slides": [fit_holes(s, scale, fonts) for s in slides]}
        self.keys = list(dict.fromkeys(k for s in deck["slides"] for e in s["elements"] for k in element_template_keys(e, scale)))
        self.uses_templates = {s["page"]: any(element_template_keys(e, scale) for e in s["elements"]) for s in deck["slides"]}
        self.shifts = {s["page"]: formula_shifts(s, scale, fonts) for s in deck["slides"]}
        self.overlays = {s["page"]: overlay_boxes(s, scale, fonts) for s in deck["slides"]}

    def contain(self, slide: dict) -> dict:
        """`slide` (as classify wrote it) with its blocks merged, as emit writes it - but for every
        element emit cannot plan, which is the picture of its region instead (`fallback_element`,
        what a refused element becomes), recorded in `contained`.

        A deck.json element is a plain dict with several producers (classify, `marked.py`'s read of
        an adopted source, `deck_ir`, `fallback_pictures`), and one missing a field emit reads (an
        adopted `custom` shape with no `flip`, 688ebf4) failed the whole conversion before anything
        was sent. So each slide is planned here first as it will be later (`_rehearse`: the .pptx's
        template shapes and tables, `measure_jobs`, `copy_request`, `slide_parts`, Google's answers
        made up as in `plan_offline`), and an element whose own planning raises is replaced where it
        stands: the others keep their indices, so their object ids and z-order do not change. A step
        over the whole slide (merging blocks, fitting holes, growing panels, grouping) names no
        element: the one whose picture lets it through is taken (`_culprit`), and the step raises
        when none does. The same slide gives the same pictures. `strict()` raises instead."""
        n = slide["page"]
        merged, gone = _settle(slide["elements"], lambda out: (merge_blocks(_swapped(slide["elements"], out)), []))
        merged = {**slide, "elements": merged}

        def rehearsal(out: set) -> tuple[dict, list[tuple[str, Exception]]]:
            s = {**merged, "elements": _swapped(merged["elements"], out)}
            return s, [(s["elements"][i]["id"], e) for i, e in self._rehearse(s)]

        result, more = _settle(merged["elements"], rehearsal)
        kinds = {el.get("id"): el.get("kind") for el in slide["elements"]}
        for eid, e in {**gone, **more}.items():
            self.contained.append({"page": n, "id": eid, "kind": kinds.get(eid), "error": f"{type(e).__name__}: {e}"})
        return result

    def _rehearse(self, slide: dict) -> list[tuple[int, Exception]]:
        """Plan one slide (blocks merged) as build_deck will: [(element index, exception)] for each
        element whose own planning raised. A step over the whole slide raises."""
        scale, fonts = self.scale, self.fonts
        slide = fit_holes(slide, scale, fonts)
        failed: list[tuple[int, Exception]] = []
        for i, el in enumerate(slide["elements"]):  # what the .pptx carries for it (build_pptx)
            try:
                keys = element_template_keys(el, scale)
                if keys:
                    _add_template_shapes(self._scratch_slide(), keys)
                if self.pptx_tables and el["kind"] == "table":
                    _add_table(self._scratch_slide(), pptx_table(el, scale, fonts))
            except Exception as e:  # noqa: BLE001 - one element's planning, contained by `contain`
                failed.append((i, e))
        if failed:
            return failed
        plan = _slide_plan(slide, scale, fonts, self.pptx_tables, self.page_slide)
        measure_jobs(plan.deck, scale, fonts, plan.placed, self.page_slide)
        request, sizes, copied = _offline_copy(plan, slide)
        sid = request["duplicateObject"]["objectIds"][request["duplicateObject"]["objectId"]]
        plan.slide_parts(slide, {sid: copied}, {sid: f"{sid}_notes"}, {}, sizes, failed)
        return failed

    def _scratch_slide(self):
        """A python-pptx slide `_rehearse` puts template shapes and tables on, as build_pptx will."""
        if self._scratch is None:
            from pptx import Presentation
            prs = Presentation()
            self._scratch = prs.slides.add_slide(prs.slide_layouts[TEMPLATE_LAYOUTS["BLANK"]])
        return self._scratch

    def placed(self, el: dict, n: int) -> dict:
        """Inline formula pictures sit over the gap Slides leaves for them (formula_shifts),
        graphics drawn at words over those words (overlay_boxes)."""
        overlays, shifts = self.overlays[n], self.shifts[n]
        if el["id"] in overlays:
            return {**el, "bbox": [overlays[el["id"]][0], el["bbox"][1], overlays[el["id"]][1], el["bbox"][3]]}
        dx = shifts.get(el["id"])
        return el if dx is None else {**el, "bbox": [el["bbox"][0] + dx, el["bbox"][1], el["bbox"][2] + dx, el["bbox"][3]]}

    def pictures(self, slide: dict) -> list[tuple[dict, list[float]]]:
        """The slide's pictures with their boxes in the .pptx (slide pt)."""
        return [(e, [v * self.scale for v in self.placed(e, slide["page"])["bbox"]])
                for e in slide["elements"] if e["kind"] == "image"]

    def tables(self, slide: dict) -> list[dict]:
        """The slide's tables as the .pptx carries them (pptx_table), when it does."""
        if not self.pptx_tables:
            return []
        return [pptx_table(e, self.scale, self.fonts) for e in slide["elements"] if e["kind"] == "table"]

    def copy_request(self, slide: dict, source: dict) -> tuple[dict, list[tuple[float, float]]]:
        """Phase 1: the duplicateObject copying a slide's imported source under our object IDs,
        and the sizes of the template shapes on the source."""
        n = slide["page"]  # PDF page index; slides may skip pages (overlays)
        slide_id = f"b2s_s{n:03}"
        keys, uses_templates = self.keys, self.uses_templates
        els = source.get("pageElements", [])
        placeholders = {e["shape"]["placeholder"]["type"]: e["objectId"] for e in els if "placeholder" in e.get("shape", {})}
        pictures = [e["objectId"] for e in els if "image" in e]
        tables = [e["objectId"] for e in els if "table" in e]
        shapes = [e for e in els if "image" not in e and "table" not in e and "placeholder" not in e.get("shape", {})]
        picture_idx = [i for i, e in enumerate(slide["elements"]) if e["kind"] == "image"]
        table_idx = [i for i, e in enumerate(slide["elements"]) if e["kind"] == "table"] if self.pptx_tables else []
        if len(pictures) != len(picture_idx) or len(tables) != len(table_idx) or \
                len(shapes) != (len(keys) if uses_templates[n] else 0):
            raise RuntimeError(f"slide {n + 1}: the import brought {len(pictures)} pictures, {len(tables)} tables and "
                               f"{len(shapes)} template shapes, expected {len(picture_idx)}, {len(table_idx)} and "
                               f"{len(keys) if uses_templates[n] else 0}")
        ids = {source["objectId"]: slide_id}
        ids.update({oid: f"{slide_id}_f{i}" for oid, i in zip(pictures, picture_idx)})
        ids.update({oid: f"{slide_id}_tab{i}" for oid, i in zip(tables, table_idx)})
        ids.update({e["objectId"]: f"{slide_id}_k{j}" for j, e in enumerate(shapes)})
        title_idx = title_element(slide)
        if title_idx is not None:
            ids[placeholders[slide_layout(slide)[1]]] = f"{slide_id}_t{title_idx}"
            sub_idx = subtitle_element(slide, title_idx)
            if sub_idx is not None and "SUBTITLE" in placeholders:
                ids[placeholders["SUBTITLE"]] = f"{slide_id}_t{sub_idx}"
        return {"duplicateObject": {"objectId": source["objectId"], "objectIds": ids}}, [size_pt(e) for e in shapes]

    def slide_parts(self, slide: dict, page_elements: dict[str, list[dict]], speaker_notes: dict[str, str | None],
                    moves: dict[str, tuple[float, float]], template_sizes: list[tuple[float, float]],
                    failed: list[tuple[int, Exception]] | None = None
                    ) -> tuple[list[tuple[dict | None, list[dict]]], list[str]]:
        """Phase 2 for one slide after its copy: requests in parts ((element, requests), so a
        rejected batch can be narrowed down to the element at fault) and the element object IDs.
        `page_elements` and `speaker_notes` describe the copied slides (slide id -> elements with
        objectId and size, speaker notes object id), `moves` are measure_places' results.
        `failed`: an element whose requests raise gets none, and (its index, the exception) goes
        there (`contain`, build_deck); without it, or `strict()`, the exception is raised."""
        scale, fonts, keys = self.scale, self.fonts, self.keys
        placed, page_slide, uses_templates = self.placed, self.page_slide, self.uses_templates
        placeholder_dy = PPTX_TITLE_DY

        def template_on_slide(slide_id: str, key: tuple) -> dict:
            """The slide's copy of a template shape ({"id", "w", "h"}: its unscaled size in pt)."""
            j = keys.index(key)
            w, h = template_sizes[j]
            return {"id": f"{slide_id}_k{j}", "w": w, "h": h}

        n = slide["page"]
        slide = grown_panels(slide, scale, fonts)
        slide_id = f"b2s_s{n:03}"
        # The title (and title page subtitle) is refilled in its placeholder only when the slide
        # holds one: sync demotes a title with no live placeholder, and on an adopted slide the
        # next title-role box (`marked.py` gives the role per box) is a box like any other.
        live = {e["objectId"] for e in page_elements.get(slide_id, [])}
        title_idx = title_element(slide)
        title_oid = f"{slide_id}_t{title_idx}" if title_idx is not None else None
        sub_idx = subtitle_element(slide, title_idx) if title_idx is not None else None
        subtitle_oid = f"{slide_id}_t{sub_idx}" if sub_idx is not None else None
        title_oid, subtitle_oid = (oid if oid in live else None for oid in (title_oid, subtitle_oid))
        ours = (f"{slide_id}_k", f"{slide_id}_f", f"{slide_id}_tab")  # template shapes, pictures, tables from the .pptx
        parts: list[tuple[dict | None, list[dict]]] = [(None, [
            {"deleteObject": {"objectId": e["objectId"]}}
            for e in page_elements.get(slide_id, [])
            if e["objectId"] not in (title_oid, subtitle_oid) and not e["objectId"].startswith(ours)])]
        def element_requests(el: dict, oid: str) -> list[dict]:
            if el["kind"] == "shape":
                key = template_key(el, scale)
                return shape_requests(el, slide_id, oid, scale, template_on_slide(slide_id, key) if key else None)
            if el["kind"] == "table":
                return table_requests(el, slide_id, oid, scale, fonts, self.pptx_tables)
            if el["kind"] == "diagram":
                return diagram_requests(el, slide_id, oid, scale, fonts,
                                        (lambda key, s=slide_id: template_on_slide(s, key)) if keys else None)
            if el["kind"] == "image":
                # The picture came with the slide: move it to its place in the z-order.
                reqs = [{"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}}]
                if el["id"] in moves:  # to the gap or words measured for it (measure_places)
                    dx, dy, sx = (*moves[el["id"]], 1.0)[:3]
                    # (a relative transform scales about the page origin: the left edge keeps its dx)
                    reqs.insert(0, {"updatePageElementTransform": {"objectId": oid, "applyMode": "RELATIVE", "transform": {
                        "scaleX": sx, "scaleY": 1, "unit": "EMU",
                        "translateX": round((dx + (1 - sx) * el["bbox"][0] * scale) * EMU_PER_PT),
                        "translateY": round(dy * EMU_PER_PT)}}})
                if el.get("number"):
                    reqs += number_box_requests(el["number"], slide_id, f"{oid}n", scale, fonts)
                return reqs
            placeholder = None
            if oid in (title_oid, subtitle_oid):
                size = next(e["size"] for e in page_elements[slide_id] if e["objectId"] == oid)
                placeholder = {"base_w": size["width"]["magnitude"] / EMU_PER_PT,
                               "base_h": size["height"]["magnitude"] / EMU_PER_PT, "dy": placeholder_dy}
            return text_box_requests(el, slide_id, oid, scale, fonts, placeholder, page_slide,
                                     title_bar_under(el, slide), text_right_limit(el, slide))

        element_ids = []
        for i, el in enumerate(slide["elements"]):  # shapes, then pictures, then text on top
            oid = f"{slide_id}_{OBJECT_PREFIX.get(el['kind'], 't')}{i}"
            try:
                el = placed(el, n)
                reqs = element_requests(el, oid)
            except Exception as e:  # noqa: BLE001 - one element's planning (`contain`); nothing is sent here
                if failed is None or strict():
                    raise
                failed.append((i, e))
                reqs = []
            parts.append((el, reqs))
            element_ids.append(oid)
        # The slide's copies of the template shapes have been duplicated from: remove them.
        extra = [{"deleteObject": {"objectId": f"{slide_id}_k{j}"}} for j in range(len(keys)) if uses_templates[n]]
        # Inline formula pictures move with their text: group them (placeholders can't be grouped).
        by_id = {el["id"]: oid for el, oid in zip(slide["elements"], element_ids)}
        anchored: dict[str, list[str]] = {}
        for el, oid in zip(slide["elements"], element_ids):
            if el.get("anchor") in by_id and by_id[el["anchor"]] not in (title_oid, subtitle_oid):
                anchored.setdefault(by_id[el["anchor"]], []).extend([oid, f"{oid}n"] if el.get("number") else [oid])
        grouped = set()
        for text_oid, pictures in anchored.items():
            extra.append({"groupObjects": {"groupObjectId": f"{text_oid}_g", "childrenObjectIds": [text_oid] + pictures}})
            grouped |= {text_oid, *pictures}
        # A beamer block (title bar and body shapes plus everything on them) moves as one.
        for bi, members in enumerate(block_groups(slide["elements"], element_ids, title_oid)):
            children = [f"{m}_g" if m in anchored else m for m in members if m not in grouped or m in anchored]
            if len(children) >= 2:
                extra.append({"groupObjects": {"groupObjectId": f"{slide_id}_blk{bi}", "childrenObjectIds": children}})
                # A group takes the place of its topmost member, above a table lying on the
                # block (tables can't join the group): blocks are backdrops, send them back.
                extra.append({"updatePageElementsZOrder": {"pageElementObjectIds": [f"{slide_id}_blk{bi}"],
                                                           "operation": "SEND_TO_BACK"}})
        for ri, members in enumerate(rule_groups(slide["elements"], element_ids)):
            extra.append({"groupObjects": {"groupObjectId": f"{slide_id}_rules{ri}", "childrenObjectIds": members}})
        if slide.get("notes") and speaker_notes.get(slide_id):
            extra.append({"insertText": {"objectId": speaker_notes[slide_id], "text": slide["notes"]}})
        if title_oid and len(slide["elements"]) > 1:
            # The placeholder was created with the slide, below everything added since.
            extra.append({"updatePageElementsZOrder": {"pageElementObjectIds": [o for o in (title_oid, subtitle_oid) if o],
                                                       "operation": "BRING_TO_FRONT"}})
        parts += [(None, [r]) for r in extra]
        return parts, element_ids


def plan_offline(deck: dict, placeholder_size: tuple[float, float] = (612.0, 90.0),
                 template_size: tuple[float, float] = (100.0, 100.0)) -> dict:
    """What emit would send for a classified deck, without Google: the imported slides are made
    up as the .pptx brings them (layout placeholders, pictures, template shapes) and hole
    pictures keep their predicted places. {"plan": DeckPlan, "pictures": {page: [(element, .pptx
    box)]}, "copies": phase 1 requests, "page_elements" and "speaker_notes": the copied slides,
    "measure": measure_places' scratch slide requests, "slides": [(slide id, page, parts, element ids)],
    "contained": the elements it could not plan, pictures now (`DeckPlan.contain`)}. `deck`: as
    classify wrote it (the plan merges its blocks)."""
    plan = DeckPlan(deck, pptx_tables=True, contain=True)
    copies, page_elements, template_sizes = [], {}, []
    for slide in plan.deck["slides"]:
        request, sizes, copied = _offline_copy(plan, slide, placeholder_size, template_size)
        copies.append(request)
        template_sizes = template_sizes or sizes
        page_elements[request["duplicateObject"]["objectIds"][request["duplicateObject"]["objectId"]]] = copied
    speaker_notes = {slide_id: f"{slide_id}_notes" for slide_id in page_elements}
    slides = []
    for slide in plan.deck["slides"]:
        parts, element_ids = plan.slide_parts(slide, page_elements, speaker_notes, {}, template_sizes)
        slides.append((f"b2s_s{slide['page']:03}", slide["page"], parts, element_ids))
    return {"plan": plan, "pictures": {s["page"]: plan.pictures(s) for s in plan.deck["slides"]}, "copies": copies,
            "page_elements": page_elements, "speaker_notes": speaker_notes,
            "measure": measure_jobs(plan.deck, plan.scale, plan.fonts, plan.placed, plan.page_slide)[0], "slides": slides,
            "contained": plan.contained}


def _offline_copy(plan: DeckPlan, slide: dict, placeholder_size: tuple[float, float] = (612.0, 90.0),
                  template_size: tuple[float, float] = (100.0, 100.0)) -> tuple[dict, list, list[dict]]:
    """A slide's phase 1 without Google (`plan_offline`, `DeckPlan._rehearse`): its source slide made
    up as the .pptx brings it (layout placeholders, pictures, tables, template shapes, at made-up
    sizes). (copy_request's request, the template shapes' sizes, the copied slide's elements)"""
    def size(w: float, h: float) -> dict:
        return {"width": emu(w), "height": emu(h)}

    n, source = slide["page"], f"src{slide['page']:03}"
    els = [{"objectId": f"{source}_{kind}", "size": size(*placeholder_size), "shape": {"placeholder": {"type": kind}}}
           for kind in LAYOUT_PLACEHOLDERS[slide_layout(slide)[0]]]
    els += [{"objectId": f"{source}_p{i}", "size": size(1, 1), "image": {}} for i, _ in enumerate(plan.pictures(slide))]
    els += [{"objectId": f"{source}_tb{i}", "size": size(sum(t["widths"]), sum(t["heights"])), "table": {}}
            for i, t in enumerate(plan.tables(slide))]
    els += [{"objectId": f"{source}_k{j}", "size": size(*template_size), "shape": {}}
            for j in range(len(plan.keys) if plan.uses_templates[n] else 0)]
    request, sizes = plan.copy_request(slide, {"objectId": source, "pageElements": els})
    ids = request["duplicateObject"]["objectIds"]
    return request, sizes, [{"objectId": ids.get(e["objectId"], f"{e['objectId']}_copy"), "size": e["size"]} for e in els]


def _slide_plan(slide: dict, scale: float, fonts: FontMapper, pptx_tables: bool, page_slide: dict) -> DeckPlan:
    """A DeckPlan of one slide (holes fitted), with none of the deck-wide work __init__ does: the
    template shapes are the slide's own (`slide_emission`, `DeckPlan._rehearse`)."""
    n = slide["page"]
    keys = list(dict.fromkeys(k for e in slide["elements"] for k in element_template_keys(e, scale)))
    plan = DeckPlan.__new__(DeckPlan)
    plan.page_width, plan.pptx_tables, plan.scale, plan.fonts = slide["size"][0] * scale, pptx_tables, scale, fonts
    plan.deck = {"slides": [slide]}
    plan.keys, plan.uses_templates = keys, {n: bool(keys)}
    plan.shifts = {n: formula_shifts(slide, scale, fonts)}
    plan.overlays = {n: overlay_boxes(slide, scale, fonts)}
    plan.page_slide = page_slide
    plan.contained, plan._scratch, plan.merged = [], None, plan.deck
    return plan


def _swapped(elements: list[dict], gone: set) -> list[dict]:
    """`elements` with each whose id is in `gone` the picture of its region, where it stands."""
    return [fallback_element(e) if e.get("id") in gone else e for e in elements]


def _settle(elements: list[dict], attempt: Callable[[set], tuple[_T, list[tuple[str, Exception]]]]
            ) -> tuple[_T, dict[str, Exception]]:
    """Run `attempt(gone)` -> (result, [(element id, exception)] for each element whose own planning
    raised) with more and more of `elements` made pictures, until none raises: (that result, {id:
    exception} of those made pictures). An exception `attempt` raises itself (a step over the whole
    slide) is blamed on `_culprit`'s element; it is raised when there is none, and so is one of an
    element already made a picture, and every one under `strict()`."""
    gone: dict[str, Exception] = {}
    while True:
        try:
            result, failed = attempt(set(gone))
        except Exception as e:  # noqa: BLE001 - local planning only: nothing has been sent
            culprit = None if strict() else _culprit(elements, gone, attempt, e)
            if culprit is None:
                raise
            new = {culprit: e}  # (never one of `gone`: _culprit leaves those out)
        else:
            if failed and strict():
                raise failed[0][1]
            new = {eid: e for eid, e in failed if eid not in gone}
            if not new:
                if failed:
                    raise failed[0][1]
                return result, gone
        for eid, e in new.items():
            if not any(el.get("id") == eid and _can_picture(el) for el in elements):
                raise e  # (no region to make a picture of: raised, never dropped)
        gone.update(new)


def _can_picture(el: dict) -> bool:
    """Whether `fallback_element` can make a picture of the element (an id, a box)."""
    try:
        fallback_element(el)
    except Exception:  # noqa: BLE001
        return False
    return True


def _culprit(elements: list[dict], gone: dict, attempt, error: Exception) -> str | None:
    """The element a step over the whole slide failed on (`error`), None when none is found.

    First the suspects (`_suspects`: the elements the failing code was holding, one lacking the
    key of a KeyError first), each made a picture alone: the first after which the step no longer
    fails the same way, on the same suspect, is blamed. (Passing is too much to ask: two panels
    missing the same `flip` fail alike, and each is to blame.) Else the elements are made pictures
    one after the other in slide order, and the one whose picture lets `attempt` through is."""
    # (one with no id or box to make a picture of is never blamed, so never lost)
    ids = [el["id"] for el in elements if el.get("id") not in gone and _can_picture(el)]

    def signature(e: Exception) -> tuple:
        return type(e), e.args, next(iter(_suspects(e, ids)), None)

    was = signature(error)
    for eid in _suspects(error, ids):
        try:
            attempt(set(gone) | {eid})
        except Exception as e:  # noqa: BLE001
            if signature(e) == was:
                continue
        return eid
    out = set(gone)
    for eid in ids:
        out.add(eid)
        try:
            attempt(set(out))
        except Exception:  # noqa: BLE001
            continue
        return eid
    return None


def _suspects(error: Exception, ids: list[str]) -> list[str]:
    """Ids (of `ids`) of the elements the code that raised `error` held in its variables, innermost
    frame first; in each frame those lacking a KeyError's key before the others."""
    key = error.args[0] if isinstance(error, KeyError) and error.args and isinstance(error.args[0], str) else None
    frames, tb = [], error.__traceback__
    while tb is not None:
        frames.append(tb.tb_frame)
        tb = tb.tb_next
    found: list[str] = []
    for frame in reversed(frames):
        held = [v for v in frame.f_locals.values() if isinstance(v, dict) and "kind" in v and v.get("id") in ids]
        for v in sorted(held, key=lambda v: key is None or key in v):
            if v["id"] not in found:
                found.append(v["id"])
    return found


class _OnePage(dict):
    """`page_slide` for `slide_emission`: every internal link goes to one stand-in slide. Which
    slide a link names is the element's own IR (`identity.ir_fields` keys it), not its neighbours'."""

    def __bool__(self) -> bool:
        return True

    def get(self, page, default=None):
        return "b2s_link"


def slide_emission(slide: dict, scale: float, fonts: FontMapper, placeholder_size: tuple[float, float] = (612.0, 90.0),
                   template_size: tuple[float, float] = (100.0, 100.0)) -> dict:
    """What emit writes for one slide of a DeckPlan (blocks merged, holes fitted: `DeckPlan.deck`),
    worked out from that slide alone, so the same slide gives the same answer in whichever deck it
    stands: {"slide_id", "parts" and "element_ids" (`DeckPlan.slide_parts`), "boxes" (each
    picture's predicted place in slide pt, None for other kinds), "title" and "subtitle" (element
    indices of the layout placeholders' texts), "templates" ({object id of the slide's copy: its
    template key})}. As in `plan_offline` the layout's placeholders and the template shapes have
    made-up sizes; unlike it, links to other slides all go to one page and measure_places' moves
    are left out (only Google's renderer knows them), so pictures keep their predicted places.
    For sync.mark_emitted, which compares a base slide's emission with the new one's.

    Nothing is contained here (`DeckPlan.contain`): the slide is a plan's, whose elements emit
    could not plan are pictures already, the same ones every time; and a base slide an older
    converter wrote that today's emit cannot plan has to raise, which mark_emitted reads as
    "nothing to compare" - a picture in its place would be a change the source never made."""
    n = slide["page"]
    slide_id = f"b2s_s{n:03}"
    plan = _slide_plan(slide, scale, fonts, False, _OnePage())
    keys = plan.keys
    title = title_element(slide)
    subtitle = subtitle_element(slide, title) if title is not None else None
    w, h = placeholder_size
    page_elements = {slide_id: [{"objectId": f"{slide_id}_t{i}", "size": {"width": emu(w), "height": emu(h)}}
                                for i in (title, subtitle) if i is not None]}
    parts, element_ids = plan.slide_parts(slide, page_elements, {}, {}, [template_size] * len(keys))
    boxes = [[v * scale for v in plan.placed(e, n)["bbox"]] if e["kind"] == "image" else None for e in slide["elements"]]
    return {"slide_id": slide_id, "parts": parts, "element_ids": element_ids, "boxes": boxes, "title": title,
            "subtitle": subtitle, "templates": {f"{slide_id}_k{j}": k for j, k in enumerate(keys)}}
