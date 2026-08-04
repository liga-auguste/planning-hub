from datetime import date, timedelta


def get_demo_projects():
    today = date.today()

    def d(offset):
        return today + timedelta(days=offset)

    return [
        {
            "id": "demo-1",
            "name": "Musik zur Marktzeit",
            "event_date": d(3),
            "performers": "Bläserquartett",
            "status": "in Vorbereitung",
            "status_color": "default",
            "tasks": [
                {"id": "demo-1-1", "name": "Ensemble anfragen", "due": d(-40), "done": True, "kontext": []},
                {"id": "demo-1-2", "name": "Termin und Honorar bestätigen", "due": d(-35), "done": True, "kontext": []},
                {"id": "demo-1-3", "name": "Programm festlegen", "due": d(-14), "done": True, "kontext": []},
                {"id": "demo-1-4", "name": "Pressetext schreiben", "due": d(-10), "done": True, "kontext": []},
                {"id": "demo-1-5", "name": "Pressetext an Lokalzeitung", "due": d(-7), "done": True, "kontext": []},
                {"id": "demo-1-6", "name": "Facebook-Veranstaltung anlegen", "due": d(-5), "done": True, "kontext": []},
                {"id": "demo-1-7", "name": "Noten vorbereiten und kopieren", "due": d(-2), "done": False, "kontext": []},
                {"id": "demo-1-8", "name": "Stühle und Aufbau koordinieren", "due": d(2), "done": False, "kontext": []},
                {"id": "demo-1-9", "name": "Abendkasse organisieren", "due": d(3), "done": False, "kontext": []},
            ],
        },
        {
            "id": "demo-2",
            "name": "Trio romantique",
            "event_date": d(19),
            "performers": "Klaviertrio",
            "status": "in Vorbereitung",
            "status_color": "default",
            "tasks": [
                {"id": "demo-2-1", "name": "Musiker bestätigt", "due": d(-30), "done": True, "kontext": []},
                {"id": "demo-2-2", "name": "Programm abstimmen", "due": d(-7), "done": True, "kontext": []},
                {"id": "demo-2-3", "name": "Plakat gestalten (Graphiker)", "due": d(-5), "done": False, "kontext": []},
                {"id": "demo-2-4", "name": "Pressetext schreiben", "due": d(-3), "done": False, "kontext": []},
                {"id": "demo-2-5", "name": "Pressetext an Zeitung", "due": d(3), "done": False, "kontext": []},
                {"id": "demo-2-6", "name": "Plakate drucken und aushängen", "due": d(7), "done": False, "kontext": []},
                {"id": "demo-2-7", "name": "Mikrofon-Anlage prüfen", "due": d(14), "done": False, "kontext": []},
                {"id": "demo-2-8", "name": "Noten kopieren", "due": d(17), "done": False, "kontext": []},
                {"id": "demo-2-9", "name": "GEMA-Meldung vorbereiten", "due": d(25), "done": False, "kontext": []},
            ],
        },
        {
            "id": "demo-3",
            "name": "Reformationskonzert",
            "event_date": d(87),
            "performers": "Kantorei, Blechbläser, Solistin",
            "status": "in Vorbereitung",
            "status_color": "default",
            "tasks": [
                {"id": "demo-3-1", "name": "Solistin anfragen", "due": d(-10), "done": True, "kontext": []},
                {"id": "demo-3-2", "name": "Programm-Entwurf", "due": d(7), "done": False, "kontext": []},
                {"id": "demo-3-3", "name": "Probentermine festlegen", "due": d(14), "done": False, "kontext": []},
                {"id": "demo-3-4", "name": "Probenplan an Chor versenden", "due": d(21), "done": False, "kontext": []},
                {"id": "demo-3-5", "name": "Graphiker beauftragen", "due": d(35), "done": False, "kontext": []},
                {"id": "demo-3-6", "name": "Pressetext verfassen", "due": d(50), "done": False, "kontext": []},
                {"id": "demo-3-7", "name": "Plakate drucken", "due": d(60), "done": False, "kontext": []},
                {"id": "demo-3-8", "name": "Ankündigung Gemeindebrief", "due": d(65), "done": False, "kontext": []},
                {"id": "demo-3-9", "name": "Generalprobe koordinieren", "due": d(80), "done": False, "kontext": []},
                {"id": "demo-3-10", "name": "GEMA-Meldung einreichen", "due": d(95), "done": False, "kontext": []},
            ],
        },
        {
            "id": "demo-4",
            "name": "Adventskonzert Gospelchor",
            "event_date": d(122),
            "performers": "Gospelchor Good News",
            "status": "geplant / mit Zeitplan",
            "status_color": "default",
            "tasks": [
                {"id": "demo-4-1", "name": "Chor-Kontakt bestätigen", "due": d(10), "done": False, "kontext": []},
                {"id": "demo-4-2", "name": "Kirchenraum und Bestuhlung planen", "due": d(30), "done": False, "kontext": []},
                {"id": "demo-4-3", "name": "Tonanlage und Technik klären", "due": d(45), "done": False, "kontext": []},
                {"id": "demo-4-4", "name": "Programm-Ablauf abstimmen", "due": d(60), "done": False, "kontext": []},
                {"id": "demo-4-5", "name": "Plakat gestalten lassen", "due": d(75), "done": False, "kontext": []},
                {"id": "demo-4-6", "name": "Eintrittspreis festlegen", "due": d(80), "done": False, "kontext": []},
                {"id": "demo-4-7", "name": "Programmheft drucken", "due": d(115), "done": False, "kontext": []},
            ],
        },
        {
            "id": "demo-5",
            "name": "Neujahrskonzert",
            "event_date": d(150),
            "performers": "Streichquartett",
            "status": "geplant / mit Zeitplan",
            "status_color": "default",
            "tasks": [
                {"id": "demo-5-1", "name": "Ensemble anfragen und bestätigen", "due": d(20), "done": False, "kontext": []},
                {"id": "demo-5-2", "name": "Honorarvereinbarung schriftlich", "due": d(30), "done": False, "kontext": []},
                {"id": "demo-5-3", "name": "Programm-Wünsche abstimmen", "due": d(60), "done": False, "kontext": []},
                {"id": "demo-5-4", "name": "Graphiker beauftragen", "due": d(90), "done": False, "kontext": []},
                {"id": "demo-5-5", "name": "Pressetext schreiben", "due": d(110), "done": False, "kontext": []},
                {"id": "demo-5-6", "name": "Plakate drucken und aushängen", "due": d(125), "done": False, "kontext": []},
                {"id": "demo-5-7", "name": "Noten und Aufführungsrechte prüfen", "due": d(140), "done": False, "kontext": []},
                {"id": "demo-5-8", "name": "GEMA-Meldung vorbereiten", "due": d(155), "done": False, "kontext": []},
            ],
        },
    ]


def get_demo_history():
    today = date.today()

    def d(offset):
        return today + timedelta(days=offset)

    return [
        {
            "name": "Sommerkonzert Kantorei",
            "event_date": d(-30),
            "performers": "Kantorei, Orgel",
            "tasks": [
                {"name": "Ensemble anfragen", "due": d(-120), "done": True, "kontext": []},
                {"name": "Programm festlegen", "due": d(-60), "done": True, "kontext": []},
                {"name": "Plakat gestalten", "due": d(-45), "done": True, "kontext": []},
                {"name": "Pressetext schreiben", "due": d(-40), "done": True, "kontext": []},
                {"name": "Plakate aushängen", "due": d(-21), "done": True, "kontext": []},
                {"name": "Generalprobe", "due": d(-32), "done": True, "kontext": []},
                {"name": "Noten kopieren", "due": d(-35), "done": True, "kontext": []},
                {"name": "GEMA-Meldung", "due": d(-20), "done": True, "kontext": []},
            ],
        },
        {
            "name": "Musik zur Marktzeit",
            "event_date": d(-60),
            "performers": "Flötentrio",
            "tasks": [
                {"name": "Ensemble anfragen", "due": d(-100), "done": True, "kontext": []},
                {"name": "Programm abstimmen", "due": d(-75), "done": True, "kontext": []},
                {"name": "Pressetext schreiben", "due": d(-70), "done": True, "kontext": []},
                {"name": "Noten vorbereiten", "due": d(-62), "done": True, "kontext": []},
                {"name": "Aufbau koordinieren", "due": d(-61), "done": True, "kontext": []},
                {"name": "GEMA-Meldung", "due": d(-50), "done": True, "kontext": []},
            ],
        },
        {
            "name": "Passionsandacht mit Musik",
            "event_date": d(-90),
            "performers": "Vokalensemble, Orgel",
            "tasks": [
                {"name": "Programm-Konzept erstellen", "due": d(-150), "done": True, "kontext": []},
                {"name": "Sängerinnen anfragen", "due": d(-140), "done": True, "kontext": []},
                {"name": "Noten beschaffen", "due": d(-120), "done": True, "kontext": []},
                {"name": "Proben planen", "due": d(-110), "done": True, "kontext": []},
                {"name": "Pressetext und Plakat", "due": d(-100), "done": True, "kontext": []},
                {"name": "Plakate aushängen", "due": d(-98), "done": True, "kontext": []},
                {"name": "Generalprobe", "due": d(-92), "done": True, "kontext": []},
                {"name": "GEMA-Meldung", "due": d(-80), "done": True, "kontext": []},
                {"name": "Honorare abrechnen", "due": d(-85), "done": True, "kontext": []},
            ],
        },
    ]
