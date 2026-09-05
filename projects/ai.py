import json as _json
import logging
import time
from contextlib import contextmanager
from datetime import date

import anthropic
from django.utils import timezone

from .dates import is_same_iso_week

logger = logging.getLogger(__name__)


class AIUnavailableError(Exception):
    """Raised when Claude can't be reached, after the SDK's own retries are
    exhausted. Callers show one German "not available right now" state for
    this instead of a stack trace — see the failure table in issue #29.
    """


@contextmanager
def log_claude_call(call_name: str):
    """Wraps a Claude call site: exception translation (#29) plus structured
    duration/usage logging (#31), so a slow or expensive call is visible
    without going through the Anthropic console.

    anthropic.Anthropic already retries connection errors, timeouts, 429s and
    5xxs internally (max_retries=2 by default) before raising, so the except
    clause only has to catch what survives that and turn it into the one
    exception the views know how to show.

    Populate result["message"] with the SDK response (or
    stream.get_final_message() for a streaming call) before the block ends,
    so the log line can report the model and token usage.
    """
    started = time.monotonic()
    result = {}
    try:
        yield result
    except anthropic.APIError as exc:
        logger.warning(
            "claude_call call=%s duration_ms=%.0f outcome=error error=%s",
            call_name,
            (time.monotonic() - started) * 1000,
            exc,
        )
        raise AIUnavailableError("Claude request failed") from exc
    else:
        message = result.get("message")
        usage = message.usage if message else None
        logger.info(
            "claude_call call=%s model=%s duration_ms=%.0f input_tokens=%s output_tokens=%s outcome=success",
            call_name,
            message.model if message else "?",
            (time.monotonic() - started) * 1000,
            usage.input_tokens if usage else "?",
            usage.output_tokens if usage else "?",
        )


KONTEXTE = ["Planung", "Büro", "Graphiker", "Kommunikation", "Unterwegs", "Vor Ort"]

# The two summary sections, in render order: JSON key ↔ German heading. The
# keys are German by decision (#122 plan) — they mirror the two fixed block
# headings the summary always had, so prompt text and key agree.
SUMMARY_SECTIONS = (
    ("jetzt_faellig", "Jetzt fällig"),
    ("naechste_woche", "Nächste Woche"),
)


def _number_projects_and_tasks(projects: list) -> tuple[list, list]:
    """The single source of the reference numbering shared by build_prompt
    and resolve_weekly_summary (#122): a 1-based position in these two lists
    is what project_ref / task_refs mean.

    Every task occupies a number, done ones included, even though the prompt
    only ever shows open tasks: numbering by openness would shift every later
    number the moment a task is toggled between cache-write and render time,
    silently re-pointing the cached refs at the wrong tasks. Position depends
    only on task order, which is stable across a toggle. That order is the
    chronological one _annotate_tasks (views.py) establishes before every
    prompt build and every resolve, so both sides number the same list (#140).
    """
    numbered_projects = [p for p in projects if p["event_date"]]
    numbered_tasks = [t for p in numbered_projects for t in p["tasks"]]
    return numbered_projects, numbered_tasks


def build_prompt(projects: list, today: date, single_project_demo: bool = False) -> str:
    numbered_projects, _ = _number_projects_and_tasks(projects)
    lines = [
        f"Heute ist der {today.strftime('%d.%m.%Y')}.",
        "",
        "Ich verwalte mehrere Projekte und Events parallel.",
        "Hier ist der aktuelle Stand meiner laufenden Projekte:",
        "",
    ]

    task_no = 0
    for project_no, p in enumerate(numbered_projects, start=1):
        days_until = (p["event_date"] - today).days
        open_tasks = [t for t in p["tasks"] if not t["done"]]
        done_count = len([t for t in p["tasks"] if t["done"]])

        lines.append(f"## {'Dein Projekt' if single_project_demo else p['name']}")
        if not single_project_demo:
            lines.append(f"Projekt-Nr.: {project_no}")
        lines.append(
            f"Termin: {p['event_date'].strftime('%d.%m.%Y')} (in {days_until} Tagen)"
        )
        lines.append(f"Mitwirkende: {p.get('performers', '')}")
        lines.append(f"Erledigt: {done_count} Aufgaben")
        lines.append(f"Offene Aufgaben ({len(open_tasks)}):")

        for t in p["tasks"]:
            task_no += 1
            if t["done"]:
                continue
            if t["due"] is None:
                urgency = " — ohne Termin"
            else:
                diff = (t["due"] - today).days
                # #169: calendar-week based, not a rolling 7-day window — but
                # due<=today (overdue or today) is handled first and keeps
                # its exact old label, so an overdue task from a *past*
                # calendar week still reads "DIESE WOCHE" rather than
                # falling into the days-remaining else branch below.
                if diff == 0:
                    urgency = " — HEUTE fällig"
                elif diff < 0 or is_same_iso_week(t["due"], today):
                    urgency = " — DIESE WOCHE"
                else:
                    urgency = f" (fällig in {diff} Tagen)"
            kontext = (
                f" [Kontext: {', '.join(t['kontext'])}]"
                if (t["kontext"] and not single_project_demo)
                else ""
            )
            lines.append(f"  - [{task_no}] {t['name']}{urgency}{kontext}")

        lines.append("")

    # Context overview across all projects — omitted entirely when no task
    # carries a kontext (kontext is production-only, see #18), rather than
    # emitting the heading over an empty block.
    all_open = [t for p in projects for t in p["tasks"] if not t["done"]]
    kontext_lines = []
    for kontext in KONTEXTE:
        tasks_im_kontext = [t for t in all_open if kontext in t["kontext"]]
        if tasks_im_kontext:
            kontext_lines.append(
                f"**{kontext}:** {', '.join(t['name'] for t in tasks_im_kontext)}"
            )
    if kontext_lines:
        lines += ["---", "", "## Kontext-Übersicht (projektübergreifend)", ""]
        lines += kontext_lines
        lines.append("")

    if single_project_demo:
        lines += [
            "---",
            "",
            "Erstelle eine Übersicht für dieses einzelne Projekt. Schreibe als Assistentin — direkt, klar, hilfreich.",
            "Nur Infos aus den Daten. Auf Deutsch, Du-Form.",
            "Datumsformat: '5. August' — keine führenden Nullen.",
            "",
            "Antworte NUR mit JSON, kein anderer Text darum. Format:",
            '{"jetzt_faellig": [{"heading": "Jetzt kritisch", "assessment": "die Buchung muss heute raus, sonst wird der Termin knapp", "task_refs": [1, 2]}], "naechste_woche": []}',
            "",
            '- "heading": Status oder Kontext als kurzes Thema (2–3 Wörter). Nenne den Projektnamen NICHT — er ist bereits im Header sichtbar.',
            '- "assessment": ein Satz mit Einschätzung und echtem Assistenzwert: Was ist kritisch? Was läuft gut?',
            '- "task_refs": die Nummern (in eckigen Klammern bei jeder offenen Aufgabe oben) der relevantesten Aufgaben, max. 4.',
            "",
            "Zuordnung der Blöcke:",
            '- "jetzt_faellig": überfällige und diese Woche fällige Aufgaben.',
            '- "naechste_woche": Aufgaben in den kommenden 7–14 Tagen.',
        ]
    else:
        lines += [
            "---",
            "",
            "Erstelle mir eine Wochenübersicht. Schreibe als Assistentin — nicht als Auflistungsmaschine.",
            "Nur Infos aus den Daten. Auf Deutsch, Du-Form.",
            "Datumsformat: '5. August' — keine führenden Nullen.",
            "",
            "Antworte NUR mit JSON, kein anderer Text darum. Format:",
            '{"jetzt_faellig": [{"project_ref": 1, "assessment": "übermorgen, alles läuft, nur Aufbau noch offen", "task_refs": [1, 2]}], "naechste_woche": []}',
            "",
            '- "project_ref": die Projekt-Nr. des Projekts (steht bei jedem Projekt oben).',
            '- "assessment": ein einziger Satz mit Einschätzung/Kontext und echtem Assistenzwert: Was ist der Status? Was ist kritisch?',
            "  Nicht: 'Tasks offen'. Sondern: 'Plakate müssen heute raus' oder 'noch gut im Zeitplan'.",
            "  Nenne den Projektnamen NICHT im Satz — er wird aus den Daten ergänzt.",
            '- "task_refs": die Nummern (in eckigen Klammern bei jeder offenen Aufgabe oben) der relevantesten Aufgaben, max. 4.',
            "",
            "Zuordnung der Blöcke:",
            '- "jetzt_faellig": überfällige und diese Woche fällige Projekte.',
            '- "naechste_woche": Projekte mit Aufgaben in den kommenden 7–14 Tagen.',
        ]

    return "\n".join(lines)


def _valid_moments(raw) -> list:
    """Keeps the moments whose date the rest of the app can actually use.

    The dates travel into the session, become the allowlist of postable sim dates
    and are parsed back with date.fromisoformat(), while the dashboard JS builds a
    timestamp from them as `date + 'T12:00:00'`. Neither can be given whatever the
    model happened to emit, so a moment with an unparseable date is dropped and a
    parseable one is normalised to YYYY-MM-DD. The result is sorted by that
    normalised date, since the model's own "chronologisch sortiert" instruction
    isn't enforced anywhere downstream (#101).
    """
    moments = []
    for moment in raw if isinstance(raw, list) else []:
        if not isinstance(moment, dict) or not isinstance(moment.get("date"), str):
            continue
        try:
            parsed = date.fromisoformat(moment["date"])
        except ValueError:
            continue
        moments.append({**moment, "date": parsed.isoformat()})
    return sorted(moments, key=lambda m: m["date"])


def generate_timelapse_moments(
    project_name: str, event_date: date, tasks: list
) -> list:
    """Returns 4 narrative key moments as [{date_iso, label, description}]."""
    today = timezone.localdate()
    client = anthropic.Anthropic()
    task_lines = "\n".join(
        f"- {t['name']} (fällig: {t['date']})" for t in tasks if t.get("date")
    )
    prompt = f"""Du planst ein Projekt: "{project_name}", Termin: {event_date.strftime("%d.%m.%Y")}.

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

    with log_claude_call("generate_timelapse_moments") as result:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        result["message"] = response
    text = response.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        text = text.removeprefix("json")
        text = text.strip()
    return _valid_moments(_json.loads(text))


def generate_weekly_summary(
    projects: list, today: date, single_project_demo: bool = False
) -> dict:
    """Returns Claude's raw reference dict (#122): section keys mapping to
    blocks of {project_ref | heading, assessment, task_refs}. The refs are
    resolved against live data by resolve_weekly_summary at render time —
    this raw dict is what the caches store, never the resolved result.

    Retries once if the answer isn't a valid JSON object with both section
    keys — a plain re-ask, same contract as generate_plan — and only gives
    up with AIUnavailableError after the second attempt.
    """
    client = anthropic.Anthropic()
    prompt = build_prompt(projects, today, single_project_demo=single_project_demo)

    last_error = None
    for attempt in (1, 2):
        with (
            log_claude_call("generate_weekly_summary") as result,
            client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            ) as stream,
        ):
            text = stream.get_final_text()
            result["message"] = stream.get_final_message()
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError as exc:
            last_error = exc
            logger.warning(
                "Claude returned an unparseable weekly summary (attempt %d/2): %s",
                attempt,
                exc,
            )
            continue
        if not isinstance(data, dict) or not all(
            isinstance(data.get(key), list) for key, _ in SUMMARY_SECTIONS
        ):
            last_error = None
            logger.warning(
                "Claude returned a weekly summary without both section lists (attempt %d/2)",
                attempt,
            )
            continue
        return data
    raise AIUnavailableError(
        "Claude returned an unusable weekly summary twice"
    ) from last_error


def build_closeout_prompt(stats: dict, today: date) -> str:
    """#169: the close-out review's summary — appreciative by design, not a
    second status report. Rescheduled tasks are named as decisions, not as
    a shortfall against the week.
    """
    return "\n".join(
        [
            f"Heute ist der {today.strftime('%d.%m.%Y')}. Ich schließe die Woche ab.",
            "",
            f"Erledigt: {stats['completed_count']} Aufgaben",
            f"Verschoben in die nächste Woche: {stats['rescheduled_count']} Aufgaben",
            f"Neu dazugekommen: {stats['added_count']} Aufgaben",
            "",
            "Schreib eine kurze Rückschau auf diese Woche. Anerkennend, nicht bewertend:",
            "was erledigt wurde, zählt. Verschobene Aufgaben sind bewusste",
            "Planungsentscheidungen, keine verpassten Deadlines — benenne sie neutral,",
            "nicht als Rückstand. Auf Deutsch, Du-Form, 2–3 Sätze.",
            "",
            "Antworte NUR mit JSON, kein anderer Text darum. Format:",
            '{"summary_text": "..."}',
        ]
    )


def generate_closeout_summary(stats: dict, today: date) -> str:
    """Returns the close-out review's German summary text.

    Same retry contract as generate_weekly_summary: one re-ask on
    unparseable or wrong-shape JSON, AIUnavailableError after the second bad
    response, SDK failures never spent as a JSON retry.
    """
    client = anthropic.Anthropic()
    prompt = build_closeout_prompt(stats, today)

    last_error = None
    for attempt in (1, 2):
        with (
            log_claude_call("generate_closeout_summary") as result,
            client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            ) as stream,
        ):
            text = stream.get_final_text()
            result["message"] = stream.get_final_message()
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError as exc:
            last_error = exc
            logger.warning(
                "Claude returned an unparseable close-out summary (attempt %d/2): %s",
                attempt,
                exc,
            )
            continue
        if not isinstance(data, dict) or not isinstance(data.get("summary_text"), str):
            last_error = None
            logger.warning(
                "Claude returned a close-out summary without summary_text (attempt %d/2)",
                attempt,
            )
            continue
        return data["summary_text"]
    raise AIUnavailableError(
        "Claude returned an unusable close-out summary twice"
    ) from last_error


def _resolve_ref(ref, numbered: list):
    """A 1-based index into the numbered list, or None. bool is excluded
    explicitly — True is an int subclass and would resolve as index 1."""
    if isinstance(ref, bool) or not isinstance(ref, int):
        return None
    if not 1 <= ref <= len(numbered):
        return None
    return numbered[ref - 1]


def resolve_weekly_summary(
    data: dict, projects: list, single_project_demo: bool = False
) -> list:
    """Builds the render-ready sections from Claude's raw reference dict,
    resolved against `projects` as they are *now* — called at render time,
    never at cache-write time, so checkbox state can't go stale behind the
    summary's cache layers (#122).

    Robustness over completeness: an unresolvable task ref is dropped and
    the rest of its block stays; a block with no usable heading (bad
    project_ref, or a missing heading in single-project mode) is dropped
    whole — there is nothing to head it with.
    """
    numbered_projects, numbered_tasks = _number_projects_and_tasks(projects)
    sections = []
    for key, title in SUMMARY_SECTIONS:
        raw_blocks = data.get(key)
        blocks = []
        for raw_block in raw_blocks if isinstance(raw_blocks, list) else []:
            if not isinstance(raw_block, dict):
                continue
            assessment = raw_block.get("assessment")
            block = {"assessment": assessment if isinstance(assessment, str) else ""}
            if single_project_demo:
                heading = raw_block.get("heading")
                if not isinstance(heading, str) or not heading.strip():
                    continue
                block["heading"] = heading
            else:
                project = _resolve_ref(raw_block.get("project_ref"), numbered_projects)
                if project is None:
                    continue
                block["project_id"] = project["id"]
                block["project_name"] = project.get("display_name") or project["name"]
                block["event_date_display"] = project.get("event_date_display", "")
            refs = raw_block.get("task_refs")
            block["tasks"] = [
                {
                    "id": task["id"],
                    "name": task["name"],
                    "done": task["done"],
                    "urgency": task.get("urgency", "ok"),
                    # #190: the raw date, not a formatted string — both
                    # summary templates run it through plan_date, so they
                    # share one format with the task rows (#189).
                    "due": task.get("due"),
                }
                for ref in (refs if isinstance(refs, list) else [])
                if (task := _resolve_ref(ref, numbered_tasks)) is not None
            ]
            blocks.append(block)
        sections.append({"title": title, "blocks": blocks})
    return sections
