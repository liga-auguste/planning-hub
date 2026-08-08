import os
from datetime import date, timedelta
from unittest.mock import patch

import anthropic
import httpx
import markdown
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from notion_client.errors import HTTPResponseError, RequestTimeoutError
from unittest.mock import Mock

from .ai import AIUnavailableError, derive_kontext, generate_timelapse_moments, generate_weekly_summary, _valid_moments
from .notion import (
    NotionUnavailableError, create_project, create_tasks, find_project,
    get_historical_projects, get_upcoming_projects, toggle_task, update_task_date,
)
from .planner import generate_plan, get_clarifying_questions
from .planner_views import _get_history, _parse_event_date
from .startup import require_api_keys, MissingAPIKeyError
from .views import CACHE_KEY, SUMMARY_KEY, _annotate_tasks, _fix_ai_markdown

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


class FooterPinningTest(DemoModeTestCase):
    """Part A of #21: the footer pinning rules used to live only in
    landing.html's extra_css override, so every other public page had the
    footer floating mid-viewport. They belong in base_public.html."""

    def test_base_template_makes_body_a_flex_column(self):
        response = self.client.get('/impressum/')
        self.assertContains(response, 'min-height: 100vh; display: flex; flex-direction: column;')

    def test_base_template_pins_the_footer(self):
        response = self.client.get('/impressum/')
        self.assertContains(response, 'margin-top: auto; padding-top: 20px;')

    def test_landing_page_no_longer_duplicates_the_override(self):
        # Exactly one occurrence: the base rule, not a second copy in extra_css.
        response = self.client.get('/')
        self.assertContains(response, 'margin-top: auto', count=1)

    def test_wrapper_padding_for_the_floating_footer_is_gone(self):
        # The 80px bottom padding only existed to keep content clear of the
        # floating footer; once pinned it would double the spacing.
        response = self.client.get('/impressum/')
        self.assertNotContains(response, 'padding: 32px 20px 80px')


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


class PlannerCreateClearsOldSummariesTest(DemoModeTestCase):
    """Replanning clears cached summaries by the unversioned prefix, so
    summaries written under any older key version go too — a session can
    outlive several format changes."""

    def test_all_summary_versions_are_cleared(self):
        session = self.client.session
        session['demo_plan_summary_v3_today'] = '<p>alt</p>'
        session[f'{SUMMARY_KEY}_today'] = '<p>aktuell</p>'
        session.save()
        self.client.post(reverse('planner_create'), data={
            'description': 'Konzert am 5. September',
            'project_name': 'Sommerkonzert',
            'event_date': (date.today() + timedelta(days=30)).isoformat(),
            'task_name': ['Programm festlegen'],
            'task_date': [(date.today() + timedelta(days=7)).isoformat()],
            'task_kontext': ['Planung'],
        })
        self.assertNotIn('demo_plan_summary_v3_today', self.client.session)
        self.assertNotIn(f'{SUMMARY_KEY}_today', self.client.session)


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
        self.assertNotIn(f'{SUMMARY_KEY}_today', self.client.session)


class SummarySessionCacheTest(DemoModeTestCase):
    """Proves the views actually write the current versioned key — without
    this, a key bump could leave every view writing a dead key and the
    assertNotIn tests above would pass vacuously."""

    def test_a_successful_summary_is_cached_under_the_current_key(self):
        self.given_session_plan()
        self.client.get(reverse('dashboard'))
        self.assertIn(f'{SUMMARY_KEY}_today', self.client.session)

    def test_my_plan_reads_and_writes_the_same_key(self):
        self.given_session_plan()
        self.client.get(reverse('my_plan'))
        self.assertIn(f'{SUMMARY_KEY}_today', self.client.session)


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
        self.assertNotIn(f'{SUMMARY_KEY}_{moment}', self.client.session)


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
        """A boundary glued to the last task line would be lazily continued
        into the list by Markdown — a blank line is restored before it."""
        result = _fix_ai_markdown('- **Konzert**\n---\nFreier Text')
        self.assertEqual(result, '- **Konzert**\n\n---\nFreier Text')

    def test_bold_line_ends_the_block(self):
        result = _fix_ai_markdown('- **Konzert**\n**Hinweis**\nFreier Text')
        self.assertEqual(result, '- **Konzert**\n\n**Hinweis**\nFreier Text')

    def test_text_outside_a_block_is_untouched(self):
        self.assertEqual(_fix_ai_markdown('Nur ein Satz.'), 'Nur ein Satz.')

    def test_section_header_keeps_its_blank_line(self):
        """A '##' header after a project block used to lose the blank line that
        makes it a header, so Markdown rendered it as list content and the
        summary showed an unexplained gap. See #20.
        """
        text = '- **Konzert**\n\n## Jetzt fällig\n\nPlakate aushängen'
        self.assertEqual(_fix_ai_markdown(text), text)

    def test_section_header_ends_the_block(self):
        """'#' must reset in_project — text after the header is not a sub-task."""
        result = _fix_ai_markdown('- **Konzert**\nPlakate aushängen\n## Nächste Woche\nFreier Text')
        self.assertEqual(
            result, '- **Konzert**\n    - Plakate aushängen\n\n## Nächste Woche\nFreier Text'
        )

    def test_shallow_sub_task_indent_is_deepened_to_nest(self):
        """python-markdown nests a sub-list at four spaces of indent; the two
        the model tends to emit leave every sub-task a flat sibling li."""
        result = _fix_ai_markdown('- **Konzert**\n  - Plakate aushängen')
        self.assertEqual(result, '- **Konzert**\n    - Plakate aushängen')

    def test_four_space_indent_is_left_alone(self):
        result = _fix_ai_markdown('- **Konzert**\n    - Plakate aushängen')
        self.assertEqual(result, '- **Konzert**\n    - Plakate aushängen')

    def test_shallow_indent_outside_a_block_is_untouched(self):
        self.assertEqual(_fix_ai_markdown('  - Notiz'), '  - Notiz')

    def test_new_format_reply_renders_headers_and_nested_lists(self):
        """The whole pipeline: a reply in the ## format (see build_prompt)
        through markdown() ends up with real h2 headers and nested sub-task
        lists — not a <p><strong> next to an invisible <hr>. See #20."""
        reply = '\n'.join([
            '## Jetzt fällig',
            '',
            '- **Sommerkonzert, 5. Aug** — Plakate müssen heute raus:',
            '  - Plakate aushängen',
            '  - GEMA-Meldung',
            '',
            '## Nächste Woche',
            '',
            '- **Herbstkonzert** — noch gut im Zeitplan:',
            '  - Programm festlegen',
        ])
        html = markdown.markdown(_fix_ai_markdown(reply))
        self.assertIn('<h2>Jetzt fällig</h2>', html)
        self.assertIn('<h2>Nächste Woche</h2>', html)
        self.assertNotIn('<hr', html)
        self.assertNotIn('<p><strong>', html)
        # Two blocks, each an outer project list with a nested sub-task list.
        self.assertEqual(html.count('<ul>'), 4)
        self.assertIn('<li>Plakate aushängen</li>', html)


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

    def test_valid_json_that_is_not_an_object_is_retried(self):
        """A bare task array passes json.loads but would crash
        planner_review on plan.get() — it has to count as a bad response,
        not as a success (the third finding from PR #34's review)."""
        with patch('anthropic.Anthropic') as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [
                _fake_response('[{"name": "Programm festlegen", "days_before": 30}]'),
                _fake_response(self.VALID),
            ]
            result = self.generate()
        self.assertEqual(result, {"project_name": "Testkonzert", "tasks": []})
        self.assertEqual(create.call_count, 2)

    def test_raises_ai_unavailable_after_a_second_non_object_response(self):
        with patch('anthropic.Anthropic') as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [_fake_response('[]'), _fake_response('"nur ein String"')]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(create.call_count, 2)

    def test_an_object_without_a_tasks_list_is_retried(self):
        """A valid object that lacks the "tasks" key passes the dict check
        but would crash planner_review on plan['tasks'] — it has to count
        as a bad response too (the finding from PR #34's review)."""
        with patch('anthropic.Anthropic') as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [
                _fake_response('{"project_name": "Testkonzert"}'),
                _fake_response(self.VALID),
            ]
            result = self.generate()
        self.assertEqual(result, {"project_name": "Testkonzert", "tasks": []})
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


class NotionFailureTranslationTest(SimpleTestCase):
    """notion.py's six public functions each build a fresh client per call
    (_client()) and were entirely unguarded. translate_notion_errors() covers
    notion_client's own exception types (HTTPResponseError, RequestTimeoutError)
    plus httpx.HTTPError as a safety net — notion_client only wraps a timeout,
    not a raw connection failure like httpx.ConnectError."""

    def setUp(self):
        # _client() reads os.environ["NOTION_API_KEY"] directly (a KeyError,
        # not a graceful failure, if unset) — irrelevant to what's under test
        # here, so pin it rather than depend on the ambient environment.
        patcher = patch.dict(os.environ, {'NOTION_API_KEY': 'testkey'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stub_every_call(self, MockClient, exc):
        instance = MockClient.return_value
        instance.databases.query.side_effect = exc
        instance.pages.update.side_effect = exc
        instance.pages.create.side_effect = exc
        return instance

    def test_get_upcoming_projects_translates_a_timeout(self):
        with patch('projects.notion.Client') as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                get_upcoming_projects(date.today())

    def test_get_historical_projects_translates_an_http_error(self):
        request = httpx.Request('POST', 'https://api.notion.com/v1/databases/x/query')
        response = httpx.Response(500, request=request)
        with patch('projects.notion.Client') as MockClient:
            self._stub_every_call(MockClient, HTTPResponseError(response))
            with self.assertRaises(NotionUnavailableError):
                get_historical_projects()

    def test_toggle_task_translates_a_raw_connection_error(self):
        with patch('projects.notion.Client') as MockClient:
            self._stub_every_call(MockClient, httpx.ConnectError('boom'))
            with self.assertRaises(NotionUnavailableError):
                toggle_task('task-id', True)

    def test_update_task_date_translates_a_failure(self):
        with patch('projects.notion.Client') as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                update_task_date('task-id', '2026-09-05')

    def test_create_project_translates_a_failure(self):
        with patch('projects.notion.Client') as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                create_project('Test', date.today())

    def test_create_tasks_translates_a_failure(self):
        with patch('projects.notion.Client') as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                create_tasks('project-id', [{'name': 'x', 'date': '2026-09-05'}])


class FindProjectTest(SimpleTestCase):
    """find_project makes retrying planner_create idempotent at the project
    level: an attempt that died in create_tasks left a project page behind,
    and the retry must find and reuse it instead of creating a twin."""

    def setUp(self):
        patcher = patch.dict(os.environ, {'NOTION_API_KEY': 'testkey'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_the_id_of_an_exact_match(self):
        with patch('projects.notion.Client') as MockClient:
            MockClient.return_value.databases.query.return_value = {'results': [{'id': 'page-1'}]}
            self.assertEqual(find_project('Sommerkonzert', date(2026, 9, 5)), 'page-1')

    def test_queries_by_exact_name_and_date(self):
        with patch('projects.notion.Client') as MockClient:
            query = MockClient.return_value.databases.query
            query.return_value = {'results': []}
            find_project('Sommerkonzert', date(2026, 9, 5))
        conditions = query.call_args.kwargs['filter']['and']
        self.assertIn({'property': 'Name der Veranstaltung', 'title': {'equals': 'Sommerkonzert'}}, conditions)
        self.assertIn({'property': 'Termin', 'date': {'equals': '2026-09-05'}}, conditions)

    def test_returns_none_when_nothing_matches(self):
        with patch('projects.notion.Client') as MockClient:
            MockClient.return_value.databases.query.return_value = {'results': []}
            self.assertIsNone(find_project('Sommerkonzert', date(2026, 9, 5)))

    def test_translates_a_failure(self):
        with patch('projects.notion.Client') as MockClient:
            MockClient.return_value.databases.query.side_effect = RequestTimeoutError()
            with self.assertRaises(NotionUnavailableError):
                find_project('Sommerkonzert', date(2026, 9, 5))


def _fake_task_page(name, iso_date):
    # Shaped the way _get_tasks parses a Notion task page.
    return {
        'id': 'task-1',
        'properties': {
            'Aufgabe': {'title': [{'plain_text': name}]},
            'Wann?': {'date': {'start': iso_date}},
            'Done': {'checkbox': False},
            'Kontext': {'multi_select': []},
        },
    }


class CreateTasksIdempotencyTest(SimpleTestCase):
    """A retried save reaches create_tasks with the same list a failed
    attempt may have partially written (one API call per task) — what
    already made it to Notion must be skipped, not created again."""

    def setUp(self):
        patcher = patch.dict(os.environ, {'NOTION_API_KEY': 'testkey'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_already_written_tasks_are_skipped(self):
        with patch('projects.notion.Client') as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {
                'results': [_fake_task_page('Programm festlegen', '2026-08-20')]
            }
            create_tasks('project-id', [
                {'name': 'Programm festlegen', 'date': '2026-08-20'},
                {'name': 'Plakate aushängen', 'date': '2026-08-27'},
            ])
        self.assertEqual(instance.pages.create.call_count, 1)
        created = instance.pages.create.call_args.kwargs['properties']
        self.assertEqual(created['Aufgabe']['title'][0]['text']['content'], 'Plakate aushängen')

    def test_a_fresh_project_writes_the_whole_list(self):
        with patch('projects.notion.Client') as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {'results': []}
            create_tasks('project-id', [
                {'name': 'Programm festlegen', 'date': '2026-08-20'},
                {'name': 'Plakate aushängen', 'date': '2026-08-27'},
            ])
        self.assertEqual(instance.pages.create.call_count, 2)

    def test_same_name_on_a_different_date_is_not_skipped(self):
        with patch('projects.notion.Client') as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {
                'results': [_fake_task_page('Programm festlegen', '2026-08-20')]
            }
            create_tasks('project-id', [{'name': 'Programm festlegen', 'date': '2026-08-27'}])
        self.assertEqual(instance.pages.create.call_count, 1)


def _fake_upcoming_project(name='Testkonzert'):
    return {
        'id': 'p1', 'name': name, 'event_date': date.today() + timedelta(days=10),
        'performers': '', 'status': None, 'status_color': 'gray', 'tasks': [],
    }


@override_settings(DEMO_MODE=False)
class DashboardNotionFailureTest(TestCase):
    """dashboard()'s production branch used to have nothing between it and
    Notion — a single failed read 500'd the whole page. It now either serves
    the last successful read (flagged stale) or, the very first time ever,
    an honest empty state — never a stack trace."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_cold_cache_and_a_failure_is_an_honest_empty_state_not_a_500(self):
        with patch('projects.views.get_upcoming_projects', side_effect=NotionUnavailableError('boom')):
            response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'nicht verfügbar')

    def test_falls_back_to_the_last_successful_read_when_notion_then_fails(self):
        with patch('projects.views.get_upcoming_projects', return_value=[_fake_upcoming_project()]), \
             patch('projects.views.generate_weekly_summary', return_value='**Sommerkonzert**'):
            first = self.client.get(reverse('dashboard'))
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, 'Testkonzert')

        cache.delete(CACHE_KEY)  # the 8h primary cache expiring; the stale copy outlives it
        with patch('projects.views.get_upcoming_projects', side_effect=NotionUnavailableError('boom')):
            second = self.client.get(reverse('dashboard'))
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, 'Testkonzert')
        self.assertContains(second, 'evtl. nicht')

    def test_no_stale_banner_on_a_normal_successful_request(self):
        with patch('projects.views.get_upcoming_projects', return_value=[_fake_upcoming_project()]), \
             patch('projects.views.generate_weekly_summary', return_value='**Sommerkonzert**'):
            response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, 'evtl. nicht')


@override_settings(DEMO_MODE=False)
class DashboardAiFailureCacheTest(TestCase):
    """A Claude failure while Notion is fine must not be remembered as a
    success: (projects, None) used to land in CACHE_KEY (blanking the AI
    card for the whole 8h TTL) and in STALE_CACHE_KEY (clobbering the last
    good summary) — the second finding from PR #34's review."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_failed_summary_is_retried_on_the_next_request(self):
        with patch('projects.views.get_upcoming_projects', return_value=[_fake_upcoming_project()]), \
             patch('projects.views.generate_weekly_summary', side_effect=AIUnavailableError('boom')):
            first = self.client.get(reverse('dashboard'))
        self.assertContains(first, 'nicht verfügbar')
        # Claude recovers. Without any cache-busting in between, the very
        # next request must pick the summary up again.
        with patch('projects.views.get_upcoming_projects', return_value=[_fake_upcoming_project()]), \
             patch('projects.views.generate_weekly_summary', return_value='**Wieder da**'):
            second = self.client.get(reverse('dashboard'))
        self.assertContains(second, 'Wieder da')

    def test_a_failed_summary_does_not_clobber_the_last_good_one(self):
        with patch('projects.views.get_upcoming_projects', return_value=[_fake_upcoming_project()]), \
             patch('projects.views.generate_weekly_summary', return_value='**Letzte gute Übersicht**'):
            self.client.get(reverse('dashboard'))

        cache.delete(CACHE_KEY)  # the 8h primary cache expiring; the stale copy stays
        with patch('projects.views.get_upcoming_projects', return_value=[_fake_upcoming_project()]), \
             patch('projects.views.generate_weekly_summary', side_effect=AIUnavailableError('boom')):
            self.client.get(reverse('dashboard'))

        cache.delete(CACHE_KEY)  # must be a no-op — a failed fetch may not have cached
        with patch('projects.views.get_upcoming_projects', side_effect=NotionUnavailableError('boom')):
            third = self.client.get(reverse('dashboard'))
        self.assertContains(third, 'Letzte gute Übersicht')
        self.assertContains(third, 'evtl. nicht')


@override_settings(DEMO_MODE=False)
class HistoryFallbackTest(TestCase):
    """_get_history() feeds straight into the planner prompt — a Notion
    failure here used to 500 before the visitor's description even reached
    Claude. Falling back to no calibration data is a worse plan, not a
    broken one, so this degrades to [] rather than serving anything stale.
    """

    def test_notion_failure_falls_back_to_an_empty_history(self):
        cache.clear()
        with patch('projects.planner_views.get_historical_projects', side_effect=NotionUnavailableError('boom')):
            self.assertEqual(_get_history(), [])


@override_settings(DEMO_MODE=False)
class ToggleTaskNotionFailureTest(TestCase):
    """toggle_task_view always returned {"ok": True} regardless of what
    toggle_task() actually did — a Notion failure here used to either 500 or
    (before #29) go unnoticed entirely, leaving the checkbox and Notion
    silently disagreeing. A non-200 is what lets the frontend refuse to
    apply the change it was hoping for."""

    def test_notion_failure_is_a_502_not_a_500(self):
        with patch('projects.views.toggle_task', side_effect=NotionUnavailableError('boom')):
            response = self.client.post(
                reverse('toggle_task', args=['task-1']),
                data='{"done": true}',
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {'error': 'notion unavailable'})

    def test_success_still_reports_ok(self):
        with patch('projects.views.toggle_task') as mock_toggle:
            response = self.client.post(
                reverse('toggle_task', args=['task-1']),
                data='{"done": true}',
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)
        mock_toggle.assert_called_once_with('task-1', True)


@override_settings(DEMO_MODE=False)
class RescheduleTaskNotionFailureTest(TestCase):
    def test_notion_failure_is_a_502_not_a_500(self):
        with patch('projects.views.update_task_date', side_effect=NotionUnavailableError('boom')):
            response = self.client.post(
                reverse('reschedule_task', args=['task-1']),
                data='{"date": "2026-09-05"}',
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {'error': 'notion unavailable'})

    def test_success_still_reports_ok(self):
        with patch('projects.views.update_task_date') as mock_update:
            response = self.client.post(
                reverse('reschedule_task', args=['task-1']),
                data='{"date": "2026-09-05"}',
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)
        mock_update.assert_called_once_with('task-1', '2026-09-05')


@override_settings(DEMO_MODE=False)
class PlannerCreateNotionFailureTest(TestCase):
    """create_project/create_tasks were unguarded — a Notion failure here
    used to 500 after the visitor had already reviewed and adjusted a full
    task list, losing all of it. It's now redisplayed with the same tasks
    and dates instead of vanishing."""

    def post_plan(self):
        event_date = date.today() + timedelta(days=30)
        task_date = date.today() + timedelta(days=7)
        return self.client.post(reverse('planner_create'), data={
            'description': 'Konzert am 5. September',
            'project_name': 'Sommerkonzert',
            'event_date': event_date.isoformat(),
            'task_name': ['Programm festlegen'],
            'task_date': [task_date.isoformat()],
            'task_kontext': ['Planung'],
        })

    def test_notion_failure_redisplays_the_plan_instead_of_losing_it(self):
        with patch('projects.planner_views.find_project', return_value=None), \
             patch('projects.planner_views.create_project', side_effect=NotionUnavailableError('boom')):
            response = self.post_plan()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'projects/planner_review.html')
        self.assertContains(response, 'Sommerkonzert')
        self.assertContains(response, 'Programm festlegen')
        self.assertContains(response, 'nicht gespeichert')

    def test_a_failure_in_create_tasks_also_redisplays_the_plan(self):
        with patch('projects.planner_views.find_project', return_value=None), \
             patch('projects.planner_views.create_project', return_value='page-id'), \
             patch('projects.planner_views.create_tasks', side_effect=NotionUnavailableError('boom')):
            response = self.post_plan()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Programm festlegen')

    def test_a_failure_in_the_lookup_itself_also_redisplays_the_plan(self):
        with patch('projects.planner_views.find_project', side_effect=NotionUnavailableError('boom')):
            response = self.post_plan()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Programm festlegen')
        self.assertContains(response, 'nicht gespeichert')

    def test_a_retry_reuses_the_project_the_failed_attempt_created(self):
        """The error page invites the visitor to re-POST the same plan. If
        the first attempt died between create_project and create_tasks, the
        retry must attach the tasks to the existing page, not create a twin
        project — the duplicate-data finding from PR #34's review."""
        with patch('projects.planner_views.find_project', return_value='page-id') as mock_find, \
             patch('projects.planner_views.create_project') as mock_create_project, \
             patch('projects.planner_views.create_tasks') as mock_create_tasks:
            response = self.post_plan()
        self.assertRedirects(response, reverse('dashboard'), fetch_redirect_response=False)
        mock_find.assert_called_once()
        mock_create_project.assert_not_called()
        mock_create_tasks.assert_called_once()
        called_project_id, called_tasks = mock_create_tasks.call_args.args
        self.assertEqual(called_project_id, 'page-id')
        self.assertEqual([t['name'] for t in called_tasks], ['Programm festlegen'])

    def test_success_still_redirects_to_the_dashboard(self):
        with patch('projects.planner_views.find_project', return_value=None), \
             patch('projects.planner_views.create_project', return_value='page-id'), \
             patch('projects.planner_views.create_tasks') as mock_create_tasks:
            response = self.post_plan()
        # fetch_redirect_response=False: dashboard()'s own behavior has its
        # own tests (DashboardNotionFailureTest); this only checks the
        # redirect target, not a live render of it.
        self.assertRedirects(response, reverse('dashboard'), fetch_redirect_response=False)
        mock_create_tasks.assert_called_once()

    def test_a_saved_plan_busts_the_dashboard_cache(self):
        """planner_create used to delete the cache by a hardcoded string —
        a key bump in views.py would silently turn that into a no-op and a
        freshly saved project would hide behind the 8h TTL."""
        self.addCleanup(cache.clear)
        cache.set(CACHE_KEY, ([], '<p>alt</p>'), 60)
        with patch('projects.planner_views.find_project', return_value=None), \
             patch('projects.planner_views.create_project', return_value='page-id'), \
             patch('projects.planner_views.create_tasks'):
            self.post_plan()
        self.assertIsNone(cache.get(CACHE_KEY))
