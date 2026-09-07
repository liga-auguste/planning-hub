"""The AI weekly summary: prompt, parsing, resolution against live projects
and the caches in front of it."""

import json
from datetime import (
    date,
    timedelta,
)
from unittest.mock import patch

import anthropic
import httpx
from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import reverse

from ..ai import (
    AIUnavailableError,
    _number_projects_and_tasks,
    build_prompt,
    generate_timelapse_moments,
    generate_weekly_summary,
    log_claude_call,
    resolve_weekly_summary,
)
from ..views import (
    DEMO_MULTI_SUMMARY_KEY,
    SUMMARY_KEY,
)
from .base import (
    DemoModeTestCase,
    _anthropic_timeout_error,
    _fake_response,
    _fake_stream,
    _summary_data,
)


class SummarySessionCacheTest(DemoModeTestCase):
    """Proves the views actually write the current versioned key — without
    this, a key bump could leave every view writing a dead key and the
    assertNotIn tests above would pass vacuously."""

    def test_a_successful_summary_is_cached_under_the_current_key(self):
        self.given_session_plan()
        self.client.get(reverse("dashboard"))
        self.assertIn(f"{SUMMARY_KEY}_today", self.client.session)

    def test_my_plan_reads_and_writes_the_same_key(self):
        self.given_session_plan()
        self.client.get(reverse("my_plan"))
        self.assertIn(f"{SUMMARY_KEY}_today", self.client.session)


class MultiViewSummaryCacheTest(DemoModeTestCase):
    """#51: the multi-project view called Claude on every single GET — the path
    the landing-page CTA sends every first-time visitor to. Its input is a pure
    function of date.today() with no per-visitor data, so one call per day
    serves everyone."""

    @property
    def summary_mock(self):
        return self.ai_mocks["projects.views.generate_weekly_summary"]

    def test_claude_is_called_once_for_repeated_visits(self):
        self.client.get(reverse("dashboard") + "?mode=multi")
        self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertEqual(self.summary_mock.call_count, 1)

    def test_the_second_visit_still_renders_the_summary(self):
        """ "Called once" must not be bought with a blank AI card."""
        self.summary_mock.return_value = _summary_data("Alles im Plan")
        self.client.get(reverse("dashboard") + "?mode=multi")
        second = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(second, "Alles im Plan")

    def test_the_summary_is_cached_under_the_current_key(self):
        """Without this, a key bump would leave the tests above vacuously green."""
        self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertIsNotNone(
            cache.get(f"{DEMO_MULTI_SUMMARY_KEY}_{date.today().isoformat()}")
        )

    def test_a_failure_is_not_cached(self):
        self.summary_mock.side_effect = AIUnavailableError("boom")
        first = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(first, "nicht verfügbar")
        self.summary_mock.side_effect = None
        self.summary_mock.return_value = _summary_data("Alles im Plan")
        second = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(second, "Alles im Plan")

    def test_the_single_plan_view_does_not_use_the_multi_cache(self):
        """The visitor's own plan is per-session data and stays in the session."""
        self.given_session_plan()
        self.client.get(reverse("dashboard"))
        self.assertIsNone(
            cache.get(f"{DEMO_MULTI_SUMMARY_KEY}_{date.today().isoformat()}")
        )
        self.assertIn(f"{SUMMARY_KEY}_today", self.client.session)


class BuildPromptKontextUebersichtTest(SimpleTestCase):
    """kontext is production-only (#18): a demo prompt carries no per-project
    kontext, so the cross-project overview it feeds has nothing to group —
    the heading itself must then not appear, rather than render empty."""

    def test_single_project_demo_with_no_kontext_omits_the_heading(self):
        project = {
            "name": "Sommerkonzert",
            "event_date": date.today() + timedelta(days=10),
            "performers": "",
            "tasks": [
                {
                    "name": "Programm festlegen",
                    "done": False,
                    "due": None,
                    "kontext": [],
                }
            ],
        }
        prompt = build_prompt([project], date.today(), single_project_demo=True)
        self.assertNotIn("Kontext-Übersicht", prompt)

    def test_production_prompt_with_kontext_keeps_the_heading(self):
        project = {
            "name": "Sommerkonzert",
            "event_date": date.today() + timedelta(days=10),
            "performers": "",
            "tasks": [
                {
                    "name": "GEMA-Meldung",
                    "done": False,
                    "due": None,
                    "kontext": ["Büro"],
                }
            ],
        }
        prompt = build_prompt([project], date.today())
        self.assertIn("Kontext-Übersicht", prompt)
        self.assertIn("**Büro:** GEMA-Meldung", prompt)


class PromptUndatedAndTodayTest(SimpleTestCase):
    """#160: the weekly-summary prompt describes undated open tasks as
    "ohne Termin" (previously the nonsensical "fällig in ? Tagen") and flags
    due-today tasks as "HEUTE fällig"."""

    TODAY = date(2026, 9, 1)

    def project_with_task(self, **task):
        return {
            "id": "p-solo",
            "name": "Konzert Solo",
            "event_date": self.TODAY + timedelta(days=5),
            "performers": "",
            "tasks": [
                {
                    "id": "t-x",
                    "name": "Aufgabe X",
                    "due": None,
                    "done": False,
                    "kontext": [],
                    **task,
                }
            ],
        }

    def test_an_undated_task_reads_ohne_termin(self):
        prompt = build_prompt([self.project_with_task(due=None)], self.TODAY)
        self.assertIn("Aufgabe X — ohne Termin", prompt)
        self.assertNotIn("fällig in ? Tagen", prompt)

    def test_a_task_due_today_reads_heute_faellig(self):
        prompt = build_prompt([self.project_with_task(due=self.TODAY)], self.TODAY)
        self.assertIn("Aufgabe X — HEUTE fällig", prompt)

    def test_a_task_due_this_week_keeps_diese_woche(self):
        prompt = build_prompt(
            [self.project_with_task(due=self.TODAY + timedelta(days=3))], self.TODAY
        )
        self.assertIn("Aufgabe X — DIESE WOCHE", prompt)


class BuildPromptCalendarWeekLabelTest(SimpleTestCase):
    """#169: build_prompt's "DIESE WOCHE" label follows the same calendar-week
    rule as _annotate_tasks now, not a rolling 7-day window — but an overdue
    task's label must stay exactly as it was (see the edge case called out
    in the issue's implementation plan)."""

    # A Tuesday — ISO week 36 runs through the following Sunday.
    TODAY = date(2026, 9, 1)

    def project_with_task(self, **task):
        return {
            "id": "p-solo",
            "name": "Konzert Solo",
            "event_date": self.TODAY + timedelta(days=5),
            "performers": "",
            "tasks": [
                {
                    "id": "t-x",
                    "name": "Aufgabe X",
                    "due": None,
                    "done": False,
                    "kontext": [],
                    **task,
                }
            ],
        }

    def label_for(self, due):
        prompt = build_prompt([self.project_with_task(due=due)], self.TODAY)
        line = next(line for line in prompt.splitlines() if "Aufgabe X" in line)
        return line

    def test_due_the_last_day_of_this_iso_week_is_diese_woche(self):
        self.assertIn("DIESE WOCHE", self.label_for(self.TODAY + timedelta(days=5)))

    def test_due_the_first_day_of_next_iso_week_is_days_remaining(self):
        line = self.label_for(self.TODAY + timedelta(days=6))
        self.assertIn("(fällig in 6 Tagen)", line)
        self.assertNotIn("DIESE WOCHE", line)

    def test_overdue_from_a_past_calendar_week_still_reads_diese_woche(self):
        # Naively swapping in is_same_iso_week here would send an overdue
        # task from a past week into the days-remaining branch instead —
        # the exact regression the issue's plan calls out.
        line = self.label_for(self.TODAY - timedelta(days=5))
        self.assertIn("DIESE WOCHE", line)
        self.assertNotIn("fällig in -5 Tagen", line)


def _anthropic_rate_limit_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    return anthropic.RateLimitError("rate limited", response=response, body=None)


class AnthropicFailureTranslationTest(SimpleTestCase):
    """ai.py's two Claude calls translate SDK failures into one app-level
    exception, after the SDK's own retries (max_retries=2 by default) are
    exhausted. Views only need to catch AIUnavailableError, never an
    anthropic.* type directly."""

    def test_weekly_summary_translates_a_timeout(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.stream.side_effect = (
                _anthropic_timeout_error()
            )
            with self.assertRaises(AIUnavailableError):
                generate_weekly_summary([], date.today())

    def test_weekly_summary_translates_a_rate_limit(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.stream.side_effect = (
                _anthropic_rate_limit_error()
            )
            with self.assertRaises(AIUnavailableError):
                generate_weekly_summary([], date.today())

    def test_timelapse_moments_translates_a_failure(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.side_effect = (
                _anthropic_timeout_error()
            )
            with self.assertRaises(AIUnavailableError):
                generate_timelapse_moments("Test", date.today(), [])

    def test_original_exception_is_preserved_as_the_cause(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            timeout = _anthropic_timeout_error()
            MockAnthropic.return_value.messages.stream.side_effect = timeout
            with self.assertRaises(AIUnavailableError) as ctx:
                generate_weekly_summary([], date.today())
        self.assertIs(ctx.exception.__cause__, timeout)


class LogClaudeCallTest(SimpleTestCase):
    """log_claude_call (#31) wraps a Claude call site: exception translation
    (#29) plus structured duration/usage logging on success."""

    def test_logs_model_duration_and_tokens_on_success(self):
        with (
            self.assertLogs("projects.ai", level="INFO") as cm,
            log_claude_call("some_call") as result,
        ):
            result["message"] = _fake_response(
                "hi", model="claude-sonnet-4-6", input_tokens=123, output_tokens=45
            )
        [record] = cm.output
        self.assertIn("call=some_call", record)
        self.assertIn("model=claude-sonnet-4-6", record)
        self.assertIn("input_tokens=123", record)
        self.assertIn("output_tokens=45", record)
        self.assertIn("outcome=success", record)

    def test_logs_outcome_error_on_anthropic_failure(self):
        with (
            self.assertLogs("projects.ai", level="WARNING") as cm,
            self.assertRaises(AIUnavailableError),
            log_claude_call("some_call"),
        ):
            raise _anthropic_timeout_error()
        [record] = cm.output
        self.assertIn("call=some_call", record)
        self.assertIn("outcome=error", record)

    def test_success_path_does_not_also_log_a_warning(self):
        with (
            self.assertLogs("projects.ai", level="INFO") as cm,
            log_claude_call("some_call") as result,
        ):
            result["message"] = _fake_response("hi")
        self.assertEqual(len(cm.output), 1)


VALID_SUMMARY_JSON = '{"jetzt_faellig": [], "naechste_woche": []}'


class WeeklySummaryLoggingTest(SimpleTestCase):
    def test_logs_usage_on_success(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.stream.return_value = _fake_stream(
                VALID_SUMMARY_JSON, input_tokens=200, output_tokens=80
            )
            with self.assertLogs("projects.ai", level="INFO") as cm:
                data = generate_weekly_summary([], date.today())
        self.assertEqual(data, {"jetzt_faellig": [], "naechste_woche": []})
        [record] = cm.output
        self.assertIn("call=generate_weekly_summary", record)
        self.assertIn("input_tokens=200", record)
        self.assertIn("output_tokens=80", record)
        self.assertIn("outcome=success", record)


def _summary_projects(today=None):
    """Two projects, four tasks; the second task is already done. The
    numbering the prompt and the resolver share counts every task, done ones
    included: 1=Programm, 2=Ensemble (done), 3=Plakate, 4=Technik."""
    today = today or date(2026, 9, 1)
    return [
        {
            "id": "p-alpha",
            "name": "Konzert Alpha",
            "event_date": today + timedelta(days=5),
            "performers": "",
            "tasks": [
                {
                    "id": "t-programm",
                    "name": "Programm festlegen",
                    "due": today + timedelta(days=1),
                    "done": False,
                    "kontext": [],
                    "urgency": "urgent",
                },
                {
                    "id": "t-ensemble",
                    "name": "Ensemble anfragen",
                    "due": today - timedelta(days=10),
                    "done": True,
                    "kontext": [],
                    "urgency": "done",
                },
            ],
        },
        {
            "id": "p-beta",
            "name": "Konzert Beta",
            "event_date": today + timedelta(days=30),
            "performers": "",
            "tasks": [
                {
                    "id": "t-plakate",
                    "name": "Plakate drucken",
                    "due": today + timedelta(days=10),
                    "done": False,
                    "kontext": [],
                    "urgency": "ok",
                },
                {
                    "id": "t-technik",
                    "name": "Technik prüfen",
                    "due": today + timedelta(days=12),
                    "done": False,
                    "kontext": [],
                    "urgency": "ok",
                },
            ],
        },
    ]


class NumberingAndPromptTest(SimpleTestCase):
    """#122: build_prompt and resolve_weekly_summary share one numbering,
    produced by _number_projects_and_tasks. Every task occupies a number,
    done ones included — numbering only open tasks would shift every later
    ref the moment a task is toggled between cache-write and render."""

    def test_numbering_counts_every_task_across_projects(self):
        numbered_projects, numbered_tasks = _number_projects_and_tasks(
            _summary_projects()
        )
        self.assertEqual([p["id"] for p in numbered_projects], ["p-alpha", "p-beta"])
        self.assertEqual(
            [t["id"] for t in numbered_tasks],
            ["t-programm", "t-ensemble", "t-plakate", "t-technik"],
        )

    def test_a_project_without_event_date_is_skipped_like_in_the_prompt(self):
        projects = _summary_projects()
        projects[0]["event_date"] = None
        numbered_projects, numbered_tasks = _number_projects_and_tasks(projects)
        self.assertEqual([p["id"] for p in numbered_projects], ["p-beta"])
        self.assertEqual([t["id"] for t in numbered_tasks], ["t-plakate", "t-technik"])

    def test_prompt_numbers_open_tasks_with_their_global_position(self):
        prompt = build_prompt(_summary_projects(), date(2026, 9, 1))
        self.assertIn("[1] Programm festlegen", prompt)
        # Position 2 belongs to the done task, which is never listed — the
        # gap is deliberate, it keeps the numbering stable across toggles.
        self.assertIn("[3] Plakate drucken", prompt)
        self.assertIn("[4] Technik prüfen", prompt)

    def test_done_tasks_are_not_listed_in_the_prompt(self):
        prompt = build_prompt(_summary_projects(), date(2026, 9, 1))
        self.assertNotIn("Ensemble anfragen", prompt)

    def test_multi_mode_states_each_projects_ref(self):
        prompt = build_prompt(_summary_projects(), date(2026, 9, 1))
        self.assertIn("Projekt-Nr.: 1", prompt)
        self.assertIn("Projekt-Nr.: 2", prompt)
        self.assertIn('"project_ref"', prompt)

    def test_single_project_demo_mode_has_no_project_refs(self):
        prompt = build_prompt(
            _summary_projects()[:1], date(2026, 9, 1), single_project_demo=True
        )
        self.assertNotIn("Projekt-Nr.", prompt)
        self.assertNotIn('"project_ref"', prompt)
        self.assertIn('"heading"', prompt)

    def test_prompt_asks_for_json_only(self):
        prompt = build_prompt(_summary_projects(), date(2026, 9, 1))
        self.assertIn("NUR mit JSON", prompt)
        self.assertIn('"jetzt_faellig"', prompt)
        self.assertIn('"naechste_woche"', prompt)


class ResolveWeeklySummaryTest(SimpleTestCase):
    """#122: resolve_weekly_summary turns Claude's raw reference dict into
    render-ready sections against *live* projects — the raw dict is what
    every cache layer stores, so done-state must come from projects at
    render time, never from the cached artifact."""

    def resolve(self, data, projects=None, **kwargs):
        return resolve_weekly_summary(
            data, projects if projects is not None else _summary_projects(), **kwargs
        )

    def test_valid_refs_resolve_to_projects_and_tasks(self):
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {
                        "project_ref": 1,
                        "assessment": "Programm ist der Engpass",
                        "task_refs": [1],
                    }
                ],
                "naechste_woche": [
                    {
                        "project_ref": 2,
                        "assessment": "noch gut im Zeitplan",
                        "task_refs": [3, 4],
                    }
                ],
            }
        )
        self.assertEqual(sections[0]["title"], "Jetzt fällig")
        self.assertEqual(sections[1]["title"], "Nächste Woche")
        [block] = sections[0]["blocks"]
        self.assertEqual(block["project_id"], "p-alpha")
        self.assertEqual(block["project_name"], "Konzert Alpha")
        self.assertEqual(block["assessment"], "Programm ist der Engpass")
        self.assertEqual([t["id"] for t in block["tasks"]], ["t-programm"])
        [block2] = sections[1]["blocks"]
        self.assertEqual([t["id"] for t in block2["tasks"]], ["t-plakate", "t-technik"])

    def test_the_projection_carries_the_tasks_due_date(self):
        # #190: the summary listed a name and a status dot but no date,
        # which is exactly the moment a date is worth most — a task the
        # summary calls urgent says nothing about when it is actually due.
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [1]}
                ],
                "naechste_woche": [],
            }
        )
        [task] = sections[0]["blocks"][0]["tasks"]
        self.assertEqual(task["due"], date(2026, 9, 2))

    def test_the_projection_carries_the_raw_date_not_a_formatted_one(self):
        # #189: a formatted string here would reintroduce the same mistake
        # on a second projection — the templates format at render time.
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [1]}
                ],
                "naechste_woche": [],
            }
        )
        [task] = sections[0]["blocks"][0]["tasks"]
        self.assertNotIn("due_display", task)

    def test_a_task_without_a_date_projects_none_rather_than_dropping_out(self):
        # An undated task still belongs in the summary; only its date is
        # missing, and the template hides the empty span for that case.
        projects = _summary_projects()
        projects[0]["tasks"][0]["due"] = None
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [1]}
                ],
                "naechste_woche": [],
            },
            projects=projects,
        )
        [task] = sections[0]["blocks"][0]["tasks"]
        self.assertEqual(task["id"], "t-programm")
        self.assertIsNone(task["due"])

    def test_an_invalid_task_ref_is_dropped_and_the_rest_survive(self):
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [1, 99, 3]}
                ],
                "naechste_woche": [],
            }
        )
        [block] = sections[0]["blocks"]
        self.assertEqual([t["id"] for t in block["tasks"]], ["t-programm", "t-plakate"])

    def test_an_invalid_project_ref_drops_the_whole_block(self):
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 99, "assessment": "x", "task_refs": [1]},
                    {"project_ref": 2, "assessment": "y", "task_refs": []},
                ],
                "naechste_woche": [],
            }
        )
        self.assertEqual(len(sections[0]["blocks"]), 1)
        self.assertEqual(sections[0]["blocks"][0]["project_id"], "p-beta")

    def test_a_bool_ref_does_not_resolve_as_an_integer(self):
        # True is an int subclass — without the explicit check it would
        # resolve as ref 1 and silently attach the wrong task.
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [True]}
                ],
                "naechste_woche": [],
            }
        )
        self.assertEqual(sections[0]["blocks"][0]["tasks"], [])

    def test_single_project_demo_uses_the_free_text_heading(self):
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"heading": "Jetzt kritisch", "assessment": "x", "task_refs": [1]}
                ],
                "naechste_woche": [],
            },
            single_project_demo=True,
        )
        [block] = sections[0]["blocks"]
        self.assertEqual(block["heading"], "Jetzt kritisch")
        self.assertNotIn("project_id", block)

    def test_single_project_demo_drops_a_block_without_heading(self):
        sections = self.resolve(
            {
                "jetzt_faellig": [{"assessment": "x", "task_refs": [1]}],
                "naechste_woche": [],
            },
            single_project_demo=True,
        )
        self.assertEqual(sections[0]["blocks"], [])

    def test_a_done_task_resolves_as_done_regardless_of_the_cached_refs(self):
        # The core regression case: the raw dict was cached while the task
        # was open; by render time it is done in projects — the checkbox
        # must render done.
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [2]}
                ],
                "naechste_woche": [],
            }
        )
        [task] = sections[0]["blocks"][0]["tasks"]
        self.assertEqual(task["id"], "t-ensemble")
        self.assertTrue(task["done"])

    def test_garbage_blocks_and_missing_keys_are_tolerated(self):
        sections = self.resolve(
            {
                "jetzt_faellig": ["kein dict", 42, {"project_ref": 1}],
                "naechste_woche": "gar keine Liste",
            }
        )
        [block] = sections[0]["blocks"]
        self.assertEqual(block["assessment"], "")
        self.assertEqual(block["tasks"], [])
        self.assertEqual(sections[1]["blocks"], [])


class GenerateWeeklySummaryRetryTest(SimpleTestCase):
    """#122: generate_weekly_summary parses Claude's response as JSON with
    the same retry contract as generate_plan (GeneratePlanRetryTest): one
    re-ask on unparseable or wrong-shape JSON, AIUnavailableError after the
    second bad response, SDK failures never spent as a JSON retry."""

    def generate(self):
        return generate_weekly_summary(_summary_projects(), date(2026, 9, 1))

    def test_returns_parsed_dict_on_first_valid_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.return_value = _fake_stream(VALID_SUMMARY_JSON)
            data = self.generate()
        self.assertEqual(data, {"jetzt_faellig": [], "naechste_woche": []})
        self.assertEqual(stream.call_count, 1)

    def test_retries_once_on_invalid_json_then_succeeds(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream("kein json"),
                _fake_stream(VALID_SUMMARY_JSON),
            ]
            data = self.generate()
        self.assertEqual(data, {"jetzt_faellig": [], "naechste_woche": []})
        self.assertEqual(stream.call_count, 2)

    def test_raises_ai_unavailable_after_a_second_invalid_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream("kein json"),
                _fake_stream("immer noch kein json"),
            ]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 2)

    def test_valid_json_in_the_wrong_shape_is_retried(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream('["nur", "eine", "liste"]'),
                _fake_stream('{"jetzt_faellig": []}'),
            ]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 2)

    def test_an_sdk_failure_is_not_retried_as_a_json_error(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = _anthropic_timeout_error()
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 1)

    def test_fenced_response_is_still_parsed(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.return_value = _fake_stream(f"```json\n{VALID_SUMMARY_JSON}\n```")
            data = self.generate()
        self.assertEqual(data, {"jetzt_faellig": [], "naechste_woche": []})


class AiSummaryCheckboxViewTest(DemoModeTestCase):
    """#122 end to end: the rendered summary carries real inline checkboxes
    wired to the existing toggle endpoints, and their done state is read
    from live data at render time, not from the cached Claude response."""

    def summary_stub(self):
        return self.ai_mocks["projects.views.generate_weekly_summary"]

    def single_project_summary(self, task_refs):
        return {
            "jetzt_faellig": [
                {
                    "heading": "Jetzt kritisch",
                    "assessment": "Programm zuerst",
                    "task_refs": task_refs,
                }
            ],
            "naechste_woche": [],
        }

    def test_dashboard_summary_renders_a_checkbox_for_a_referenced_task(self):
        self.given_session_plan()
        self.summary_stub().return_value = self.single_project_summary([1])
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Jetzt kritisch")
        self.assertContains(response, "Programm zuerst")
        # Once in the AI card, once in the project section's task list —
        # both are the same .toggle-form markup on the same endpoint.
        self.assertContains(
            response,
            'class="toggle-form" data-task-id="demo-session-0"',
            count=2,
        )

    def test_dashboard_checkbox_state_follows_a_toggle_not_the_cache(self):
        self.given_session_plan()
        self.summary_stub().return_value = self.single_project_summary([1])
        self.client.get(reverse("dashboard"))  # caches the raw refs in the session
        self.client.post(
            reverse("toggle_task", args=["demo-session-0"]),
            json.dumps({"done": True}),
            content_type="application/json",
        )
        response = self.client.get(reverse("dashboard"))
        # The cached refs were written while the task was open; the rendered
        # checkbox must still show the live done state — in the AI card and
        # the task list alike.
        self.assertContains(
            response,
            'data-task-id="demo-session-0" data-done="true"',
            count=2,
        )
        self.summary_stub().assert_called_once()

    def test_an_unresolvable_ref_does_not_break_the_page(self):
        self.given_session_plan()
        self.summary_stub().return_value = self.single_project_summary([99])
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Jetzt kritisch")

    def test_multi_project_summary_links_the_project_by_id(self):
        # Demo multi mode (no session plan): headings come from project_ref,
        # resolved server-side — no PROJECT_MAP substring matching anywhere.
        self.summary_stub().return_value = {
            "jetzt_faellig": [
                {"project_ref": 1, "assessment": "läuft", "task_refs": []}
            ],
            "naechste_woche": [],
        }
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "showProject('demo-1')")
        self.assertContains(response, 'class="ai-project-link"')
        self.assertNotContains(response, "PROJECT_MAP")

    def test_my_plan_summary_renders_the_same_live_checkbox(self):
        plan = self.given_session_plan()
        plan["tasks"][0]["done"] = True
        session = self.client.session
        session["demo_plan"] = plan
        # Raw refs cached while the task was still open — done must come
        # from the live session plan.
        session[f"{SUMMARY_KEY}_today"] = self.single_project_summary([1])
        session.save()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "Jetzt kritisch")
        # The summary checkbox renders the live done state (the task list
        # row formats its attributes across lines, so this single-line
        # pattern matches the summary markup).
        self.assertContains(response, 'data-task-id="demo-session-0" data-done="true"')
