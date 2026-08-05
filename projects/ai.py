import anthropic
from datetime import date


KONTEXTE = ["Planung", "Büro", "Graphiker", "Kommunikation", "Unterwegs", "Vor Ort"]

TASK_KONTEXT = {
    # Planung — gebündelt in Planungs-Sessions
    "Eintrag in die Veranstaltungsdatenbank": "Planung",
    "Eintrag iCal": "Planung",
    "Eintrag Papierkalender": "Planung",
    "Eintrag in den Veranstaltungskalender": "Planung",
    "Eintrag in die Veranstaltungskalender": "Planung",
    "Verbindliche Vereinbarung": "Planung",
    "Eintrittspreis festlegen": "Planung",
    "Programm festlegen": "Planung",
    # Büro — am Computer
    "Pressetext": "Büro",
    "GEMA-Meldung": "Büro",
    "Musikervertrag": "Büro",
    "Musikerverträge": "Büro",
    "Programm machen": "Büro",
    "Programm formatieren": "Büro",
    "Programme bereitlegen": "Büro",
    "Kostenabrechnung": "Büro",
    "Abrechnung": "Büro",
    "Saalplan": "Büro",
    "Vorverkauf vorbereiten": "Büro",
    "Orgaplan": "Büro",
    "Social Media": "Büro",
    "Social-Media": "Büro",
    "Info auf": "Büro",
    "Website": "Büro",
    "Antrag": "Büro",
    # Graphiker — externer Auftrag
    "Plakat": "Graphiker",
    "Lesezeichen": "Graphiker",
    "Banner": "Graphiker",
    "Eintrittskarten": "Graphiker",
    "Graphik": "Graphiker",
    # Kommunikation — Nachrichten, Anfragen
    "Nachricht": "Kommunikation",
    "Mail": "Kommunikation",
    "fragen": "Kommunikation",
    "organisieren": "Kommunikation",
    "vereinbaren": "Kommunikation",
    "schicken": "Kommunikation",
    "Aushilfen": "Kommunikation",
    # Unterwegs — physisch außer Haus
    "Plakate aushängen": "Unterwegs",
    "Plakate verteilen": "Unterwegs",
    "Plakatverteilung": "Unterwegs",
    "Blumen": "Unterwegs",
    "Vorverkauf hinbringen": "Unterwegs",
    # Vor Ort — nur am Veranstaltungstag
    "Kasse": "Vor Ort",
    "Körbchen": "Vor Ort",
    "Aufbau": "Vor Ort",
    "Abbau": "Vor Ort",
    "Besucher": "Vor Ort",
    "Spenden": "Vor Ort",
    "Einlass": "Vor Ort",
    "Programme bereitlegen": "Vor Ort",
}


def derive_kontext(task_name: str) -> list:
    for keyword, kontext in TASK_KONTEXT.items():
        if keyword.lower() in task_name.lower():
            return [kontext]
    return []


def build_prompt(projects: list, today: date) -> str:
    lines = [
        f"Heute ist der {today.strftime('%d.%m.%Y')}.",
        "",
        "Ich verwalte mehrere Projekte und Events parallel.",
        "Hier ist der aktuelle Stand meiner laufenden Projekte:",
        "",
    ]

    for p in projects:
        if not p["event_date"]:
            continue
        days_until = (p["event_date"] - today).days
        open_tasks = [t for t in p["tasks"] if not t["done"]]
        done_count = len([t for t in p["tasks"] if t["done"]])

        lines.append(f"## {p['name']}")
        lines.append(f"Termin: {p['event_date'].strftime('%d.%m.%Y')} (in {days_until} Tagen)")
        lines.append(f"Mitwirkende: {p.get('performers', '')}")
        lines.append(f"Erledigt: {done_count} Aufgaben ✅")
        lines.append(f"Offene Aufgaben ({len(open_tasks)}):")

        for t in open_tasks:
            diff = (t["due"] - today).days if t["due"] else "?"
            urgency = " ⚠️ DIESE WOCHE" if isinstance(diff, int) and diff <= 7 else f" (fällig in {diff} Tagen)"
            kontext = f" [Kontext: {', '.join(t['kontext'])}]" if t["kontext"] else ""
            lines.append(f"  ☐ {t['name']}{urgency}{kontext}")

        lines.append("")

    # Kontext-Übersicht projektübergreifend
    lines += ["---", "", "## Kontext-Übersicht (projektübergreifend)", ""]
    all_open = [t for p in projects for t in p["tasks"] if not t["done"]]
    for kontext in KONTEXTE:
        tasks_im_kontext = [t for t in all_open if kontext in t["kontext"]]
        if tasks_im_kontext:
            lines.append(f"**{kontext}:** {', '.join(t['name'] for t in tasks_im_kontext)}")
    lines.append("")

    lines += [
        "---",
        "",
        "Erstelle mir eine Wochenübersicht. Schreibe als Assistentin — nicht als Auflistungsmaschine.",
        "Kein Intro, kein Outro. Auf Deutsch, Du-Form. Nur Infos aus den Daten.",
        "Datumsformat: '5. August' — keine führenden Nullen.",
        "",
        "Formatierung — verschachtelte Markdown-Listen:",
        "- Jedes Projekt als Listenpunkt: **Projektname** — ein einziger Satz mit Einschätzung/Kontext",
        "- Tasks darunter als Unterpunkte, nur die relevantesten (max. 4)",
        "Beispiel:",
        "- **Musik zur Marktzeit, 5. Aug** — übermorgen, alles läuft, nur Aufbau noch offen:",
        "  - Aufbau koordinieren",
        "  - Noten mitnehmen",
        "",
        "Der Satz nach dem — soll echten Assistenzwert haben: Was ist der Status? Was ist kritisch?",
        "Nicht: 'Tasks offen'. Sondern: 'Plakate müssen heute raus' oder 'noch gut im Zeitplan'.",
        "",
        "Struktur — zwei Blöcke, getrennt durch ---:",
        "",
        "**Jetzt fällig** — überfällige und diese Woche fällige Projekte",
        "",
        "---",
        "",
        "**Nächste Woche** — Projekte mit Tasks in den kommenden 7–14 Tagen",
    ]

    return "\n".join(lines)


def generate_weekly_summary(projects: list, today: date) -> str:
    client = anthropic.Anthropic()
    prompt = build_prompt(projects, today)

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        return stream.get_final_text()