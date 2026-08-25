"""Storage for the planning rules that go into the Claude prompt.

Two backends behind one interface (#22). In production the rules are the
global PlannerRule table the maintainer curates, seeded once from
INITIAL_RULES via the seed_rules management command. In demo mode they live
in request.session instead — the same per-session stance as demo_plan,
demo_project_type, demo_sim_date and demo_timelapse_moments, so one anonymous
visitor cannot rewrite the rules every other visitor's plan is generated
with — and start empty rather than seeded from INITIAL_RULES, so a visitor's
example project is planned with their own rules, not the maintainer's
concert-specific ones (#105).

Every public function takes the request first and branches on DEMO_MODE, so
the views never learn which backend they are talking to. The session entries
are dicts with the same 'id' / 'text' / 'active' keys the template reads off
the model, which is why planner_rules.html renders both unchanged.
"""

from django.conf import settings

from .models import PlannerRule

# The rule texts are German on purpose: they go into the Claude prompt and are
# shown in the UI. Seeds the production PlannerRule table only (via
# seed_rules) — the demo session starts empty instead (#105). project_types
# names PLANNER_TILES types (planner_views.py); an empty list means the rule
# applies to all of them.
INITIAL_RULES = [
    {
        "text": "Bei Konzertveranstaltungen GEMA-Meldung einplanen — nicht bei Gottesdiensten",
        "project_types": ["konzert"],
    },
    {
        "text": "Bei externen Mitwirkenden: Verträge, Honorare und Fahrtkosten einplanen",
        "project_types": [],
    },
    {
        "text": "Bei Konzerten Plakat als Standard voraussetzen; bei Gottesdiensten genügt ein Hausausdruck",
        "project_types": ["konzert"],
    },
    {
        "text": "Vorverkauf nur bei größeren Konzerten relevant",
        "project_types": ["konzert"],
    },
    {
        "text": "Bei Recruiting / Personalplanung: Stellenausschreibung, Bewerbungsschluss, Interview-Runden, Referenzcheck und Angebot einplanen",
        "project_types": ["recruiting"],
    },
]

DEMO_RULES_KEY = "demo_rules"


# --- Session backend ---


def _seed():
    """The demo starts with no rules — a visitor's own example plan should not
    be built from the maintainer's concert-specific production rules (#105).
    The rules feature (add/edit/toggle/reorder/delete) still works from here,
    it just has nothing to show until the visitor adds their own."""
    return []


def _is_valid(rules):
    return isinstance(rules, list) and all(
        isinstance(r, dict)
        and isinstance(r.get("id"), int)
        and isinstance(r.get("text"), str)
        and isinstance(r.get("active"), bool)
        and isinstance(r.get("project_types"), list)
        and all(isinstance(t, str) for t in r["project_types"])
        for r in rules
    )


def _session_rules(request):
    """The visitor's own rules, seeded but deliberately not persisted.

    Seeding does not write the session, so merely reading the rules page — a
    GET, and the demo is public and uncrawled (#27) — creates no session row.
    Every mutating function below saves right afterwards anyway.

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


def _applies(rule_project_types, project_type):
    return not rule_project_types or project_type in rule_project_types


def get_visible_rules(request, project_type):
    """The rules to show on the rules page for project_type.

    In demo mode this is scoped to project_type — a visitor planning a
    Workshop should not see (or be confused by) a Konzert-only rule they, or
    an earlier visit in the same session, added while planning something else
    (#105). Without a project_type (the rules page reached without having
    picked a tile yet) every rule is shown, since there is nothing to scope
    by. In production every rule is always shown — the maintainer curates the
    whole set together, across every project type at once.
    """
    rules = get_rules(request)
    if settings.DEMO_MODE and project_type:
        rules = [r for r in rules if _applies(r["project_types"], project_type)]
    return rules


def get_active_rule_texts(request, project_type):
    """The texts the planner prompt is built from — active rules that apply
    to project_type (an empty project_types list applies to every type)."""
    if settings.DEMO_MODE:
        return [
            r["text"]
            for r in _session_rules(request)
            if r["active"] and _applies(r["project_types"], project_type)
        ]
    return [
        r.text
        for r in PlannerRule.objects.filter(active=True)
        if _applies(r.project_types, project_type)
    ]


def add_rule(request, text, project_types=None):
    project_types = project_types or []
    if settings.DEMO_MODE:
        rules = _session_rules(request)
        next_id = max((r["id"] for r in rules), default=0) + 1
        _save_session_rules(
            request,
            rules
            + [
                {
                    "id": next_id,
                    "text": text,
                    "active": True,
                    "project_types": project_types,
                }
            ],
        )
        return
    last = PlannerRule.objects.order_by("-order").first()
    next_order = (last.order + 1) if last else 0
    PlannerRule.objects.create(
        text=text, active=True, order=next_order, project_types=project_types
    )


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


def update_rule(request, rule_id, text, project_types=None):
    """Returns False if there is no such rule. An empty text is ignored.
    project_types=None means "not sent" and leaves the existing assignment
    untouched."""
    if settings.DEMO_MODE:
        rules = _session_rules(request)
        rule = _find(rules, rule_id)
        if rule is None:
            return False
        changed = False
        if text:
            rule["text"] = text
            changed = True
        if project_types is not None:
            rule["project_types"] = project_types
            changed = True
        if changed:
            _save_session_rules(request, rules)
        return True
    rule = PlannerRule.objects.filter(pk=rule_id).first()
    if rule is None:
        return False
    changed = False
    if text:
        rule.text = text
        changed = True
    if project_types is not None:
        rule.project_types = project_types
        changed = True
    if changed:
        rule.save()
    return True


def delete_rule(request, rule_id):
    """Returns False if there is no such rule."""
    if settings.DEMO_MODE:
        rules = _session_rules(request)
        remaining = [r for r in rules if r["id"] != rule_id]
        if len(remaining) == len(rules):
            return False
        _save_session_rules(request, remaining)
        return True
    deleted, _ = PlannerRule.objects.filter(pk=rule_id).delete()
    return deleted > 0


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
