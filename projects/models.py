from django.db import models

class ProjectType(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    
    def __str__(self):
        return self.name
    
class TaskTemplate(models.Model):
    project_type = models.ForeignKey(
        ProjectType, on_delete=models.CASCADE, related_name='task_templates'
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    days_before_event = models.IntegerField(null=True, blank=True)
    is_batchable = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.project_type.name} – {self.name}"

class Project(models.Model):
    name = models.CharField(max_length=200)
    project_type = models.ForeignKey(
        ProjectType, on_delete=models.PROTECT, related_name='projects'
    )
    event_date = models.DateField()
    performers = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=100, blank=True)
    user_id = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Task(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='tasks'
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    done = models.BooleanField(default=False)
    is_batchable = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['due_date', 'order']

    def __str__(self):
        return self.name


class DemoEvent(models.Model):
    EVENT_TYPES = [
        ('plan_started', 'Plan gestartet'),
        ('plan_generated', 'Plan generiert'),
        ('plan_downloaded', 'Plan heruntergeladen'),
    ]
    PROJECT_TYPES = [
        ('konzert', 'Konzert / Event'),
        ('hochzeit', 'Hochzeit / Feier'),
        ('recruiting', 'Recruiting'),
        ('eigenes', 'Eigenes Projekt'),
    ]
    event_type = models.CharField(max_length=30, choices=EVENT_TYPES)
    project_type = models.CharField(max_length=20, choices=PROJECT_TYPES, blank=True)
    task_count = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.event_type} / {self.project_type} / {self.created_at:%d.%m.%Y %H:%M}"


class PlannerRule(models.Model):
    text = models.TextField()
    active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return self.text[:60]