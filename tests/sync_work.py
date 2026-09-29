"""What the tests hand `Sync`'s phases, written as the dicts they have always written: a slide
plan as `merge.slide_plan_json` writes it (the fields a test's reader never reads may be left
out), and a slide's work as `Sync.prepare` / `update_slide` fill it in."""

from beamer2slides import merge, sync


def plan(p):
    """A hand-written plan (or a `merge.plan_merge` one) as the record `sync` reads; one that says
    no action is an update (the only kind the phases a test drives read)."""
    if not isinstance(p, dict):
        return p
    return sync.slide_plan_of({"key": "s", "base": 0, "ours": 0, "action": "update", "units": [], **p}, "plan")


def slide_work(w):
    """A `SlideWork` from {"plan", "sid", "units", "objects", "new_oid", "in_place", "tops",
    "doomed", "groups"}, any of them but the plan left out."""
    p = plan(w["plan"])
    sw = sync.SlideWork(p, w["sid"] if "sid" in w else sync.live_id(p), list(w.get("units", [])))
    sw.objects = dict(w.get("objects", {}))
    sw.new_oid = dict(w.get("new_oid", {}))
    sw.in_place = dict(w.get("in_place", {}))
    sw.tops = dict(w.get("tops", {}))
    sw.doomed = set(w.get("doomed", ()))
    sw.groups = list(w.get("groups", []))
    return sw


def work(slides, order=(), writes=True, pictures=None):
    """A `Work` of `slides` (dicts for `slide_work`, or `SlideWork`s)."""
    return sync.Work(slides=[w if isinstance(w, sync.SlideWork) else slide_work(w) for w in slides],
                     pictures=dict(pictures or {}), new_ids={}, page_slide={}, writes=writes, order=list(order))


def merge_plan(mplan):
    """A `merge.plan_merge` plan as the record `sync` reads, with its slides in the same order."""
    return merge.MergePlan(slides=tuple(plan(p) for p in mplan["slides"]), order=tuple(mplan.get("order", ())),
                           report=merge.Report())


def run_result(slides, theirs):
    """What `Sync.run` hands `new_base`: `slides` (as `work` takes them) planned against `theirs`."""
    w = work(slides)
    mplan = merge.MergePlan(slides=tuple(x.plan for x in w.slides), order=(), report=merge.Report())
    return sync.RunResult(attempts=1, plan=mplan, work=w, theirs=theirs, revision_id=None)


def bare_sync(**attrs):
    """A `Sync` with no deck behind it: its per-run state as `__init__` starts it, and `attrs`."""
    s = sync.Sync.__new__(sync.Sync)
    s.warnings, s.overruns, s.refit_moves, s.urls, s.recovery = [], [], [], {}, sync.no_recovery()
    s.cleanup_ids, s.cleanup_requests, s.in_place_readback, s.final_revision = [], [], {}, None
    s.theme_side, s.theme_plan, s.theme_applied, s.theme_conflicts, s.raw_after = None, None, [], [], None
    s.created, s.staging, s.picture_reads, s.live_read, s.reshaped = {}, None, {}, None, {}
    s.sent, s.deleting, s.first_read, s.way_back, s.trust_generation = {}, [], None, None, True
    s.drive, s.slides = None, None  # (no Google services: a test that needs one hands it in)
    for k, v in attrs.items():
        setattr(s, k, v)
    return s


def with_ours(s, built):
    """Hand `s` the new conversion `built` (`sync.build_ours_of`) as `Sync.__init__` takes it: its JSON
    view (returned, for a test to change what the phases read), plan, scale, source and folder."""
    s.ours, s.plan, s.scale = sync.ours_json(built), built.plan, built.plan.scale
    s.source, s.ours_out = built.source, built.out
    return s.ours
