"""/mein-plan/ — the single-project view of a session plan."""

from datetime import (
    date,
    timedelta,
)

from django.urls import reverse

from ..ai import AIUnavailableError
from ..date_format import format_date
from .base import DemoModeTestCase


class MyPlanMultiViewCtaTest(DemoModeTestCase):
    """#7: my_plan only ever renders with a session plan present, so its
    "Mehrprojekt-Dashboard ansehen" CTA landing on plain {% url 'dashboard' %}
    always bounced back into the single-project view instead of the example
    projects it promises."""

    def test_the_cta_links_to_the_multi_project_view(self):
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, f'href="{reverse("dashboard")}?mode=multi"')


class MyPlanAiFailureTest(DemoModeTestCase):
    def test_my_plan_degrades_without_a_summary(self):
        self.given_session_plan()
        self.ai_mocks[
            "projects.views.generate_weekly_summary"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.get(reverse("my_plan"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nicht verfügbar")


class MyPlanEventDateDisplayTest(DemoModeTestCase):
    """my_plan() built its own task dict inline instead of reusing
    _build_session_project (#9) — this pins the one field that builder
    doesn't set, so the refactor to reuse it can't quietly drop it."""

    def test_my_plan_still_renders_the_formatted_event_date(self):
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, format_date(date.today() + timedelta(days=30)))


class MyPlanProgressBarTest(DemoModeTestCase):
    """§4 of #10: the bar rendered `width: {{ done_count }}00%` — the done count
    times 100 rather than a percentage — so it was full from the first completed
    task on. updateProgress() computes it correctly but only runs after a toggle,
    so on page load the bar was never right."""

    def given_plan_with(self, total, done):
        return self.given_session_plan(
            tasks=[
                {
                    "id": f"demo-session-{i}",
                    "name": f"Aufgabe {i}",
                    "date": (date.today() + timedelta(days=i + 1)).isoformat(),
                    "kontext": "Planung",
                    "done": i < done,
                }
                for i in range(total)
            ]
        )

    def test_shows_the_correct_percentage_on_load(self):
        self.given_plan_with(total=4, done=2)
        self.assertContains(self.client.get(reverse("my_plan")), "width: 50%")

    def test_does_not_multiply_the_done_count_by_hundred(self):
        self.given_plan_with(total=3, done=3)
        response = self.client.get(reverse("my_plan"))
        self.assertNotContains(response, "width: 300%")
        self.assertContains(response, "width: 100%")

    def test_nothing_done_is_zero_percent(self):
        self.given_plan_with(total=4, done=0)
        self.assertContains(self.client.get(reverse("my_plan")), "width: 0%")


class MyPlanDoneCounterTest(DemoModeTestCase):
    """#151: the "x / y erledigt" counter was server-rendered only, so after a
    toggle the progress bar moved while the counter kept its load-time value
    until the next reload. updateProgress() now rewrites the counter alongside
    the bar, which requires the count to sit in an addressable element."""

    def given_plan_with(self, total, done):
        return self.given_session_plan(
            tasks=[
                {
                    "id": f"demo-session-{i}",
                    "name": f"Aufgabe {i}",
                    "date": (date.today() + timedelta(days=i + 1)).isoformat(),
                    "kontext": "Planung",
                    "done": i < done,
                }
                for i in range(total)
            ]
        )

    def test_done_count_renders_inside_an_addressable_element(self):
        self.given_plan_with(total=4, done=2)
        self.assertContains(
            self.client.get(reverse("my_plan")),
            '<span id="done-count">2</span> / 4 erledigt',
        )

    def test_update_progress_rewrites_the_counter(self):
        self.given_plan_with(total=4, done=1)
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "getElementById('done-count')")
