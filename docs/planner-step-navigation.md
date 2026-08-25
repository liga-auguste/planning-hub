# Planner Stepper: One Step Back

Implements [Issue #116](https://github.com/liga-auguste/planning-hub/issues/116).

## Context

The step tracker (`_progress.html`) was purely visual — its dots carried no links. Backward
navigation instead ran through a `.back` text link on each planner page, and every one of
those links jumped straight to `planner_start`, regardless of which step the visitor was
actually on. From Klärung that skipped Beschreiben; from Review it skipped both Beschreiben
and Klärung, discarding whatever the visitor had already typed.

`planner_questions` and `planner_review` had no GET handler to land on either: `planner_start`
rendered `planner_questions.html` inline on POST, and `planner_review`'s GET branch
unconditionally redirected to `planner_start`. Both gaps were already on record —
`docs/template-refactoring.md` (#1) flagged the missing GET handlers, and `docs/demo-mode.md`
(#7) explicitly deferred the free-text preservation as not worth the cost at the time. #116
reverses that call.

**Branch:** `fix/planner-stepper-one-step-back`

---

## Changes

- **New session keys** (`planner_views.py`, `DRAFT_SESSION_KEYS`): `planner_description`,
  `planner_questions_html`, `planner_answers`, `planner_review_state`. Shared by DEMO_MODE and
  production alike, unlike the `demo_*` keys already in use — the planner flow itself doesn't
  branch on mode. Cleared on two boundaries: a successful `planner_create` (the flow finished),
  and a `planner_start` visit with no `?type=` (the tile grid is the explicit "start over"
  point) — so an abandoned draft can't bleed into a later, unrelated attempt.
- **`/planner/questions/` is back.** It existed once, was removed as dead code (`1e8d206`)
  because nothing referenced it, and is reintroduced now with a real purpose: the target of the
  stepper's one-step-back link from Klärung. `planner_start`'s POST handling is untouched — it
  still renders `planner_questions.html` inline — the new view only adds a GET path that
  redisplays the same content from session, or redirects to `planner_start` if there's nothing
  to show.
- **`planner_review`'s GET branch** now renders the stored plan from `planner_review_state`
  when present, instead of unconditionally redirecting away — a refresh on Review no longer
  discards the visitor's edits.
- **`_progress.html`** takes an optional `back_url`. Only the step immediately before
  `active_step` becomes an `<a>`; every other step — including earlier "done" ones — stays a
  plain `<div>`, so a visitor can only ever go back exactly one step at a time.
- **Back-link labels now name their destination**, matching the convention `planner_start.html`
  already used for its own step-2 link ("← Projekttyp"): `planner_questions.html`'s link is
  "← Beschreiben", `planner_review.html`'s is "← Klärung" (previously "← Neu planen"). The
  one-click jump straight to Projekttyp from Review is gone; a full restart is still one click
  away via the wordmark.

## Verification

Full click-through in a browser (DEMO_MODE): Projekttyp → Beschreiben → Klärung → Review →
back to Klärung (questions and the typed answer both still there) → back to Beschreiben (the
description still in the textarea). `python manage.py test projects` — 422 tests, including the
new `StepperBackLinkTest`, `PlannerBackNavigationPreservesDataTest`, and
`PlannerFreshStartClearsStaleDraftTest`.
