from datetime import date
import re
import markdown
import json
from django.shortcuts import render, redirect
from django.core.cache import cache
from django.http import JsonResponse
from django.conf import settings
from .notion import get_upcoming_projects, toggle_task, update_task_date
from .ai import generate_weekly_summary, derive_kontext
from .demo_data import get_demo_projects

MONTHS_DE = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April",
    5: "Mai", 6: "Juni", 7: "Juli", 8: "August",
    9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
}
MONTHS_SHORT = {
    1: "Jan", 2: "Feb", 3: "Mär", 4: "Apr",
    5: "Mai", 6: "Jun", 7: "Jul", 8: "Aug",
    9: "Sep", 10: "Okt", 11: "Nov", 12: "Dez",
}
WEEKDAYS_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _format_date(d):
    if not d:
        return ""
    weekday = WEEKDAYS_SHORT[d.weekday()]
    return f"{weekday}, {d.day}. {MONTHS_DE[d.month]}"

CACHE_KEY = "dashboard_data"
CACHE_TTL = 60 * 60 * 8  # 8 Stunden


def _annotate_tasks(projects, today):
    for project in projects:
        project_urgency = "ok"
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
            if not task["kontext"]:
                task["kontext"] = derive_kontext(task["name"])
        project["urgency"] = project_urgency
    return projects


def _fix_ai_markdown(text: str) -> str:
    lines = text.split('\n')
    result = []
    in_project = False
    for line in lines:
        if line.startswith('- **'):
            in_project = True
            result.append(line)
        elif line.startswith(('---', '**')):
            in_project = False
            result.append(line)
        elif in_project and not line.strip():
            pass  # Leerzeilen innerhalb eines Projekts überspringen
        elif in_project and not line.strip().startswith(('-', '#', '>')):
            result.append(f'    - {line.strip()}')
        else:
            result.append(line)
    return '\n'.join(result)


def _fetch_fresh_data(today):
    projects = get_upcoming_projects(today)
    projects = _annotate_tasks(projects, today)
    summary_md = generate_weekly_summary(projects, today)
    summary = markdown.markdown(_fix_ai_markdown(summary_md))
    return projects, summary


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


def _strip_year(name):
    return re.sub(r'\s+\d{4}$', '', name).strip()


def dashboard(request):
    today = date.today()

    if settings.DEMO_MODE:
        projects = _annotate_tasks(get_demo_projects(), today)
        summary = markdown.markdown(_fix_ai_markdown(generate_weekly_summary(projects, today)))
    else:
        cached = cache.get(CACHE_KEY)
        if cached:
            projects, summary = cached
        else:
            projects, summary = _fetch_fresh_data(today)
            cache.set(CACHE_KEY, (projects, summary), CACHE_TTL)

    for project in projects:
        project["display_name"] = _strip_year(project["name"])
        project["event_date_display"] = _format_date(project["event_date"])

    month_groups = _group_by_month(projects)
    years = sorted({g["year"] for g in month_groups if g["year"]})

    project_map = {
        p['display_name']: p['id']
        for group in month_groups
        for p in group['projects']
    }

    return render(request, 'projects/dashboard.html', {
        'month_groups': month_groups,
        'years': years,
        'summary': summary,
        'today': today,
        'today_display': _format_date(today),
        'today_iso': today.isoformat(),
        'project_map': json.dumps(project_map),
    })


def refresh(request):
    if request.method == "POST":
        cache.delete(CACHE_KEY)
    return redirect("dashboard")

def toggle_task_view(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "methon not allowed"}, status=405)
    
    data =json.loads(request.body)
    done = data["done"]
    if not settings.DEMO_MODE:
        toggle_task(task_id, done)
    return JsonResponse({"ok": True})


def reschedule_task_view(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data = json.loads(request.body)
    if not settings.DEMO_MODE:
        update_task_date(task_id, data["date"])
    return JsonResponse({"ok": True})