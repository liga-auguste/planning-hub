import anthropic
from datetime import date

TODAY = date(2026, 8, 3)

# Beschreibungen gelten für alle MzM-Events (Template-Ebene)
MZM_TASK_DESCRIPTIONS = {
    "Eintrag in die Veranstaltungsdatenbank": "Event in die offizielle Veranstaltungsdatenbank eintragen — wird gebündelt für mehrere Events gemacht.",
    "Eintrag Papierkalender": "Event in den Papierkalender eintragen — wird gebündelt für mehrere Events gemacht.",
    "Eintrag iCal": "Event in iCal eintragen — wird gebündelt für mehrere Events gemacht.",
    "Veranstaltung in den Facebook-Kalender": "Facebook-Event anlegen — wird manchmal gebündelt für mehrere Termine gemacht.",
    "Plakate machen": "Mail an den Grafiker schicken mit der Bitte, die aktuelle Plakatdatei (mit Datum und Mitwirkenden) zu schicken. Ich drucke dann selbst aus.",
    "Plakate aushängen": "Plakatdatei ausdrucken, dann selbst aushängen oder Kolleginnen bitten. Sollte ~10 Tage vor der Veranstaltung erledigt sein.",
    "Pressetext an die Vlothoer Zeitung": "Pressetext schreiben und am Mittwoch vor der Veranstaltung (= 4 Tage vorher) an die Vlothoer Zeitung schicken.",
    "Programm machen": "Zweistufig: (1) ~14 Tage vor der Veranstaltung die Künstler ans Programm erinnern und Infos anfordern. (2) Sobald die Daten vorliegen, Programm formatieren und ausdrucken — typischerweise in der Veranstaltungswoche.",
    "Programme bereitlegen": "Ausgedruckte Programme am Veranstaltungsort bereitstellen.",
    "Blumen": "Blumen für die Veranstaltung besorgen.",
    "GEMA-Meldung": "Gespieltes Repertoire bei der GEMA melden.",
    "Musikervertrag / Rechnung": "Honorarvertrag bzw. Rechnung ausstellen — am besten zusammen mit dem Programmausdruck in einer Büroeinheit erledigen.",
    "Programm abheften": "Programmheft nach der Veranstaltung archivieren.",
    "Besucher und Spenden aufschreiben": "Besucherzahl und Spendeneinnahmen notieren.",
}

def make_tasks(overrides: dict) -> list:
    """Erzeugt die Task-Liste für ein MzM-Event mit projektspezifischen Daten."""
    base = [
        {"name": "Eintrag in die Veranstaltungsdatenbank", "done": True},
        {"name": "Eintrag Papierkalender",                  "done": True},
        {"name": "Eintrag iCal",                            "done": True},
        {"name": "Plakate machen",                          "done": False},
        {"name": "Plakate aushängen",                       "done": False},
        {"name": "Veranstaltung in den Facebook-Kalender",  "done": False},
        {"name": "Pressetext an die Vlothoer Zeitung",      "done": False},
        {"name": "Programm machen",                         "done": False},
        {"name": "Programme bereitlegen",                   "done": False},
        {"name": "Blumen",                                  "done": False},
        {"name": "GEMA-Meldung",                            "done": False},
        {"name": "Musikervertrag / Rechnung",               "done": False},
        {"name": "Programm abheften",                       "done": False},
        {"name": "Besucher und Spenden aufschreiben",       "done": False},
    ]
    for task in base:
        task["due"] = overrides[task["name"]]
        task["description"] = MZM_TASK_DESCRIPTIONS[task["name"]]
    return base


projects = [
    {
        "name": "Musik zur Marktzeit am 5. September 2026",
        "event_date": date(2026, 9, 5),
        "performers": "KMD Martin Winkler, Orgel und Gerlind Tautorus, Violine",
        "status": "geplant / mit Zeitplan",
        "tasks": make_tasks({
            "Eintrag in die Veranstaltungsdatenbank": date(2025, 12, 10),
            "Eintrag Papierkalender":                  date(2025, 12, 10),
            "Eintrag iCal":                            date(2025, 12, 10),
            "Plakate machen":                          date(2026,  8, 22),
            "Plakate aushängen":                       date(2026,  8, 26),  # 10 Tage vorher
            "Veranstaltung in den Facebook-Kalender":  date(2026,  8, 22),
            "Pressetext an die Vlothoer Zeitung":      date(2026,  9,  2),  # Mittwoch vorher
            "Programm machen":                         date(2026,  9,  2),
            "Programme bereitlegen":                   date(2026,  9,  5),
            "Blumen":                                  date(2026,  9,  4),
            "GEMA-Meldung":                            date(2026,  9,  5),
            "Musikervertrag / Rechnung":               date(2026,  9,  2),
            "Programm abheften":                       date(2026,  9,  5),
            "Besucher und Spenden aufschreiben":       date(2026,  9,  5),
        }),
    },
    {
        "name": "Musik zur Marktzeit am 3. Oktober 2026",
        "event_date": date(2026, 10, 3),
        "performers": "Posaunenchor der Christuskirche Herford",
        "status": "geplant / mit Zeitplan",
        "tasks": make_tasks({
            "Eintrag in die Veranstaltungsdatenbank": date(2025, 12, 10),
            "Eintrag Papierkalender":                  date(2025, 12, 10),
            "Eintrag iCal":                            date(2025, 12, 10),
            "Plakate machen":                          date(2026,  9, 19),
            "Plakate aushängen":                       date(2026,  9, 23),  # 10 Tage vorher
            "Veranstaltung in den Facebook-Kalender":  date(2026,  9, 19),
            "Pressetext an die Vlothoer Zeitung":      date(2026,  9, 30),  # Mittwoch vorher
            "Programm machen":                         date(2026,  9, 30),
            "Programme bereitlegen":                   date(2026, 10,  3),
            "Blumen":                                  date(2026, 10,  2),
            "GEMA-Meldung":                            date(2026, 10,  3),
            "Musikervertrag / Rechnung":               date(2026,  9, 30),
            "Programm abheften":                       date(2026, 10,  3),
            "Besucher und Spenden aufschreiben":       date(2026, 10,  3),
        }),
    },
    {
        "name": "Musik zur Marktzeit am 7. November 2026",
        "event_date": date(2026, 11, 7),
        "performers": "Blockflötenensemble 5+1, Leitung Elisabeth Schwanda",
        "status": "geplant / mit Zeitplan",
        "tasks": make_tasks({
            "Eintrag in die Veranstaltungsdatenbank": date(2025, 12, 10),
            "Eintrag Papierkalender":                  date(2025, 12, 10),
            "Eintrag iCal":                            date(2025, 12, 10),
            "Plakate machen":                          date(2026, 10, 24),
            "Plakate aushängen":                       date(2026, 10, 28),  # 10 Tage vorher
            "Veranstaltung in den Facebook-Kalender":  date(2026, 10, 24),
            "Pressetext an die Vlothoer Zeitung":      date(2026, 11,  4),  # Mittwoch vorher
            "Programm machen":                         date(2026, 11,  4),
            "Programme bereitlegen":                   date(2026, 11,  7),
            "Blumen":                                  date(2026, 11,  6),
            "GEMA-Meldung":                            date(2026, 11,  7),
            "Musikervertrag / Rechnung":               date(2026, 11,  4),
            "Programm abheften":                       date(2026, 11,  7),
            "Besucher und Spenden aufschreiben":       date(2026, 11,  7),
        }),
    },
    {
        "name": "Musik zur Marktzeit am 5. Dezember 2026",
        "event_date": date(2026, 12, 5),
        "performers": "Kreiskantorin Rina Sawabe (Kirchenkreis Lübbecke), Orgel",
        "status": "geplant / mit Zeitplan",
        "tasks": make_tasks({
            "Eintrag in die Veranstaltungsdatenbank": date(2025, 12, 10),
            "Eintrag Papierkalender":                  date(2025, 12, 10),
            "Eintrag iCal":                            date(2025, 12, 10),
            "Plakate machen":                          date(2026, 11, 21),
            "Plakate aushängen":                       date(2026, 11, 25),  # 10 Tage vorher
            "Veranstaltung in den Facebook-Kalender":  date(2026, 11, 21),
            "Pressetext an die Vlothoer Zeitung":      date(2026, 12,  2),  # Mittwoch vorher
            "Programm machen":                         date(2026, 12,  2),
            "Programme bereitlegen":                   date(2026, 12,  5),
            "Blumen":                                  date(2026, 12,  4),
            "GEMA-Meldung":                            date(2026, 12,  5),
            "Musikervertrag / Rechnung":               date(2026, 12,  2),
            "Programm abheften":                       date(2026, 12,  5),
            "Besucher und Spenden aufschreiben":       date(2026, 12,  5),
        }),
    },
]


def build_prompt(projects: list, today: date) -> str:
    lines = [
        f"Heute ist der {today.strftime('%d.%m.%Y')}.",
        "",
        "Ich bin Kirchenmusikerin und verwalte mehrere Veranstaltungsprojekte parallel.",
        "Hier ist der aktuelle Stand meiner laufenden Projekte:",
        "",
    ]

    for p in projects:
        days_until = (p["event_date"] - today).days
        open_tasks = [t for t in p["tasks"] if not t["done"]]

        lines.append(f"## {p['name']}")
        lines.append(f"Termin: {p['event_date'].strftime('%d.%m.%Y')} (in {days_until} Tagen)")
        lines.append(f"Mitwirkende: {p['performers']}")
        lines.append(f"Status: {p['status']}")
        lines.append("")

        done_count = len([t for t in p["tasks"] if t["done"]])
        if done_count:
            lines.append(f"Erledigt: {done_count} Aufgaben ✅")

        if open_tasks:
            lines.append(f"Offene Aufgaben ({len(open_tasks)}):")
            for t in open_tasks:
                diff = (t["due"] - today).days
                urgency = " ⚠️ DIESE WOCHE" if diff <= 7 else f" (fällig in {diff} Tagen, {t['due'].strftime('%d.%m.')})"
                lines.append(f"  ☐ {t['name']}{urgency}")
                lines.append(f"    → {t['description']}")
        else:
            lines.append("Alle Aufgaben erledigt. ✅")
        lines.append("")

    lines += [
        "---",
        "",
        "Erstelle mir eine priorisierte Wochenübersicht als Kirchenmusikerin.",
        "Stil: knapp, direkt, keine Floskeln. Stichpunkte statt langer Absätze.",
        "Maximal 300 Wörter. Kein Intro, kein Outro — direkt zur Sache.",
        "Struktur: (1) Was jetzt dran ist, (2) Was in 4 Wochen kommt, (3) Kritische Abhängigkeiten falls vorhanden.",
        "Auf Deutsch, Du-Form. Nur Infos aus den Aufgabenbeschreibungen — keine eigenen Annahmen ergänzen.",
    ]

    return "\n".join(lines)


def main():
    client = anthropic.Anthropic()
    prompt = build_prompt(projects, TODAY)

    print("\n=== CLAUDE-ANTWORT ===\n")

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
    print()


if __name__ == "__main__":
    main()
