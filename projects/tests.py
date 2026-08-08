from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

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
