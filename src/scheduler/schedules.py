"""Clock-aligned schedules: when a Job's next automatic run is due.

A scheduled Job runs at real clock times in the configured timezone - an
hourly job at 00:00, 01:00, 02:00, a six-hourly one at 00:00, 06:00, 12:00,
18:00 - not N hours after the process happened to start.

The grid is a pure function of the clock and the period. Nothing about it is
stored, so a restart, a redeploy or a week of downtime all land on the same
instants, and the next run is always computed forward rather than replayed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

_HOURS_IN_DAY = 24

# Periods that divide the day evenly. The grid is anchored at local midnight,
# so a period that does not divide 24 would leave a short step there: 5 hours
# would give 00, 05, 10, 15, 20 and then a 4-hour gap, which is not the
# uniform schedule the configuration promises.
VALID_PERIOD_HOURS = tuple(h for h in range(1, _HOURS_IN_DAY + 1) if _HOURS_IN_DAY % h == 0)


def next_aligned(now: datetime, period_hours: int) -> datetime:
    """The first grid instant strictly after `now`.

    Strictly after is what gives downtime its semantics: coming back at 09:17
    with an hourly job returns 10:00, never the 06:00 and 07:00 that were
    missed. Runs are skipped, never replayed - the same rule the interval
    check has always followed.

    `now` must be timezone-aware, in the zone the grid is expressed in. The
    arithmetic is wall-clock, which is exact for a zone without daylight
    saving (America/Santo_Domingo, this project's zone). In a DST zone a slot
    inside a spring-forward gap would move by the offset change; no behaviour
    here depends on that today.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    step = timedelta(hours=period_hours)
    elapsed_slots = int((now - midnight) / step)
    return midnight + (elapsed_slots + 1) * step
