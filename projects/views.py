import copy
import json
import logging
import re
from datetime import date, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import Error, connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .ai import (
    AIUnavailableError,
    generate_timelapse_moments,
    generate_weekly_summary,
    resolve_kontext_hint,
    resolve_weekly_summary,
    summary_has_content,
)
from .annotate import _KANBAN_COLUMN, _annotate_tasks, _classify_due_urgency
from .dashboard_cache import (
    CACHE_DEADLINE_KEY,
    CACHE_KEY,
    CACHE_TTL,
    STALE_CACHE_KEY,
    STALE_UNASSIGNED_CACHE_KEY,
    UNASSIGNED_CACHE_DEADLINE_KEY,
    UNASSIGNED_CACHE_KEY,
    UNASSIGNED_CACHE_TTL,
    _attach_regenerated_summary,
    _bust_dashboard_cache,
    _cache_fresh_read,
    _drop_fresh_dashboard_cache,
    _find_task,
    _patch_cached_tasks,
    _remap_summary_refs,
    _summary_ref_order,
)
from .date_format import (
    MONTHS_DE,
    WEEKDAYS_SHORT,
    format_date,
    format_week_range,
)
from .dates import iso_week_bounds
from .demo_data import get_demo_projects, get_demo_unassigned_tasks
from .models import DemoEvent
from .notion import (
    NotionUnavailableError,
    create_task,
    get_exclusive_task_ids,
    get_exclusive_tasks,
    get_unassigned_tasks,
    get_upcoming_projects,
    increment_postpone_count,
    rename_task,
    toggle_task,
    trash_project,
    trash_task,
    update_project_date,
    update_task_date,
)

logger = logging.getLogger(__name__)

# Session key prefix for demo summaries; planner_create clears every version
# by the unversioned "demo_plan_summary" prefix when a new plan is generated.
SUMMARY_KEY = "demo_plan_summary_v7"
# The multi-project demo summary: get_demo_projects() is a pure function of
# timezone.localdate() and holds no per-visitor data, so one Claude call per day serves
# every visitor. The day is part of the key, so a rollover invalidates by
# itself and the TTL only bounds how long one day's entry lives. The cache is
# shared across both gunicorn workers (DatabaseCache, settings.py CACHES, #52),
# so expect up to one call per day rather than one per worker.
DEMO_MULTI_SUMMARY_KEY = "demo_multi_summary_v5"
DEMO_MULTI_SUMMARY_TTL = 60 * 60 * 24


def _simulated_project(session_plan, sim_date):
    """The session plan as one project, rendered at `sim_date`.

    A Zeitreise moment is a rendering of a date, so everything due by that
    date counts as cleared. The deepcopy is what keeps that forcing off the
    session itself — the moment is a view, not a write (#217).

    Each forced task is given its own due date as its completion date. Not
    decoration: since #211 part 2 the dot's green reads completed_date, and
    a forced task carrying none would render the whole moment gray — the
    Zeitreise's own point is watching tasks clear. The due date is the
    honest answer here, because "would have been done by then" is exactly
    what the forcing above asserts, and it makes the moment's green obey the
    same week rule the real dashboard does rather than an exception to it.

    Three call sites had this mutation copied out — dashboard(), the
    single-project summary and the reschedule figures — which is two chances
    to add a field here and forget it there.
    """
    project = copy.deepcopy(_build_session_project(session_plan))
    if sim_date:
        for task in project["tasks"]:
            if task.get("due") and task["due"] <= sim_date:
                task["done"] = True
                task["completed_date"] = task["due"]
    return project


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


# The urgencies that mean "this week still has work in it" — the same three
# _build_week_view buckets by, so the note and the Heute/Diese-Woche lists
# cannot disagree about whether the week is clear.
_PENDING_URGENCIES = {"overdue", "today", "urgent"}


def _summary_empty_state(summary, projects, unassigned_tasks):
    """The note that replaces a weekly summary which resolved to nothing
    (#214), or None when there is a summary to render — or none at all,
    which the templates' "nicht verfügbar" branch owns.

    Every sentence is guarded by live data rather than by Claude's silence:
    an empty answer while something is due this week is a model error, and
    "Diese Woche steht nichts an." next to a task due tomorrow would be the
    same class of bug this issue is about, one volume louder. So the three
    are independent — any combination can render, and at least one always
    does (see below).

    Overdue work gets its own key rather than riding along in next_due: a
    date already past is not the *next* task, and "Die nächste Aufgabe ist
    am 16. August." was the same failure the note exists to prevent, told
    backwards. Splitting it is what keeps the note honest without ever
    leaving the box blank — week_is_clear is false only when some open task
    is overdue, due today or due this week, and each of those lands in
    exactly one of the other two keys.

    Urgency comes from _annotate_tasks rather than being re-derived from
    dates — here for the overdue split too, so a simulated moment measures
    against its own date the way every other surface does (#153, #169).
    Unassigned tasks count too: the summary itself only covers projects
    carrying an event_date, but an "Ohne Projekt" task due earlier would
    make the sentence contradict the list below it.
    """
    if summary is None or summary_has_content(summary):
        return None
    tasks = [t for p in projects for t in p["tasks"]] + list(unassigned_tasks)
    # Dateless open tasks (urgency "undated") drop out with the `due` test:
    # they carry no date either sentence could name.
    open_tasks = [t for t in tasks if t["due"] and not t["done"]]
    overdue = [t["due"] for t in open_tasks if t["urgency"] == "overdue"]
    upcoming = [t["due"] for t in open_tasks if t["urgency"] != "overdue"]
    return {
        "week_is_clear": not any(t["urgency"] in _PENDING_URGENCIES for t in tasks),
        "overdue_since": min(overdue, default=None),
        "next_due": min(upcoming, default=None),
    }


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


def _week_monday(raw, default_monday):
    """`2026-W37` -> that week's Monday, whatever carried the value.

    Anything unusable — absent, malformed, a week number ISO doesn't have,
    or one too close to date.min/date.max to render (#216) — falls back to
    default_monday rather than erroring a whole page over a value a visitor
    is free to hand-edit.

    #263: source-agnostic because the close-out now carries a week too, in a
    hidden form field rather than a query param. One wire format app-wide,
    one guard, one fallback rule — the alternative was a second week format
    that would have had to be kept in step with this one.
    """
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


def _parse_week_param(request, default_monday):
    """#180: ?week=2026-W37 navigates the day columns to that week."""
    return _week_monday(request.GET.get("week"), default_monday)


def _week_param(monday):
    """A week in the wire format _week_monday reads back — the one form
    ?week= links and the close-out's hidden field both carry."""
    iso_year, iso_week, _ = monday.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


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
    """The live Notion projects, annotated — and nothing else (#156).

    It used to generate the weekly summary too, which is what made a first
    uncached dashboard load block for 6–7 seconds before the browser saw any
    HTML. The Claude call moved to summary_fragment(), which blocks its own
    XHR instead.
    """
    return _annotate_tasks(get_upcoming_projects(today), today)


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


# The long weekday names, which date_format.py has no constant for — it
# formats dates and never spells a weekday out, while this reads what a
# Notion name may *contain*. Listed beside WEEKDAYS_SHORT rather than
# derived from it: "Mo" to "Montag" is a lookup, not a suffix.
_WEEKDAYS_LONG = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonnabend",
    "Sonntag",
)

# A trailing German date or bare year, with an optional comma/dash separator
# and an optional "am" — the maintainer's Notion naming habit (#134). Covers
# the numeric forms (12.09.2026, 1.9.) and the spelled-out ones the live
# Notion data actually uses ("am 5. September", "15. November 2026").
#
# #282: and an optional weekday between the two. "am Do, 17. Dezember 2026"
# used to strip from the comma on and leave "… am Do" standing — a weekday
# with nothing after it, which reads as part of the name. The long forms go
# first so that "Sonntag" cannot be matched as "So" with "nntag" left over.
_TRAILING_DATE_RE = re.compile(
    r"[\s,–—-]+(?:am\s+)?"
    r"(?:(?:" + "|".join(_WEEKDAYS_LONG + tuple(WEEKDAYS_SHORT)) + r")\.?,?\s*)?"
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
        # plan_exists, not has_session_plan: the landing page shows no plan at
        # all, so the question it asks is "does one exist" — the distinction
        # docs/demo-mode.md draws between the two flags (#129).
        return render(
            request,
            "projects/landing.html",
            {"plan_exists": bool(request.session.get("demo_plan"))},
        )
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


def _session_task_order(session_plan, today):
    """The numbering a demo session summary's task_refs are positions in.

    Built through the same two steps dashboard() renders from — the project
    as _build_session_project makes it, sorted by _annotate_tasks — so a
    session plan is measured against the same order production measures its
    cached projects against.
    """
    _, numbered_tasks = _summary_ref_order(
        _annotate_tasks([_build_session_project(session_plan)], today)
    )
    return numbered_tasks


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
    # #156: set by whichever branch below finds no cached summary. The page
    # renders the loading state and fetches summary_fragment() for it; every
    # other state is rendered inline, so a cached summary never flashes.
    summary_pending = False

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
            projects = _annotate_tasks(
                [_simulated_project(session_plan, sim_date)], effective_today
            )
            # #53: the planner always ties every task it generates to the one
            # project it just created — a session plan never has a
            # project-less task to show under "Ohne Projekt".
            unassigned_tasks = []
            # #156: read, never generated. An absent entry is the loading
            # state, and summary_fragment() fills it from the same key.
            summary_key = f"{SUMMARY_KEY}_{sim_date_str or 'today'}"
            summary_data = request.session.get(summary_key)
            summary_pending = not summary_data
        else:
            # The example projects carry none of the plan's moments, so they
            # are always classified against the real today (#50).
            projects = _annotate_tasks(get_demo_projects(), today)
            unassigned_tasks = _annotate_tasks(
                [{"id": "_unassigned", "tasks": get_demo_unassigned_tasks()}], today
            )[0]["tasks"]
            summary_cache_key = f"{DEMO_MULTI_SUMMARY_KEY}_{today.isoformat()}"
            summary_data = cache.get(summary_cache_key)
            summary_pending = summary_data is None
    else:
        cached = cache.get(CACHE_KEY)
        if cached:
            projects, summary_data = cached
            # #199: CACHE_KEY holds (projects, summary_data) as one tuple, so
            # "invalidate only the summary" means writing (patched_projects,
            # None) after a reschedule. A hit in that shape means "the
            # projects are good, the summary has to be generated" — which
            # #156 turned from an inline Claude call into the loading state.
            # It is now also the shape a *first* load leaves behind, see
            # below.
            summary_pending = summary_data is None
        else:
            try:
                projects = _fetch_fresh_data(today)
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
                    # No loading state: CACHE_KEY is cold, which is exactly
                    # the shape _production_summary() has nothing to work
                    # from. Whatever summary the stale copy carries is what
                    # this page can show.
            else:
                summary_data = None
                summary_pending = True
                # #156: (projects, None) is cached deliberately. This used to
                # be guarded on `summary_data is not None` — a fetch whose
                # Claude call had failed was not a success worth remembering.
                # With the call gone from here, summary_data is *always* None,
                # so keeping the guard would mean never caching a Notion read
                # again and re-reading Notion on every single load: the exact
                # opposite of what this issue is for. The guard's purpose
                # survives in summary_fragment(), which writes back only on
                # success, so a failed summary is still not sticky.
                #
                # STALE_CACHE_KEY is deliberately *not* written here, and for
                # the reason the guard existed: the last-known-good copy is
                # what a Notion outage serves, and overwriting it with a None
                # summary would throw away the last summary a Claude call was
                # paid for. _attach_regenerated_summary writes it, with these
                # same projects, as soon as the summary arrives.
                _cache_fresh_read(
                    CACHE_KEY,
                    (projects, None),
                    CACHE_DEADLINE_KEY,
                    CACHE_TTL,
                )

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
    summary_empty_state = _summary_empty_state(summary, projects, unassigned_tasks)
    # #145: production only, and DEMO_MODE is what says that — not
    # has_session_plan, which is false for the demo's example projects too
    # and would have let the hint render on the public demo. Kontext never
    # reaches a demo prompt at all (#18), so build_prompt does not ask for
    # the field there either; this is the second half of the same rule, so
    # that a summary cached before that change cannot surface one.
    kontext_hint = (
        resolve_kontext_hint(summary_data)
        if summary_data and not settings.DEMO_MODE
        else ""
    )

    moments = (
        request.session.get("demo_timelapse_moments", []) if settings.DEMO_MODE else []
    )
    # #156: a plan whose moments have not been generated yet. The bar is built
    # from the answer instead of from this render, and only ever once per
    # plan — see timelapse_moments().
    timelapse_pending = bool(
        settings.DEMO_MODE
        and has_session_plan
        and request.session.get("demo_timelapse_pending")
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
            "summary_empty_state": summary_empty_state,
            "summary_pending": summary_pending,
            # Built here rather than in the template so the one rule that
            # says which summary this is stays server-side (#156). ?mode=multi
            # travels because it is the only part summary_fragment() cannot
            # read off the session.
            "summary_url": reverse("summary_fragment")
            + ("?mode=multi" if force_multi else ""),
            "kontext_hint": kontext_hint,
            "today": today,
            "today_display": format_date(today, role="long"),
            "today_iso": today.isoformat(),
            "has_session_plan": has_session_plan,
            "plan_exists": plan_exists,
            "force_multi": force_multi,
            "viewing_demo_data": viewing_demo_data,
            "demo_mode": settings.DEMO_MODE,
            "timelapse_moments": moments,
            "timelapse_pending": timelapse_pending,
            "precached_moments": precached_moments,
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
            "prev_week_param": _week_param(prev_monday),
            "next_week_param": _week_param(next_monday),
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
    """Pre-generates and caches the AI summary for a given sim date (called
    from JS background).

    The generating half is _session_summary(), shared with summary_fragment()
    since #156 — the two do the same work and differ only in what they
    answer with. The answer here is the preloader's contract: `cached` is
    what keeps a moment's green dot on without paying for it twice.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    sim_date_str, error = _parse_posted_date(request)  # None = today
    if error:
        return error
    _summary_data, status = _session_summary(request, sim_date_str)
    if status == "cached":
        return JsonResponse({"ok": True, "cached": True})
    return JsonResponse({"ok": status == "generated"})


# #156: the three places a weekly summary is generated and cached, one
# function each, and each one returns (summary_data, status) where status is
# "cached", "generated" or "unavailable".
#
# They are the half of dashboard()/my_plan() that used to run inline. The
# page keeps the Notion read; these keep the Claude call. That split is the
# whole issue: a page load never waits on Claude any more, and the wait it
# used to hide happens inside summary_fragment()'s own XHR, where there is
# something on screen to show it with.
#
# Deliberately no Notion read in any of them. A fragment request arrives
# after the page it belongs to has already read and cached, so reading again
# would pay for the same data twice — and a cold cache here means the page
# load failed or a write busted it, both of which the next load handles.
# "unavailable" renders the state the templates already had for a missing
# summary, and nothing is written, so the next load retries.


def _session_summary(request, sim_date_str):
    """The visitor's own demo plan, summarized at `sim_date_str` (None =
    today). The session is the cache, keyed per simulated date.

    Shared by summary_fragment() and preload_timelapse_summary(): the two
    want the same work done and differ only in what they answer with, which
    is exactly the split #235's queue already assumes — both go through
    withSessionLock on the client because both write the session.
    """
    summary_key = f"{SUMMARY_KEY}_{sim_date_str or 'today'}"
    cached = request.session.get(summary_key)
    if cached:
        return cached, "cached"
    session_plan = request.session.get("demo_plan")
    if not session_plan:
        return None, "unavailable"
    sim_date = date.fromisoformat(sim_date_str) if sim_date_str else None
    effective_today = sim_date or timezone.localdate()
    projects = _annotate_tasks(
        [_simulated_project(session_plan, sim_date)], effective_today
    )
    try:
        summary_data = generate_weekly_summary(
            projects, effective_today, single_project_demo=True
        )
    except AIUnavailableError:
        # Nothing written to the session — the next request for this date
        # tries again instead of replaying a cached failure.
        return None, "unavailable"
    request.session[summary_key] = summary_data
    return summary_data, "generated"


def _demo_multi_summary(today):
    """The example catalog, summarized once per day for every visitor — see
    DEMO_MULTI_SUMMARY_KEY for why one call serves them all."""
    summary_cache_key = f"{DEMO_MULTI_SUMMARY_KEY}_{today.isoformat()}"
    cached = cache.get(summary_cache_key)
    if cached is not None:
        return cached, "cached"
    projects = _annotate_tasks(get_demo_projects(), today)
    try:
        summary_data = generate_weekly_summary(projects, today)
    except AIUnavailableError:
        # Not cached, so the next request retries Claude.
        return None, "unavailable"
    cache.set(summary_cache_key, summary_data, DEMO_MULTI_SUMMARY_TTL)
    return summary_data, "generated"


def _production_summary(cached, today):
    """The live Notion projects CACHE_KEY is holding, summarized. `cached` is
    that entry, read by the caller so that one read serves both the summary
    and the projects it is resolved against.

    The projects come out of the cache rather than out of Notion, which is
    what makes "projects good, summary missing" the one shape this works
    from — the #199 branch, now reached on every first load of an entry
    rather than only after a reschedule.

    A cold CACHE_KEY is "unavailable" rather than a Notion read: this runs
    after the page load that filled it, so a miss means that load failed or
    a write busted the entry in between. Either way the next load re-reads,
    and paying for a second Notion round-trip here would buy a summary for
    data the page below it is not showing.
    """
    if cached is None:
        return None, "unavailable"
    projects, summary_data = cached
    if summary_data is not None:
        return summary_data, "cached"
    try:
        summary_data = generate_weekly_summary(projects, today)
    except AIUnavailableError:
        # Left unwritten, so the next request retries Claude.
        return None, "unavailable"
    # #216: only the summary is new here, so the write-back neither renews
    # the read-freshness window nor carries this request's snapshot of the
    # projects over whatever landed during the Claude call. Its
    # _summary_ref_order guard is #122/#140's ref-position coupling, reused
    # rather than restated.
    _attach_regenerated_summary(projects, summary_data)
    return summary_data, "generated"


def summary_fragment(request):
    """The KI-Wochenübersicht as a rendered block, generated on demand (#156).

    `summary/` rather than a German path: this is technical routing a
    visitor never reads as a word, the rule CLAUDE.md states for
    dashboard/, refresh/ and timelapse/ (#15).

    POST, not GET. Every branch writes — the session, the day cache or
    CACHE_KEY — and the demo branch therefore has to go through the client's
    withSessionLock queue, which only exists for writes (#235). The shape is
    /timelapse/preload/'s, one surface further.

    `surface` says which markup to answer with: the dashboard's card body or
    /mein-plan/'s box. Two partials rather than one, because the two pages
    render the same summary differently (#92 owns unifying them).

    What is *not* a parameter is the context. Which of the three summaries
    this is follows from the session and from ?mode=multi, the same way
    dashboard() decides it — so the page cannot ask for a summary of
    something it is not showing, and the client needs no second copy of that
    rule. The page builds this URL and the JS only fetches it.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)

    today = timezone.localdate()

    if request.GET.get("surface") == "my_plan":
        if not settings.DEMO_MODE:
            return JsonResponse({"error": "not available"}, status=404)
        session_plan = request.session.get("demo_plan")
        if not session_plan:
            return JsonResponse({"error": "no plan"}, status=404)
        # Today, never the simulated date: /mein-plan/ reads the moment to
        # *name* it and renders the real stand (#246).
        summary_data, _status = _session_summary(request, None)
        project = _build_session_project(session_plan)
        project["display_name"] = _strip_trailing_date(project["name"])
        project["event_date_display"] = format_date(project["event_date"], role="long")
        _annotate_tasks([project], today)
        summary = (
            resolve_weekly_summary(summary_data, [project], single_project_demo=True)
            if summary_data
            else None
        )
        return render(
            request,
            "projects/_my_plan_summary_body.html",
            {
                "summary": summary,
                "summary_empty_state": _summary_empty_state(summary, [project], []),
                "summary_error": summary_data is None,
            },
        )

    force_multi = request.GET.get("mode") == "multi"
    sim_date, sim_date_str = None, None
    has_session_plan = False
    unassigned_tasks = []

    if settings.DEMO_MODE:
        session_plan = request.session.get("demo_plan")
        if session_plan and not force_multi:
            has_session_plan = True
            sim_date, sim_date_str = _get_sim_date(request)
            projects = _annotate_tasks(
                [_simulated_project(session_plan, sim_date)], sim_date or today
            )
            summary_data, _status = _session_summary(request, sim_date_str)
        else:
            projects = _annotate_tasks(get_demo_projects(), today)
            unassigned_tasks = _annotate_tasks(
                [{"id": "_unassigned", "tasks": get_demo_unassigned_tasks()}], today
            )[0]["tasks"]
            summary_data, _status = _demo_multi_summary(today)
    else:
        # One read, used twice: the summary is generated against this
        # numbering and resolved against it too, so the block cannot point
        # at tasks a write that landed mid-call moved underneath it.
        cached = cache.get(CACHE_KEY)
        summary_data, _status = _production_summary(cached, today)
        projects = cached[0] if cached else []
        # No fetch of its own, for the reason _production_summary gives: a
        # miss here only costs the empty-state note its unassigned half, and
        # the page below already renders the live list.
        unassigned_tasks = cache.get(UNASSIGNED_CACHE_KEY) or []

    for project in projects:
        project["display_name"] = _strip_trailing_date(project["name"])
        project["event_date_display"] = format_date(project["event_date"], role="long")

    # Resolved here, against the projects as they are *now* — the same
    # render-time resolution dashboard() does (#122), one request later.
    summary = (
        resolve_weekly_summary(
            summary_data, projects, single_project_demo=has_session_plan
        )
        if summary_data
        else None
    )
    return render(
        request,
        "projects/_ai_summary_body.html",
        {
            "summary": summary,
            "summary_empty_state": _summary_empty_state(
                summary, projects, unassigned_tasks
            ),
            # #145: production only, and DEMO_MODE is what says so — the
            # same rule dashboard() applies, for the same reason.
            "kontext_hint": (
                resolve_kontext_hint(summary_data)
                if summary_data and not settings.DEMO_MODE
                else ""
            ),
            "sim_date": sim_date_str,
            "viewing_demo_data": settings.DEMO_MODE and not has_session_plan,
        },
    )


def timelapse_moments(request):
    """Generates the plan's narrative Zeitreise moments, out of the request
    that created the plan (#156).

    planner_create used to make this Haiku call inline, between "Zum
    Dashboard" and the dashboard — and the dashboard then made the summary
    call, so the visitor waited twice with nothing on screen for either. The
    same shape as the summary endpoint: the call blocks its own XHR and the
    page is already up.

    demo_timelapse_pending is what says a plan is still owed its moments, and
    it is cleared *before* the call rather than after. A failure must not be
    retried on every dashboard load — one plan, one attempt — which is what
    planner_create's try/except has always bought.

    The broad except is that try/except, moved: anything this call raises
    must not cost the visitor the plan they just waited for.
    generate_timelapse_moments already narrows API failures to
    AIUnavailableError, so what is caught here is the unexpected rest.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    if not settings.DEMO_MODE:
        return JsonResponse({"error": "not available"}, status=404)
    session_plan = request.session.get("demo_plan")
    if not request.session.pop("demo_timelapse_pending", False) or not session_plan:
        return JsonResponse({"moments": []})
    try:
        moments = generate_timelapse_moments(
            session_plan["name"],
            date.fromisoformat(session_plan["event_date"]),
            session_plan["tasks"],
        )
    except Exception:  # noqa: BLE001 — deliberate: the moments are a garnish
        return JsonResponse({"moments": []})
    request.session["demo_timelapse_moments"] = moments
    return JsonResponse({"moments": moments})


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
        # #217: a Zeitreise moment is a rendering of a date, not a place to
        # check things off. dashboard() forces every task due by sim_date to
        # done, so the write would land in the session and the page would
        # still render exactly as before — and a visitor cannot tell that
        # from nothing having happened. The templates offer no button while
        # a moment is active (_task_dot.html); a POST arriving anyway is the
        # same honest miss as an unknown task, refused before it writes.
        if sim_date:
            return JsonResponse({"error": "simulated moment is read-only"}, status=404)
        today = timezone.localdate()
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
        task["completed_date"] = today.isoformat() if done else None
        request.session["demo_plan"] = plan
        # _build_session_project builds its task dicts fresh out of the
        # session, so there is nothing shared to copy before annotating —
        # the deepcopy this used to make existed only for the forced-done
        # mutation above, which no longer happens here.
        projects = _annotate_tasks([_build_session_project(plan)], today)
        figures = _toggle_answer(
            projects,
            [],
            task_id,
            today,
            _parse_week_start(data, iso_week_bounds(today)[0]),
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


def _parse_posted_name(request_data):
    """Returns (name, error_response) from an already-parsed body. #154/#61's
    shape: a name is only a name once it has something in it — an empty
    rename would leave a row nothing identifies it by, and an added task
    nothing to call it.

    Takes the dict rather than the request, the way reschedule_task_view
    already reads its own date: #148 needs the project id out of the same
    body, and parsing it twice to get two fields out of it is one parse too
    many."""
    name = request_data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, JsonResponse({"error": "invalid name"}, status=400)
    return name.strip(), None


def rename_task_view(request, task_id):
    """#239 stage 2. Shaped like toggle_task_view: POST only, a 400 on a
    malformed body, a 404 for a task that was never saved rather than a
    cheerful ok (#61).

    No figures in the answer — a rename changes no count, no stage and no
    position. It is the one write that moves nothing."""
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data, error = _parse_json_dict_body(request)
    if error:
        return error
    name, error = _parse_posted_name(data)
    if error:
        return error
    if settings.DEMO_MODE:
        # #217: a Zeitreise moment is a rendering of a date, not a place to
        # change things. Same refusal as toggle_task_view, and the menu
        # offers no item while one is active.
        sim_date, _ = _get_sim_date(request)
        if sim_date:
            return JsonResponse({"error": "simulated moment is read-only"}, status=404)
        plan = request.session.get("demo_plan")
        task = (
            next((t for t in plan["tasks"] if t["id"] == task_id), None)
            if plan
            else None
        )
        if task is None:
            return JsonResponse({"error": "unknown task"}, status=404)
        task["name"] = name
        request.session["demo_plan"] = plan
    else:
        try:
            rename_task(task_id, name)
        except NotionUnavailableError:
            return JsonResponse({"error": "notion unavailable"}, status=502)

        # #199: a rename moves nothing in the chronological order
        # (_annotate_tasks sorts by due date), so the cached lists carry it
        # rather than being thrown away and re-read from Notion at a Claude
        # call's expense. _remap_summary_refs is then a no-op over an
        # unchanged order, which is exactly what a toggle already relies on.
        def relabel(task):
            task["name"] = name

        if _patch_cached_tasks(task_id, relabel, timezone.localdate()) is None:
            _bust_dashboard_cache()
    return JsonResponse({"ok": True, "name": name})


def trash_task_view(request, task_id):
    """#239 stage 3. Same shape as rename_task_view above — POST only, a 404
    for a task that was never saved (#61), refused while a Zeitreise moment
    is active (#217).

    The cache is busted rather than patched, deliberately.
    _patch_cached_tasks mutates in place and has no removal path, and a
    removal shifts every count *and* every cached task_ref, since
    _number_projects_and_tasks numbers by position. _remap_summary_refs
    exists for exactly that and could carry it, but _patch_cached_tasks
    would have to give up its mutate(task) signature to get there — and that
    is the one place every other write hangs off. A removal is rare; the
    fallback costs one Notion read and, because the summary lives in the
    same entry, one Claude call. That is the price of not reshaping the
    patch path for the least frequent write in the app.

    The answer therefore carries no figures — there is nothing warm left to
    derive them from — and the client reloads. Which is also the strongest
    form of #210: every count, progress bar and board badge is re-rendered
    by the server rather than reconciled by hand.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    _, error = _parse_json_dict_body(request)
    if error:
        return error
    if settings.DEMO_MODE:
        sim_date, _ = _get_sim_date(request)
        if sim_date:
            return JsonResponse({"error": "simulated moment is read-only"}, status=404)
        plan = request.session.get("demo_plan")
        task = (
            next((t for t in plan["tasks"] if t["id"] == task_id), None)
            if plan
            else None
        )
        if task is None:
            return JsonResponse({"error": "unknown task"}, status=404)
        plan["tasks"] = [t for t in plan["tasks"] if t["id"] != task_id]
        request.session["demo_plan"] = plan
        # The cached summaries were numbered against an order this task was
        # part of, so they cannot be rewritten — a ref no longer points at
        # the task it was written for. Swept rather than remapped, the same
        # treatment reschedule_task_view gives a summary it cannot renumber.
        for key in list(request.session.keys()):
            if key.startswith("demo_plan_summary"):
                del request.session[key]
    else:
        try:
            trash_task(task_id)
        except NotionUnavailableError:
            return JsonResponse({"error": "notion unavailable"}, status=502)
        _bust_dashboard_cache()
    return JsonResponse({"ok": True})


def trash_project_view(request, project_id):
    """#284: the project and its tasks to Notion's trash, together.

    Production only, and that is what the write is rather than a gate on
    top of it. A demo visitor sees two kinds of project and neither has a
    page to archive: the example projects are in no session (#10 §5), and a
    session plan is not one project among several but the sitting itself —
    discarding it is a different action from archiving a Notion page, and
    one this endpoint would answer wrongly by doing half of it.

    The tasks go first. trash_project's docstring says why they cannot be
    left: a task whose project page is in the trash still points at that
    page, so get_unassigned_tasks (relation.is_empty, #53) does not find it
    either — it would be invisible in the app and alive in Notion, the
    silent write #217 refuses in the other direction.

    *Which* tasks is asked of Notion rather than of the cache, and
    get_exclusive_task_ids owns both halves of that answer: the cached entry
    can be eight hours old, and a task that also belongs to another project
    is reachable there and stays.

    A partial failure therefore errs toward visible rather than tidy. Tasks
    that are already trashed stay trashed and the project stays live, so the
    project is still on the dashboard with fewer tasks under it and the
    action can simply be repeated. The reverse order would leave the
    opposite: a vanished project and tasks no read can reach.

    The cache is busted rather than patched, the answer trash_task_view
    gives for the same reason one level down — a removal shifts every count
    and every cached task_ref, and _patch_cached_tasks has no removal path.
    Here it is not even close: the projects list itself is shorter, which
    moves the month groups, the sidebar and every figure derived from them.
    The client reloads.

    How much is busted follows whether the write landed. A confirmed removal
    takes the stale copy with it; a partial failure drops the fresh entries
    and leaves the fallback, because this endpoint only fails that way when
    Notion is unreachable and that copy is what an unreachable Notion gets
    rendered from (_drop_fresh_dashboard_cache).
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    _, error = _parse_json_dict_body(request)
    if error:
        return error
    if settings.DEMO_MODE:
        return JsonResponse({"error": "not available"}, status=404)
    cached = cache.get(CACHE_KEY)
    projects = cached[0] if cached else []
    if not any(project["id"] == project_id for project in projects):
        # The entry is asked whether the app knows this project, and nothing
        # else — the task list comes from Notion below. The page that offered
        # the control rendered from this entry and from no other (the menu is
        # not in a stale render, dashboard.html), so a miss means the entry
        # went cold or the project is already gone. Either way the next load
        # is right, and archiving a page the app cannot currently see is the
        # one thing this endpoint must not do on a guess.
        return JsonResponse({"error": "unknown project"}, status=404)
    try:
        task_ids = get_exclusive_task_ids(project_id)
    except NotionUnavailableError:
        # Nothing has been written, so the cached entry is still true — the
        # one failure on this path that costs the cache nothing.
        return JsonResponse({"error": "notion unavailable"}, status=502)
    try:
        for task_id in task_ids:
            trash_task(task_id)
        trash_project(project_id)
    except NotionUnavailableError:
        # Whatever landed before the failure stays landed, so the fresh
        # entries are dropped on the way out — the stale pair stays, since an
        # unreachable Notion is what this failure is made of.
        _drop_fresh_dashboard_cache()
        return JsonResponse({"error": "notion unavailable"}, status=502)
    _bust_dashboard_cache()
    return JsonResponse({"ok": True})


def _parse_posted_task_date(request_data):
    """Returns (iso_string, error_response). A date is mandatory, unlike the
    timelapse's — an absent one there clears the simulated moment, while a
    dateless task would drop out of every list this app sorts and buckets by
    date (_annotate_tasks, _bucket_by_day, the export).

    Parsed to prove it is a date, and handed on as what that date spells
    rather than as what arrived. date.fromisoformat has accepted every ISO
    8601 date form since 3.11, so "20260905" and "2026-W36-5" get through it
    too — and the string is what both worlds then store and compare. The
    export sorts on it (download_plan) where "-" < "0", so a compact form
    would sort after every hyphenated date while the rendered list, which
    sorts on a real date object, put it in the right place. One canonical
    spelling in, one order out."""
    raw = request_data.get("date")
    try:
        parsed = date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None, JsonResponse({"error": "invalid date"}, status=400)
    return parsed.isoformat(), None


def _next_demo_task_id(tasks):
    """The id for a task appended to a demo session plan.

    Not len(tasks): planner_create numbers by enumerate, so after a trash an
    id derived from the length re-uses one that is still live — and every
    write addresses a task by id. Highest existing suffix + 1 instead,
    stepping over any id that does not parse (a session written by an older
    format, or one this function itself never produced)."""
    prefix = "demo-session-"
    highest = -1
    for task in tasks:
        task_id = str(task.get("id", ""))
        # An explicit prefix test rather than rpartition: with the separator
        # absent rpartition hands the whole string back as the suffix, so an
        # id of "7" would be read as this scheme's number 7 instead of being
        # stepped over.
        if not task_id.startswith(prefix):
            continue
        suffix = task_id.removeprefix(prefix)
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{prefix}{highest + 1}"


def add_task_view(request):
    """#148: a task added to a plan that already exists.

    The project is in the body rather than in the path, unlike every other
    write here. A create has no task id to name itself with, and when this
    row later surfaces on the cross-project work list (#53's follow-up) the
    project simply becomes nullable there rather than the route becoming a
    second route.

    The cache is busted rather than patched, in both worlds. In production
    it has no choice: _patch_cached_tasks mutates a task found by id and has
    no insertion path, and the new Notion page id does not exist until the
    write returns. Demo *could* do better — _remap_summary_refs maps by task
    identity, so an insertion is losslessly remappable — and deliberately
    does not: a rescheduled task is one the summary already knew about,
    while an added task is content the summary should have mentioned and
    now cannot. Having the two worlds answer the same write differently
    costs more in reasoning than the Claude call it would save.

    So the answer carries no figures, and the client reloads. Same shape as
    trash_task_view, and the same #210 argument: every count, progress bar
    and board badge is re-rendered by the server rather than reconciled by
    hand.

    Refused under a Zeitreise moment (#217), the way toggling is and
    rescheduling is not. A task dated before the moment would be forced
    `done` by the simulated render the instant it appeared — exactly the
    invisible write #217 refused. The row is not offered there either.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data, error = _parse_json_dict_body(request)
    if error:
        return error
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return JsonResponse({"error": "invalid project"}, status=400)
    project_id = project_id.strip()
    name, error = _parse_posted_name(data)
    if error:
        return error
    task_date, error = _parse_posted_task_date(data)
    if error:
        return error

    if settings.DEMO_MODE:
        sim_date, _ = _get_sim_date(request)
        if sim_date:
            return JsonResponse({"error": "simulated moment is read-only"}, status=404)
        plan = request.session.get("demo_plan")
        # The demo example projects come from get_demo_projects() and are in
        # no session, so nothing there can be written to (#10 §5) — a 404
        # rather than a cheerful ok for something that was never saved.
        if not plan or project_id != "session-plan":
            return JsonResponse({"error": "unknown project"}, status=404)
        plan["tasks"].append(
            {
                "id": _next_demo_task_id(plan["tasks"]),
                "name": name,
                "date": task_date,
                "done": False,
            }
        )
        request.session["demo_plan"] = plan
        # Swept rather than remapped — see the docstring. The unversioned
        # prefix covers every summary version a long-lived session may still
        # carry (SUMMARY_KEY above).
        for key in list(request.session.keys()):
            if key.startswith("demo_plan_summary"):
                del request.session[key]
    else:
        try:
            create_task(project_id, name, task_date)
        except NotionUnavailableError:
            # Including an unknown project_id, which production cannot tell
            # apart from an outage without an extra read it does not need to
            # pay for.
            return JsonResponse({"error": "notion unavailable"}, status=502)
        _bust_dashboard_cache()
    return JsonResponse({"ok": True})


def reschedule_task_view(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data, error = _parse_json_dict_body(request)
    if error:
        return error
    try:
        parsed_date = date.fromisoformat(data.get("date"))
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid date"}, status=400)
    # Canonical rather than as it arrived, for _parse_posted_task_date's
    # reason: fromisoformat takes every ISO 8601 date form, and this string
    # is what the session stores and the export sorts on. Hence iso_date and
    # not raw_date — what reaches Notion and the session is the date's own
    # spelling, not the request's.
    iso_date = parsed_date.isoformat()
    due_display = format_date(parsed_date, role="long")
    # #238: two formats, because the client writes this answer into two
    # elements. The Kanban card spells the month out; the task row was
    # shortened to give the task name back the width it costs on a phone. One
    # field for both would have put the long form back into every rescheduled
    # row until the next reload.
    due_display_row = format_date(parsed_date, role="row")
    # #266: the close-out triage list's move button carries the date it would
    # move to ("→ Mi, 24. Juni"), so a manual pick has to relabel it. Derived
    # here rather than in the client for the reason #189/#192 drew the line:
    # format_date is the one place that knows German date formatting, and the
    # alternative is a second copy of the month names in JavaScript.
    next_week_display = format_date(parsed_date + timedelta(days=7), role="long")

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
        # Read before the move, for the same reason _patch_cached_tasks
        # reads before its own: the summaries below are numbered against
        # this order.
        numbered_before = _session_task_order(plan, effective_today)
        task["date"] = iso_date
        # #171: awareness, not punishment — starts counting from the second
        # move, but the counter itself increments on every reschedule from
        # the first one (the badge threshold is a display concern, applied
        # in the templates).
        task["postpone_count"] = task.get("postpone_count", 0) + 1
        request.session["demo_plan"] = plan
        # The task order is chronological (#140), so a new date moves the
        # task and the cached summaries' task_refs no longer point where
        # they did (see _annotate_tasks). They are rewritten rather than
        # swept: a visitor who moves a task would otherwise wait out a
        # fresh Claude call on the next render, the same cost the
        # production branch below stopped paying. A leftover from an older
        # summary format still goes — its refs were numbered against an
        # order this code no longer produces, so they cannot be rewritten
        # (the unversioned-prefix sweep planner_create does, kept for
        # exactly those).
        numbered_after = _session_task_order(plan, timezone.localdate())
        for key in list(request.session.keys()):
            if key.startswith(SUMMARY_KEY):
                request.session[key] = _remap_summary_refs(
                    request.session[key], numbered_before, numbered_after
                )
            elif key.startswith("demo_plan_summary"):
                del request.session[key]
        postpone_count = task["postpone_count"]
        # The same moment dashboard() renders, so the figures match what a
        # reload would show.
        _, figures = _surface_figures(
            _annotate_tasks([_simulated_project(plan, sim_date)], effective_today),
            [],
            task_id,
            effective_today,
            browsed_monday,
            # #183: a demo session's bar counts the whole plan, not the week.
            whole_plan=True,
        )
    else:
        try:
            update_task_date(task_id, iso_date)
        except NotionUnavailableError:
            return JsonResponse({"error": "notion unavailable"}, status=502)
        # Applied right away, before the counter call: the date change is
        # already confirmed in Notion at this point, so a failure below must
        # not leave the cache serving the pre-move date (_bust_dashboard_cache
        # promises this for "every confirmed Notion write").
        #
        # #199: the projects survive the move — re-sorted and re-annotated.
        # So does the summary: a new date renumbers the task_refs it holds
        # (_number_projects_and_tasks, ai.py), and those get rewritten
        # rather than thrown away (_remap_summary_refs). Neither the Notion
        # read nor the Claude call is paid for again.
        #
        # effective_today rather than a second timezone.localdate() call:
        # this branch only runs outside DEMO_MODE, where the two are the same
        # day, and one value keeps the figures below from being derived
        # against a different date than the urgency answered with.

        def move(task):
            task["due"] = parsed_date

        if _patch_cached_tasks(task_id, move, effective_today) is None:
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
            "due_display_row": due_display_row,
            "next_week_display": next_week_display,
            "urgency": _classify_due_urgency(parsed_date, effective_today),
            **figures,
        }
    )


def reschedule_project_view(request, project_id):
    """#283: a project's event date, and — if that is what was asked for —
    its tasks' dates with it.

    Shifting a concert by a week shifts its posters, its press text and its
    GEMA filing by a week too; correcting a date that was a day off must
    touch nothing. Both are real, so the write does not guess: the client
    asks first (the confirmation bar below `.project-header`,
    dashboard.html) and `move_tasks` carries the answer. It must be a real
    boolean — an absent one is a 400 rather than a default, because
    defaulting it either way would move tasks nobody consented to or leave
    behind tasks somebody did.

    **The delta comes from the cache; the tasks come from Notion.** That
    looks inconsistent and is the point. The visitor confirmed "+7 Tage, 12
    Aufgaben" against the date the page showed them, which came out of
    CACHE_KEY — a server that re-read Termin from Notion and recomputed the
    difference would shift by an amount nobody agreed to. The *tasks* are
    shifted relative to their own dates, so Notion's truth is the right base
    there: a task somebody rescheduled in Notion's own UI since the entry
    was read moves from its real date. A cold cache is therefore a 404, for
    the reason trash_project_view gives one — the delta cannot be
    reconstructed from anything the visitor saw.

    Relative rather than recomputed from a lead time, because Notion stores
    none: only the absolute Wann?. `new = due + delta` is the same
    arithmetic the planner review does with `event_date - days_before`
    (updateDates, planner_review.html), so a task rescheduled by hand keeps
    its own offset either way. One difference, deliberate: the review clamps
    to today and this does not. A task shifted into the past is overdue, and
    _classify_due_urgency has an honest state for that; clamping would
    silently collapse several tasks onto one date.

    **Only open, dated tasks move.** A completed task's date records when
    the work was due and met, and moving it rewrites the plan's own history;
    an undated task has no date to shift. The green dots are unaffected
    either way — "done this week" hangs off completed_date, not off the due
    date (_annotate_tasks).

    **The number the bar named comes back and is checked** (`shift_count`,
    required exactly when move_tasks is true — with nothing moving there is
    no count to consent to). The bar reads it off `shiftable_count` in a
    CACHE_KEY entry up to eight hours old, while the tasks come from a read
    taken now, so the two can genuinely disagree: a task created in Notion's
    own UI inside that window would be shifted without ever having been
    counted. A mismatch is a 409 carrying the true figure, before any write
    — the question is asked again with a number that is right rather than
    the write being carried out against a consent nobody gave. The client
    re-words the bar and leaves it standing (commitProjectDate,
    dashboard.html).

    **Project first, and no order is repeatable.** Notion has no batch
    update, so this is one write per page, in sequence. Project first and a
    retry computes a zero delta; tasks first and a retry shifts twice what
    already moved. There is no third option while Notion stores no lead time,
    so the project — the write that was actually asked for — goes first and
    the answer names how many tasks landed. Every task left behind is
    individually fixable through the date control the row already carries.
    Errs toward visible rather than tidy, which is trash_project_view's own
    stance.

    **The cache is busted, not patched.** A new event date can move the
    project between month groups and changes the order of the project list
    itself, which no existing write does — every one of them touches a task
    *inside* a project. The order comes from Notion's own sort (Termin
    ascending); reproducing it in Python, including how Notion orders equal
    dates, is a rule this app has never had to own. So the client reloads,
    which is #210 in its strongest form. How much is busted follows whether
    the write landed, exactly as it does one level down: a confirmed write
    takes the stale copy with it, a partial failure drops the fresh entries
    and leaves the fallback, because an unreachable Notion is what
    dashboard() renders that copy for.

    Both worlds, unlike the trash. A demo session plan carries an
    `event_date` and an `event_date_uncertain` of its own, and the planner's
    fallback lead time is reachable there whenever the description held no
    date — so the public demo renders "Termin unsicher" and, until now, no
    way to answer it.

    **Refused under a Zeitreise moment**, the way adding is and rescheduling
    a single task is not, and the count is what decides it. A moment renders
    every task due by sim_date as done (_simulated_project), so the page's
    shiftable_count is the count *at that moment* while the write — which
    must not depend on which moment somebody happens to be looking at, a
    moment being a view and not a write (#217) — moves every open dated task
    the plan holds. The two cannot both be right, and a question whose
    number contradicts the dots beside it is worse than a question that is
    not asked. reschedule_task_view asks no such question, which is why it
    stays available there. The control is not rendered under a moment either
    (dashboard.html), the way _task_add_row.html decides its own case.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data, error = _parse_json_dict_body(request)
    if error:
        return error
    # The same parser every task date goes through: one canonical spelling
    # in, one order out (_parse_posted_task_date).
    iso_date, error = _parse_posted_task_date(data)
    if error:
        return error
    move_tasks = data.get("move_tasks")
    if not isinstance(move_tasks, bool):
        return JsonResponse({"error": "invalid move_tasks"}, status=400)
    # The figure the bar named, carried back so the server can hold the
    # write to it. Required exactly when it means something: "Nur Termin"
    # moves nothing and so consents to no count. isinstance(True, int) is
    # True in Python, hence the explicit bool rejection — a client sending
    # the move_tasks flag twice by mistake would otherwise consent to 1.
    shift_count = data.get("shift_count")
    if move_tasks and (
        isinstance(shift_count, bool)
        or not isinstance(shift_count, int)
        or shift_count < 0
    ):
        return JsonResponse({"error": "invalid shift_count"}, status=400)
    new_date = date.fromisoformat(iso_date)

    if settings.DEMO_MODE:
        # A moment is a rendering of a date, and the count in the question
        # is what makes this write unable to live inside one — see the
        # docstring. Refused before anything is read, the way add_task_view
        # refuses, so a direct POST gets the same answer the page gives by
        # not offering the control.
        if _get_sim_date(request)[0]:
            return JsonResponse({"error": "simulated moment is read-only"}, status=404)
        # Only the visitor's own session plan can be written to; the example
        # projects come from get_demo_projects() and are in no session (#10
        # §5), so a 404 rather than a cheerful ok for something that was
        # never saved.
        plan = request.session.get("demo_plan")
        if not plan or project_id != "session-plan":
            return JsonResponse({"error": "unknown project"}, status=404)
        old_date = plan.get("event_date")
        delta = new_date - date.fromisoformat(old_date) if old_date else None
        if move_tasks and not delta:
            return JsonResponse({"error": "no previous date"}, status=400)
        # Only built when something is going to move: "Nur Termin" leaves
        # every task alone, so the list is empty and the loop below a no-op.
        movable = (
            [t for t in plan["tasks"] if not t.get("done") and t.get("date")]
            if move_tasks
            else []
        )
        if move_tasks and len(movable) != shift_count:
            # The same check production makes, for the narrower version of
            # the same reason: the page this answer was given on can have
            # been rendered before another tab added or checked off a task.
            # Both worlds answer one write the same way.
            return JsonResponse(
                {"error": "count changed", "shift_count": len(movable)}, status=409
            )
        # Read before the shift, for _patch_cached_tasks' reason: the
        # cached summaries' task_refs are positions in this order.
        numbered_before = _session_task_order(plan, timezone.localdate())
        plan["event_date"] = iso_date
        # A picked date is a date somebody has looked at, which is what the
        # flag asks about — the same answer clearDateUncertain() gives a
        # manual edit in the planner review.
        plan["event_date_uncertain"] = False
        for task in movable:
            task["date"] = (date.fromisoformat(task["date"]) + delta).isoformat()
        moved = len(movable)
        request.session["demo_plan"] = plan
        # The shift moves only the open tasks, so it can re-order a plan
        # whose done tasks stay put — rewritten rather than swept, the way
        # a reschedule rewrites them (reschedule_task_view). An unchanged
        # order is a no-op inside _remap_summary_refs, so this costs
        # nothing in the common case. A leftover from an older summary
        # format still goes: its refs were numbered against an order this
        # code no longer produces.
        numbered_after = _session_task_order(plan, timezone.localdate())
        for key in list(request.session.keys()):
            if key.startswith(SUMMARY_KEY):
                request.session[key] = _remap_summary_refs(
                    request.session[key], numbered_before, numbered_after
                )
            elif key.startswith("demo_plan_summary"):
                del request.session[key]
        return JsonResponse({"ok": True, "moved": moved, "partial": False})

    cached = cache.get(CACHE_KEY)
    project = next(
        (p for p in (cached[0] if cached else []) if p["id"] == project_id), None
    )
    if project is None:
        # Both a cold cache and an unknown project. The page that offered
        # the control rendered from this entry and from no other (the date
        # is a <span> in a stale render, dashboard.html), so a miss means
        # the entry went cold or the project is gone — and the delta the
        # visitor consented to was read off that entry, so there is nothing
        # left to honour it against.
        return JsonResponse({"error": "unknown project"}, status=404)
    delta = new_date - project["event_date"] if project["event_date"] else None
    if move_tasks and not delta:
        # A project Notion holds with no Termin at all has no difference to
        # shift by, and neither does a pick that lands on the date already
        # there. The page offers neither case — a dateless project renders
        # the span rather than the control, and an <input type="date"> fires
        # no change for an unchanged value — so this is the direct POST.
        return JsonResponse({"error": "no previous date"}, status=400)
    try:
        # Before any write, so a failed read costs the cache nothing — and
        # asked of Notion rather than of the entry above for
        # get_exclusive_tasks' own reason: that entry can be eight hours
        # old, and a task created or moved in Notion's UI inside that window
        # has to shift from its real date.
        tasks = get_exclusive_tasks(project_id) if move_tasks else []
    except NotionUnavailableError:
        return JsonResponse(
            {"error": "notion unavailable", "partial": False}, status=502
        )
    # The same rule _annotate_tasks counts by, so the figure checked below
    # is the figure the bar named rather than a second reading of what
    # "open" means.
    movable = [task for task in tasks if not task["done"] and task["due"]]
    if move_tasks and len(movable) != shift_count:
        # Still before any write: the number the visitor agreed to was read
        # off a cache entry up to eight hours old, and this read is of now.
        # The question goes back with the true figure rather than the write
        # going ahead against a consent for a different one.
        return JsonResponse(
            {"error": "count changed", "shift_count": len(movable)}, status=409
        )
    try:
        update_project_date(project_id, iso_date)
    except NotionUnavailableError:
        # Nothing landed, so the entry this request read is still true.
        return JsonResponse(
            {"error": "notion unavailable", "partial": False}, status=502
        )
    moved = 0
    try:
        for task in movable:
            update_task_date(task["id"], (task["due"] + delta).isoformat())
            moved += 1
    except NotionUnavailableError:
        # The project date has already moved and some tasks may have, so the
        # fresh entries go; the stale pair stays, since an unreachable
        # Notion is what this failure is made of (_drop_fresh_dashboard_cache).
        _drop_fresh_dashboard_cache()
        return JsonResponse(
            {"error": "notion unavailable", "partial": True, "moved": moved},
            status=502,
        )
    _bust_dashboard_cache()
    return JsonResponse({"ok": True, "moved": moved, "partial": False})


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
    # #246: read to be *named*, never to be rendered from. The list, the
    # counter, the progress bar and the sidebar ring all stay on today — the
    # boundary from "The Zeitreise stays a dashboard device" is unchanged, it
    # is only labelled now. Without the label a task the dashboard shows
    # struck through stands open here with nothing saying why.
    # _get_sim_date, not session.get: it heals a value written before the
    # moments were validated, the same way the dashboard is protected.
    sim_date = None
    if settings.DEMO_MODE:
        sim_date, _ = _get_sim_date(request)
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

    # #156: read, never generated — same as dashboard(). An absent entry is
    # the loading state, and summary_fragment() fills it from the same key.
    # summary_error is therefore unreachable from here: the only thing that
    # can fail now is the fragment request, which renders that state itself.
    summary_data = request.session.get(f"{SUMMARY_KEY}_today")
    summary_pending = not summary_data

    # Resolved against the live session plan at render time (#122) — the
    # session-cached raw dict never carries done state.
    summary = (
        resolve_weekly_summary(summary_data, [project], single_project_demo=True)
        if summary_data
        else None
    )
    summary_empty_state = _summary_empty_state(summary, [project], [])

    return render(
        request,
        "projects/my_plan.html",
        {
            "project": project,
            "done_count": done_count,
            "total": total,
            "today": today,
            "today_display": format_date(today, role="long"),
            "sim_date": sim_date,
            "sim_date_display": format_date(sim_date, role="long") if sim_date else "",
            "summary": summary,
            "summary_empty_state": summary_empty_state,
            "summary_pending": summary_pending,
            "summary_url": reverse("summary_fragment") + "?surface=my_plan",
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

    # Sorted rather than iterated in session order (#148). That order only
    # ever happened to be chronological because planner_review sorted it
    # once: reschedule_task_view rewrites task["date"] in place and never
    # re-sorts, and an added task is appended. Same key _annotate_tasks
    # sorts the rendered lists by, so the export and the page agree.
    for t in sorted(
        plan["tasks"], key=lambda t: (not t.get("date"), t.get("date") or "")
    ):
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
    # #246: no sim_date guard here, unlike toggle_task_view. The rule is that
    # a write is offered where it takes effect, not that a moment locks the
    # session plan: my_plan() never reads sim_date, renders the real state on
    # the real date and keeps a live button.dot (_task_dot.html drops one only
    # on the dashboard, where the render forces done and would swallow the
    # write). A guard here would refuse a toggle the visitor can see land.
    plan = request.session.get("demo_plan")
    task = (
        next((t for t in plan["tasks"] if t["id"] == task_id), None) if plan else None
    )
    if task is None:
        return JsonResponse({"error": "unknown task"}, status=404)
    today = timezone.localdate()
    task["done"] = done
    # #246: the same pairing toggle_task_view writes (#19), and the same one
    # _count_done_in_range's docstring already promises for "any task toggled
    # through this app". Without it a task cleared here reached
    # _demo_completed_in_range with no completion date, so the week close-out
    # placed it by its due date or not at all.
    task["completed_date"] = today.isoformat() if done else None
    request.session["demo_plan"] = plan
    return JsonResponse({"ok": True})
