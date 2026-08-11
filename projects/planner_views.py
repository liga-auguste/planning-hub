import json
import logging
import re
from datetime import date

import markdown as md
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect, render

from . import rules as rules_store
from .ai import AIUnavailableError, generate_timelapse_moments
from .demo_data import get_demo_history
from .models import DemoEvent
from .notion import (
    NotionUnavailableError,
    create_project,
    create_tasks,
    find_project,
    get_historical_projects,
)
from .planner import generate_plan, get_clarifying_questions
from .views import CACHE_KEY

logger = logging.getLogger(__name__)

MONTHS_DE_REV = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4,
    'mai': 5, 'juni': 6, 'juli': 7, 'august': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'dezember': 12,
}

HISTORY_CACHE_KEY = "historical_projects"
HISTORY_CACHE_TTL = 60 * 60 * 24  # 24 hours

# Also defined, disagreeing, in ai.py's KONTEXTE ("Graphiker" vs. "Extern")
# — a known inconsistency (#17), not touched here.
KONTEXTE = ["Planung", "Büro", "Extern", "Kommunikation", "Unterwegs", "Vor Ort"]


def _get_history():
    if settings.DEMO_MODE:
        return get_demo_history()
    history = cache.get(HISTORY_CACHE_KEY)
    if not history:
        try:
            history = get_historical_projects()
        except NotionUnavailableError:
            # No stale-cache fallback here (contrast views.dashboard): the
            # planner still works without calibration data, just less
            # precisely, so failing open to [] is enough.
            logger.warning("Historical projects unavailable; planning without calibration data")
            return []
        cache.set(HISTORY_CACHE_KEY, history, HISTORY_CACHE_TTL)
    return history


def _parse_event_date(description: str):
    today = date.today()
    # With a year
    m = re.search(r'(\d{1,2})\.\s+(\w+)\s+(\d{4})', description, re.IGNORECASE)
    if m:
        month = MONTHS_DE_REV.get(m.group(2).lower())
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(1)))
            except ValueError:
                pass
    # Without a year — next occurrence within roughly 12 months
    m = re.search(r'(\d{1,2})\.\s+([A-Za-zÄäÖöÜüß]+)', description, re.IGNORECASE)
    if m:
        month = MONTHS_DE_REV.get(m.group(2).lower())
        if month:
            try:
                day = int(m.group(1))
                candidate = date(today.year, month, day)
                if candidate <= today:
                    candidate = date(today.year + 1, month, day)
                return candidate
            except ValueError:
                pass
    return None


def planner_start(request):
    if request.method == 'POST':
        description = request.POST.get('description', '').strip()
        if description:
            history = _get_history()
            rules = rules_store.get_active_rule_texts(request)
            try:
                questions = get_clarifying_questions(description, history, rules)
            except AIUnavailableError:
                return render(request, 'projects/planner_start.html', {
                    'prefill': description,
                    'show_tiles': False,
                    'error': True,
                })
            questions_html = md.markdown(questions)
            return render(request, 'projects/planner_questions.html', {
                'description': description,
                'questions': questions_html,
            })
    prefill = request.GET.get('prefill', '')
    project_type = request.GET.get('type', '')
    if project_type:
        request.session['demo_project_type'] = project_type
    show_tiles = not bool(project_type)
    return render(request, 'projects/planner_start.html', {
        'prefill': prefill,
        'show_tiles': show_tiles,
    })


def planner_review(request):
    if request.method == 'POST':
        description = request.POST.get('description', '').strip()
        full_answers = request.POST.get('answers', '').strip()

        history = _get_history()
        rules = rules_store.get_active_rule_texts(request)
        try:
            plan = generate_plan(description, full_answers, history, rules)
        except AIUnavailableError:
            return render(request, 'projects/planner_questions.html', {
                'description': description,
                'answers': full_answers,
                'error': True,
            })

        event_date = _parse_event_date(description)
        project_name = plan.get('project_name') or description.split(',')[0].strip()
        return render(request, 'projects/planner_review.html', {
            'description': description,
            'project_name': project_name,
            'tasks': plan['tasks'],
            'kontexte': KONTEXTE,
            'event_date_iso': event_date.isoformat() if event_date else '',
            'demo_mode': settings.DEMO_MODE,
        })
    return redirect('planner_start')


def planner_create(request):
    if request.method == 'POST':
        description = request.POST.get('description', '').strip()
        event_date_str = request.POST.get('event_date', '')
        names = request.POST.getlist('task_name')
        dates = request.POST.getlist('task_date')
        kontexte = request.POST.getlist('task_kontext')

        project_name = request.POST.get('project_name', description).strip()
        event_date = date.fromisoformat(event_date_str)

        if settings.DEMO_MODE:
            tasks = [
                {'id': f'demo-session-{i}', 'name': n, 'date': d, 'kontext': k, 'done': False}
                for i, (n, d, k) in enumerate(zip(names, dates, kontexte))
                if n and d
            ]
            request.session['demo_plan'] = {
                'name': project_name,
                'event_date': event_date_str,
                'tasks': tasks,
            }
            # Clear cached summaries and old timelapse state. Matching the
            # unversioned prefix clears every summary version a long-lived
            # session may still carry (see SUMMARY_KEY in views.py).
            for key in list(request.session.keys()):
                if key.startswith('demo_plan_summary'):
                    del request.session[key]
            request.session.pop('demo_sim_date', None)
            # Generate narrative timelapse moments
            try:
                moments = generate_timelapse_moments(project_name, event_date, tasks)
                request.session['demo_timelapse_moments'] = moments
            except Exception:  # noqa: BLE001 — deliberate: the moments are a garnish
                # Anything this call raises must not cost the visitor the plan
                # they just waited for. generate_timelapse_moments already
                # narrows API failures to AIUnavailableError, so what is caught
                # here is the unexpected rest — a malformed model reply, say.
                request.session.pop('demo_timelapse_moments', None)
            project_type = request.session.get('demo_project_type', '')
            DemoEvent.objects.create(
                event_type='plan_generated',
                project_type=project_type,
                task_count=len(tasks),
            )
            return redirect('dashboard')

        try:
            # A retry after the NotionUnavailableError below re-POSTs the
            # same plan — reuse the project page a failed attempt already
            # created (create_tasks likewise skips already-written tasks),
            # so retrying can't duplicate anything in Notion.
            project_id = find_project(project_name, event_date) or create_project(project_name, event_date)
            tasks = [{"name": n, "date": d} for n, d in zip(names, dates) if n and d]
            create_tasks(project_id, tasks)
        except NotionUnavailableError:
            # The visitor already reviewed and adjusted this exact task list —
            # losing it to a Notion hiccup would mean redoing the whole
            # planner flow. Reconstruct days_before from the dates they had
            # so planner_review.html's own JS recomputes the same dates.
            return render(request, 'projects/planner_review.html', {
                'description': description,
                'project_name': project_name,
                'tasks': [
                    {'name': n, 'days_before': (event_date - date.fromisoformat(d)).days, 'kontext': k}
                    for n, d, k in zip(names, dates, kontexte)
                    if n and d
                ],
                'kontexte': KONTEXTE,
                'event_date_iso': event_date_str,
                'demo_mode': settings.DEMO_MODE,
                'error': True,
            })
        cache.delete(CACHE_KEY)
        return redirect('dashboard')
    return redirect('planner_start')


# --- Rule management ---

def rules_list(request):
    return render(request, 'projects/planner_rules.html', {
        'rules': rules_store.get_rules(request),
        'demo_mode': settings.DEMO_MODE,
    })


def rule_add(request):
    if request.method == 'POST':
        text = request.POST.get('text', '').strip()
        if text:
            rules_store.add_rule(request, text)
    return redirect('rules_list')


def rule_toggle(request, rule_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)
    active = rules_store.toggle_rule(request, rule_id)
    if active is None:
        return JsonResponse({'error': 'not found'}, status=404)
    return JsonResponse({'active': active})


def rule_update(request, rule_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)
    data = json.loads(request.body)
    if not rules_store.update_rule(request, rule_id, data.get('text', '').strip()):
        return JsonResponse({'error': 'not found'}, status=404)
    return JsonResponse({'ok': True})


def rule_delete(request, rule_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)
    rules_store.delete_rule(request, rule_id)
    return JsonResponse({'ok': True})


def rule_reorder(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)
    data = json.loads(request.body)
    rules_store.reorder_rules(request, data.get('order', []))
    return JsonResponse({'ok': True})
