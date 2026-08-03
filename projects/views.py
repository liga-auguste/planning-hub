from datetime import date
import markdown
from django.shortcuts import render
from .models import Project
from .ai import generate_weekly_summary

def dashboard(request):
    today = date.today()
    projects = Project.objects.filter(
        event_date__gte=today
    ).prefetch_related('tasks').order_by('event_date')

    summary_md = generate_weekly_summary(projects, today)
    summary = markdown.markdown(summary_md)

    return render(request, 'projects/dashboard.html', {
        'projects': projects,
        'summary': summary,
        'today': today,
    })
