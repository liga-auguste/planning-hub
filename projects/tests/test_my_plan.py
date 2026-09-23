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

    def test_an_unavailable_summary_is_not_an_empty_one(self):
        """#214: "Claude could not answer" and "Claude answered nothing"
        are different states and must not share a wording."""
        self.given_session_plan()
        self.ai_mocks[
            "projects.views.generate_weekly_summary"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.get(reverse("my_plan"))
        self.assertNotContains(response, "Diese Woche steht nichts an.")
        self.assertNotContains(response, "Die nächste Aufgabe ist am")


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


class MyPlanEmptySummaryTest(DemoModeTestCase):
    """#214: a summary that resolved to nothing left the
    "KI-Wochenübersicht" label standing over a gap. It now says so in
    words. The default AI stub in base.py already returns an empty summary,
    so these need no mock of their own.

    Each sentence is guarded by live data rather than by Claude's silence:
    an empty answer while something is due this week is a model error, and
    "Diese Woche steht nichts an." next to a task due today would be the
    same bug one volume louder.
    """

    def given_plan_with_task(self, *, days_out=120, done=False):
        due = date.today() + timedelta(days=days_out)
        return self.given_session_plan(
            event_date=(date.today() + timedelta(days=days_out + 7)).isoformat(),
            tasks=[
                {
                    "id": "demo-session-0",
                    "name": "Programm festlegen",
                    "date": due.isoformat(),
                    "done": done,
                }
            ],
        )

    def test_a_plan_months_away_says_both_sentences(self):
        self.given_plan_with_task()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "Diese Woche steht nichts an.")
        self.assertContains(
            response,
            f"Die nächste Aufgabe ist am "
            f"{format_date(date.today() + timedelta(days=120), role='note')}.",
        )

    def test_nothing_open_left_drops_the_date_sentence(self):
        self.given_plan_with_task(done=True)
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "Diese Woche steht nichts an.")
        self.assertNotContains(response, "Die nächste Aufgabe ist am")

    def test_an_overdue_task_gets_its_own_sentence(self):
        # "Die nächste Aufgabe ist am <past date>." named a date already
        # gone as the next one — the failure this note exists to prevent,
        # told backwards. Overdue work is its own sentence now.
        self.given_plan_with_task(days_out=-30)
        response = self.client.get(reverse("my_plan"))
        self.assertContains(
            response,
            f"Überfällig seit dem "
            f"{format_date(date.today() - timedelta(days=30), role='note')}.",
        )
        self.assertNotContains(response, "Die nächste Aufgabe ist am")
        self.assertNotContains(response, "Diese Woche steht nichts an.")

    def test_something_due_today_drops_the_clear_week_sentence(self):
        self.given_plan_with_task(days_out=0)
        response = self.client.get(reverse("my_plan"))
        self.assertNotContains(response, "Diese Woche steht nichts an.")
        self.assertContains(
            response,
            f"Die nächste Aufgabe ist am {format_date(date.today(), role='note')}.",
        )

    def test_the_note_keeps_the_label_and_the_summary_box(self):
        self.given_plan_with_task()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "KI-Wochenübersicht")
        self.assertContains(response, '<div class="summary-box ai-error">')

    def test_a_summary_with_blocks_renders_no_note(self):
        self.given_plan_with_task()
        self.ai_mocks["projects.views.generate_weekly_summary"].return_value = {
            "jetzt_faellig": [
                {
                    "heading": "Testkonzert",
                    "assessment": "Programm ist der Engpass",
                    "task_refs": [],
                }
            ],
            "naechste_woche": [],
        }
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "Programm ist der Engpass")
        self.assertNotContains(response, "Diese Woche steht nichts an.")
        self.assertNotContains(response, "Die nächste Aufgabe ist am")


class MyPlanOffersTheDatePickerTest(DemoModeTestCase):
    """#186: this page listed dates and offered no way to change one. Both
    runs of tasks on it — the summary and "Alle Aufgaben" — render the
    shared partial now and bind the shared picker (#266)."""

    def test_both_runs_of_tasks_render_the_shared_control(self):
        self.given_session_plan()
        self.ai_mocks["projects.views.generate_weekly_summary"].return_value = {
            "jetzt_faellig": [
                {"heading": "Testkonzert", "assessment": "x", "task_refs": [1]}
            ],
            "naechste_woche": [],
        }
        html = self.client.get(reverse("my_plan")).content.decode()
        split = html.index('<div class="task-list">')
        summary = html[html.index('<div class="summary-box">') : split]
        self.assertIn('<button type="button" class="task-due', summary)
        self.assertIn('<button type="button" class="task-due', html[split:])

    def test_the_local_task_date_class_is_gone(self):
        # It was the drift #195 named by name: this page spelled the date
        # .task-date where every other surface spelled it .task-due, which is
        # also what kept the shared selector from ever reaching it.
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertNotContains(response, 'class="task-date')

    def test_a_successful_move_reloads_rather_than_patching(self):
        # Unlike the dashboard: the list is chronological (#140) and nothing
        # here re-sorts it, and the postpone badge, the progress bar and the
        # summary's prose are all server-rendered. The session-cached summary
        # is remapped rather than regenerated on a reschedule, so the reload
        # costs no Claude call.
        self.given_session_plan()
        html = self.client.get(reverse("my_plan")).content.decode()
        binding = html[html.index("bindTaskDatePickers(") :]
        self.assertIn("`/task/${taskId}/reschedule/`", binding)
        self.assertIn("window.location.reload();", binding)

    def test_a_failed_move_is_flashed_rather_than_swallowed(self):
        self.given_session_plan()
        html = self.client.get(reverse("my_plan")).content.decode()
        self.assertIn("flashActionFailed(dueEl);", html)
