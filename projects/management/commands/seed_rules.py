from django.core.management.base import BaseCommand
from django.db import transaction

from projects.models import PlannerRule, RulesSeeded
from projects.rules import INITIAL_RULES


class Command(BaseCommand):
    help = "Seed initial planner rules"

    def handle(self, *args, **kwargs):
        if RulesSeeded.objects.exists():
            self.stdout.write("Rules already seeded, skipped.")
            return
        with transaction.atomic():
            for i, rule in enumerate(INITIAL_RULES):
                PlannerRule.objects.create(
                    text=rule["text"],
                    active=True,
                    order=i,
                    project_types=rule["project_types"],
                )
            RulesSeeded.objects.create()
        self.stdout.write(f"Created {len(INITIAL_RULES)} rules.")
