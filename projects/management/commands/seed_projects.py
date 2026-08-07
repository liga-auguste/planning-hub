from datetime import date
from django.core.management.base import BaseCommand
from projects.models import ProjectType, Project, Task


class Command(BaseCommand):
    help = 'Seed MzM projects with tasks for Sep–Dec 2026'

    def handle(self, *args, **kwargs):
        mzm = ProjectType.objects.get(slug='mzm')

        projects_data = [
            {
                'name': 'Musik zur Marktzeit am 5. September 2026',
                'event_date': date(2026, 9, 5),
                'performers': 'KMD Martin Winkler, Orgel und Gerlind Tautorus, Violine',
                'tasks': [
                    ('Plakate machen', date(2026, 8, 22), False),
                    ('Plakate aushängen', date(2026, 8, 26), False),
                    ('Veranstaltung in den Facebook-Kalender', date(2026, 8, 22), False),
                    ('Pressetext an die Vlothoer Zeitung', date(2026, 9, 2), False),
                    ('Programm machen', date(2026, 9, 2), False),
                    ('Programme bereitlegen', date(2026, 9, 5), False),
                    ('Blumen', date(2026, 9, 4), False),
                    ('GEMA-Meldung', date(2026, 9, 5), False),
                    ('Musikervertrag / Rechnung', date(2026, 9, 2), False),
                    ('Programm abheften', date(2026, 9, 5), False),
                    ('Besucher und Spenden aufschreiben', date(2026, 9, 5), False),
                ],
            },
            {
                'name': 'Musik zur Marktzeit am 3. Oktober 2026',
                'event_date': date(2026, 10, 3),
                'performers': 'Posaunenchor der Christuskirche Herford',
                'tasks': [
                    ('Plakate machen', date(2026, 9, 19), False),
                    ('Plakate aushängen', date(2026, 9, 23), False),
                    ('Veranstaltung in den Facebook-Kalender', date(2026, 9, 19), False),
                    ('Pressetext an die Vlothoer Zeitung', date(2026, 9, 30), False),
                    ('Programm machen', date(2026, 9, 30), False),
                    ('Programme bereitlegen', date(2026, 10, 3), False),
                    ('Blumen', date(2026, 10, 2), False),
                    ('GEMA-Meldung', date(2026, 10, 3), False),
                    ('Musikervertrag / Rechnung', date(2026, 9, 30), False),
                    ('Programm abheften', date(2026, 10, 3), False),
                    ('Besucher und Spenden aufschreiben', date(2026, 10, 3), False),
                ],
            },
            {
                'name': 'Musik zur Marktzeit am 7. November 2026',
                'event_date': date(2026, 11, 7),
                'performers': 'Blockflötenensemble 5+1, Leitung Elisabeth Schwanda',
                'tasks': [
                    ('Plakate machen', date(2026, 10, 24), False),
                    ('Plakate aushängen', date(2026, 10, 28), False),
                    ('Veranstaltung in den Facebook-Kalender', date(2026, 10, 24), False),
                    ('Pressetext an die Vlothoer Zeitung', date(2026, 11, 4), False),
                    ('Programm machen', date(2026, 11, 4), False),
                    ('Programme bereitlegen', date(2026, 11, 7), False),
                    ('Blumen', date(2026, 11, 6), False),
                    ('GEMA-Meldung', date(2026, 11, 7), False),
                    ('Musikervertrag / Rechnung', date(2026, 11, 4), False),
                    ('Programm abheften', date(2026, 11, 7), False),
                    ('Besucher und Spenden aufschreiben', date(2026, 11, 7), False),
                ],
            },
            {
                'name': 'Musik zur Marktzeit am 5. Dezember 2026',
                'event_date': date(2026, 12, 5),
                'performers': 'Kreiskantorin Rina Sawabe (Kirchenkreis Lübbecke), Orgel',
                'tasks': [
                    ('Plakate machen', date(2026, 11, 21), False),
                    ('Plakate aushängen', date(2026, 11, 25), False),
                    ('Veranstaltung in den Facebook-Kalender', date(2026, 11, 21), False),
                    ('Pressetext an die Vlothoer Zeitung', date(2026, 12, 2), False),
                    ('Programm machen', date(2026, 12, 2), False),
                    ('Programme bereitlegen', date(2026, 12, 5), False),
                    ('Blumen', date(2026, 12, 4), False),
                    ('GEMA-Meldung', date(2026, 12, 5), False),
                    ('Musikervertrag / Rechnung', date(2026, 12, 2), False),
                    ('Programm abheften', date(2026, 12, 5), False),
                    ('Besucher und Spenden aufschreiben', date(2026, 12, 5), False),
                ],
            },
        ]

        for p_data in projects_data:
            project, created = Project.objects.get_or_create(
                name=p_data['name'],
                defaults={
                    'project_type': mzm,
                    'event_date': p_data['event_date'],
                    'performers': p_data['performers'],
                    'status': 'planned',
                    'user_id': 'liga',
                }
            )
            if created:
                for order, (name, due_date, done) in enumerate(p_data['tasks']):
                    Task.objects.create(
                        project=project,
                        name=name,
                        due_date=due_date,
                        done=done,
                        order=order,
                    )

        self.stdout.write(self.style.SUCCESS('Created 4 projects with tasks.'))