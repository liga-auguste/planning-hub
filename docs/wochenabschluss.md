# Weekly Close-Out Ritual

Implements [Issue #169](https://github.com/liga-auguste/planning-hub/issues/169) and
[Issue #171](https://github.com/liga-auguste/planning-hub/issues/171).

## Context

The urgency system used to punish deliberate planning. `urgent` was a rolling 7-day
window (`(due - today).days <= 7`): move a task on Friday to next Tuesday and it turned
orange again the moment it was saved — rescheduling looked identical to procrastinating.
There was no way to *close* a week; the window never shut, and the dashboard permanently
signalled "not enough". #169 replaces the rolling window with a calendar-week comparison
and adds a deliberate "close the week" ritual with a review page. #171 adds quiet
awareness for tasks that keep getting pushed, riding the same reschedule call site.

## Calendar-week urgency, not a rolling window

`urgent` now means: due date falls in the same ISO calendar week (Monday–Sunday) as
today. `projects/dates.py` holds the one comparison both `_annotate_tasks` (`views.py`)
and `build_prompt` (`ai.py`) use:

```python
def is_same_iso_week(a: date, b: date) -> bool:
    return a.isocalendar()[:2] == b.isocalendar()[:2]
```

Comparing the `(ISO year, ISO week)` tuple, not the bare week number, is what makes this
safe across a year boundary — `isocalendar()` already assigns Dec 29–31 to week 1 of the
following ISO year where appropriate; a bare week-number comparison would wrongly equate
two "week 1"s a year apart.

**Design decision:** "closing the week is what arms the next week's signals" is a pure
calendar comparison, not a persisted state machine. There is no stored "current effective
week" pointer and no gating beyond the calendar itself — closing a week is the triage
ritual and the stats snapshot below; it does not itself move the urgency boundary, the
calendar does that for free. The rejected alternative was a persisted pointer that would
let closing on a Friday afternoon make next week's tasks urgent immediately, ahead of the
real rollover — resolved in favour of the simpler reading: less state, no extra field
just to compute urgency.

**Overdue edge case in `build_prompt`:** the same rolling-window logic existed a second
time, driving the "DIESE WOCHE" label in the Claude prompt. An overdue task has a
negative day-difference, and the old `diff <= 7` condition incidentally caught that too,
labelling it "DIESE WOCHE" — naively swapping in `is_same_iso_week` would send an overdue
task from a *past* calendar week into the days-remaining branch instead
(`fällig in -5 Tagen`). The rewrite checks `diff <= 0` first, so overdue keeps its exact
old label and only the future-due boundary changes.

## The close-out flow

| Route | Name | Purpose |
|---|---|---|
| `woche-abschliessen/` | `close_week_start` | GET: triage list — every still-open task due in the browsed ISO week, `?week=YYYY-Www` (default: the current week) |
| `woche-abschliessen/bestaetigen/` | `close_week_confirm` | POST: compute stats for the week the form carries, persist, generate the AI summary, redirect to that week's review |
| `wochenrueckblick/` | `week_review` | GET: one close-out + stats + AI text, `?week=YYYY-Www` (default: the latest close-out) |

German paths, English `{% url %}` names — the project's #15 convention for a page a
visitor navigates to.

### The week is a parameter, and the form wins (#263)

All three views used to derive the week they act on from `timezone.localdate()`
independently, and the form between the first two carried nothing but a CSRF token and a
list of task ids. That holds only while the whole ritual happens inside one calendar day,
which is not how a weekly review is used.

`_week_monday(raw, default_monday)` (`views.py`) is the one parser: `2026-W25` → that
week's Monday, falling back to `default_monday` on anything unusable — absent, malformed,
a week number ISO doesn't have, or one too close to `date.min`/`date.max` to render
(#216). `_parse_week_param` is its `request.GET` caller (#180's dashboard navigation),
`close_week_confirm` its `request.POST` caller, and `_week_param(monday)` writes the same
wire format back into links and the hidden field. One format app-wide, one guard, one
fallback rule — the rejected alternative was a second week format for the form.

**The posted week wins, and there is no mismatch case.** `close_week_confirm` closes the
week its form was showing, whatever week the request itself falls in: a review begun on
Friday evening and submitted on Monday still closes the week it was triaging. Once the
week is a parameter, a week other than today's is a *choice* — reported back as an error
it would fire on exactly the use the `?week=` navigation exists for. Submitted after the
rollover, the old code stored the close-out under the following week, counted that week's
completions (a few hours old), and counted **every** posted task as rescheduled, since a
task due last week is by definition no longer in "this" one.

**Closing the week that just ended** is the same parameter read from the query string.
`close_week_start` offers "← Vorwoche" and, while browsing, a way back to the current
week; there is no forward link, because closing a week that has not happened yet is not
what the ritual means. `already_closed`, the subtitle and the weekend empty state all
follow the browsed week — "Genieße dein Wochenende" is about the week that is ending now,
not about a Saturday spent looking back at one that is over.

**The review is addressable.** `week_review` renders `get_latest_closeout` when no week is
named, which is every route into the page that existed before. `close_week_confirm` now
names the week it just closed: with KW 26 already closed, closing KW 25 would otherwise
show KW 26's numbers as the result of this visitor's own action. `closeout.py` gained
`get_closeout(request, iso_year, iso_week)` alongside it, both backends behind one
interface and sharing one row-to-dict mapping. Demo mode hid this defect rather than
lacking it: its session holds exactly one close-out, overwritten each time.

`_closeout_dates` is untouched — it stays the single place demo mode's simulated date
enters the flow, and the browsed week defaults off it, so the timelapse works exactly as
documented below.

**The triage list holds every still-open task of the week being closed** (#263), whether
or not its day has passed. The bound used to be `due >= today` as well, on the reading
that overdue tasks "already have their own signal" — true for a task from a *past* week,
which `is_same_iso_week` still keeps out, and backwards for a task from the very week
being closed: what that one is missing is precisely a deliberate decision, and this is the
page the visitor is sitting on to make it. Before, a task due Tuesday and still open on
Friday could not be moved from the one surface whose job is moving tasks.

One list, one meaning for the button: "→ nächste Woche" is `due + 7` for every row.
Browsing two or more weeks back, that can offer a date which is itself in the past — a
real date the visitor reads before clicking and can correct afterwards. Splitting the list
into two groups with two different move semantics was judged to cost more than that is
worth.

**The triage list's "→ nächste Woche" button posts to the existing**
`/task/<id>/reschedule/` **endpoint** — the same one the dashboard's inline date edit and
"→ heute" button already use, just with a client-computed `due + 7 days` instead of
`TODAY`. No new reschedule mechanism: `reschedule_task_view` stays the single call site
for every date change app-wide, which is also why the postpone counter (#171, below)
covers this flow automatically without either issue needing to know about the other.

**Stats definitions** (`close_week_confirm`). Two of the three numbers measure the ISO
week the review is headlined with; the third measures the close-out interaction, and is
rendered as a sentence rather than a tile so the row stops reading as three answers to
one question (#215).

- **completed** *(week)* — tasks whose Notion `Erledigt am` falls in the week **and**
  whose `Done` checkbox is ticked, from `get_tasks_completed_in_range`. This is a direct
  `TASKS_DB` read on purpose: `get_upcoming_projects` filters at the *project* level, so
  a task finished in a project that was then closed would leave the count, and
  `_get_tasks` only ever reaches tasks that carry a project relation (#53). Measured
  against the live database for KW 36/2026: 19 completions, of which the project-keyed
  path could reach 10.
- **added** *(week)* — tasks whose `created_time` falls in the week, from
  `get_tasks_created_in_range`, the same independent read so both numbers describe one
  population. Production only: a demo plan is created in one shot, so the count is
  structurally 0 and the tile is left out rather than shown (`week_review.html`).
- **rescheduled** *(this close-out, not the week)* — of the ids the triage page posted,
  those no longer due **the week being closed** (or now undated) and not done. The week
  comes from the form (#263), so a submission after the rollover measures the same week
  the list was built for rather than reporting a move for every task on it. Notion's
  `Verschoben` is a bare counter with no timestamp (#171), so "moved this week" is not
  derivable from the schema at all; this number is the honest, reachable one, and its
  scope is named in `WeekCloseout`, in the prompt and on the page.

**Known floor, by design:** a task checked off directly in Notion, or before `Erledigt am`
existed in the schema, has `Done` without a date and cannot be placed in a week. It is
missing from **completed**. `_count_done_in_range` (the dashboard's progress bar) works
around this by counting `done` instead — an option a week-scoped count does not have.

**Pagination:** the two week reads page through every result (`_query_all_pages`). Notion
returns at most 100 rows per query, and the busiest creation week in the live Tasks
database holds 157 — a first-page cut here would be the same silent undercount #215
removed. Since #196 every read in `notion.py` that can outgrow one page pages. Two cannot:
the planner's project history is capped on purpose at `HISTORY_PROJECT_LIMIT`, because
everything it returns goes whole into the prompt, and `find_project` looks up one exact
name and date and takes the first result.

**Demo mode follows the timelapse** (`_closeout_dates`): with a simulated date set, that
date is "today" for the triage list, the counts and the review's KW, so the close-out does
not talk about a different week than the dashboard is showing. Completions the timelapse
produced reach this flow with no `completed_date` — a moment forces them done on a
deepcopy (`_simulated_project`) that is never written back, and the close-out reads the
session plan itself rather than that copy — so `_demo_completed_in_range` places them by
their due date, which is what made them done. Since #246 that placement is asked *alongside* a hand-written completion
date rather than only where none stands: `toggle_session_task` records the real date,
because `/mein-plan/` renders the real date and a write there lands where it shows. A task
the moment had struck through would otherwise leave the simulated week the moment the
visitor also cleared it by hand — the real date displacing a placement that was carrying
it. Either date inside the range counts the task, and a task both dates place there is
still one task.

**Persistence** (`projects/closeout.py`): two backends behind one interface, the same
shape as `rules.py` — production stores a `WeekCloseout` row (unique on
`iso_year`/`iso_week`, so re-closing a week updates rather than duplicates); demo mode
keeps the visitor's own latest close-out in the session, and only when a session plan
exists — the generic multi-project demo view has no session identity and stays out of
scope, same as reschedule itself (#10 §5).

**Re-closing a week is supported, not guarded against** (#215). `close_week_start` used to
hide the submit button once a week was closed and nothing was left to triage, because
re-confirming posted an empty `task_id` list and zeroed the counts. Both week-scoped counts
are read from the week itself now, so a second close-out recomputes them — and the guard
was freezing the numbers in the one case where they go stale fastest, a week finished off
after it was closed. The button stays, labelled "Rückblick aktualisieren", next to a link
to the existing review. Only the reschedule sentence is lost on a re-close, and since it
describes the close-out that just ran, its disappearing when that one moved nothing is
correct rather than a loss.

**A failed close-out says so** (#215): all three Notion reads in `close_week_confirm` land
on `_closeout_read_failed`, which redirects to the triage page and renders the same
`.stale-notice` the dashboard uses for an unreachable Notion. Nothing is persisted on that
path — storing a zeroed week as if it were the answer is the defect this issue removed.

The notice travels as a **claim ticket**: a random value written to the session *and*
repeated in the redirect URL (`?notice=…`), shown only when the two match, and cleared by
the match. A bare session flag would have been read by whichever request arrived first,
which in a second open tab is a notice about a failure that tab never had — and the tab
that earned it would then get nothing. Requiring both halves addresses the notice to the
one response that follows the redirect, and consuming the ticket means a reload of that
same URL stops warning about a failure that is over. No JavaScript involved.

**Out of scope:** no browsable *list* of past close-outs. Since #263 a specific one is
addressable (`/wochenrueckblick/?week=2026-W25`) and a past week can be closed, but there
is still no page that enumerates them and no navigation between them — `week_review` shows
the week it was asked for, or the latest. The data model has always supported the list;
what is missing is only the surface.

## The postpone-counter badge (#171)

Rescheduling used to be invisible: a task could be moved indefinitely and always looked
freshly planned. A small badge (`N× verschoben`) now appears once a task has been moved
**more than once** — the first move stays unmarked, since moving something once is normal
planning, not a pattern worth flagging.

- **Storage:** production reads/writes a `Verschoben` Notion number property (added by
  hand to the Tasks database before this ships — the app only ever reads/writes existing
  properties). Demo mode keeps the count on the session task dict, the same way `done`
  does.
- **`increment_postpone_count`** (`notion.py`) is read-then-write, since Notion has no
  atomic increment — deliberately a second call rather than folding it into
  `update_task_date`, so that function and its own tests stay untouched. Accepted gap: if
  the date update succeeds but this second call fails, the date has moved but the counter
  hasn't; `reschedule_task_view` still reports the same 502, and the count self-heals on
  the next reschedule.
- **Display:** `.badge-neutral` (dashboard.html/my_plan.html) is `.date-uncertain-badge`'s
  declarations under a shared name, right of the due date wherever it renders — including
  Kanban cards, a new precedent there.

## Cache version bump

`CACHE_KEY`/`STALE_CACHE_KEY` moved `_v5` → `_v6`: the cached `projects` tuple bakes in
both the old rolling-window urgency and task dicts without `postpone_count` — a
pre-deploy entry in the old shape would misclassify urgency and, worse, crash the
template (`{% if task.postpone_count and ... %}` on a genuinely missing dict key resolves
falsy and short-circuits harmlessly, but the cached tuple's projects need the key present
at all for that to matter downstream). `SUMMARY_KEY`/`DEMO_MULTI_SUMMARY_KEY` are
unaffected, same reasoning as the #160 bump: they store only Claude's index references,
never urgency or counts, which are always attached fresh at render time.

## Verification

```bash
python manage.py test projects
ruff check .
ruff format --check .
```

Manual click-through, production:

1. Add a task due tomorrow and one due in 10 days — the 10-day one must **not** render
   orange (this is the whole point of #169).
2. `/woche-abschliessen/` — every still-open task due this calendar week appears,
   including one whose day has already passed; tasks from an earlier week and done tasks
   don't.
3. Move one task with "→ nächste Woche", leave another as is, tick a third done directly
   on the dashboard in a second tab, then "Woche abschließen" — the review page's
   rescheduled/completed counts should match.
4. "← Vorwoche" — the subtitle names the previous KW, the list holds that week's open
   tasks, and closing it lands on *that* week's review even when the current week is
   already closed. "Diese Woche" returns.
5. The stale tab: open `/woche-abschliessen/`, change the hidden `week` field to the
   previous week in the browser's dev tools (this stands in for leaving the tab open past
   Sunday midnight), submit — the close-out is stored under that week, and a task still
   due in it is not counted as rescheduled.
4. Reschedule the same task three times — the fourth view of the dashboard should show
   "3× verschoben" (not before the second move).

`DEMO_MODE=true` — the same click-through with a generated session plan; the multi-project
example view offers neither the close-out link nor postpone badges (no session identity).
