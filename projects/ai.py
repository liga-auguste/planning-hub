import anthropic
from datetime import date


KONTEXTE = ["Planung", "Büro", "Graphiker", "Kommunikation", "Unterwegs", "Vor Ort"]


def build_prompt(projects: list, today: date) -> str:
    lines = [
        f"Heute ist der {today.strftime('%d.%m.%Y')}.",
        "",
        "Ich bin Kirchenmusikerin und verwalte mehrere Veranstaltungsprojekte parallel.",
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
        "Erstelle mir eine Wochenübersicht als Kirchenmusikerin.",
        "Stil: knapp, direkt, keine Floskeln.",
        "Maximal 400 Wörter. Kein Intro, kein Outro — direkt zur Sache.",
        "Struktur:",
        "(1) Was jetzt dran ist — nach Dringlichkeit",
        "(2) Workflow-Blöcke — gruppiere offene Tasks nach Kontext projektübergreifend. "
        "Zeige jeden Kontext als eigenen Block. 'Planung'-Tasks immer als eigene Gruppe am Ende.",
        "(3) Kritische Abhängigkeiten falls vorhanden.",
        "Auf Deutsch, Du-Form. Nur Infos aus den Daten — keine eigenen Annahmen.",
        "Datumsformat: immer '5. August' oder 'Mo, 5. August' — keine führenden Nullen.",
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