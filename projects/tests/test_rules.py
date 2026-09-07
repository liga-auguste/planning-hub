"""Planning rules: the rules page, both backends, seeding and the
migrations that backfill them."""

import importlib
import json

from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import (
    Client,
    RequestFactory,
    TestCase,
    override_settings,
)
from django.urls import reverse

from ..models import (
    PlannerRule,
    RulesSeeded,
)
from ..rules import (
    DEMO_RULES_KEY,
    INITIAL_RULES,
    add_rule,
    get_active_rule_texts,
)
from .base import DemoModeTestCase


class RuleReorderNonListOrderTest(DemoModeTestCase):
    """#167: a non-list "order" value is a 400, never a 500 or a silent
    reorder. An int or null crashed reorder_rules with a TypeError; a
    string was iterated character by character."""

    def post_order(self, order):
        return self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": order}),
            content_type="application/json",
        )

    def test_a_non_list_order_is_a_400(self):
        for payload in (5, "12", None):
            with self.subTest(order=payload):
                response = self.post_order(payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())


class PlannerRulesBackLinkTest(DemoModeTestCase):
    """#7 (inherited from #22): the rules page's "← Planer" link always
    returned to the empty tile step, discarding whatever project type the
    visitor had already chosen — even though planner_start's ?type= handler
    already writes it to the session unconditionally. Only the step is
    restored, not unsaved free text (see PlannerTileLinksTest for why)."""

    def test_back_link_is_bare_without_a_chosen_type(self):
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, f'href="{reverse("planner_start")}"')
        self.assertNotContains(response, "?type=")

    def test_back_link_carries_the_previously_chosen_type(self):
        self.client.get(reverse("planner_start") + "?type=konzert")
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, f'href="{reverse("planner_start")}?type=konzert"')


class PlannerRulesDemoModeTest(DemoModeTestCase):
    """#22: PlannerRule was the one demo-editable object that was not
    session-scoped, so any anonymous visitor could rewrite or delete the rules
    every other visitor's plan is generated with. In demo mode the rules now
    live in request.session — and, per #105, start empty rather than seeded
    from INITIAL_RULES, so a visitor's example plan isn't built from the
    maintainer's concert-specific production rules."""

    def request_with_session(self):
        """A request carrying this client's session, as the planner views see it."""
        request = RequestFactory().get("/")
        request.session = self.client.session
        return request

    def add_rule_id(self, text, project_types=None):
        """Adds a demo rule via the view (so CSRF/session wiring matches a real
        request) and returns the id the session assigned it."""
        self.client.post(
            reverse("rule_add"),
            data={"text": text, "project_types": project_types or []},
        )
        stored = self.client.session[DEMO_RULES_KEY]
        return next(r["id"] for r in stored if r["text"] == text)

    def test_a_fresh_session_starts_with_no_rules(self):
        response = self.client.get(reverse("rules_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Noch keine Regeln")
        for rule in INITIAL_RULES:
            self.assertNotContains(response, rule["text"])

    def test_reading_the_rules_page_persists_no_session(self):
        """The demo is public and not yet behind a robots.txt (#27), so a GET
        must not leave a session row behind for every visitor and crawler."""
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, "Noch keine Regeln")
        self.assertEqual(Session.objects.count(), 0)

    def test_the_first_write_persists_an_added_rule(self):
        self.client.post(reverse("rule_add"), data={"text": "Erste Regel"})
        self.assertEqual(Session.objects.count(), 1)
        stored = self.client.session[DEMO_RULES_KEY]
        self.assertEqual([r["text"] for r in stored], ["Erste Regel"])
        self.assertEqual(stored[0]["project_types"], [])
        self.assertTrue(stored[0]["active"])

    def test_adding_a_rule_writes_nothing_to_the_database(self):
        self.client.post(reverse("rule_add"), data={"text": "Neue Regel"})
        self.assertEqual(PlannerRule.objects.count(), 0)
        self.assertContains(self.client.get(reverse("rules_list")), "Neue Regel")

    def test_adding_a_rule_persists_its_project_types(self):
        self.client.post(
            reverse("rule_add"),
            data={"text": "Neue Regel", "project_types": ["hochzeit", "konzert"]},
        )
        stored = self.client.session[DEMO_RULES_KEY]
        added = next(r for r in stored if r["text"] == "Neue Regel")
        self.assertEqual(added["project_types"], ["hochzeit", "konzert"])

    def test_add_rejects_malformed_project_types(self):
        request = self.request_with_session()
        add_rule(request, "Direkt aufgerufen", project_types="konzert")
        stored = request.session[DEMO_RULES_KEY]
        added = next(r for r in stored if r["text"] == "Direkt aufgerufen")
        self.assertEqual(added["project_types"], [])

    def test_toggle_update_delete_and_reorder_write_nothing_to_the_database(self):
        id_a = self.add_rule_id("Regel A")
        id_b = self.add_rule_id("Regel B")
        id_c = self.add_rule_id("Regel C")
        self.client.post(reverse("rule_toggle", args=[id_a]))
        self.client.post(
            reverse("rule_update", args=[id_b]),
            data=json.dumps({"text": "Geänderte Regel"}),
            content_type="application/json",
        )
        self.client.post(reverse("rule_delete", args=[id_c]))
        self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": [str(id_b), str(id_a)]}),
            content_type="application/json",
        )
        self.assertEqual(PlannerRule.objects.count(), 0)

    def test_one_visitor_cannot_change_what_another_one_sees(self):
        other = Client()
        self.client.post(reverse("rule_add"), data={"text": "Nur für mich"})

        response = other.get(reverse("rules_list"))
        self.assertNotContains(response, "Nur für mich")
        self.assertContains(response, "Noch keine Regeln")

    def test_a_deactivated_rule_stays_listed_but_leaves_the_prompt(self):
        rule_id = self.add_rule_id("GEMA-Meldung einplanen", ["konzert"])
        response = self.client.post(reverse("rule_toggle", args=[rule_id]))
        self.assertEqual(response.json()["active"], False)

        request = self.request_with_session()
        self.assertNotIn(
            "GEMA-Meldung einplanen", get_active_rule_texts(request, "konzert")
        )
        self.assertContains(
            self.client.get(reverse("rules_list")), "GEMA-Meldung einplanen"
        )

    def test_reordering_reaches_the_prompt_in_the_new_order(self):
        id_a = self.add_rule_id("Regel A")
        id_b = self.add_rule_id("Regel B")
        self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": [str(id_b), str(id_a)]}),
            content_type="application/json",
        )
        self.assertEqual(
            get_active_rule_texts(self.request_with_session(), "konzert"),
            ["Regel B", "Regel A"],
        )

    def test_a_string_order_does_not_silently_reorder_the_rules(self):
        """#167: a string like "21" used to be iterated character by
        character, applying a swap the client never validly asked for.
        It is rejected and the stored order stays untouched."""
        id_a = self.add_rule_id("Regel A")
        id_b = self.add_rule_id("Regel B")
        response = self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": f"{id_b}{id_a}"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            get_active_rule_texts(self.request_with_session(), "konzert"),
            ["Regel A", "Regel B"],
        )

    def test_rules_are_filtered_by_project_type(self):
        """#105: a rule tagged with specific project_types only reaches the
        prompt for those types; a rule with no project_types applies to all
        of them."""
        self.add_rule_id("Nur Konzert", ["konzert"])
        self.add_rule_id("Immer", [])
        request = self.request_with_session()

        self.assertIn("Nur Konzert", get_active_rule_texts(request, "konzert"))
        self.assertNotIn("Nur Konzert", get_active_rule_texts(request, "hochzeit"))
        self.assertIn("Immer", get_active_rule_texts(request, "konzert"))
        self.assertIn("Immer", get_active_rule_texts(request, "hochzeit"))

    def test_rules_page_only_shows_rules_for_the_current_project_type(self):
        """A visitor planning a Workshop should not see a Konzert-only rule
        left over from an earlier tile in the same session (#105)."""
        self.add_rule_id("Nur Konzert", ["konzert"])
        self.add_rule_id("Nur Workshop", ["workshop"])
        self.add_rule_id("Immer", [])

        session = self.client.session
        session["demo_project_type"] = "workshop"
        session.save()

        response = self.client.get(reverse("rules_list"))
        self.assertNotContains(response, "Nur Konzert")
        self.assertContains(response, "Nur Workshop")
        self.assertContains(response, "Immer")

    def test_rules_page_shows_everything_without_a_current_project_type(self):
        """Reached from outside the planner flow (no tile picked yet), there is
        nothing to scope by, so every rule stays visible."""
        self.add_rule_id("Nur Konzert", ["konzert"])
        self.add_rule_id("Nur Workshop", ["workshop"])

        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, "Nur Konzert")
        self.assertContains(response, "Nur Workshop")

    def test_updating_a_rules_project_types_is_reflected_in_the_filter(self):
        rule_id = self.add_rule_id("Nur Konzert", ["konzert"])
        self.client.post(
            reverse("rule_update", args=[rule_id]),
            data=json.dumps({"project_types": []}),
            content_type="application/json",
        )
        request = self.request_with_session()
        self.assertIn("Nur Konzert", get_active_rule_texts(request, "hochzeit"))

    def test_omitting_project_types_on_update_leaves_it_unchanged(self):
        rule_id = self.add_rule_id("Nur Konzert", ["konzert"])
        self.client.post(
            reverse("rule_update", args=[rule_id]),
            data=json.dumps({"text": "Neuer Text"}),
            content_type="application/json",
        )
        stored = self.client.session[DEMO_RULES_KEY]
        updated = next(r for r in stored if r["id"] == rule_id)
        self.assertEqual(updated["text"], "Neuer Text")
        self.assertEqual(updated["project_types"], ["konzert"])

    def test_malformed_project_types_on_update_leaves_it_unchanged(self):
        """A crafted request straight to the JSON endpoint (the UI never sends
        a non-list) must not be able to write a value into the session that
        would later force a re-seed — same intent as _is_valid()'s self-healing
        on read, applied at the point the bad value would otherwise land."""
        rule_id = self.add_rule_id("Nur Konzert", ["konzert"])
        self.client.post(
            reverse("rule_update", args=[rule_id]),
            data=json.dumps({"project_types": "konzert"}),
            content_type="application/json",
        )
        stored = self.client.session[DEMO_RULES_KEY]
        updated = next(r for r in stored if r["id"] == rule_id)
        self.assertEqual(updated["project_types"], ["konzert"])

    def test_toggling_an_unknown_rule_is_a_404(self):
        response = self.client.post(reverse("rule_toggle", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_deleting_an_unknown_rule_is_a_404(self):
        response = self.client.post(reverse("rule_delete", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_a_poisoned_session_re_seeds_instead_of_crashing(self):
        session = self.client.session
        session[DEMO_RULES_KEY] = "kaputt"
        session.save()
        response = self.client.get(reverse("rules_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Noch keine Regeln")

    def test_entries_of_the_wrong_shape_re_seed_instead_of_crashing(self):
        session = self.client.session
        session[DEMO_RULES_KEY] = [{"id": 1}, "kaputt"]
        session.save()
        response = self.client.get(reverse("rules_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Noch keine Regeln")

    def test_malformed_project_types_re_seeds_instead_of_crashing(self):
        session = self.client.session
        session[DEMO_RULES_KEY] = [
            {"id": 1, "text": "x", "active": True, "project_types": "konzert"}
        ]
        session.save()
        response = self.client.get(reverse("rules_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Noch keine Regeln")

    def test_the_page_explains_the_demo_scope_and_inactive_rules(self):
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, "diesem Besuch")
        self.assertContains(response, "nicht in den Plan")


@override_settings(DEMO_MODE=False)
class PlannerRulesDatabaseModeTest(TestCase):
    """The production path is untouched by #22: rules stay in the database and
    the session stays empty."""

    def setUp(self):
        for i, rule in enumerate(INITIAL_RULES):
            PlannerRule.objects.create(
                text=rule["text"],
                active=True,
                order=i,
                project_types=rule["project_types"],
            )

    def test_add_creates_a_rule_in_the_database(self):
        self.client.post(reverse("rule_add"), data={"text": "Neue Regel"})
        rule = PlannerRule.objects.get(text="Neue Regel")
        self.assertTrue(rule.active)
        self.assertEqual(rule.order, len(INITIAL_RULES))
        self.assertEqual(rule.project_types, [])
        self.assertNotIn(DEMO_RULES_KEY, self.client.session)

    def test_add_persists_project_types_in_the_database(self):
        self.client.post(
            reverse("rule_add"),
            data={"text": "Neue Regel", "project_types": ["hochzeit"]},
        )
        rule = PlannerRule.objects.get(text="Neue Regel")
        self.assertEqual(rule.project_types, ["hochzeit"])

    def test_add_rejects_malformed_project_types(self):
        """add_rule's only caller (the view) always sends a list via
        request.POST.getlist(), but the function itself should not rely on
        that — same guard as update_rule, applied on the way in instead of
        on the way out."""
        request = RequestFactory().get("/")
        request.session = self.client.session
        add_rule(request, "Direkt aufgerufen", project_types="konzert")
        rule = PlannerRule.objects.get(text="Direkt aufgerufen")
        self.assertEqual(rule.project_types, [])

    def test_toggle_flips_the_database_row(self):
        rule = PlannerRule.objects.first()
        response = self.client.post(reverse("rule_toggle", args=[rule.pk]))
        self.assertEqual(response.json()["active"], False)
        rule.refresh_from_db()
        self.assertFalse(rule.active)

    def test_update_changes_the_database_row(self):
        rule = PlannerRule.objects.first()
        self.client.post(
            reverse("rule_update", args=[rule.pk]),
            data=json.dumps({"text": "Geänderte Regel"}),
            content_type="application/json",
        )
        rule.refresh_from_db()
        self.assertEqual(rule.text, "Geänderte Regel")

    def test_update_persists_project_types(self):
        rule = PlannerRule.objects.first()  # seeded as ["konzert"]
        self.client.post(
            reverse("rule_update", args=[rule.pk]),
            data=json.dumps({"project_types": ["hochzeit", "recruiting"]}),
            content_type="application/json",
        )
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, ["hochzeit", "recruiting"])

    def test_omitting_project_types_on_update_leaves_it_unchanged(self):
        rule = PlannerRule.objects.first()  # seeded as ["konzert"]
        self.client.post(
            reverse("rule_update", args=[rule.pk]),
            data=json.dumps({"text": "Nur Text geändert"}),
            content_type="application/json",
        )
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, ["konzert"])

    def test_malformed_project_types_on_update_leaves_it_unchanged(self):
        """The DB backend has no read-time self-healing like the session's
        _is_valid() — an int would make _applies() raise, a string would
        silently turn its membership check into a substring check. Rejecting
        the bad value here, before it reaches the JSONField, is the only
        guard this backend gets."""
        rule = PlannerRule.objects.first()  # seeded as ["konzert"]
        self.client.post(
            reverse("rule_update", args=[rule.pk]),
            data=json.dumps({"project_types": "konzert"}),
            content_type="application/json",
        )
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, ["konzert"])

    def test_delete_removes_the_database_row(self):
        rule = PlannerRule.objects.first()
        self.client.post(reverse("rule_delete", args=[rule.pk]))
        self.assertFalse(PlannerRule.objects.filter(pk=rule.pk).exists())
        self.assertEqual(PlannerRule.objects.count(), len(INITIAL_RULES) - 1)

    def test_deleting_an_unknown_rule_is_a_404(self):
        response = self.client.post(reverse("rule_delete", args=[9999]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(PlannerRule.objects.count(), len(INITIAL_RULES))

    def test_reorder_writes_the_new_order_column(self):
        ids = list(PlannerRule.objects.values_list("pk", flat=True))
        self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": [str(i) for i in reversed(ids)]}),
            content_type="application/json",
        )
        self.assertEqual(
            list(PlannerRule.objects.values_list("pk", flat=True)),
            list(reversed(ids)),
        )

    def test_the_prompt_gets_the_active_rules_from_the_database(self):
        PlannerRule.objects.filter(text=INITIAL_RULES[0]["text"]).update(active=False)
        request = RequestFactory().get("/")
        request.session = self.client.session
        expected = [
            r["text"]
            for r in INITIAL_RULES[1:]
            if not r["project_types"] or "konzert" in r["project_types"]
        ]
        self.assertEqual(get_active_rule_texts(request, "konzert"), expected)

    def test_rules_are_filtered_by_project_type_in_the_database(self):
        """#105: same filter as the demo session backend."""
        request = RequestFactory().get("/")
        request.session = self.client.session
        konzert_only = INITIAL_RULES[0]["text"]  # tagged ["konzert"]
        always = INITIAL_RULES[1]["text"]  # tagged []

        self.assertIn(konzert_only, get_active_rule_texts(request, "konzert"))
        self.assertNotIn(konzert_only, get_active_rule_texts(request, "hochzeit"))
        self.assertIn(always, get_active_rule_texts(request, "konzert"))
        self.assertIn(always, get_active_rule_texts(request, "hochzeit"))

    def test_the_demo_notice_is_absent(self):
        response = self.client.get(reverse("rules_list"))
        self.assertNotContains(response, "diesem Besuch")

    def test_the_rules_page_is_scoped_to_the_current_project_type(self):
        """The maintainer only ever plans Konzert-tile events (concerts and,
        within that same tile, church services) and does not want to manage
        an event-type distinction she has no use for — so, same as the demo,
        the page filters to whatever she is currently planning (#105)."""
        session = self.client.session
        session["demo_project_type"] = "hochzeit"
        session.save()
        response = self.client.get(reverse("rules_list"))
        self.assertNotContains(response, INITIAL_RULES[0]["text"])  # konzert-only
        self.assertContains(response, INITIAL_RULES[1]["text"])  # applies to all

    def test_the_rules_page_shows_everything_without_a_current_project_type(self):
        """Reached from the dashboard sidebar rather than mid-planning, there
        is no type to scope by, so the maintainer sees her whole rule set."""
        response = self.client.get(reverse("rules_list"))
        for rule in INITIAL_RULES:
            self.assertContains(response, rule["text"])


@override_settings(DEMO_MODE=False)
class SeedRulesCommandTest(TestCase):
    def test_seeds_all_initial_rules_with_their_project_types(self):
        call_command("seed_rules")
        self.assertEqual(PlannerRule.objects.count(), len(INITIAL_RULES))
        for i, rule in enumerate(INITIAL_RULES):
            stored = PlannerRule.objects.get(text=rule["text"])
            self.assertEqual(stored.project_types, rule["project_types"])
            self.assertEqual(stored.order, i)
            self.assertTrue(stored.active)

    def test_marks_itself_seeded(self):
        call_command("seed_rules")
        self.assertTrue(RulesSeeded.objects.exists())

    def test_is_a_no_op_on_a_second_run(self):
        call_command("seed_rules")
        call_command("seed_rules")
        self.assertEqual(PlannerRule.objects.count(), len(INITIAL_RULES))

    def test_does_not_reseed_after_every_rule_is_deleted(self):
        """A maintainer can clear PlannerRule down to zero via the rules UI
        (rules.py exposes full add/delete). entrypoint.sh now runs seed_rules
        on every container start, and a deploy reruns the whole stack, so an
        idempotency check based on PlannerRule's row count would silently
        resurrect the deleted defaults on the next deploy. Tracking "already
        seeded" via RulesSeeded instead avoids that."""
        call_command("seed_rules")
        PlannerRule.objects.all().delete()
        call_command("seed_rules")
        self.assertEqual(PlannerRule.objects.count(), 0)


@override_settings(DEMO_MODE=False)
class BackfillPlannerRuleProjectTypesMigrationTest(TestCase):
    """#105 review follow-up: rows a pre-#105 seed_rules run created sit at
    project_types' field default ([]) once the migration adds the column.
    Migration 0007 backfills the scoping those rows are missing by matching
    on rule text."""

    def setUp(self):
        from django.apps import apps

        self.backfill = importlib.import_module(
            "projects.migrations.0007_backfill_planner_rule_project_types"
        ).backfill_project_types
        self.apps = apps

    def test_backfills_a_known_rule_still_at_the_default(self):
        rule = PlannerRule.objects.create(
            text="Vorverkauf nur bei größeren Konzerten relevant",
            active=True,
            order=0,
        )
        self.backfill(self.apps, None)
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, ["konzert"])

    def test_leaves_a_manually_assigned_row_untouched(self):
        rule = PlannerRule.objects.create(
            text="Vorverkauf nur bei größeren Konzerten relevant",
            active=True,
            order=0,
            project_types=["hochzeit"],
        )
        self.backfill(self.apps, None)
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, ["hochzeit"])

    def test_leaves_a_custom_rule_untouched(self):
        rule = PlannerRule.objects.create(
            text="Eigene Regel der Maintainerin", active=True, order=0
        )
        self.backfill(self.apps, None)
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, [])


@override_settings(DEMO_MODE=False)
class MarkSeededIfRulesAlreadyExistMigrationTest(TestCase):
    """Migration 0009 backfills a RulesSeeded marker for any environment that
    already ran the old, row-count-based seed_rules before this migration
    existed — e.g. a maintainer who invoked it by hand before entrypoint.sh
    called it automatically. Without this, deploying the fix would find no
    marker and duplicate the INITIAL_RULES on top of what is already there.
    """

    def setUp(self):
        from django.apps import apps

        self.mark_seeded = importlib.import_module(
            "projects.migrations.0009_rulesseeded"
        ).mark_seeded_if_rules_already_exist
        self.apps = apps

    def test_marks_seeded_when_rules_already_exist(self):
        PlannerRule.objects.create(text="Vorhandene Regel", active=True, order=0)
        self.mark_seeded(self.apps, None)
        self.assertTrue(RulesSeeded.objects.exists())

    def test_is_a_no_op_on_a_fresh_empty_table(self):
        self.mark_seeded(self.apps, None)
        self.assertFalse(RulesSeeded.objects.exists())
