import anthropic
import json as _json
import logging
from contextlib import contextmanager
from datetime import date

logger = logging.getLogger(__name__)


class AIUnavailableError(Exception):
    """Raised when Claude can't be reached, after the SDK's own retries are
    exhausted. Callers show one German "not available right now" state for
    this instead of a stack trace — see the failure table in issue #29.
    """


@contextmanager
def translate_anthropic_errors():
    """Wraps a Claude call site.

    anthropic.Anthropic already retries connection errors, timeouts, 429s and
    5xxs internally (max_retries=2 by default) before raising, so this only
    has to catch what survives that and turn it into the one exception the
    views know how to show.
    """
    try:
        yield
    except anthropic.APIError as exc:
        logger.warning("Anthropic call failed: %s", exc)
        raise AIUnavailableError("Claude request failed") from exc


KONTEXTE = ["Planung", "Büro", "Graphiker", "Kommunikation", "Unterwegs", "Vor Ort"]

TASK_KONTEXT = {
    # Planung — batched into planning sessions
    "Eintrag in die Veranstaltungsdatenbank": "Planung",
    "Eintrag iCal": "Planung",
    "Eintrag Papierkalender": "Planung",
    "Eintrag in den Veranstaltungskalender": "Planung",
    "Eintrag in die Veranstaltungskalender": "Planung",
    "Verbindliche Vereinbarung": "Planung",
    "Eintrittspreis festlegen": "Planung",
    "Programm festlegen": "Planung",
    # Büro — desk work
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
    # Graphiker — commissioned externally
    "Plakat": "Graphiker",
    "Lesezeichen": "Graphiker",
    "Banner": "Graphiker",
    "Eintrittskarten": "Graphiker",
    "Graphik": "Graphiker",
    # Kommunikation — messages and enquiries
    "Nachricht": "Kommunikation",
    "Mail": "Kommunikation",
    "fragen": "Kommunikation",
    "organisieren": "Kommunikation",
    "vereinbaren": "Kommunikation",
    "schicken": "Kommunikation",
    "Aushilfen": "Kommunikation",
    # Unterwegs — requires leaving the house
    "Plakate aushängen": "Unterwegs",
    "Plakate verteilen": "Unterwegs",
    "Plakatverteilung": "Unterwegs",
    "Blumen": "Unterwegs",
    "Vorverkauf hinbringen": "Unterwegs",
    # Vor Ort — on the event day only
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


def build_prompt(projects: list, today: date, single_project_demo: bool = False) -> str:
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

        lines.append(f"## {'Dein Projekt' if single_project_demo else p['name']}")
        lines.append(f"Termin: {p['event_date'].strftime('%d.%m.%Y')} (in {days_until} Tagen)")
        lines.append(f"Mitwirkende: {p.get('performers', '')}")
        lines.append(f"Erledigt: {done_count} Aufgaben ✅")
        lines.append(f"Offene Aufgaben ({len(open_tasks)}):")

        for t in open_tasks:
            diff = (t["due"] - today).days if t["due"] else "?"
            urgency = " ⚠️ DIESE WOCHE" if isinstance(diff, int) and diff <= 7 else f" (fällig in {diff} Tagen)"
            kontext = f" [Kontext: {', '.join(t['kontext'])}]" if (t["kontext"] and not single_project_demo) else ""
            lines.append(f"  ☐ {t['name']}{urgency}{kontext}")

        lines.append("")

    # Context overview across all projects
    lines += ["---", "", "## Kontext-Übersicht (projektübergreifend)", ""]
    all_open = [t for p in projects for t in p["tasks"] if not t["done"]]
    for kontext in KONTEXTE:
        tasks_im_kontext = [t for t in all_open if kontext in t["kontext"]]
        if tasks_im_kontext:
            lines.append(f"**{kontext}:** {', '.join(t['name'] for t in tasks_im_kontext)}")
    lines.append("")

    if single_project_demo:
        lines += [
            "---",
            "",
            "Erstelle eine Übersicht für dieses einzelne Projekt. Schreibe als Assistentin — direkt, klar, hilfreich.",
            "Kein Intro, kein Outro. Auf Deutsch, Du-Form. Nenne den Projektnamen NICHT — er ist bereits im Header sichtbar.",
            "Datumsformat: '5. August' — keine führenden Nullen.",
            "",
            "Formatierung — verschachtelte Markdown-Listen ohne Projektname:",
            "- Status oder Kontext als Listenpunkt: **Thema** — ein Satz mit Einschätzung",
            "- Tasks darunter als Unterpunkte (max. 4)",
            "Beispiel:",
            "- **Jetzt kritisch** — die Buchung muss heute raus, sonst wird der Termin knapp:",
            "    - Venue buchen",
            "    - Catering bestätigen",
            "",
            "Der Satz soll echten Assistenzwert haben: Was ist kritisch? Was läuft gut?",
            "",
            "Struktur — zwei Blöcke mit genau diesen Überschriften:",
            "",
            "## Jetzt fällig",
            "Darunter: überfällige und diese Woche fällige Aufgaben.",
            "",
            "## Nächste Woche",
            "Darunter: Aufgaben in den kommenden 7–14 Tagen.",
        ]
    else:
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
            "    - Aufbau koordinieren",
            "    - Noten mitnehmen",
            "",
            "Der Satz nach dem — soll echten Assistenzwert haben: Was ist der Status? Was ist kritisch?",
            "Nicht: 'Tasks offen'. Sondern: 'Plakate müssen heute raus' oder 'noch gut im Zeitplan'.",
            "",
            "Struktur — zwei Blöcke mit genau diesen Überschriften:",
            "",
            "## Jetzt fällig",
            "Darunter: überfällige und diese Woche fällige Projekte.",
            "",
            "## Nächste Woche",
            "Darunter: Projekte mit Tasks in den kommenden 7–14 Tagen.",
        ]

    return "\n".join(lines)


def _valid_moments(raw) -> list:
    """Keeps the moments whose date the rest of the app can actually use.

    The dates travel into the session, become the allowlist of postable sim dates
    and are parsed back with date.fromisoformat(), while the dashboard JS builds a
    timestamp from them as `date + 'T12:00:00'`. Neither can be given whatever the
    model happened to emit, so a moment with an unparseable date is dropped and a
    parseable one is normalised to YYYY-MM-DD.
    """
    moments = []
    for moment in raw if isinstance(raw, list) else []:
        if not isinstance(moment, dict) or not isinstance(moment.get('date'), str):
            continue
        try:
            parsed = date.fromisoformat(moment['date'])
        except ValueError:
            continue
        moments.append({**moment, 'date': parsed.isoformat()})
    return moments


def generate_timelapse_moments(project_name: str, event_date: date, tasks: list) -> list:
    """Returns 4 narrative key moments as [{date_iso, label, description}]."""
    today = date.today()
    client = anthropic.Anthropic()
    task_lines = "\n".join(
        f"- {t['name']} (fällig: {t['date']})"
        for t in tasks if t.get('date')
    )
    prompt = f"""Du planst ein Projekt: "{project_name}", Termin: {event_date.strftime('%d.%m.%Y')}.

Aufgaben:
{task_lines}

Wähle 4 dramatisch interessante Momente aus dem Zeitverlauf — Wendepunkte, bei denen etwas Entscheidendes passiert oder der Status des Projekts sich spürbar verändert. Benenne jeden Moment nach dem, was inhaltlich passiert (z.B. "Buchungen starten", "Öffentlichkeitsphase", "Letzter Schliff", "Generalprobe"). Keine generischen Zeitangaben.

Antworte NUR mit einem JSON-Array, kein anderer Text:
[
  {{"date": "YYYY-MM-DD", "label": "2–3 Wörter", "description": "Ein Satz was gerade passiert"}},
  {{"date": "YYYY-MM-DD", "label": "...", "description": "..."}},
  {{"date": "YYYY-MM-DD", "label": "...", "description": "..."}},
  {{"date": "YYYY-MM-DD", "label": "...", "description": "..."}}
]

Zeitraum: {today.isoformat()} bis {event_date.isoformat()}, chronologisch sortiert."""

    with translate_anthropic_errors():
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
    text = response.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return _valid_moments(_json.loads(text))


def generate_weekly_summary(projects: list, today: date, single_project_demo: bool = False) -> str:
    client = anthropic.Anthropic()
    prompt = build_prompt(projects, today, single_project_demo=single_project_demo)

    with translate_anthropic_errors():
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            return stream.get_final_text()