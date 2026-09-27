"""Shared configuration: values and small helpers more than one module
needs.

Deliberately free of project imports, so every other module can
import this without any risk of an import cycle."""

import os
from datetime import timedelta
from pathlib import Path


def utc_offset_label(dt):
    """'UTC-7' for an aware datetime's own offset, to sit alongside the
    zone's abbreviation in a title ("14:00 PDT (UTC-7)").

    UTC rather than GMT deliberately: GMT is a time zone in its own
    right, while what this prints is an offset from UTC -- which is what
    utcoffset() returns and what aviation and weather products use.

    Taken from the datetime rather than the zone, so it reports the
    offset in force on *that* date rather than today's -- the whole point
    of converting through a real timezone. Half-hour and quarter-hour
    zones come out as 'UTC+5:30' / 'UTC+5:45'."""
    total_minutes = int((dt.utcoffset() or timedelta(0)).total_seconds()) // 60
    sign = '-' if total_minutes < 0 else '+'
    hours, minutes = divmod(abs(total_minutes), 60)
    return f'UTC{sign}{hours}' + (f':{minutes:02d}' if minutes else '')


# Priority order for modeled (arbitrary lat/lon) profiles -- RRFS first
# (NOAA's newer HRRR/RAP successor), then HRRR, then GFS as the global
# fallback. Defined here (rather than down by the fetch functions that
# use it) since parse_args() needs it for --model/--compare-model's
# choices.
MODEL_PRIORITY = ['rrfs', 'hrrr', 'gfs']


# Toggle: overlay the previous synoptic sounding (12h earlier) on top of
# the current one, faded and dashed, for comparison.
SHOW_PREVIOUS = True

# Default unit for the MSL altitude axis: 'km' (1 km ticks) or 'kft'
# (thousands of feet, 3 kft ticks). --altitude-unit overrides it per run,
# threaded through to render_skewt_panel rather than read from here, so
# nothing in this module depends on command-line state having been parsed.
DEFAULT_ALTITUDE_UNIT = 'km'
KM_TO_KFT = 3.280839895

# Cache fetched soundings locally so re-running the script (e.g. while
# tweaking the plot) doesn't have to hit the Wyoming archive every time.
# SOUNDING_CACHE_DIR moves it onto a mounted volume when containerised.
CACHE_DIR = Path(os.environ.get('SOUNDING_CACHE_DIR') or Path(__file__).parent / 'cache')
CACHE_DIR.mkdir(parents=True, exist_ok=True)
