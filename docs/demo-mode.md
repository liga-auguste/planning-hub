# Demo Mode Navigation

Implements [Issue #7](https://github.com/liga-auguste/planning-hub/issues/7).

## Context

In demo mode a visitor can see two different datasets on the same `dashboard` view: the 5
example projects from `get_demo_projects()`, and the one plan they generated themselves,
held in `request.session['demo_plan']`. Before this change, the sidebar and the page could
not reliably tell those two situations apart, which produced dead-end links (a "Mein Plan"
link shown when no plan existed, a CTA that always bounced back to the wrong view) and gave
no visual indication that the example projects are sample data rather than something real.
Section A of the original navigation cleanup (the sidebar link logic itself) landed earlier
in `ce1e494` as part of #39; this document covers the remaining scope: linking `/mein-plan/`
from the sidebar, fixing two more dead/hardcoded links, and adding a demo-data banner.

**Branch:** `fix/demo-mode-navigation`

---

## The four navigation states

`views.dashboard` computes four flags. `force_multi` reflects the URL only; `plan_exists`
reflects the session only; `has_session_plan` is derived from both, and `viewing_demo_data`
from `has_session_plan` in turn:

```python
force_multi = request.GET.get("mode") == "multi"
plan_exists = bool(request.session.get("demo_plan"))  # DEMO_MODE only
has_session_plan = plan_exists and not force_multi
viewing_demo_data = settings.DEMO_MODE and not has_session_plan
```

| # | Session plan | URL | `plan_exists` | `has_session_plan` | `force_multi` | `viewing_demo_data` | Sidebar badge |
|---|---|---|---|---|---|---|---|
| 1 | no | `/dashboard/` | False | False | False | True | "Beispieldaten" |
| 2 | no | `/dashboard/?mode=multi` | False | False | True | True | "Beispieldaten" |
| 3 | yes | `/dashboard/` | True | True | False | False | "Dein Plan" |
| 4 | yes | `/dashboard/?mode=multi` | True | False | True | True | "Beispieldaten" |

`has_session_plan` answers "is the visitor's own plan on screen right now?" — it drives the
time-lapse bar and suppresses the project name on Kanban cards, since there is only one
project to show. `plan_exists` answers "does a plan exist at all, regardless of what's on
screen?" — it decides whether the sidebar can offer a way back to it. `viewing_demo_data`
answers "is what's on screen the example fixtures?" — it drives the banner below and the
sidebar badge. All four states share the same `dashboard.html` template; only the sidebar and
the banner change.

**Sidebar badge (#183):** a permanent `.sidebar-mode-badge` under the "Demo" eyebrow label,
switched on `has_session_plan` alone (`"Dein Plan"` vs. `"Beispieldaten"`). It sits in
`{% block sidebar_content %}`, which is not duplicated between `view-overview` and
`view-today` — so unlike the banners below, it needed no separate fix to show up in both.

---

## The demo-data banner

`viewing_demo_data` is true in states 1, 2, and 4 — every state where the example projects,
not the visitor's own plan, are what's rendered. The banner, the time-lapse banner
(`{% if sim_date %}`) and the stale-data notice (`{% if data_unavailable %}`) all live in one
shared partial, `_status_banners.html`, `{% include %}`d identically at the top of both
`view-overview` and `view-today` (#183). Before that, each new banner had to be copied into
both view `<div>`s by hand — the demo banner was, and stayed missing from `view-today` for a
while, exactly the class of bug the shared partial closes off for future banners too. The
demo banner and the time-lapse banner remain mutually exclusive by construction, since one
needs `has_session_plan` and the other needs its negation. It does **not** reuse `.sim-banner`
— that surface is deliberately dark in both themes (see its comment in `base.css`), the right
weight for a cookie notice or an active time-lapse simulation, but too heavy for a banner that
sits on screen through most of a demo visit. `.demo-banner` copies `.sim-banner`'s layout with
`--color-bg-tertiary`/`--color-text-secondary` instead — the same neutral grey the sidebar's
active state and the task-context badges already use, rather than a new colour.

**One condition instead of six (#183):** every place that used to gate demo-data
write-protection off an inline `demo_mode and not has_session_plan` (or its negation) —
`_day_task_card.html`'s `no-drag` class, `_task_row.html`'s reschedule affordances, the
project-detail task rows and the SortableJS include in `dashboard.html` — now reads
`viewing_demo_data` (or `not viewing_demo_data`) instead, the same value already computed once
in `views.dashboard`. `{% include %}` without `only` passes the parent context through
automatically, so no new context key was needed to reach `_day_task_card.html`/`_task_row.html`.

**"Ohne Projekt" explanation (#183 Tier 3):** a session plan (state 3) is always tied to the
one project the planner just created, so it structurally never has an unassigned task to show
in the Heute view. Rather than leaving that silent, `view-today` shows a one-line note when
`has_session_plan` is true, right after the shared banner partial.

---

## Why `/mein-plan/` is sidebar-only

`/mein-plan/` (`my_plan` view) has been fully built since early on but was reachable only by
typing the URL. It is now linked from the sidebar in states 3 and 4 — wherever `plan_exists`
is true — as "☰ Plan als Liste". It is deliberately **not** linked from `planner_create`'s
redirect: right after generating a plan, the time-lapse bar on `/dashboard/` is the intended
highlight, and adding a second competing redirect target there would only add a decision the
visitor doesn't need yet.

## Why `my_plan.html` stays a standalone page

`my_plan.html` overrides `{% block full_page %}` from `base_dashboard.html` (see
[`template-refactoring.md`](template-refactoring.md)) and renders a focused, centered list
view with no sidebar at all — navigation runs through its own top-of-page links instead. This
is deliberate, not an oversight: the page's purpose is a clean, printable/downloadable plan
view, and a sidebar would compete with that. The decision is recorded as a comment above
`{% block body %}` in the template itself, matching how `docs/template-refactoring.md` point 2
already called this out.

---

## Other fixes in this branch

- **`my_plan.html`'s "Mehrprojekt-Dashboard ansehen" CTA** linked to plain `{% url 'dashboard' %}`.
  Since `my_plan` only ever renders with a session plan present, that always resolved to state 3
  (the visitor's own plan) rather than the example projects the label promises — it now appends
  `?mode=multi`.
- **`dashboard.html`'s production sidebar branch** hardcoded `href="/planner/"` instead of
  `{% url 'planner_start' %}`. The demo-mode branch already used the URL tag; this brings the
  production branch in line. No template regression is possible (`{% url %}` resolves to the
  identical string), so no test covers this one — it is a source-only correction.
- **`planner_rules.html`'s "← Planer" link** always returned to the empty tile-selection step,
  discarding whatever project type the visitor had already chosen. `rules_list()` now reads
  `request.session.get("demo_project_type", "")` — already written unconditionally by
  `planner_start`'s `?type=` handler — and passes it as `back_type`; the template appends
  `?type={{ back_type|urlencode }}` when set. Deliberately scoped to the *step* only, not the
  visitor's unsaved free text: `planner_questions.html` and `planner_review.html` share the
  identical back-link shape and stay untouched, since this codebase already removed a
  URL-carried draft-text mechanism once (#5/#72) in favor of an empty field, and a
  `history.back()` or session-draft mechanism would cost materially more for a gap the rest of
  the app already tolerates.

---

## Verification

```bash
python manage.py test projects
ruff check .
ruff format --check .
```

Manual click-through, fresh session:

1. `/` → "Mehrprojekt-Dashboard ansehen" → state 2: banner visible, no "Mein Plan" in the
   sidebar, "+ Projekt selbst planen" present.
2. `/dashboard/` bare → identical content and sidebar to step 1.
3. Plan via the planner → redirect to `/dashboard/` → state 3: time-lapse bar, no banner,
   sidebar shows "⊞ Mehrprojekt-Ansicht" and "☰ Plan als Liste".
4. "⊞ Mehrprojekt-Ansicht" → state 4: banner and "← Mein Plan" and still "☰ Plan als Liste".
5. "☰ Plan als Liste" → `/mein-plan/` → "Mehrprojekt-Dashboard ansehen →" now actually reaches
   `?mode=multi`.
6. `/planner/?type=konzert` → rules link → "← Planer" returns to `/planner/?type=konzert`, not
   the empty tile step.

`DEMO_MODE=false python manage.py runserver` — no regression: the production sidebar branch
still shows "↻ Sync mit Notion" / "+ Neue Veranstaltung", now via `{% url %}` instead of a
hardcoded path.
