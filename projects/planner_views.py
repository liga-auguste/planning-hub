from django.shortcuts import render, redirect
from django.core.cache import cache
from django.conf import settings
from django.http import JsonResponse
from .models import PlannerRule, DemoEvent
from .notion import get_historical_projects, create_project, create_tasks
from .planner import get_clarifying_questions, generate_plan
from .demo_data import get_demo_history
from .ai import generate_timelapse_moments
import markdown as md
import json
import re
from datetime import date

MONTHS_DE_REV = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4,
    'mai': 5, 'juni': 6, 'juli': 7, 'august': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'dezember': 12,
}

HISTORY_CACHE_KEY = "historical_projects"
HISTORY_CACHE_TTL = 60 * 60 * 24  # 24 hours


def _get_history():
    if settings.DEMO_MODE:
        return get_demo_history()
    history = cache.get(HISTORY_CACHE_KEY)
    if not history:
        history = get_historical_projects()
        cache.set(HISTORY_CACHE_KEY, history, HISTORY_CACHE_TTL)
    return history


def _get_active_rules():
    return list(PlannerRule.objects.filter(active=True).order_by('order').values_list('text', flat=True))


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
            rules = _get_active_rules()
            questions = get_clarifying_questions(description, history, rules)
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
        rules = _get_active_rules()
        plan_json = generate_plan(description, full_answers, history, rules)
        try:
            plan = json.loads(plan_json)
        except json.JSONDecodeError as e:
            print("=== JSON ERROR ===")
            print(f"Error: {e}")
            print(f"Position: {plan_json[max(0,e.pos-100):e.pos+100]}")
            raise

        KONTEXTE = ["Planung", "Büro", "Extern", "Kommunikation", "Unterwegs", "Vor Ort"]
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
            # Clear cached summaries and old timelapse state
            for key in list(request.session.keys()):
                if key.startswith('demo_plan_summary_v3'):
                    del request.session[key]
            request.session.pop('demo_sim_date', None)
            # Generate narrative timelapse moments
            try:
                moments = generate_timelapse_moments(project_name, event_date, tasks)
                request.session['demo_timelapse_moments'] = moments
            except Exception:
                request.session.pop('demo_timelapse_moments', None)
            project_type = request.session.get('demo_project_type', '')
            DemoEvent.objects.create(
                event_type='plan_generated',
                project_type=project_type,
                task_count=len(tasks),
            )
            return redirect('dashboard')

        project_id = create_project(project_name, event_date)
        tasks = [{"name": n, "date": d} for n, d in zip(names, dates) if n and d]
        create_tasks(project_id, tasks)
        cache.delete('dashboard_data')
        return redirect('dashboard')
    return redirect('planner_start')


# --- Rule management ---

def rules_list(request):
    rules = PlannerRule.objects.all()
    return render(request, 'projects/planner_rules.html', {'rules': rules})


def rule_add(request):
    if request.method == 'POST':
        text = request.POST.get('text', '').strip()
        if text:
            last = PlannerRule.objects.order_by('-order').first()
            next_order = (last.order + 1) if last else 0
            PlannerRule.objects.create(text=text, active=True, order=next_order)
    return redirect('rules_list')


def rule_toggle(request, rule_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)
    try:
        rule = PlannerRule.objects.get(pk=rule_id)
        rule.active = not rule.active
        rule.save()
        return JsonResponse({'active': rule.active})
    except PlannerRule.DoesNotExist:
        return JsonResponse({'error': 'not found'}, status=404)


def rule_update(request, rule_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)
    try:
        data = json.loads(request.body)
        rule = PlannerRule.objects.get(pk=rule_id)
        text = data.get('text', '').strip()
        if text:
            rule.text = text
            rule.save()
        return JsonResponse({'ok': True})
    except PlannerRule.DoesNotExist:
        return JsonResponse({'error': 'not found'}, status=404)


def rule_delete(request, rule_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)
    PlannerRule.objects.filter(pk=rule_id).delete()
    return JsonResponse({'ok': True})


def rule_reorder(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'method not allowed'}, status=405)
    data = json.loads(request.body)
    for i, rule_id in enumerate(data.get('order', [])):
        PlannerRule.objects.filter(pk=rule_id).update(order=i)
    return JsonResponse({'ok': True})
