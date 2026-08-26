import copy
import json
import logging
import math
import re
from datetime import date

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from .ai import AIUnavailableError, generate_weekly_summary, resolve_weekly_summary
from .demo_data import get_demo_projects
from .models import DemoEvent
from .notion import (
    NotionUnavailableError,
    get_upcoming_projects,
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
# (#20: v2/v4; #122: v3/v5) — otherwise a pre-deploy entry in the old shape
# would crash or misrender under the new resolver.
CACHE_KEY = "dashboard_data_v3"
CACHE_TTL = 60 * 60 * 8  # 8 hours
# Written alongside CACHE_KEY on every successful fetch, never expired — the
# fallback dashboard() serves when a fresh Notion read fails and the primary
# entry has already expired. See DashboardNotionFailureTest.
STALE_CACHE_KEY = "dashboard_data_stale_v3"


def _bust_dashboard_cache():
    """Called after every confirmed Notion write, so a completed task or a
    freshly saved project never hides behind CACHE_TTL or the stale fallback."""
    cache.delete(CACHE_KEY)
    cache.delete(STALE_CACHE_KEY)


# Session key prefix for demo summaries; planner_create clears every version
# by the unversioned "demo_plan_summary" prefix when a new plan is generated.
SUMMARY_KEY = "demo_plan_summary_v5"
# The multi-project demo summary: get_demo_projects() is a pure function of
# timezone.localdate() and holds no per-visitor data, so one Claude call per day serves
# every visitor. The day is part of the key, so a rollover invalidates by
# itself and the TTL only bounds how long one day's entry lives. The cache is
# shared across both gunicorn workers (DatabaseCache, settings.py CACHES, #52),
# so expect up to one call per day rather than one per worker.
DEMO_MULTI_SUMMARY_KEY = "demo_multi_summary_v2"
DEMO_MULTI_SUMMARY_TTL = 60 * 60 * 24

# The sidebar progress ring's geometry (#76): radius never varies, so the
# circumference is a fixed stroke-dasharray and only stroke-dashoffset moves.
RING_RADIUS = 7
RING_CIRCUMFERENCE = round(2 * math.pi * RING_RADIUS, 2)  # 43.98


def _annotate_tasks(projects, today):
    for project in projects:
        project_urgency = "ok"
        done_count = 0
        for task in project["tasks"]:
            if task["done"] or not task["due"]:
                task["urgency"] = "done"
            elif task["due"] < today:
                task["urgency"] = "overdue"
                project_urgency = "overdue"
            elif (task["due"] - today).days <= 7:
                task["urgency"] = "urgent"
                if project_urgency != "overdue":
                    project_urgency = "urgent"
            else:
                task["urgency"] = "ok"
            task["due_display"] = _format_date(task["due"])
            # Counted from task["done"] directly, not urgency == "done": a
            # task with no due date is annotated "done" above even when it
            # isn't, which would otherwise miscount it as complete.
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


# A trailing German date (12.09.2026, 1.9., …) or bare year, with an optional
# comma/dash separator — the maintainer's Notion naming habit (#134).
_TRAILING_DATE_RE = re.compile(r"[\s,–—-]+(?:\d{1,2}\.\d{1,2}\.(?:\d{4})?|\d{4})$")


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
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return None, JsonResponse({"error": "invalid json"}, status=400)
    if not isinstance(data, dict):
        return None, JsonResponse({"error": "invalid json"}, status=400)
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

    viewing_demo_data = settings.DEMO_MODE and not has_session_plan

    for project in projects:
        project["display_name"] = _strip_trailing_date(project["name"])
        project["event_date_display"] = _format_date(project["event_date"])

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
    data = json.loads(request.body)
    done = data["done"]
    if settings.DEMO_MODE:
        # Same collapse-to-404 rule as reschedule_task_view (#10 §5, #61):
        # answering ok for a task that was never saved is worse than an
        # honest miss.
        plan = request.session.get("demo_plan")
        task = (
            next((t for t in plan["tasks"] if t["id"] == task_id), None)
            if plan
            else None
        )
        if task is None:
            return JsonResponse({"error": "unknown task"}, status=404)
        task["done"] = done
        request.session["demo_plan"] = plan
    else:
        try:
            toggle_task(task_id, done)
        except NotionUnavailableError:
            # A non-200 so the caller knows not to apply its optimistic
            # update — see the dashboard.html JS changes in the same commit.
            return JsonResponse({"error": "notion unavailable"}, status=502)
        _bust_dashboard_cache()
    return JsonResponse({"ok": True})


def reschedule_task_view(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid json"}, status=400)
    raw_date = data.get("date") if isinstance(data, dict) else None
    try:
        date.fromisoformat(raw_date)
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid date"}, status=400)

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
        request.session["demo_plan"] = plan
    else:
        try:
            update_task_date(task_id, raw_date)
        except NotionUnavailableError:
            return JsonResponse({"error": "notion unavailable"}, status=502)
        _bust_dashboard_cache()
    return JsonResponse({"ok": True})


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


def impressum(request):
    return render(request, "projects/impressum.html")


def datenschutz(request):
    return render(request, "projects/datenschutz.html")


def toggle_session_task(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data = json.loads(request.body)
    done = data["done"]
    plan = request.session.get("demo_plan")
    task = (
        next((t for t in plan["tasks"] if t["id"] == task_id), None) if plan else None
    )
    if task is None:
        return JsonResponse({"error": "unknown task"}, status=404)
    task["done"] = done
    request.session["demo_plan"] = plan
    return JsonResponse({"ok": True})
