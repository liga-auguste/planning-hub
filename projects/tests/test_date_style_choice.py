"""#192 option 2: the date style chosen on /regeln/, stored per backend the way
rules.py stores rules, and applied to every date a request formats."""

from datetime import date

from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .. import display_style
from ..date_format import DATE_STYLES, active_style, format_date, style_resolver
from ..models import DisplaySetting
from .base import DemoModeTestCase


class StyleLabelsTest(TestCase):
    def test_every_style_has_a_label_and_no_label_names_a_missing_style(self):
        self.assertEqual(set(display_style.STYLE_LABELS), set(DATE_STYLES))


class OutsideARequestTest(TestCase):
    """Management commands and direct calls have no request, so the deploy-time
    default decides — what option 1 did everywhere."""

    @override_settings(DATE_STYLE="numeric")
    def test_the_setting_decides(self):
        self.assertEqual(active_style(), "numeric")
        self.assertEqual(format_date(date(2026, 3, 3)), "Di, 03.03.")

    def test_the_resolver_is_restored_after_the_block(self):
        with style_resolver(lambda: "no_weekday"):
            self.assertEqual(active_style(), "no_weekday")
        self.assertEqual(active_style(), "standard")


@override_settings(DEMO_MODE=False)
class ProductionBackendTest(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/")

    def test_nothing_chosen_means_the_setting(self):
        with override_settings(DATE_STYLE="no_weekday"):
            self.assertEqual(display_style.get_style(self.request), "no_weekday")

    def test_a_choice_is_stored_in_the_single_row(self):
        self.assertTrue(display_style.set_style(self.request, "numeric"))
        self.assertTrue(display_style.set_style(self.request, "no_weekday"))
        self.assertEqual(DisplaySetting.objects.count(), 1)
        self.assertEqual(display_style.get_style(self.request), "no_weekday")

    def test_an_unknown_style_cannot_be_saved(self):
        self.assertFalse(display_style.set_style(self.request, "numerisch"))
        self.assertFalse(DisplaySetting.objects.exists())

    def test_a_stored_style_that_no_longer_exists_falls_back(self):
        # Only reachable by a style removed after it was saved — the page
        # keeps rendering in the default instead of failing.
        DisplaySetting.objects.create(pk=1, date_style="removed")
        self.assertEqual(display_style.get_style(self.request), "standard")


class DemoBackendTest(DemoModeTestCase):
    def test_the_choice_lives_in_the_session(self):
        self.client.post(reverse("date_style_update"), {"date_style": "numeric"})
        self.assertEqual(
            self.client.session[display_style.DEMO_DATE_STYLE_KEY], "numeric"
        )
        self.assertFalse(DisplaySetting.objects.exists())

    def test_one_visitors_choice_does_not_reach_another(self):
        self.client.post(reverse("date_style_update"), {"date_style": "numeric"})
        other = self.client_class()
        response = other.get(reverse("rules_list"))
        checked = [o["value"] for o in response.context["date_styles"] if o["checked"]]
        self.assertEqual(checked, ["standard"])


class ChoosingOnTheRulesPageTest(DemoModeTestCase):
    def test_the_page_offers_every_style_with_todays_examples(self):
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, "Datumsformat")
        for style, label in display_style.STYLE_LABELS.items():
            with self.subTest(style=style):
                self.assertContains(response, f'value="{style}"')
                self.assertContains(response, label)

    def test_each_example_renders_in_its_own_style(self):
        # Not in the request's style: the examples are what the choice is
        # made between.
        options = self.client.get(reverse("rules_list")).context["date_styles"]
        today = timezone.localdate()
        for option in options:
            with self.subTest(style=option["value"]):
                with style_resolver(lambda style=option["value"]: style):
                    expected = [format_date(today, "long"), format_date(today, "row")]
                self.assertEqual(option["examples"], expected)

    def test_saving_marks_the_choice_and_redirects_back(self):
        response = self.client.post(
            reverse("date_style_update"), {"date_style": "no_weekday"}
        )
        self.assertRedirects(response, reverse("rules_list") + "#datumsformat")
        options = self.client.get(reverse("rules_list")).context["date_styles"]
        self.assertEqual([o["value"] for o in options if o["checked"]], ["no_weekday"])

    def test_a_save_says_so_once(self):
        # The page looks the same after a save, so the note is the only sign
        # the click did anything — and it belongs to that save, not to every
        # later visit.
        response = self.client.post(
            reverse("date_style_update"), {"date_style": "numeric"}, follow=True
        )
        self.assertContains(response, "Datumsformat gespeichert.")
        self.assertContains(response, 'id="datumsformat"')
        self.assertNotContains(
            self.client.get(reverse("rules_list")), "Datumsformat gespeichert."
        )

    def test_a_refused_style_says_nothing_was_saved(self):
        self.client.post(reverse("date_style_update"), {"date_style": "numerisch"})
        self.assertNotContains(
            self.client.get(reverse("rules_list")), "Datumsformat gespeichert."
        )

    def test_an_unknown_style_is_refused(self):
        response = self.client.post(
            reverse("date_style_update"), {"date_style": "numerisch"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(display_style.DEMO_DATE_STYLE_KEY, self.client.session)

    def test_a_get_changes_nothing(self):
        self.client.get(reverse("date_style_update"))
        self.assertNotIn(display_style.DEMO_DATE_STYLE_KEY, self.client.session)


class TheChoiceReachesEveryRenderTest(DemoModeTestCase):
    """The middleware is what carries the choice into format_date, so templates
    (plan_date), the add row's pattern and JSON labels follow without a
    request argument at any call site."""

    def choose(self, style):
        self.client.post(reverse("date_style_update"), {"date_style": style})

    def test_the_dashboard_renders_in_the_chosen_style(self):
        self.given_session_plan()
        self.choose("numeric")
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(f'data-date-pattern="{DATE_STYLES["numeric"]["row"]}"', html)
        with style_resolver(lambda: "numeric"):
            self.assertIn(format_date(timezone.localdate(), "long"), html)

    def test_the_choice_beats_the_setting(self):
        self.given_session_plan()
        self.choose("no_weekday")
        with override_settings(DATE_STYLE="numeric"):
            html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(f'data-date-pattern="{DATE_STYLES["no_weekday"]["row"]}"', html)

    def test_nothing_leaks_past_the_request(self):
        self.given_session_plan()
        self.choose("numeric")
        self.client.get(reverse("dashboard"))
        self.assertEqual(active_style(), "standard")


class TheWayBackTest(DemoModeTestCase):
    """#192: /regeln/ is reached from the sidebar as well as from the planner,
    so its back link names where the visitor came from — and keeps naming it
    after a save, which redirects back to the page."""

    dashboard_back = '<a href="/dashboard/" class="back">← Dashboard</a>'

    def test_from_the_sidebar_it_leads_back_to_the_dashboard(self):
        response = self.client.get(reverse("rules_list") + "?from=dashboard")
        self.assertContains(response, self.dashboard_back)
        self.assertNotContains(response, "← Planer")

    def test_otherwise_it_leads_back_to_the_planner(self):
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, "← Planer")
        self.assertNotContains(response, self.dashboard_back)

    def test_a_date_style_save_keeps_it(self):
        response = self.client.post(
            reverse("date_style_update"),
            {"date_style": "numeric", "from": "dashboard"},
        )
        self.assertRedirects(
            response, reverse("rules_list") + "?from=dashboard#datumsformat"
        )

    def test_a_rule_add_keeps_it(self):
        response = self.client.post(
            reverse("rule_add"), {"text": "Neue Regel", "from": "dashboard"}
        )
        self.assertRedirects(response, reverse("rules_list") + "?from=dashboard")

    def test_an_unknown_origin_is_ignored(self):
        response = self.client.post(
            reverse("date_style_update"),
            {"date_style": "numeric", "from": "https://example.com"},
        )
        self.assertRedirects(response, reverse("rules_list") + "#datumsformat")
