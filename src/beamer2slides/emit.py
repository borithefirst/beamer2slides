"""Stage 4: build the Google Slides deck from deck.json and the background images.

This module is the stage's face: `emit` and `build_deck`, the rebuild guard's preflight, and
`DeckPlan` / `plan_offline` / `slide_emission`, which say what a deck or one slide is written as.
The rest lives by topic: `emit_metrics` (Slides' measures, FontMapper, bullets), `emit_widths`
(measured advances and line breaks), `emit_text` (text boxes), `emit_pptx` (the .pptx upload,
shapes), `emit_tables`, `emit_diagrams`, `emit_holes` and `emit_places` (pictures placed by
prediction, then by measurement) and `emit_theme` (the presentation, master and layouts). Names
callers have always taken from here still are.
"""

import os
from collections.abc import Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from concurrent.futures import Future, ThreadPoolExecutor
from copy import copy
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Callable, TypedDict, TypeVar

from . import emit_state
from .emit_state import Emitted
from .emit_diagrams import (
    HeldPicture, block_groups, block_stacking, diagram_requests_of, element_template_keys_on, rule_groups,
)
from .emit_diagrams import (  # noqa: F401 (callers take these from here)
    bend_template_key, connection, diagram_requests, element_template_keys, label_inside, node_template_key,
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
from .emit_model import (
    ElementDict, JsonMap, ObjectMap, Place, Placeholder, PptxTable, PptxText, Shell, Template, TemplateKey, box_of,
    dict_of, json_number, objects_of, set_text, table_of,
)
from .emit_pptx import TEMPLATE_LAYOUTS, api_error, batch, build_pptx, shape_element_requests
from .emit_pptx import shape_requests, template_key  # noqa: F401 (callers take these from here)
from .emit_pptx import _add_table, _add_template_shapes, _add_text_shell  # (DeckPlan.contain tries what the .pptx carries)
from .emit_pptx import NO_TABLE_STYLE, NS_A, VARIANT  # noqa: F401 (callers take these from here)
from .emit_tables import pptx_table_of, table_element_requests
from .emit_tables import (  # noqa: F401 (callers take these from here)
    TABLE_CELL_PAD, TABLE_MARGIN, TABLE_MIN_SHRINK, TABLE_TEXT_TOP, fit_columns, pptx_table, squeezed_columns,
    table_columns, table_fits, table_layout, table_requests,
)
from .emit_text import (
    Page, merge_blocks, number_requests, text_element_requests, text_requests_on_page, text_shell_of,
    text_shell_requests,
)
from .emit_text import (  # noqa: F401 (callers take these from here)
    number_box_requests, text_box_requests,
)
from .emit_text import (  # noqa: F401 (callers take these from here)
    HOLE_BREAK, HOLE_FONT, HOLE_SPACE_EM, LINE_MARGIN, extra_above, extra_below, hole_run, hole_runs,
    in_sentence, inner_pitch, line_pitch, line_size, line_sizes, pitch_between, run_sizes, snap,
    vertical_layout,
)
from .emit_theme import (
    LAYOUT_PLACEHOLDERS, BgKey, import_presentation, master_plan_of, slide_layout, subtitle_element, title_element,
    write_layouts,
)
from .emit_theme import (  # noqa: F401 (callers take these from here)
    LAYOUT_TEXT_PREFIX, PPTX_MIME, background_key, layout_placeholder_requests, layout_style_spec, master_ground,
    master_plan, plan_theme, style_layout_placeholders,
)
from .emit_widths import (  # noqa: F401 (callers take these from here)
    SCRIPT_SIZE, SMALL_CAPS_SIZE, WRAP_MARGIN, ZWSP, first_break, pdf_line_breaks, pdf_width, slides_lines,
    slides_width, wrap_joins, wrap_window, wrapped_width,
)
from .fonts import font_info  # noqa: F401 (callers take these from here)
from .gapi import HttpError
from .google_auth import credentials_for_threads, drive_service, shared_service, slides_service
from .google_types import (
    DriveService, Presentation, SlidesRequest, SlidesService, as_json, object_id, slides_request_kind,
)
from .gslides import EMU_PER_PT, emu_json, execute, per_thread
from .ir_types import (
    DiagramElement, Element, FallbackImage, ImageElement, MarkedShape, RenderedElement, ShapeElement, TableElement,
    TextElement, parse_element, parse_rendered_element,
)
from .json_types import Json, JsonObject, as_array, as_int, as_object, as_objects, as_optional_str, as_str
from .typing_compat import assert_never, override

if TYPE_CHECKING:
    from pptx.slide import Slide

BATCH_MAX_REQUESTS = 400  # slides are sent together until a batch reaches this size
# A round trip to Google costs about a second whatever it carries, so the wall clock of a
# conversion is round trips and not work (measured: tools/probe_batch_parallelism.py). Several
# batches may be in flight on one presentation at once - Google takes them and loses nothing -
# and four is where the curve flattens: 8 batches of 200 requests take 9.4 s one at a time,
# 6.1 s two at a time, 3.6 s four at a time and 3.1 s eight at a time. Eight lost to four on a
# whole conversion twice (real_presentation-biore, 2026-10-03, and interleaved on 2026-10-04:
# 43.3 s against 39.5 s, every round), and on the 48-slide ambiguous.pdf, whose fifth batch
# waits for a worker, the two were within noise (20.9 / 21.2 s).
CONTENT_WORKERS = 4
PICTURE_TITLES = {"math": "Formula", "icon": "Icon", "fallback": "Picture"}
# An element emit trips over while planning it is the picture of its region, with a warning, instead
# of the whole conversion failing (`DeckPlan.contain`). Set (to anything but "0"), the failure is
# raised: the offline suite runs so (tests/conftest.py), where a contained failure would hide the bug.
STRICT_ENV = "B2S_EMIT_STRICT"
OBJECT_PREFIX = {"shape": "s", "table": "tab", "diagram": "dg", "image": "f"}  # (text: "t")
_T = TypeVar("_T")


def picture_title(e: JsonMap) -> str:
    """The title a picture's object carries: its role's (PICTURE_TITLES), else "Figure"."""
    role = e.get("role")
    return PICTURE_TITLES.get(role, "Figure") if isinstance(role, str) else "Figure"


def strict() -> bool:
    """Whether a failure planning one element is raised rather than contained (STRICT_ENV)."""
    return os.environ.get(STRICT_ENV, "") not in ("", "0")


def parse_slide_element(el: JsonObject, rendered: bool, where: str) -> Element | RenderedElement:
    """An element of a slide emit plans, parsed at the stage its slide is at (`rendered`: the slide
    has its background). A picture `DeckPlan.contain` put in an element's place is a rendered
    one whatever its slide's stage: it is made of the region's crop (`fallback_element`)."""
    if rendered or (el.get("kind") == "image" and el.get("role") == "fallback"):
        return parse_rendered_element(el, where)
    return parse_element(el, where)


# ---------------------------------------------------------------- main entry

@dataclass(frozen=True, kw_only=True)
class Preflight:
    """What `preflight_rebuild` found: the deck the output folder points at and the guard's finding
    about it (`guard.check_rebuild`), which `plan_rebuild` confirms with one field of one read."""
    presentation_id: str
    found: JsonObject


class ContainedEntry(TypedDict):
    """An element `DeckPlan.contain` made the picture of its region (emit.json's "contained")."""
    page: int
    id: str
    kind: str | None
    error: str


def existing_presentation(drive: DriveService, out: Path) -> str | None:
    """The deck from a previous run of this output folder, if it still exists (not trashed)."""
    from .guard import previous_deck
    previous: JsonObject | None = previous_deck(drive, out)
    return as_str(previous["presentationId"], "presentationId") if previous and previous["state"] == "live" else None


def size_pt(element: JsonMap) -> tuple[float, float]:
    """A page element's size as the API answered it, in pt."""
    size = as_object(element["size"], "size")

    def side(k: str) -> float:
        d = as_object(size[k], k)
        return json_number(d["magnitude"], "magnitude") / (EMU_PER_PT if d["unit"] == "EMU" else 1)
    return side("width"), side("height")


def fallback_element(el: JsonMap) -> JsonObject:
    """What an element becomes when Slides refused it (`fallback_pictures`) or emit could not plan
    it (`DeckPlan.contain`): the picture of its region, 2 pt round its box, which `crop_fallbacks`
    cuts out of the PDF page."""
    x0, y0, x1, y1 = box_of(el["bbox"], "bbox")
    return {"kind": "image", "id": el["id"], "role": "fallback", "bbox": [x0 - 2, y0 - 2, x1 + 2, y1 + 2],
            "file": f"figures/fallback-{el['id']}.png"}


def crop_fallbacks(deck: JsonMap, wanted: list[tuple[int, str]], out: Path, why: str) -> None:
    """Crop the picture of every fallback element ((PDF page, element id) in `wanted`) out of the
    PDF the deck was built from, into `out`. `why`: what made them pictures, for the error when
    that PDF is not there."""
    from .render import crop_region

    if not wanted:
        return
    source_pdf = out / "slides.pdf" if (out / "slides.pdf").exists() else \
        Path(as_str(as_object(deck["source"], "source")["pdf"], "source.pdf"))
    if not source_pdf.exists():
        # The one step of a conversion that needs the PDF itself rather than what was classified
        # out of it, and the only reason `agent.deck_tools.deck_upload` asks for one at all. A
        # folder that travelled without its source says so here rather than inside `crop_region`.
        raise FileNotFoundError(
            f"{why} and the region of each has to be cropped from the page, but the PDF this deck "
            f"was built from is not at {source_pdf}. Put it back beside the folder (or pass it in) "
            f"and build the deck again.")
    regions = set(wanted)
    for slide in objects_of(deck["slides"], "slides"):
        page = _page(slide)
        for el in _elements(slide):
            if (page, el["id"]) in regions and el.get("role") == "fallback":
                file = out / as_str(el["file"], "file")
                file.parent.mkdir(parents=True, exist_ok=True)
                crop_region(source_pdf, page, list(box_of(el["bbox"], "bbox")), file, 6.0)


def fallback_pictures(deck: JsonMap, refused: list[tuple[int, str]], out: Path) -> JsonObject:
    """The deck with every element the API refused ((PDF page, element id)) replaced by a
    picture of its region, cropped from the PDF the deck was built from."""
    new_slides: list[Json] = []
    for slide in objects_of(deck["slides"], "slides"):
        ids = {eid for page, eid in refused if page == slide["page"]}
        elements: list[Json] = []
        for el in _elements(slide):
            eid = el["id"]
            if isinstance(eid, str) and eid in ids and el["kind"] != "image":
                ids.discard(eid)
                el = fallback_element(el)
            elements.append(el)
        new_slides.append({**slide, "elements": elements})
    new: JsonObject = {**deck, "slides": new_slides}
    crop_fallbacks(new, refused, out, f"the API refused {len(refused)} element(s)")
    return new


def preflight_rebuild(out: Path, source_pdf: Path | None, new_deck: bool, force_rebuild: bool,
                      slides: SlidesService | None, drive: DriveService | None) -> Preflight | None:
    """The guard's question (guard.check_rebuild) before the conversion work starts, so a refusal
    comes in a second instead of after extract, classify and render. `emit` asks again - and backs
    the deck up - immediately before the write, in case the deck is edited in between.

    Returns what it found, which that second ask confirms with one field of one read instead of
    asking the whole question again (`plan_rebuild`'s `checked`); None where there was nothing to
    ask. `slides` / `drive`: the clients to ask with (None: this thread's own)."""
    from . import guard

    if new_deck or force_rebuild or not (out / "emit.json").exists():
        return None
    drive = drive or drive_service(None)
    previous: JsonObject | None = guard.previous_deck(drive, out)
    if not previous or previous["state"] != "live":
        return None
    pid = as_str(previous["presentationId"], "presentationId")
    found: JsonObject = guard.check_rebuild(slides or slides_service(None), drive, pid, out, source_pdf, False)
    return Preflight(presentation_id=pid, found=found)


def preflight_in_background(out: Path, source_pdf: Path | None, new_deck: bool,
                            force_rebuild: bool) -> Callable[[], Preflight | None]:
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
        found = preflight_rebuild(out, source_pdf, new_deck, force_rebuild, None, None)
        return lambda: found
    creds = credentials_for_threads()  # here: a worker thread inherits no context (google_auth)
    pool = ThreadPoolExecutor(1, thread_name_prefix="b2s-preflight")
    work = pool.submit(lambda: preflight_rebuild(out, source_pdf, new_deck, force_rebuild,
                                                 slides_service(creds), drive_service(creds)))
    pool.shutdown(wait=False)

    def answer() -> Preflight | None:
        return work.result()
    return answer


def look_again(slides: SlidesService, drive: DriveService, out: Path, checked: Preflight | None
               ) -> tuple[JsonObject | None, JsonObject | None]:
    """What the output folder points at, and the preflight's finding where it still stands.

    Two reads that need nothing of each other - the deck's place in Drive and its revision - so
    they are made at once, one client per thread. `guard.recheck` is the cheap half of the second
    ask; a deck that has moved since the preflight (or one the folder no longer points at, or one
    now in the trash) gets the whole question again, in `plan_rebuild`."""
    from . import guard

    previous: JsonObject | None
    if checked is None or not checked.presentation_id or shared_service("slides", "v1"):
        previous = guard.previous_deck(drive, out)
        return previous, None
    pid, finding = checked.presentation_id, checked.found
    creds = credentials_for_threads()  # here: a worker thread inherits no context (google_auth)
    with ThreadPoolExecutor(1, thread_name_prefix="b2s-recheck") as pool:
        again = pool.submit(lambda: guard.recheck(slides_service(creds), pid, finding))
        previous = guard.previous_deck(drive, out)
        found: JsonObject | None = again.result()
    if previous and previous["presentationId"] == pid and previous["state"] == "live":
        return previous, found
    return previous, None


def plan_rebuild(slides: SlidesService, drive: DriveService, out: Path, new_deck: bool, force_rebuild: bool,
                 backup: str, source_pdf: Path | None, checked: Preflight | None
                 ) -> tuple[str | None, JsonObject | None]:
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
    pid = as_str(previous["presentationId"], "presentationId")
    url = guard.deck_url(pid)
    if previous["state"] != "live":
        where = {"trashed": "is in the Drive trash", "gone": "is gone (deleted, or not this app's file any more)",
                 "other": "is not a presentation any more"}[as_str(previous["state"], "state")]
        print(f"the deck of the previous run ({pid}) {where}: making a new one, that deck is left as it is")
        return None, {"presentationId": pid, "state": previous["state"], "action": "new deck", "url": url}
    if new_deck:
        print(f"--new-deck: the previous deck is left as it is at {url}\n"
              f"  (this folder tracks the new deck from now on; the old one is only reachable by that link)")
        return None, {"presentationId": pid, "state": "kept", "action": "new deck", "url": url}
    found = found or guard.check_rebuild(slides, drive, pid, out, source_pdf, force_rebuild)
    mode = backup if backup != "auto" else ("file" if found["reason"] else "none")
    reason = as_str(found.get("reason") or "no deck edits", "reason")
    entry: JsonObject = {
        "presentationId": pid, "url": url, "action": "rebuilt in place", "revisionId": found.get("revisionId"),
        "modifiedTime": previous.get("modifiedTime"), "out": str(out),  # Drive's clock, and where to restore from
        "checked": found.get("checked"), "reason": reason,
        "summary": guard.summary_line(found) if found.get("edited") else "no deck edits",
        "examples": found.get("examples", []), "base_from": found.get("base_from")}
    if found["reason"]:
        print(f"WARNING: rebuilding a deck that {'was edited in Slides' if found['reason'] == 'edited' else found['reason']} "
              f"(--force-rebuild): {entry['summary']}")
    entry["backup"] = guard.backup_deck(drive, pid, out, mode, reason, True, slides)
    guard.record(out, entry)  # the attempt belongs in the log even when it failed, and what follows
    if found["reason"]:
        guard.demand_way_back(pid, out, source_pdf, entry, mode)  # no backup, no forced rebuild
    print(f"updating existing deck {pid} (revision {found.get('revisionId')})")
    for line in guard.restore_hint(entry, "rebuild") if found["reason"] else []:
        print(line)
    return pid, entry


def emit(deck: ObjectMap, out: Path, title: str, new_deck: bool, measure: bool, force_rebuild: bool, backup: str,
         source_pdf: Path | None, checked: Preflight | None) -> Emitted:
    """Build the deck. An output folder that already has a deck is rebuilt in place unless
    `new_deck`; that replaces the deck's whole content, so `guard.check_rebuild` refuses when
    the deck was edited in Slides (`force_rebuild` goes ahead, after a backup). `checked`: what
    the preflight found (`preflight_rebuild`), which saves the second ask a read of the deck."""
    slides, drive = slides_service(None), drive_service(None)
    # (blocks are merged by the plan, `DeckPlan.contain`, where a block it trips over is a picture)
    existing, previous_entry = plan_rebuild(slides, drive, out, new_deck, force_rebuild, backup, source_pdf, checked)
    built, refused = build_deck(slides, drive, deck, out, title, existing, measure)
    if refused:
        # A picture can only come with the imported .pptx (the API inserts images from public
        # URLs only), so the deck is built once more with the refused elements as pictures.
        print(f"rebuilding the deck with {len(refused)} refused element(s) as pictures")
        # The refused ids are the built deck's (blocks merged, and what emit could not plan made
        # pictures already): the rebuild starts from that deck, so nothing is contained or said twice.
        merged, contained = DeckPlan(deck, SLIDE_W, pptx_tables=True, contain=True).merged, built.state.contained
        built, again = build_deck(slides, drive, fallback_pictures(merged, refused, out), out, title,
                                  built.state.presentation_id, measure)
        if contained:
            built = replace(built, state=replace(built.state, contained=contained))
        for page, eid in again:
            print(f"warning: slide {page + 1}: {eid} was refused again and is missing")
    if previous_entry:
        # what this run replaced, and how to get it back
        built = replace(built, state=replace(built.state, previous=previous_entry))
    emit_state.write(out, built.state)
    return built


def upload_plan(deck: ObjectMap, out: Path) -> "DeckPlan":
    """build_deck's plan of `deck` (as classify wrote it). What the plan could not make of an
    element (a field its producer never wrote: `DeckPlan.contain`) is the picture of its region, as
    a refused element's is: each is said in a warning, listed in emit.json ("contained"), and its
    picture cut out of the page here, before the .pptx is built."""
    plan = DeckPlan(deck, SLIDE_W, pptx_tables=True, contain=True)
    for c in plan.contained:
        print(f"warning: slide {c['page'] + 1}: {c['kind']} {c['id']} could not be planned ({c['error']}); "
              f"using a picture of it instead")
    crop_fallbacks(plan.deck, [(c["page"], c["id"]) for c in plan.contained], out,
                   f"emit could not plan {len(plan.contained)} element(s)")
    return plan


def build_deck(slides: SlidesService, drive: DriveService, deck: ObjectMap, out: Path, title: str, existing: str | None,
               measure: bool) -> tuple[Emitted, list[tuple[int, str]]]:
    """Import the .pptx and fill in the content. Returns the state for emit.json with the deck
    as written, and the elements the API refused ((PDF page, element id)). `measure`: hole and overlay pictures go
    where a thumbnail shows their gaps and words (measure_places), not only where they are predicted."""
    plan = upload_plan(deck, out)
    written, scale, fonts = plan.deck, plan.scale, plan.fonts
    written_slides = plan.slides()
    page_w, page_h = (json_number(v, "size") for v in as_array(written_slides[0]["size"], "size"))

    # Backgrounds: the most common one becomes the master's (the deck's theme): layouts and
    # slides inherit it, and slides added later too. Identical pictures are stored once.
    mp = master_plan_of(written, out, "plan")
    theme = mp.theme

    def fill(key: BgKey) -> dict[str, str | Path]:
        return {"color": key[1]} if key[0] == "color" else {"picture": mp.bg_file[key]}

    master_fill = fill(mp.fill)
    pages = [{
        "layout": theme.layouts[_page(s)] if theme else slide_layout(s)[0],
        "fill": None if mp.bg_key[_page(s)] == mp.shared else fill(mp.bg_key[_page(s)]),
        "pictures": [{"file": out / as_str(e["file"], "file"), "bbox": bbox, "alt": e.get("alt"),
                      "title": picture_title(e)} for e, bbox in plan.pictures(s)],
        "tables": plan.tables(s),
        "shells": plan.shells(s),
        "templates": plan.uses_templates[_page(s)],
    } for s in written_slides]
    pptx = build_pptx(page_w, page_h, plan.keys, pages, master_fill, dict(theme.decorations) if theme else None)
    pres = import_presentation(slides, drive, title, page_w, page_h, pptx, existing)
    pid = as_str(pres["presentationId"], "presentationId")
    sources = as_objects(pres.get("slides", []), "the imported slides")
    if len(sources) != len(written_slides):
        raise RuntimeError(f"the import brought {len(sources)} slides, expected {len(written_slides)}")

    # Phase 1: every source slide is copied under our object IDs (slide, title and subtitle
    # placeholders, pictures, template shapes), and the sources deleted.
    template_sizes: list[tuple[float, float]] = []
    reqs: list[SlidesRequest] = []
    for slide, source in zip(written_slides, sources):
        request, sizes = plan.copy_request(slide, source)
        template_sizes = template_sizes or sizes
        reqs.append(request)
    # The sources go in the same batch: a layout write reaches every slide inheriting from it and
    # Google charges for each, so the layout pass below costs less with half the slides (3.6 s
    # against 4.7 s on an 86-slide deck, emit 25.9 s against 27.8 s, three interleaved pairs).
    reqs += [{"deleteObject": {"objectId": as_str(s["objectId"], "an imported slide's objectId")}} for s in sources]
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
    ground = mp.ground
    # (the layout pass reads the masters and layouts off the import's own read: `write_layouts`)
    layout_pool, layout_work = None, None
    if threaded:
        layout_pool = ThreadPoolExecutor(1, thread_name_prefix="b2s-layout")
        layout_work = layout_pool.submit(write_layouts, client, pid, written, scale, fonts, ground, pres)
    else:
        write_layouts(client, pid, written, scale, fonts, ground, pres)

    slide_states: list[emit_state.SlideState] = []  # (emit.json's "slides", which the base is read with)
    master_color = master_fill.get("color")
    theme_state = None if not theme else emit_state.ThemeState(
        ground=theme.ground, master=master_color if isinstance(master_color, str) else None,
        decorations={k: str(p.relative_to(out)).replace("\\", "/") if p else None for k, p in theme.decorations.items()},
        layouts={str(k): v for k, v in theme.layouts.items()})
    # (emit.json's "contained": which elements are pictures because emit could not plan them)
    contained = tuple(emit_state.Contained(page=c["page"], id=c["id"], kind=c["kind"], error=c["error"])
                      for c in plan.contained) or None
    # Placeholder sizes (needed to resize them) and any extra layout placeholders. The read is in
    # the air while `measure_places` works: neither needs the other, and all `measure_places`
    # writes is scratch slides of its own (b2s_m...), which an answer may or may not list - every
    # lookup below is by our slides' ids (`slide_parts`).
    def read_copies() -> Presentation:
        return execute(client().presentations().get(
            presentationId=pid,
            fields="slides(objectId,pageElements(objectId,size),slideProperties/notesPage/notesProperties)"))
    reading = pool.submit(read_copies) if pool else None
    moves: Mapping[str, Place] = {}
    scratch: list[str] = []
    if measure:
        moves, scratch = measure_places(slides, pid, written, scale, fonts, plan.placed, plan.page_slide, out, SLIDE_W)
    # The scratch slides have served their purpose once `measure_places` returns: their thumbnails
    # are read and measured, and nothing below looks at them again (the content is planned from
    # `moves`). So they are deleted beside the content rather than after it, on the layout thread,
    # behind its own pass (its batches stay where the race below needs them). A delete that fails
    # is raised where it always was, once the content has landed.
    cleanup: Future[None] | None = None
    if scratch and layout_pool is not None:
        cleanup = layout_pool.submit(delete_scratch, client, pid, scratch)
    created = reading.result() if reading is not None else read_copies()
    copied = created.get("slides")
    if copied is None:
        raise KeyError("slides")  # (the answer always has them: as reading it by key said before)
    page_elements = {object_id(s): [as_json(e, "a copied slide's element") for e in s.get("pageElements", [])]
                     for s in copied}
    speaker_notes = {object_id(s): as_optional_str(s.get("slideProperties", {}).get("notesPage", {})
                                                   .get("notesProperties", {}).get("speakerNotesObjectId"),
                                                   "speakerNotesObjectId")
                     for s in copied}

    # Phase 2: content, batched over slides. Each slide's requests come in parts (one per
    # element) so that a rejected batch can be narrowed down to the element at fault.
    refused: list[tuple[int, str]] = []

    def send(items: list[tuple[str, int, list[Part]]]) -> None:
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
                what = f"{el['kind']} {el['id']}" if el else "request"
                print(f"warning: {slide_id}: {what} rejected "
                      f"({api_error(e)})" + ("; using a picture of it instead" if el and el["kind"] != "image" else ""))
                if el and el["kind"] != "image":
                    refused.append((page, as_str(el["id"], "element id")))

    # Every slide is planned first - local work, done while the layout pass may still be in the
    # air - and the batches then go out all at once (CONTENT_WORKERS at most).
    batches: list[list[tuple[str, int, list[Part]]]] = []
    sent: list[Future[None]] = []
    pending: list[tuple[str, int, list[Part]]] = []
    pending_size = 0
    try:
        for slide in plan.slides():
            n = _page(slide)
            slide_id = _slide_id(n)
            late: list[tuple[int, Exception]] = []
            parts, element_ids = plan.slide_parts(slide, page_elements, speaker_notes, moves, template_sizes, late)
            for i, e in late:
                # Planned with Google's own sizes, an element the rehearsal passed tripped all the
                # same: it goes the way of a refused one, into the rebuild with pictures (`emit`).
                # (a picture is one already, and stays as the .pptx brought it)
                el = _elements(slide)[i]
                print(f"warning: {slide_id}: {el['kind']} {el['id']} could not be planned ({type(e).__name__}: {e})"
                      + ("; using a picture of it instead" if el["kind"] != "image" else ""))
                if el["kind"] != "image":
                    refused.append((n, as_str(el["id"], "element id")))
            size = sum(len(rs) for _, rs in parts)
            # Several slides per round trip; a slide's requests are never split across batches.
            if pending and pending_size + size > BATCH_MAX_REQUESTS:
                batches.append(pending)
                pending = []
                pending_size = 0
            pending.append((slide_id, n, parts))
            pending_size += size
            objects, groups = element_objects(parts, element_ids)
            # the base records the margins: a sync refills such a table in place (sync.table_refill)
            margins: dict[str, emit_state.Margins] | None = {
                str(i): tuple(tuple(m) for m in plan.pptx_table(el).margins)
                for i, el in enumerate(_elements(slide)) if el["kind"] == "table"} if plan.pptx_tables else None
            slide_states.append(emit_state.SlideState(
                page=n, object_id=slide_id, elements=tuple(element_ids), objects=tuple(tuple(o) for o in objects),
                groups=tuple(groups), table_margins=margins))
            kinds = [el["kind"] for el in _elements(slide)]
            print(f"  slide {n + 1}: {kinds.count('text')} text boxes, {kinds.count('image')} pictures, "
                  f"{kinds.count('shape')} shapes, {kinds.count('table')} tables")
        if pending:
            batches.append(pending)
        # The layouts first, and never beside the slides: a slide's title placeholder inherits
        # the layout's box until we give it one of its own, and while the layout batch is in the
        # air together with the content batch that does that, the one Google commits LAST wins -
        # a layout batch landing second takes every title's own box away again and the deck's
        # titles all sit at the layout's, silently. Measured with three conversions at once
        # (`tools/probe_layout_race.py`): the deck whose layout batch landed after its first
        # content batch lost all ten titles, the two that landed first kept theirs. So the
        # layout pass overlaps the read, `measure_places` and the planning above, and no batch.
        if layout_work is not None:
            layout_work.result()
        for items in batches:
            if pool:
                sent.append(pool.submit(send, items))
            else:
                send(items)
        for job in sent:             # every content batch has landed
            job.result()
        if cleanup is not None:      # (and the scratch slides are gone: a failed delete raises here)
            cleanup.result()
    finally:
        for p in (pool, layout_pool):
            if p:
                p.shutdown()
    refused.sort()                   # several threads appended to it
    if scratch and cleanup is None:  # (one client, lent by the caller: one call at a time, last)
        delete_scratch(client, pid, scratch)
    state = emit_state.EmitState(presentation_id=pid, url=f"https://docs.google.com/presentation/d/{pid}/edit",
                                 scale=scale, slides=tuple(slide_states), contained=contained, theme=theme_state,
                                 previous=None)
    return Emitted(state=state, deck=written), refused  # (the deck as built, for the sync snapshot)


def delete_scratch(client: Callable[[], SlidesService], pid: str, scratch: Sequence[str]) -> None:
    """`measure_places`' scratch slides out of the deck, in one batch (`client()`: the Slides
    client of the thread this runs on)."""
    batch(client(), pid, [{"deleteObject": {"objectId": oid}} for oid in scratch])


def created_ids(reqs: Sequence[SlidesRequest]) -> list[str]:
    """Object ids a list of requests creates."""
    out: list[str] = []
    for r in reqs:
        kind = slides_request_kind(r)  # (a request of no kind or several: a ValueError)
        for made in (r.get("createShape"), r.get("createLine"), r.get("createTable"), r.get("createImage"),
                     r.get("createSlide")):
            if made is not None:
                out.append(_named(made.get("objectId"), f"{kind}.objectId"))
        copied = r.get("duplicateObject")
        if copied is not None:
            out += [*copied.get("objectIds", {}).values()]
        group = r.get("groupObjects")
        if group is not None:
            out.append(_named(group.get("groupObjectId"), "groupObjectId"))
    return out


def _named(object_id: str | None, what: str) -> str:
    """The object id a create names (every one emit plans does), or a KeyError naming `what`."""
    if object_id is None:
        raise KeyError(what)
    return object_id


def element_objects(parts: Sequence[tuple[JsonMap | None, Sequence[SlidesRequest]]], element_ids: Sequence[str]
                    ) -> tuple[list[list[str]], list[str]]:
    """Per element (in slide_parts order) every object id created for it: its main object first,
    then what its requests create and its group with anchored pictures ({oid}_g); and the slide's
    other groups (blocks, rules)."""
    objects = [[oid] for oid in element_ids]
    index = {oid: i for i, oid in enumerate(element_ids)}
    groups: list[str] = []
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


PLACEHOLDER_SIZE = (612.0, 90.0)  # the made-up size of a layout placeholder offline (pt)
TEMPLATE_SIZE = (100.0, 100.0)    # and of a template shape's copy

Part = tuple[JsonObject | None, list[SlidesRequest]]
"""A slide's requests in parts (`DeckPlan.slide_parts`): an element's (the element as placed, its
requests), or the slide's own (None, requests), so a rejected batch narrows down to one."""


def _json_list(items: Sequence[JsonObject]) -> list[Json]:
    out: list[Json] = [*items]
    return out


def _page(slide: JsonMap) -> int:
    return as_int(slide["page"], "slide page")


def _elements(slide: JsonMap) -> list[JsonObject]:
    """A slide's elements: the same dicts."""
    return objects_of(slide["elements"], "elements")


def _slide_id(page: int) -> str:
    return f"b2s_s{page:03}"


def _object_id(e: JsonMap) -> str:
    return as_str(e["objectId"], "objectId")


def _magnitude(size: Json, side: str) -> float:
    """A width or height of a page element's size the API answered, as it says it (EMU)."""
    return json_number(as_object(as_object(size, "size")[side], side)["magnitude"], "magnitude")


def _is_placeholder(e: JsonMap) -> bool:
    shape = e.get("shape")
    return isinstance(shape, dict) and "placeholder" in shape


def _placeholder_type(e: JsonMap) -> str:
    return as_str(as_object(as_object(e["shape"], "shape")["placeholder"], "placeholder")["type"], "placeholder type")


def _with_bbox(el: ElementDict, bbox: Sequence[float]) -> ElementDict:
    """`el` (a copy) at another box: the caller's own dict type."""
    new = copy(el)
    box: list[Json] = [*bbox]
    new["bbox"] = box
    return new


def _object_prefix(el: JsonMap) -> str:
    kind = el["kind"]
    return OBJECT_PREFIX.get(kind, "t") if isinstance(kind, str) else "t"


class DeckPlan:
    """The requests build_deck sends, apart from what only Google knows (the imported slides'
    object IDs, placeholder and template sizes, measured hole moves): pure, so tests can check
    them offline (plan_offline).

    The plan's own state is typed: `page_slide` (PDF page -> the slide internal links go to), `keys`
    (the template shapes the .pptx carries) and `uses_templates` per page, the predicted `shifts` of
    formula pictures and `overlays` boxes per page. `deck` (what emit writes: blocks merged, holes
    fitted) and `merged` (the same before the holes are fitted: `emit`'s rebuild) are deck.json
    as JSON, whose slides `slides()` reads; `contained` (`ContainedEntry` per element made a
    picture) is what emit.json writes."""

    page_width: float
    pptx_tables: bool
    scale: float
    fonts: FontMapper
    page_slide: Mapping[int, str]
    contained: list[ContainedEntry]
    merged: JsonObject
    deck: JsonObject
    keys: list[TemplateKey]
    uses_templates: dict[int, bool]
    shifts: dict[int, dict[str, float]]
    overlays: dict[int, dict[str, tuple[float, float]]]
    _scratch: "Slide | None"

    def __init__(self, deck: ObjectMap, page_width: float, *, pptx_tables: bool, contain: bool):
        # `page_width`: the width of the deck this plan is for, in slide pt. A deck `convert` makes
        # is always SLIDE_W wide (it uploads the .pptx that says so), but `sync` may be writing into
        # a deck a person built at any size (`adopt_sync`), and every box, font size and hole width
        # below is this converter's PDF pt times `scale`.
        # `pptx_tables`: tables come with the imported .pptx, empty and with their cell margins
        # (`tables`, build_pptx), and are filled in; else (sync) they are made by createTable. So do
        # the text boxes whose bullets no preset draws (`shells`: beamer's ▶ as itself, not ➢).
        # `contain`: the deck as classify wrote it, whose blocks the plan merges, and an element
        # it cannot plan is the picture of its region (`contain`, listed in `contained`), whose
        # file the caller crops (`crop_fallbacks`). Without it the deck's blocks are merged
        # already and such an element raises.
        whole = dict_of(deck, "deck")
        slides = objects_of(whole["slides"], "slides")
        self.page_width = page_width
        self.pptx_tables = pptx_tables
        self.scale = scale = page_width / json_number(as_array(slides[0]["size"], "size")[0], "size")
        self.fonts = fonts = FontMapper()
        # Internal link targets: PDF page -> slide. A skipped overlay step maps to the kept
        # (last) step of its frame, which comes right after it.
        kept = sorted(_page(s) for s in slides)
        page_slide: dict[int, str] = {}
        for page in range(kept[-1] + 1):
            target = next(k for k in kept if k >= page)
            page_slide[page] = _slide_id(target)
        self.page_slide = page_slide
        self.contained = []  # elements made pictures
        self._scratch = None
        merged = copy(whole)  # (blocks merged, holes not yet fitted: `emit`'s rebuild)
        if contain:
            merged["slides"] = _json_list([self.contain(s) for s in slides])
        self.merged = merged
        fitted = [fit_holes(s, scale, fonts) for s in objects_of(merged["slides"], "slides")]
        self.deck = deck_out = copy(whole)
        deck_out["slides"] = _json_list(fitted)
        self.keys = list(dict.fromkeys(k for s in fitted for e in _elements(s)
                                       for k in element_template_keys_on(e, scale, _bounds(s, page_width, scale))))
        self.uses_templates = {_page(s): any(element_template_keys_on(e, scale, _bounds(s, page_width, scale))
                                             for e in _elements(s)) for s in fitted}
        self.shifts = {_page(s): formula_shifts(s, scale, fonts) for s in fitted}
        self.overlays = {_page(s): overlay_boxes(s, scale, fonts) for s in fitted}

    def slides(self) -> list[JsonObject]:
        """The slides of `deck`, as emit writes them."""
        return objects_of(self.deck["slides"], "slides")

    def contain(self, slide: JsonObject) -> JsonObject:
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
        n = _page(slide)
        elements = _elements(slide)

        def merging(out: set[str]) -> tuple[list[JsonObject], list[tuple[str, Exception]]]:
            return merge_blocks(_swapped(elements, out)), []

        blocks, gone = _settle(elements, merging)
        merged = copy(slide)
        merged["elements"] = _json_list(blocks)

        def rehearsal(out: set[str]) -> tuple[JsonObject, list[tuple[str, Exception]]]:
            s = copy(merged)
            swapped = _swapped(blocks, out)
            s["elements"] = _json_list(swapped)
            return s, [(as_str(swapped[i]["id"], "element id"), e) for i, e in self._rehearse(s)]

        result, more = _settle(blocks, rehearsal)
        kinds: dict[str, str | None] = {}
        for el in elements:
            eid, kind = el.get("id"), el.get("kind")
            if isinstance(eid, str):
                kinds[eid] = kind if isinstance(kind, str) else None
        for eid, e in {**gone, **more}.items():
            self.contained.append({"page": n, "id": eid, "kind": kinds.get(eid), "error": f"{type(e).__name__}: {e}"})
        return result

    def _rehearse(self, slide: JsonObject) -> list[tuple[int, Exception]]:
        """Plan one slide (blocks merged) as build_deck will: [(element index, exception)] for each
        element whose own planning raised. A step over the whole slide raises."""
        scale, fonts = self.scale, self.fonts
        slide = fit_holes(slide, scale, fonts)
        failed: list[tuple[int, Exception]] = []
        for i, el in enumerate(_elements(slide)):  # what the .pptx carries for it (build_pptx)
            try:
                keys = element_template_keys_on(el, scale, _bounds(slide, self.page_width, scale))
                if keys:
                    _add_template_shapes(self._scratch_slide(), keys)
                if self.pptx_tables and el["kind"] == "table":
                    _add_table(self._scratch_slide(), self.pptx_table(el))
                shell = self.text_shell(slide, el) if self.pptx_tables and el["kind"] == "text" else None
                if shell is not None:
                    _add_text_shell(self._scratch_slide(), shell)
            except Exception as e:  # noqa: BLE001 - one element's planning, contained by `contain`
                failed.append((i, e))
        if failed:
            return failed
        plan = _slide_plan(slide, scale, fonts, self.pptx_tables, self.page_slide)
        measure_jobs(plan.deck, scale, fonts, plan.placed, self.page_slide)
        _, sizes, copied = _offline_copy(plan, slide, PLACEHOLDER_SIZE, TEMPLATE_SIZE)
        sid = _slide_id(_page(slide))
        plan.slide_parts(slide, {sid: copied}, {sid: f"{sid}_notes"}, {}, sizes, failed)
        return failed

    def _scratch_slide(self) -> "Slide":
        """A python-pptx slide `_rehearse` puts template shapes and tables on, as build_pptx will."""
        if self._scratch is None:
            from pptx import Presentation
            prs = Presentation()
            self._scratch = prs.slides.add_slide(prs.slide_layouts[TEMPLATE_LAYOUTS["BLANK"]])
        return self._scratch

    def placed(self, el: ElementDict, n: int) -> ElementDict:
        """Inline formula pictures sit over the gap Slides leaves for them (formula_shifts),
        graphics drawn at words over those words (overlay_boxes). (A copy where it moved, of the
        caller's own dict type: sync and measure_places hand theirs in.)"""
        overlays, shifts = self.overlays[n], self.shifts[n]
        eid = el["id"]
        if not isinstance(eid, str):
            return el
        span = overlays.get(eid)
        if span is not None:
            _, y0, _, y1 = box_of(el["bbox"], "bbox")
            return _with_bbox(el, [span[0], y0, span[1], y1])
        dx = shifts.get(eid)
        if dx is None:
            return el
        x0, y0, x1, y1 = box_of(el["bbox"], "bbox")
        return _with_bbox(el, [x0 + dx, y0, x1 + dx, y1])

    def pictures(self, slide: JsonMap) -> list[tuple[JsonObject, list[float]]]:
        """The slide's pictures with their boxes in the .pptx (slide pt)."""
        return [(e, [v * self.scale for v in box_of(self.placed(e, _page(slide))["bbox"], "bbox")])
                for e in _elements(slide) if e["kind"] == "image"]

    def tables(self, slide: JsonMap) -> list[PptxTable]:
        """The slide's tables as the .pptx carries them (pptx_table), when it does."""
        if not self.pptx_tables:
            return []
        return [self.pptx_table(e) for e in _elements(slide) if e["kind"] == "table"]

    def shells(self, slide: JsonMap) -> list[PptxText]:
        """The slide's text shells as the .pptx carries them (text_shell_of), in element order, when
        it carries tables (`pptx_tables`: what convert uploads; sync creates every box itself)."""
        return [shell for _, shell in self._shells(slide)]

    def shell_indices(self, slide: JsonMap) -> list[int]:
        """The indices of the elements `shells` holds a text shell for."""
        return [i for i, _ in self._shells(slide)]

    def _shells(self, slide: JsonMap) -> list[tuple[int, PptxText]]:
        if not self.pptx_tables:
            return []
        title = title_element(slide)
        named = {title, subtitle_element(slide, title) if title is not None else None}  # (layout placeholders)
        out: list[tuple[int, PptxText]] = []
        for i, el in enumerate(_elements(slide)):
            if el["kind"] != "text" or i in named:
                continue
            shell = self.text_shell(slide, el)
            if shell is not None:
                out.append((i, shell))
        return out

    def text_shell(self, slide: JsonMap, el: JsonObject) -> PptxText | None:
        """The text shell of one text element of `slide` (`text_shell_of`); None: it is created."""
        typed = parse_slide_element(el, "background" in slide, f"slide page {_page(slide)}")
        if not isinstance(typed, TextElement):
            return None
        box = box_of(el["bbox"], "bbox")
        return text_shell_of(set_text(typed), (box[0] * self.scale, box[1] * self.scale, box[2] * self.scale,
                                               box[3] * self.scale), self.scale, self.fonts)

    def pptx_table(self, el: ObjectMap) -> PptxTable:
        """The empty table the .pptx carries for a table element. (Its page is SLIDE_W / scale wide,
        as `table_element_requests` takes it: a deck of another width than SLIDE_W is not asked.)"""
        return pptx_table_of(table_of(el), self.scale, self.fonts, SLIDE_W / self.scale)

    def copy_ids(self, slide: JsonMap, source: JsonMap) -> tuple[dict[str, str], list[tuple[float, float]]]:
        """`copy_request`'s objectIds (the source's object id -> ours) and the sizes of the template
        shapes on the source."""
        n = _page(slide)  # PDF page index; slides may skip pages (overlays)
        slide_id = _slide_id(n)
        keys, uses_templates = self.keys, self.uses_templates
        els = objects_of(source.get("pageElements", []), "pageElements")
        placeholders = {_placeholder_type(e): _object_id(e) for e in els if _is_placeholder(e)}
        pictures = [_object_id(e) for e in els if "image" in e]
        tables = [_object_id(e) for e in els if "table" in e]
        # (the text shells, then the template shapes, in the order build_pptx put them on the slide)
        shapes = [e for e in els if "image" not in e and "table" not in e and not _is_placeholder(e)]
        elements = _elements(slide)
        picture_idx = [i for i, e in enumerate(elements) if e["kind"] == "image"]
        table_idx: list[int] = [i for i, e in enumerate(elements) if e["kind"] == "table"] if self.pptx_tables else []
        shell_idx = self.shell_indices(slide)
        templates = len(keys) if uses_templates[n] else 0
        if len(pictures) != len(picture_idx) or len(tables) != len(table_idx) or len(shapes) != len(shell_idx) + templates:
            raise RuntimeError(f"slide {n + 1}: the import brought {len(pictures)} pictures, {len(tables)} tables and "
                               f"{len(shapes)} text shells and template shapes, expected {len(picture_idx)}, "
                               f"{len(table_idx)} and {len(shell_idx)} + {templates}")
        shells, shapes = shapes[:len(shell_idx)], shapes[len(shell_idx):]
        ids = {_object_id(source): slide_id}
        ids.update({oid: f"{slide_id}_f{i}" for oid, i in zip(pictures, picture_idx)})
        ids.update({oid: f"{slide_id}_tab{i}" for oid, i in zip(tables, table_idx)})
        ids.update({_object_id(e): f"{slide_id}_t{i}" for e, i in zip(shells, shell_idx)})
        ids.update({_object_id(e): f"{slide_id}_k{j}" for j, e in enumerate(shapes)})
        title_idx, (_, title_kind) = title_element(slide), slide_layout(slide)
        if title_idx is not None and title_kind is not None:  # (a title is never on the BLANK layout)
            ids[placeholders[title_kind]] = f"{slide_id}_t{title_idx}"
            sub_idx = subtitle_element(slide, title_idx)
            if sub_idx is not None and "SUBTITLE" in placeholders:
                ids[placeholders["SUBTITLE"]] = f"{slide_id}_t{sub_idx}"
        return ids, [size_pt(e) for e in shapes]

    def copy_request(self, slide: JsonMap, source: JsonMap) -> tuple[SlidesRequest, list[tuple[float, float]]]:
        """Phase 1: the duplicateObject copying a slide's imported source under our object IDs,
        and the sizes of the template shapes on the source."""
        ids, sizes = self.copy_ids(slide, source)
        return _duplicate(_object_id(source), ids), sizes

    def slide_parts(self, slide: JsonObject, page_elements: Mapping[str, Sequence[JsonMap]],
                    speaker_notes: Mapping[str, str | None], moves: Mapping[str, Place],
                    template_sizes: Sequence[tuple[float, float]], failed: list[tuple[int, Exception]] | None
                    ) -> tuple[list[Part], list[str]]:
        """Phase 2 for one slide after its copy: requests in parts ((element, requests), so a
        rejected batch can be narrowed down to the element at fault) and the element object IDs.
        `page_elements` and `speaker_notes` describe the copied slides (slide id -> elements with
        objectId and size, speaker notes object id), `moves` are measure_places' results.
        `failed`: an element whose requests raise gets none, and (its index, the exception) goes
        there (`contain`, build_deck); without it, or `strict()`, the exception is raised."""
        scale, fonts, keys = self.scale, self.fonts, self.keys
        placed, page_slide, uses_templates = self.placed, self.page_slide, self.uses_templates
        placeholder_dy = PPTX_TITLE_DY
        # (what every box written here is kept within, as far as its words allow: on_page)
        bounds = _bounds(slide, self.page_width, scale)

        n = _page(slide)
        slide = grown_panels(slide, scale, fonts)
        slide_id = _slide_id(n)
        # The title (and title page subtitle) is refilled in its placeholder only when the slide
        # holds one: sync demotes a title with no live placeholder, and on an adopted slide the
        # next title-role box (`marked.py` gives the role per box) is a box like any other.
        live = {_object_id(e) for e in page_elements.get(slide_id, [])}
        title_idx = title_element(slide)
        title_oid = f"{slide_id}_t{title_idx}" if title_idx is not None else None
        sub_idx = subtitle_element(slide, title_idx) if title_idx is not None else None
        subtitle_oid = f"{slide_id}_t{sub_idx}" if sub_idx is not None else None
        title_oid, subtitle_oid = (oid if oid in live else None for oid in (title_oid, subtitle_oid))
        ours = (f"{slide_id}_k", f"{slide_id}_f", f"{slide_id}_tab")  # template shapes, pictures, tables from the .pptx
        shells = {f"{slide_id}_t{i}" for i in self.shell_indices(slide)} & live  # text shells from the .pptx
        parts: list[Part] = [(None, [
            {"deleteObject": {"objectId": oid}}
            for oid in (_object_id(e) for e in page_elements.get(slide_id, []))
            if oid not in (title_oid, subtitle_oid) and oid not in shells and not oid.startswith(ours)])]

        def template_record(key: TemplateKey) -> Template:
            """The slide's copy of a template shape and its unscaled size (pt)."""
            j = keys.index(key)
            w, h = template_sizes[j]
            return Template(id=f"{slide_id}_k{j}", w=w, h=h)

        def element_requests(el: JsonObject, oid: str) -> list[SlidesRequest]:
            """The requests of one element, planned from its parsed IR: a field its producer never
            wrote or wrote in another type raises `IRError` here, which `failed` contains."""
            typed = parse_slide_element(el, "background" in slide, f"slide page {n}")
            match typed:
                case ShapeElement() | MarkedShape():
                    return shape_element_requests(typed, slide_id, oid, scale, template_record)
                case TableElement():
                    return table_element_requests(typed, slide_id, oid, scale, fonts, self.pptx_tables)
                case DiagramElement():
                    return diagram_requests_of(typed, slide_id, oid, scale, fonts, template_record if keys else None,
                                               bounds, held.get(typed.id, []))
                case ImageElement() if typed.id in in_diagrams:
                    return []  # (its diagram brings it over its box and groups it: diagram_requests_of)
                case ImageElement() | FallbackImage():
                    # The picture came with the slide: move it to its place in the z-order.
                    reqs: list[SlidesRequest] = [
                        {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}}]
                    move = moves.get(typed.id)
                    if move is not None:  # to the gap or words measured for it (measure_places)
                        dx, dy, sx = move.dx, move.dy, 1.0 if move.sx is None else move.sx
                        # (a relative transform scales about the page origin: the left edge keeps its dx)
                        reqs.insert(0, {"updatePageElementTransform": {"objectId": oid, "applyMode": "RELATIVE", "transform": {
                            "scaleX": sx, "scaleY": 1, "unit": "EMU",
                            "translateX": round((dx + (1 - sx) * typed.bbox[0] * scale) * EMU_PER_PT),
                            "translateY": round(dy * EMU_PER_PT)}}})
                    if isinstance(typed, ImageElement) and typed.number is not None:
                        reqs += number_requests(typed.number, slide_id, f"{oid}n", scale, fonts, bounds)
                    return reqs
                case TextElement():
                    # (planned by the entries tests may stand in for, then kept on the page where
                    # that box would reach past it: text_requests_on_page)
                    placeholder = None
                    bar, right_limit = title_bar_under(el, slide), text_right_limit(el, slide)
                    if oid in shells:  # (bullets no preset draws: the .pptx's, text_shell_of)
                        size = next(e["size"] for e in page_elements[slide_id] if e["objectId"] == oid)
                        shell = Shell(base_w=_magnitude(size, "width") / EMU_PER_PT,
                                      base_h=_magnitude(size, "height") / EMU_PER_PT)
                        planned = text_shell_requests(typed, slide_id, oid, scale, fonts, shell, page_slide, bar,
                                                      right_limit, None)
                        return text_requests_on_page(planned, typed, slide_id, oid, scale, fonts, None, shell,
                                                     page_slide, bar, right_limit, bounds)
                    if oid in (title_oid, subtitle_oid):
                        size = next(e["size"] for e in page_elements[slide_id] if e["objectId"] == oid)
                        placeholder = Placeholder(base_w=_magnitude(size, "width") / EMU_PER_PT,
                                                  base_h=_magnitude(size, "height") / EMU_PER_PT, dy=placeholder_dy)
                    planned = text_element_requests(typed, slide_id, oid, scale, fonts, placeholder, page_slide, bar,
                                                    right_limit, None)
                    return text_requests_on_page(planned, typed, slide_id, oid, scale, fonts, placeholder, None,
                                                 page_slide, bar, right_limit, bounds)
                case _:
                    assert_never(typed)

        elements = _elements(slide)
        # The pictures a diagram holds (anchored to it), which go into its groups.
        diagrams = {el["id"] for el in elements if el["kind"] == "diagram"}
        held: dict[str, list[HeldPicture]] = {}
        in_diagrams: set[str] = set()
        for i, el in enumerate(elements):
            anchor = el.get("anchor")
            if el["kind"] == "image" and isinstance(anchor, str) and anchor in diagrams:
                held.setdefault(anchor, []).append(HeldPicture(object_id=f"{slide_id}_{_object_prefix(el)}{i}",
                                                               bbox=box_of(el["bbox"], "picture bbox")))
                in_diagrams.add(as_str(el["id"], "picture id"))
        element_ids: list[str] = []
        for i, el in enumerate(elements):  # shapes, then pictures, then text on top
            oid = f"{slide_id}_{_object_prefix(el)}{i}"
            reqs: list[SlidesRequest]
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
        extra: list[SlidesRequest] = [{"deleteObject": {"objectId": f"{slide_id}_k{j}"}}
                                   for j in range(len(keys)) if uses_templates[n]]
        # Inline formula pictures move with their text: group them (placeholders can't be grouped).
        by_id: dict[str, str] = {}
        for el, oid in zip(elements, element_ids):
            eid = el["id"]
            if isinstance(eid, str):
                by_id[eid] = oid
        anchored: dict[str, list[str]] = {}
        grouped: set[str] = set()
        tops: dict[str, str] = {}  # object id -> the group it went into
        for el, oid in zip(elements, element_ids):
            anchor = el.get("anchor")
            if isinstance(anchor, str) and anchor in diagrams:
                grouped.add(oid)  # (in its diagram's group already)
                tops[oid] = by_id[anchor]
            elif isinstance(anchor, str) and anchor in by_id and by_id[anchor] not in (title_oid, subtitle_oid):
                anchored.setdefault(by_id[anchor], []).extend([oid, f"{oid}n"] if el.get("number") else [oid])
        for text_oid, pictures in anchored.items():
            extra.append({"groupObjects": {"groupObjectId": f"{text_oid}_g", "childrenObjectIds": [text_oid, *pictures]}})
            grouped |= {text_oid, *pictures}
            tops.update((m, f"{text_oid}_g") for m in (text_oid, *pictures))
        rules = rule_groups(elements, element_ids)
        # A beamer block (title bar and body shapes plus everything on them) moves as one.
        blocks: list[tuple[str, list[str]]] = []
        for bi, members in enumerate(block_groups(elements, element_ids, title_oid, {m for g in rules for m in g})):
            children = [f"{m}_g" if m in anchored else m for m in members if m not in grouped or m in anchored]
            if len(children) >= 2:
                extra.append({"groupObjects": {"groupObjectId": f"{slide_id}_blk{bi}", "childrenObjectIds": children}})
                blocks.append((f"{slide_id}_blk{bi}", children))
        for ri, members in enumerate(rules):
            extra.append({"groupObjects": {"groupObjectId": f"{slide_id}_rules{ri}", "childrenObjectIds": [*members]}})
            tops.update((m, f"{slide_id}_rules{ri}") for m in members)
        # A group takes the place of its topmost member, above a table lying on the block (tables
        # can't join the group): blocks are backdrops, sent back where their first panel stood.
        for oid in block_stacking(elements, element_ids, blocks, tops):
            extra.append({"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "SEND_TO_BACK"}})
        notes_id = speaker_notes.get(slide_id)
        if slide.get("notes") and notes_id:
            extra.append({"insertText": {"objectId": notes_id, "text": as_str(slide["notes"], "notes")}})
        if title_oid and len(elements) > 1:
            # The placeholder was created with the slide, below everything added since.
            extra.append({"updatePageElementsZOrder": {"pageElementObjectIds": [o for o in (title_oid, subtitle_oid) if o],
                                                       "operation": "BRING_TO_FRONT"}})
        parts += [(None, [r]) for r in extra]
        return parts, element_ids


def _duplicate(source: str, ids: Mapping[str, str]) -> SlidesRequest:
    """The duplicateObject copying an imported source slide under our object ids."""
    return {"duplicateObject": {"objectId": source, "objectIds": dict(ids)}}


class OfflinePlan(TypedDict):
    """`plan_offline`'s answer."""
    plan: DeckPlan
    pictures: dict[int, list[tuple[JsonObject, list[float]]]]
    copies: list[SlidesRequest]
    page_elements: dict[str, list[JsonObject]]
    speaker_notes: dict[str, str]
    measure: list[SlidesRequest]
    slides: list[tuple[str, int, list[Part], list[str]]]
    contained: list[ContainedEntry]


def plan_offline(deck: ObjectMap) -> OfflinePlan:
    """What emit would send for a classified deck, without Google: the imported slides are made
    up as the .pptx brings them (layout placeholders, pictures, template shapes) and hole
    pictures keep their predicted places. {"plan": DeckPlan, "pictures": {page: [(element, .pptx
    box)]}, "copies": phase 1 requests, "page_elements" and "speaker_notes": the copied slides,
    "measure": measure_places' scratch slide requests, "slides": [(slide id, page, parts, element ids)],
    "contained": the elements it could not plan, pictures now (`DeckPlan.contain`)}. `deck`: as
    classify wrote it (the plan merges its blocks). The made-up sizes are PLACEHOLDER_SIZE and
    TEMPLATE_SIZE."""
    plan = DeckPlan(deck, SLIDE_W, pptx_tables=True, contain=True)
    copies: list[SlidesRequest] = []
    page_elements: dict[str, list[JsonObject]] = {}
    template_sizes: list[tuple[float, float]] = []
    for slide in plan.slides():
        request, sizes, copied = _offline_copy(plan, slide, PLACEHOLDER_SIZE, TEMPLATE_SIZE)
        copies.append(request)
        template_sizes = template_sizes or sizes
        page_elements[_slide_id(_page(slide))] = copied
    speaker_notes = {slide_id: f"{slide_id}_notes" for slide_id in page_elements}
    slides: list[tuple[str, int, list[Part], list[str]]] = []
    for slide in plan.slides():
        parts, element_ids = plan.slide_parts(slide, page_elements, speaker_notes, {}, template_sizes, None)
        slides.append((_slide_id(_page(slide)), _page(slide), parts, element_ids))
    return {"plan": plan, "pictures": {_page(s): plan.pictures(s) for s in plan.slides()}, "copies": copies,
            "page_elements": page_elements, "speaker_notes": speaker_notes,
            "measure": measure_jobs(plan.deck, plan.scale, plan.fonts, plan.placed, plan.page_slide)[0], "slides": slides,
            "contained": plan.contained}


def _offline_copy(plan: DeckPlan, slide: JsonObject, placeholder_size: tuple[float, float],
                  template_size: tuple[float, float]) -> tuple[SlidesRequest, list[tuple[float, float]], list[JsonObject]]:
    """A slide's phase 1 without Google (`plan_offline`, `DeckPlan._rehearse`): its source slide made
    up as the .pptx brings it (layout placeholders, pictures, tables, template shapes, at made-up
    sizes). (copy_request's request, the template shapes' sizes, the copied slide's elements)"""
    def size(w: float, h: float) -> JsonObject:
        return {"width": emu_json(w), "height": emu_json(h)}

    n = _page(slide)
    source = f"src{n:03}"
    els: list[JsonObject] = [
        {"objectId": f"{source}_{kind}", "size": size(*placeholder_size), "shape": {"placeholder": {"type": kind}}}
        for kind in LAYOUT_PLACEHOLDERS[slide_layout(slide)[0]]]
    els.extend({"objectId": f"{source}_p{i}", "size": size(1, 1), "image": {}} for i, _ in enumerate(plan.pictures(slide)))
    els.extend({"objectId": f"{source}_tb{i}", "size": size(sum(t.widths), sum(t.heights)), "table": {}}
               for i, t in enumerate(plan.tables(slide)))
    els.extend({"objectId": f"{source}_sh{i}", "size": size(t.box[2] - t.box[0], t.box[3] - t.box[1]), "shape": {}}
               for i, t in enumerate(plan.shells(slide)))
    els.extend({"objectId": f"{source}_k{j}", "size": size(*template_size), "shape": {}}
               for j in range(len(plan.keys) if plan.uses_templates[n] else 0))
    ids, sizes = plan.copy_ids(slide, {"objectId": source, "pageElements": _json_list(els)})
    copied: list[JsonObject] = [{"objectId": ids.get(_object_id(e), f"{_object_id(e)}_copy"), "size": e["size"]}
                                for e in els]
    return _duplicate(source, ids), sizes, copied


def _bounds(slide: JsonMap, page_width: float, scale: float) -> Page:
    """The page a slide's objects are kept within (`on_page`, `arc_plan`), slide pt."""
    return Page(width=page_width, height=json_number(as_array(slide["size"], "size")[1], "size") * scale)


def _slide_plan(slide: JsonObject, scale: float, fonts: FontMapper, pptx_tables: bool,
                page_slide: Mapping[int, str]) -> DeckPlan:
    """A DeckPlan of one slide (holes fitted), with none of the deck-wide work __init__ does: the
    template shapes are the slide's own (`slide_emission`, `DeckPlan._rehearse`)."""
    n = _page(slide)
    page_width = json_number(as_array(slide["size"], "size")[0], "size") * scale
    keys = list(dict.fromkeys(k for e in _elements(slide)
                              for k in element_template_keys_on(e, scale, _bounds(slide, page_width, scale))))
    plan = DeckPlan.__new__(DeckPlan)
    plan.page_width = page_width
    plan.pptx_tables, plan.scale, plan.fonts = pptx_tables, scale, fonts
    plan.deck = {"slides": [slide]}
    plan.keys, plan.uses_templates = keys, {n: bool(keys)}
    plan.shifts = {n: formula_shifts(slide, scale, fonts)}
    plan.overlays = {n: overlay_boxes(slide, scale, fonts)}
    plan.page_slide = page_slide
    plan.contained = []
    plan._scratch = None
    plan.merged = plan.deck
    return plan


def _swapped(elements: Sequence[JsonObject], gone: AbstractSet[str]) -> list[JsonObject]:
    """`elements` with each whose id is in `gone` the picture of its region, where it stands."""
    return [fallback_element(e) if e.get("id") in gone else e for e in elements]


def _settle(elements: Sequence[JsonMap], attempt: Callable[[set[str]], tuple[_T, list[tuple[str, Exception]]]]
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


def _can_picture(el: JsonMap) -> bool:
    """Whether `fallback_element` can make a picture of the element (an id, a box)."""
    try:
        fallback_element(el)
    except Exception:  # noqa: BLE001
        return False
    return True


def _culprit(elements: Sequence[JsonMap], gone: Mapping[str, Exception], attempt: Callable[[set[str]], object],
             error: Exception) -> str | None:
    """The element a step over the whole slide failed on (`error`), None when none is found.

    First the suspects (`_suspects`: the elements the failing code was holding, one lacking the
    key of a KeyError first), each made a picture alone: the first after which the step no longer
    fails the same way, on the same suspect, is blamed. (Passing is too much to ask: two panels
    missing the same `flip` fail alike, and each is to blame.) Else the elements are made pictures
    one after the other in slide order, and the one whose picture lets `attempt` through is."""
    # (one with no id or box to make a picture of is never blamed, so never lost)
    ids: list[str] = []
    for el in elements:
        eid = el.get("id")
        if isinstance(eid, str) and eid not in gone and _can_picture(el):
            ids.append(eid)

    def signature(e: Exception) -> tuple[type[Exception], tuple[object, ...], str | None]:
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
    frames: list[FrameType] = []
    tb = error.__traceback__
    while tb is not None:
        frames.append(tb.tb_frame)
        tb = tb.tb_next
    found: list[str] = []
    for frame in reversed(frames):
        held: list[JsonObject] = []
        for v in frame.f_locals.values():
            if isinstance(v, dict) and "kind" in v and v.get("id") in ids:
                held.append(v)
        for d in sorted(held, key=lambda d: key is None or key in d):
            eid = d["id"]
            if isinstance(eid, str) and eid not in found:
                found.append(eid)
    return found


class _OnePage(Mapping[int, str]):
    """`page_slide` for `slide_emission`: every internal link goes to one stand-in slide. Which
    slide a link names is the element's own IR (`identity.ir_fields` keys it), not its neighbours'.
    (Empty as a mapping, as it has always been; only `get` and truth are asked of it.)"""

    @override
    def __getitem__(self, page: int) -> str:
        return "b2s_link"

    @override
    def __contains__(self, page: object) -> bool:
        return False

    @override
    def __iter__(self) -> Iterator[int]:
        return iter(())

    @override
    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return True


class Emission(TypedDict):
    """`slide_emission`'s answer."""
    slide_id: str
    parts: list[Part]
    element_ids: list[str]
    boxes: list[list[float] | None]
    title: int | None
    subtitle: int | None
    templates: dict[str, TemplateKey]


def slide_emission(slide: JsonObject, scale: float, fonts: FontMapper) -> Emission:
    """What emit writes for one slide of a DeckPlan (blocks merged, holes fitted: `DeckPlan.deck`),
    worked out from that slide alone, so the same slide gives the same answer in whichever deck it
    stands: {"slide_id", "parts" and "element_ids" (`DeckPlan.slide_parts`), "boxes" (each
    picture's predicted place in slide pt, None for other kinds), "title" and "subtitle" (element
    indices of the layout placeholders' texts), "templates" ({object id of the slide's copy: its
    template key})}. As in `plan_offline` the layout's placeholders and the template shapes have
    made-up sizes (PLACEHOLDER_SIZE, TEMPLATE_SIZE); unlike it, links to other slides all go to one page and measure_places' moves
    are left out (only Google's renderer knows them), so pictures keep their predicted places.
    For sync.mark_emitted, which compares a base slide's emission with the new one's.

    Nothing is contained here (`DeckPlan.contain`): the slide is a plan's, whose elements emit
    could not plan are pictures already, the same ones every time; and a base slide an older
    converter wrote that today's emit cannot plan has to raise, which mark_emitted reads as
    "nothing to compare" - a picture in its place would be a change the source never made."""
    n = _page(slide)
    slide_id = _slide_id(n)
    plan = _slide_plan(slide, scale, fonts, False, _OnePage())
    keys = plan.keys
    title = title_element(slide)
    subtitle = subtitle_element(slide, title) if title is not None else None
    w, h = PLACEHOLDER_SIZE
    page_elements: dict[str, list[JsonObject]] = {
        slide_id: [{"objectId": f"{slide_id}_t{i}", "size": {"width": emu_json(w), "height": emu_json(h)}}
                   for i in (title, subtitle) if i is not None]}
    parts, element_ids = plan.slide_parts(slide, page_elements, {}, {}, [TEMPLATE_SIZE] * len(keys), None)
    boxes: list[list[float] | None] = [
        [v * scale for v in box_of(plan.placed(e, n)["bbox"], "bbox")] if e["kind"] == "image" else None
        for e in _elements(slide)]
    return {"slide_id": slide_id, "parts": parts, "element_ids": element_ids, "boxes": boxes, "title": title,
            "subtitle": subtitle, "templates": {f"{slide_id}_k{j}": k for j, k in enumerate(keys)}}
