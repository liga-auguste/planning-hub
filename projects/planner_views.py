from django.shortcuts import render, redirect
from django.core.cache import cache
from .notion import get_historical_projects, create_project, create_tasks
from .planner import get_clarifying_questions, generate_plan
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
HISTORY_CACHE_TTL = 60 * 60 * 24  # 24 Stunden

def _get_history():
    history = cache.get(HISTORY_CACHE_KEY)
    if not history:
        history = get_historical_projects()
        cache.set(HISTORY_CACHE_KEY, history, HISTORY_CACHE_TTL)
    return history


def _parse_event_date(description: str):
    m = re.search(r'(\d{1,2})\.\s+(\w+)\s+(\d{4})', description, re.IGNORECASE)
    if m:
        month = MONTHS_DE_REV.get(m.group(2).lower())
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(1)))
            except ValueError:
                pass
    return None


def planner_start(request):
    if request.method == 'POST':
        description = request.POST.get('description', '').strip()
        if description:
            history = _get_history()
            questions = get_clarifying_questions(description, history)
            questions_html = md.markdown(questions)
            return render(request, 'projects/planner_questions.html', {
                'description': description,
                'questions': questions_html,
})
    return render(request, 'projects/planner_start.html')


def planner_questions(request):
    pass


def planner_review(request):
    if request.method == 'POST':
        description = request.POST.get('description', '').strip()
        answers = request.POST.get('answers', '').strip()
        uhrzeit = request.POST.get('uhrzeit', '')
        honorar = request.POST.get('honorar', '')
        fahrtkosten = request.POST.get('fahrtkosten', '')
        programm = request.POST.get('programm', '')
        bedarf = request.POST.get('bedarf', '')

        full_answers = f"""{answers}

Vereinbarung:
- Uhrzeit: {uhrzeit}
- Honorar: {honorar} €
- Fahrtkosten: {fahrtkosten} €
- Programm: {programm}
- Besonderer Bedarf: {bedarf}"""

        history = _get_history()
        plan_json = generate_plan(description, full_answers, history)
        try:
            plan = json.loads(plan_json)
        except json.JSONDecodeError as e:
            print("=== JSON FEHLER ===")
            print(f"Fehler: {e}")
            print(f"Stelle: {plan_json[max(0,e.pos-100):e.pos+100]}")
            raise

        KONTEXTE = ["Planung", "Büro", "Graphiker", "Kommunikation", "Unterwegs", "Vor Ort"]
        event_date = _parse_event_date(description)
        project_name = description.split(',')[0].strip()
        return render(request, 'projects/planner_review.html', {
            'description': description,
            'project_name': project_name,
            'tasks': plan['tasks'],
            'kontexte': KONTEXTE,
            'event_date_iso': event_date.isoformat() if event_date else '',
        })
        
def planner_create(request):
    if request.method == 'POST':
        description = request.POST.get('description', '').strip()
        event_date_str = request.POST.get('event_date', '')
        names = request.POST.getlist('task_name')
        dates = request.POST.getlist('task_date')
        kontexte = request.POST.getlist('task_kontext')

        project_name = request.POST.get('project_name', description).strip()
        event_date = date.fromisoformat(event_date_str)
        project_id = create_project(project_name, event_date)

        tasks = [
            {"name": n, "date": d}
            for n, d in zip(names, dates)
            if n and d
        ]
        create_tasks(project_id, tasks)

        cache.delete('dashboard_data')

        return redirect('dashboard')