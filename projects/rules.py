"""Storage for the planning rules that go into the Claude prompt.

Two backends behind one interface (#22). In production the rules are the
global PlannerRule table the maintainer curates. In demo mode they live in
request.session instead, seeded from INITIAL_RULES — the same per-session
stance as demo_plan, demo_project_type, demo_sim_date and
demo_timelapse_moments, so one anonymous visitor cannot rewrite the rules
every other visitor's plan is generated with.

Every public function takes the request first and branches on DEMO_MODE, so
the views never learn which backend they are talking to. The session entries
are dicts with the same 'id' / 'text' / 'active' keys the template reads off
the model, which is why planner_rules.html renders both unchanged.
"""

from django.conf import settings

from .models import PlannerRule

# The rule texts are German on purpose: they go into the Claude prompt and are
# shown in the UI. Kept here rather than in the management command so an empty
# rule set is never the effective state (#22).
INITIAL_RULES = [
    "Bei Konzertveranstaltungen GEMA-Meldung einplanen — nicht bei Gottesdiensten",
    "Bei externen Mitwirkenden: Verträge, Honorare und Fahrtkosten einplanen",
    "Bei Konzerten Plakat als Standard voraussetzen; bei Gottesdiensten genügt ein Hausausdruck",
    "Vorverkauf nur bei größeren Konzerten relevant",
    "Bei Recruiting / Personalplanung: Stellenausschreibung, Bewerbungsschluss, Interview-Runden, Referenzcheck und Angebot einplanen",
]

DEMO_RULES_KEY = "demo_rules"


# --- Session backend ---


def _seed():
    return [
        {"id": i + 1, "text": text, "active": True}
        for i, text in enumerate(INITIAL_RULES)
    ]


def _is_valid(rules):
    return isinstance(rules, list) and all(
        isinstance(r, dict)
        and isinstance(r.get("id"), int)
        and isinstance(r.get("text"), str)
        and isinstance(r.get("active"), bool)
        for r in rules
    )


def _session_rules(request):
    """The visitor's own rules, seeded but deliberately not persisted.

    Seeding does not write the session, so merely reading the rules page — a
    GET, and the demo is public and uncrawled (#27) — creates no session row.
    Every mutating function below saves right afterwards anyway, and _seed()
    hands out the same ids every time, so a later toggle of id 3 still resolves
    against an unsaved seed.

    Self-healing like _get_sim_date / _allowed_sim_dates (views.py): a session
    written by an older version — or by hand — can hold anything, and the rules
    page is not worth a 500. Anything that isn't a list of well-formed entries
    is replaced by a fresh seed.
    """
    rules = request.session.get(DEMO_RULES_KEY)
    if rules is None or not _is_valid(rules):
        rules = _seed()
    return rules


def _save_session_rules(request, rules):
    # Reassigning is what marks the session dirty — mutating the list in place
    # leaves the change unsaved.
    request.session[DEMO_RULES_KEY] = rules


def _find(rules, rule_id):
    for rule in rules:
        if rule["id"] == rule_id:
            return rule
    return None


# --- Public interface ---


def get_rules(request):
    """All rules, active and inactive, in display order."""
    if settings.DEMO_MODE:
        return _session_rules(request)
    return list(PlannerRule.objects.all())


def get_active_rule_texts(request):
    """The texts the planner prompt is built from — active rules only."""
    if settings.DEMO_MODE:
        return [r["text"] for r in _session_rules(request) if r["active"]]
    return list(PlannerRule.objects.filter(active=True).values_list("text", flat=True))


def add_rule(request, text):
    if settings.DEMO_MODE:
        rules = _session_rules(request)
        next_id = max((r["id"] for r in rules), default=0) + 1
        _save_session_rules(
            request, rules + [{"id": next_id, "text": text, "active": True}]
        )
        return
    last = PlannerRule.objects.order_by("-order").first()
    next_order = (last.order + 1) if last else 0
    PlannerRule.objects.create(text=text, active=True, order=next_order)


def toggle_rule(request, rule_id):
    """Returns the new active value, or None if there is no such rule."""
    if settings.DEMO_MODE:
        rules = _session_rules(request)
        rule = _find(rules, rule_id)
        if rule is None:
            return None
        rule["active"] = not rule["active"]
        _save_session_rules(request, rules)
        return rule["active"]
    rule = PlannerRule.objects.filter(pk=rule_id).first()
    if rule is None:
        return None
    rule.active = not rule.active
    rule.save()
    return rule.active


def update_rule(request, rule_id, text):
    """Returns False if there is no such rule. An empty text is ignored."""
    if settings.DEMO_MODE:
        rules = _session_rules(request)
        rule = _find(rules, rule_id)
        if rule is None:
            return False
        if text:
            rule["text"] = text
            _save_session_rules(request, rules)
        return True
    rule = PlannerRule.objects.filter(pk=rule_id).first()
    if rule is None:
        return False
    if text:
        rule.text = text
        rule.save()
    return True


def delete_rule(request, rule_id):
    if settings.DEMO_MODE:
        rules = _session_rules(request)
        _save_session_rules(request, [r for r in rules if r["id"] != rule_id])
        return
    PlannerRule.objects.filter(pk=rule_id).delete()


def reorder_rules(request, ids):
    """Applies the order the drag-and-drop list posted.

    The ids arrive as strings from the browser. In the session backend there is
    no order field — list order *is* the ordering — so unknown ids are dropped
    and rules the payload does not mention keep their relative order at the end.
    """
    ids = [int(i) for i in ids if str(i).lstrip("-").isdigit()]
    if settings.DEMO_MODE:
        rules = _session_rules(request)
        by_id = {r["id"]: r for r in rules}
        ordered = [by_id.pop(i) for i in dict.fromkeys(ids) if i in by_id]
        _save_session_rules(request, ordered + [r for r in rules if r["id"] in by_id])
        return
    for i, rule_id in enumerate(ids):
        PlannerRule.objects.filter(pk=rule_id).update(order=i)
