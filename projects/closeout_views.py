"""The weekly close-out flow's views (#169, #302): the triage page, the
confirm POST and the review page, plus the helpers only they use.

Storage lives in closeout.py and the flow is documented in
docs/wochenabschluss.md. Moved out of views.py in #302, the same way
planner_views.py was: this module imports from views.py, views.py never
imports this one. Every Notion read these pages make happens here, so their
tests patch `projects.closeout_views.<name>`.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .ai import AIUnavailableError, generate_closeout_summary
from .annotate import _annotate_tasks
from .closeout import (
    get_closeout,
    get_latest_closeout,
    is_week_closed,
    save_closeout,
)
from .dashboard_cache import CACHE_KEY
from .date_format import format_date, format_week_range
from .dates import is_same_iso_week, iso_week_bounds
from .notion import (
    NotionUnavailableError,
    get_tasks_completed_in_range,
    get_tasks_created_in_range,
    get_upcoming_projects,
)
from .views import (
    _build_session_project,
    _get_sim_date,
    _group_by_month,
    _strip_trailing_date,
    _week_monday,
    _week_param,
)


def _sidebar_projects(request, today, projects=None):
    """month_groups/years for the sidebar's "Projekte" list (#185) — the
    same shape dashboard() builds inline for its own project-section
    rendering, rebuilt here for close_week_start()/week_review(), which
    don't otherwise fetch project data. Not shared code with dashboard()
    itself: its own `projects` is already sequenced through several other
    steps (week view, day columns, sim-date mutation) this lighter fetch
    has no reason to duplicate.

    `projects` skips the read entirely for a caller that already holds
    the same data — close_week_start() does, and passes its own uncached
    triage fetch in (#185 follow-up). It is an already-fetched list, never
    None as a "nothing found" answer: an empty list is passed through as
    the empty sidebar it means.

    Without it, demo mode always reflects the visitor's own session plan,
    never the example catalog — matching dashboard()'s own rule that the
    catalog only ever shows with no session plan yet or ?mode=multi
    forced, neither a state reachable from these two views. Production
    prefers dashboard()'s own cache to avoid a redundant Notion
    round-trip, falling back to a direct fetch on a cold cache; either
    way, a NotionUnavailableError degrades to an empty list rather than
    breaking a page that exists for reasons other than showing this list.

    The cached projects are annotated in place, not copied first (#185
    follow-up): every Django cache backend serializes on set and get —
    DatabaseCache (settings.CACHES) stores a pickled blob in a table — so
    cache.get() already hands back an object graph no other request
    shares. dashboard() relies on that same guarantee when it writes
    display_name onto its own cached projects, and a copy here would only
    pay for it twice. See SidebarProjectsCacheTest."""
    if projects is None:
        if settings.DEMO_MODE:
            session_plan = request.session.get("demo_plan")
            if not session_plan:
                return [], []
            projects = [_build_session_project(session_plan)]
        else:
            cached = cache.get(CACHE_KEY)
            if cached:
                projects = cached[0]
            else:
                try:
                    projects = get_upcoming_projects(today)
                except NotionUnavailableError:
                    return [], []
    projects = _annotate_tasks(projects, today)
    for project in projects:
        project["display_name"] = _strip_trailing_date(project["name"])
        project["event_date_display"] = format_date(project["event_date"], role="long")
    month_groups = _group_by_month(projects)
    years = sorted({g["year"] for g in month_groups if g["year"]})
    return month_groups, years


def _closeout_dates(request):
    """(today, sim_date) for the close-out flow (#215).

    In demo mode "today" follows the timelapse when a simulated date is set:
    dashboard() already renders the week around it, so a close-out pinned to
    the real calendar day would triage, count and headline a different week
    than the one the visitor is looking at. Production has no simulated date
    and always gets the real one.
    """
    sim_date = None
    if settings.DEMO_MODE:
        sim_date, _ = _get_sim_date(request)
    return sim_date or timezone.localdate(), sim_date


def _closeout_week_monday(raw, current_monday):
    """The week the close-out flow acts on — _week_monday's answer, never a
    later one than the week the request itself falls in.

    #263 made the week a parameter so the review that actually happens on a
    Monday morning can reach the week that just ended. Forward is the
    direction the ritual has no meaning in, which is why the triage page
    offers no link for it — but the value is hand-editable all the same, and
    a close-out stored under a future week outranks every real one in
    get_latest_closeout's ordering, so /wochenrueckblick/ with no parameter
    would answer with it for as long as that week stays in the future.
    Clamped rather than rejected: it is the same "not a week this page can
    act on, show the current one" the parser already applies to everything
    else it cannot use.

    The dashboard's own ?week= (#180) is deliberately *not* clamped —
    browsing ahead is exactly what its next-week link is for.
    """
    return min(_week_monday(raw, current_monday), current_monday)


# #215: the session half of the failure notice's claim ticket. The other
# half rides the redirect URL, so the notice reaches the tab that actually
# failed — see _closeout_read_failed.
CLOSEOUT_FAILED_KEY = "closeout_read_failed"
CLOSEOUT_NOTICE_PARAM = "notice"


def _demo_completed_in_range(tasks, start, end, sim_date):
    """#215: the demo's own answer to "what was completed this week".

    A task toggled by hand carries `completed_date`, written by
    toggle_task_view exactly the way toggle_task writes "Erledigt am" in
    production. The timelapse completes tasks a second way — a moment marks
    everything due on or before the simulated date as done — and it does
    that on a deepcopy (_simulated_project) that is never written back,
    while this flow reads the session plan itself. So a task the moment
    cleared arrives here with no completion date whatever that copy carries
    of its own. The due date is what made it done, so it is the date that
    places it in a week; without this branch a time-travelled demo would
    report the same 0 this issue exists to remove.

    #246: the two ways are asked together rather than the second answering
    only where the first is silent. Since toggle_session_task records a
    completion date of its own, one task can be completed both ways at once —
    struck through by the moment because it is due by `sim_date`, and checked
    off by hand on /mein-plan/, which renders the real date and therefore
    writes the real one. Read as "completed only where the hand-written date
    is missing", that real date displaced the moment's placement and a visitor
    who cleared a task lost it from the very week the close-out was counting.
    So both placements are collected and any one of them inside the range
    counts the task — `any`, not a sum: a task placed there twice is still one
    task.
    """
    count = 0
    for task in tasks:
        placements = []
        completed = task.get("completed_date")
        if completed:
            placements.append(completed)
        if sim_date and task.get("due") and task["due"] <= sim_date:
            placements.append(task["due"])
        if any(start <= placement <= end for placement in placements):
            count += 1
    return count


def _current_projects_for_closeout(request, today):
    """This session's or production's projects for the close-out flow
    (#169) — read fresh, not from the dashboard cache: a stale week's data
    would misclassify the triage list. Returns None if there is nothing to
    close out (demo mode, no session plan).

    Returns projects rather than the flat task list both callers actually
    triage on, so close_week_start() can hand the same fetch to
    _sidebar_projects() (#185 follow-up). An empty list is a real answer
    (production with nothing upcoming) and stays distinct from None."""
    if settings.DEMO_MODE:
        session_plan = request.session.get("demo_plan")
        if not session_plan:
            return None
        return [_build_session_project(session_plan)]
    return get_upcoming_projects(today)


def close_week_start(request):
    today, _ = _closeout_dates(request)
    try:
        projects = _current_projects_for_closeout(request, today)
    except NotionUnavailableError:
        return redirect("dashboard")
    if projects is None:
        return redirect("index")
    tasks = [t for p in projects for t in p["tasks"]]
    # #263: the week being triaged, which is not always the week the request
    # falls in. A weekly review happens on Monday morning as readily as on
    # Friday evening, and ?week= is what lets it reach the week it means.
    current_monday = iso_week_bounds(today)[0]
    browsed_monday = _closeout_week_monday(request.GET.get("week"), current_monday)
    browsed_sunday = browsed_monday + timedelta(days=6)
    is_current_week = browsed_monday == current_monday
    # #263: the rule is the ISO week, whether or not the day has passed —
    # is_same_iso_week keeps genuinely older tasks out, and those are the
    # ones that already have their own signal. A task due Tuesday and still
    # open on Friday belongs to the week being closed, and this page is the
    # one surface whose whole job is deciding what happens to it; the old
    # `>= today` bound cut exactly those out of it.
    open_this_week = [
        t
        for t in tasks
        if not t["done"] and t["due"] and is_same_iso_week(t["due"], browsed_monday)
    ]
    for task in open_this_week:
        # The move button's own label — otherwise "→ nächste Woche" doesn't
        # say which date that actually is. #263: still due + 7 for a task
        # whose date has passed, which can offer a day that is itself in the
        # past. Deliberate: it is a real date the visitor reads before
        # clicking and can correct afterwards, and the alternative — a second
        # group with its own move semantics — buys less than it costs.
        task["next_week_display"] = format_date(
            task["due"] + timedelta(days=7), role="long"
        )
    iso_year, iso_week, _ = browsed_monday.isocalendar()
    # #215: this no longer gates the submit button, it only changes what the
    # page says and offers. Re-closing a week is the supported way to bring
    # a review up to date — both week-scoped counts are read from the week
    # itself and simply recompute — so the template labels the button
    # "Rückblick aktualisieren" and adds a link to the existing review
    # instead of taking the button away.
    already_closed = is_week_closed(request, iso_year, iso_week)
    # #185 follow-up: the projects fetched above, not a second read — on a
    # cold cache this view used to pair its own uncached triage fetch with
    # an identical one inside _sidebar_projects(), and every task toggle or
    # move in this very flow busts that cache (_bust_dashboard_cache), so
    # cold is the normal state here. Sharing the dicts is safe:
    # _annotate_tasks only adds `urgency` (which the triage template doesn't
    # render), and it sorts project["tasks"], not the separate open_this_week
    # list built above.
    month_groups, years = _sidebar_projects(request, today, projects=projects)
    # #215: a close-out that died on a Notion read bounced back here without
    # a word, so the button looked like it had done nothing. The notice shows
    # only when the ticket in the URL matches the one in the session, and
    # clearing it is what consuming the match means:
    #   - a second tab, which has no ticket in its URL, neither sees the
    #     notice nor eats it out from under the tab that earned it;
    #   - reloading the failed tab keeps the ticket in the URL but no longer
    #     matches, so the page stops warning about a failure that is over.
    ticket = request.GET.get(CLOSEOUT_NOTICE_PARAM)
    read_failed = bool(ticket) and request.session.get(CLOSEOUT_FAILED_KEY) == ticket
    if read_failed:
        del request.session[CLOSEOUT_FAILED_KEY]
    return render(
        request,
        "projects/close_week_start.html",
        {
            "tasks": open_this_week,
            "already_closed": already_closed,
            "read_failed": read_failed,
            # #263: the page names the week it is triaging, not the day it
            # was opened on — the two are no longer the same question.
            "week_display": f"KW {iso_week}, {format_week_range(browsed_monday, browsed_sunday)}",
            "week_param": _week_param(browsed_monday),
            # #266: the week's own bounds, so a date picked on this page can
            # be judged against the week being closed without the client
            # deriving a second answer to "which week is this". The form
            # renders them; the comparison is the string form of the
            # is_same_iso_week() close_week_confirm counts with, so what the
            # row shows is a preview of rescheduled_count rather than a
            # second rule that can disagree with it.
            "week_start_iso": browsed_monday.isoformat(),
            "week_end_iso": browsed_sunday.isoformat(),
            "prev_week_param": _week_param(browsed_monday - timedelta(days=7)),
            "is_current_week": is_current_week,
            # A weekend-specific empty state reads oddly on a Tuesday — and
            # on a Saturday spent looking back at a week that is already
            # over, which is why it asks the browsed week too.
            "is_weekend": is_current_week and today.weekday() >= 5,
            # #183 follow-up: the sidebar is now shared with dashboard() via
            # _sidebar_nav.html, so this view owes it the same three flags
            # (see the contract note in that partial). In DEMO_MODE a session
            # plan is guaranteed here (_current_projects_for_closeout redirects
            # to index otherwise), which makes plan_exists true for the same
            # reason has_session_plan is; in production neither applies.
            "demo_mode": settings.DEMO_MODE,
            "has_session_plan": settings.DEMO_MODE,
            "plan_exists": settings.DEMO_MODE,
            "active_nav": "close_week",
            "month_groups": month_groups,
            "years": years,
        },
    )


def _closeout_read_failed(request, week_start):
    """#215: back to the triage page, but saying why.

    Persisting a close-out whose numbers came from a failed read would store
    a zeroed week as if it were the answer — the very thing this issue
    removed — so nothing is saved and the visitor is told to try again.

    This is a redirect (POST/redirect/GET, so a reload cannot re-submit the
    form) and template context does not survive one, so the notice is carried
    across as a claim ticket: a random value stored in the session and
    repeated in the URL. A bare session flag would have been read by whichever
    request arrived first, which in a second open tab is a notice about a
    failure that tab never had. Requiring both halves addresses the notice to
    the one response that follows this redirect.

    #263: and back to the *same* week. This is the only path that returns a
    visitor to the triage page, so dropping the parameter here would land a
    KW 38 review on the KW 39 list, under a notice whose whole message is
    "try again" — and the next press of the button would close the wrong
    week with a triage selection nobody made.
    """
    ticket = secrets.token_urlsafe(8)
    request.session[CLOSEOUT_FAILED_KEY] = ticket
    return redirect(
        f"{reverse('close_week_start')}?week={_week_param(week_start)}"
        f"&{CLOSEOUT_NOTICE_PARAM}={ticket}"
    )


def close_week_confirm(request):
    """#215: the two tiles count the ISO week the page is headlined with.
    Only the reschedule number is scoped to this one interaction, and it is
    rendered as a sentence rather than a tile so the row measures one thing.
    """
    if request.method != "POST":
        return redirect("close_week_start")
    today, sim_date = _closeout_dates(request)
    # #263: the week the page was showing wins, and every number below is
    # measured against it. The form carries it, so a review begun on Friday
    # and submitted on Monday still closes the week it was triaging instead
    # of the one that started a few hours ago — and browsing to a past week
    # is a choice rather than a mismatch to report back. A missing or
    # hand-edited value falls back to the request's own week, the behaviour
    # this flow had when the week was never carried at all.
    week_start = _closeout_week_monday(
        request.POST.get("week"), iso_week_bounds(today)[0]
    )
    week_end = week_start + timedelta(days=6)
    iso_year, iso_week, _ = week_start.isocalendar()
    task_ids = request.POST.getlist("task_id")

    try:
        projects = _current_projects_for_closeout(request, today)
    except NotionUnavailableError:
        return _closeout_read_failed(request, week_start)
    if projects is None:
        return redirect("index")
    tasks = [t for p in projects for t in p["tasks"]]

    # The posted ids have one job left: the triage list is the only surface
    # that moves a task on to next week, so it is the only thing that can
    # say how many this close-out moved. What it actually measures is
    # "changed since the list was loaded" — a move made meanwhile by any
    # other reschedule path lands here too. Session-scoped by nature:
    # "Verschoben" carries no timestamp in Notion (#171), so "moved this
    # week" is not derivable from the schema at all.
    live_by_id = {t["id"]: t for t in tasks}
    rescheduled_count = 0
    for task_id in task_ids:
        task = live_by_id.get(task_id)
        if task is None:
            continue
        if not task["done"] and (
            task["due"] is None or not is_same_iso_week(task["due"], week_start)
        ):
            rescheduled_count += 1

    if settings.DEMO_MODE:
        completed_count = _demo_completed_in_range(
            tasks, week_start, week_end, sim_date
        )
        # A demo plan is created in one shot, so "added this week" has no
        # meaning here (#169). None rather than 0: the prompt then leaves the
        # line out instead of narrating a zero, and week_review.html leaves
        # the tile out instead of showing a number that can never move —
        # which is the very shape of defect this issue is about.
        added_count = None
    else:
        try:
            # Both counts come from their own TASKS_DB reads, not from the
            # posted ids and not from get_upcoming_projects: see the
            # docstrings for the two project filters and the #53 bucket they
            # would otherwise miss. `done` is required alongside the date so
            # "Erledigt" means the checkbox, not a stray hand-set date.
            completed_count = sum(
                1
                for t in get_tasks_completed_in_range(week_start, week_end)
                if t["done"]
            )
            added_count = len(get_tasks_created_in_range(week_start, week_end))
        except NotionUnavailableError:
            return _closeout_read_failed(request, week_start)

    stats_dict = {
        "completed_count": completed_count,
        "rescheduled_count": rescheduled_count,
        "added_count": added_count,
    }
    try:
        summary_text = generate_closeout_summary(stats_dict, week_start)
    except AIUnavailableError:
        summary_text = ""

    save_closeout(request, iso_year, iso_week, stats_dict, summary_text)
    # #263: the review is asked for the week that was just closed. Without
    # the parameter it renders the *latest* close-out, so closing KW 25
    # while KW 26 is already closed would answer with KW 26's numbers.
    return redirect(f"{reverse('week_review')}?week={_week_param(week_start)}")


def week_review(request):
    if settings.DEMO_MODE and not request.session.get("demo_plan"):
        return redirect("index")
    # #263: ?week= names one close-out; without it — or with a week that was
    # never closed — the latest one is still what this page shows, which is
    # every route into it that existed before a past week could be closed.
    browsed_monday = _week_monday(request.GET.get("week"), None)
    closeout = None
    if browsed_monday is not None:
        iso_year, iso_week, _ = browsed_monday.isocalendar()
        closeout = get_closeout(request, iso_year, iso_week)
    if closeout is None:
        closeout = get_latest_closeout(request)
    if closeout is None:
        return redirect("close_week_start")
    # Same date as close_week_start/close_week_confirm (#215), so the sidebar
    # does not jump back to the real week between two pages of one flow.
    today, _ = _closeout_dates(request)
    month_groups, years = _sidebar_projects(request, today)
    return render(
        request,
        "projects/week_review.html",
        {
            "closeout": closeout,
            # #183 follow-up: same three flags as close_week_start() — the
            # sidebar is shared via _sidebar_nav.html. The guard above is
            # this view's own (a demo_plan check, not the closeout lookup),
            # so a session plan is equally guaranteed by the time we render.
            "demo_mode": settings.DEMO_MODE,
            "has_session_plan": settings.DEMO_MODE,
            "plan_exists": settings.DEMO_MODE,
            "active_nav": "close_week",
            "month_groups": month_groups,
            "years": years,
        },
    )
