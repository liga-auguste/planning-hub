from datetime import date
import markdown
from django.shortcuts import render
from .notion import get_upcoming_projects
from .ai import generate_weekly_summary

def dashboard(request):
    today = date.today()
    projects = get_upcoming_projects(today)

    summary_md = generate_weekly_summary(projects, today)
    summary = markdown.markdown(summary_md)

    return render(request, 'projects/dashboard.html', {
        'projects': projects,
        'summary': summary,
        'today': today,
    })