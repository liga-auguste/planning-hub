"""Shared fixtures for the test package.

Not collected: the module name does not match unittest's `test*.py`
pattern, which is what lets the base classes below be imported into every
module without being counted once per import."""

from datetime import (
    date,
    timedelta,
)
from unittest.mock import (
    MagicMock,
    Mock,
    patch,
)

import anthropic
import httpx
from django.core.cache import cache
from django.test import (
    TestCase,
    override_settings,
)
from django.urls import reverse

# The view modules import the AI functions with `from .ai import ...`, so the
# name to patch is the one bound in the view module, not the one in projects.ai.
AI_STUBS = {
    # generate_weekly_summary returns the raw reference dict since #122 —
    # the stub has to match that shape, not the markdown string Claude used
    # to hand back (same evolution as the generate_plan stub below).
    "projects.views.generate_weekly_summary": {
        "jetzt_faellig": [],
        "naechste_woche": [],
    },
    "projects.planner_views.get_clarifying_questions": "**Wie viele Mitwirkende?**",
    # generate_plan now parses its own response and returns a dict (see #29 /
    # GeneratePlanRetryTest) — this stub has to match that shape, not the raw
    # JSON string Claude used to hand back.
    "projects.planner_views.generate_plan": {
        "project_name": "Testkonzert",
        "tasks": [],
    },
    "projects.planner_views.generate_timelapse_moments": [],
    "projects.views.generate_closeout_summary": "Gute Woche gewesen.",
}


def _summary_data(marker="Zusammenfassung läuft"):
    """A minimal raw reference dict (#122) whose assessment carries a
    recognisable marker; project_ref 1 resolves against whatever project the
    test's get_upcoming_projects stub returns first (and the block is simply
    dropped when there is none)."""
    return {
        "jetzt_faellig": [{"project_ref": 1, "assessment": marker, "task_refs": []}],
        "naechste_woche": [],
    }


class AiStubMixin:
    """Stubs the Claude API — no test may make a real call.

    Mixed into a TestCase rather than owned by DemoModeTestCase, because a
    production-mode class needs the same guarantee without DEMO_MODE=True.
    Getting it from the class instead of from a patch inside each test is the
    whole point: two close-out tests once reached the real API because they
    simply left the patch out (#215), and that is invisible on a machine
    whose .env carries a key — only CI, which has none, failed. A guarantee
    every test has to remember is not one.
    """

    def setUp(self):
        super().setUp()
        self.ai_mocks = {}
        for target, return_value in AI_STUBS.items():
            patcher = patch(target, return_value=return_value)
            self.ai_mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)


@override_settings(DEMO_MODE=True)
class DemoModeTestCase(AiStubMixin, TestCase):
    """The demo-mode half of the same guarantee."""

    def setUp(self):
        # The demo caches live in the test database, shared across the whole
        # run, so an entry a previous test left behind would make a later
        # test skip the Claude call it asserts on — see AiStubTest.
        cache.clear()
        self.addCleanup(cache.clear)
        super().setUp()

    def given_session_plan(self, **overrides):
        """Creates a session plan the way planner_create produces it."""
        plan = {
            "name": "Testkonzert",
            "event_date": (date.today() + timedelta(days=30)).isoformat(),
            "tasks": [
                {
                    "id": "demo-session-0",
                    "name": "Programm festlegen",
                    "date": (date.today() + timedelta(days=7)).isoformat(),
                    "done": False,
                },
            ],
        }
        plan.update(overrides)
        session = self.client.session
        session["demo_plan"] = plan
        session.save()
        return plan

    def given_timelapse_moments(self, *dates):
        """Stores moments the way planner_create does. Only these dates are postable."""
        session = self.client.session
        session["demo_timelapse_moments"] = [
            {"date": d, "label": "Moment", "description": "Beschreibung"} for d in dates
        ]
        session.save()


class PlannerStepsMixin:
    """Fetches the three planner steps that carry a submit button."""

    def steps(self):
        return {
            "start": self.client.get(reverse("planner_start") + "?type=eigenes"),
            "questions": self.client.post(
                reverse("planner_start"),
                data={"description": "Konzert am 15. September 2026"},
            ),
            "review": self.client.post(
                reverse("planner_review"),
                data={
                    "description": "Konzert am 5. September 2026",
                    "answers": "keine weiteren Angaben",
                },
            ),
        }


def _anthropic_timeout_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APITimeoutError(request=request)


def _fake_response(text, model="claude-sonnet-4-6", input_tokens=100, output_tokens=50):
    return Mock(
        content=[Mock(text=text)],
        model=model,
        usage=Mock(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _fake_stream(text, input_tokens=100, output_tokens=50):
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.get_final_text.return_value = text
    stream.get_final_message.return_value = _fake_response(
        text, input_tokens=input_tokens, output_tokens=output_tokens
    )
    return stream


def _fake_upcoming_project(name="Testkonzert"):
    return {
        "id": "p1",
        "name": name,
        "event_date": date.today() + timedelta(days=10),
        "performers": "",
        "status": None,
        "status_color": "gray",
        "tasks": [],
    }


def _fake_upcoming_project_with_task():
    """A project whose tasks actually render — the empty task list above never
    reaches the per-task markup."""
    project = _fake_upcoming_project()
    project["tasks"] = [
        {
            "id": "task-1",
            "name": "Programm festlegen",
            "due": date.today() + timedelta(days=3),
            "done": False,
            "kontext": [],
        }
    ]
    return project


def _closeout_tasks(fixed_today):
    return [
        {
            "id": "t-this-week",
            "name": "Diese Woche",
            "date": (fixed_today + timedelta(days=2)).isoformat(),
            "done": False,
        },
        {
            "id": "t-next-week",
            "name": "Nächste Woche",
            "date": (fixed_today + timedelta(days=9)).isoformat(),
            "done": False,
        },
        {
            "id": "t-overdue",
            "name": "Überfällig",
            "date": (fixed_today - timedelta(days=1)).isoformat(),
            "done": False,
        },
        {
            "id": "t-done",
            "name": "Schon erledigt",
            "date": (fixed_today + timedelta(days=1)).isoformat(),
            "done": True,
        },
    ]


# A Monday — matches AnnotateTasksTest/IsSameIsoWeekTest's reference date.
CLOSEOUT_TODAY = date(2026, 6, 15)
