# Serving pages without waiting on Claude

Implements [#156](https://github.com/liga-auguste/planning-hub/issues/156), and
subsumes [#93](https://github.com/liga-auguste/planning-hub/issues/93).

Three page loads used to block server-side on a full Claude round trip before the
browser received any HTML:

| Load | What it generated inline | Measured |
|---|---|---|
| Dashboard, first uncached visit | `generate_weekly_summary` | 6–7 s |
| `/mein-plan/`, first visit | the same call | 6–7 s |
| "Zum Dashboard" after plan creation | `generate_timelapse_moments`, then the above | two waits in a row |

The only feedback during any of them was the browser tab's own spinner, which cannot
tell working from hung. [#6](https://github.com/liga-auguste/planning-hub/issues/6)
had solved exactly this for the planner's *buttons* — but a button state ends at the
redirect, and the blocked page load on the other side of it had no equivalent.

## What changed, and what deliberately did not

The README's "Synchronous AI calls" decision stands. There is still no task queue, no
broker and no worker: a Claude call still runs inside a request and blocks the thread
serving it. What moved is *which* request. The page keeps the Notion read and renders
everything that needs no AI call; the summary comes from an endpoint of its own, which
blocks its own `fetch` while the page is already on screen.

The pattern was already here. `/timelapse/preload/` has always run a Claude call
synchronously inside a background `fetch` fired by the page; this is that shape applied
to the two surfaces that show a summary, plus the moments.

```
GET  /dashboard/        projects out of the cache, loading state, no Claude call
POST /summary/          generate, cache, answer with the rendered card body
GET  /dashboard/        the cached summary, rendered inline — no loading state
```

## The three contexts, one endpoint

`summary/` serves every context the pages do, and decides which from the session and
`?mode=multi` rather than taking it as a parameter — so the client needs no second copy
of that rule and cannot ask for a summary of something the page is not showing.

| Context | Cache | Helper |
|---|---|---|
| Demo session plan | `session[f"{SUMMARY_KEY}_{sim_date or 'today'}"]` | `_session_summary` |
| Demo example catalog | `cache[f"{DEMO_MULTI_SUMMARY_KEY}_{today}"]` | `_demo_multi_summary` |
| Production | `CACHE_KEY`, written back by `_attach_regenerated_summary` | `_production_summary` |

`?surface=my_plan` picks the markup. Two partials rather than one, because the two
pages render the same summary differently — `.ai-card` against `.summary-box`, submit
handlers against inline `onclick`. Unifying them is
[#92](https://github.com/liga-auguste/planning-hub/issues/92)'s job.

`_session_summary` is shared with `preload_timelapse_summary`: the two want the same
work done and differ only in what they answer with.

**No Notion read anywhere in the endpoint.** A fragment request arrives after the page
load that filled the cache, so a cold `CACHE_KEY` means that load failed or a write
busted the entry — both of which the next load handles. A miss answers with the
"nicht verfügbar" state and writes nothing, so the retry is the next request.

## The trap: the production cache guard

`CACHE_KEY` holds `(projects, summary_data)` as one tuple, and `dashboard()` cached the
Notion read only when the summary was not None:

```python
if summary_data is not None:
    _cache_fresh_read(CACHE_KEY, (projects, summary_data), ...)
```

A sensible guard while the Claude call sat in that path — a fetch whose summary failed
was not a success worth remembering. Once the call left, `summary_data` is *always*
None there, so keeping the guard would have meant never caching a Notion read again and
re-reading Notion on every single dashboard load. The opposite of this issue's goal, and
the way the refactor fails silently.

`(projects, None)` is now cached deliberately. The guard's purpose survives in
`summary_fragment()`, which writes back only on success, so a failed summary still is
not sticky.

`STALE_CACHE_KEY` keeps the guard, where it still applies: that copy is what a Notion
outage serves, and overwriting it with a summaryless read would throw away the last
summary a Claude call was paid for. `_attach_regenerated_summary` writes it, with the
same projects, as soon as the summary arrives.

The `(projects, None)` shape is not new — it is
[#199](https://github.com/liga-auguste/planning-hub/issues/199)'s "projects good,
summary missing", previously reached only after a reschedule. It is now the shape every
first load leaves behind, which is why that branch became the loading state rather than
an inline call.

## Rebinding, and why it was necessary

Every binding in `dashboard.html` is direct, not delegated. The comment above the
project links said why that was safe: *"neither list is rebuilt from markup … and the
summary reloads the page."* The summary is markup now, so `bindProjectLinks` and
`bindToggleForms` take a root and are called again against the region that was swapped
in. Without that, a project heading opens nothing and a checkbox's form submits as a GET
— a silent reload that looks exactly like a toggle that did not take.

`bindTaskDatePickers` needed no change: it queries globally and filters with
`closest(within)`, so re-calling it with `within: '.ai-card'` reaches only the new
elements. On `/mein-plan/` the load-time call has *no* scope, so its rebind is scoped to
`#ai-summary` — an unscoped second call would double-bind every row in "Alle Aufgaben".

## Session writes

Django saves the whole session dict per response, so two overlapping session-writing
requests mean the later save silently drops the earlier one's write
([#235](https://github.com/liga-auguste/planning-hub/issues/235)). The dashboard already
serialises every such `fetch` through `withSessionLock`; the summary request joins it
with `priority: true` (the visitor is watching the spinner) and the moments request in
the background half (nothing on screen is waiting for the Zeitreise bar).

In production neither writes the session, only the cache — the queue costs nothing
there, and one path is better than a branch on the mode.

`/mein-plan/` has no such queue and needs none: it has exactly one background request,
so one promise is the same guarantee at the size this page needs it. Its toggle and its
reschedule `await summarySettled()` before writing.

## The moments

`planner_create` no longer calls `generate_timelapse_moments`. It sets
`demo_timelapse_pending` and redirects; the dashboard fetches `timelapse/moments/` in
the background. The endpoint clears the flag *before* it calls, which keeps
`planner_create`'s old `try/except` budget exactly: one plan, one attempt, and a failure
leaves the bar hidden the way a plan without moments has always looked — rather than a
Haiku call on every dashboard load for the rest of the session.

The bar is built by a function rather than by a block that could only ever run once, so
moments rendered into the page and moments fetched afterwards build the same bar and
start the same preloads 800 ms later. That is #93: the preloads begin once the shell is
up rather than 800 ms after a fully blocked load.

## The four states of the card

The chain in `_ai_summary_body.html` gained one branch, and it comes first because it is
the only one that is about this *request* rather than about the summary:

1. **pending** — a spinner and a line of German copy, mirroring the card's own label
   ("Deine Wochenübersicht wird erstellt …"). Not "Übersicht" on its own: that is the
   word [#48](https://github.com/liga-auguste/planning-hub/issues/48) removed for
   labelling two different things.
2. **empty** — [#214](https://github.com/liga-auguste/planning-hub/issues/214)'s note,
   when Claude answered nothing.
3. **resolved** — the blocks.
4. **unavailable** — when the call failed.

State 4 now has two carriers: the endpoint's answer, and an element the loading state
keeps hidden for a request that never arrives at all (offline, server unreachable) — an
AI failure comes back as that state with a 200, so there is HTML to swap in; a rejected
`fetch` has none. The sentence lives once, in `_summary_unavailable.html`, with the
dashboard's second clause behind a `with_data_note` flag.

The card renders `aria-live="polite"` and, while pending, `aria-busy="true"`: the
content arrives without a navigation, and a summary belongs at the reader's next pause
rather than over whatever they are on.

## Measured

Against the running demo stack on 2026-10-02, three runs each, `curl`'s `time_total` for
the uncached case and the `claude_call` log lines ([#31](https://github.com/liga-auguste/planning-hub/issues/31))
beside them. The cache is cleared before every run.

**Dashboard, example catalog, first uncached visit** — the load the issue measured at
6–7 s:

| | `main` | this branch |
|---|---|---|
| `GET /dashboard/?mode=multi` | 6.34 / 6.97 / 7.41 s | **0.0064 / 0.0067 / 0.0067 s** |
| `POST /summary/?mode=multi` | — (inside the page) | 6.82 / 6.95 / 7.53 s |
| `claude_call duration_ms` | 6309 / 6933 / 7377 | 6802 / 6915 / 7514 |

**Planner, "Plan speichern" to a painted dashboard** — a session plan of two tasks, so a
much smaller prompt than the catalog above:

| | `main` | this branch |
|---|---|---|
| `POST /planner/create/` | 2.64 / 2.83 / 3.16 s | **0.0022 / 0.0023 / 0.0030 s** |
| `GET /dashboard/` right after | 2.30 / 2.50 / 2.66 s | **0.0024 / 0.0025 / 0.0028 s** |
| what the visitor waits for before seeing anything | **5.1 – 5.8 s** | **~5 ms** |
| `POST /summary/` afterwards | — | 2.24 / 2.44 s |
| `POST /timelapse/moments/` afterwards | — | 2.67 / 2.71 s |

Two readings:

- **The page is roughly a thousand times faster to first byte** — 6–7 s to ~6 ms on the
  catalog, 5.1–5.8 s to ~5 ms out of the planner. That is the whole of this change: the
  browser gets a complete, usable page while the model is still being asked.
- **The wait itself is unchanged**, as the issue predicted. The summary is readable at
  about the same wall-clock moment either way. What moved is that the visitor spends
  those seconds looking at their own projects and a labelled spinner rather than at a
  blank tab.

The `claude_call` lines also answer the issue's latency question with a number: the
summary runs 6.3–7.5 s on 1,714 input tokens for the five-project catalog, and 2.2–2.4 s
on 529 for a two-task session plan. It is prompt size, not a constant — so "Sonnet costs
~7 s" is only true of the largest surface, and the Haiku comparison the issue suggests
has to be run per surface rather than once.

## What this does *not* change

- **The wait itself.** It shrinks only slightly — measured above, not at all. What
  changes is that it is visible, labelled and non-blocking; perceived performance is the
  point.
- **Caching behaviour.** A visitor who already has a summary gets it inline, with no
  loading flash.
- **The model.** The summary still runs on `claude-sonnet-4-6`; the Haiku-instead-of-
  Sonnet lever the issue names is a *quality* question for `eval_language`, and swapping
  it in the same pass would spoil this change's own before/after measurement. Same
  separation PR #276 drew for the model id.

## Two residual risks

- **The fragment and the page can disagree.** The fragment is rendered from the projects
  live at fetch time; the task lists on the page come from page load. A toggle in between
  leaves the summary on the new state and the list below it on the old one until the
  next reload. The window is small, and it did not exist before — both used to come out
  of one request.
- **`/mein-plan/`'s add row.** Its write lives inside `js/task_add_row.js`, which owns
  the whole fetch, so it cannot `await summarySettled()` the way the toggle and the
  reschedule do. An add in the first seconds after load can therefore still lose the
  race. The same window has existed on the dashboard since the preloader was written —
  its toggle does not go through `withSessionLock` either — so this is a known shape
  rather than a new one. Recorded in `ANALYSEN.md`.
