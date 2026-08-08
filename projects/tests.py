from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

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
