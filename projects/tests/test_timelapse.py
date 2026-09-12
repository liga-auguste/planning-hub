"""Zeitreise: generated moments, the simulated date and the preloader."""

import json
from datetime import (
    UTC,
    date,
    datetime,
    timedelta,
)
from unittest.mock import patch

from django.test import (
    SimpleTestCase,
    override_settings,
)
from django.urls import reverse

from ..ai import (
    AIUnavailableError,
    _valid_moments,
    generate_timelapse_moments,
)
from ..date_format import format_date
from ..views import SUMMARY_KEY
from .base import (
    DemoModeTestCase,
    _fake_response,
)


class PreloadAiFailureTest(DemoModeTestCase):
    def test_preload_reports_ok_false_and_writes_nothing_to_the_session(self):
        self.given_session_plan()
        moment = (date.today() + timedelta(days=5)).isoformat()
        self.given_timelapse_moments(moment)
        self.ai_mocks[
            "projects.views.generate_weekly_summary"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.post(
            reverse("preload_timelapse_summary"),
            data=f'{{"date": "{moment}"}}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": False})
        self.assertNotIn(f"{SUMMARY_KEY}_{moment}", self.client.session)


class TimelapseValidationTest(DemoModeTestCase):
    """An unvalidated string in demo_sim_date used to break every later request."""

    def post_date(self, body):
        return self.client.post(
            reverse("set_timelapse_date"), data=body, content_type="application/json"
        )

    def test_invalid_date_is_rejected(self):
        response = self.post_date('{"date": "kaputt"}')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("demo_sim_date", self.client.session)

    def test_malformed_json_is_rejected(self):
        response = self.post_date("{")
        self.assertEqual(response.status_code, 400)

    def test_valid_date_is_stored(self):
        sim_date = (date.today() + timedelta(days=5)).isoformat()
        self.given_timelapse_moments(sim_date)
        response = self.post_date(f'{{"date": "{sim_date}"}}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["demo_sim_date"], sim_date)

    def test_empty_date_clears_the_session(self):
        today = date.today().isoformat()
        self.given_timelapse_moments(today)
        self.post_date(f'{{"date": "{today}"}}')
        response = self.post_date('{"date": null}')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("demo_sim_date", self.client.session)

    @override_settings(DEMO_MODE=False)
    def test_unavailable_outside_demo_mode(self):
        self.assertEqual(self.post_date('{"date": null}').status_code, 404)

    def test_preload_rejects_invalid_date(self):
        response = self.client.post(
            reverse("preload_timelapse_summary"),
            data='{"date": "kaputt"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_preload_rejects_malformed_json(self):
        response = self.client.post(
            reverse("preload_timelapse_summary"),
            data="{",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class SimDateIsRestrictedToGeneratedMomentsTest(DemoModeTestCase):
    """A parseable date is not enough. Every accepted date costs a Claude call, so
    only the moments planner_create generated for this session may be posted."""

    def post_date(self, url_name, raw):
        return self.client.post(
            reverse(url_name),
            data=f'{{"date": "{raw}"}}',
            content_type="application/json",
        )

    def test_well_formed_date_outside_the_moments_is_rejected(self):
        self.given_timelapse_moments((date.today() + timedelta(days=5)).isoformat())
        other = (date.today() + timedelta(days=6)).isoformat()
        self.assertEqual(self.post_date("set_timelapse_date", other).status_code, 400)
        self.assertNotIn("demo_sim_date", self.client.session)

    def test_far_future_date_is_rejected(self):
        self.given_timelapse_moments(date.today().isoformat())
        self.assertEqual(
            self.post_date("set_timelapse_date", "9999-12-31").status_code, 400
        )

    def test_no_moments_means_no_date_is_accepted(self):
        sim_date = (date.today() + timedelta(days=5)).isoformat()
        self.assertEqual(
            self.post_date("set_timelapse_date", sim_date).status_code, 400
        )

    def test_preload_spends_no_api_call_on_an_unlisted_date(self):
        self.given_session_plan()
        self.given_timelapse_moments(date.today().isoformat())
        unlisted = (date.today() + timedelta(days=99)).isoformat()
        response = self.post_date("preload_timelapse_summary", unlisted)
        self.assertEqual(response.status_code, 400)
        self.ai_mocks["projects.views.generate_weekly_summary"].assert_not_called()

    def test_preload_accepts_a_listed_date(self):
        moment = (date.today() + timedelta(days=5)).isoformat()
        self.given_session_plan()
        self.given_timelapse_moments(moment)
        response = self.post_date("preload_timelapse_summary", moment)
        self.assertEqual(response.status_code, 200)
        self.ai_mocks["projects.views.generate_weekly_summary"].assert_called()

    def test_replanning_invalidates_the_old_moments(self):
        old = (date.today() + timedelta(days=5)).isoformat()
        self.given_timelapse_moments(old)
        self.given_timelapse_moments((date.today() + timedelta(days=9)).isoformat())
        self.assertEqual(self.post_date("set_timelapse_date", old).status_code, 400)


class UnparseableMomentDateTest(DemoModeTestCase):
    """Being on the allowlist is not enough — the moments come from Claude, so a
    session written before they were validated can list a date nothing can parse."""

    def post_date(self, url_name, raw):
        return self.client.post(
            reverse(url_name),
            data=f'{{"date": "{raw}"}}',
            content_type="application/json",
        )

    def test_wrong_format_is_not_written_to_the_session(self):
        self.given_timelapse_moments("05.09.2026")
        self.assertEqual(
            self.post_date("set_timelapse_date", "05.09.2026").status_code, 400
        )
        self.assertNotIn("demo_sim_date", self.client.session)

    def test_impossible_day_does_not_reach_fromisoformat(self):
        self.given_session_plan()
        self.given_timelapse_moments("2026-02-30")
        self.assertEqual(
            self.post_date("preload_timelapse_summary", "2026-02-30").status_code, 400
        )


class MalformedPayloadTest(DemoModeTestCase):
    """Valid JSON of the wrong shape must be a 400, not an unhandled exception.
    Both endpoints are unauthenticated on the public demo."""

    URL_NAMES = ("set_timelapse_date", "preload_timelapse_summary")

    def post_body(self, url_name, body):
        return self.client.post(
            reverse(url_name), data=body, content_type="application/json"
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
        for body in ("null", "[]", '"2026-09-05"', "5"):
            self.assert_rejects(body)

    def test_unhashable_moment_in_the_session_does_not_break_the_allowlist(self):
        session = self.client.session
        session["demo_timelapse_moments"] = [{"date": ["2026-09-05"]}, {"date": None}]
        session.save()
        self.assert_rejects('{"date": "2026-09-05"}')


class PoisonedSessionHealingTest(DemoModeTestCase):
    """Sessions poisoned before the validation landed must recover on their own."""

    def poison(self):
        session = self.client.session
        session["demo_sim_date"] = "kaputt"
        session.save()

    def test_dashboard_renders_and_clears_the_bad_value(self):
        self.given_session_plan()
        self.poison()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("demo_sim_date", self.client.session)

    def test_dashboard_renders_without_a_session_plan(self):
        self.poison()
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)


class MultiViewSimDateTest(DemoModeTestCase):
    """#50: the simulated date used to be read before the mode was known, so a
    Zeitreise set on the visitor's own plan classified and narrated the example
    projects too — while every control that reveals or resets it stayed hidden."""

    def given_sim_date(self, sim_date):
        """Writes the value straight into the session, the way set_timelapse_date does."""
        session = self.client.session
        session["demo_sim_date"] = sim_date.isoformat()
        session.save()

    def given_plan_in_the_future(self):
        sim_date = date.today() + timedelta(days=120)
        self.given_session_plan()
        self.given_timelapse_moments(sim_date.isoformat())
        self.given_sim_date(sim_date)
        return sim_date

    def multi_tasks(self, response):
        return [
            task
            for group in response.context["month_groups"]
            for project in group["projects"]
            for task in project["tasks"]
        ]

    def test_a_future_task_is_not_overdue(self):
        self.given_plan_in_the_future()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        future = [
            t
            for t in self.multi_tasks(response)
            if t["due"] and t["due"] > date.today()
        ]
        self.assertTrue(future, "the fixtures should carry tasks that are still due")
        self.assertEqual([t for t in future if t["urgency"] == "overdue"], [])

    def test_the_summary_is_generated_for_the_real_today(self):
        """A fix that only corrects the classification would leave the AI card
        narrating the simulated date — the contradiction the issue observed."""
        self.given_plan_in_the_future()
        self.client.get(reverse("dashboard") + "?mode=multi")
        call = self.ai_mocks["projects.views.generate_weekly_summary"].call_args
        self.assertEqual(call[0][1], date.today())

    def test_no_simulation_banner_and_no_simulation_label(self):
        self.given_plan_in_the_future()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertNotContains(response, "Simulierter Zeitpunkt")
        self.assertNotContains(response, "KI-Simulation")
        self.assertContains(response, "KI-Wochenübersicht")

    def test_the_simulated_date_survives_the_detour(self):
        """The simulation belongs to the visitor's plan, so a look at the example
        projects scopes it out rather than resetting it."""
        self.given_plan_in_the_future()
        self.client.get(reverse("dashboard") + "?mode=multi")
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Simulierter Zeitpunkt")


class ValidMomentsTest(SimpleTestCase):
    """generate_timelapse_moments returns raw model JSON. These dates become an
    allowlist and are parsed back later, so they cannot be taken on trust."""

    def test_well_formed_moments_survive_untouched(self):
        moments = [{"date": "2026-09-05", "label": "Probe", "description": "Text"}]
        self.assertEqual(_valid_moments(moments), moments)

    def test_unparseable_date_is_dropped(self):
        self.assertEqual(_valid_moments([{"date": "05.09.2026", "label": "Probe"}]), [])

    def test_impossible_day_is_dropped(self):
        self.assertEqual(_valid_moments([{"date": "2026-02-30"}]), [])

    def test_non_string_date_is_dropped(self):
        self.assertEqual(_valid_moments([{"date": None}, {"date": ["2026-09-05"]}]), [])

    def test_moment_without_a_date_is_dropped(self):
        self.assertEqual(_valid_moments([{"label": "Probe"}, "nonsense"]), [])

    def test_parseable_date_is_normalised(self):
        """date.fromisoformat also takes the basic and week forms, which the dashboard
        JS cannot — it builds `date + 'T12:00:00'`. Rewrite them rather than drop them."""
        self.assertEqual(
            _valid_moments([{"date": "20260905", "label": "Probe"}]),
            [{"date": "2026-09-05", "label": "Probe"}],
        )
        self.assertEqual(
            _valid_moments([{"date": "2026-W36-6"}]), [{"date": "2026-09-05"}]
        )

    def test_datetime_string_is_dropped(self):
        self.assertEqual(_valid_moments([{"date": "2026-09-05T10:00:00"}]), [])

    def test_good_moments_are_kept_when_a_sibling_is_dropped(self):
        result = _valid_moments([{"date": "kaputt"}, {"date": "2026-09-05"}])
        self.assertEqual(result, [{"date": "2026-09-05"}])

    def test_a_non_list_response_yields_no_moments(self):
        self.assertEqual(_valid_moments({"date": "2026-09-05"}), [])
        self.assertEqual(_valid_moments(None), [])

    def test_out_of_order_moments_are_sorted_chronologically(self):
        moments = [
            {"date": "2026-12-23", "label": "Verträge besiegelt"},
            {"date": "2026-12-14", "label": "Visuelles Gesicht"},
            {"date": "2027-01-08", "label": "Verkauf öffnet"},
        ]
        self.assertEqual(
            _valid_moments(moments),
            [
                {"date": "2026-12-14", "label": "Visuelles Gesicht"},
                {"date": "2026-12-23", "label": "Verträge besiegelt"},
                {"date": "2027-01-08", "label": "Verkauf öffnet"},
            ],
        )

    def test_sort_uses_the_normalised_date_not_the_original_spelling(self):
        moments = [
            {"date": "2026-12-23", "label": "Verträge besiegelt"},
            {"date": "20261214", "label": "Visuelles Gesicht"},
        ]
        self.assertEqual(
            _valid_moments(moments),
            [
                {"date": "2026-12-14", "label": "Visuelles Gesicht"},
                {"date": "2026-12-23", "label": "Verträge besiegelt"},
            ],
        )

    def test_invalid_moments_are_dropped_before_sorting(self):
        moments = [
            {"date": "2026-12-23", "label": "Verträge besiegelt"},
            {"date": "kaputt"},
            {"date": "2026-12-14", "label": "Visuelles Gesicht"},
        ]
        self.assertEqual(
            _valid_moments(moments),
            [
                {"date": "2026-12-14", "label": "Visuelles Gesicht"},
                {"date": "2026-12-23", "label": "Verträge besiegelt"},
            ],
        )


class TimelapseSingleDateAuthorityTest(DemoModeTestCase):
    """#153: with a simulated moment active the dashboard showed two
    "today"s at once — the sim banner's simulated date and the real
    {{ today_display }} under the header. The header date now hides during
    a simulation, leaving the banner as the single date authority."""

    def given_active_simulation(self):
        sim = (date.today() + timedelta(days=5)).isoformat()
        self.given_timelapse_moments(sim)
        session = self.client.session
        session["demo_sim_date"] = sim
        session.save()

    def test_the_header_date_hides_during_a_simulation(self):
        self.given_session_plan()
        self.given_active_simulation()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Simulierter Zeitpunkt")
        self.assertNotContains(response, format_date(date.today()))

    def test_the_header_date_is_back_without_a_simulation(self):
        # "Zurück zu heute" clears demo_sim_date, so the no-sim render is
        # exactly the state that button restores.
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, format_date(date.today()))
        self.assertNotContains(response, "Simulierter Zeitpunkt")

    def test_a_session_plan_has_only_the_one_today_surface(self):
        """#53 added a second "today" surface and two assertions here kept
        its banner in step with the overview's. #240 hid that surface for a
        session plan while the view is reworked, and the pairing they
        guarded cannot occur meanwhile — the banner needs a session plan and
        the panel is not rendered for one.

        Inverted rather than deleted: the two belong back in step the moment
        the panel returns, and this way that return has to come past a test
        that says so. #153's own rule keeps a witness either way — one
        simulated date, named in exactly one place."""
        self.given_session_plan()
        self.given_active_simulation()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Simulierter Zeitpunkt", count=1)
        self.assertNotContains(response, 'id="view-today"')


class TimelapseBaselineUsesLocalDateTest(SimpleTestCase):
    """#85: generate_timelapse_moments used date.today() as the "Zeitraum"
    start date fed into the Claude prompt — same day-boundary bug as task
    urgency, just baked into a prompt instead of an urgency bucket."""

    @patch("django.utils.timezone.now")
    def test_prompt_zeitraum_start_is_the_berlin_date(self, mock_now):
        mock_now.return_value = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.return_value = _fake_response("[]")
            generate_timelapse_moments("Test", date(2026, 2, 1), [])
        prompt = create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Zeitraum: 2026-01-16 bis", prompt)


class TimelapseMomentsLoggingTest(SimpleTestCase):
    def test_logs_usage_on_success(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.return_value = _fake_response(
                "[]", input_tokens=77, output_tokens=12
            )
            with self.assertLogs("projects.ai", level="INFO") as cm:
                generate_timelapse_moments("Test", date.today(), [])
        [record] = cm.output
        self.assertIn("call=generate_timelapse_moments", record)
        self.assertIn("input_tokens=77", record)
        self.assertIn("output_tokens=12", record)
        self.assertIn("outcome=success", record)


class TimelapsePrecachedMomentsTest(DemoModeTestCase):
    """#36: preloadAll() re-fires all 4 preload POSTs on every reload because
    the in-memory `preloaded` Set is empty again. Moments the session already
    cached a summary for are now passed to the template so the JS can seed
    `preloaded` from them instead of re-requesting."""

    def test_a_moment_with_a_cached_summary_is_precached(self):
        self.given_timelapse_moments("2026-09-05")
        session = self.client.session
        session[f"{SUMMARY_KEY}_2026-09-05"] = "<p>cached</p>"
        session.save()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'const PRECACHED_MOMENTS = ["2026-09-05"]')

    def test_a_moment_without_a_cached_summary_is_not_precached(self):
        self.given_timelapse_moments("2026-09-05")
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "const PRECACHED_MOMENTS = []")

    def test_malformed_session_moments_do_not_break_the_dashboard(self):
        session = self.client.session
        session["demo_timelapse_moments"] = [
            {"date": ["2026-09-05"]},
            {"date": None},
            "not-a-dict",
        ]
        session.save()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)


class TimelapsePreloadMarkupTest(DemoModeTestCase):
    """#36: markup-contract tests for the preloader fix — runtime behaviour
    (a rate-limited fetch really gets skipped) isn't provable by a Django
    TestCase and gets a manual browser pass, same boundary documented on
    PlannerLoadingStateTest."""

    def test_preload_one_checks_response_ok_before_marking_preloaded(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "if (!response.ok) return;")

    def test_template_seeds_preloaded_from_precached_moments(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "const PRECACHED_MOMENTS = ")
        self.assertContains(response, "const preloaded = new Set(PRECACHED_MOMENTS);")

    def test_sim_date_awaits_preload_before_reloading(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            "if (dateStr) {\n        await preloadOne(dateStr, {priority: true});\n    }",
        )

    def test_preload_one_dedupes_concurrent_calls_for_the_same_date(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "const preloadPromises = new Map();")
        self.assertContains(response, "if (preloadPromises.has(dateStr)) {")
        self.assertContains(response, "return preloadPromises.get(dateStr);")

    def test_preload_one_checks_json_ok_not_just_http_status(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "if (!data.ok) return;")

    def test_session_writing_fetches_are_serialized_through_one_queue(self):
        """#93 follow-up: Django saves the whole session dict per response, so
        concurrent /timelapse/preload/ calls (fired together from
        preloadAll's Promise.all) raced and silently dropped each other's
        cached summary — reproduced by clearing all moment summaries and
        observing only some survive a fresh preloadAll(). withSessionLock
        forces every session-writing fetch onto one queue.

        #235 replaced the promise chain with an explicit queue, so this
        asserts the flag that still lets exactly one task be in flight: the
        serialisation is what has to survive, not the shape it had."""
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "let sessionLockRunning = false;")
        self.assertContains(response, "if (sessionLockRunning) return;")
        self.assertContains(response, "withSessionLock(() => fetch('/timelapse/', {")
        self.assertContains(response, "const promise = withSessionLock(async () => {")

    def test_moment_tiles_stretch_to_fill_the_row(self):
        # grow: 1 is what this asserts — the row fills, no trailing gap. The
        # basis moved from 0 to auto so a tile is at least as wide as its own
        # label: the labels come back from Claude and differ in length, and
        # an equal-width row sized every one of them to the shortest.
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, ".moment-btn { flex: 1 1 auto;")
        self.assertContains(response, ".moment-btn-today { flex: 1 1 auto;")

    def test_a_long_moment_label_is_clipped_rather_than_painted_over_its_neighbour(
        self,
    ):
        """The label is nowrap and not length-bounded — Claude writes it. With
        no clip it ran straight out of its tile, and the active tile, drawn
        later, swallowed the overflow: the text read as cut off by accident
        rather than by design. Same recipe as .day-task-name."""
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            ".moment-title { font-size: 12px; font-weight: 600; "
            "white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }",
        )

    def test_a_moment_label_wraps_rather_than_truncates_at_phone_width(self):
        """Two tiles per row leave too little for an ellipsis to say
        anything, and the label is the whole point of the moment — so below
        the breakpoint it wraps instead. Asserted inside the media block, not
        just anywhere in the sheet: as a desktop rule it would undo the
        single-line tile the row's even height depends on."""
        response = self.client.get(reverse("dashboard"))
        _, mobile = response.content.decode().split("@media (max-width: 768px) {", 1)
        self.assertIn(".moment-title { white-space: normal; }", mobile)

    def test_reload_fades_out_before_reloading(self):
        """A cross-document view transition would be the native fix, but
        Chrome doesn't grant one to a same-URL reload — verified by listening
        for `pagereveal`'s `viewTransition` around a real setSimDate() click,
        which came back null. This CSS fade is the fallback."""
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "document.body.style.opacity = '0';")

    def test_loading_spinner_lives_next_to_the_heading_not_inside_each_tile(self):
        """Toggling a spinner's display inside a moment-btn changed that
        tile's height, so every click reflowed the whole row — one shared
        spinner next to the "Zeitreise" label avoids that; the tiles
        themselves now only ever change color, never size."""
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            '<div class="timelapse-label">Zeitreise<span class="timelapse-spinner"></span></div>',
        )
        self.assertContains(
            response, ".timelapse-bar.loading .timelapse-spinner { display: block; }"
        )
        self.assertNotContains(response, '<div class="spinner"></div>')


class TimelapseClickPriorityTest(DemoModeTestCase):
    """#235: a click used to enter the session-write queue at the back, so
    even an already-cached moment (green dot, no API call needed) waited for
    every queued preload to finish first. Markup contract only — the queue's
    runtime ordering gets a manual browser pass, the same boundary
    TimelapsePreloadMarkupTest documents."""

    def test_the_lock_keeps_two_lists_and_takes_the_priority_one_first(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "const priorityQueue = [];")
        self.assertContains(response, "const backgroundQueue = [];")
        self.assertContains(
            response,
            "const entry = priorityQueue.shift() || backgroundQueue.shift();",
        )

    def test_a_click_asks_for_priority_for_both_of_its_writes(self):
        """The preload the click needs and the /timelapse/ POST that stores
        the date. The POST is trivial — it writes one session key — but it
        used to wait behind every remaining Claude call all the same."""
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "await preloadOne(dateStr, {priority: true});")
        self.assertContains(response, "}), {priority: true});")

    def test_a_click_drops_the_preloads_that_have_not_started(self):
        """First statement of setSimDate, before the spinner. Deferring them
        would be pointless work: the reload throws the page away and
        preloadAll() starts over 800 ms later."""
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            "async function setSimDate(dateStr, clickedBtn) {\n"
            "    dropPendingPreloads(dateStr);",
        )
        self.assertContains(response, "function dropPendingPreloads(keepKey = null) {")

    def test_the_clicked_moment_is_promoted_rather_than_awaited_at_the_back(self):
        """preloadAll registers all four moments 800 ms after load, and
        preloadOne hands a second caller the promise it already registered.
        Without promotion a click after that point gets the background
        promise and waits at the back of the queue no matter what priority
        it asked for — which is every click, not an edge case."""
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "if (priority) promoteSessionTask(dateStr);")
        self.assertContains(response, "function promoteSessionTask(key) {")
        self.assertContains(
            response,
            "if (index !== -1) priorityQueue.push(backgroundQueue.splice(index, 1)[0]);",
        )

    def test_an_in_flight_preload_is_never_aborted(self):
        """The floor this issue requires: a click waits out the one call
        already running. Aborting it client-side does not stop Django from
        finishing the request and saving its session snapshot — that is the
        clobbering the queue exists to prevent — and it would let
        location.reload(), which sits outside the queue, fire into a session
        write still in flight."""
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "AbortController")


class TimelapseBarRendersOncePerPageTest(DemoModeTestCase):
    """#183 gave the bar to both views: it only rendered in view-overview,
    so switching to "Heute" lost the ability to jump between simulated
    moments and the visitor had to flip back just to change the date.

    #240 leaves it with one view to render in while "Heute" is hidden for a
    session plan: the bar needs one (_timelapse_bar.html), which is exactly
    the state the panel is not rendered in, so the second copy could not
    appear and the include reads as dead code. It goes back beside the view
    whenever the view does — #183's reasoning is untouched. The count is
    asserted rather than dropped, so that two copies mean the panel is back
    rather than a duplicate having crept in; the preloader marks moments
    across every copy it finds (`querySelectorAll`) precisely because that
    number is not its business."""

    def test_the_timelapse_bar_renders_once(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'class="timelapse-bar"', count=1)
        self.assertContains(response, 'class="timelapse-moments"', count=1)

    def test_absent_without_a_session_plan(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'class="timelapse-bar"')


class MomentFixtureMixin:
    """The two-task moment fixture, shared by the classes that need a moment
    to be active. A mixin rather than a base class, and not named test*, so
    unittest collects it nowhere — the same reason base.py is not
    test_base.py."""

    def given_two_tasks_around_a_moment(self):
        """A moment with one task due before it (forced done) and one after
        it (still open) — so the dot's own state is observable either way.

        The open task sits ten days past the moment, not three: urgency is
        calendar-week based (#169), and a three-day gap lands in the moment's
        own ISO week on four weekdays out of seven, which made the task's
        stage — and with it `dot ok` — depend on the day the suite ran.
        Ten days cannot share an ISO week with the moment, on any weekday.
        """
        moment = date.today() + timedelta(days=10)
        self.given_session_plan(
            tasks=[
                {
                    "id": "demo-session-0",
                    "name": "Programm festlegen",
                    "date": (moment - timedelta(days=3)).isoformat(),
                    "done": False,
                },
                {
                    "id": "demo-session-1",
                    "name": "Programmhefte drucken",
                    "date": (moment + timedelta(days=10)).isoformat(),
                    "done": False,
                },
            ]
        )
        self.given_timelapse_moments(moment.isoformat())
        return moment

    def given_active_moment(self):
        moment = self.given_two_tasks_around_a_moment()
        session = self.client.session
        session["demo_sim_date"] = moment.isoformat()
        session.save()
        return moment


class NoToggleDuringAMomentTest(MomentFixtureMixin, DemoModeTestCase):
    """#217: dashboard() renders a moment by forcing every task due by
    sim_date to done on a deep copy — that is what a moment *is*. A toggle
    therefore persisted into the session and changed nothing the visitor
    could see: the clicked dot un-struck itself while the Kanban card, the
    week bar, the day counters and the sidebar ring all still read it as
    done, and a reload put the strike-through back. A visitor cannot tell
    "nothing happened" from "it happened and you cannot see it", so the
    interaction goes while the moment is on — the same rule that keeps
    rescheduling to where it persists (#10 §5), applied to visibility.
    """

    TOKEN_INPUT = '<input type="hidden" name="csrfmiddlewaretoken"'

    def post_toggle(self, task_id, done=True):
        return self.client.post(
            reverse("toggle_task", args=[task_id]),
            data=json.dumps({"done": done}),
            content_type="application/json",
        )

    def test_a_toggle_during_a_moment_is_a_404(self):
        self.given_active_moment()
        self.assertEqual(self.post_toggle("demo-session-0", done=True).status_code, 404)

    def test_the_rejected_toggle_writes_nothing_to_the_session_plan(self):
        self.given_active_moment()
        self.post_toggle("demo-session-0", done=True)
        task = self.client.session["demo_plan"]["tasks"][0]
        self.assertFalse(task["done"])
        self.assertIsNone(task.get("completed_date"))

    def test_unchecking_a_forced_done_task_is_a_404_too(self):
        """The task the issue observed: due before the moment, so it renders
        as done without ever having been written that way."""
        self.given_active_moment()
        self.assertEqual(
            self.post_toggle("demo-session-0", done=False).status_code, 404
        )

    def test_the_dashboard_offers_no_toggle_during_a_moment(self):
        self.given_active_moment()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Simulierter Zeitpunkt")
        self.assertNotContains(response, 'class="toggle-form"')

    def test_the_dot_still_renders_with_its_urgency(self):
        """Only the affordance goes. The status indicator is what the whole
        moment exists to show, so it stays — as a span, which .dot styles
        identically (only button.dot carries cursor, border and :hover)."""
        self.given_active_moment()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, '<span class="dot done')
        self.assertContains(response, '<span class="dot ok')

    def test_the_toggle_is_back_without_a_moment(self):
        self.given_two_tasks_around_a_moment()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'class="toggle-form"')
        self.assertEqual(self.post_toggle("demo-session-0", done=True).status_code, 200)
        self.assertTrue(self.client.session["demo_plan"]["tasks"][0]["done"])

    def test_the_page_carries_a_csrf_token_during_a_moment(self):
        """The regression this fix's first cut caused: every JavaScript write
        on this page reads the token with
        document.querySelector('[name=csrfmiddlewaretoken]'), and in a demo
        session the only tokens rendered were the toggle forms' own. Removing
        those left the Zeitreise POSTs with an empty token — 403, while
        setSimDate reloads regardless, so the tiles and "Zurück zu heute"
        read as dead and the visitor was stuck inside the moment."""
        self.given_active_moment()
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'class="toggle-form"')
        # The rendered input, not the string: every JS line that reads the
        # token spells "csrfmiddlewaretoken" too, so a bare substring check
        # passes with no token on the page at all.
        self.assertContains(response, self.TOKEN_INPUT)

    def test_the_page_token_does_not_depend_on_the_toggle_forms(self):
        """Asserted with no moment too, so the token cannot quietly go back
        to being a side effect of whichever form happens to render."""
        self.given_two_tasks_around_a_moment()
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(self.TOKEN_INPUT, html)
        self.assertLess(
            html.index(self.TOKEN_INPUT),
            html.index('class="toggle-form"'),
            "the page's own token must come first — querySelector takes the "
            "first match, and it is the one that is always there",
        )

    def test_rescheduling_is_still_offered_during_a_moment(self):
        """Unlike a toggle, a new date takes visible effect — it moves the
        task out of the forced-done range or leaves it outside. Only the
        toggle contradicts itself, so only the toggle goes."""
        self.given_active_moment()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'title="Datum ändern"')
        moved = self.client.post(
            reverse("reschedule_task", args=["demo-session-1"]),
            data=json.dumps({"date": (date.today() + timedelta(days=40)).isoformat()}),
            content_type="application/json",
        )
        self.assertEqual(moved.status_code, 200)


class AMomentSaysWhatItLocksTest(MomentFixtureMixin, DemoModeTestCase):
    """#244: #217 took the write affordances out of a moment and said nothing
    about it — the dot is a <span>, three ⋮ entries are omitted, and a visitor
    who clicks gets no refusal, no hint and no cursor change on the way in. The
    write protection stays exactly as #217 built it; what is added is the
    explanation, in the two places a visitor reaches for a write: the dot that
    was clicked, and the menu that dropped the entries."""

    CONSEQUENCE = "hier lässt sich nichts abhaken"

    def dashboard(self):
        return self.client.get(reverse("dashboard"))

    def test_the_banner_names_the_state_and_stops_there(self):
        """The banner carried the consequence for one release and it was one
        sentence too many: it is on screen the whole time a moment is on, so
        it announced a refusal to every visitor including the ones who never
        try to check anything off. The answer belongs where the attempt is
        made — pinned here so the clause does not drift back in."""
        self.given_active_moment()
        response = self.dashboard()
        self.assertContains(response, "Simulierter Zeitpunkt")
        self.assertNotContains(response, self.CONSEQUENCE)

    def test_the_simulated_date_is_still_named_exactly_once(self):
        """#153's one-date rule, re-asserted against the new copy. The notice
        and the menu note spell it inflected and lowercase ("Im simulierten
        Zeitpunkt"), so neither collides with the banner's own label — a
        deliberate choice, not luck."""
        self.given_active_moment()
        self.assertContains(self.dashboard(), "Simulierter Zeitpunkt", count=1)

    def test_a_locked_dot_has_a_notice_to_answer_with(self):
        response = self.given_active_moment() and self.dashboard()
        self.assertContains(response, 'id="sim-lock-notice"')
        self.assertContains(
            response, "Im simulierten Zeitpunkt lässt sich nichts abhaken."
        )

    def test_the_notice_starts_hidden(self):
        """It answers a click, so it must not be on the page before one."""
        self.given_active_moment()
        self.assertContains(
            self.dashboard(), 'class="sim-lock-notice" role="status" hidden'
        )

    def test_no_notice_element_without_a_moment(self):
        """Which is what lets the delegated handler treat every span.dot on
        the page as a locked dot: outside a moment there is no element and no
        listener at all."""
        self.given_two_tasks_around_a_moment()
        self.assertNotContains(self.dashboard(), 'id="sim-lock-notice"')

    def test_the_notice_offers_the_way_back_to_today(self):
        """ "Heute anzeigen", not "Zurück zu heute": the banner's short label
        is pinned page-wide by a design test, and the Zeitreise tile beside
        it is already "Heute"."""
        self.given_active_moment()
        response = self.dashboard()
        self.assertContains(response, 'onclick="setSimDate(null)">Heute anzeigen<')
        self.assertNotContains(response, ">Zurück zu heute<")

    def test_the_notice_answers_every_attempt_not_only_the_first(self):
        """A visitor who tries again two minutes later deserves the same
        answer, so the timer restarts rather than an "already shown" flag
        being set."""
        self.given_active_moment()
        self.assertContains(self.dashboard(), "clearTimeout(simLockTimer);")

    def test_the_notice_dismisses_itself(self):
        self.given_active_moment()
        self.assertContains(
            self.dashboard(), "setTimeout(hideSimLockNotice, SIM_LOCK_NOTICE_MS)"
        )

    def test_the_notice_is_placed_against_the_dot_that_was_clicked(self):
        """Not at the top of the page: it answers where the visitor was
        looking. All four dot surfaces are covered by one delegated listener."""
        self.given_active_moment()
        response = self.dashboard()
        self.assertContains(response, "dot.getBoundingClientRect()")
        self.assertContains(response, "e.target.closest('span.dot')")

    def test_the_notice_leaves_when_the_page_scrolls(self):
        """Fixed coordinates are written once, on show — the same reason the
        actions menu closes on scroll rather than following. Capturing,
        because the day columns and the board scroll inside the page."""
        self.given_active_moment()
        self.assertContains(
            self.dashboard(),
            "window.addEventListener('scroll', hideSimLockNotice, true)",
        )

    def test_the_notice_never_hides_behind_an_open_menu(self):
        self.given_active_moment()
        response = self.dashboard()
        self.assertContains(
            response, ".sim-lock-notice { position: fixed; z-index: 40;"
        )
        self.assertContains(
            response, ".task-menu-items { position: fixed; z-index: 30;"
        )

    def test_the_notice_obeys_its_hidden_attribute(self):
        """display: flex otherwise beats the UA rule for [hidden] — the same
        answer .task-menu-items[hidden] already needed."""
        self.given_active_moment()
        self.assertContains(
            self.dashboard(), ".sim-lock-notice[hidden] { display: none; }"
        )

    def test_the_notice_inherits_the_banner_tokens(self):
        """No new tokens: the surface is borrowed from the Zeitreise banner,
        so the answer and the state it explains read as one thing."""
        self.given_active_moment()
        response = self.dashboard()
        self.assertContains(
            response,
            "background: var(--color-notice-bg); color: var(--color-notice-text); border-radius: 6px; padding: 8px 12px;",
        )

    def test_the_dot_gains_no_affordance(self):
        """The explanation is added, the affordance is not given back (#217).
        The span is a click target and still not a control: cursor, border
        and :hover stay on button.dot alone."""
        self.given_active_moment()
        response = self.dashboard()
        self.assertNotContains(response, 'class="toggle-form"')
        self.assertNotContains(response, "span.dot { cursor")

    def test_the_menu_names_what_the_moment_took_out(self):
        """Three ⋮ entries are dropped during a moment and were dropped
        silently. One line replaces them, so the menu says the same thing the
        dot does."""
        self.given_active_moment()
        response = self.dashboard()
        self.assertContains(response, 'class="task-menu-note"')
        self.assertContains(
            response,
            "Im simulierten Zeitpunkt nicht verfügbar: Abhaken, Umbenennen, Papierkorb.",
        )

    def test_the_menu_note_is_absent_outside_a_moment(self):
        self.given_two_tasks_around_a_moment()
        self.assertNotContains(self.dashboard(), 'class="task-menu-note"')

    def test_the_menu_note_is_not_a_menu_item(self):
        """Deliberately not .task-menu-item: the keyboard handler collects
        exactly that class and openTaskMenuFor focuses the first it finds, so
        a note carrying it would be a focusable menu entry that does nothing.
        It also needs white-space: normal, because the items beside it are
        nowrap and the sentence would stretch the menu to its own width."""
        self.given_active_moment()
        response = self.dashboard()
        self.assertNotContains(response, 'class="task-menu-note task-menu-item"')
        self.assertNotContains(response, 'class="task-menu-item task-menu-note"')
        self.assertContains(
            response,
            ".task-menu-note { font-size: 11px; color: var(--color-text-quaternary); padding: 6px 10px 8px; border-bottom: 1px solid var(--color-border-primary); margin-bottom: 4px; white-space: normal; max-width: 220px; }",
        )
