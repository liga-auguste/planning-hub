import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import anthropic
import httpx
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from unittest.mock import Mock

from .ai import AIUnavailableError, derive_kontext, generate_timelapse_moments, generate_weekly_summary, _valid_moments
from .planner import generate_plan, get_clarifying_questions
from .planner_views import _parse_event_date
from .startup import require_api_keys, MissingAPIKeyError
from .views import _annotate_tasks, _fix_ai_markdown

# The view modules import the AI functions with `from .ai import ...`, so the
# name to patch is the one bound in the view module, not the one in projects.ai.
AI_STUBS = {
    'projects.views.generate_weekly_summary': '**Test summary**',
    'projects.planner_views.get_clarifying_questions': '**Wie viele Mitwirkende?**',
    # generate_plan now parses its own response and returns a dict (see #29 /
    # GeneratePlanRetryTest) — this stub has to match that shape, not the raw
    # JSON string Claude used to hand back.
    'projects.planner_views.generate_plan': {"project_name": "Testkonzert", "tasks": []},
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

    def given_timelapse_moments(self, *dates):
        """Stores moments the way planner_create does. Only these dates are postable."""
        session = self.client.session
        session['demo_timelapse_moments'] = [
            {'date': d, 'label': 'Moment', 'description': 'Beschreibung'} for d in dates
        ]
        session.save()


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


class PlannerReviewHappyPathTest(DemoModeTestCase):
    """First test to exercise planner_review's POST path at all. generate_plan
    now returns a dict directly (see GeneratePlanRetryTest in planner.py) —
    this guards against a stub/view mismatch regressing that silently."""

    def test_post_renders_the_generated_plan(self):
        response = self.client.post(reverse('planner_review'), data={
            'description': 'Konzert am 5. September 2026',
            'answers': 'keine weiteren Angaben',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Testkonzert')


class PlannerStartAiFailureTest(DemoModeTestCase):
    """get_clarifying_questions used to be entirely unguarded — a Claude
    failure here 500'd before the visitor ever saw the questions step."""

    def test_shows_a_german_error_and_keeps_the_description(self):
        self.ai_mocks['projects.planner_views.get_clarifying_questions'].side_effect = AIUnavailableError('boom')
        response = self.client.post(reverse('planner_start'), data={
            'description': 'Konzert am 5. September',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'projects/planner_start.html')
        self.assertContains(response, 'Konzert am 5. September')
        self.assertContains(response, 'nicht erstellt werden')


class PlannerReviewAiFailureTest(DemoModeTestCase):
    """generate_plan's retry-once-then-raise (see GeneratePlanRetryTest in
    planner.py) still has to land somewhere other than a 500 — this is that
    landing, and it's the one case in the whole table where the visitor has
    already typed two rounds of input (description, then answers)."""

    def test_shows_a_german_error_and_keeps_description_and_answers(self):
        self.ai_mocks['projects.planner_views.generate_plan'].side_effect = AIUnavailableError('boom')
        response = self.client.post(reverse('planner_review'), data={
            'description': 'Konzert am 5. September',
            'answers': '20 Gäste, in der Kirche',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'projects/planner_questions.html')
        self.assertContains(response, 'Konzert am 5. September')
        self.assertContains(response, '20 Gäste, in der Kirche')
        self.assertContains(response, 'nicht erstellt werden')


class DashboardAiFailureTest(DemoModeTestCase):
    """generate_weekly_summary is called from four different places in
    views.py (dashboard x2, my_plan, preload) — none of them guarded before
    #29. The dashboard must still show projects/tasks even when the AI card
    can't."""

    def test_multi_project_dashboard_degrades_without_a_summary(self):
        self.ai_mocks['projects.views.generate_weekly_summary'].side_effect = AIUnavailableError('boom')
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'nicht verfügbar')

    def test_session_plan_dashboard_degrades_without_a_summary(self):
        self.given_session_plan()
        self.ai_mocks['projects.views.generate_weekly_summary'].side_effect = AIUnavailableError('boom')
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'nicht verfügbar')

    def test_a_failure_is_not_cached_as_a_summary(self):
        """A later, healthy request must retry rather than replay a blank."""
        self.given_session_plan()
        self.ai_mocks['projects.views.generate_weekly_summary'].side_effect = AIUnavailableError('boom')
        self.client.get(reverse('dashboard'))
        self.assertNotIn('demo_plan_summary_v3_today', self.client.session)


class MyPlanAiFailureTest(DemoModeTestCase):
    def test_my_plan_degrades_without_a_summary(self):
        self.given_session_plan()
        self.ai_mocks['projects.views.generate_weekly_summary'].side_effect = AIUnavailableError('boom')
        response = self.client.get(reverse('my_plan'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'nicht verfügbar')


class PreloadAiFailureTest(DemoModeTestCase):
    def test_preload_reports_ok_false_and_writes_nothing_to_the_session(self):
        self.given_session_plan()
        moment = (date.today() + timedelta(days=5)).isoformat()
        self.given_timelapse_moments(moment)
        self.ai_mocks['projects.views.generate_weekly_summary'].side_effect = AIUnavailableError('boom')
        response = self.client.post(
            reverse('preload_timelapse_summary'),
            data=f'{{"date": "{moment}"}}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': False})
        self.assertNotIn(f'demo_plan_summary_v3_{moment}', self.client.session)


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
        self.given_timelapse_moments(sim_date)
        response = self.post_date(f'{{"date": "{sim_date}"}}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session['demo_sim_date'], sim_date)

    def test_empty_date_clears_the_session(self):
        today = date.today().isoformat()
        self.given_timelapse_moments(today)
        self.post_date(f'{{"date": "{today}"}}')
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


class SimDateIsRestrictedToGeneratedMomentsTest(DemoModeTestCase):
    """A parseable date is not enough. Every accepted date costs a Claude call, so
    only the moments planner_create generated for this session may be posted."""

    def post_date(self, url_name, raw):
        return self.client.post(
            reverse(url_name), data=f'{{"date": "{raw}"}}', content_type='application/json'
        )

    def test_well_formed_date_outside_the_moments_is_rejected(self):
        self.given_timelapse_moments((date.today() + timedelta(days=5)).isoformat())
        other = (date.today() + timedelta(days=6)).isoformat()
        self.assertEqual(self.post_date('set_timelapse_date', other).status_code, 400)
        self.assertNotIn('demo_sim_date', self.client.session)

    def test_far_future_date_is_rejected(self):
        self.given_timelapse_moments(date.today().isoformat())
        self.assertEqual(self.post_date('set_timelapse_date', '9999-12-31').status_code, 400)

    def test_no_moments_means_no_date_is_accepted(self):
        sim_date = (date.today() + timedelta(days=5)).isoformat()
        self.assertEqual(self.post_date('set_timelapse_date', sim_date).status_code, 400)

    def test_preload_spends_no_api_call_on_an_unlisted_date(self):
        self.given_session_plan()
        self.given_timelapse_moments(date.today().isoformat())
        unlisted = (date.today() + timedelta(days=99)).isoformat()
        response = self.post_date('preload_timelapse_summary', unlisted)
        self.assertEqual(response.status_code, 400)
        self.ai_mocks['projects.views.generate_weekly_summary'].assert_not_called()

    def test_preload_accepts_a_listed_date(self):
        moment = (date.today() + timedelta(days=5)).isoformat()
        self.given_session_plan()
        self.given_timelapse_moments(moment)
        response = self.post_date('preload_timelapse_summary', moment)
        self.assertEqual(response.status_code, 200)
        self.ai_mocks['projects.views.generate_weekly_summary'].assert_called()

    def test_replanning_invalidates_the_old_moments(self):
        old = (date.today() + timedelta(days=5)).isoformat()
        self.given_timelapse_moments(old)
        self.given_timelapse_moments((date.today() + timedelta(days=9)).isoformat())
        self.assertEqual(self.post_date('set_timelapse_date', old).status_code, 400)


class UnparseableMomentDateTest(DemoModeTestCase):
    """Being on the allowlist is not enough — the moments come from Claude, so a
    session written before they were validated can list a date nothing can parse."""

    def post_date(self, url_name, raw):
        return self.client.post(
            reverse(url_name), data=f'{{"date": "{raw}"}}', content_type='application/json'
        )

    def test_wrong_format_is_not_written_to_the_session(self):
        self.given_timelapse_moments('05.09.2026')
        self.assertEqual(self.post_date('set_timelapse_date', '05.09.2026').status_code, 400)
        self.assertNotIn('demo_sim_date', self.client.session)

    def test_impossible_day_does_not_reach_fromisoformat(self):
        self.given_session_plan()
        self.given_timelapse_moments('2026-02-30')
        self.assertEqual(self.post_date('preload_timelapse_summary', '2026-02-30').status_code, 400)


class MalformedPayloadTest(DemoModeTestCase):
    """Valid JSON of the wrong shape must be a 400, not an unhandled exception.
    Both endpoints are unauthenticated on the public demo."""

    URL_NAMES = ('set_timelapse_date', 'preload_timelapse_summary')

    def post_body(self, url_name, body):
        return self.client.post(
            reverse(url_name), data=body, content_type='application/json'
        )

    def assert_rejects(self, body):
        for url_name in self.URL_NAMES:
            with self.subTest(url=url_name, body=body):
                self.assertEqual(self.post_body(url_name, body).status_code, 400)

    def test_unhashable_date_is_rejected(self):
        # `raw not in <set>` hashes the value first, so a list used to raise TypeError.
        self.assert_rejects('{"date": ["2026-09-05"]}')
        self.assert_rejects('{"date": {"a": 1}}')

    def test_non_string_date_is_rejected(self):
        self.assert_rejects('{"date": 20260905}')

    def test_body_that_is_not_an_object_is_rejected(self):
        for body in ('null', '[]', '"2026-09-05"', '5'):
            self.assert_rejects(body)

    def test_unhashable_moment_in_the_session_does_not_break_the_allowlist(self):
        session = self.client.session
        session['demo_timelapse_moments'] = [{'date': ['2026-09-05']}, {'date': None}]
        session.save()
        self.assert_rejects('{"date": "2026-09-05"}')


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


class ValidMomentsTest(SimpleTestCase):
    """generate_timelapse_moments returns raw model JSON. These dates become an
    allowlist and are parsed back later, so they cannot be taken on trust."""

    def test_well_formed_moments_survive_untouched(self):
        moments = [{'date': '2026-09-05', 'label': 'Probe', 'description': 'Text'}]
        self.assertEqual(_valid_moments(moments), moments)

    def test_unparseable_date_is_dropped(self):
        self.assertEqual(_valid_moments([{'date': '05.09.2026', 'label': 'Probe'}]), [])

    def test_impossible_day_is_dropped(self):
        self.assertEqual(_valid_moments([{'date': '2026-02-30'}]), [])

    def test_non_string_date_is_dropped(self):
        self.assertEqual(_valid_moments([{'date': None}, {'date': ['2026-09-05']}]), [])

    def test_moment_without_a_date_is_dropped(self):
        self.assertEqual(_valid_moments([{'label': 'Probe'}, 'nonsense']), [])

    def test_parseable_date_is_normalised(self):
        """date.fromisoformat also takes the basic and week forms, which the dashboard
        JS cannot — it builds `date + 'T12:00:00'`. Rewrite them rather than drop them."""
        self.assertEqual(
            _valid_moments([{'date': '20260905', 'label': 'Probe'}]),
            [{'date': '2026-09-05', 'label': 'Probe'}],
        )
        self.assertEqual(_valid_moments([{'date': '2026-W36-6'}]), [{'date': '2026-09-05'}])

    def test_datetime_string_is_dropped(self):
        self.assertEqual(_valid_moments([{'date': '2026-09-05T10:00:00'}]), [])

    def test_good_moments_are_kept_when_a_sibling_is_dropped(self):
        result = _valid_moments([{'date': 'kaputt'}, {'date': '2026-09-05'}])
        self.assertEqual(result, [{'date': '2026-09-05'}])

    def test_a_non_list_response_yields_no_moments(self):
        self.assertEqual(_valid_moments({'date': '2026-09-05'}), [])
        self.assertEqual(_valid_moments(None), [])


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


# --- #29: fail at startup, not at first request ---

class RequiredApiKeysTest(SimpleTestCase):
    """wsgi.py calls require_api_keys() before serving a single request. This is
    deliberately not a Django system check: `manage.py test` runs the full check
    registry with no tag filtering (DiscoverRunner.run_checks -> call_command
    ("check", ...)), which would break the offline proof in README.md ("env -u
    ANTHROPIC_API_KEY python manage.py test projects"). wsgi.py is only imported
    by runserver and gunicorn — the processes that actually serve traffic.
    """

    def test_missing_anthropic_key_raises(self):
        with patch.dict(os.environ, {'NOTION_API_KEY': 'x'}, clear=True):
            with self.assertRaises(MissingAPIKeyError) as ctx:
                require_api_keys()
        self.assertIn('ANTHROPIC_API_KEY', str(ctx.exception))

    @override_settings(DEMO_MODE=False)
    def test_missing_notion_key_raises_outside_demo_mode(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x'}, clear=True):
            with self.assertRaises(MissingAPIKeyError) as ctx:
                require_api_keys()
        self.assertIn('NOTION_API_KEY', str(ctx.exception))

    @override_settings(DEMO_MODE=True)
    def test_missing_notion_key_is_fine_in_demo_mode(self):
        # Demo mode never calls notion.py — get_upcoming_projects etc. are only
        # reached from the non-demo branch of every view that touches Notion.
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x'}, clear=True):
            require_api_keys()

    @override_settings(DEMO_MODE=False)
    def test_all_keys_present_is_fine(self):
        env = {'ANTHROPIC_API_KEY': 'x', 'NOTION_API_KEY': 'y'}
        with patch.dict(os.environ, env, clear=True):
            require_api_keys()

    @override_settings(DEMO_MODE=False)
    def test_both_missing_names_both_variables(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingAPIKeyError) as ctx:
                require_api_keys()
        self.assertIn('ANTHROPIC_API_KEY', str(ctx.exception))
        self.assertIn('NOTION_API_KEY', str(ctx.exception))


def _anthropic_timeout_error():
    request = httpx.Request('POST', 'https://api.anthropic.com/v1/messages')
    return anthropic.APITimeoutError(request=request)


def _anthropic_rate_limit_error():
    request = httpx.Request('POST', 'https://api.anthropic.com/v1/messages')
    response = httpx.Response(429, request=request)
    return anthropic.RateLimitError('rate limited', response=response, body=None)


class AnthropicFailureTranslationTest(SimpleTestCase):
    """ai.py's two Claude calls translate SDK failures into one app-level
    exception, after the SDK's own retries (max_retries=2 by default) are
    exhausted. Views only need to catch AIUnavailableError, never an
    anthropic.* type directly."""

    def test_weekly_summary_translates_a_timeout(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            MockAnthropic.return_value.messages.stream.side_effect = _anthropic_timeout_error()
            with self.assertRaises(AIUnavailableError):
                generate_weekly_summary([], date.today())

    def test_weekly_summary_translates_a_rate_limit(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            MockAnthropic.return_value.messages.stream.side_effect = _anthropic_rate_limit_error()
            with self.assertRaises(AIUnavailableError):
                generate_weekly_summary([], date.today())

    def test_timelapse_moments_translates_a_failure(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            MockAnthropic.return_value.messages.create.side_effect = _anthropic_timeout_error()
            with self.assertRaises(AIUnavailableError):
                generate_timelapse_moments('Test', date.today(), [])

    def test_original_exception_is_preserved_as_the_cause(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            timeout = _anthropic_timeout_error()
            MockAnthropic.return_value.messages.stream.side_effect = timeout
            with self.assertRaises(AIUnavailableError) as ctx:
                generate_weekly_summary([], date.today())
        self.assertIs(ctx.exception.__cause__, timeout)


def _fake_response(text):
    return Mock(content=[Mock(text=text)])


class GeneratePlanRetryTest(SimpleTestCase):
    """planner_review used to `raise` a json.JSONDecodeError straight at the
    visitor (planner_views.py, pre-#29) — the only place in the app that had
    even looked at a Claude failure, and it still produced a 500. generate_plan
    now retries a bad response once and never raises JSONDecodeError itself."""

    VALID = '{"project_name": "Testkonzert", "tasks": []}'

    def generate(self):
        return generate_plan('Konzert am 5. September', 'keine weiteren Angaben', [])

    def test_returns_parsed_dict_on_first_valid_response(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.return_value = _fake_response(self.VALID)
            result = self.generate()
        self.assertEqual(result, {"project_name": "Testkonzert", "tasks": []})
        self.assertEqual(create.call_count, 1)

    def test_retries_once_on_invalid_json_then_succeeds(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [_fake_response('not json'), _fake_response(self.VALID)]
            result = self.generate()
        self.assertEqual(result, {"project_name": "Testkonzert", "tasks": []})
        self.assertEqual(create.call_count, 2)

    def test_raises_ai_unavailable_after_a_second_invalid_response(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [_fake_response('not json'), _fake_response('still not json')]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(create.call_count, 2)

    def test_an_sdk_failure_is_not_retried_as_a_json_error(self):
        """The SDK already retried transport failures internally (see
        AnthropicFailureTranslationTest); a second attempt here would silently
        double that budget instead of surfacing the failure."""
        with patch('anthropic.Anthropic') as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = _anthropic_timeout_error()
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(create.call_count, 1)

    def test_fenced_response_is_still_parsed(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.return_value = _fake_response(f'```json\n{self.VALID}\n```')
            result = self.generate()
        self.assertEqual(result, {"project_name": "Testkonzert", "tasks": []})


class GetClarifyingQuestionsTest(SimpleTestCase):
    def test_translates_an_sdk_failure(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            MockAnthropic.return_value.messages.create.side_effect = _anthropic_timeout_error()
            with self.assertRaises(AIUnavailableError):
                get_clarifying_questions('Konzert am 5. September', [])

    def test_returns_the_response_text_on_success(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = _fake_response('Wie viele Gäste?')
            self.assertEqual(get_clarifying_questions('Konzert', []), 'Wie viele Gäste?')
