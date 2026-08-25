from django.db import models
from django.utils import timezone

# Project and task data lives in Notion (see projects/notion.py), not here.
# What remains are the two things that are genuinely local: anonymous demo
# usage counters, and the planning rules that go into the Claude prompt.


class DemoEvent(models.Model):
    EVENT_TYPES = [
        ("plan_started", "Plan gestartet"),
        ("plan_generated", "Plan generiert"),
        ("plan_downloaded", "Plan heruntergeladen"),
    ]
    PROJECT_TYPES = [
        ("konzert", "Konzert / Event"),
        ("hochzeit", "Hochzeit / Feier"),
        ("recruiting", "Recruiting"),
        ("eigenes", "Eigenes Projekt"),
    ]
    event_type = models.CharField(max_length=30, choices=EVENT_TYPES)
    project_type = models.CharField(max_length=20, choices=PROJECT_TYPES, blank=True)
    task_count = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        local_created_at = timezone.localtime(self.created_at)
        return f"{self.event_type} / {self.project_type} / {local_created_at:%d.%m.%Y %H:%M}"


class PlannerRule(models.Model):
    text = models.TextField()
    active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    # PLANNER_TILES types this rule applies to (planner_views.py); empty
    # means every type (#105).
    project_types = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.text[:60]
