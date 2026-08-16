import json
import logging

import anthropic

from .ai import KONTEXTE, AIUnavailableError, log_claude_call

logger = logging.getLogger(__name__)


def _format_history(projects: list) -> str:
    lines = ["# Vergangene Projekte als Referenz\n"]
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


def _format_rules(rules: list) -> str:
    if not rules:
        return ""
    lines = "\n".join(f"- {r}" for r in rules)
    return f"\nWende folgende Regeln an, sofern sie zum Projekttyp passen:\n{lines}\n"


def get_clarifying_questions(
    event_description: str, historical_projects: list, rules: list | None = None
) -> str:
    history = _format_history(historical_projects)
    rules_block = _format_rules(rules or [])
    client = anthropic.Anthropic()

    prompt = f"""{history}

---

Ein neues Projekt soll geplant werden:
{event_description}

Du bist ein erfahrener Planungsassistent. Erkenne den Projekttyp selbst und leite
alle relevanten Rahmenbedingungen aus dem Kontext ab.
Nutze die Referenzdaten nur intern zur Kalibrierung von Zeitabständen — erwähne sie
nicht in deiner Antwort.
{rules_block}
Basierend auf dem beschriebenen Projekt: Welche Informationen
brauchst du noch, um einen vollständigen Aufgabenplan zu erstellen?

Stelle maximal 4 gezielte Fragen. Nur Fragen, deren Antwort die Aufgabenliste
wirklich verändert. Keine Fragen, die du aus dem Kontext schon beantworten kannst.
Auf Deutsch, kurz und direkt."""

    with log_claude_call("get_clarifying_questions") as result:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        result["message"] = response
    return response.content[0].text


def _generate_plan_text(client, prompt: str) -> str:
    with log_claude_call("generate_plan") as result:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        result["message"] = response
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return raw


def generate_plan(
    event_description: str,
    answers: str,
    historical_projects: list,
    rules: list | None = None,
) -> dict:
    """Returns the parsed task plan. Retries once if Claude's answer isn't
    a valid JSON object — a plain re-ask, since the same prompt often
    self-corrects — and only gives up with AIUnavailableError after the
    second attempt.
    """
    history = _format_history(historical_projects)
    rules_block = _format_rules(rules or [])
    client = anthropic.Anthropic()

    prompt = f"""{history}

---

Neues Projekt: {event_description}

Ausgefüllte Angaben:
{answers}

Erstelle einen vollständigen Aufgabenplan als JSON.
Orientiere dich an den typischen Zeitabständen aus den historischen Daten — erwähne
die Referenzdaten aber nicht im Output.
Erkenne den Projekttyp selbst.
{rules_block}
Antworte NUR mit JSON, kein erklärender Text darum.

Format:
{{
  "project_name": "Kurzer prägnanter Eventname (max. 5 Wörter, kein Datum)",
  "tasks": [
    {{"name": "Aufgabenname", "days_before": 30, "kontext": "Büro"}},
    ...
  ]
}}

Mögliche Kontexte: {", ".join(KONTEXTE)}"""

    last_error = None
    for attempt in (1, 2):
        raw = _generate_plan_text(client, prompt)
        try:
            plan = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = exc
            logger.warning(
                "Claude returned unparseable JSON (attempt %d/2): %s", attempt, exc
            )
            continue
        if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
            # Valid JSON in the wrong shape (a bare task array, or an object
            # without a task list) would pass json.loads only to crash
            # planner_review on plan['tasks'] — one more bad response, worth
            # the same retry.
            last_error = None
            logger.warning(
                "Claude returned JSON that is not a plan object with a task list (attempt %d/2)",
                attempt,
            )
            continue
        return plan
    raise AIUnavailableError("Claude returned an unusable plan twice") from last_error
