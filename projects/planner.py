import anthropic

def _format_history(projects: list) -> str:
    lines = ["# Vergangene Veranstaltungen als Referenz\n"]
    for p in projects:
        if not p["event_date"]:
            continue
        lines.append(f"## {p['name']}")
        if p["performers"]:
            lines.append(f"Mitwirkende: {p['performers']}")
        for t in p["tasks"]:
            if t["due"]:
                offset = (p["event_date"] - t["due"]).days
                lines.append(f"  - {t['name']} ({offset} Tage vor dem Termin)")
            else:
                lines.append(f"  - {t['name']} (kein Datum)")
        lines.append("")
    return "\n".join(lines)


def get_clarifying_questions(event_description: str, historical_projects: list) -> str:
    history = _format_history(historical_projects)
    client = anthropic.Anthropic()

    prompt = f"""{history}

---

Eine neue Veranstaltung soll geplant werden:
{event_description}

Du bist ein erfahrener Veranstaltungsassistent für eine Kirchenmusikerin.
Wichtige Regeln für diesen Kontext:
- GEMA-Meldung: immer bei Konzertveranstaltungen, nie bei Gottesdiensten — hängt am Typ, nicht am Repertoire
- Musikerverträge + Honorare + Fahrtkosten: immer wenn externe Musiker mitwirken
- Vorverkauf: selten, nur bei größeren Konzerten relevant
- Gottesdienste haben grundsätzlich keinen Eintritt
- Plakat: bei Konzerten immer vorausgesetzt (Standard: professioneller Druck beim Graphiker) — nur fragen wenn die Situation davon abweicht. Bei Gottesdiensten: einfacher Hausausdruck, keine Frage nötig. Bei Veranstaltungsreihen: ein Plakat für die ganze Reihe.

Basierend auf den historischen Daten: Welche Informationen brauchst du noch,
um einen vollständigen Aufgabenplan zu erstellen?

Stelle maximal 4 gezielte Fragen. Nur Fragen, deren Antwort die Aufgabenliste
wirklich verändert. Keine Fragen die du aus dem Kontext schon beantworten kannst.
Auf Deutsch, kurz und direkt."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

def generate_plan(event_description: str, answers: str, historical_projects: list) -> str:
    history = _format_history(historical_projects)
    client = anthropic.Anthropic()

    prompt = f"""{history}

---

Neue Veranstaltung: {event_description}

Ausgefüllte Angaben:
{answers}

Erstelle einen vollständigen Aufgabenplan als JSON.
Orientiere dich an den typischen Zeitabständen aus den historischen Daten.
Antworte NUR mit JSON, kein erklärender Text darum.

Format:
{{
  "tasks": [
    {{"name": "Aufgabenname", "days_before": 30, "kontext": "Büro"}},
    ...
  ]
}}

Mögliche Kontexte: Planung, Büro, Graphiker, Kommunikation, Unterwegs, Vor Ort"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return raw