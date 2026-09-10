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

| # | Session plan | URL | `plan_exists` | `has_session_plan` | `force_multi` | `viewing_demo_data` | Demo group's overview entry |
|---|---|---|---|---|---|---|---|
| 1 | no | `/dashboard/` | False | False | False | True | toggle, on screen |
| 2 | no | `/dashboard/?mode=multi` | False | False | True | True | toggle, on screen |
| 3 | yes | `/dashboard/` | True | True | False | False | link, jumps here |
| 4 | yes | `/dashboard/?mode=multi` | True | False | True | True | toggle, on screen |

`has_session_plan` answers "is the visitor's own plan on screen right now?" — it drives the
time-lapse bar and suppresses the project name on Kanban cards, since there is only one
project to show. `plan_exists` answers "does a plan exist at all, regardless of what's on
screen?" — it decides whether the sidebar can offer a way back to it. `viewing_demo_data`
answers "is what's on screen the example fixtures?" — it drives the banner below. All four
states share the same `dashboard.html` template; only the sidebar and the banner change.

**Sidebar grouping (#183):** a single "Demo" eyebrow plus one swapped nav link wasn't enough —
"Dashboard"/"Heute" render identically worded in every state while showing entirely different
data, and the one link that actually jumps to the other state sat undistinguished among links
that stay in the current one (state 3 mixed "Mehrprojekt-Dashboard" in among the visitor's own
plan actions; state 4 did the same in reverse). The sidebar now opens with a
`.sidebar-title` header per group — "Dein Projekt" and "Demo-Projekte" — both always
present, whichever data is on screen. Any link that jumps to the *other* state sits in the
group it belongs to, so switching context is never mixed in among actions that stay put.

**One view, one name.** Both groups list "Dashboard" — one view each, over a different set
of projects. The headings carry the difference, so no entry has to. The demo overview used to read
"Mehrprojekt-Dashboard" where it is the jump-in link and "Dashboard" where it is the fast
toggle for the view already on screen, so the same view changed its name as a visitor
moved between the two states.

**"Heute" is hidden under "Dein Projekt" for now (#240).** In its current shape it is a
cross-project work surface — overdue, due today, then the week — and "Dein Projekt" is one
project, so it re-sorted the tasks the dashboard above had just shown under three more
headings. Hidden rather than reworked in that cut: what the view should say about a single
project is a design question, and the branch was already carrying two issues.

The entry and the panel move as a pair — `dashboard.html` renders no `#view-today` for a
session plan and `_sidebar_nav.html` lists no "Heute", because an entry whose toggle has
nothing to toggle is a dead link that also throws. Bringing the view back for that state
means flipping both conditions; the JS reads `#view-today` once and asks whether it exists,
so nothing there has to move. Meanwhile "Plan als Liste", already in the group, is where a
single plan's tasks are read in full. `TodayViewHiddenForASessionPlanTest`
(`test_week_view.py`) asserts both states, so that the hold stays a decision on the record
rather than drifting into a permanent absence nobody chose.

`SidebarModeGroupingTest` asserts the states together rather than one per test: each
already had a test of its own, and the wording drifted apart anyway, because nothing
compared them. Those assertions read one group at a time (`_sidebar_group`) — a bare
substring check cannot tell the groups apart now that both say "Dashboard", which is the
point of the grouping.

The heading is "Demo-Projekte" rather than "Demo" for the same reason: it names what the
group holds, not the mode it belongs to.

"Mehrprojekt-Dashboard" is unchanged where there is no heading to lean on — the landing
page and `/mein-plan/`'s CTA, whose link text has to name the view and the data at once.
That is #48's wording, settled before this partial had groups.

**Still inconsistent, deliberately deferred:** the icon. The entry carries a dot where it
is the toggle and `⊞` where it is the jump-in link. Same defect as the wording, left for a
separate pass. This lives in `{% block sidebar_content %}`, which is not
duplicated between `view-overview` and `view-today` — so unlike the banners below, it needed
no separate fix to show up in both.

---

## The demo-data banner

`viewing_demo_data` is true in states 1, 2, and 4 — every state where the example projects,
not the visitor's own plan, are what's rendered. The banner, the sim-date notice
(`{% if sim_date %}`) and the stale-data notice (`{% if data_unavailable %}`) all live in one
shared partial, `_status_banners.html`, `{% include %}`d identically at the top of both
`view-overview` and `view-today` (#183). Before that, each new banner had to be copied into
both view `<div>`s by hand — the demo banner was, and stayed missing from `view-today` for a
while, exactly the class of bug the shared partial closes off for future banners too. The
demo banner and the sim-date notice remain mutually exclusive by construction, since one
needs `has_session_plan` and the other needs its negation. It does **not** reuse `.sim-banner`
— that surface is deliberately dark in both themes (see its comment in `base.css`), the right
weight for a cookie notice or an active time-lapse simulation, but too heavy for a banner that
sits on screen through most of a demo visit. `.demo-banner` copies `.sim-banner`'s layout with
`--color-bg-tertiary`/`--color-text-secondary` instead — the same neutral grey the sidebar's
active state and the task-context badges already use, rather than a new colour.

**The Zeitreise bar itself (#183 follow-up):** the sim-date *notice* (passive: "you're looking
at a simulated date") is one thing; the Zeitreise *bar* (active: buttons to jump between
moments) is another, and had the identical duplication bug — only in `view-overview`, so
switching to "Heute" lost the ability to change the simulated date at all. Extracted into its
own `_timelapse_bar.html` partial, included at the top of both views, same as the status
banners. Since it rendered twice, its container elements lost their page-unique
`id="timelapse-bar"`/`id="timelapse-moments"`/`id="btn-today"` (duplicate IDs are invalid HTML,
and `getElementById` only ever finds the first) — the populating JS uses
`document.querySelectorAll('.timelapse-bar')` and builds the moment buttons into every
instance found, so every bar stays in sync from the same `TIMELAPSE_MOMENTS`/`SIM_DATE` data.

It is at one instance while #240 hides "Heute" for a session plan: the bar needs
`has_session_plan`, which is exactly the state the view is not rendered in, so the second
include could not produce anything and reads as dead code. It belongs back beside the view
whenever the view returns — nothing about #183's reasoning changed. The class-based selector
stays either way, since it is right at any count, and `TimelapseBarRendersOncePerPageTest`
pins the current one so a second copy has to mean the panel is back rather than a duplicate
having crept in.

**One condition instead of six (#183):** every place that used to gate demo-data
write-protection off an inline `demo_mode and not has_session_plan` (or its negation) —
`_day_task_card.html`'s `no-drag` class, `_task_row.html`'s reschedule affordances, the
project-detail task rows and the SortableJS include in `dashboard.html` — now reads
`viewing_demo_data` (or `not viewing_demo_data`) instead, the same value already computed once
in `views.dashboard`. `{% include %}` without `only` passes the parent context through
automatically, so no new context key was needed to reach `_day_task_card.html`/`_task_row.html`.

**No "Ohne Projekt" explanation for a session plan:** a session plan (state 3) is always tied
to the one project the planner just created, so it structurally never has an unassigned task to
show in the Heute view. #183 Tier 3 originally added a one-line note explaining the absence —
removed again on live feedback: in the demo instance there's only ever the visitor's one new
project and the example projects, so nothing sets up an expectation of an "Ohne Projekt" bucket
in the first place. Explaining the absence of something nobody expected wasn't useful. #240
hid the whole view for that state, for the same reason one step further out: the bucket was
not the only part of it shaped around having several projects.

**The overview progress bar tracks the whole plan for a session plan (#183 follow-up):**
everywhere else it's week-scoped (#182), but for `has_session_plan` a week-scoped count barely
moved between Zeitreise moments — a session plan's tasks rarely all fall in one calendar week,
so the bar often sat at 0/0 several moments in a row. `dashboard()` marks every task due on or
before the simulated moment "done" on a deep copy (`views.py`, right where `sim_date` is read),
so a whole-plan count does visibly progress as you scrub through moments; the label switches
to "Projektfortschritt" so it doesn't keep promising a week-scoped number it no longer shows.

---

## Why `/mein-plan/` is sidebar-only

`/mein-plan/` (`my_plan` view) has been fully built since early on but was reachable only by
typing the URL. It is now linked from the sidebar in states 3 and 4 — wherever `plan_exists`
is true — as "☰ Plan als Liste". It is deliberately **not** linked from `planner_create`'s
redirect: right after generating a plan, the time-lapse bar on `/dashboard/` is the intended
highlight, and adding a second competing redirect target there would only add a decision the
visitor doesn't need yet.

## `my_plan.html` now keeps the sidebar (#183 follow-up)

Originally `my_plan.html` overrode `{% block body %}` from `base_dashboard.html` entirely — a
focused, centered list view with no sidebar, navigation running through its own top-of-page
logo/back-link instead. That was deliberate at the time: the sidebar's own navigation didn't
yet exist in a form worth reusing here.

It doesn't hold once the sidebar is the thing meant to guide a visitor through the whole app
(#183's Tier 1/2 follow-ups): a page reachable *from* the sidebar ("Plan als Liste") that then
drops the sidebar entirely defeats that. `my_plan.html`, `close_week_start.html` and
`week_review.html` all use the normal `sidebar_content`/`content` blocks now, `{% include
"projects/_sidebar_nav.html" %}`d the same way `dashboard.html` does — see `_sidebar_nav.html`'s
own comment for how `active_nav` adapts it (every entry that is a client-side toggle on
`dashboard.html` becomes a real link, since `view-overview`/`view-today` don't exist on these
pages; whichever of "Plan als Liste"/"Woche abschließen" matches the current page is marked
active).

`stats.html` is the one exception, left as a standalone page on purpose: it's a maintainer-only
usage-stats view linked from nowhere in the UI, so no sidebar click ever leads there — the
"guides you through the app" rationale doesn't apply to a page nothing points at.

This move also closed two of #129's four findings before they were worked on: `/mein-plan/`
had no link back to `/dashboard/`, and it was missing the "Über dieses Projekt" overlay that
`dashboard.html` carries. Both came free with `_sidebar_nav.html` and `_about_overlay.html`,
and `SidebarNavOnStandalonePagesTest` covers them.

## The landing page is session-aware (#129)

`landing.html`'s CTAs used to be static: "Eigenes Projekt planen" always went to
`planner_start`, "Mehrprojekt-Dashboard ansehen" always to `/dashboard/?mode=multi`. A visitor
who had already run the planner therefore had no route from the landing page back to their own
Übersicht — and the one offer that page did make replaces the plan they came back for.

`index()` now passes `plan_exists` (`bool(request.session.get("demo_plan"))`), the same flag
`dashboard()` computes. Deliberately not `has_session_plan`: the landing page shows no plan at
all, so the only question it can ask is "does one exist", which is exactly the split the four
navigation states above draw between the two.

With a plan in session the desktop action row gains "Mein Dashboard →" as the primary entry and
the planner link drops to secondary; the multi-project link is unchanged in both states. No new
CSS: `.action`, `.action.primary` and `.seq-cta-btn` already exist, and `.actions` already has
`flex-wrap` for the third button.

Mobile has one fixed slot (`.seq-cta-btn`) for three destinations, so one always loses. With a
plan it goes to the visitor's own dashboard, which costs the planner its landing-page entry
there. That is the cheap direction: the sidebar on the dashboard carries both the planner
("Projekt selbst planen") and the multi-project view, so every destination stays one click away
— which is not true if the CTA keeps pointing at the planner instead.

## A second plan replaces the first, and says so (#129)

`planner_create()` writes `request.session["demo_plan"]` unconditionally. It still does — the
demo has one dashboard slot, and a second session key plus a restore path costs more than it
buys. What changed is that the overwrite is no longer silent.

`planner_views._existing_plan_name(request)` returns the session plan's name through
`_strip_trailing_date` (the same cleaning every other surface applies), or `None` when there is
nothing to replace. In production it always returns `None`: `planner_create` writes to Notion
there, where a second project is simply a second project and nothing is being replaced.

The notice appears at both ends of the flow — the entry step (`planner_start.html`, both the
tile grid and the describe form) and the review step, directly above `.review-actions`. Step 2
(`planner_questions.html`) deliberately carries none: "at both ends" means on entry and
immediately before submitting, and a third copy in between would be nagging rather than
informing. It is not a confirm checkbox either, which would cost an extra click even for the
visitor who wants exactly this.

`.replace-notice` sits on the neutral chip surface (`--color-bg-tertiary` /
`--color-text-secondary`), not `.error-notice`'s red — nothing has gone wrong, this is a
statement about what the next click does. Same reasoning `.demo-banner` carries below.

Both changes were clicked through in a running demo session, light and dark, including two
full planner runs that ended with the second plan actually replacing the first. That pass is
also what caught a bug no test could: a Django `{# … #}` comment is single-line only, so the
two multiline comments added to `landing.html` here rendered as visible body text.
`TemplateCommentsNeverReachThePageTest` now sweeps the rendered public pages for `{#`.

## The sidebar's project list, not just its links (#185)

The above covered the nav *links* (Dashboard/Heute/Plan als Liste/Woche abschließen). The
"Projekte" block further down the sidebar — projects grouped by month, each with a progress
ring, `dashboard.html`'s own `showProject()` toggle — used to render only inside `dashboard.html`
itself. `my_plan.html`, `close_week_start.html` and `week_review.html` carried the nav links but
not this list, so leaving Dashboard still meant losing sight of every other project.

The block is now `projects/_sidebar_project_list.html`, included by all four pages. It branches
on `active_nav` exactly like `_sidebar_nav.html`'s own Dashboard/Heute links do: unset (rendering
inside `dashboard.html`) keeps the client-side `showProject()` toggle; set (the three standalone
pages) renders a real link into `{% url 'dashboard' %}?project=<id>` instead — `dashboard.html`'s
own deep-link handling already opens the right project detail from that query param, so no new
client-side code was needed on the standalone pages.

`views._sidebar_projects()` supplies `month_groups`/`years` to `close_week_start()` and
`week_review()`, mirroring what `dashboard()` already builds for itself: the visitor's own
session plan in demo mode (never the example catalog — that state isn't reachable from these
pages), or production's real projects. `my_plan()` builds its single-entry list inline, since it
already has everything needed from its own session-plan fetch.

### One Notion read per request

Where the projects come from differs per view, and the rule is "never read the same thing twice":

- `close_week_start()` already fetches projects for its triage list, deliberately uncached so a
  stale week can't misclassify it. It passes that fetch straight into `_sidebar_projects()`
  (`projects=`), so the sidebar costs nothing. `_current_projects_for_closeout()` returns projects
  rather than a flat task list for exactly this reason; the two callers flatten it themselves.
  Sharing the dicts is safe because `_annotate_tasks()` only adds `urgency` (which the triage
  template never renders) and rewrites `due_display` to the same value.
- `week_review()` has no fetch of its own, so `_sidebar_projects()` reads for it: `dashboard()`'s
  warm `CACHE_KEY` entry when there is one, a direct Notion call otherwise.

The distinction matters because `get_upcoming_projects()` is 1 + N requests (one per project for
its tasks, `notion._get_tasks`), and because the cold cache is the *normal* state in this flow —
every task toggle and every "→ nächste Woche" move calls `_bust_dashboard_cache()`. Either way a
`NotionUnavailableError` degrades to an empty list rather than breaking a page that exists for
reasons other than showing this list.

The cached projects are annotated in place rather than deep-copied first. Every Django cache
backend serializes on both `set` and `get` — the configured `DatabaseCache` stores a pickled blob
in a table — so `cache.get()` already returns an object graph no other request shares.
`dashboard()` has always relied on that (it writes `display_name` onto its own cached projects),
so a copy here would have paid for the same guarantee twice.

### A moment is a view, not a work surface

With a moment active `dashboard()` marks every task due on or before it done on a deep
copy, so the page is a picture of the plan at that date. Checking a task off there wrote
into `demo_plan` correctly and changed nothing visible: the render overrode it again on
the spot, so the clicked dot un-struck itself while the Kanban card, the week bar, the day
counters and the sidebar ring all still read the task as done — and a reload put the
strike-through back. A visitor cannot tell "nothing happened" from "it happened and you
cannot see it" (#217).

So the toggle is not offered while a moment is on. `_task_dot.html` renders the dot as a
plain `<span>` instead of the `<form>`/`<button>` — the status colour stays, the
affordance goes — and `toggle_task_view` refuses the POST with the same 404 the example
projects already get. Rescheduling stays: a new date visibly moves the task in or out of
the forced-done range, so it is not the same contradiction.

Removing the affordance was correct and silent. The moment also takes three ⋮ entries with
it — "Als erledigt markieren", "Umbenennen" and "In den Papierkorb" — so a visitor had four
write paths vanish and no word about any of them. Since #244 the page explains it three
times over: the banner names the consequence ("— hier lässt sich nichts abhaken"), a click
on a locked dot gets a short self-dismissing notice beside that dot with a way back to
today, and the menu says in one line which entries the moment removed. The protection
itself is unchanged. Full reasoning in
[`docs/dashboard-write-paths.md`](dashboard-write-paths.md).

### The Zeitreise stays a dashboard device

The sidebar's progress ring reads `timezone.localdate()` on the three standalone pages, so with a
simulated moment active it shows *real* progress there while `dashboard()`'s own ring shows the
simulated one. This is deliberate, not an oversight to fix later:

- The Zeitreise bar, the "⏱ Simulierter Zeitpunkt" banner and its "Zurück zu heute" reset all
  live on `dashboard.html` only. A simulated ring on a page with no banner and no way back would
  be a state the visitor can see but not explain or leave.
- `my_plan()` has always rendered its task list and its "x von y" count against the real date.
  Making only the sidebar ring simulation-aware would put it in direct conflict with the progress
  number in the body of the very same page — worse than the current split, not better.
- `close_week_start()`/`week_review()` write state keyed by the real ISO week (`WeekCloseout`,
  `is_week_closed`). A simulated clock there would mean closing out a simulated week, which is a
  product decision about what the close-out *is*, not a display detail.

Each page is internally consistent with its own notion of today; the boundary runs along the
dashboard, which is the one place the simulation is announced and reversible.

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
   sidebar shows "Demo-Projekte → Dashboard" and "☰ Plan als Liste".
4. That "Dashboard" → state 4: banner, and the entry still reads "Dashboard" under the
   same heading — that is the check, since it is the fast toggle here rather than a link —
   plus "☰ Plan als Liste".
5. "☰ Plan als Liste" → `/mein-plan/` → "Mehrprojekt-Dashboard ansehen →" now actually reaches
   `?mode=multi`.
6. `/planner/?type=konzert` → rules link → "← Planer" returns to `/planner/?type=konzert`, not
   the empty tile step.
7. Back to `/` with that plan still in session → three buttons, "Mein Dashboard" primary
   (mobile: the fixed CTA reads "Mein Dashboard").
8. "Eigenes Projekt planen" → the tile step names the existing plan and links to
   `/mein-plan/` → run the planner → the notice sits above "Zum Dashboard →" → submit → the
   old plan is gone and the new one is on the dashboard.

`DEMO_MODE=false python manage.py runserver` — no regression: the production sidebar branch
still shows "↻ Sync mit Notion" / "+ Neue Veranstaltung", now via `{% url %}` instead of a
hardcoded path.
