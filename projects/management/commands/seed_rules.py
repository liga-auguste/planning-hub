from django.core.management.base import BaseCommand
from projects.models import PlannerRule

INITIAL_RULES = [
    "Bei Konzertveranstaltungen GEMA-Meldung einplanen — nicht bei Gottesdiensten",
    "Bei externen Mitwirkenden: Verträge, Honorare und Fahrtkosten einplanen",
    "Bei Konzerten Plakat als Standard voraussetzen; bei Gottesdiensten genügt ein Hausausdruck",
    "Vorverkauf nur bei größeren Konzerten relevant",
    "Bei Recruiting / Personalplanung: Stellenausschreibung, Bewerbungsschluss, Interview-Runden, Referenzcheck und Angebot einplanen",
]


class Command(BaseCommand):
    help = "Seed initial planner rules"

    def handle(self, *args, **kwargs):
        if PlannerRule.objects.exists():
            self.stdout.write("Regeln vorhanden, übersprungen.")
            return
        for i, text in enumerate(INITIAL_RULES):
            PlannerRule.objects.create(text=text, active=True, order=i)
        self.stdout.write(f"{len(INITIAL_RULES)} Regeln angelegt.")
