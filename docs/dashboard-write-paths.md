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

Two rules govern it:

1. **A patch never serves a state predating a confirmed write.** A stale snapshot that
   does not carry the task at all cannot be corrected, so it is deleted rather than left
   in place.
2. **The fallback is the normal path, not an edge case.** A cold cache, a half-cold
   cache, or a task no cached list carries all return `None`, and the caller busts
   exactly as before.

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
whole_plan=False)` is the single source. `dashboard()` renders from it; `toggle_task_view`
answers with it.

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

The toggle request carries `week_start`, the Monday of the week the day columns are
showing. `?week=` navigates them to any week and the server cannot guess which one is on
screen.

## When a reload still happens

| Action | Response | Client |
|---|---|---|
| Toggle, warm cache | figures | writes them |
| Toggle, cold cache | bare `{"ok": true}` | reloads |
| Toggle fails in Notion | 502 | leaves the checkbox alone, flashes the button |
| Reschedule, same stage | `urgency`, `due_display`, `postpone_count` | reclassifies and re-sorts in place |
| Reschedule, stage changed | same | reloads |
| Reschedule from the day-column drag | same | reloads — the column change is definitional |
| Reschedule fails in Notion | 502 | undoes the drag / restores the date |

The stage is what decides bucket membership: the Heute lists, the day columns, the week
bar, the Kanban column and the sidebar ring. When it changes, the task has to change
*list*, not position within one — worth a server render rather than rebuilding by hand.

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
  column as a single field exists to avoid. The card sits right on the next load.
- **A project-less task has no Kanban card to move.** The board renders only
  `project["tasks"]` (#182), so there is nothing there for the toggle to update.

## Cache versions

Both key pairs went to `v9` / `v4` with #210, because every cached task dict gained a
`kanban_column`. The cache stores already-annotated projects and does not re-annotate on
a hit, so a pre-deploy entry would render an empty board — and the `STALE_*` entries never
expire, so they would serve that shape indefinitely. A new derived field is a format
change, and the bump is mandatory rather than cosmetic.

## Verification

`projects/tests.py` covers this in six classes:

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
