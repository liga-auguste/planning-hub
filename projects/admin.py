from django.contrib import admin
from .models import ProjectType, TaskTemplate, Project, Task

admin.site.register(ProjectType)
admin.site.register(TaskTemplate)
admin.site.register(Project)
admin.site.register(Task)