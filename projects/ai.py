import json as _json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date

import anthropic
from django.utils import timezone

from .date_format import format_week_range
from .dates import is_same_iso_week, iso_week_bounds

logger = logging.getLogger(__name__)


class AIUnavailableError(Exception):
    """Raised when Claude can't be reached, after the SDK's own retries are
    exhausted. Callers show one German "not available right now" state for
    this instead of a stack trace — see the failure table in issue #29.
    """


# Set only while collect_call_usage is active — None in normal operation, so
# the app itself never accumulates per-call records. A ContextVar rather than
# a module global because the app runs under threaded workers: a plain global
# set by one request would collect another request's calls too.
_usage_records: ContextVar[list | None] = ContextVar("_usage_records", default=None)


@contextmanager
def collect_call_usage():
    """Yields a list that receives one record per successful Claude call made
    inside the block: {"call", "model", "input_tokens", "output_tokens"}.

    Exists for the language eval, which reports what a touchpoint's prompt
    actually costs — the one number that says whether a prompt got shorter.
    log_claude_call already has the usage in hand for its log line; this hands
    the same figures to a caller that wants them as data rather than as text.

    A failed call records nothing: it has no usage to report, and
    log_claude_call raises before reaching the success branch.
    """
    records = []
    token = _usage_records.set(records)
    try:
        yield records
    finally:
        _usage_records.reset(token)


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
        records = _usage_records.get()
        if records is not None:
            records.append(
                {
                    "call": call_name,
                    "model": message.model if message else "?",
                    "input_tokens": usage.input_tokens if usage else None,
                    "output_tokens": usage.output_tokens if usage else None,
                }
            )


KONTEXTE = ["Planung", "Büro", "Graphiker", "Kommunikation", "Unterwegs", "Vor Ort"]

# The two summary sections, in render order: JSON key ↔ German heading. The
# keys are German by decision (#122 plan) — they mirror the two fixed block
# headings the summary always had, so prompt text and key agree.
SUMMARY_SECTIONS = (
    ("jetzt_faellig", "Jetzt fällig"),
    ("naechste_woche", "Nächste Woche"),
)

# The voice every Claude touchpoint shares (#262), sent as the `system`
# parameter rather than copied into each prompt body. The API renders
# tools → system → messages, so an instruction that never varies sits ahead
# of the data that does, and all six call sites read one constant instead of
# six copies that had already drifted: "Auf Deutsch, Du-Form" existed three
# times and was missing from three touchpoints entirely, the date format
# twice, the JSON rule in five different wordings.
#
# A relocation, not a rewrite — the same rules stated once. The one addition
# is brevity, which language_eval.py's judge has always scored ("clear,
# friendly, short") and no prompt actually asked for. Each touchpoint's own
# task, its JSON shape and its domain rules stay in the prompt that needs them.
#
# What did *not* survive the move are the register clauses the six prompts
# carried in four wordings ("nicht als Auflistungsmaschine", "direkt, klar,
# hilfreich", "ein erfahrener Planungsassistent"). Measured: broadcasting
# them to all six raised both input and output tokens, which is #262's own
# finding — an expansive instruction produces an expansive answer. The bar
# is stated once, short, and the sentence rule is what makes it operable.
VOICE_INSTRUCTION = (
    "Du bist die Planungsassistentin dieser App.\n"
    "Schreib klar, freundlich und kurz — ein Gedanke pro Satz.\n"
    "Auf Deutsch, Du-Form.\n"
    "Datumsformat: '5. August' — keine führenden Nullen."
)

# The one wording for the five touchpoints that answer in JSON, replacing the
# five that existed before. Separate from VOICE_INSTRUCTION because
# get_clarifying_questions answers in prose: a single shared block carrying
# this rule would be actively wrong for that touchpoint.
JSON_ONLY_INSTRUCTION = "Antworte NUR mit JSON, kein anderer Text darum."


def system_instruction(json_only: bool = True) -> str:
    """The `system` value a touchpoint sends: the shared voice, plus the
    JSON-only rule for the five touchpoints whose answer is parsed as JSON.

    Pass json_only=False for a touchpoint that answers in prose — today only
    get_clarifying_questions.
    """
    if not json_only:
        return VOICE_INSTRUCTION
    return f"{VOICE_INSTRUCTION}\n\n{JSON_ONLY_INSTRUCTION}"


def _number_projects_and_tasks(projects: list) -> tuple[list, list]:
    """The single source of the reference numbering shared by build_prompt
    and resolve_weekly_summary (#122): a 1-based position in the task list
    is what task_refs means. The project list is what the prompt renders
    and what the task list is drawn from — since #49 nothing refers to a
    project by number any more.

    Every task occupies a number, done ones included, even though the prompt
    only ever shows open tasks: numbering by openness would shift every later
    number the moment a task is toggled between cache-write and render time,
    silently re-pointing the cached refs at the wrong tasks. Position depends
    only on task order, which is stable across a toggle. That order is the
    chronological one _annotate_tasks (views.py) establishes before every
    prompt build and every resolve, so both sides number the same list (#140).

    Each numbered task carries the project it came from (#49). A theme may
    hold tasks from two projects, so the resolver attributes the row rather
    than the heading, and this flattening is the one place that still knows
    which project a position belongs to. A copy, not a mutation — these
    dicts come straight out of the dashboard's cache.
    """
    numbered_projects = [p for p in projects if p["event_date"]]
    numbered_tasks = [
        {
            **task,
            "project_id": project["id"],
            "project_name": project.get("display_name") or project["name"],
        }
        for project in numbered_projects
        for task in project["tasks"]
    ]
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
    for p in numbered_projects:
        done_count = len([t for t in p["tasks"] if t["done"]])

        # The project name stays in the *input* even though #49 took it out
        # of the output: a theme that bundles work across projects can only
        # be found by something that knows which project a task is in. The
        # number that used to follow it is gone with project_ref.
        lines.append(f"## {'Dein Projekt' if single_project_demo else p['name']}")
        # #262: the date alone. The "(in N Tagen)" suffix that used to follow
        # it was arithmetic over two figures the prompt already carries —
        # this date and the "Heute ist der …" line above.
        lines.append(f"Termin: {p['event_date'].strftime('%d.%m.%Y')}")
        lines.append(f"Mitwirkende: {p.get('performers', '')}")
        # Stays, unlike the open count below: the render loop skips done
        # tasks, so this number cannot be read off anything else in the
        # prompt (#262 — measured, not assumed).
        lines.append(f"Erledigt: {done_count} Aufgaben")
        lines.append("Offene Aufgaben:")

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

    # #49: one output format for both modes. The two branches used to ask
    # for the same thing in two copies bar the block key — single mode for a
    # thematic "heading", multi mode for a "project_ref" — which is what
    # made the multi-project summary structurally unable to bundle work
    # across projects: one bullet per project, by instruction. Both now ask
    # for a theme, and the project a task belongs to is rendered on its own
    # row instead of in the heading.
    #
    # What still differs is the task sentence and the cross-project hint, so
    # that is all that sits outside the shared block. Continuing #262 one
    # level down: four of these lines already stood here verbatim twice.
    lines += [
        "---",
        "",
        (
            "Erstelle eine Übersicht für dieses einzelne Projekt."
            if single_project_demo
            else "Erstelle mir eine Wochenübersicht."
        ),
        "Nur Infos aus den Daten.",
        "",
        "Format:",
        '{"jetzt_faellig": [{"heading": "Programm offen", "assessment": "die Buchung muss heute raus, sonst wird der Termin knapp", "task_refs": [1, 2]}], "naechste_woche": []}',
        "",
        '- "heading": das gemeinsame Thema der Aufgaben im Block, kurz (2–3 Wörter). Nenne keine Projektnamen — die Zuordnung steht bereits auf der Seite.',
        '- "assessment": ein Satz mit Einschätzung und echtem Assistenzwert: Was ist kritisch? Was läuft gut? Nicht "Tasks offen", sondern "Plakate müssen heute raus" oder "noch gut im Zeitplan".',
        '- "task_refs": die Nummern (in eckigen Klammern bei jeder offenen Aufgabe oben) der relevantesten Aufgaben, max. 4.',
    ]
    if not single_project_demo:
        lines.append(
            "- Ein Thema darf Aufgaben aus verschiedenen Projekten zusammenfassen — genau dafür ist diese Übersicht da."
        )
        # Asked for only where the Kontext-Übersicht above was actually
        # emitted. The hint is a statement *about* that block, so requesting
        # it over data the prompt does not carry invites a sentence about
        # contexts the reader never sees — and demo mode is exactly that
        # case: no task carries a kontext (#18), the block is omitted, and
        # this instruction goes with it rather than being filtered out again
        # at render time.
        if kontext_lines:
            lines.append(
                '- "kontext_hinweis" (optional, oberste Ebene, kein Block): Wenn zwei oder mehr offene Aufgaben aus VERSCHIEDENEN Projekten denselben Kontext teilen und im selben Zeitraum liegen, nenne die Gelegenheit, sie zusammen zu erledigen — ein einziger Satz, z. B. "Wenn du ohnehin im Büro bist: GEMA-Meldung und Musikervertrag in einem Rutsch." Gibt es keine solche Häufung über Projektgrenzen hinweg, lass das Feld weg.'
            )
    lines += [
        "",
        # #49 opens with "harder to scan", and a per-project listing of five
        # projects is exactly that. A cap is the established answer here —
        # task_refs has carried "max. 4" since #122.
        "Pro Abschnitt max. 3 Themen — lieber bündeln als auflisten.",
        "",
        "Zuordnung der Blöcke:",
        '- "jetzt_faellig": überfällige und diese Woche fällige Aufgaben.',
        '- "naechste_woche": Aufgaben in den kommenden 7–14 Tagen.',
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
    # #262: the event date is stated once, in the `Zeitraum:` line, in the
    # ISO form the answer has to come back in — the opening line used to
    # repeat it as %d.%m.%Y. The example object likewise appears once: the
    # count is already given in words ("vier Objekten", the array guard from
    # PR #275), so it does not also have to be demonstrated four times.
    prompt = f"""Du planst ein Projekt: "{project_name}".

Aufgaben:
{task_lines}

Wähle 4 dramatisch interessante Momente aus dem Zeitverlauf — Wendepunkte, bei denen etwas Entscheidendes passiert oder der Status des Projekts sich spürbar verändert. Benenne jeden Moment nach dem, was inhaltlich passiert (z.B. "Buchungen starten", "Öffentlichkeitsphase", "Letzter Schliff", "Generalprobe"). Keine generischen Zeitangaben.

Format — ein JSON-Array mit vier Objekten:
[
  {{"date": "YYYY-MM-DD", "label": "2–3 Wörter", "description": "Ein Satz was gerade passiert"}}
]

Zeitraum: {today.isoformat()} bis {event_date.isoformat()}, chronologisch sortiert."""

    with log_claude_call("generate_timelapse_moments") as result:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            system=system_instruction(),
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
    blocks of {heading, assessment, task_refs} — one shape for both modes
    since #49, plus an optional top-level kontext_hinweis. The refs are
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
                system=system_instruction(),
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


def build_closeout_prompt(stats: dict, week_start: date) -> str:
    """#169: the close-out review's summary — appreciative by design, not a
    second status report. Rescheduled tasks are named as decisions, not as
    a shortfall against the week.

    #215: the two week-scoped numbers say so in their own line, and the
    reschedule line says it describes this close-out rather than the week —
    Claude echoes the framing it is given, and the previous wording invited
    it to narrate a session-scoped number as a fact about the week.
    `added_count` is None for a demo close-out (a plan created in one shot
    has nothing "new"), and the line is then left out rather than stating a
    zero the number can never leave.

    #262: takes the week, not the day. Since #263 the week being closed can
    differ from the week of the request — reproduced in the browser on
    22.09.2026, where KW 38 was closed from a day in KW 39 and Claude wrote
    "Diese Woche war eine ruhige … Woche" about KW 39. The numbers were
    always right; "Heute ist der …" was the only date the prompt carried,
    so the wording pointed at the wrong week. One date in, one week out:
    the pair cannot be made inconsistent because there is no pair.

    #262 also drops the clause justifying the neutral framing. That exact
    wording ("bewusste Planungsentscheidungen, keine verpassten Deadlines")
    came back as padding in the generated text, which is this issue's own
    finding — and "Deadlines" is on the eval's anglicism list, so the
    prompt was handing Claude the word the check then failed on.
    """
    monday, sunday = iso_week_bounds(week_start)
    _, iso_week, _ = monday.isocalendar()
    lines = [
        f"Ich schließe KW {iso_week} ab ({format_week_range(monday, sunday)}).",
        "",
        f"In dieser Woche erledigt: {stats['completed_count']} Aufgaben",
    ]
    if stats.get("added_count") is not None:
        lines.append(
            f"In dieser Woche neu dazugekommen: {stats['added_count']} Aufgaben"
        )
    lines += [
        (
            "Gerade beim Abschließen in die nächste Woche verschoben: "
            f"{stats['rescheduled_count']} Aufgaben"
        ),
        "",
        "Schreib eine kurze Rückschau auf diese Woche. Anerkennend, nicht bewertend.",
        "Verschobene Aufgaben neutral benennen, nicht als Rückstand. 2–3 Sätze.",
        "",
        "Format:",
        '{"summary_text": "..."}',
    ]
    return "\n".join(lines)


def generate_closeout_summary(stats: dict, week_start: date) -> str:
    """Returns the close-out review's German summary text.

    Same retry contract as generate_weekly_summary: one re-ask on
    unparseable or wrong-shape JSON, AIUnavailableError after the second bad
    response, SDK failures never spent as a JSON retry.
    """
    client = anthropic.Anthropic()
    prompt = build_closeout_prompt(stats, week_start)

    last_error = None
    for attempt in (1, 2):
        with (
            log_claude_call("generate_closeout_summary") as result,
            client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=system_instruction(),
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


def resolve_kontext_hint(data: dict) -> str:
    """The cross-project batch opportunity Claude may have spotted, or "".

    #145: kontext exists to batch work *across* projects, and until now
    nothing in the app did that — the prompt carried the data and never
    asked for anything to be done with it.

    Still a separate top-level field after #49, for a different reason than
    the one originally written here. That reason was that blocks are per
    project, so a statement about two of them would be attributed to one —
    and a thematic block *can* span projects, so it no longer holds. What
    does hold is the horizon: build_prompt asks for the hint over every
    open task with no date limit, while the blocks are scoped to "this
    week" and "7–14 days". A clear week is exactly when the hint can be the
    only thing the card has to say, which is why it also renders outside
    the resolved state (_ai_summary_body.html).

    Optional by design. A week with no cluster gets no hint, and so does a
    summary cached before this field existed — the same robustness rule
    resolve_weekly_summary follows, so whatever the model emitted, the
    template gets something it can render.
    """
    hint = data.get("kontext_hinweis")
    return hint if isinstance(hint, str) else ""


def _summary_task(task: dict, with_project: bool) -> dict:
    """The projection a summary task row renders from — deliberately not
    the annotated task itself, so every field a template reads has to be
    named here.

    with_project adds the attribution #49 moved out of the block heading:
    a theme may hold tasks from two projects, so the project name belongs
    on the row, in the project_id / project_name shape _task_row.html
    already renders. Off in single-project mode, where the one project is
    named in the card's own header and a label per row would repeat it as
    many times as the block has tasks.
    """
    projected = {
        "id": task["id"],
        "name": task["name"],
        "done": task["done"],
        "urgency": task.get("urgency", "ok"),
        # #211: the summary's dot renders from this dict, not from the
        # annotated task, so a field the dot reads has to be copied across
        # or the same task renders one colour in the summary and another in
        # the list below.
        "done_this_week": task.get("done_this_week", False),
        # #190: the raw date, not a formatted string — both summary
        # templates run it through plan_date, so they share one format with
        # the task rows (#189).
        "due": task.get("due"),
    }
    if with_project:
        projected["project_id"] = task["project_id"]
        projected["project_name"] = task["project_name"]
    return projected


def resolve_weekly_summary(
    data: dict, projects: list, single_project_demo: bool = False
) -> list:
    """Builds the render-ready sections from Claude's raw reference dict,
    resolved against `projects` as they are *now* — called at render time,
    never at cache-write time, so checkbox state can't go stale behind the
    summary's cache layers (#122).

    Robustness over completeness: an unresolvable task ref is dropped and
    the rest of its block stays; a block with no usable heading is dropped
    whole — there is nothing to head it with. One rule for both modes since
    #49, where the multi-project branch's heading stopped being a resolved
    project and became the same free-text theme the single-project branch
    already asked for.
    """
    _, numbered_tasks = _number_projects_and_tasks(projects)
    sections = []
    for key, title in SUMMARY_SECTIONS:
        raw_blocks = data.get(key)
        blocks = []
        for raw_block in raw_blocks if isinstance(raw_blocks, list) else []:
            if not isinstance(raw_block, dict):
                continue
            heading = raw_block.get("heading")
            if not isinstance(heading, str) or not heading.strip():
                continue
            assessment = raw_block.get("assessment")
            refs = raw_block.get("task_refs")
            blocks.append(
                {
                    "heading": heading,
                    "assessment": assessment if isinstance(assessment, str) else "",
                    "tasks": [
                        _summary_task(task, with_project=not single_project_demo)
                        for ref in (refs if isinstance(refs, list) else [])
                        if (task := _resolve_ref(ref, numbered_tasks)) is not None
                    ],
                }
            )
        sections.append({"title": title, "blocks": blocks})
    return sections


def summary_has_content(sections: list) -> bool:
    """True if any section actually carries a block.

    resolve_weekly_summary returns one entry per SUMMARY_SECTIONS whether or
    not anything resolved, so its result is never falsy — "a summary exists"
    and "a summary says something" are two different questions, and the
    templates were asking the first one while rendering the second (#214).
    """
    return any(section["blocks"] for section in sections)
