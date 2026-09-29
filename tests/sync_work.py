"""What the tests hand `Sync`'s phases: a slide plan as `merge.slide_plan_json` writes it (the fields
a test's reader never reads may be left out) made the record `sync` reads, and a slide's work as
`Sync.prepare` / `update_slide` fill it in - a `SlideWork` whose fields a test sets itself."""

from collections.abc import Mapping, Sequence

from beamer2slides import merge, sync
from beamer2slides.json_types import JsonObject

from .fake_google import NoDrive, NoSlides

PlanLike = JsonObject | merge.SlidePlan


def plan(p: PlanLike) -> merge.SlidePlan:
    """A hand-written plan (or a `merge.plan_merge` one) as the record `sync` reads; one that says
    no action is an update (the only kind the phases a test drives read)."""
    if not isinstance(p, dict):
        return p
    return sync.slide_plan_of({"key": "s", "base": 0, "ours": 0, "action": "update", "units": [], **p}, "plan")


def slide_work(p: PlanLike) -> sync.SlideWork:
    """The `SlideWork` of plan `p` on the slide its plan names, nothing made on it yet: a test sets
    what the phase it drives reads (`sid`, `units`, `objects`, `new_oid`, `in_place`, `tops`,
    `doomed`, `groups`)."""
    sp = plan(p)
    return sync.SlideWork(sp, sync.live_id(sp), [])


def work(slides: Sequence[sync.SlideWork], order: Sequence[str]) -> sync.Work:
    """A `Work` of `slides` that writes, in slide order `order`, with no pictures to stage."""
    return staged(slides, order, {})


def staged(slides: Sequence[sync.SlideWork], order: Sequence[str], pictures: Mapping[str, str | None]) -> sync.Work:
    """A `Work` of `slides` bringing `pictures` (file -> "background" or a figure kind) to stage."""
    return sync.Work(slides=list(slides), pictures=dict(pictures), new_ids={}, page_slide={}, writes=True,
                     order=list(order))


def run_result(slides: Sequence[sync.SlideWork], theirs: JsonObject) -> sync.RunResult:
    """What `Sync.run` hands `new_base`: `slides` planned against `theirs`."""
    w = work(slides, ())
    mplan = merge.MergePlan(slides=tuple(x.plan for x in w.slides), order=(), report=merge.Report())
    return sync.RunResult(attempts=1, plan=mplan, work=w, theirs=theirs, revision_id=None)


def bare_sync() -> sync.Sync:
    """A `Sync` with no deck behind it: its per-run state as `__init__` starts it. Its Google
    services (`drive`, `slides`) refuse every call (`fake_google`): a test that needs one hands it in."""
    s = sync.Sync.__new__(sync.Sync)
    s.slides = NoSlides()
    s.drive = NoDrive()
    s.warnings = []
    s.overruns = []
    s.refit_moves = []
    s.urls = {}
    s.recovery = sync.no_recovery()
    s.cleanup_ids = []
    s.cleanup_requests = []
    s.in_place_readback = {}
    s.final_revision = None
    s.theme_side, s.theme_plan, s.raw_after = None, None, None
    s.theme_applied = []
    s.theme_conflicts = []
    s.created = {}
    s.staging = None
    s.picture_reads = {}
    s.live_read = None
    s.reshaped = {}
    s.sent = {}
    s.deleting = []
    s.first_read, s.way_back, s.trust_generation = None, None, True
    return s


def with_ours(s: sync.Sync, built: sync.Built) -> JsonObject:
    """Hand `s` the new conversion `built` (`sync.build_ours_of`) as `Sync.__init__` takes it: its JSON
    view (returned, for a test to change what the phases read), plan, scale, source and folder."""
    s.ours, s.plan, s.scale = sync.ours_json(built), built.plan, built.plan.scale
    s.source, s.ours_out = built.source, built.out
    return s.ours
