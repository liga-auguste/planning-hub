import json
import logging

import anthropic

from .ai import KONTEXTE, AIUnavailableError, log_claude_call, system_instruction

logger = logging.getLogger(__name__)


def _format_history(projects: list, *, with_tasks: bool) -> str:
    """The reference block both planner calls open with: past projects, and
    optionally each project's tasks with their day offset from the event.

    #262: `with_tasks` is required rather than defaulting, because the two
    call sites want different answers and neither is the obvious one. This
    block was the largest duplicate across the six prompts — 1,274
    characters on the demo fixture, sent twice per planning flow, and capped
    at HISTORY_PROJECT_LIMIT (40) closed projects in production, where it
    runs to roughly 16,000.

    generate_plan needs the offsets: they are what it calibrates a new
    plan's intervals against. get_clarifying_questions does not — no
    clarifying question asks about an interval — but it does need the
    project names and performers, which are what tell it the kind of
    projects this person runs.
    """
    lines = ["# Vergangene Projekte als Referenz\n"]
    for p in projects:
        if not p["event_date"]:
            continue
        lines.append(f"## {p['name']}")
        if p["performers"]:
            lines.append(f"Mitwirkende: {p['performers']}")
        if with_tasks:
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
    # #262: without the task lines. The offsets are calibration data for the
    # plan, and the sentence that pointed at them goes with them — an
    # instruction about data the prompt no longer carries would be
    # half-false, which is the shape of wording this issue removes.
    history = _format_history(historical_projects, with_tasks=False)
    rules_block = _format_rules(rules or [])
    client = anthropic.Anthropic()

    prompt = f"""{history}

---

Ein neues Projekt soll geplant werden:
{event_description}

Erkenne den Projekttyp selbst und leite alle relevanten Rahmenbedingungen aus dem
Kontext ab.
Erwähne die Referenzprojekte nicht in deiner Antwort.
{rules_block}
Basierend auf dem beschriebenen Projekt: Welche Informationen
brauchst du noch, um einen vollständigen Aufgabenplan zu erstellen?

Stelle maximal 4 gezielte Fragen. Nur Fragen, deren Antwort die Aufgabenliste
wirklich verändert. Keine Fragen, die du aus dem Kontext schon beantworten kannst."""

    with log_claude_call("get_clarifying_questions") as result:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system_instruction(json_only=False),
            messages=[{"role": "user", "content": prompt}],
        )
        result["message"] = response
    return response.content[0].text


def _generate_plan_text(client, prompt: str) -> str:
    with log_claude_call("generate_plan") as result:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_instruction(),
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
    # With the task lines: this is the call that calibrates a new plan's
    # intervals against the past ones (#262).
    history = _format_history(historical_projects, with_tasks=True)
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
