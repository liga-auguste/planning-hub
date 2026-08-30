from django.contrib import admin

from .models import DemoEvent, PlannerRule, WeekCloseout

admin.site.register(DemoEvent)
admin.site.register(PlannerRule)
admin.site.register(WeekCloseout)
