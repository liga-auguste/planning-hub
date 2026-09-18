"""Language-consistency eval for the six spots where Claude writes German
prose (see the internal touchpoint note). Calls the real Anthropic API by
design — deliberately kept out of the test suite, which treats a real API
call as a bug (#215). Run via `manage.py eval_language`.

Formal checks are regex heuristics: built to over-report rather than miss a
real violation, so a PASS here is not a guarantee, only the absence of a
known red flag. The judge call is itself a Claude request and is not
perfectly consistent between two runs — a signal to read, not a hard gate.
"""

import json
import re
from dataclasses import dataclass
from datetime import timedelta

import anthropic
from django.utils import timezone

from .ai import (
    AIUnavailableError,
    generate_closeout_summary,
    generate_timelapse_moments,
    generate_weekly_summary,
    log_claude_call,
    resolve_weekly_summary,
)
from .demo_data import get_demo_history, get_demo_projects
from .planner import generate_plan, get_clarifying_questions
from .rules import INITIAL_RULES

GERMAN_MONTHS = (
    "Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
)

# Targets the characteristic formal-address tokens instead of bare "Sie" —
# German capitalizes the first word of a sentence regardless of case, so a
# sentence starting with "Sie" (as in "they") would otherwise false-positive.
SIE_FORM_PATTERN = re.compile(
    r"\bIhre[nmr]?\b|\bIhnen\b|\bSie (können|müssen|sollten|haben|sind|hast)\b"
)
LEADING_ZERO_DATE_PATTERN = re.compile(rf"\b0[1-9]\.\s?(?:{GERMAN_MONTHS})\b")
EMOJI_PATTERN = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]"
)
# Starter list — extend as new Denglisch creeps in.
ANGLICISMS = ["task", "tasks", "feedback", "content", "deadline", "call", "workflow"]

EVENT_DESCRIPTION = (
    "Adventskonzert mit dem Kammerchor, Kirche St. Marien, Mitte Dezember. "
    "Solistin für zwei Stücke dazu."
)
ANSWERS = (
    "1. 14. Dezember 2026\n"
    "2. Solistin ist schon gebucht (Sopranistin)\n"
    "3. Programm steht noch nicht fest, muss erarbeitet werden\n"
    "4. Ja, Solistin bekommt Honorar, Chor ist ehrenamtlich"
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class JudgeResult:
    clear_friendly_short: bool | None
    focus_named: bool | None
    order_suggested: bool | None
    bundling_named: bool | None
    reasoning: str


@dataclass
class CaseResult:
    key: str
    title: str
    texts: list
    checks: list
    judge: JudgeResult | None
    error: str | None = None


def check_du_form(texts: list) -> CheckResult:
    for text in texts:
        match = SIE_FORM_PATTERN.search(text)
        if match:
            return CheckResult(
                "Du-Form", False, f'found "{match.group()}" in: "{text}"'
            )
    return CheckResult("Du-Form", True)


def check_date_format(texts: list) -> CheckResult:
    for text in texts:
        match = LEADING_ZERO_DATE_PATTERN.search(text)
        if match:
            return CheckResult(
                "Date format", False, f'found "{match.group()}" in: "{text}"'
            )
    return CheckResult("Date format", True)


def check_no_emoji(texts: list) -> CheckResult:
    for text in texts:
        match = EMOJI_PATTERN.search(text)
        if match:
            return CheckResult(
                "No emoji", False, f'found "{match.group()}" in: "{text}"'
            )
    return CheckResult("No emoji", True)


def _sentence_count(text: str) -> int:
    # A period right after a digit is a German ordinal date ("17. September"),
    # not a sentence end — the lookbehind keeps those from splitting the text.
    return len([s for s in re.split(r"(?<!\d)[.!?]+\s+", text.strip()) if s])


def check_sentence_count(texts: list, max_sentences: int) -> CheckResult:
    for text in texts:
        count = _sentence_count(text)
        if count > max_sentences:
            return CheckResult(
                "Length", False, f'{count} sentences (max {max_sentences}) in: "{text}"'
            )
    return CheckResult("Length", True)


def check_question_count(text: str, max_questions: int) -> CheckResult:
    count = len(re.findall(r"^\s*\d+[.)]\s", text, re.MULTILINE))
    if count > max_questions:
        return CheckResult(
            "Question count", False, f"{count} questions (max {max_questions})"
        )
    return CheckResult("Question count", True)


def check_no_anglicisms(texts: list) -> CheckResult:
    for text in texts:
        lowered = text.lower()
        for word in ANGLICISMS:
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                return CheckResult("Anglicisms", False, f'found "{word}" in: "{text}"')
    return CheckResult("Anglicisms", True)


def _check_no_project_name_repeat(blocks: list) -> CheckResult:
    for block in blocks:
        name = block.get("project_name", "")
        assessment = block.get("assessment", "")
        if name and name.lower() in assessment.lower():
            return CheckResult(
                "No repeated project name", False, f'"{name}" found in: "{assessment}"'
            )
    return CheckResult("No repeated project name", True)


JUDGE_STRUCTURE_INSTRUCTIONS = (
    "Also judge whether the text follows this three-part pattern: naming a "
    "focus, suggesting an order to tackle things in, and pointing out a "
    "bundling opportunity. Set focus_named, order_suggested and "
    "bundling_named to true or false accordingly."
)
JUDGE_NO_STRUCTURE_INSTRUCTIONS = (
    "Do not require any particular structure — this kind of text has no "
    "natural order or bundling to name. Set focus_named, order_suggested "
    "and bundling_named to null."
)


def judge_voice(texts: list, strict_structure: bool) -> JudgeResult:
    """One extra Claude call scoring the qualitative "voice" bar: clear,
    friendly, short. Only checked strictly for the weekly-overview
    touchpoints, where a focus/order/bundling pattern actually applies."""
    combined = "\n".join(f"- {t}" for t in texts)
    structure_instructions = (
        JUDGE_STRUCTURE_INSTRUCTIONS
        if strict_structure
        else JUDGE_NO_STRUCTURE_INSTRUCTIONS
    )
    prompt = f"""You are reviewing German text an AI assistant generated inside a project-planning app. The voice bar is: clear, friendly, short.

Text under review:
{combined}

{structure_instructions}

Respond with JSON only, no other text:
{{"clear_friendly_short": true/false, "focus_named": true/false/null, "order_suggested": true/false/null, "bundling_named": true/false/null, "reasoning": "one sentence, in English"}}"""

    client = anthropic.Anthropic()
    with log_claude_call("eval_judge") as result:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        result["message"] = response
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    data = json.loads(raw)
    return JudgeResult(
        clear_friendly_short=data.get("clear_friendly_short"),
        focus_named=data.get("focus_named"),
        order_suggested=data.get("order_suggested"),
        bundling_named=data.get("bundling_named"),
        reasoning=data.get("reasoning", ""),
    )


def _safe_judge(texts: list, strict_structure: bool) -> JudgeResult | None:
    if not texts:
        return None
    try:
        return judge_voice(texts, strict_structure)
    except (AIUnavailableError, json.JSONDecodeError) as exc:
        return JudgeResult(None, None, None, None, f"Judge call failed: {exc}")


def _eval_a():
    today = timezone.localdate()
    projects = get_demo_projects()
    data = generate_weekly_summary(projects, today, single_project_demo=False)
    sections = resolve_weekly_summary(data, projects, single_project_demo=False)
    blocks = [block for section in sections for block in section["blocks"]]
    texts = [block["assessment"] for block in blocks]
    checks = [
        check_du_form(texts),
        check_date_format(texts),
        check_no_emoji(texts),
        check_sentence_count(texts, max_sentences=1),
        check_no_anglicisms(texts),
        _check_no_project_name_repeat(blocks),
    ]
    return texts, checks


def _eval_b():
    today = timezone.localdate()
    projects = get_demo_projects()[:1]
    data = generate_weekly_summary(projects, today, single_project_demo=True)
    sections = resolve_weekly_summary(data, projects, single_project_demo=True)
    blocks = [block for section in sections for block in section["blocks"]]
    texts = [block["assessment"] for block in blocks]
    checks = [
        check_du_form(texts),
        check_date_format(texts),
        check_no_emoji(texts),
        check_sentence_count(texts, max_sentences=1),
        check_no_anglicisms(texts),
    ]
    return texts, checks


def _eval_c():
    today = timezone.localdate()
    stats = {"completed_count": 7, "added_count": 3, "rescheduled_count": 2}
    summary_text = generate_closeout_summary(stats, today)
    texts = [summary_text]
    checks = [
        check_du_form(texts),
        check_no_emoji(texts),
        check_sentence_count(texts, max_sentences=3),
        check_no_anglicisms(texts),
    ]
    return texts, checks


def _eval_d():
    today = timezone.localdate()
    project_name = "Reformationskonzert"
    event_date = today + timedelta(days=90)
    tasks = [
        {"name": "Programm-Entwurf", "date": (today + timedelta(days=7)).isoformat()},
        {
            "name": "Probentermine festlegen",
            "date": (today + timedelta(days=14)).isoformat(),
        },
        {
            "name": "Graphiker beauftragen",
            "date": (today + timedelta(days=35)).isoformat(),
        },
        {
            "name": "Pressetext verfassen",
            "date": (today + timedelta(days=50)).isoformat(),
        },
        {"name": "Plakate drucken", "date": (today + timedelta(days=60)).isoformat()},
        {
            "name": "Generalprobe koordinieren",
            "date": (today + timedelta(days=80)).isoformat(),
        },
    ]
    moments = generate_timelapse_moments(project_name, event_date, tasks)
    labels = [m["label"] for m in moments if isinstance(m.get("label"), str)]
    descriptions = [
        m["description"] for m in moments if isinstance(m.get("description"), str)
    ]
    texts = labels + descriptions
    checks = [
        check_du_form(texts),
        check_no_emoji(texts),
        check_sentence_count(descriptions, max_sentences=1),
        check_no_anglicisms(texts),
    ]
    return texts, checks


def _eval_e():
    history = get_demo_history()
    rules = [r["text"] for r in INITIAL_RULES]
    questions_text = get_clarifying_questions(EVENT_DESCRIPTION, history, rules=rules)
    texts = [questions_text]
    checks = [
        check_du_form(texts),
        check_no_emoji(texts),
        check_question_count(questions_text, max_questions=4),
        check_no_anglicisms(texts),
    ]
    return texts, checks


def _eval_f():
    history = get_demo_history()
    rules = [r["text"] for r in INITIAL_RULES]
    plan = generate_plan(EVENT_DESCRIPTION, ANSWERS, history, rules=rules)
    task_names = [
        t["name"]
        for t in plan.get("tasks", [])
        if isinstance(t, dict) and isinstance(t.get("name"), str)
    ]
    texts = [plan.get("project_name", "")] + task_names
    checks = [
        check_du_form(texts),
        check_no_emoji(texts),
        check_no_anglicisms(texts),
    ]
    return texts, checks


# key -> (title, runner, strict voice structure)
CASE_RUNNERS = {
    "a": ("Wochenübersicht — Mehrprojekt", _eval_a, True),
    "b": ("Wochenübersicht — Einzelprojekt", _eval_b, True),
    "c": ("Wochenabschluss-Rückschau", _eval_c, False),
    "d": ("Zeitreise-Momente", _eval_d, False),
    "e": ("Rückfragen zum neuen Projekt", _eval_e, False),
    "f": ("Aufgabenplan erstellen", _eval_f, False),
}


def run_eval(only: list | None = None) -> list:
    keys = only if only else list(CASE_RUNNERS.keys())
    results = []
    for key in keys:
        title, runner, strict_structure = CASE_RUNNERS[key]
        try:
            texts, checks = runner()
        except AIUnavailableError as exc:
            results.append(CaseResult(key, title, [], [], None, error=str(exc)))
            continue
        judge = _safe_judge(texts, strict_structure)
        results.append(CaseResult(key, title, texts, checks, judge))
    return results


def _yn(value):
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def format_report(results: list) -> str:
    lines = []
    for result in results:
        lines.append(f"=== ({result.key}) {result.title} ===")
        if result.error:
            lines.append(f"[ERROR] API call failed: {result.error}")
            lines.append("")
            continue
        for check in result.checks:
            status = "PASS" if check.passed else "FAIL"
            detail = f" — {check.detail}" if check.detail else ""
            lines.append(f"[{status}] {check.name}{detail}")
        lines.append("--- Voice (judge) ---")
        if result.judge is None:
            lines.append("not evaluated (no text)")
        else:
            lines.append(
                f"Clear/friendly/short: {_yn(result.judge.clear_friendly_short)}"
            )
            if result.judge.focus_named is not None:
                lines.append(
                    f"Focus named: {_yn(result.judge.focus_named)} | "
                    f"Order suggested: {_yn(result.judge.order_suggested)} | "
                    f"Bundling named: {_yn(result.judge.bundling_named)}"
                )
            lines.append(f'Reasoning: "{result.judge.reasoning}"')
        lines.append("")
    return "\n".join(lines)
