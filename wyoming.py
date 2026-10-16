"""Observed radiosonde soundings from the University of Wyoming
upper-air archive: real balloon launches, fixed station locations,
twice daily. See grib.py/thredds.py for the modeled alternative at an
arbitrary point."""

import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from siphon.simplewebservice.wyoming import WyomingUpperAir

from config import CACHE_DIR


# Pauses between retries of a launch the archive failed to serve, as
# opposed to one it says doesn't exist.
SERVER_ERROR_RETRIES = (3, 10)


class ArchiveUnavailable(RuntimeError):
    """The Wyoming archive answered with an error rather than a sounding
    or a "no such launch"."""


def _http_status(error):
    """The HTTP status behind siphon's ValueError, or None. siphon wraps
    every HTTP error the same way, keeping the status only in the text of
    the HTTPError it chains ("Server Error ( 503: The server is too busy,
    try again later.)" -- the space is siphon's format spec)."""
    m = re.search(r'Server Error \(\s*(\d{3})', str(error.__cause__ or ''))
    return int(m.group(1)) if m else None


def load_sounding(date, station):
    """One launch, from the cache or the archive. Raises ValueError if the
    archive has no such launch (yet), ArchiveUnavailable if it wouldn't
    say.

    The two used to look the same -- siphon turns every HTTP error into a
    bare ValueError -- so a busy archive read as four missing launches in
    a row and the request failed with "No recent sounding data". Only a
    404 means missing; anything else is retried here and then reported as
    what it is."""
    cache_file = CACHE_DIR / f'{station}_{date:%Y%m%d_%HZ}.csv'
    if cache_file.exists():
        return pd.read_csv(cache_file, parse_dates=['time'])
    for pause in (*SERVER_ERROR_RETRIES, None):
        try:
            df = WyomingUpperAir.request_data(date.replace(tzinfo=None), station)
            break
        except ValueError as e:
            status = _http_status(e)
            if status in (None, 404):
                raise
            if pause is None:
                raise ArchiveUnavailable(
                    f'The Wyoming sounding archive is failing (HTTP {status}) -- '
                    'try again in a few minutes') from e
            time.sleep(pause)
    df.to_csv(cache_file, index=False)
    return df


def fetch_recent_sounding(start_date, station, max_tries=4):
    """Load a sounding at start_date, falling back 12h at a time if it
    hasn't posted yet (or doesn't exist for that run)."""
    date = start_date
    for _ in range(max_tries):
        try:
            df = load_sounding(date, station)
            df = df.dropna(subset=('temperature', 'dewpoint', 'direction', 'speed'),
                           how='all')
            # High-resolution modern soundings (e.g. station 72572) can
            # report the same rounded pressure across several consecutive
            # rows -- the pressure sensor's 0.1 hPa resolution is coarser
            # than the actual sampling rate, so height/temperature keep
            # changing while pressure doesn't. That breaks anything
            # (interpolation, the lapse-rate window) that needs pressure
            # to be a valid unique coordinate, so keep only the first row
            # per distinct pressure value.
            df = df.drop_duplicates(subset='pressure').reset_index(drop=True)
            return df, date
        except ValueError:
            date -= timedelta(hours=12)
    raise RuntimeError(f'No recent sounding data available for {station}')


def latest_synoptic_time(now=None):
    """Round down to the most recent 00Z/12Z sounding launch time."""
    now = now or datetime.now(timezone.utc)
    synoptic_hour = 12 if now.hour >= 12 else 0
    return now.replace(hour=synoptic_hour, minute=0, second=0, microsecond=0)
