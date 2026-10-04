"""Replaying earlier renders instead of fetching and plotting again.

A cold modeled sounding costs over a minute and hundreds of MB of GRIB2;
re-sending an image already drawn from the same data costs nothing. So
each render is indexed by the request that produced it, and a repeat
request gets the stored image back for as long as it is still what a
fresh render would draw:

- Observed soundings (Wyoming) never change once launched, so a render of
  the synoptic time asked for is kept for good. One that fell back to an
  earlier launch (the requested one not posted yet) is retried after
  OBSERVED_RETRY.
- HRRR/RRFS renders know their run, and stay current until the bucket has
  a newer run covering the same valid time (grib.newer_run_posted) -- a
  forecast is replaced as soon as a fresher run reaches its hour, an
  analysis as soon as the next run posts.
- GFS doesn't expose which cycle it answered from, and a render that had
  to fall back from the preferred model may be beatable once that model
  is back, so both are simply kept for FALLBACK_TTL.

Once a requested valid time is SETTLE[model] in the past, nothing newer
can still post for it, so its render is marked final and never checked
again -- past events are held indefinitely.

The index is a directory of small JSON files beside the images, one per
request key; the key includes a hash of this program's source, so a new
version of the bot re-renders rather than replaying old-looking plots.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import grib
from config import MODEL_PRIORITY

OBSERVED_RETRY = timedelta(minutes=15)
FALLBACK_TTL = timedelta(hours=1)

# How long after a valid time its newest possible data can still be
# posted. Measured off the buckets: RRFS/HRRR analyses and short leads
# land within ~2 h of their run; GFS cycles are 6-hourly and post ~4-5 h
# late, plus THREDDS ingest.
SETTLE = {'rrfs': timedelta(hours=4), 'hrrr': timedelta(hours=4), 'gfs': timedelta(hours=8)}

SOURCE_VERSION = hashlib.sha256(b''.join(
    path.read_bytes() for path in sorted(Path(__file__).parent.glob('*.py')))).hexdigest()[:12]


def _hour(dt):
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _iso(dt):
    return None if dt is None else dt.isoformat()


def _parse(value):
    return None if value is None else datetime.fromisoformat(value)


def request_key(args):
    """Everything about a request that shapes its image, as stable text.
    Times are cut to the hour, the resolution everything downstream works
    at, so "19:30" and "19" share an entry."""
    fields = {name: (_iso(_hour(value)) if isinstance(value, datetime) else value)
              for name, value in sorted(vars(args).items())}
    fields['version'] = SOURCE_VERSION
    return json.dumps(fields, sort_keys=True)


class ReplayIndex:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _entry_path(self, key):
        return self.directory / f'{hashlib.sha256(key.encode()).hexdigest()}.json'

    def lookup(self, args, now=None):
        """The stored PNG for this request if it is still current, else
        None. Must be called with the render lock held, like record()."""
        now = now or datetime.now(timezone.utc)
        path = self._entry_path(request_key(args))
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        png = Path(entry['png'])
        if not png.exists() or not self._current(entry, now):
            return None
        if not entry.get('final') and self._settled(entry, now):
            entry['final'] = True
            self._write(path, entry)
        return png

    def record(self, args, rendered, now=None):
        now = now or datetime.now(timezone.utc)
        requested = args.datetime
        entry = {
            'png': f'{rendered.stub}.png',
            'model': rendered.model,
            'preferred_model': args.model or MODEL_PRIORITY[0],
            'run_time': _iso(rendered.run_time),
            'valid_time': _iso(_hour(rendered.valid_time)),
            'requested_time': _iso(None if requested is None else _hour(requested)),
            'rendered_at': _iso(now),
        }
        entry['final'] = self._settled(entry, now)
        self._write(self._entry_path(request_key(args)), entry)

    @staticmethod
    def _settled(entry, now):
        """Whether nothing newer can ever replace this render: the
        observation asked for, the preferred model's own analysis of the
        hour asked for, or anything once that hour is SETTLE old."""
        requested = _parse(entry['requested_time'])
        if requested is None:
            return False
        if entry['model'] is None:
            return entry['valid_time'] == entry['requested_time']
        if (entry['model'] == entry['preferred_model'] != 'gfs'
                and entry['run_time'] == entry['requested_time']):
            return True
        return now >= requested + SETTLE[entry['model']]

    @staticmethod
    def _current(entry, now):
        if entry['final']:
            return True
        rendered_at = _parse(entry['rendered_at'])
        if entry['model'] is None:
            return now - rendered_at < OBSERVED_RETRY
        if entry['model'] == 'gfs' or entry['model'] != entry['preferred_model']:
            return now - rendered_at < FALLBACK_TTL
        return not grib.newer_run_posted(entry['model'], _parse(entry['run_time']),
                                         _parse(entry['requested_time']), now)

    @staticmethod
    def _write(path, entry):
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(entry, indent=1))
        os.replace(tmp, path)
