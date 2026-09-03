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
    generate_closeout_summary,
    generate_weekly_summary,
    resolve_weekly_summary,
)
from .closeout import get_latest_closeout, is_week_closed, save_closeout
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

# #14: kept rather than switched to Django's l10n date formatting. Every date
# display that reads LANGUAGE_CODE-dependent formatting (dashboard, kanban,
# /mein-plan/, /stats/, planner review, Markdown export) goes through these
# tables or _format_date(), not Django's |date filter — the one |date use in
# dashboard.html is a fully numeric, locale-invariant format. Removing these
# would buy nothing.
MONTHS_DE = {
    1: "Januar",
    2: "Februar",
    3: "März",
    4: "April",
    5: "Mai",
    6: "Juni",
    7: "Juli",
    8: "August",
    9: "September",
    10: "Oktober",
    11: "November",
    12: "Dezember",
}
MONTHS_SHORT = {
    1: "Jan",
    2: "Feb",
    3: "Mär",
    4: "Apr",
    5: "Mai",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Okt",
    11: "Nov",
    12: "Dez",
}
WEEKDAYS_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _format_date(d):
    if not d:
        return ""
    weekday = WEEKDAYS_SHORT[d.weekday()]
    return f"{weekday}, {d.day}. {MONTHS_DE[d.month]}"


# Both cache keys and SUMMARY_KEY store the summary's raw reference dict
# (#122), so they carry a version that is bumped on every format change
# (#20: v2/v4; #122: v3/v5; #140: v4/v6 — task order changed, and pre-deploy
# task_refs were numbered against the unsorted order; #160: v5 — the stored
# projects are annotated, and a pre-deploy entry would keep rendering open
# undated tasks as done; #169/#171: v6 — urgent is calendar-week based now,
# and task dicts carry a new postpone_count; #19: v7 — task dicts carry a
# new completed_date, and a pre-deploy entry would undercount "done this
# week" until it expires) — otherwise a pre-deploy entry in the old shape
# would crash or misrender under the new resolver.
CACHE_KEY = "dashboard_data_v7"
CACHE_TTL = 60 * 60 * 8  # 8 hours
# Written alongside CACHE_KEY on every successful fetch, never expired — the
# fallback dashboard() serves when a fresh Notion read fails and the primary
# entry has already expired. See DashboardNotionFailureTest.
STALE_CACHE_KEY = "dashboard_data_stale_v7"

# #53: a separate key pair rather than folded into CACHE_KEY's tuple — this
# is an independent Notion read (get_unassigned_tasks carries no AI summary,
# no partial-success nuance) and keeping it apart avoids reshaping the
# already-tested primary tuple for every existing cache test.
# #19: v2 — task dicts carry the same new completed_date field.
UNASSIGNED_CACHE_KEY = "dashboard_unassigned_v2"
UNASSIGNED_CACHE_TTL = 60 * 60 * 8  # 8 hours, same as CACHE_TTL
STALE_UNASSIGNED_CACHE_KEY = "dashboard_unassigned_stale_v2"


def _bust_dashboard_cache():
    """Called after every confirmed Notion write, so a completed task or a
    freshly saved project never hides behind CACHE_TTL or the stale fallback."""
    cache.delete(CACHE_KEY)
    cache.delete(STALE_CACHE_KEY)
    cache.delete(UNASSIGNED_CACHE_KEY)
    cache.delete(STALE_UNASSIGNED_CACHE_KEY)


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
            elif not task["due"]:
                # An open task without a date is its own state (#160) — no
                # deadline pressure, so it never lifts the project urgency.
                task["urgency"] = "undated"
            elif task["due"] < today:
                task["urgency"] = "overdue"
            elif task["due"] == today:
                task["urgency"] = "today"
            elif is_same_iso_week(task["due"], today):
                # #169: calendar-week based, not a rolling 7-day window — a
                # task due next week is not urgent today. Closing the week
                # (see closeout.py) is what arms next week's signal, and it
                # does that for free just by the calendar rolling over.
                task["urgency"] = "urgent"
            else:
                task["urgency"] = "ok"
            if _URGENCY_RANK[task["urgency"]] > _URGENCY_RANK[project_urgency]:
                project_urgency = task["urgency"]
            task["due_display"] = _format_date(task["due"])
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


_WEEK_PARAM_RE = re.compile(r"(\d{4})-W(\d{2})")


def _parse_week_param(request, default_monday):
    """#180: ?week=2026-W37 navigates the day columns to that week. Anything
    unparseable — absent, malformed, or a week number ISO doesn't have —
    falls back to default_monday rather than erroring the whole page over a
    query param a visitor is free to hand-edit."""
    raw = request.GET.get("week")
    if not raw:
        return default_monday
    match = _WEEK_PARAM_RE.fullmatch(raw)
    if not match:
        return default_monday
    try:
        return date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        return default_monday


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


def _format_week_range(monday, sunday):
    if monday.month == sunday.month:
        return f"{monday.day}.–{sunday.day}. {MONTHS_DE[sunday.month]}"
    return (
        f"{monday.day}. {MONTHS_SHORT[monday.month]} – "
        f"{sunday.day}. {MONTHS_DE[sunday.month]}"
    )


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
        project["event_date_display"] = _format_date(project["event_date"])
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
                    cache.set(CACHE_KEY, (projects, summary_data), CACHE_TTL)
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
                cache.set(UNASSIGNED_CACHE_KEY, unassigned_tasks, UNASSIGNED_CACHE_TTL)
                cache.set(STALE_UNASSIGNED_CACHE_KEY, unassigned_tasks, None)

    viewing_demo_data = settings.DEMO_MODE and not has_session_plan

    for project in projects:
        project["display_name"] = _strip_trailing_date(project["name"])
        project["event_date_display"] = _format_date(project["event_date"])

    week_view = _build_week_view(projects, unassigned_tasks)

    # #19: the week progress bar — counted against the calendar week
    # containing effective_today (not `today`, so demo time-travel moves the
    # bar along with everything else).
    #
    # #182: deliberately excludes unassigned_tasks, unlike week_view above.
    # The Kanban board beneath this bar renders strictly from
    # project["tasks"] and can never show a project-less task, so counting
    # one here made the bar and the board disagree on the same number.
    week_start, week_end = iso_week_bounds(effective_today)
    all_tasks = [t for project in projects for t in project["tasks"]]
    if has_session_plan:
        # #183 follow-up: a week-scoped count barely moved between Zeitreise
        # moments, often showing 0/0 several moments in a row — the story
        # the bar should tell here is the plan's overall completion, which
        # does visibly progress: a moment marks every task due on/before it
        # "done" (see the deepcopy mutation above), so the whole-plan count
        # fills up moment to moment the way the week-scoped one didn't.
        week_done_count = sum(1 for t in all_tasks if t["done"])
        week_total_count = len(all_tasks)
    else:
        week_done_count, week_total_count = _count_done_in_range(
            all_tasks, week_start, week_end
        )
    week_progress_pct = (
        round(week_done_count / week_total_count * 100) if week_total_count else 0
    )

    # #180: the day-column breakdown can browse any week, independent of
    # effective_today — week_start above stays the *current* week for the
    # progress bar even while these columns show a different one.
    browsed_monday = _parse_week_param(request, week_start)
    browsed_sunday = browsed_monday + timedelta(days=6)
    prev_monday = browsed_monday - timedelta(days=7)
    next_monday = browsed_monday + timedelta(days=7)
    is_current_week = browsed_monday == week_start
    day_columns = _bucket_by_day(projects, unassigned_tasks, browsed_monday)

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
            "today_display": _format_date(today),
            "today_iso": today.isoformat(),
            "has_session_plan": has_session_plan,
            "plan_exists": plan_exists,
            "force_multi": force_multi,
            "viewing_demo_data": viewing_demo_data,
            "demo_mode": settings.DEMO_MODE,
            "timelapse_moments": json.dumps(timelapse_moments),
            "precached_moments": json.dumps(precached_moments),
            "sim_date": sim_date_str,
            "sim_date_display": _format_date(sim_date) if sim_date else "",
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
            "day_columns": day_columns,
            "week_range_label": _format_week_range(browsed_monday, browsed_sunday),
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
    else:
        completed_date = timezone.localdate().isoformat() if done else None
        try:
            toggle_task(task_id, done, completed_date)
        except NotionUnavailableError:
            # A non-200 so the caller knows not to apply its optimistic
            # update — see the dashboard.html JS changes in the same commit.
            return JsonResponse({"error": "notion unavailable"}, status=502)
        _bust_dashboard_cache()
    return JsonResponse({"ok": True})


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
    due_display = _format_date(parsed_date)

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
    else:
        try:
            update_task_date(task_id, raw_date)
        except NotionUnavailableError:
            return JsonResponse({"error": "notion unavailable"}, status=502)
        # Busted right away, before the counter call: the date change is
        # already confirmed in Notion at this point, so a failure below must
        # not leave the cache serving the pre-move date (_bust_dashboard_cache
        # promises this for "every confirmed Notion write").
        _bust_dashboard_cache()
        try:
            # #171 accepted gap: if this second call fails, the date has
            # already moved but the counter hasn't — reported as the same
            # 502 below, self-healing on the next reschedule.
            postpone_count = increment_postpone_count(task_id)
        except NotionUnavailableError:
            return JsonResponse({"error": "notion unavailable"}, status=502)
    return JsonResponse(
        {"ok": True, "postpone_count": postpone_count, "due_display": due_display}
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
        task["due_display"] = _format_date(task["due"])
        # The move button's own label — otherwise "→ nächste Woche" doesn't
        # say which date that actually is.
        task["next_week_display"] = _format_date(task["due"] + timedelta(days=7))
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
    # render) and rewrites `due_display` to the same value, and it sorts
    # project["tasks"], not the separate open_this_week list built above.
    month_groups, years = _sidebar_projects(request, today, projects=projects)
    return render(
        request,
        "projects/close_week_start.html",
        {
            "tasks": open_this_week,
            "already_closed": already_closed,
            "today_display": _format_date(today),
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
    project["event_date_display"] = _format_date(project["event_date"])
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
            "today_display": _format_date(today),
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
    event_display = _format_date(event_date)

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
        due_str = f" — {_format_date(due)}" if due else ""
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
