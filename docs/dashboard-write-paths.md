# Dashboard Write Paths

Implements [Issue #210](https://github.com/liga-auguste/planning-hub/issues/210),
[Issue #199](https://github.com/liga-auguste/planning-hub/issues/199) and the client
half of [Issue #194](https://github.com/liga-auguste/planning-hub/issues/194).

## Context

The dashboard renders three views out of one document — the overview, the Heute view and
the per-project sections — plus a shared sidebar. Each one grew its own surface for task
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
| Reschedule a task | patched and re-sorted | dropped | full bust |
| Reschedule → postpone counter | patched in place | already dropped | full bust |
| Create a project (planner) | full bust | full bust | — |

`_patch_cached_tasks(task_id, mutate, today, drop_summary=False)` applies `mutate` to
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
- **A project-less task has no Kanban card to move.** The board renders only
  `project["tasks"]` (#182), so there is nothing there for the toggle to update.
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

`projects/tests.py` covers this in eleven classes:

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
