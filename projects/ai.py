import anthropic
from datetime import date

def build_prompt(projects, today: date) -> str:
    lines = [
        f"Heute ist der {today.strftime('%d.%m.%Y')}.",
        "",
        "Ich bin Kirchenmusikerin und verwalte mehrere Veranstaltungsprojekte parallel.",
        "Hier ist der aktuelle Stand meiner laufenden Projekte:",
        "",
    ]

    for p in projects:
        days_until = (p.event_date - today).days
        open_tasks = p.tasks.filter(done=False)
        done_count = p.tasks.filter(done=True).count()

        lines.append(f"## {p.name}")
        lines.append(f"Termin: {p.event_date.strftime('%d.%m.%Y')} (in {days_until} Tagen)")
        lines.append(f"Mitwirkende: {p.performers}")
        lines.append(f"Erledigt: {done_count} Aufgaben ✅")
        lines.append(f"Offene Aufgaben ({open_tasks.count()}):")

        for t in open_tasks:
            diff = (t.due_date - today).days if t.due_date else '?'
            urgency = " ⚠️ DIESE WOCHE" if isinstance(diff, int) and diff <= 7 else f" (fällig in {diff} Tagen)"
            lines.append(f"  ☐ {t.name}{urgency}")
            if t.description:
                lines.append(f"    → {t.description}")

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


def generate_weekly_summary(projects, today: date) -> str:
    client = anthropic.Anthropic()
    prompt = build_prompt(projects, today)

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        return stream.get_final_text()