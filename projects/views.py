from datetime import date
import re
import markdown
import json
from django.shortcuts import render, redirect
from django.core.cache import cache
from django.http import JsonResponse, HttpResponse
from django.conf import settings
from .notion import get_upcoming_projects, toggle_task, update_task_date
from .ai import generate_weekly_summary, derive_kontext
import copy
from .demo_data import get_demo_projects
from .models import DemoEvent

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


def index(request):
    if settings.DEMO_MODE:
        return render(request, 'projects/landing.html')
    return redirect('dashboard')


def _build_session_project(session_plan):
    event_date = date.fromisoformat(session_plan['event_date'])
    tasks = [
        {
            'id': t['id'],
            'name': t['name'],
            'due': date.fromisoformat(t['date']) if t.get('date') else None,
            'done': t['done'],
            'kontext': t['kontext'] if t.get('kontext') else '',
        }
        for t in session_plan['tasks']
    ]
    return {
        'id': 'session-plan',
        'name': session_plan['name'],
        'event_date': event_date,
        'performers': '',
        'tasks': tasks,
        'status': 'in Vorbereitung',
        'status_color': 'default',
    }


def dashboard(request):
    today = date.today()
    has_session_plan = False
    force_multi = request.GET.get('mode') == 'multi'

    if settings.DEMO_MODE:
        sim_date_str = request.session.get('demo_sim_date')
        sim_date = date.fromisoformat(sim_date_str) if sim_date_str else None
        effective_today = sim_date or today

        session_plan = request.session.get('demo_plan')
        if session_plan and not force_multi:
            has_session_plan = True
            project = copy.deepcopy(_build_session_project(session_plan))
            if sim_date:
                for task in project['tasks']:
                    if task.get('due') and task['due'] <= sim_date:
                        task['done'] = True
            projects = _annotate_tasks([project], effective_today)
            summary_key = f'demo_plan_summary_v3_{sim_date_str or "today"}'
            summary_html = request.session.get(summary_key)
            if not summary_html:
                summary_html = markdown.markdown(_fix_ai_markdown(generate_weekly_summary(projects, effective_today, single_project_demo=True)))
                request.session[summary_key] = summary_html
            summary = summary_html
        else:
            projects = _annotate_tasks(get_demo_projects(), effective_today)
            summary = markdown.markdown(_fix_ai_markdown(generate_weekly_summary(projects, effective_today)))
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

    timelapse_moments = request.session.get('demo_timelapse_moments', []) if settings.DEMO_MODE else []
    sim_date_str = request.session.get('demo_sim_date') if settings.DEMO_MODE else None

    # Project name for demo single-project header
    demo_project_name = ''
    demo_project_date = ''
    if settings.DEMO_MODE and has_session_plan and month_groups:
        first_project = month_groups[0]['projects'][0] if month_groups[0]['projects'] else None
        if first_project:
            demo_project_name = first_project['display_name']
            demo_project_date = first_project['event_date_display']

    return render(request, 'projects/dashboard.html', {
        'month_groups': month_groups,
        'years': years,
        'summary': summary,
        'today': today,
        'today_display': _format_date(today),
        'today_iso': today.isoformat(),
        'project_map': json.dumps(project_map),
        'has_session_plan': has_session_plan,
        'force_multi': force_multi,
        'demo_mode': settings.DEMO_MODE,
        'timelapse_moments': json.dumps(timelapse_moments),
        'sim_date': sim_date_str,
        'sim_date_display': _format_date(date.fromisoformat(sim_date_str)) if sim_date_str else '',
        'demo_project_name': demo_project_name,
        'demo_project_date': demo_project_date,
    })


def refresh(request):
    if request.method == "POST":
        cache.delete(CACHE_KEY)
    return redirect("dashboard")


def set_timelapse_date(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)
    data = json.loads(request.body)
    sim_date = data.get('date')
    if sim_date:
        request.session['demo_sim_date'] = sim_date
    else:
        request.session.pop('demo_sim_date', None)
    return JsonResponse({'ok': True})


def preload_timelapse_summary(request):
    """Pre-generates and caches KI summary for a given sim date (called from JS background)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)
    data = json.loads(request.body)
    sim_date_str = data.get('date')  # None = heute

    today = date.today()
    sim_date = date.fromisoformat(sim_date_str) if sim_date_str else None
    effective_today = sim_date or today
    summary_key = f'demo_plan_summary_v3_{sim_date_str or "today"}'

    if request.session.get(summary_key):
        return JsonResponse({'ok': True, 'cached': True})

    session_plan = request.session.get('demo_plan')
    if not session_plan:
        return JsonResponse({'ok': False})

    project = copy.deepcopy(_build_session_project(session_plan))
    if sim_date:
        for task in project['tasks']:
            if task.get('due') and task['due'] <= sim_date:
                task['done'] = True
    projects = _annotate_tasks([project], effective_today)
    summary_html = markdown.markdown(_fix_ai_markdown(generate_weekly_summary(projects, effective_today, single_project_demo=True)))
    request.session[summary_key] = summary_html
    return JsonResponse({'ok': True})

def toggle_task_view(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data = json.loads(request.body)
    done = data["done"]
    if settings.DEMO_MODE:
        plan = request.session.get('demo_plan')
        if plan:
            for t in plan['tasks']:
                if t['id'] == task_id:
                    t['done'] = done
                    break
            request.session['demo_plan'] = plan
    else:
        toggle_task(task_id, done)
    return JsonResponse({"ok": True})


def reschedule_task_view(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data = json.loads(request.body)
    if not settings.DEMO_MODE:
        update_task_date(task_id, data["date"])
    return JsonResponse({"ok": True})


def stats(request):
    from django.db.models import Count
    from django.db.models.functions import TruncDate

    total_generated = DemoEvent.objects.filter(event_type='plan_generated').count()
    total_downloaded = DemoEvent.objects.filter(event_type='plan_downloaded').count()

    by_type = (
        DemoEvent.objects
        .filter(event_type='plan_generated')
        .values('project_type')
        .annotate(count=Count('id'))
        .order_by('-count')
    )

    by_day = (
        DemoEvent.objects
        .filter(event_type='plan_generated')
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(count=Count('id'))
        .order_by('-day')[:14]
    )

    TYPE_LABELS = {
        'konzert': 'Konzert / Event',
        'hochzeit': 'Hochzeit / Feier',
        'recruiting': 'Recruiting',
        'eigenes': 'Eigenes Projekt',
        '': 'Unbekannt',
    }

    return render(request, 'projects/stats.html', {
        'total_generated': total_generated,
        'total_downloaded': total_downloaded,
        'download_rate': round(total_downloaded / total_generated * 100) if total_generated else 0,
        'by_type': [{'label': TYPE_LABELS.get(r['project_type'], r['project_type']), 'count': r['count']} for r in by_type],
        'by_day': list(by_day),
    })


def my_plan(request):
    plan = request.session.get('demo_plan')
    if not plan:
        return redirect('index')

    today = date.today()
    event_date = date.fromisoformat(plan['event_date'])

    tasks = []
    for t in plan['tasks']:
        due = date.fromisoformat(t['date']) if t.get('date') else None
        tasks.append({
            'id': t['id'],
            'name': t['name'],
            'due': due,
            'done': t['done'],
            'kontext': [t['kontext']] if t.get('kontext') else [],
        })

    project = {
        'id': 'session-plan',
        'name': plan['name'],
        'event_date': event_date,
        'event_date_display': _format_date(event_date),
        'performers': '',
        'tasks': tasks,
        'status': 'in Vorbereitung',
    }
    _annotate_tasks([project], today)

    done_count = sum(1 for t in tasks if t['done'])
    total = len(tasks)

    summary_html = request.session.get('demo_plan_summary_v3_today')
    if not summary_html:
        summary_md = generate_weekly_summary([project], today, single_project_demo=True)
        summary_html = markdown.markdown(_fix_ai_markdown(summary_md))
        request.session['demo_plan_summary_v3_today'] = summary_html

    return render(request, 'projects/my_plan.html', {
        'project': project,
        'done_count': done_count,
        'total': total,
        'today': today,
        'today_display': _format_date(today),
        'summary': summary_html,
    })


def download_plan(request):
    plan = request.session.get('demo_plan')
    if not plan:
        return redirect('index')

    today = date.today()
    event_date = date.fromisoformat(plan['event_date'])
    event_display = _format_date(event_date)

    lines = [
        f"# {plan['name']}",
        f"**Zieldatum:** {event_display}",
        "",
        "---",
        "",
        "## Aufgabenplan",
        "",
    ]

    for t in plan['tasks']:
        checkbox = "[x]" if t['done'] else "[ ]"
        due = date.fromisoformat(t['date']) if t.get('date') else None
        due_str = f" — {_format_date(due)}" if due else ""
        kontext = f" *({t['kontext']})*" if t.get('kontext') else ""
        lines.append(f"- {checkbox} {t['name']}{kontext}{due_str}")

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
    filename = plan['name'].replace(" ", "_").replace("/", "-")[:50] + ".md"

    project_type = request.session.get('demo_project_type', '')
    DemoEvent.objects.create(event_type='plan_downloaded', project_type=project_type)

    response = HttpResponse(content, content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def impressum(request):
    return render(request, 'projects/impressum.html')


def datenschutz(request):
    return render(request, 'projects/datenschutz.html')


def toggle_session_task(request, task_id):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    data = json.loads(request.body)
    done = data["done"]
    plan = request.session.get('demo_plan')
    if plan:
        for t in plan['tasks']:
            if t['id'] == task_id:
                t['done'] = done
                break
        request.session['demo_plan'] = plan
    return JsonResponse({"ok": True})