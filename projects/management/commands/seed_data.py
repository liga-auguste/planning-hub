from django.core.management.base import BaseCommand
from projects.models import ProjectType, TaskTemplate


class Command(BaseCommand):
    help = 'Seed initial project types and task templates'

    def handle(self, *args, **kwargs):
        mzm, created = ProjectType.objects.get_or_create(
            slug='mzm',
            defaults={
                'name': 'Musik zur Marktzeit',
                'description': 'Wiederkehrende Konzertreihe, 11x/Jahr, standardisierter Ablauf',
            }
        )

        templates = [
            ('Plakate machen', 'Mail an den Grafiker schicken mit der Bitte, die aktuelle Plakatdatei (mit Datum und Mitwirkenden) zu schicken.', 14, False),
            ('Plakate aushängen', 'Plakatdatei ausdrucken, dann selbst aushängen oder Kolleginnen bitten.', 10, False),
            ('Veranstaltung in den Facebook-Kalender', 'Facebook-Event anlegen — kann gebündelt für mehrere Termine gemacht werden.', 14, True),
            ('Pressetext an die Vlothoer Zeitung', 'Pressetext schreiben und am Mittwoch vor der Veranstaltung (4 Tage vorher) verschicken.', 4, False),
            ('Programm machen', 'Zweistufig: (1) ~14 Tage vorher Künstler ans Programm erinnern. (2) Sobald Daten vorliegen formatieren und ausdrucken.', 3, False),
            ('Programme bereitlegen', 'Ausgedruckte Programme am Veranstaltungsort bereitstellen.', 0, False),
            ('Blumen', 'Blumen für die Veranstaltung besorgen.', 1, False),
            ('GEMA-Meldung', 'Gespieltes Repertoire bei der GEMA melden.', 0, False),
            ('Musikervertrag / Rechnung', 'Honorarvertrag ausstellen — zusammen mit Programmausdruck in einer Büroeinheit erledigen.', 3, False),
            ('Programm abheften', 'Programmheft nach der Veranstaltung archivieren.', 0, False),
            ('Besucher und Spenden aufschreiben', 'Besucherzahl und Spendeneinnahmen notieren.', 0, False),
        ]

        for order, (name, description, days_before, is_batchable) in enumerate(templates):
            TaskTemplate.objects.get_or_create(
                project_type=mzm,
                name=name,
                defaults={
                    'description': description,
                    'days_before_event': days_before,
                    'is_batchable': is_batchable,
                    'order': order,
                }
            )

        self.stdout.write(self.style.SUCCESS(
            f'Created ProjectType "{mzm.name}" with {len(templates)} task templates.'
        ))