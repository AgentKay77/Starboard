"""League clock — single source of truth for "today" in the league's timezone.

`date.today()` returns the server's local date, which on the Pi is UTC. That
is one day ahead of America/Chicago for the last 5-6 hours of every CT day,
so submissions made in the evening land on a phantom puzzle-day for
"tomorrow" and APR recompute then penalises every other active player as
"absent" on that ghost day. Always go through this helper instead.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Chicago"


def local_today(tz_name: str = DEFAULT_TZ) -> date:
    """Today's date in the league timezone (default America/Chicago)."""
    return datetime.now(ZoneInfo(tz_name)).date()


def local_now(tz_name: str = DEFAULT_TZ) -> datetime:
    return datetime.now(ZoneInfo(tz_name))
