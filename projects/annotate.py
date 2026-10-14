"""The pure derivation layer over task lists: urgency, Kanban column,
progress ring and order (#302).

Every read path runs its projects through _annotate_tasks before rendering,
and so does every cache patch after a write (dashboard_cache.py). A module
of its own because both sides need it: views.py imports the cache helpers,
and the cache helpers import this, so living in views.py would make the
import circular. It holds no I/O and reads no request — `today` is passed
in, so a demo session can annotate against its simulated date.
"""

import math
from datetime import date

from .dates import is_same_iso_week

# The sidebar progress ring's geometry (#76): radius never varies, so the
# circumference is a fixed stroke-dasharray and only stroke-dashoffset moves.
RING_RADIUS = 7
RING_CIRCUMFERENCE = round(2 * math.pi * RING_RADIUS, 2)  # 43.98


# Project urgency is the highest-ranked task urgency; "done" and "undated"
# rank like "ok" — neither exerts deadline pressure.
_URGENCY_RANK = {
    "overdue": 3,
    "today": 2,
    "urgent": 1,
    "ok": 0,
    "done": 0,
    "undated": 0,
}


# The Kanban board's three columns, keyed by the stage _annotate_tasks
# already assigned. The board used to spell this out three times in the
# template as `{% if task.urgency == ... %}`; a toggle that moves a card
# needs the same rule client-side, and a fourth copy of it in JavaScript is
# exactly the drift #210 is about. So the mapping lives here once, ships as
# a field on every task, and the template renders from that field.
_KANBAN_COLUMN = {
    "overdue": "urgent",
    "today": "urgent",
    "urgent": "urgent",
    "ok": "open",
    "undated": "open",
    "done": "done",
}


def _kanban_column(urgency):
    return _KANBAN_COLUMN[urgency]


def _classify_due_urgency(due, today):
    """The stage a due date alone puts a task in, completion aside.

    Split out of _annotate_tasks so reschedule_task_view can answer with the
    stage the moved task now belongs in instead of leaving the client to
    re-derive a rule that is calendar-week based (#169) and, in a demo
    session, measured against the simulated date rather than the real today.
    Completion stays the caller's business: the dot carries `done` as its own
    class, and the .dot.done rule wins over the urgency one by source order.
    """
    if not due:
        # An open task without a date is its own state (#160) — no deadline
        # pressure, so it never lifts the project urgency.
        return "undated"
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    if is_same_iso_week(due, today):
        # #169: calendar-week based, not a rolling 7-day window — a task due
        # next week is not urgent today. Closing the week (see closeout.py) is
        # what arms next week's signal, and it does that for free just by the
        # calendar rolling over.
        return "urgent"
    return "ok"


def _annotate_tasks(projects, today):
    for project in projects:
        # Chronological order for every task-list view, dateless tasks last
        # (#140). `done` is deliberately not part of the key: done tasks stay
        # interleaved at their date position, and the cached summary's
        # task_refs are positions in this order (_number_projects_and_tasks,
        # ai.py) — a key that moved tasks on toggle would re-point them.
        project["tasks"].sort(key=lambda t: (t["due"] is None, t["due"] or date.max))
        project_urgency = "ok"
        done_count = 0
        for task in project["tasks"]:
            if task["done"]:
                task["urgency"] = "done"
            else:
                task["urgency"] = _classify_due_urgency(task["due"], today)
            # #211 part 2: green says "cleared this week", not "cleared at
            # some point" — the same ISO week the progress bar above the
            # dots counts, so the green dots are exactly the tasks filling
            # it. A done task with no completed_date stays out on purpose:
            # those are the ones checked off before "Erledigt am" existed in
            # the Notion schema, or checked off in Notion's own UI, and
            # without this clause the whole back catalogue would be
            # permanently green (_count_done_in_range documents the same
            # case for the same reason). Computed here rather than in the
            # template because `today` is a request-level fact, and it
            # reuses is_same_iso_week (#169) rather than restating what a
            # week is.
            completed = task.get("completed_date")
            task["done_this_week"] = bool(
                task["done"] and completed and is_same_iso_week(completed, today)
            )
            task["kanban_column"] = _kanban_column(task["urgency"])
            if _URGENCY_RANK[task["urgency"]] > _URGENCY_RANK[project_urgency]:
                project_urgency = task["urgency"]
            if task["done"]:
                done_count += 1
        project["urgency"] = project_urgency
        total_count = len(project["tasks"])
        project["done_count"] = done_count
        project["total_count"] = total_count
        # #283: what a date change would move, which is not the same set as
        # "not done". A completed task stays where it is — its date records
        # when the work was due and met — and a task with no date has no
        # date to shift, so neither belongs in the number the confirmation
        # bar names. Hence this rather than total_count - done_count: a plan
        # holding one undated task would otherwise ask about a task the
        # write cannot touch, and the bar's number is a promise the write
        # has to be able to keep (reschedule_project_view checks it).
        # Counted here rather than in the template for done_count's reason —
        # the server is the one place that gets to say what a count means
        # (#210).
        project["shiftable_count"] = sum(
            1 for task in project["tasks"] if not task["done"] and task["due"]
        )
        fraction = done_count / total_count if total_count else 0
        # An f-string, not Django's floatformat filter: USE_I18N = True could
        # make floatformat emit a comma decimal separator and corrupt this
        # SVG attribute.
        project["ring_dashoffset"] = f"{RING_CIRCUMFERENCE * (1 - fraction):.2f}"
    return projects
