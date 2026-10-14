"""The dashboard's cache: its keys, and every helper that reads, writes,
patches or drops them (#302).

What gets cached, and what each write path does to it afterwards, is
documented in docs/dashboard-write-paths.md. The rule the helpers exist to
keep: after a confirmed Notion write, either patch every cached copy of the
task (_patch_cached_tasks) or bust the cache (_bust_dashboard_cache) — never
leave an entry that contradicts the write.

Imports nothing from views.py, which imports this; the annotation the
patches re-run lives in annotate.py for the same reason.
"""

from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from .ai import SUMMARY_SECTIONS, _number_projects_and_tasks
from .annotate import _annotate_tasks

# Both cache keys and SUMMARY_KEY (which stays in views.py: it keys the demo
# session, not this cache) store the summary's raw reference dict (#122), so
# they carry a version that is bumped on every format change
# (#20: v2/v4; #122: v3/v5; #140: v4/v6 — task order changed, and pre-deploy
# task_refs were numbered against the unsorted order; #160: v5 — the stored
# projects are annotated, and a pre-deploy entry would keep rendering open
# undated tasks as done; #169/#171: v6 — urgent is calendar-week based now,
# and task dicts carry a new postpone_count; #19: v7 — task dicts carry a
# new completed_date, and a pre-deploy entry would undercount "done this
# week" until it expires; #189: v8 — task dicts no longer carry
# due_display, see below; #210: v9 — task dicts carry a new kanban_column,
# and a pre-deploy entry would render an empty Kanban board) — otherwise a
# pre-deploy entry in the old shape would crash or misrender under the new
# resolver; #211: v11 — task dicts carry a new done_this_week, and a
# pre-deploy entry would render every completed dot gray for up to eight
# hours, or forever from the stale copy.
#
# #189 is the one bump that is not correctness-critical: a pre-deploy entry
# still renders right, because the leftover due_display is simply no longer
# read. It is bumped anyway so that STALE_CACHE_KEY, which never expires,
# cannot keep serving a shape no code writes any more — and because a
# formatted date living in this cache was the bug in the first place.
#
# #49 (v12) is correctness-critical again: a stored block keys its heading
# on project_ref, which the resolver no longer reads — every block would be
# dropped for want of a heading and the card would render its empty state
# over a summary that exists. Under a new key the entry is simply not found
# instead, and the page falls into #156's loading state. The price is one
# extra Notion read after the deploy, because CACHE_KEY holds projects and
# summary as one tuple.
#
# #145 (v10) is the same kind: the cached summary_data gained an optional
# kontext_hinweis field, and an entry written before it simply renders
# without the hint. Bumped for the same STALE_CACHE_KEY reason, and so the
# first summary after the deploy can carry a hint instead of the last cached
# one holding it back for up to eight hours. UNASSIGNED_CACHE_KEY goes with
# it by the #19 lockstep even though its own shape is unchanged — the two
# are counted across each other everywhere, and a half-refreshed pair is the
# state _patch_cached_tasks refuses to work with anyway.
#
# #283 (v13) is the same kind again, one level up: every cached project
# gained a `shiftable_count`, and the confirmation bar the new write asks
# through reads it off the rendered page. A pre-deploy entry carries no such
# key, `Number('')` is 0, and a bar that reads 0 offers no shift at all — so
# the write would silently move nothing. Mandatory, not cosmetic, for the
# reason every bump above gives: the cache stores already-annotated projects
# and does not re-annotate on a hit.
CACHE_KEY = "dashboard_data_v13"
CACHE_TTL = 60 * 60 * 8  # 8 hours
# Written alongside CACHE_KEY on every successful fetch, never expired — the
# fallback dashboard() serves when a fresh Notion read fails and the primary
# entry has already expired. See DashboardNotionFailureTest.
STALE_CACHE_KEY = "dashboard_data_stale_v13"

# #53: a separate key pair rather than folded into CACHE_KEY's tuple — this
# is an independent Notion read (get_unassigned_tasks carries no AI summary,
# no partial-success nuance) and keeping it apart avoids reshaping the
# already-tested primary tuple for every existing cache test.
# #19: v2 — task dicts carry the same new completed_date field.
# #189: v3 — and they lost due_display in the same way, bumped in lockstep
# with CACHE_KEY as #19 established.
# #210: v4 — and they gained kanban_column, bumped in the same lockstep.
# #211: v6 — and done_this_week, bumped in the same lockstep again.
# #49: v7 — the summary half of CACHE_KEY's tuple changed shape, not this
# one, which is the #145 case exactly; the lockstep is applied for the same
# reason it was there.
# #283: v8 — and the *project* half changed shape (shiftable_count), not this
# one, which is that same case from the other side. Bumped in lockstep again
# rather than left behind: the lockstep is what #19 decided and every bump
# since has applied, and its reason has not moved — the two entries are
# counted across each other everywhere, and a half-refreshed pair is the
# state _patch_cached_tasks refuses to work with anyway.
UNASSIGNED_CACHE_KEY = "dashboard_unassigned_v8"
UNASSIGNED_CACHE_TTL = 60 * 60 * 8  # 8 hours, same as CACHE_TTL
STALE_UNASSIGNED_CACHE_KEY = "dashboard_unassigned_stale_v8"

# #216: the moment each live entry falls due, stamped when a fresh Notion
# read fills it and never touched afterwards. Django's cache API offers no
# portable "how long has this entry got left", so without a stamp every
# re-write has to name a timeout — and #199 turned a toggle from a delete
# into a re-write. Naming CACHE_TTL there would renew the eight hours on
# every checkbox: check one task off per working day and the dashboard
# never performs an unforced Notion read again, so anything edited in
# Notion's own UI (which _count_done_in_range explicitly expects) stays
# invisible for as long as the patching continues. The TTL is a
# freshness policy about the *read*, not about the last write.
#
# Unversioned: these hold a bare deadline, no task shape, so a pre-deploy
# entry cannot misrender. They are absent rather than wrong before the
# first fetch after a deploy, and absent means "cannot patch safely".
CACHE_DEADLINE_KEY = "dashboard_data_deadline"
UNASSIGNED_CACHE_DEADLINE_KEY = "dashboard_unassigned_deadline"


def _bust_dashboard_cache():
    """Called after every confirmed Notion write, so a completed task or a
    freshly saved project never hides behind CACHE_TTL or the stale fallback.

    The stale pair goes too, which is what makes this the *confirmed*-write
    answer: a last-known-good copy that contradicts a write that happened is
    worse than no copy at all. A write that failed is the other case, and
    _drop_fresh_dashboard_cache below is its answer."""
    _drop_fresh_dashboard_cache()
    cache.delete(STALE_CACHE_KEY)
    cache.delete(STALE_UNASSIGNED_CACHE_KEY)


def _drop_fresh_dashboard_cache():
    """The fresh entries only, with the last-known-good pair left standing.

    #284: trash_project_view reaches this after a *partial* failure, where
    the entry it read no longer describes Notion — but it reaches it because
    Notion is unreachable, and STALE_CACHE_KEY is precisely what dashboard()
    serves in that situation. Dropping it there would answer a half-finished
    removal with an empty dashboard (data_unavailable), and the next load
    cannot read Notion either, so there would be nothing to look at and
    nothing to repeat the action from. A stale copy carrying its own notice
    is the lesser wrong, and it corrects itself on the first read that
    succeeds."""
    cache.delete(CACHE_KEY)
    cache.delete(UNASSIGNED_CACHE_KEY)
    cache.delete(CACHE_DEADLINE_KEY)
    cache.delete(UNASSIGNED_CACHE_DEADLINE_KEY)


def _cache_fresh_read(key, value, deadline_key, ttl):
    """Stores a fresh Notion read together with the moment it falls due.

    The only place a dashboard entry's life is allowed to start over
    (#216). Every later re-write of the same data — a patched toggle, a
    regenerated summary — goes through _remaining_ttl instead, so it can
    put the entry back without pushing its expiry out."""
    cache.set(key, value, ttl)
    cache.set(deadline_key, timezone.now() + timedelta(seconds=ttl), ttl)


def _remaining_ttl(deadline_key):
    """Seconds left before the entry `deadline_key` describes falls due, or
    None when it cannot be re-written without outliving that moment — no
    deadline recorded (a first request after a deploy), or one already
    passed. None is the caller's cue to fall back, never to guess a TTL."""
    deadline = cache.get(deadline_key)
    if deadline is None:
        return None
    remaining = (deadline - timezone.now()).total_seconds()
    return remaining if remaining > 0 else None


def _summary_ref_order(projects):
    """The identity sequence a summary's task_refs are positions in, plus
    the project list those positions were drawn from. Read through
    _number_projects_and_tasks (ai.py) rather than rebuilt here, so the
    check cannot drift from the numbering it is checking.

    Since #49 nothing refers to a project by number, so the project half is
    no longer a ref order of its own — it stays in the comparison because a
    changed project set is a changed task numbering."""
    numbered_projects, numbered_tasks = _number_projects_and_tasks(projects)
    return [p["id"] for p in numbered_projects], [t["id"] for t in numbered_tasks]


def _remap_summary_refs(summary_data, before, after):
    """Rewrites a summary's task_refs from the numbering `before` into the
    one in `after`, so a write that re-sorts the tasks keeps its summary
    instead of dropping it.

    Dropping it was correct but expensive. task_refs are positions in the
    chronological task order (_number_projects_and_tasks, ai.py) and a new
    date moves the task, so the stored positions go on resolving — to the
    wrong tasks. What the summary *says* is still true, though; only the
    numbers it points with are stale. Translating each one through the task
    it meant keeps the summary valid, and keeps the next render off the
    Claude call that regenerating it costs: measured against live data, a
    dashboard that has to regenerate takes ~8.8 s, one that finds a summary
    in the cache ~0.2 s. Every reschedule paid that, one after the other.

    Only tasks are renumbered here, and since #49 they are the only
    positions a block carries at all — its heading is free text.

    A ref whose task is gone from `after` is dropped, the same answer
    resolve_weekly_summary gives one it cannot resolve. A ref that is not a
    position we numbered is passed through untouched — judging the shape of
    Claude's raw dict is the resolver's job, and it belongs in one place.
    """
    if not isinstance(summary_data, dict) or before == after:
        return summary_data
    new_position = {task_id: i for i, task_id in enumerate(after, start=1)}
    moved = {i: new_position.get(task_id) for i, task_id in enumerate(before, start=1)}

    def remapped_ref(ref):
        # bool is an int subclass and would look like position 1, cf.
        # _resolve_ref (ai.py). None means "this task is no longer numbered".
        if isinstance(ref, bool) or not isinstance(ref, int) or ref not in moved:
            return ref
        return moved[ref]

    def remapped_block(block):
        if not isinstance(block, dict) or not isinstance(block.get("task_refs"), list):
            return block
        refs = (remapped_ref(ref) for ref in block["task_refs"])
        return {**block, "task_refs": [ref for ref in refs if ref is not None]}

    remapped = dict(summary_data)
    for key, _title in SUMMARY_SECTIONS:
        blocks = summary_data.get(key)
        if isinstance(blocks, list):
            remapped[key] = [remapped_block(block) for block in blocks]
    return remapped


def _attach_regenerated_summary(numbered_against, summary_data):
    """Writes a freshly generated summary onto the projects the cache holds
    *now*, never onto the snapshot the generating request opened with
    (#216).

    generate_weekly_summary takes seconds. Writing back the projects read
    before it would undo a toggle that patched the cache inside that
    window — the entry would go on serving a task as open that Notion has
    as done, which is the one thing _patch_cached_tasks promises against.
    Only the summary is this request's to contribute.

    The summary is dropped rather than written when the numbering it was
    built against no longer holds. task_refs are positions in that order,
    so a reschedule landing during the call would leave them pointing at
    the wrong tasks — in range, and therefore rendered rather than dropped
    by resolve_weekly_summary. A toggle moves nothing (`done` is not in
    _annotate_tasks' sort key), so its patch keeps the order and the
    summary still fits. A cache busted meanwhile is not resurrected
    either: writing it back would restore the state the bust discarded."""
    current = cache.get(CACHE_KEY)
    if current is None:
        return
    projects = current[0]
    if _summary_ref_order(projects) != _summary_ref_order(numbered_against):
        return
    remaining = _remaining_ttl(CACHE_DEADLINE_KEY)
    if remaining:
        cache.set(CACHE_KEY, (projects, summary_data), remaining)
    cache.set(STALE_CACHE_KEY, (projects, summary_data), None)


def _find_task(projects, unassigned_tasks, task_id):
    """Returns (task, project) — the one place that knows a task can live in
    either of the dashboard's two independently cached lists. `project` is
    None for a task with no project of its own ("Ohne Projekt", #53)."""
    for project in projects:
        for task in project["tasks"]:
            if task["id"] == task_id:
                return task, project
    for task in unassigned_tasks:
        if task["id"] == task_id:
            return task, None
    return None, None


def _patch_cached_tasks(task_id, mutate, today):
    """Applies `mutate(task)` to every cached copy of one task and re-runs
    the cheap derivations on top of it, instead of throwing the whole cache
    away after a confirmed Notion write (#199).

    Returns (projects, unassigned_tasks) — the patched data the caller then
    derives its answer from — or None when the write cannot be reflected
    safely, in which case the caller busts the cache as before. That
    fallback is the normal path, not an edge case: a cold cache, a half-cold
    one, a task no cached list carries, or an entry whose deadline has run
    out from under it all take it.

    The summary survives every write that gets this far. A write that moves
    the task in the chronological order (a reschedule, #140) renumbers the
    task_refs it holds, so they are rewritten rather than the summary being
    dropped — see _remap_summary_refs. A toggle moves nothing, so the same
    call leaves it untouched.
    """
    primary = cache.get(CACHE_KEY)
    unassigned = cache.get(UNASSIGNED_CACHE_KEY)
    if primary is None or unassigned is None:
        # Half a picture is no picture: every figure the dashboard renders
        # is counted across both lists.
        return None
    # #216: read before the write, and separately per pair — the two are
    # independent Notion reads and their deadlines drift apart whenever one
    # of them fails alone. Without both, this write would have to name a
    # fresh TTL and renew the entry's eight hours; the bust it falls back
    # to costs one Notion read and keeps the freshness policy intact.
    primary_ttl = _remaining_ttl(CACHE_DEADLINE_KEY)
    unassigned_ttl = _remaining_ttl(UNASSIGNED_CACHE_DEADLINE_KEY)
    if primary_ttl is None or unassigned_ttl is None:
        return None
    projects, summary_data = primary
    task, project = _find_task(projects, unassigned, task_id)
    if task is None:
        return None
    # Read before the write: the summary's task_refs are positions in this
    # order, and rewriting them needs both sides of the move.
    _, numbered_before = _summary_ref_order(projects)
    mutate(task)
    # Both lists are re-annotated regardless of which one held the task:
    # _annotate_tasks is a pure re-derivation over data already in memory,
    # and paying it twice is cheaper than tracking where the task lived.
    projects = _annotate_tasks(projects, today)
    unassigned = _annotate_tasks([{"id": "_unassigned", "tasks": unassigned}], today)[
        0
    ]["tasks"]
    _, numbered_after = _summary_ref_order(projects)
    cache.set(
        CACHE_KEY,
        (projects, _remap_summary_refs(summary_data, numbered_before, numbered_after)),
        primary_ttl,
    )
    cache.set(UNASSIGNED_CACHE_KEY, unassigned, unassigned_ttl)
    # `project is None` for a task that was found means it came out of the
    # project-less list — which is the half of the stale pair the write
    # concerns, see _patch_stale_copies.
    _patch_stale_copies(task_id, mutate, today, in_project=project is not None)
    return projects, unassigned


def _patch_stale_copies(task_id, mutate, today, in_project):
    """The never-expiring last-known-good entry for the list this write
    concerns, patched in step with the live one.

    Each cache.get hands back its own deserialized object graph, so the
    write has to be applied to this one separately. It can be older than the
    live pair (which expires and gets refilled while it does not), so a
    snapshot predating the task itself cannot be corrected — it is dropped
    rather than left to serve a state older than a confirmed write.

    Only the entry that would carry this task is looked at, hence
    `in_project`. A project task is never in STALE_UNASSIGNED_CACHE_KEY and
    a project-less one is never in STALE_CACHE_KEY, so a miss in one says
    nothing about the other — and the two exist without each other often
    enough for that to matter: dashboard() writes STALE_CACHE_KEY only when
    the summary is not None, so a single Claude outage leaves the
    project-less copy alone in the cache. Judging them together dropped a
    last-known-good copy this write had never touched, in both directions
    (the costlier one being a project-less toggle discarding the projects
    *and* the summary the last Claude call paid for).
    """
    if in_project:
        stale = cache.get(STALE_CACHE_KEY)
        task, _ = _find_task(stale[0] if stale else [], [], task_id)
        if task is None:
            cache.delete(STALE_CACHE_KEY)
            return
        stale_projects, stale_summary = stale
        # Its own numbering, read off its own copy: this entry can be older
        # than the live pair, so the positions its summary holds are not
        # necessarily the ones the live summary holds.
        _, numbered_before = _summary_ref_order(stale_projects)
        mutate(task)
        stale_projects = _annotate_tasks(stale_projects, today)
        _, numbered_after = _summary_ref_order(stale_projects)
        cache.set(
            STALE_CACHE_KEY,
            (
                stale_projects,
                _remap_summary_refs(stale_summary, numbered_before, numbered_after),
            ),
            None,
        )
        return

    stale_unassigned = cache.get(STALE_UNASSIGNED_CACHE_KEY)
    task, _ = _find_task([], stale_unassigned or [], task_id)
    if task is None:
        cache.delete(STALE_UNASSIGNED_CACHE_KEY)
        return
    mutate(task)
    # No renumbering here: the summary numbers project tasks only
    # (_number_projects_and_tasks, ai.py), so moving a project-less task
    # moves no position and the copy's own summary stays valid.
    cache.set(
        STALE_UNASSIGNED_CACHE_KEY,
        _annotate_tasks([{"id": "_unassigned", "tasks": stale_unassigned}], today)[0][
            "tasks"
        ],
        None,
    )
