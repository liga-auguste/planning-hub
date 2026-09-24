# Dashboard Write Paths

Implements [Issue #210](https://github.com/liga-auguste/planning-hub/issues/210),
[Issue #199](https://github.com/liga-auguste/planning-hub/issues/199),
[Issue #217](https://github.com/liga-auguste/planning-hub/issues/217),
[Issue #233](https://github.com/liga-auguste/planning-hub/issues/233),
the pending half of [Issue #198](https://github.com/liga-auguste/planning-hub/issues/198)
and the client half of
[Issue #194](https://github.com/liga-auguste/planning-hub/issues/194).

## Context

The dashboard renders three views out of one document — the overview, the Heute view and
the per-project sections — plus a shared sidebar. (Two, for a demo session plan: "Heute"
spans projects and that state has one, so [#240](https://github.com/liga-auguste/planning-hub/issues/240)
hides it there for now. Everything below holds either way — the client sync addresses its
surfaces by selector, so a view that is absent simply matches nothing, and a view that comes
back needs no change here.) Each one grew
its own surface for task
state: the AI summary's checkboxes, the "Diese Woche" progress bar, the Kanban board and
its column counts, the Heute lists, the day columns and their counters, the sidebar
progress rings.

Every one of those was correct on load. None of them, except the two that existed when
[#122](https://github.com/liga-auguste/planning-hub/issues/122) wrote the DOM sync, was
carried forward to the write path. Checking a task off updated the copy you clicked and
left the rest of the page showing the same task as both done and not done — the Kanban
board still listing it under "Dringend", the day column's counter still on `0/3`. Only a
full reload restored agreement, and that reload was the most expensive read the app has.

The failure mode was additive: nobody broke anything, each new surface simply was not
told about the toggle. Hence the rule this change writes into the template:

> **A surface that shows task state gets updated on toggle, or it does not render task
> state.**

## The shape all three issues share

The server already knows the answer. It hands it back instead of the client re-deriving
it or the page reloading.

- A **write patches the cache** rather than invalidating it, so the data to derive from
  is still in memory (#199).
- **One helper derives every count**, called by the load path and by the write path, so
  the two cannot show different numbers for the same board (#210).
- **The client writes only what it was given** — no rule is implemented twice (#210,
  #194).

## Which write touches which cache

Four keys hold the dashboard's data: `CACHE_KEY` / `STALE_CACHE_KEY` for the projects and
the AI summary, `UNASSIGNED_CACHE_KEY` / `STALE_UNASSIGNED_CACHE_KEY` for the tasks with
no project of their own. The `STALE_*` pair never expires; it is what a failed Notion
read falls back to.

| Write | Projects | Summary | Fallback |
|---|---|---|---|
| Toggle a task | patched in place | kept | full bust |
| Rename a task | patched in place | kept | full bust |
| Reschedule a task | patched and re-sorted | dropped | full bust |
| Reschedule → postpone counter | patched in place | already dropped | full bust |
| Move a task to the trash | full bust | full bust | — |
| Create a project (planner) | full bust | full bust | — |

A rename ([#239](https://github.com/liga-auguste/planning-hub/issues/239)) sits with the
toggle: `_annotate_tasks` sorts by due date, so a new name moves nothing and the summary's
`task_refs` still point where they did.

A removal is the one write with no patch path, and that is a decision rather than an
omission. `_patch_cached_tasks` mutates in place and has no way to drop a task, and a
removal shifts every count *and* every cached `task_ref`, since
`_number_projects_and_tasks` numbers by position. `_remap_summary_refs` exists for exactly
that and could carry it, but `_patch_cached_tasks` would have to give up its
`mutate(task)` signature to get there — and that is the one place every other write hangs
off. A removal is rare; the bust costs one Notion read and, because the summary lives in
the same entry, one Claude call. The answer therefore carries no figures and the client
reloads, which is the rule above in its strongest form: nothing on the page is left to
reconcile by hand.

`_patch_cached_tasks(task_id, mutate, today)` applies `mutate` to
every cached copy of one task and re-runs `_annotate_tasks` on top of it. Each
`cache.get` hands back its own deserialized object graph, so all four entries are patched
separately.

Three rules govern it:

1. **A patch never serves a state predating a confirmed write.** A stale snapshot that
   does not carry the task at all cannot be corrected, so it is deleted rather than left
   in place — but only the snapshot that *would* carry it. A project task is never in
   `STALE_UNASSIGNED_CACHE_KEY` and a project-less one is never in `STALE_CACHE_KEY`, so
   a miss in one says nothing about the other, and the two exist without each other often
   enough for that to matter: `dashboard()` writes `STALE_CACHE_KEY` only when the summary
   is not `None`, so one Claude outage leaves the project-less copy alone in the cache.
2. **A patch never extends the entry's life.** See below.
3. **The fallback is the normal path, not an edge case.** A cold cache, a half-cold
   cache, a task no cached list carries, or an entry whose deadline has run out all
   return `None`, and the caller busts exactly as before.

### A patch puts the entry back, it does not renew it

A delete needs no timeout; a re-write does. Naming `CACHE_TTL` there renewed the eight
hours on every checkbox — check one task off per working day and the dashboard never
performs an unforced Notion read again, so anything edited in Notion's own UI stays
invisible for as long as the patching continues. That is not a small gap: this app is
not the only writer, which is why `_count_done_in_range` accounts for a task "checked
off directly in Notion's own UI". `↻ Aktualisieren` deletes `CACHE_KEY` alone, so the
project-less list would have had no manual escape at all (#216).

The TTL is a freshness policy about the *read*, not about the last write, so a fresh
Notion read stamps the moment its entry falls due — `CACHE_DEADLINE_KEY` and
`UNASSIGNED_CACHE_DEADLINE_KEY`, one per pair, because the two are independent reads
whose deadlines drift apart whenever one of them fails alone. `_cache_fresh_read` is
the only writer allowed to move a stamp; every later write asks `_remaining_ttl` what
is left and names that. Django's cache API has no portable "how long has this entry
got left", which is why the deadline is recorded rather than read back.

`None` from `_remaining_ttl` — no stamp (the first request after this deploy) or one
already passed — means the entry cannot go back without outliving its read, so the
caller busts. The stamps are deliberately unversioned: they hold a bare deadline and
no task shape, so a pre-deploy entry cannot misrender, only be absent, and absent
already means "cannot patch safely".

Regenerating a dropped summary follows the same rule. Those projects came out of the
cache, not out of Notion — only the summary is new — so the entry goes back with what
its deadline has left, and past due it is not written back at all: it is seconds from
expiring anyway, and the stale copy keeps the summary the Claude call paid for.

### The regenerated summary attaches to the projects, it does not carry its own

`generate_weekly_summary` takes seconds, and that branch used to write back the
`projects` it had read *before* the call. A toggle confirmed in Notion inside that
window was discarded by the write-back, and the cache went on serving a task as open
that Notion had as done — the one thing `_patch_cached_tasks` promises against, and
for the rest of the entry's life rather than until the next bust (#216).

`_attach_regenerated_summary(numbered_against, summary_data)` re-reads `CACHE_KEY`
after the call and writes the summary onto whatever the entry holds now. Only the
summary is the regenerating request's to contribute; the projects belong to whoever
wrote last. Two cases withhold it entirely:

- **The cache was busted meanwhile.** Writing the entry back would restore exactly
  the state the bust discarded, so nothing is written and the next load refetches.
- **The numbering moved.** `task_refs` are positions in the order
  `_number_projects_and_tasks` (`ai.py`) establishes, so a reschedule landing during
  the call leaves them pointing at the wrong tasks — *in range*, and therefore
  rendered rather than dropped by `resolve_weekly_summary`. `_summary_ref_order`
  reads that order through the same helper rather than rebuilding it, so the check
  cannot drift from the numbering it checks. A toggle moves nothing, so its patch
  keeps the order and the summary still fits — which is the whole reason a toggle and
  a reschedule are treated differently one section down.

The same race exists, unfixed, in the cold-cache branch beside it: `_fetch_fresh_data`
is equally slow and its projects genuinely are new, so a re-read cannot resolve it —
that needs a write fence, and it predates this issue.

### Why a toggle keeps its summary and a reschedule does not

`_annotate_tasks` sorts tasks chronologically and deliberately keeps `done` out of the
sort key. A toggle therefore moves no task. The summary's `task_refs` are positions in
that order (`_number_projects_and_tasks`, `ai.py`), so they stay valid and the summary
survives untouched.

A new date does move the task, which renumbers every reference after it. That summary
cannot be salvaged. `CACHE_KEY` holds `(projects, summary_data)` as one tuple, so
"invalidate only the summary" means writing `(patched_projects, None)` — and a cache hit
in that shape now means *the projects are good, regenerate the summary*, written back
afterwards. Smaller than splitting the summary into its own key, and it matches the cost
this already accepts: the Notion read goes, the Claude call stays.

## Where each surface gets its number

`_derive_dashboard_figures(projects, unassigned_tasks, effective_today, browsed_monday,
whole_plan=False)` is the single source. `dashboard()` renders from it; both writes answer
with it through `_surface_figures`, which adds the ring of the project the write landed
in. On the client the counts are written by one `applyFigures()`; each write then moves
only the cards its own kind of move displaces.

| Surface | Field | Membership rule |
|---|---|---|
| "Diese Woche" bar and label | `week` | `_count_done_in_range` over project tasks in the current ISO week. Excludes project-less tasks — the board below it can never show one (#182) |
| Day-column counters | `days[iso]` | Tasks due on that day, project-less ones included — the column shows them |
| Kanban column counts | `kanban` | `_KANBAN_COLUMN[task["urgency"]]` over project tasks |
| Sidebar rings | `projects[id]` | `done_count / total_count` per project, as `stroke-dashoffset` |

In a demo session the bar counts the **whole plan** rather than the week (`whole_plan`):
a week-scoped count barely moved between Zeitreise moments, often showing `0/0` several
in a row (#183).

Counts cannot be recomputed in the browser, and this is the reason rather than a
preference: `_count_done_in_range` admits a task whose due date falls in the range **or**
that was completed in it. Checking a task off can therefore raise the **denominator** —
an overdue task from an earlier week, cleared today, joins this week's total without a
single card moving on screen.

Both write requests carry `week_start`, the Monday of the week the day columns are
showing. `?week=` navigates them to any week and the server cannot guess which one is on
screen.

Both that field and `?week=` funnel through `_usable_week_start`, which rejects a
Monday within seven days of `date.min` / `date.max` (#216). `_bucket_by_day` walks a
week forward from the Monday it is given and `dashboard()` reaches a week either side
for the navigation links, so such a Monday raises `OverflowError` instead of rendering.
It parses, so neither parser's existing "unparseable" guard saw it: `?week=9999-W52`
took the whole page down, and `week_start` did it *after* the Notion write had been
confirmed — a 500 the client reads as "it failed" for a write that happened. Out of
range is one more value these parsers cannot use, handled where they already handle
the others.

## Which writes are offered at all

A write is offered where it takes effect, and refused where it would not — the same rule
either way, so a click never has to be interpreted.

| Situation | Toggle | Reschedule |
|---|---|---|
| Production, a Notion task | yes | yes |
| A demo session's own plan | yes | yes |
| A demo example project | no — in no session, 404 (#61) | no — in no session, 404 (#10 §5) |
| A demo session under a Zeitreise moment, on the dashboard (`task/<id>/toggle/`) | no — read-only, 404 (#217) | yes |
| A demo session under a Zeitreise moment, on `/mein-plan/` (`session-task/<id>/toggle/`) | yes — the page renders the real date (#246) | yes |

The last two rows are the ones that are not about persistence, and they are the same
rule reaching opposite answers. `dashboard()` renders a moment by
forcing every task due by `sim_date` to done on a deep copy — that is what a moment *is*,
a picture of the plan at that date. A toggle under one wrote into the session correctly
and every derived number correctly ignored it, because the render overrides it anyway. So
the write persisted and nothing on screen moved, and a reload put the strike-through back.
A visitor cannot tell "nothing happened" from "it happened and you cannot see it".

`_task_dot.html` is the single place that decides it: with `sim_date` set the dot renders
as a `<span>`, without it as the `<form>` and `button` it always was. Four surfaces include
it — the AI summary's list item, the project section's task row, `_task_row.html`'s Heute
rows and `_day_task_card.html`'s day card — for the same reason `applyTaskDone` keeps one
selector list rather than four call sites. Only `button.dot` carries `cursor`, `border` and
`:hover`, so the span keeps the status colour and loses exactly the affordance;
`.ai-card span.dot` picks up the 2px the vanished form used to contribute.
`toggle_task_view` refuses the same case server-side, before it writes, so a POST that
arrives anyway gets the honest miss rather than a silent one.

Rescheduling stays available: a new date visibly moves the task in or out of the
forced-done range, so it is not the contradiction the toggle is.

### The other toggle route keeps its button, and that is the same rule

`/mein-plan/` drives `session-task/<id>/toggle/` (`toggle_session_task`), and that route
carries no `sim_date` guard. Read as a rule about the session plan, the asymmetry looks
like an oversight — the same plan, the same `done` field, one route refusing and the other
writing (#246). It is not. The rule at the top of this section is about the *surface*, and
the two surfaces differ in exactly the way the rule cares about: `my_plan()` never reads
`sim_date`, so its list, its counter and its progress bar all render the real state on the
real date. A toggle there is visible precisely where it is made, and it stays visible after
a reload. Guarding it would refuse a write whose effect is on screen — #217's failure with
the sign flipped.

So both routes are correct and they answer differently because they are asked different
questions. `TheSessionToggleStaysLiveDuringAMomentTest` (`test_timelapse.py`) pins it, so a
later consistency fix has to argue with a red test rather than with an absence.

What #246 found genuinely missing is the other half: `/mein-plan/` left the moment without
a word, so the task the dashboard renders forced-done stood open on the list with nothing
naming why — "a state the visitor can see but not explain or leave", the failure
`docs/demo-mode.md` already warns about. That is answered by a notice on the page, not by a
guard on the route: see [`docs/demo-mode.md`](demo-mode.md), "The Zeitreise stays a
dashboard device".

### What the page says about it

Refusing correctly is only half of it. Until #244 the moment removed four write paths —
the dot, and the ⋮ menu's "Als erledigt markieren", "Umbenennen" and "In den Papierkorb" —
and named none of them, so a visitor who clicked the way they had just learned to got no
refusal, no hint and no cursor change on the way in. The protection is unchanged; the
explanation is added where the attempt is made:

| Where | What it says | Reaches |
|---|---|---|
| `#sim-lock-notice` (`dashboard.html`) | "Im simulierten Zeitpunkt lässt sich nichts abhaken." plus "Heute anzeigen" | the visitor who actually clicked a locked dot |
| `.task-menu-note` (`_task_actions_menu.html`) | "Im simulierten Zeitpunkt nicht verfügbar: Abhaken, Umbenennen, Papierkorb." | the visitor who opens the menu instead |

The banner is deliberately not a third row in that table. #244 shipped with one — it
named the consequence too ("⏱ Simulierter Zeitpunkt: … — hier lässt sich nichts
abhaken.") on the grounds that it reaches everyone, including a visitor who never
clicks. That is exactly what was wrong with it: the banner is on screen the whole time a
moment is on, so the clause announced a refusal continuously, to every visitor, most of
whom were never going to reach for a write. An answer that arrives before the question
is noise, and it made the banner a warning rather than a label. The banner names the
state; the two surfaces above answer the attempt. Both spell the moment inflected and
lowercase ("Im simulierten Zeitpunkt"), so #153's rule that the simulated date is named
exactly once still holds against the banner's own label.

The notice is one element, moved to whichever dot was clicked by a listener delegated on
`document` — the same reason `applyTaskDone` keeps one selector list rather than four call
sites, and what covers all four dot surfaces without `_task_dot.html` gaining anything.
During a moment every `span.dot` on the page is a locked dot, and the element only exists
during a moment at all. It lives in `dashboard.html` beside the page-owned
`{% csrf_token %}`, not in `_status_banners.html`, because that partial is included twice
and an `id=` in it would inherit which include happened to render. Every attempt is
answered, not only the first per moment: the 5s timer restarts rather than an
"already shown" flag being set.

A `<span>` is not focusable, so a keyboard visitor never triggers the click notice. Making
it focusable would hand back the affordance #217 removed, so that is not the answer
either. What is left for them is the menu note, reached by tabbing to the ⋮ trigger — and
that is thinner than it looks, because `.task-menu-note` is a plain `<div>` inside
`role="menu"`, which a screen reader in menu mode may skip entirely.

That is a decision, not a loose end. The whole row this section is about — a demo session
under a Zeitreise moment — exists only in demo mode: `sim_date` is read in one place,
inside `dashboard()`'s `DEMO_MODE` branch and only for a visitor's own session plan, so
production never renders a locked dot, a notice or a menu note at all. The gap is
therefore bounded by a feature that is a showcase of the planner, not a work surface
anyone depends on. Written down rather than fixed, with the shape a fix would take if the
Zeitreise ever became something more than that: a role or an `aria-describedby` on
`.task-menu-items`. Putting the sentence back in the banner is not it — that trade was
already made and reversed.

### The page owns its CSRF token

Removing the toggle forms removed something else with them. Every JavaScript write on
this page reads its token with `document.querySelector('[name=csrfmiddlewaretoken]')` —
the Zeitreise (`setSimDate`, `preloadOne`), `reschedule()` and the day-column drag. None
of them belongs to a form, so the token they were finding came from whichever
`{% csrf_token %}` happened to be rendered nearby: the toggle buttons' own in a demo
session, `↻ Aktualisieren`'s in production (and that one is inside `{% if not demo_mode %}`).

With the toggle forms gone under a moment, a demo session rendered no token at all.
`CSRF` fell back to `''`, every Zeitreise POST came back **403** — and `setSimDate`
reloads without checking the response, so the moment tiles and "Zurück zu heute" looked
dead rather than broken and the visitor was stuck inside the moment. `reschedule()` and
the drag handler read `.value` off the same lookup with no `?.`, so they would have
thrown outright.

`dashboard.html` now renders one `{% csrf_token %}` of its own at the top of the content
block, before any form. A hidden input outside a form is valid HTML and carries the same
value, so the lookup the JavaScript already does finds a token that is always there
instead of one that depends on which forms this particular render contains. The
assertion is covered both ways — with a moment and without — and it matches on the
rendered `<input>`, not on the string `csrfmiddlewaretoken`, which every one of those JS
lines also spells.

## The Zeitreise write queue

Every session-writing fetch on this page goes through `withSessionLock`
(`dashboard.html`). Django saves the whole session dict on every response, not just the
keys a request touched, so two `/timelapse/preload/` calls in flight at once each start
from their own snapshot and the one that saves last silently overwrites the other's
write — one moment's cached summary vanishes even though its own request reported
`ok: true`. One task at a time is the rule, and it is not negotiable.

What #235 changed is only *which* task goes next. The lock keeps two lists instead of one
promise chain, and `drainSessionQueue()` takes `priorityQueue.shift() || backgroundQueue.shift()`.
A click puts its work in the priority list; `preloadAll()`'s four background preloads stay
in the other one. Before that, a click was appended to the tail of the chain like anything
else, so clicking a moment whose green dot promised "generated and ready" still waited for
up to three unrelated Claude calls to finish.

Two details carry the fix:

- **A queued preload is promoted, not queued behind.** `preloadOne` hands a second caller
  the promise it already registered for that date, and `preloadAll()` has registered all
  four moments 800 ms after load. Inserting fresh priority work would therefore change
  nothing for any click later than that — the click has to move the existing entry into
  the priority list, which is what `promoteSessionTask(key)` does.
- **The preloads that have not started are dropped.** `setSimDate` calls
  `dropPendingPreloads(dateStr)` as its first statement, keeping only the clicked
  moment's own entry. Deferring the rest would be work for a page that is about to be
  thrown away: the reload restarts `preloadAll()` 800 ms later, and `precached_moments`
  (`views.py`) keeps the preloads that did finish from being paid for a second time.

**An in-flight call is never aborted.** Aborting client-side does not stop Django from
finishing the request and saving its session snapshot, so an abort would cause exactly the
clobbering the queue exists to prevent. Waiting out that one call is the floor — and it is
also what keeps `window.location.reload()`, a document navigation that sits outside the
queue by construction, from firing while a session write is still in flight; the reloaded
dashboard writes to the session too (it stores the generated summary on a cache miss), so
one of the two summaries would be dropped and cost another Claude call to regenerate. The
window is narrow — Django saves the session only when a request modified it, and
`SESSION_SAVE_EVERY_REQUEST` is left at its default `False` — but it is real, and silent.

This all rests on the switch still being a full reload. If that ever becomes a
fetch-and-swap (#236's open follow-up), the swap fetch is itself a session write and has
to go through the lock, and dropping pending preloads stops being safe unless the swap
re-triggers `preloadAll()` against the fresh `precached_moments`.

## When a reload still happens

| Action | Response | Client |
|---|---|---|
| Toggle, warm cache | figures | writes them |
| Toggle, cold cache | bare `{"ok": true}` | reloads |
| Toggle fails in Notion | 502 | leaves the checkbox alone, flashes the button |
| Reschedule, same stage | `urgency`, `due_display`, `postpone_count` + figures | writes them, moves the day card, re-sorts in place |
| Reschedule, stage changed | same | reloads |
| Reschedule into the browsed week from outside it | same | reloads — the day card does not exist yet |
| Reschedule, cold cache | no figures | reloads |
| Reschedule from the day-column drag | same | reloads — the column change is definitional |
| Reschedule fails in Notion | 502 | undoes the drag / restores the date |
| Zeitreise moment set | `{"ok": true}` | fades out and reloads — the whole page is a different date |
| Zeitreise POST refused or never lands | 403 / 502 / nothing | **no reload** — takes the paint back, flashes the trigger, re-arms the preloads |

The stage is what decides which *list* a task belongs to: the Heute lists and the Kanban
column. When it changes, the task has to change list, not position within one — worth a
server render rather than rebuilding by hand.

The day columns are the exception, and they were the gap: their membership is the *date*,
so every reschedule changes them, stage or no stage. A Wednesday task moved to Thursday
kept its card under Wednesday while the row above it read Thursday, with both counters
stale — the same page disagreeing with itself that #210 is about, on the one path that
does not reload. The counts now come from `_surface_figures` like every other number, and
only the two cards that render the date move by hand: the day card, whose column *is* its
date, and the Kanban card, which spells the date out.

## Every write reports its own failure

The convention was already here and already written down in its own comments; what
#233 found is that one path did not follow it. `setSimDate` POSTed to `/timelapse/`,
never looked at the answer, and faded out and reloaded either way — into exactly the
state the click had meant to leave. Found live on 2026-09-08, where #232's missing CSRF
token turned every Zeitreise POST into a 403 for as long as a moment was active: the
tiles and "Zurück" read as dead buttons, and a visitor had no way out of the moment.
Several minutes went into hunting for a stuck lock before the server log showed the
requests arriving and being refused.

| Call site | Checks | On failure |
|---|---|---|
| Toggle (`dashboard.html`) | `!response \|\| !response.ok`, with `catch` | flashes the button (#159) |
| `reschedule()` (`dashboard.html`) | same | flashes, restores the date |
| Day-column drag (`dashboard.html`) | same | undoes the drag |
| Rename, trash (`dashboard.html`) | same | flashes the name / the trigger |
| `my_plan.html` | same | reverts the optimistic toggle, flashes |
| Triage `+7` and picker (`close_week_start.html`) | `reschedule()` answers `null` | flashes the button / the date (#233) |
| `setSimDate` (`dashboard.html`) | same guard, same `catch` | no reload, paint undone, flashes the trigger (#233) |
| `preloadOne` (`dashboard.html`) | `!response.ok` → return | silent **by design** — a dropped preload costs a green dot |

Three things had to move for the last two rows to be one line each rather than a third
and fourth copy of the same block.

**`flashActionFailed` is a module** (`static/projects/js/action_feedback.js`), loaded
from `base_dashboard.html` beside the date picker and for the same reason: every caller
is an inline script in `extra_js`, which runs while the document is still parsing, so a
`defer`red module would not be defined yet.

**`.action-failed` and `@keyframes flash-failed` are in `dashboard.css`**, which that
same base already loads. This is the half that actually decided it. The triage list had
the handler's shape and none of the animation — it could not have reported a failure
even if it had wanted to, which is why both of its paths returned silently. A surface
now inherits the whole feedback or none of it; it cannot inherit half. The bound is that
all four surfaces extend `base_dashboard.html`; one outside that base would lose the
animation again.

**The failure branch does not reload, and takes its own paint back.** `setSimDate`
paints optimistically before the request — `active` and `loading` on the clicked tile,
`loading` on the bars, a reduced opacity on the moment title. Left standing after a
refusal, the bar claims a moment that was never set. `active` is *restored* rather than
removed: `setSimDate` never clears it off the tile that had it, so a failed click on the
already-active moment would otherwise leave the bar showing no moment at all while one
is still on. The dropped preloads are re-armed too — `dropPendingPreloads()` threw them
out of the queue on the way in and only the reload ever brought them back, so without
`preloadAll()` here the other moments' green dots would stay off for the rest of the
session. `preloadOne` is idempotent on both sides (`preloaded` client-side,
`precached_moments` server-side), so re-arming costs no Claude call for anything already
generated.

**"Zurück zu heute" gets no separate treatment.** Failing to *leave* a moment strands a
visitor in a way failing to *enter* one does not, which is a real asymmetry — but the
answer to it is not a second feedback shape. The flash lands on the control that was
clicked, which for the return path is the button inside the banner, and the banner
naming the simulated date stays on screen. The visitor is no longer facing a silent dead
button either way.

**The "Heute" tile was bound twice**, recorded by neither issue. `_timelapse_bar.html`
carried an `onclick` and `dashboard.html` added a listener to the same button, so one
press fired `setSimDate` twice: two POSTs and two reload paths. Harmless while nothing
was checked, two flashes once something was. The attribute goes and the listener stays —
it is the one that passes the button, which is what the flash needs as a target. The two
inline callers that remain (the sim banner's "Zurück", #244's locked-dot notice) pass
`this` for the same reason.

**A rejected preload is not a failed click.** `setSimDate` awaits the clicked moment's
preload before the POST, and that await had no catch either: offline, the rejection threw
out of the handler before the POST was attempted at all — #233's silence one step earlier
than #233 found it. The moment can still be entered without its preload; it only costs a
Claude call on the other side of the reload.

## A write that is running says so

#198 asked for the reschedule to be optimistic the way the toggle is: write the new date
into the DOM immediately, correct it when the server answers. That half is **declined**,
deliberately, and the reasons are the project's own existing decisions rather than new
ones:

- Writing the date client-side means a second copy of German date formatting in
  JavaScript. `reschedule_task_view` says so where it derives `next_week_display`, and
  #192 will change what a role produces — a JS mirror would drift the day it lands.
  `Intl.DateTimeFormat('de-DE', …)` does not avoid it: the roles produce `Mo, 15. Jun`
  with no trailing period, which no option set reproduces, so the optimistic guess would
  visibly differ from the string replacing it a moment later.
- Painting the new stage means implementing #169's calendar-week urgency rule a second
  time — and in a demo session it is measured against the simulated date.
  `URGENCY_CLASSES`' own comment already settles this the other way.

What is left of the intent — the interaction stops looking stuck — is met by the picker
marking its input while `onPick` runs (`pending` + `aria-busy`, cleared in the same
`finally` that swaps the display element back, so a thrown callback cannot leave it
marked). The row shows the `<input type="date">` holding the newly picked date, marked as
saving, rather than sitting unchanged with no sign that anything is happening. The wait
is shared because every surface's wait is the same two Notion round trips
(`increment_postpone_count` is read-then-write, `notion.py`); what the wait *ends* in is
not, which is why the flash stays with each surface's `onPick`.

Marked, not `disabled`: disabling blurs the input, and `blur` is what swaps the display
element back — mid-request.

## One picker, four consequences

`_task_due.html` shared the date's *markup* (#195); `projects/static/projects/js/task_date_picker.js`
shares its *behaviour* (#266). The cut between them is what keeps the second from
collapsing into a fourth copy of the first.

What is shared is the **asking**: swap the button for an `<input type="date">`, call
`showPicker()`, track whether the pointer or the keyboard opened it (#200), swap back, and
hand focus back to a keyboard user only. That is identical wherever a date is rendered,
and it carries the Safari/Chrome focus-modality reasoning that should be derived once. Since
#198 it also carries the `pending` mark on the input while `onPick` runs, for the same
reason: the wait is the same two Notion round trips on every surface (see the section
above).

What is **not** shared is the consequence. The obvious reading of "extract the handler" is
to share `reschedule()`, and it is wrong: the dashboard's depends on `browsedWeekStart()`,
`reclassify()`, `URGENCY_CLASSES`, `applyRescheduleFigures()` and `sortRows()`, none of
which mean anything on a triage list that does `fetch` and reads `ok`. So the module takes
a callback and each surface keeps its own:

```js
bindTaskDatePickers(onPick, {rowSelector, within, exclude});
onPick(taskId, isoDate, dueEl, row) -> Promise<boolean>
```

| Surface | After a successful move |
|---|---|
| Dashboard rows | patches in place, re-sorts, repaints the dot, writes the figures |
| Dashboard AI summary | reloads — the prose makes urgency claims a new date invalidates, and `task_refs` are positions in an order the move just changed |
| `/mein-plan/` | reloads — nothing there re-sorts the list, and badge, progress and summary are all server-rendered |
| Close-out triage list | patches in place; a reload would drop the moved row and its `task_id` input out of the form that counts it |

`row` is read at click time and handed in, and `within`/`exclude` are read at bind time,
for the same reason: the picker detaches the button while the request runs, so `closest()`
called inside a callback finds nothing. The dashboard's two behaviours name the region
each owns (`{exclude: '.ai-card'}` and `{within: '.ai-card'}`) rather than depending on
which binding runs first.

Adding a surface is therefore an include plus one call. Copying `reschedule()` into it is
the thing this split exists to prevent.

## Deliberate gaps

These are decisions, not omissions.

- **The Heute lists keep a checked-off row in place**, struck through, until the next
  load. Their membership is urgency-based and a done task belongs to no bucket, so a
  strict render would make the row vanish under the cursor.
- **A day column counts what it shows** — tasks due on that day. A task cleared today but
  due elsewhere moves its own due day's counter, not today's. This is #182's rule for the
  week bar applied one level down: a counter counts the cards beneath it.
- **The same-date tiebreak is not reproduced client-side.** The server sorts the Heute
  lists by `(due, project_name)`; `sortRows()` sorts by date alone, so a row moved onto
  an occupied date lands at the end of that date group instead of in project-name order.
  Correct again on the next load.
- **A moved Kanban card lands at the end of its new column**, not at the position the
  server would render it in (month, then project, then due date). Reproducing that order
  client-side would put the board's structure into JavaScript, which is what shipping the
  column as a single field exists to avoid. The card sits right on the next load. A day
  card moved between columns lands the same way, for the same reason — the server sorts a
  day's cards by project name.
- **The postpone badge waits for the next load.** A reschedule rewrites the date beside
  it but not the badge, on the row and on the Kanban card alike: the badge only renders
  from the second move on, so making it appear means creating markup and the threshold
  rule that decides it. The row has had this gap since #171; the board now matches it
  rather than growing a second answer.
- **The day card's hover title keeps its old date.** `_day_task_card.html` puts
  `project · d.m.` in a `title`, and that short form is not in the answer. Adding it would
  mean a second date format in the API for a tooltip that repeats the column the card
  already sits in. The visible surfaces — the column itself, the row's label, the board's
  label — all move.
- **A moment stays read-only rather than explaining itself.** Withholding the button was
  chosen over keeping it and flashing `action-failed`, or writing a notice: a moment is a
  view of a past date, and a control that is always going to refuse is worse than no
  control. `my_plan` keeps its toggle (`toggle_session_task`) for the opposite half of the
  same reason — it has never read `sim_date`, so it renders the real date and a write there
  lands where it shows (#246). What it lacked was any mention of the moment
  at all, which is what the notice above `.project-header` now adds.
- **A project-less task has no Kanban card to move.** The board renders only
  `project["tasks"]` (#182), so there is nothing there for the toggle to update.
- **Two writes landing at once can lose one of them.** `_patch_cached_tasks` is a
  read-modify-write over a cache shared across processes, with no fence: A reads, B reads,
  A writes, B writes, and B's entry no longer carries A's task as done. Notion still has
  both, but the cache serves the older state until its deadline runs out. The delete this
  replaced was idempotent and self-correcting under the same interleaving, so this window
  is new. It wants the same write fence as the gap below, and production is one person
  behind a VPN — recorded rather than fenced.
- **A write landing during a cold-cache fetch is still lost.** `_fetch_fresh_data` is as
  slow as the Claude call above, and the branch that follows it writes the projects it
  just read from Notion. The re-read that fixes the regeneration branch cannot fix this
  one — there, only the summary is the request's to contribute, whereas here the projects
  genuinely are new. It needs a write fence: a counter every confirmed write bumps, which
  a long read checks before writing back. Predates #216 and is left as it was.

## Cache versions

Both key pairs went to `v9` / `v4` with #210, because every cached task dict gained a
`kanban_column`. The cache stores already-annotated projects and does not re-annotate on
a hit, so a pre-deploy entry would render an empty board — and the `STALE_*` entries never
expire, so they would serve that shape indefinitely. A new derived field is a format
change, and the bump is mandatory rather than cosmetic.

## Verification

`projects/tests/test_dashboard_writes.py` covers this in thirteen classes:

- `ToggleSyncCoversEveryCardShapeTest` — each card shape asserted on its own, because a
  single "the handler exists" check is exactly what would have passed all along
- `KanbanColumnTest` — the mapping, including a completeness check against `_URGENCY_RANK`
- `ToggleKeepsTheDashboardCacheWarmTest` — patched not deleted, stale copies included,
  derived fields re-derived, and the fallback to a full bust
- `ToggleAnswersTheRecomputedFiguresTest` — the response shape, the denominator effect,
  and that the load path and the write path call the same helper
- `ToggleUpdatesEverySurfaceTest` — each surface written from the response, no count
  derived in JavaScript
- `RescheduleKeepsTheCachedProjectsTest`, `RescheduleResortsTheRowTest` — the reschedule
  half, server and client
- `RescheduleAnswersTheRecomputedFiguresTest`, `RescheduleFiguresFromTheSessionPlanTest`,
  `RescheduleUpdatesTheDayColumnsTest` — the day columns following a same-stage move, in
  production and in a demo session, and the client writing what it was handed
- `PatchingDoesNotRenewTheReadWindowTest` — every assertion on the timeout a write
  actually named, never on the deadline stamp beside it: a patch leaves that stamp
  alone either way, so asserting on it would pass with the bug still in place
- `RegeneratingASummaryDoesNotUndoAConcurrentWriteTest` — the second request runs
  inside the stubbed Claude call, which is exactly where it would land; a toggle
  survives, a reschedule takes the summary with it, a bust is not resurrected
- `TheFailureFlashIsOneModuleTest` — the module holds the flash, no template does, the
  base loads it un-`defer`red and ahead of `extra_js`, and the animation lives exactly
  once in `dashboard.css` and in no surface's own block
- `ThePickerSaysItIsSavingTest` — the input is marked before the `await` and unmarked in
  the same `finally` that swaps the date back, marked rather than disabled, with a rule
  in the shared sheet

`projects/tests/test_timelapse.py` carries the moment half in
`NoToggleDuringAMomentTest`, where the `sim_date` fixtures already live: the 404 and the
untouched session plan, the dashboard rendering no `toggle-form`, the dot still rendering
with its urgency, the unchanged behaviour with no moment active, the reschedule that
deliberately stays, and the page's own CSRF token — present under a moment, and rendered
ahead of the toggle forms without one, so it cannot go back to being a side effect of
whichever form happens to render.

`TheZeitreiseChecksItsAnswerTest` and `TheHeuteTileIsBoundOnceTest`, also in that
module, carry #233: the guard and the `catch`, a failure branch holding neither a reload
nor the fade, the paint taken back with `active` *restored* rather than removed, the
flash, the re-armed preloads, the return path deliberately sharing all of it, the bar's
tile bound by listener alone, and both remaining inline callers handing their own control
in. Markup contract only — the behaviour gets a browser pass with the network offline,
the same boundary `TimelapsePreloadMarkupTest` and `TimelapseClickPriorityTest` already
document.

`projects/tests/test_closeout.py` carries the third write path in
`TheTriageListReportsAFailedMoveTest`: both paths flashing, the guard unchanged, and the
surface carrying no copy of either half of the feedback it now inherits.

`AMomentSaysWhatItLocksTest`, in the same module, carries what the page *says*: the
banner naming the state and stopping there — the consequence clause pinned out rather
than in, so it does not drift back — #153's one-date rule re-asserted against the new
copy, the notice element (present only in a moment, hidden
until a click, tokens borrowed from the banner, above the menu's `z-index`, obeying its own
`[hidden]`, placed against the clicked dot, leaving on scroll, restarting rather than
flagging, and offering "Heute anzeigen" rather than the banner's pinned short label), the
dot gaining no affordance back, and the menu note — present in a moment, absent outside
one, and deliberately not a `.task-menu-item` the keyboard handler would focus.
