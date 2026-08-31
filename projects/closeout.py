"""Storage for the weekly close-out ritual (#169).

Two backends behind one interface, the same shape as rules.py: production
persists to the WeekCloseout table; demo mode keeps the visitor's own latest
close-out in the session instead — the same per-session stance as demo_plan
and friends, and, like the close-out flow itself, only ever offered to a
visitor with a session plan (see views.close_week_start). Every public
function takes the request first and branches on DEMO_MODE, so the views
never learn which backend they are talking to. get_latest_closeout returns
the same plain dict shape from either backend, so week_review.html does not
need to know either.
"""

from django.conf import settings
from django.utils import timezone

from .models import WeekCloseout

DEMO_CLOSEOUT_KEY = "demo_week_closeout"


def is_week_closed(request, iso_year, iso_week):
    if settings.DEMO_MODE:
        closeout = request.session.get(DEMO_CLOSEOUT_KEY)
        return bool(
            closeout
            and closeout.get("iso_year") == iso_year
            and closeout.get("iso_week") == iso_week
        )
    return WeekCloseout.objects.filter(iso_year=iso_year, iso_week=iso_week).exists()


def save_closeout(request, iso_year, iso_week, stats, summary_text):
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


def get_latest_closeout(request):
    """The one close-out ever shown (no browsable history, see #169's scope
    note) — always the same plain-dict shape, from either backend."""
    if settings.DEMO_MODE:
        return request.session.get(DEMO_CLOSEOUT_KEY)
    closeout = WeekCloseout.objects.order_by("-iso_year", "-iso_week").first()
    if closeout is None:
        return None
    return {
        "iso_year": closeout.iso_year,
        "iso_week": closeout.iso_week,
        "completed_count": closeout.completed_count,
        "rescheduled_count": closeout.rescheduled_count,
        "added_count": closeout.added_count,
        "summary_text": closeout.summary_text,
        "closed_at": closeout.closed_at.isoformat(),
    }
