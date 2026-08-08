import unittest
from datetime import date, timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .ai import derive_kontext
from .planner_views import _parse_event_date
from .views import _annotate_tasks, _fix_ai_markdown

# The view modules import the AI functions with `from .ai import ...`, so the
# name to patch is the one bound in the view module, not the one in projects.ai.
AI_STUBS = {
    'projects.views.generate_weekly_summary': '**Test summary**',
    'projects.planner_views.get_clarifying_questions': '**Wie viele Mitwirkende?**',
    'projects.planner_views.generate_plan': '{"project_name": "Testkonzert", "tasks": []}',
    'projects.planner_views.generate_timelapse_moments': [],
}


@override_settings(DEMO_MODE=True)
class DemoModeTestCase(TestCase):
    """Stubs the Claude API — no test may make a real call."""

    def setUp(self):
        self.ai_mocks = {}
        for target, return_value in AI_STUBS.items():
            patcher = patch(target, return_value=return_value)
            self.ai_mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)

    def given_session_plan(self, **overrides):
        """Creates a session plan the way planner_create produces it."""
        plan = {
            'name': 'Testkonzert',
            'event_date': (date.today() + timedelta(days=30)).isoformat(),
            'tasks': [
                {
                    'id': 'demo-session-0',
                    'name': 'Programm festlegen',
                    'date': (date.today() + timedelta(days=7)).isoformat(),
                    'kontext': 'Planung',
                    'done': False,
                },
            ],
        }
        plan.update(overrides)
        session = self.client.session
        session['demo_plan'] = plan
        session.save()
        return plan


class DashboardKanbanCssTest(DemoModeTestCase):
    def test_kanban_meta_has_gap(self):
        response = self.client.get('/dashboard/')
        self.assertContains(response, 'gap: 6px')

    def test_kanban_meta_first_child_selector_exists(self):
        response = self.client.get('/dashboard/')
        self.assertContains(response, '.kanban-card-meta span:first-child')

    def test_kanban_meta_last_child_selector_exists(self):
        response = self.client.get('/dashboard/')
        self.assertContains(response, '.kanban-card-meta span:last-child')


class AiStubTest(DemoModeTestCase):
    """Guards the guard: proves the stubs are actually in the request path."""

    def test_dashboard_does_not_call_the_real_api(self):
        self.client.get('/dashboard/')
        self.ai_mocks['projects.views.generate_weekly_summary'].assert_called()


class PlannerGetFallthroughTest(DemoModeTestCase):
    """Both views used to fall through to an implicit `return None` on GET."""

    def test_review_get_redirects_to_start(self):
        response = self.client.get(reverse('planner_review'))
        self.assertRedirects(response, reverse('planner_start'))

    def test_create_get_redirects_to_start(self):
        response = self.client.get(reverse('planner_create'))
        self.assertRedirects(response, reverse('planner_start'))

    def test_questions_route_is_gone(self):
        # Literal path: the URL name no longer exists, so reverse() cannot be used.
        self.assertEqual(self.client.get('/planner/questions/').status_code, 404)


class TimelapseValidationTest(DemoModeTestCase):
    """An unvalidated string in demo_sim_date used to break every later request."""

    def post_date(self, body):
        return self.client.post(
            reverse('set_timelapse_date'), data=body, content_type='application/json'
        )

    def test_invalid_date_is_rejected(self):
        response = self.post_date('{"date": "kaputt"}')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('demo_sim_date', self.client.session)

    def test_malformed_json_is_rejected(self):
        response = self.post_date('{')
        self.assertEqual(response.status_code, 400)

    def test_valid_date_is_stored(self):
        sim_date = (date.today() + timedelta(days=5)).isoformat()
        response = self.post_date(f'{{"date": "{sim_date}"}}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session['demo_sim_date'], sim_date)

    def test_empty_date_clears_the_session(self):
        self.post_date(f'{{"date": "{date.today().isoformat()}"}}')
        response = self.post_date('{"date": null}')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('demo_sim_date', self.client.session)

    @override_settings(DEMO_MODE=False)
    def test_unavailable_outside_demo_mode(self):
        self.assertEqual(self.post_date('{"date": null}').status_code, 404)

    def test_preload_rejects_invalid_date(self):
        response = self.client.post(
            reverse('preload_timelapse_summary'),
            data='{"date": "kaputt"}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_preload_rejects_malformed_json(self):
        response = self.client.post(
            reverse('preload_timelapse_summary'),
            data='{',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)


class PoisonedSessionHealingTest(DemoModeTestCase):
    """Sessions poisoned before the validation landed must recover on their own."""

    def poison(self):
        session = self.client.session
        session['demo_sim_date'] = 'kaputt'
        session.save()

    def test_dashboard_renders_and_clears_the_bad_value(self):
        self.given_session_plan()
        self.poison()
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('demo_sim_date', self.client.session)

    def test_dashboard_renders_without_a_session_plan(self):
        self.poison()
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)


# --- Unit tests for the logic that is not a view ---

class DeriveKontextTest(SimpleTestCase):
    def test_keyword_match_returns_its_kontext(self):
        self.assertEqual(derive_kontext('GEMA-Meldung'), ['Büro'])

    def test_match_is_case_insensitive_and_partial(self):
        self.assertEqual(derive_kontext('Heute noch das Programm festlegen'), ['Planung'])

    def test_no_match_returns_empty(self):
        self.assertEqual(derive_kontext('Irgendetwas Unbekanntes'), [])

    def test_returns_a_list_not_a_string(self):
        """_annotate_tasks writes this into a field that otherwise holds a string. See #9."""
        self.assertIsInstance(derive_kontext('GEMA-Meldung'), list)


class ParseEventDateTest(SimpleTestCase):
    def test_explicit_year_is_used(self):
        self.assertEqual(
            _parse_event_date('Konzert am 5. September 2026'), date(2026, 9, 5)
        )

    def test_without_year_returns_the_next_occurrence(self):
        result = _parse_event_date('Konzert am 5. September')
        self.assertEqual((result.month, result.day), (9, 5))
        self.assertGreater(result, date.today())
        self.assertLessEqual((result - date.today()).days, 366)

    def test_impossible_day_returns_none(self):
        self.assertIsNone(_parse_event_date('Konzert am 31. Februar 2026'))

    def test_unknown_month_returns_none(self):
        self.assertIsNone(_parse_event_date('Konzert am 5. Smarch 2026'))

    def test_no_date_returns_none(self):
        self.assertIsNone(_parse_event_date('Konzert irgendwann im Herbst'))


class AnnotateTasksTest(SimpleTestCase):
    TODAY = date(2026, 6, 15)

    def annotate(self, *tasks):
        project = {'tasks': [
            {'name': 'Aufgabe', 'kontext': 'Büro', 'done': False, 'due': None, **t}
            for t in tasks
        ]}
        return _annotate_tasks([project], self.TODAY)[0]

    def urgency_for(self, **task):
        return self.annotate(task)['tasks'][0]['urgency']

    def test_done_task_is_done(self):
        self.assertEqual(self.urgency_for(done=True, due=self.TODAY - timedelta(days=1)), 'done')

    def test_task_without_due_date_is_done(self):
        self.assertEqual(self.urgency_for(due=None), 'done')

    def test_past_due_is_overdue(self):
        self.assertEqual(self.urgency_for(due=self.TODAY - timedelta(days=1)), 'overdue')

    def test_today_is_urgent(self):
        self.assertEqual(self.urgency_for(due=self.TODAY), 'urgent')

    def test_seven_days_out_is_still_urgent(self):
        self.assertEqual(self.urgency_for(due=self.TODAY + timedelta(days=7)), 'urgent')

    def test_eight_days_out_is_ok(self):
        self.assertEqual(self.urgency_for(due=self.TODAY + timedelta(days=8)), 'ok')

    def test_overdue_beats_urgent_on_the_project(self):
        project = self.annotate(
            {'due': self.TODAY + timedelta(days=2)},
            {'due': self.TODAY - timedelta(days=2)},
        )
        self.assertEqual(project['urgency'], 'overdue')

    def test_project_without_open_work_stays_ok(self):
        project = self.annotate({'due': self.TODAY + timedelta(days=30)})
        self.assertEqual(project['urgency'], 'ok')

    def test_due_display_is_formatted_german(self):
        task = self.annotate({'due': date(2026, 6, 15)})['tasks'][0]
        self.assertEqual(task['due_display'], 'Mo, 15. Juni')

    def test_empty_kontext_is_derived_from_the_name(self):
        task = self.annotate({'name': 'GEMA-Meldung', 'kontext': ''})['tasks'][0]
        self.assertEqual(task['kontext'], ['Büro'])


class FixAiMarkdownTest(SimpleTestCase):
    """Claude returns task lines under a project bullet without list markers."""

    def test_continuation_lines_become_sub_bullets(self):
        result = _fix_ai_markdown('- **Konzert**\nPlakate aushängen')
        self.assertEqual(result, '- **Konzert**\n    - Plakate aushängen')

    def test_blank_lines_inside_a_block_are_dropped(self):
        result = _fix_ai_markdown('- **Konzert**\n\nPlakate aushängen')
        self.assertEqual(result, '- **Konzert**\n    - Plakate aushängen')

    def test_existing_list_markers_are_left_alone(self):
        result = _fix_ai_markdown('- **Konzert**\n- Plakate aushängen')
        self.assertEqual(result, '- **Konzert**\n- Plakate aushängen')

    def test_horizontal_rule_ends_the_block(self):
        result = _fix_ai_markdown('- **Konzert**\n---\nFreier Text')
        self.assertEqual(result, '- **Konzert**\n---\nFreier Text')

    def test_bold_line_ends_the_block(self):
        result = _fix_ai_markdown('- **Konzert**\n**Hinweis**\nFreier Text')
        self.assertEqual(result, '- **Konzert**\n**Hinweis**\nFreier Text')

    def test_text_outside_a_block_is_untouched(self):
        self.assertEqual(_fix_ai_markdown('Nur ein Satz.'), 'Nur ein Satz.')

    @unittest.expectedFailure
    def test_section_header_keeps_its_blank_line(self):
        """A '##' header after a project block loses the blank line that makes it a
        header, so Markdown renders it as list content and the summary shows an
        unexplained gap. '#' also fails to reset in_project, so anything following
        the header is indented as a sub-bullet. See #20.
        """
        result = _fix_ai_markdown('- **Konzert**\n\n## Jetzt fällig\n\nPlakate aushängen')
        self.assertIn('\n\n## Jetzt fällig', result)
