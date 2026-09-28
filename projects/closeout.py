"""Storage for the weekly close-out ritual (#169).

Two backends behind one interface, the same shape as rules.py: production
persists to the WeekCloseout table; demo mode keeps the visitor's own latest
close-out in the session instead — the same per-session stance as demo_plan
and friends, and, like the close-out flow itself, only ever offered to a
visitor with a session plan (see views.close_week_start). Every public
function takes the request first and branches on DEMO_MODE, so the views
never learn which backend they are talking to. get_closeout and
get_latest_closeout return the same plain dict shape from either backend, so
week_review.html does not need to know either.
"""

from django.conf import settings
from django.utils import timezone

from .models import WeekCloseout

DEMO_CLOSEOUT_KEY = "demo_week_closeout"


def is_week_closed(request, iso_year, iso_week):
    return get_closeout(request, iso_year, iso_week) is not None


def save_closeout(request, iso_year, iso_week, stats, summary_text):
    """`added_count` arrives as None from a demo close-out — the count has no
    meaning for a plan created in one shot (#215), and None is what makes the
    prompt and the review page leave it out rather than show a zero. Both
    backends store a number, so it is flattened here, in one place, rather
    than at each call site.
    """
    stats = {**stats, "added_count": stats["added_count"] or 0}
    if settings.DEMO_MODE:
        request.session[DEMO_CLOSEOUT_KEY] = {
            "iso_year": iso_year,
            "iso_week": iso_week,
            "completed_count": stats["completed_count"],
            "rescheduled_count": stats["rescheduled_count"],
            "added_count": stats["added_count"],
            "summary_text": summary_text,
            "closed_at": timezone.now().isoformat(),
        }
        return
    WeekCloseout.objects.update_or_create(
        iso_year=iso_year,
        iso_week=iso_week,
        defaults={
            "completed_count": stats["completed_count"],
            "rescheduled_count": stats["rescheduled_count"],
            "added_count": stats["added_count"],
            "summary_text": summary_text,
        },
    )


def _as_dict(closeout):
    """One WeekCloseout row in the plain shape the session backend stores
    natively — the single mapping both getters answer in."""
    return {
        "iso_year": closeout.iso_year,
        "iso_week": closeout.iso_week,
        "completed_count": closeout.completed_count,
        "rescheduled_count": closeout.rescheduled_count,
        "added_count": closeout.added_count,
        "summary_text": closeout.summary_text,
        "closed_at": closeout.closed_at.isoformat(),
    }


def get_closeout(request, iso_year, iso_week):
    """One named week's close-out, or None if that week was never closed.

    #263: once a past week can be closed, "the close-out that was just
    written" stopped being a synonym for "the latest one" — close KW 25
    while KW 26 is already closed and get_latest_closeout answers with KW
    26's numbers. The demo backend hid that by holding exactly one
    close-out, so it happened to be right for the wrong reason.
    """
    if settings.DEMO_MODE:
        closeout = request.session.get(DEMO_CLOSEOUT_KEY)
        if (
            closeout
            and closeout.get("iso_year") == iso_year
            and closeout.get("iso_week") == iso_week
        ):
            return closeout
        return None
    closeout = WeekCloseout.objects.filter(iso_year=iso_year, iso_week=iso_week).first()
    return _as_dict(closeout) if closeout else None


def get_latest_closeout(request):
    """The most recently *closed* week — what week_review falls back to when
    no week is named. Always the same plain-dict shape, from either
    backend."""
    if settings.DEMO_MODE:
        return request.session.get(DEMO_CLOSEOUT_KEY)
    closeout = WeekCloseout.objects.order_by("-iso_year", "-iso_week").first()
    return _as_dict(closeout) if closeout else None
