import copy
import json
import logging
import math
import re
from datetime import date, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import Error, connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from .ai import (
    AIUnavailableError,
    _number_projects_and_tasks,
    generate_closeout_summary,
    generate_weekly_summary,
    resolve_weekly_summary,
)
from .closeout import get_latest_closeout, is_week_closed, save_closeout
from .date_format import (
    MONTHS_DE,
    WEEKDAYS_SHORT,
    format_date,
    format_week_range,
)
from .dates import is_same_iso_week, iso_week_bounds
from .demo_data import get_demo_projects, get_demo_unassigned_tasks
from .models import DemoEvent
from .notion import (
    NotionUnavailableError,
    get_unassigned_tasks,
    get_upcoming_projects,
    increment_postpone_count,
    toggle_task,
    update_task_date,
)

logger = logging.getLogger(__name__)

# Both cache keys and SUMMARY_KEY store the summary's raw reference dict
# (#122), so they carry a version that is bumped on every format change
# (#20: v2/v4; #122: v3/v5; #140: v4/v6 — task order changed, and pre-deploy
# task_refs were numbered against the unsorted order; #160: v5 — the stored
# projects are annotated, and a pre-deploy entry would keep rendering open
# undated tasks as done; #169/#171: v6 — urgent is calendar-week based now,
# and task dicts carry a new postpone_count; #19: v7 — task dicts carry a
# new completed_date, and a pre-deploy entry would undercount "done this
# week" until it expires; #189: v8 — task dicts no longer carry
# due_display, see below; #210: v9 — task dicts carry a new kanban_column,
# and a pre-deploy entry would render an empty Kanban board) — otherwise a
# pre-deploy entry in the old shape would crash or misrender under the new
# resolver.
#
# #189 is the one bump that is not correctness-critical: a pre-deploy entry
# still renders right, because the leftover due_display is simply no longer
# read. It is bumped anyway so that STALE_CACHE_KEY, which never expires,
# cannot keep serving a shape no code writes any more — and because a
# formatted date living in this cache was the bug in the first place.
CACHE_KEY = "dashboard_data_v9"
CACHE_TTL = 60 * 60 * 8  # 8 hours
# Written alongside CACHE_KEY on every successful fetch, never expired — the
# fallback dashboard() serves when a fresh Notion read fails and the primary
# entry has already expired. See DashboardNotionFailureTest.
STALE_CACHE_KEY = "dashboard_data_stale_v9"

# #53: a separate key pair rather than folded into CACHE_KEY's tuple — this
# is an independent Notion read (get_unassigned_tasks carries no AI summary,
# no partial-success nuance) and keeping it apart avoids reshaping the
# already-tested primary tuple for every existing cache test.
# #19: v2 — task dicts carry the same new completed_date field.
# #189: v3 — and they lost due_display in the same way, bumped in lockstep
# with CACHE_KEY as #19 established.
# #210: v4 — and they gained kanban_column, bumped in the same lockstep.
UNASSIGNED_CACHE_KEY = "dashboard_unassigned_v4"
UNASSIGNED_CACHE_TTL = 60 * 60 * 8  # 8 hours, same as CACHE_TTL
STALE_UNASSIGNED_CACHE_KEY = "dashboard_unassigned_stale_v4"

# #216: the moment each live entry falls due, stamped when a fresh Notion
# read fills it and never touched afterwards. Django's cache API offers no
# portable "how long has this entry got left", so without a stamp every
# re-write has to name a timeout — and #199 turned a toggle from a delete
# into a re-write. Naming CACHE_TTL there would renew the eight hours on
# every checkbox: check one task off per working day and the dashboard
# never performs an unforced Notion read again, so anything edited in
# Notion's own UI (which _count_done_in_range explicitly expects) stays
# invisible for as long as the patching continues. The TTL is a
# freshness policy about the *read*, not about the last write.
#
# Unversioned: these hold a bare deadline, no task shape, so a pre-deploy
# entry cannot misrender. They are absent rather than wrong before the
# first fetch after a deploy, and absent means "cannot patch safely".
CACHE_DEADLINE_KEY = "dashboard_data_deadline"
UNASSIGNED_CACHE_DEADLINE_KEY = "dashboard_unassigned_deadline"


def _bust_dashboard_cache():
    """Called after every confirmed Notion write, so a completed task or a
    freshly saved project never hides behind CACHE_TTL or the stale fallback."""
    cache.delete(CACHE_KEY)
    cache.delete(STALE_CACHE_KEY)
    cache.delete(UNASSIGNED_CACHE_KEY)
    cache.delete(STALE_UNASSIGNED_CACHE_KEY)
    cache.delete(CACHE_DEADLINE_KEY)
    cache.delete(UNASSIGNED_CACHE_DEADLINE_KEY)


def _cache_fresh_read(key, value, deadline_key, ttl):
    """Stores a fresh Notion read together with the moment it falls due.

    The only place a dashboard entry's life is allowed to start over
    (#216). Every later re-write of the same data — a patched toggle, a
    regenerated summary — goes through _remaining_ttl instead, so it can
    put the entry back without pushing its expiry out."""
    cache.set(key, value, ttl)
    cache.set(deadline_key, timezone.now() + timedelta(seconds=ttl), ttl)


def _remaining_ttl(deadline_key):
    """Seconds left before the entry `deadline_key` describes falls due, or
    None when it cannot be re-written without outliving that moment — no
    deadline recorded (a first request after a deploy), or one already
    passed. None is the caller's cue to fall back, never to guess a TTL."""
    deadline = cache.get(deadline_key)
    if deadline is None:
        return None
    remaining = (deadline - timezone.now()).total_seconds()
    return remaining if remaining > 0 else None


def _summary_ref_order(projects):
    """The identity sequence a summary's project_ref / task_refs are
    positions in. Read through _number_projects_and_tasks (ai.py) rather
    than rebuilt here, so the check cannot drift from the numbering it is
    checking."""
    numbered_projects, numbered_tasks = _number_projects_and_tasks(projects)
    return [p["id"] for p in numbered_projects], [t["id"] for t in numbered_tasks]


def _attach_regenerated_summary(numbered_against, summary_data):
    """Writes a freshly generated summary onto the projects the cache holds
    *now*, never onto the snapshot the generating request opened with
    (#216).

    generate_weekly_summary takes seconds. Writing back the projects read
    before it would undo a toggle that patched the cache inside that
    window — the entry would go on serving a task as open that Notion has
    as done, which is the one thing _patch_cached_tasks promises against.
    Only the summary is this request's to contribute.

    The summary is dropped rather than written when the numbering it was
    built against no longer holds. task_refs are positions in that order,
    so a reschedule landing during the call would leave them pointing at
    the wrong tasks — in range, and therefore rendered rather than dropped
    by resolve_weekly_summary. A toggle moves nothing (`done` is not in
    _annotate_tasks' sort key), so its patch keeps the order and the
    summary still fits. A cache busted meanwhile is not resurrected
    either: writing it back would restore the state the bust discarded."""
    current = cache.get(CACHE_KEY)
    if current is None:
        return
    projects = current[0]
    if _summary_ref_order(projects) != _summary_ref_order(numbered_against):
        return
    remaining = _remaining_ttl(CACHE_DEADLINE_KEY)
    if remaining:
        cache.set(CACHE_KEY, (projects, summary_data), remaining)
    cache.set(STALE_CACHE_KEY, (projects, summary_data), None)


def _find_task(projects, unassigned_tasks, task_id):
    """Returns (task, project) — the one place that knows a task can live in
    either of the dashboard's two independently cached lists. `project` is
    None for a task with no project of its own ("Ohne Projekt", #53)."""
    for project in projects:
        for task in project["tasks"]:
            if task["id"] == task_id:
                return task, project
    for task in unassigned_tasks:
        if task["id"] == task_id:
            return task, None
    return None, None


def _patch_cached_tasks(task_id, mutate, today, drop_summary=False):
    """Applies `mutate(task)` to every cached copy of one task and re-runs
    the cheap derivations on top of it, instead of throwing the whole cache
    away after a confirmed Notion write (#199).

    Returns (projects, unassigned_tasks) — the patched data the caller then
    derives its answer from — or None when the write cannot be reflected
    safely, in which case the caller busts the cache as before. That
    fallback is the normal path, not an edge case: a cold cache, a half-cold
    one, a task no cached list carries, or an entry whose deadline has run
    out from under it all take it.

    `drop_summary` is for the writes that renumber the summary's task_refs
    (a reschedule moves the task in the chronological order, #140); a toggle
    moves nothing, so its summary survives untouched.
    """
    primary = cache.get(CACHE_KEY)
    unassigned = cache.get(UNASSIGNED_CACHE_KEY)
    if primary is None or unassigned is None:
        # Half a picture is no picture: every figure the dashboard renders
        # is counted across both lists.
        return None
    # #216: read before the write, and separately per pair — the two are
    # independent Notion reads and their deadlines drift apart whenever one
    # of them fails alone. Without both, this write would have to name a
    # fresh TTL and renew the entry's eight hours; the bust it falls back
    # to costs one Notion read and keeps the freshness policy intact.
    primary_ttl = _remaining_ttl(CACHE_DEADLINE_KEY)
    unassigned_ttl = _remaining_ttl(UNASSIGNED_CACHE_DEADLINE_KEY)
    if primary_ttl is None or unassigned_ttl is None:
        return None
    projects, summary_data = primary
    task, project = _find_task(projects, unassigned, task_id)
    if task is None:
        return None
    mutate(task)
    # Both lists are re-annotated regardless of which one held the task:
    # _annotate_tasks is a pure re-derivation over data already in memory,
    # and paying it twice is cheaper than tracking where the task lived.
    projects = _annotate_tasks(projects, today)
    unassigned = _annotate_tasks([{"id": "_unassigned", "tasks": unassigned}], today)[
        0
    ]["tasks"]
    cache.set(
        CACHE_KEY, (projects, None if drop_summary else summary_data), primary_ttl
    )
    cache.set(UNASSIGNED_CACHE_KEY, unassigned, unassigned_ttl)
    # `project is None` for a task that was found means it came out of the
    # project-less list — which is the half of the stale pair the write
    # concerns, see _patch_stale_copies.
    _patch_stale_copies(
        task_id, mutate, today, drop_summary, in_project=project is not None
    )
    return projects, unassigned


def _patch_stale_copies(task_id, mutate, today, drop_summary, in_project):
    """The never-expiring last-known-good entry for the list this write
    concerns, patched in step with the live one.

    Each cache.get hands back its own deserialized object graph, so the
    write has to be applied to this one separately. It can be older than the
    live pair (which expires and gets refilled while it does not), so a
    snapshot predating the task itself cannot be corrected — it is dropped
    rather than left to serve a state older than a confirmed write.

    Only the entry that would carry this task is looked at, hence
    `in_project`. A project task is never in STALE_UNASSIGNED_CACHE_KEY and
    a project-less one is never in STALE_CACHE_KEY, so a miss in one says
    nothing about the other — and the two exist without each other often
    enough for that to matter: dashboard() writes STALE_CACHE_KEY only when
    the summary is not None, so a single Claude outage leaves the
    project-less copy alone in the cache. Judging them together dropped a
    last-known-good copy this write had never touched, in both directions
    (the costlier one being a project-less toggle discarding the projects
    *and* the summary the last Claude call paid for).
    """
    if in_project:
        stale = cache.get(STALE_CACHE_KEY)
        task, _ = _find_task(stale[0] if stale else [], [], task_id)
        if task is None:
            cache.delete(STALE_CACHE_KEY)
            return
        mutate(task)
        stale_projects, stale_summary = stale
        cache.set(
            STALE_CACHE_KEY,
            (
                _annotate_tasks(stale_projects, today),
                None if drop_summary else stale_summary,
            ),
            None,
        )
        return

    stale_unassigned = cache.get(STALE_UNASSIGNED_CACHE_KEY)
    task, _ = _find_task([], stale_unassigned or [], task_id)
    if task is None:
        cache.delete(STALE_UNASSIGNED_CACHE_KEY)
        return
    mutate(task)
    # drop_summary is not read here: the summary numbers project tasks only
    # (_number_projects_and_tasks, ai.py), so moving a project-less task
    # renumbers nothing and the copy's own summary stays valid.
    cache.set(
        STALE_UNASSIGNED_CACHE_KEY,
        _annotate_tasks([{"id": "_unassigned", "tasks": stale_unassigned}], today)[0][
            "tasks"
        ],
        None,
    )


# Session key prefix for demo summaries; planner_create clears every version
# by the unversioned "demo_plan_summary" prefix when a new plan is generated.
SUMMARY_KEY = "demo_plan_summary_v6"
# The multi-project demo summary: get_demo_projects() is a pure function of
# timezone.localdate() and holds no per-visitor data, so one Claude call per day serves
# every visitor. The day is part of the key, so a rollover invalidates by
# itself and the TTL only bounds how long one day's entry lives. The cache is
# shared across both gunicorn workers (DatabaseCache, settings.py CACHES, #52),
# so expect up to one call per day rather than one per worker.
DEMO_MULTI_SUMMARY_KEY = "demo_multi_summary_v3"
DEMO_MULTI_SUMMARY_TTL = 60 * 60 * 24

# The sidebar progress ring's geometry (#76): radius never varies, so the
# circumference is a fixed stroke-dasharray and only stroke-dashoffset moves.
RING_RADIUS = 7
RING_CIRCUMFERENCE = round(2 * math.pi * RING_RADIUS, 2)  # 43.98


# Project urgency is the highest-ranked task urgency; "done" and "undated"
# rank like "ok" — neither exerts deadline pressure.
_URGENCY_RANK = {
    "overdue": 3,
    "today": 2,
    "urgent": 1,
    "ok": 0,
    "done": 0,
    "undated": 0,
}


# The Kanban board's three columns, keyed by the stage _annotate_tasks
# already assigned. The board used to spell this out three times in the
# template as `{% if task.urgency == ... %}`; a toggle that moves a card
# needs the same rule client-side, and a fourth copy of it in JavaScript is
# exactly the drift #210 is about. So the mapping lives here once, ships as
# a field on every task, and the template renders from that field.
_KANBAN_COLUMN = {
    "overdue": "urgent",
    "today": "urgent",
    "urgent": "urgent",
    "ok": "open",
    "undated": "open",
    "done": "done",
}


def _kanban_column(urgency):
    return _KANBAN_COLUMN[urgency]


def _classify_due_urgency(due, today):
    """The stage a due date alone puts a task in, completion aside.

    Split out of _annotate_tasks so reschedule_task_view can answer with the
    stage the moved task now belongs in instead of leaving the client to
    re-derive a rule that is calendar-week based (#169) and, in a demo
    session, measured against the simulated date rather than the real today.
    Completion stays the caller's business: the dot carries `done` as its own
    class, and the .dot.done rule wins over the urgency one by source order.
    """
    if not due:
        # An open task without a date is its own state (#160) — no deadline
        # pressure, so it never lifts the project urgency.
        return "undated"
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    if is_same_iso_week(due, today):
        # #169: calendar-week based, not a rolling 7-day window — a task due
        # next week is not urgent today. Closing the week (see closeout.py) is
        # what arms next week's signal, and it does that for free just by the
        # calendar rolling over.
        return "urgent"
    return "ok"


def _annotate_tasks(projects, today):
    for project in projects:
        # Chronological order for every task-list view, dateless tasks last
        # (#140). `done` is deliberately not part of the key: done tasks stay
        # interleaved at their date position, and the cached summary's
        # task_refs are positions in this order (_number_projects_and_tasks,
        # ai.py) — a key that moved tasks on toggle would re-point them.
        project["tasks"].sort(key=lambda t: (t["due"] is None, t["due"] or date.max))
        project_urgency = "ok"
        done_count = 0
        for task in project["tasks"]:
            if task["done"]:
                task["urgency"] = "done"
            else:
                task["urgency"] = _classify_due_urgency(task["due"], today)
            task["kanban_column"] = _kanban_column(task["urgency"])
            if _URGENCY_RANK[task["urgency"]] > _URGENCY_RANK[project_urgency]:
                project_urgency = task["urgency"]
            if task["done"]:
                done_count += 1
        project["urgency"] = project_urgency
        total_count = len(project["tasks"])
        project["done_count"] = done_count
        project["total_count"] = total_count
        fraction = done_count / total_count if total_count else 0
        # An f-string, not Django's floatformat filter: USE_I18N = True could
        # make floatformat emit a comma decimal separator and corrupt this
        # SVG attribute.
        project["ring_dashoffset"] = f"{RING_CIRCUMFERENCE * (1 - fraction):.2f}"
    return projects


def _build_week_view(projects, unassigned_tasks):
    """#53: the flat Heute/Diese-Woche work surface — sourced from the
    urgency _annotate_tasks already classified on every task, not re-derived
    from dates, so there stays exactly one place that decides "overdue" vs
    "today" vs "urgent". `projects` must already carry `display_name`
    (dashboard() sets it right before calling this); `unassigned_tasks` are
    already-annotated tasks with no project of their own — tagged
    "Ohne Projekt" here rather than dropped, the gap #53 found in every
    project-keyed read path.
    """
    entries = [
        (task, project["id"], project["display_name"])
        for project in projects
        for task in project["tasks"]
    ] + [(task, None, "Ohne Projekt") for task in unassigned_tasks]

    buckets = {"overdue": [], "today": [], "urgent": []}
    for task, project_id, project_name in entries:
        if task["urgency"] not in buckets:
            continue
        buckets[task["urgency"]].append(
            {**task, "project_id": project_id, "project_name": project_name}
        )
    for tasks in buckets.values():
        tasks.sort(key=lambda t: (t["due"], t["project_name"]))
    return buckets


def _count_done_in_range(tasks, start, end):
    """#19: the week progress bar's counting helper, shared with #180's
    per-day indicator (same function, a single day as the range). A task
    counts toward `total` if its due date falls in [start, end] OR it was
    actually completed in that range — an overdue task from outside the
    range that gets cleared inside it still counts (the Notion addendum
    added `completed_date` specifically to capture that, superseding the
    earlier Wann?-only proxy).

    `done` reads `task["done"]` — the real "Done" checkbox, always present on
    every task — rather than `completed_date`. A task checked off before
    "Erledigt am" existed in the Notion schema, or checked off directly in
    Notion's own UI instead of through this app, has `done=True` with no
    `completed_date`; counting by `completed_date` there undercounts a task
    the card itself already renders as done. `done` stays a subset of
    `total` regardless: `relevant` already requires the due date or the
    completed date to fall in range, and any task toggled through this app
    has `done` and `completed_date` set together (see toggle_task).
    """
    relevant = [
        t
        for t in tasks
        if (t["due"] and start <= t["due"] <= end)
        or (t.get("completed_date") and start <= t["completed_date"] <= end)
    ]
    done = sum(1 for t in relevant if t.get("done"))
    return done, len(relevant)


# #216: date.min and date.max are hard walls, and a Monday within seven days
# of either one cannot be rendered — _bucket_by_day walks a week forward
# from it and dashboard() reaches a week either side for the navigation
# links, so `week_start + timedelta(days=6)` raises OverflowError instead.
# ?week=9999-W52 did that to the whole page; week_start on the toggle did it
# *after* the Notion write had been confirmed, leaving the client told the
# write failed. Out-of-range is just one more value these parsers cannot
# use, handled where they already handle the others.
_EARLIEST_WEEK_START = date.min + timedelta(days=7)
_LATEST_WEEK_START = date.max - timedelta(days=7)


def _usable_week_start(monday, default_monday):
    if _EARLIEST_WEEK_START <= monday <= _LATEST_WEEK_START:
        return monday
    return default_monday


_WEEK_PARAM_RE = re.compile(r"(\d{4})-W(\d{2})")


def _parse_week_param(request, default_monday):
    """#180: ?week=2026-W37 navigates the day columns to that week. Anything
    unparseable — absent, malformed, a week number ISO doesn't have, or one
    too close to date.min/date.max to render (#216) — falls back to
    default_monday rather than erroring the whole page over a query param a
    visitor is free to hand-edit."""
    raw = request.GET.get("week")
    if not raw:
        return default_monday
    match = _WEEK_PARAM_RE.fullmatch(raw)
    if not match:
        return default_monday
    try:
        monday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        return default_monday
    return _usable_week_start(monday, default_monday)


def _bucket_by_day(projects, unassigned_tasks, week_start):
    """#180: the day-column breakdown of a week — independent of urgency
    (a browsed week need not be the current one, where overdue/today/urgent
    don't apply), so this buckets directly by due date instead of building
    on _build_week_view's urgency buckets. Each day also gets its own
    done/total via #19's counting helper, parameterized to that single day.
    """
    tagged = [
        {**task, "project_id": project["id"], "project_name": project["display_name"]}
        for project in projects
        for task in project["tasks"]
    ] + [
        {**task, "project_id": None, "project_name": "Ohne Projekt"}
        for task in unassigned_tasks
    ]
    days = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        day_tasks = sorted(
            (t for t in tagged if t["due"] == day), key=lambda t: t["project_name"]
        )
        done_count, total_count = _count_done_in_range(day_tasks, day, day)
        days.append(
            {
                "date": day,
                "date_iso": day.isoformat(),
                "weekday_label": WEEKDAYS_SHORT[offset],
                "tasks": day_tasks,
                "done_count": done_count,
                "total_count": total_count,
            }
        )
    return days


def _derive_dashboard_figures(
    projects, unassigned_tasks, effective_today, browsed_monday, whole_plan=False
):
    """Every count a surface on the dashboard renders task state from. One
    place, called by dashboard() on load and by toggle_task_view after a
    write — a second implementation of these rules is what #210 was.

    They cannot be recomputed in the browser: membership is subtler than
    counting the cards on screen. `_count_done_in_range` admits a task whose
    due date falls in the range *or* that was completed in it, so checking a
    task off can raise the denominator — an overdue task from an earlier
    week, cleared today, joins this week's total without any card moving.
    #194 states the same rule for urgency, and for the same reason.

    `whole_plan` is #183's exception: in a demo session the bar counts the
    plan's overall completion rather than the current week, because a
    week-scoped count barely moved between Zeitreise moments.
    """
    week_start, week_end = iso_week_bounds(effective_today)
    # #182: deliberately excludes unassigned_tasks, unlike the day columns
    # below. The Kanban board beneath this bar renders strictly from
    # project["tasks"] and can never show a project-less task, so counting
    # one here made the bar and the board disagree on the same number.
    all_tasks = [t for project in projects for t in project["tasks"]]
    if whole_plan:
        week_done = sum(1 for t in all_tasks if t["done"])
        week_total = len(all_tasks)
    else:
        week_done, week_total = _count_done_in_range(all_tasks, week_start, week_end)

    day_columns = _bucket_by_day(projects, unassigned_tasks, browsed_monday)

    kanban = dict.fromkeys(set(_KANBAN_COLUMN.values()), 0)
    for task in all_tasks:
        kanban[task["kanban_column"]] += 1

    return {
        "week": {
            "done": week_done,
            "total": week_total,
            "pct": round(week_done / week_total * 100) if week_total else 0,
        },
        "days": {
            day["date_iso"]: {"done": day["done_count"], "total": day["total_count"]}
            for day in day_columns
        },
        "kanban": kanban,
        "projects": {
            project["id"]: {
                "urgency": project["urgency"],
                "ring_dashoffset": project["ring_dashoffset"],
            }
            for project in projects
        },
        # The rendered shape day_columns needs, kept next to the counts it
        # was derived from so the load path takes no second walk over it.
        "day_columns": day_columns,
    }


def _fetch_fresh_data(today):
    """Returns (projects, summary_data) — the summary as Claude's raw
    reference dict, resolved against live projects only at render time."""
    projects = get_upcoming_projects(today)
    projects = _annotate_tasks(projects, today)
    try:
        summary_data = generate_weekly_summary(projects, today)
    except AIUnavailableError:
        summary_data = None
    return projects, summary_data


def _group_by_month(projects):
    groups = {}
    for project in projects:
        if project["event_date"]:
            key = (project["event_date"].year, project["event_date"].month)
        else:
            key = (0, 0)
        groups.setdefault(key, []).append(project)

    return [
        {"year": year, "month": MONTHS_DE.get(month, ""), "projects": projs}
        for (year, month), projs in sorted(groups.items())
    ]


def _sidebar_projects(request, today, projects=None):
    """month_groups/years for the sidebar's "Projekte" list (#185) — the
    same shape dashboard() builds inline for its own project-section
    rendering, rebuilt here for close_week_start()/week_review(), which
    don't otherwise fetch project data. Not shared code with dashboard()
    itself: its own `projects` is already sequenced through several other
    steps (week view, day columns, sim-date mutation) this lighter fetch
    has no reason to duplicate.

    `projects` skips the read entirely for a caller that already holds
    the same data — close_week_start() does, and passes its own uncached
    triage fetch in (#185 follow-up). It is an already-fetched list, never
    None as a "nothing found" answer: an empty list is passed through as
    the empty sidebar it means.

    Without it, demo mode always reflects the visitor's own session plan,
    never the example catalog — matching dashboard()'s own rule that the
    catalog only ever shows with no session plan yet or ?mode=multi
    forced, neither a state reachable from these two views. Production
    prefers dashboard()'s own cache to avoid a redundant Notion
    round-trip, falling back to a direct fetch on a cold cache; either
    way, a NotionUnavailableError degrades to an empty list rather than
    breaking a page that exists for reasons other than showing this list.

    The cached projects are annotated in place, not copied first (#185
    follow-up): every Django cache backend serializes on set and get —
    DatabaseCache (settings.CACHES) stores a pickled blob in a table — so
    cache.get() already hands back an object graph no other request
    shares. dashboard() relies on that same guarantee when it writes
    display_name onto its own cached projects, and a copy here would only
    pay for it twice. See SidebarProjectsCacheTest."""
    if projects is None:
        if settings.DEMO_MODE:
            session_plan = request.session.get("demo_plan")
            if not session_plan:
                return [], []
            projects = [_build_session_project(session_plan)]
        else:
            cached = cache.get(CACHE_KEY)
            if cached:
                projects = cached[0]
            else:
                try:
                    projects = get_upcoming_projects(today)
                except NotionUnavailableError:
                    return [], []
    projects = _annotate_tasks(projects, today)
    for project in projects:
        project["display_name"] = _strip_trailing_date(project["name"])
        project["event_date_display"] = format_date(project["event_date"], role="long")
    month_groups = _group_by_month(projects)
    years = sorted({g["year"] for g in month_groups if g["year"]})
    return month_groups, years


# A trailing German date or bare year, with an optional comma/dash separator
# and an optional "am" — the maintainer's Notion naming habit (#134). Covers
# the numeric forms (12.09.2026, 1.9.) and the spelled-out ones the live
# Notion data actually uses ("am 5. September", "15. November 2026").
_TRAILING_DATE_RE = re.compile(
    r"[\s,–—-]+(?:am\s+)?"
    r"(?:\d{1,2}\.\d{1,2}\.(?:\d{4})?"
    r"|\d{1,2}\.\s*(?:" + "|".join(MONTHS_DE.values()) + r")(?:\s+\d{4})?"
    r"|\d{4})$"
)


def _strip_trailing_date(name):
    """Display-only: the Notion property keeps the full name."""
    stripped = _TRAILING_DATE_RE.sub("", name).strip()
    return stripped or name


def index(request):
    if settings.DEMO_MODE:
        return render(request, "projects/landing.html")
    return redirect("dashboard")


def _build_session_project(session_plan):
    event_date = date.fromisoformat(session_plan["event_date"])
    tasks = [
        {
            "id": t["id"],
            "name": t["name"],
            "due": date.fromisoformat(t["date"]) if t.get("date") else None,
            "done": t["done"],
            # A list, like Notion's, so every consumer sees one shape (#9).
            # Demo-mode tasks carry no "kontext" key at all (#18) — this
            # only wraps a value for a session written before that change.
            "kontext": [t["kontext"]] if t.get("kontext") else [],
            # #171: a session written before the counter existed has no
            # such key at all.
            "postpone_count": t.get("postpone_count", 0),
            # #19: same fallback reasoning — a session written before this
            # field existed has no such key at all.
            "completed_date": date.fromisoformat(t["completed_date"])
            if t.get("completed_date")
            else None,
        }
        for t in session_plan["tasks"]
    ]
    return {
        "id": "session-plan",
        "name": session_plan["name"],
        "event_date": event_date,
        "event_date_uncertain": session_plan.get("event_date_uncertain", False),
        "performers": "",
        "tasks": tasks,
        "status": "in Vorbereitung",
        "status_color": "default",
    }


def _get_sim_date(request):
    """Reads demo_sim_date, discarding a value that predates its validation."""
    raw = request.session.get("demo_sim_date")
    if not raw:
        return None, None
    try:
        return date.fromisoformat(raw), raw
    except (ValueError, TypeError):
        request.session.pop("demo_sim_date", None)
        return None, None


def _allowed_sim_dates(request):
    """The moment dates planner_create generated for the plan now in the session.

    Only strings are collected: a session written before the moments were validated
    can hold anything the model returned, and an unhashable value would otherwise
    blow up the set itself.
    """
    moments = request.session.get("demo_timelapse_moments") or []
    if not isinstance(moments, list):
        return set()
    return {
        m["date"]
        for m in moments
        if isinstance(m, dict) and isinstance(m.get("date"), str)
    }


def _parse_json_dict_body(request):
    """Returns (data, error_response). Unparseable JSON and non-dict
    payloads are invalid client input — a 400, never a 500 (#154).

    UnicodeDecodeError is caught alongside JSONDecodeError: json.loads
    raises it for bytes that are not valid UTF-8, and it is not a
    JSONDecodeError subclass."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"error": "invalid json"}, status=400)
    if not isinstance(data, dict):
        return None, JsonResponse({"error": "invalid json"}, status=400)
    return data, None


def _parse_posted_date(request):
    """Returns (date_string, error_response). An absent date is valid — it clears the state.

    Only the moment dates this session generated are accepted. The timelapse bar
    posts nothing else, and preload_timelapse_summary spends a Claude call on every
    date it has not seen before, so accepting any parseable date would let one
    session run up an unbounded bill.

    Being on the allowlist is necessary but not sufficient: the moments come from
    Claude, so a session predating their validation can list a date that no caller
    can parse. Callers get a value date.fromisoformat() accepts or an error.
    """
    if not settings.DEMO_MODE:
        return None, JsonResponse({"error": "not available"}, status=404)
    data, error = _parse_json_dict_body(request)
    if error:
        return None, error
    raw = data.get("date")
    if not raw:
        return None, None
    if not isinstance(raw, str) or raw not in _allowed_sim_dates(request):
        return None, JsonResponse({"error": "invalid date"}, status=400)
    try:
        date.fromisoformat(raw)
    except ValueError:
        return None, JsonResponse({"error": "invalid date"}, status=400)
    return raw, None


def dashboard(request):
    today = timezone.localdate()
    effective_today = today
    has_session_plan = False
    plan_exists = False
    force_multi = request.GET.get("mode") == "multi"
    sim_date, sim_date_str = None, None
    stale = False
    data_unavailable = False

    if settings.DEMO_MODE:
        session_plan = request.session.get("demo_plan")
        plan_exists = bool(session_plan)
        if session_plan and not force_multi:
            has_session_plan = True
            # Read only here: the simulated date belongs to the visitor's own
            # plan, and reading it before the mode was known let it classify
            # and narrate the example projects too (#50).
            sim_date, sim_date_str = _get_sim_date(request)
            effective_today = sim_date or today
            project = copy.deepcopy(_build_session_project(session_plan))
            if sim_date:
                for task in project["tasks"]:
                    if task.get("due") and task["due"] <= sim_date:
                        task["done"] = True
            projects = _annotate_tasks([project], effective_today)
            # #53: the planner always ties every task it generates to the one
            # project it just created — a session plan never has a
            # project-less task to show under "Ohne Projekt".
            unassigned_tasks = []
            summary_key = f"{SUMMARY_KEY}_{sim_date_str or 'today'}"
            summary_data = request.session.get(summary_key)
            if not summary_data:
                try:
                    summary_data = generate_weekly_summary(
                        projects, effective_today, single_project_demo=True
                    )
                except AIUnavailableError:
                    summary_data = None
                else:
                    request.session[summary_key] = summary_data
        else:
            # The example projects carry none of the plan's moments, so they
            # are always classified against the real today (#50).
            projects = _annotate_tasks(get_demo_projects(), today)
            unassigned_tasks = _annotate_tasks(
                [{"id": "_unassigned", "tasks": get_demo_unassigned_tasks()}], today
            )[0]["tasks"]
            summary_cache_key = f"{DEMO_MULTI_SUMMARY_KEY}_{today.isoformat()}"
            summary_data = cache.get(summary_cache_key)
            if summary_data is None:
                try:
                    summary_data = generate_weekly_summary(projects, today)
                except AIUnavailableError:
                    # Not cached, so the next request retries Claude.
                    summary_data = None
                else:
                    cache.set(summary_cache_key, summary_data, DEMO_MULTI_SUMMARY_TTL)
    else:
        cached = cache.get(CACHE_KEY)
        if cached:
            projects, summary_data = cached
            if summary_data is None:
                # #199: CACHE_KEY holds (projects, summary_data) as one
                # tuple, so "invalidate only the summary" means writing
                # (patched_projects, None) after a reschedule. A hit in that
                # shape means "the projects are good, regenerate the
                # summary" — without this branch the card would read "KI
                # nicht verfügbar" until the TTL ran out. Smaller than
                # splitting the summary into its own key, and it matches the
                # cost #199 already accepts: the Notion read goes, the
                # Claude call stays.
                try:
                    summary_data = generate_weekly_summary(projects, today)
                except AIUnavailableError:
                    # Left unwritten, so the next request retries Claude —
                    # same reasoning as the fetch path below.
                    summary_data = None
                else:
                    # #216: only the summary is new here — these projects
                    # came out of the cache, not out of Notion — so the
                    # write-back neither renews the read-freshness window
                    # nor carries this request's snapshot of the projects
                    # over whatever landed during the Claude call.
                    _attach_regenerated_summary(projects, summary_data)
        else:
            try:
                projects, summary_data = _fetch_fresh_data(today)
            except NotionUnavailableError:
                logger.warning(
                    "Notion read failed; falling back to the last known-good dashboard data"
                )
                last_known_good = cache.get(STALE_CACHE_KEY)
                if last_known_good is None:
                    projects, summary_data = [], None
                    data_unavailable = True
                else:
                    projects, summary_data = last_known_good
                    stale = True
            else:
                # A fetch whose summary failed (None) is not a success worth
                # remembering: caching it would blank the AI card for the
                # whole TTL and overwrite the stale copy's last good summary.
                # Leaving the cache empty makes the next request retry Claude.
                if summary_data is not None:
                    _cache_fresh_read(
                        CACHE_KEY,
                        (projects, summary_data),
                        CACHE_DEADLINE_KEY,
                        CACHE_TTL,
                    )
                    cache.set(STALE_CACHE_KEY, (projects, summary_data), None)

        # #53: an independent read from get_upcoming_projects (own cache
        # pair, see UNASSIGNED_CACHE_KEY above) — annotated and cached in
        # that shape, same as `projects`, so a cache hit costs no recompute.
        cached_unassigned = cache.get(UNASSIGNED_CACHE_KEY)
        if cached_unassigned is not None:
            unassigned_tasks = cached_unassigned
        else:
            try:
                fetched_unassigned = get_unassigned_tasks(today)
            except NotionUnavailableError:
                last_known_good_unassigned = cache.get(STALE_UNASSIGNED_CACHE_KEY)
                unassigned_tasks = last_known_good_unassigned or []
            else:
                unassigned_tasks = _annotate_tasks(
                    [{"id": "_unassigned", "tasks": fetched_unassigned}], today
                )[0]["tasks"]
                _cache_fresh_read(
                    UNASSIGNED_CACHE_KEY,
                    unassigned_tasks,
                    UNASSIGNED_CACHE_DEADLINE_KEY,
                    UNASSIGNED_CACHE_TTL,
                )
                cache.set(STALE_UNASSIGNED_CACHE_KEY, unassigned_tasks, None)

    viewing_demo_data = settings.DEMO_MODE and not has_session_plan

    for project in projects:
        project["display_name"] = _strip_trailing_date(project["name"])
        project["event_date_display"] = format_date(project["event_date"], role="long")

    week_view = _build_week_view(projects, unassigned_tasks)

    # #180: the day-column breakdown can browse any week, independent of
    # effective_today — the progress bar stays on the *current* week even
    # while these columns show a different one.
    week_start = iso_week_bounds(effective_today)[0]
    browsed_monday = _parse_week_param(request, week_start)
    browsed_sunday = browsed_monday + timedelta(days=6)
    prev_monday = browsed_monday - timedelta(days=7)
    next_monday = browsed_monday + timedelta(days=7)
    is_current_week = browsed_monday == week_start

    # #210: every count on the page comes from here, and so does the
    # toggle's answer — one implementation, so the load path and the write
    # path cannot show different numbers for the same board.
    figures = _derive_dashboard_figures(
        projects,
        unassigned_tasks,
        effective_today,
        browsed_monday,
        whole_plan=has_session_plan,
    )
    week_done_count = figures["week"]["done"]
    week_total_count = figures["week"]["total"]
    week_progress_pct = figures["week"]["pct"]
    day_columns = figures["day_columns"]

    month_groups = _group_by_month(projects)
    years = sorted({g["year"] for g in month_groups if g["year"]})

    # Resolved here at render time, after display_name/event_date_display are
    # set — never where the raw dict is cached, so checkbox state and project
    # headings always reflect the live data (#122).
    summary = (
        resolve_weekly_summary(
            summary_data, projects, single_project_demo=has_session_plan
        )
        if summary_data
        else None
    )

    timelapse_moments = (
        request.session.get("demo_timelapse_moments", []) if settings.DEMO_MODE else []
    )

    # Moments whose summary is already cached in the session, so the JS
    # preloader can skip re-requesting them after a reload (#36).
    precached_moments = (
        [
            d
            for d in _allowed_sim_dates(request)
            if request.session.get(f"{SUMMARY_KEY}_{d}")
        ]
        if settings.DEMO_MODE
        else []
    )

    # Project name for demo single-project header
    demo_project_name = ""
    demo_project_date = ""
    demo_project_date_uncertain = False
    if settings.DEMO_MODE and has_session_plan and month_groups:
        first_project = (
            month_groups[0]["projects"][0] if month_groups[0]["projects"] else None
        )
        if first_project:
            demo_project_name = first_project["display_name"]
            demo_project_date = first_project["event_date_display"]
            demo_project_date_uncertain = first_project.get(
                "event_date_uncertain", False
            )

    return render(
        request,
        "projects/dashboard.html",
        {
            "month_groups": month_groups,
            "years": years,
            "summary": summary,
            "today": today,
            "today_display": format_date(today, role="long"),
            "today_iso": today.isoformat(),
            "has_session_plan": has_session_plan,
            "plan_exists": plan_exists,
            "force_multi": force_multi,
            "viewing_demo_data": viewing_demo_data,
            "demo_mode": settings.DEMO_MODE,
            "timelapse_moments": json.dumps(timelapse_moments),
            "precached_moments": json.dumps(precached_moments),
            "sim_date": sim_date_str,
            "sim_date_display": format_date(sim_date, role="long") if sim_date else "",
            "demo_project_name": demo_project_name,
            "demo_project_date": demo_project_date,
            "demo_project_date_uncertain": demo_project_date_uncertain,
            "stale": stale,
            "data_unavailable": data_unavailable,
            "heute_overdue": week_view["overdue"],
            "heute_today": week_view["today"],
            "diese_woche": week_view["urgent"],
            "week_done_count": week_done_count,
            "week_total_count": week_total_count,
            "week_progress_pct": week_progress_pct,
            "kanban_counts": figures["kanban"],
            "day_columns": day_columns,
            "week_range_label": format_week_range(browsed_monday, browsed_sunday),
            "is_current_week": is_current_week,
            "prev_week_param": f"{prev_monday.isocalendar()[0]}-W{prev_monday.isocalendar()[1]:02d}",
            "next_week_param": f"{next_monday.isocalendar()[0]}-W{next_monday.isocalendar()[1]:02d}",
        },
    )


def refresh(request):
    if request.method == "POST":
        cache.delete(CACHE_KEY)
    return redirect("dashboard")


def set_timelapse_date(request):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    sim_date, error = _parse_posted_date(request)
    if error:
        return error
    if sim_date:
        request.session["demo_sim_date"] = sim_date
    else:
        request.session.pop("demo_sim_date", None)
    return JsonResponse({"ok": True})


def preload_timelapse_summary(request):
    """Pre-generates and caches the AI summary for a given sim date (called from JS background)."""
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    sim_date_str, error = _parse_posted_date(request)  # None = today
    if error:
        return error

    today = timezone.localdate()
    sim_date = date.fromisoformat(sim_date_str) if sim_date_str else None
    effective_today = sim_date or today
    summary_key = f"{SUMMARY_KEY}_{sim_date_str or 'today'}"

    if request.session.get(summary_key):
        return JsonResponse({"ok": True, "cached": True})

    session_plan = request.session.get("demo_plan")
    if not session_plan:
        return JsonResponse({"ok": False})

    project = copy.deepcopy(_build_session_project(session_plan))
    if sim_date:
        for task in project["tasks"]:
            if task.get("due") and task["due"] <= sim_date:
                task["done"] = True
    projects = _annotate_tasks([project], effective_today)
    try:
        summary_data = generate_weekly_summary(
            projects, effective_today, single_project_demo=True
        )
    except AIUnavailableError:
        # Nothing written to the session — the next real visit to this date
        # just tries again instead of replaying a cached failure.
        return JsonResponse({"ok": False})
    request.session[summary_key] = summary_data
    return JsonResponse({"ok": True})


def _parse_week_start(data, default_monday):
    """#210: the client posts the Monday of the week its day columns are
    showing — ?week= navigates them to any week and the server cannot guess
    which one is on screen. Anything unparseable, or too close to
    date.min/date.max to render (#216), falls back to the current week — the
    same tolerance _parse_week_param applies to the query param a visitor is
    free to hand-edit, and it matters more here: this runs after the Notion
    write is confirmed, so a crash would report a write that did happen."""
    raw = data.get("week_start")
    if not isinstance(raw, str):
        return default_monday
    try:
        monday = date.fromisoformat(raw)
    except ValueError:
        return default_monday
    return _usable_week_start(monday, default_monday)


def _surface_figures(
    projects, unassigned_tasks, task_id, effective_today, browsed_monday, whole_plan
):
    """Returns (task, figures) — every number a surface on the page renders
    task state from, plus the ring of the project the write landed in.
    (None, {}) means there was nothing to derive from, and the client then
    reloads rather than being handed numbers the server had to guess at.

    Shared by both writes: a toggle and a reschedule change the same
    counters, and #210's whole point is that there is one implementation of
    them."""
    task, project = _find_task(projects, unassigned_tasks, task_id)
    if task is None:
        return None, {}
    for cached_project in projects:
        # _bucket_by_day tags every task with its project's display name;
        # the cache holds the raw Notion name, so this is set here the way
        # dashboard() sets it before its own call.
        cached_project["display_name"] = _strip_trailing_date(cached_project["name"])
    figures = _derive_dashboard_figures(
        projects, unassigned_tasks, effective_today, browsed_monday, whole_plan
    )
    answer = {
        "week": figures["week"],
        "days": figures["days"],
        "kanban": figures["kanban"],
    }
    if project is not None:
        answer["project"] = {"id": project["id"], **figures["projects"][project["id"]]}
    return task, answer


def _toggle_answer(
    projects, unassigned_tasks, task_id, effective_today, browsed_monday, whole_plan
):
    """The figures a toggle changes, next to the stage the task now carries."""
    task, figures = _surface_figures(
        projects, unassigned_tasks, task_id, effective_today, browsed_monday, whole_plan
    )
    if task is None:
        return {}
    return {
        "urgency": task["urgency"],
        "kanban_column": task["kanban_column"],
        **figures,
    }


def toggle_task_view(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data, error = _parse_json_dict_body(request)
    if error:
        return error
    done = data.get("done")
    if not isinstance(done, bool):
        return JsonResponse({"error": "invalid done"}, status=400)
    if settings.DEMO_MODE:
        # Same collapse-to-404 rule as reschedule_task_view (#10 §5, #61):
        # answering ok for a task that was never saved is worse than an
        # honest miss.
        sim_date, _ = _get_sim_date(request)
        effective_today = sim_date or timezone.localdate()
        plan = request.session.get("demo_plan")
        task = (
            next((t for t in plan["tasks"] if t["id"] == task_id), None)
            if plan
            else None
        )
        if task is None:
            return JsonResponse({"error": "unknown task"}, status=404)
        task["done"] = done
        # #19: mirrors toggle_task's own Done/Erledigt am pairing in Notion.
        task["completed_date"] = effective_today.isoformat() if done else None
        request.session["demo_plan"] = plan
        # Time travel counts here the way it does on the dashboard: the same
        # deepcopy mutation, so the figures match what a reload would render.
        project = copy.deepcopy(_build_session_project(plan))
        if sim_date:
            for plan_task in project["tasks"]:
                if plan_task.get("due") and plan_task["due"] <= sim_date:
                    plan_task["done"] = True
        projects = _annotate_tasks([project], effective_today)
        figures = _toggle_answer(
            projects,
            [],
            task_id,
            effective_today,
            _parse_week_start(data, iso_week_bounds(effective_today)[0]),
            # #183: a demo session's bar counts the whole plan, not the week.
            whole_plan=True,
        )
    else:
        today = timezone.localdate()
        completed_date = today if done else None
        try:
            toggle_task(task_id, done, completed_date.isoformat() if done else None)
        except NotionUnavailableError:
            # A non-200 so the caller knows not to apply its optimistic
            # update — see the dashboard.html JS changes in the same commit.
            return JsonResponse({"error": "notion unavailable"}, status=502)

        # #199: the toggle moves no task (see _annotate_tasks' sort key), so
        # the cached lists can carry the write instead of being thrown away
        # and re-read from Notion at a Claude call's expense.
        def mark(task):
            task["done"] = done
            task["completed_date"] = completed_date

        patched = _patch_cached_tasks(task_id, mark, today)
        if patched is None:
            _bust_dashboard_cache()
            figures = {}
        else:
            figures = _toggle_answer(
                *patched,
                task_id,
                today,
                _parse_week_start(data, iso_week_bounds(today)[0]),
                whole_plan=False,
            )
    return JsonResponse({"ok": True, **figures})


def reschedule_task_view(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data, error = _parse_json_dict_body(request)
    if error:
        return error
    raw_date = data.get("date")
    try:
        parsed_date = date.fromisoformat(raw_date)
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid date"}, status=400)
    due_display = format_date(parsed_date, role="long")

    # Read before the branches: both of them derive the figures below
    # against it. A demo visitor's time travel has to count here the way it
    # does on the dashboard, or a plan viewed at a simulated moment would
    # come back classified against the real today.
    sim_date = None
    if settings.DEMO_MODE:
        sim_date, _ = _get_sim_date(request)
    effective_today = sim_date or timezone.localdate()
    browsed_monday = _parse_week_start(data, iso_week_bounds(effective_today)[0])

    if settings.DEMO_MODE:
        # In demo mode only the visitor's own session plan can be written to;
        # the example projects come from get_demo_projects() and are in no
        # session. A task that isn't in the plan gets a 404 rather than
        # toggle_task_view's silent ok — answering ok for something that was
        # never saved is exactly the bug this view had (#10 §5).
        plan = request.session.get("demo_plan")
        task = (
            next((t for t in plan["tasks"] if t["id"] == task_id), None)
            if plan
            else None
        )
        if task is None:
            return JsonResponse({"error": "unknown task"}, status=404)
        task["date"] = raw_date
        # #171: awareness, not punishment — starts counting from the second
        # move, but the counter itself increments on every reschedule from
        # the first one (the badge threshold is a display concern, applied
        # in the templates).
        task["postpone_count"] = task.get("postpone_count", 0) + 1
        request.session["demo_plan"] = plan
        # The task order is chronological (#140), so a new date moves the
        # task — cached summaries would keep task_refs numbered against the
        # old positions and silently re-point (see _annotate_tasks). Same
        # unversioned-prefix sweep as planner_create (planner_views.py).
        for key in list(request.session.keys()):
            if key.startswith("demo_plan_summary"):
                del request.session[key]
        postpone_count = task["postpone_count"]
        # The same deepcopy mutation toggle_task_view applies, so the figures
        # match what a reload of the dashboard would render.
        project = copy.deepcopy(_build_session_project(plan))
        if sim_date:
            for plan_task in project["tasks"]:
                if plan_task.get("due") and plan_task["due"] <= sim_date:
                    plan_task["done"] = True
        _, figures = _surface_figures(
            _annotate_tasks([project], effective_today),
            [],
            task_id,
            effective_today,
            browsed_monday,
            # #183: a demo session's bar counts the whole plan, not the week.
            whole_plan=True,
        )
    else:
        try:
            update_task_date(task_id, raw_date)
        except NotionUnavailableError:
            return JsonResponse({"error": "notion unavailable"}, status=502)
        # Applied right away, before the counter call: the date change is
        # already confirmed in Notion at this point, so a failure below must
        # not leave the cache serving the pre-move date (_bust_dashboard_cache
        # promises this for "every confirmed Notion write").
        #
        # #199: the projects survive the move — re-sorted and re-annotated —
        # and only the summary is dropped, because a new date renumbers the
        # task_refs it holds (_number_projects_and_tasks, ai.py). The Notion
        # read goes, the Claude call stays.
        #
        # effective_today rather than a second timezone.localdate() call:
        # this branch only runs outside DEMO_MODE, where the two are the same
        # day, and one value keeps the figures below from being derived
        # against a different date than the urgency answered with.

        def move(task):
            task["due"] = parsed_date

        if (
            _patch_cached_tasks(task_id, move, effective_today, drop_summary=True)
            is None
        ):
            _bust_dashboard_cache()
        try:
            # #171 accepted gap: if this second call fails, the date has
            # already moved but the counter hasn't — reported as the same
            # 502 below, self-healing on the next reschedule.
            postpone_count = increment_postpone_count(task_id)
        except NotionUnavailableError:
            return JsonResponse({"error": "notion unavailable"}, status=502)

        # A second, smaller patch rather than an optimistic +1 above: the
        # counter is only known once Notion has confirmed it, and a cache
        # claiming a move Notion never counted would be the mirror image of
        # the staleness this is meant to remove.
        def count(task):
            task["postpone_count"] = postpone_count

        patched = _patch_cached_tasks(task_id, count, effective_today)
        if patched is None:
            _bust_dashboard_cache()
            figures = {}
        else:
            # #216: the day columns bucket by date, so every reschedule
            # changes them — a move within one stage kept the row and the
            # columns disagreeing, because only a stage change reloaded.
            # Same figures as the toggle, from the same helper.
            _, figures = _surface_figures(
                *patched, task_id, effective_today, browsed_monday, whole_plan=False
            )
    # The stage the row belongs in after the move, so the client can
    # reclassify the date label and its dot instead of leaving both wearing
    # the pre-move signal until the next page load. Completion is left out on
    # purpose: the dot's `done` class is the toggle's business and outranks
    # the urgency rule anyway.
    return JsonResponse(
        {
            "ok": True,
            "postpone_count": postpone_count,
            "due_display": due_display,
            "urgency": _classify_due_urgency(parsed_date, effective_today),
            **figures,
        }
    )


def _current_projects_for_closeout(request, today):
    """This session's or production's projects for the close-out flow
    (#169) — read fresh, not from the dashboard cache: a stale week's data
    would misclassify the triage list. Returns None if there is nothing to
    close out (demo mode, no session plan).

    Returns projects rather than the flat task list both callers actually
    triage on, so close_week_start() can hand the same fetch to
    _sidebar_projects() (#185 follow-up). An empty list is a real answer
    (production with nothing upcoming) and stays distinct from None."""
    if settings.DEMO_MODE:
        session_plan = request.session.get("demo_plan")
        if not session_plan:
            return None
        return [_build_session_project(session_plan)]
    return get_upcoming_projects(today)


def close_week_start(request):
    today = timezone.localdate()
    try:
        projects = _current_projects_for_closeout(request, today)
    except NotionUnavailableError:
        return redirect("dashboard")
    if projects is None:
        return redirect("index")
    tasks = [t for p in projects for t in p["tasks"]]
    # Overdue tasks stay out — they already have their own signal, and the
    # point of this list is the tasks that are still a conscious choice to
    # move, not the ones already late.
    open_this_week = [
        t
        for t in tasks
        if not t["done"]
        and t["due"]
        and t["due"] >= today
        and is_same_iso_week(t["due"], today)
    ]
    for task in open_this_week:
        # The move button's own label — otherwise "→ nächste Woche" doesn't
        # say which date that actually is.
        task["next_week_display"] = format_date(
            task["due"] + timedelta(days=7), role="long"
        )
    iso_year, iso_week, _ = today.isocalendar()
    # If the week is already closed and nothing new is open, confirming
    # again would post an empty task_id list and overwrite the real stats
    # with zeros — the template hides the button for exactly this case and
    # points to the existing review instead.
    already_closed = is_week_closed(request, iso_year, iso_week)
    # #185 follow-up: the projects fetched above, not a second read — on a
    # cold cache this view used to pair its own uncached triage fetch with
    # an identical one inside _sidebar_projects(), and every task toggle or
    # move in this very flow busts that cache (_bust_dashboard_cache), so
    # cold is the normal state here. Sharing the dicts is safe:
    # _annotate_tasks only adds `urgency` (which the triage template doesn't
    # render), and it sorts project["tasks"], not the separate open_this_week
    # list built above.
    month_groups, years = _sidebar_projects(request, today, projects=projects)
    return render(
        request,
        "projects/close_week_start.html",
        {
            "tasks": open_this_week,
            "already_closed": already_closed,
            "today_display": format_date(today, role="long"),
            # A weekend-specific empty state reads oddly on a Tuesday.
            "is_weekend": today.weekday() >= 5,
            # #183 follow-up: the sidebar is now shared with dashboard() via
            # _sidebar_nav.html, so this view owes it the same three flags
            # (see the contract note in that partial). In DEMO_MODE a session
            # plan is guaranteed here (_current_projects_for_closeout redirects
            # to index otherwise), which makes plan_exists true for the same
            # reason has_session_plan is; in production neither applies.
            "demo_mode": settings.DEMO_MODE,
            "has_session_plan": settings.DEMO_MODE,
            "plan_exists": settings.DEMO_MODE,
            "active_nav": "close_week",
            "month_groups": month_groups,
            "years": years,
        },
    )


def close_week_confirm(request):
    if request.method != "POST":
        return redirect("close_week_start")
    today = timezone.localdate()
    iso_year, iso_week, _ = today.isocalendar()
    task_ids = request.POST.getlist("task_id")

    try:
        projects = _current_projects_for_closeout(request, today)
    except NotionUnavailableError:
        return redirect("close_week_start")
    if projects is None:
        return redirect("index")
    tasks = [t for p in projects for t in p["tasks"]]

    live_by_id = {t["id"]: t for t in tasks}
    completed_count = 0
    rescheduled_count = 0
    for task_id in task_ids:
        task = live_by_id.get(task_id)
        if task is None:
            continue
        if task["done"]:
            completed_count += 1
        elif task["due"] is None or not is_same_iso_week(task["due"], today):
            # Moved since the triage list was loaded — by the "→ nächste
            # Woche" action here or by any other reschedule path meanwhile.
            rescheduled_count += 1

    # Production only (#169): a freshly generated demo plan has no
    # meaningful "added this week" — the whole plan is created in one shot.
    added_count = (
        sum(
            1
            for t in tasks
            if t.get("created_time") and is_same_iso_week(t["created_time"], today)
        )
        if not settings.DEMO_MODE
        else 0
    )

    stats_dict = {
        "completed_count": completed_count,
        "rescheduled_count": rescheduled_count,
        "added_count": added_count,
    }
    try:
        summary_text = generate_closeout_summary(stats_dict, today)
    except AIUnavailableError:
        summary_text = ""

    save_closeout(request, iso_year, iso_week, stats_dict, summary_text)
    return redirect("week_review")


def week_review(request):
    if settings.DEMO_MODE and not request.session.get("demo_plan"):
        return redirect("index")
    closeout = get_latest_closeout(request)
    if closeout is None:
        return redirect("close_week_start")
    today = timezone.localdate()
    month_groups, years = _sidebar_projects(request, today)
    return render(
        request,
        "projects/week_review.html",
        {
            "closeout": closeout,
            # #183 follow-up: same three flags as close_week_start() — the
            # sidebar is shared via _sidebar_nav.html. The guard above is
            # this view's own (a demo_plan check, not the closeout lookup),
            # so a session plan is equally guaranteed by the time we render.
            "demo_mode": settings.DEMO_MODE,
            "has_session_plan": settings.DEMO_MODE,
            "plan_exists": settings.DEMO_MODE,
            "active_nav": "close_week",
            "month_groups": month_groups,
            "years": years,
        },
    )


def stats(request):
    from django.db.models import Count
    from django.db.models.functions import TruncDate

    total_generated = DemoEvent.objects.filter(event_type="plan_generated").count()
    total_downloaded = DemoEvent.objects.filter(event_type="plan_downloaded").count()

    by_type = (
        DemoEvent.objects.filter(event_type="plan_generated")
        .values("project_type")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    by_day = (
        DemoEvent.objects.filter(event_type="plan_generated")
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("-day")[:14]
    )

    TYPE_LABELS = {
        "konzert": "Konzert / Event",
        "hochzeit": "Hochzeit / Feier",
        "recruiting": "Recruiting",
        "eigenes": "Eigenes Projekt",
        "": "Unbekannt",
    }

    return render(
        request,
        "projects/stats.html",
        {
            "total_generated": total_generated,
            "total_downloaded": total_downloaded,
            "download_rate": round(total_downloaded / total_generated * 100)
            if total_generated
            else 0,
            "by_type": [
                {
                    "label": TYPE_LABELS.get(r["project_type"], r["project_type"]),
                    "count": r["count"],
                }
                for r in by_type
            ],
            "by_day": list(by_day),
        },
    )


def my_plan(request):
    plan = request.session.get("demo_plan")
    if not plan:
        return redirect("index")

    today = timezone.localdate()
    project = _build_session_project(plan)
    project["display_name"] = _strip_trailing_date(project["name"])
    project["event_date_display"] = format_date(project["event_date"], role="long")
    _annotate_tasks([project], today)

    # #185: the sidebar's "Projekte" list, same shape dashboard() builds for
    # itself — a single-entry list here, matching how dashboard() itself
    # only ever lists the one session project once has_session_plan is true.
    month_groups = _group_by_month([project])
    years = sorted({g["year"] for g in month_groups if g["year"]})

    tasks = project["tasks"]
    done_count = sum(1 for t in tasks if t["done"])
    total = len(tasks)

    summary_data = request.session.get(f"{SUMMARY_KEY}_today")
    summary_error = False
    if not summary_data:
        try:
            summary_data = generate_weekly_summary(
                [project], today, single_project_demo=True
            )
        except AIUnavailableError:
            summary_error = True
        else:
            request.session[f"{SUMMARY_KEY}_today"] = summary_data

    # Resolved against the live session plan at render time (#122) — the
    # session-cached raw dict never carries done state.
    summary = (
        resolve_weekly_summary(summary_data, [project], single_project_demo=True)
        if summary_data
        else None
    )

    return render(
        request,
        "projects/my_plan.html",
        {
            "project": project,
            "done_count": done_count,
            "total": total,
            "today": today,
            "today_display": format_date(today, role="long"),
            "summary": summary,
            "summary_error": summary_error,
            # #183 follow-up: the sidebar is now shared with dashboard() via
            # _sidebar_nav.html — a session plan is guaranteed here (redirect
            # above), so both has_session_plan and plan_exists are always
            # true whenever this actually renders.
            "demo_mode": settings.DEMO_MODE,
            "has_session_plan": True,
            "plan_exists": True,
            "active_nav": "my_plan",
            "month_groups": month_groups,
            "years": years,
        },
    )


def download_plan(request):
    plan = request.session.get("demo_plan")
    if not plan:
        return redirect("index")

    today = timezone.localdate()
    event_date = date.fromisoformat(plan["event_date"])
    event_display = format_date(event_date, role="long")

    lines = [
        # Display-only cleanup (#134) — the export carries its own Zieldatum
        # line right below. The filename keeps the raw name.
        f"# {_strip_trailing_date(plan['name'])}",
        f"**Zieldatum:** {event_display}",
        "",
        "---",
        "",
        "## Aufgabenplan",
        "",
    ]

    for t in plan["tasks"]:
        checkbox = "[x]" if t["done"] else "[ ]"
        due = date.fromisoformat(t["date"]) if t.get("date") else None
        due_str = f" — {format_date(due, role='long')}" if due else ""
        lines.append(f"- {checkbox} {t['name']}{due_str}")

    lines += [
        "",
        "---",
        "",
        "> **Tipp für KI-Tools:** Füge diese Datei in Claude, ChatGPT oder dein",
        "> bevorzugtes KI-Tool ein und schreibe z.B.:",
        '> - "Erstelle mir einen wöchentlichen Fokusplan aus dieser Aufgabenliste"',
        '> - "Welche Aufgaben haben diese Woche Priorität?"',
        '> - "Schreibe mir eine Erinnerungs-E-Mail für die überfälligen Punkte"',
        "",
        f"*Generiert mit Planning Hub · {today.strftime('%d.%m.%Y')}*",
    ]

    content = "\n".join(lines)
    filename = plan["name"].replace(" ", "_").replace("/", "-")[:50] + ".md"

    project_type = request.session.get("demo_project_type", "")
    DemoEvent.objects.create(event_type="plan_downloaded", project_type=project_type)

    response = HttpResponse(content, content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def health(request):
    if not settings.DEMO_MODE:
        try:
            connection.ensure_connection()
        except Error:
            return HttpResponse("db unavailable", status=503)
    return HttpResponse("ok")


def impressum(request):
    return render(request, "projects/impressum.html")


def datenschutz(request):
    return render(request, "projects/datenschutz.html")


def toggle_session_task(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data, error = _parse_json_dict_body(request)
    if error:
        return error
    done = data.get("done")
    if not isinstance(done, bool):
        return JsonResponse({"error": "invalid done"}, status=400)
    plan = request.session.get("demo_plan")
    task = (
        next((t for t in plan["tasks"] if t["id"] == task_id), None) if plan else None
    )
    if task is None:
        return JsonResponse({"error": "unknown task"}, status=404)
    task["done"] = done
    request.session["demo_plan"] = plan
    return JsonResponse({"ok": True})
