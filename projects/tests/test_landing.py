"""The landing page: what it renders, and where it sends a visitor."""

from django.test import (
    TestCase,
    override_settings,
)
from django.urls import reverse

from .base import DemoModeTestCase


class LandingSessionAwarenessTest(DemoModeTestCase):
    """#129 finding 2: the CTAs were static. A visitor who had already run the
    planner got no route back to their own Übersicht from the landing page —
    the only offer was "Eigenes Projekt planen", which overwrites it."""

    def test_first_visit_leads_into_the_planner(self):
        response = self.client.get(reverse("index"))
        self.assertContains(
            response, f'class="action primary" href="{reverse("planner_start")}"'
        )
        self.assertContains(response, f'href="{reverse("dashboard")}?mode=multi"')
        self.assertNotContains(response, "Mein Dashboard")

    def test_a_returning_visitor_is_offered_their_own_dashboard_first(self):
        self.given_session_plan()
        response = self.client.get(reverse("index"))
        self.assertContains(
            response, f'class="action primary" href="{reverse("dashboard")}"'
        )
        self.assertContains(response, "Mein Dashboard")

    def test_the_planner_stays_reachable_but_no_longer_primary(self):
        self.given_session_plan()
        response = self.client.get(reverse("index"))
        self.assertContains(
            response, f'class="action" href="{reverse("planner_start")}"'
        )
        self.assertNotContains(
            response, f'class="action primary" href="{reverse("planner_start")}"'
        )

    def test_the_multi_project_link_is_unchanged_by_a_session_plan(self):
        self.given_session_plan()
        response = self.client.get(reverse("index"))
        self.assertContains(response, f'href="{reverse("dashboard")}?mode=multi"')

    def test_the_mobile_cta_points_at_the_planner_without_a_plan(self):
        response = self.client.get(reverse("index"))
        self.assertContains(
            response, f'class="seq-cta-btn" href="{reverse("planner_start")}"'
        )

    def test_the_mobile_cta_points_at_the_dashboard_with_a_plan(self):
        self.given_session_plan()
        response = self.client.get(reverse("index"))
        self.assertContains(
            response, f'class="seq-cta-btn" href="{reverse("dashboard")}"'
        )
        self.assertNotContains(
            response, f'class="seq-cta-btn" href="{reverse("planner_start")}"'
        )


@override_settings(DEMO_MODE=False)
class LandingProductionTest(TestCase):
    """The landing page does not exist in production — index() redirects. This
    is also the guard that #129's new context can never reach it."""

    def test_the_root_url_redirects_to_the_dashboard(self):
        response = self.client.get(reverse("index"))
        self.assertRedirects(
            response, reverse("dashboard"), fetch_redirect_response=False
        )
