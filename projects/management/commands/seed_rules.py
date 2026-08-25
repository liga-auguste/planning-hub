from django.core.management.base import BaseCommand

from projects.models import PlannerRule
from projects.rules import INITIAL_RULES


class Command(BaseCommand):
    help = "Seed initial planner rules"

    def handle(self, *args, **kwargs):
        if PlannerRule.objects.exists():
            self.stdout.write("Rules already present, skipped.")
            return
        for i, rule in enumerate(INITIAL_RULES):
            PlannerRule.objects.create(
                text=rule["text"],
                active=True,
                order=i,
                project_types=rule["project_types"],
            )
        self.stdout.write(f"Created {len(INITIAL_RULES)} rules.")
