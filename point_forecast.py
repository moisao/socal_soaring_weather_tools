"""Point forecast: one site's whole flying day, hour by hour.

Where Simple_Sounding draws one hour's profile in full, this reduces each
hour the sun is up to what a pilot plans around -- wind at every 250 ft,
how high thermals go and how hard they climb on the way, and whether
there is cloud -- and lays the hours side by side (forecast_table.py).

Same models and the same NOAA GRIB2 files as a modeled sounding, and the
same parcel construction for the thermal top, so a column here agrees
with `/site` for that hour. What differs is how much is fetched: a
sounding pulls every isobaric level of one hour and decodes the whole
grid, which is ~130-320 MB an hour and far too much to repeat a dozen
times. Here only the messages this point needs are pulled (see
_wanted_levels) and each is reduced to its one grid point as it is read.

    python point_forecast.py --lat 32.988 --lon -117.122 --name "Little Black"
    python point_forecast.py --lat 32.988 --lon -117.122 --date 2026-10-17
"""

import argparse
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import eccodes
import metpy.calc as mpcalc
import numpy as np
import pandas as pd
import requests
from dateutil import parser as dateutil_parser
from herbie import Herbie
from metpy.units import units
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import grib
from config import CACHE_DIR
from skewt import MIXED_LAYER_DEPTH, make_msl_height_functions

M_TO_FT = 3.280839895
MS_TO_KT = 1.9438445

# Rows of the altitude tables, in feet MSL.
ALTITUDE_STEP_FT = 250


###########################################
# Sources. One entry per model: where its profile and surface fields
# live, and the ways the three differ that matter here.
@dataclass(frozen=True)
class Source:
    model: str            # Herbie's name for it
    label: str            # as shown on the forecast
    cache_prefix: str
    profile_product: str  # isobaric levels
    surface_product: str  # 2 m / 10 m / surface diagnostics (may be the same file)
    levels: tuple         # isobaric levels published, mb, within LEVEL_TOP_MB..1000
    max_lead: int         # the longest forecast any of its runs makes, hours
    cycle_hours: int      # spacing of the runs worth probing for
    max_step_back: int    # how many older runs to try for one valid time
    flux_averaged: bool   # SHTFL is a running mean since the last reset, not instantaneous
    terrain_in_file: bool  # no cached orography (grib._fetch_terrain_height) to lean on

    def lead_exists(self, lead):
        """Whether the model writes a file at this lead at all. GFS goes
        hourly to f120 and 3-hourly beyond; the others are hourly as far
        as they reach, which only the bucket can say."""
        if self.model == 'gfs':
            # From f01: the analysis has no heat flux, being a mean over
            # hours that haven't been forecast yet.
            return 1 <= lead <= self.max_lead and (lead <= 120 or lead % 3 == 0)
        return 0 <= lead <= self.max_lead


# Top of the isobaric levels ever fetched. 500 mb is ~18,000 ft, the floor
# of Class A airspace and above any thermal worth planning for.
LEVEL_TOP_MB = 500
_EVERY_25_MB = tuple(range(1000, LEVEL_TOP_MB - 1, -25))

# max_lead is each model's longest run (HRRR f48 and RRFS f84 from their
# 00/06/12/18Z cycles, f18 otherwise): a day beyond it is passed to the next
# model without a lookup. Raise it if NOAA extends one.
SOURCES = {
    'hrrr': Source('hrrr', 'HRRR 3 km', 'HRRR', 'prs', grib.SURFACE_PRODUCT['hrrr'], _EVERY_25_MB,
                   max_lead=48, cycle_hours=1, max_step_back=12, flux_averaged=False,
                   terrain_in_file=False),
    'rrfs': Source('rrfs', 'RRFS 3 km', 'RRFS', 'prs', grib.SURFACE_PRODUCT['rrfs'], _EVERY_25_MB,
                   max_lead=84, cycle_hours=1, max_step_back=12, flux_averaged=False,
                   terrain_in_file=False),
    # The same GFS a modeled sounding falls back to, but read from NOAA's
    # GRIB2 bucket rather than through THREDDS (thredds.py): the THREDDS
    # series is 3-hourly and hands back the surface fluxes on awkward
    # mixed averaging intervals, where the bucket has hourly files with
    # everything in one place.
    'gfs': Source('gfs', 'GFS 0.25\N{DEGREE SIGN}', 'GFS', 'pgrb2.0p25', 'pgrb2.0p25',
                  (1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500),
                  max_lead=384, cycle_hours=6, max_step_back=4, flux_averaged=True,
                  terrain_in_file=True),
}

# How much atmosphere above the ground is fetched to begin with, and how
# close to the top of it a thermal may come before the rest is pulled too
# (see forecast()). Most days here top out well inside 250 hPa (~8,500 ft
# above the ground); fetching to 500 mb regardless would nearly double
# the download for sea-level sites.
INITIAL_DEPTH_MB = 250
EXTEND_WITHIN_MB = 50

SURFACE_LABELS = ('PRES:surface', 'TMP:2 m above ground', 'DPT:2 m above ground',
                  'UGRD:10 m above ground', 'VGRD:10 m above ground', 'GUST:surface',
                  'SHTFL:surface', 'LCDC:low cloud layer', 'MCDC:middle cloud layer',
                  'HCDC:high cloud layer')
TERRAIN_LABEL = 'HGT:surface'
FLUX_LABEL = 'SHTFL:surface'

FETCH_WORKERS = grib.HRRR_FETCH_WORKERS

# Models tried, in order, when none is named. Not config.MODEL_PRIORITY:
# GFS is deliberately left out. At 0.25 deg it is too coarse to trust for
# a site's day, so it is used only when asked for by name; a day beyond
# RRFS and HRRR says so rather than quietly coming back from GFS.
DEFAULT_MODELS = ('rrfs', 'hrrr')


class Unavailable(RuntimeError):
    """No model has data for what was asked."""


###########################################
# Which run and lead answers each hour.

# (model, run, lead) known to be posted -- a posted file stays posted.
_posted = {}
# (model, run) -> (lowest lead found missing, when): a run that hasn't
# reached a lead hasn't reached any later one either, so one miss answers
# for the rest of the day's hours without a lookup each. Forgotten after
# RUN_MISS_TTL, since a run still being posted reaches further by then.
_missing_from = {}
# Herbie objects from the probes, reused for the fetch that follows.
_herbies = {}


def _herbie(source, run, lead, product):
    key = (source.model, run, lead, product)
    if key not in _herbies:
        if len(_herbies) > 500:
            _herbies.clear()
        # priority='aws': the byte-range fetch needs S3 anyway, and a miss
        # then costs one lookup instead of a walk through every mirror.
        _herbies[key] = Herbie(run.replace(tzinfo=None), model=source.model, product=product,
                               fxx=lead, priority='aws', verbose=False)
    return _herbies[key]


def _is_posted(source, run, lead):
    key = (source.model, run, lead)
    if key in _posted:
        return True
    # Already in the cache is as good as posted, and costs no lookup --
    # which is most of what a repeat request would otherwise wait on.
    if HourFile(source, run, lead).index['messages']:
        _posted[key] = True
        return True
    now = datetime.now(timezone.utc)
    missing_lead, checked_at = _missing_from.get(key[:2], (None, None))
    if missing_lead is not None and lead >= missing_lead and now - checked_at < grib.RUN_MISS_TTL:
        return False
    # Both files, and the index of each: a run being uploaded has its
    # GRIB2 up before the .idx the byte ranges come from, and its
    # isobaric file before its surface one.
    products = {source.profile_product, source.surface_product}
    try:
        posted = all(herbie.grib is not None and herbie.idx is not None
                     for herbie in (_herbie(source, run, lead, product) for product in products))
    except Exception:
        posted = False
    if posted:
        if len(_posted) > 10_000:
            _posted.clear()
        _posted[key] = True
    else:
        for product in products:
            _herbies.pop((*key, product), None)
        if missing_lead is None or lead < missing_lead or now - checked_at >= grib.RUN_MISS_TTL:
            _missing_from[key[:2]] = (lead, now)
    return posted


def _latest_run(source):
    """The newest run with its analysis up, stepping back from now."""
    run = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    run -= timedelta(hours=run.hour % source.cycle_hours)
    for _ in range(source.max_step_back):
        if _is_posted(source, run, 0):
            return run
        run -= timedelta(hours=source.cycle_hours)
    raise Unavailable(f'No recent {source.model.upper()} run found')


def _resolve(source, valid_times):
    """{valid time: (run, lead)} for every hour this model can answer.

    The rule a modeled sounding uses for one valid time
    (grib._fetch_grib_profile, case 4), applied to each hour in turn: the
    freshest run that reaches a future hour, the run at a past hour as its
    own analysis, and in both cases an older run at a longer lead where
    that one doesn't exist -- so the valid time never moves, the forecast
    only ages. Hours no run within max_step_back reaches are left out."""
    # Out of every run's reach: say so without asking the bucket.
    now = datetime.now(timezone.utc)
    if min(valid_times) - now > timedelta(hours=source.max_lead):
        return {}
    latest = _latest_run(source)

    def resolve(valid):
        run = min(latest, valid)
        run -= timedelta(hours=run.hour % source.cycle_hours)
        for _ in range(source.max_step_back):
            lead = round((valid - run).total_seconds() / 3600)
            if source.lead_exists(lead) and _is_posted(source, run, lead):
                return run, lead
            run -= timedelta(hours=source.cycle_hours)
        return None

    # Every hour's walk at once: each step is a ~0.6 s round trip to the
    # bucket, and one at a time a day of them came to ~8 s whenever the
    # misses had aged out of _missing_from. The memos are plain dicts and
    # only ever hints, so threads racing on them can at worst repeat a
    # lookup.
    with ThreadPoolExecutor(max_workers=len(valid_times)) as pool:
        answers = dict(zip(valid_times, pool.map(resolve, valid_times)))
    return {valid: answer for valid, answer in answers.items() if answer is not None}


###########################################
# Fetching and decoding. One file per (model, run, lead), holding
# whichever messages have been asked of it so far.

def _session():
    session = requests.Session()
    retry = Retry(total=4, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(pool_connections=FETCH_WORKERS,
                                          pool_maxsize=FETCH_WORKERS, max_retries=retry))
    return session


def _inventory(source, run, lead, product):
    """The product's index as {label: row}, labels being the index's own
    'VAR:level' ("TMP:850 mb", "SHTFL:surface").

    Only instantaneous fields, which is what every label here means --
    the same variable and level also turn up as hourly maxima and
    averages -- except GFS's sensible heat flux, which exists only as an
    average (see _instantaneous_flux)."""
    df = _herbie(source, run, lead, product).inventory()
    rows = {}
    for row in df.itertuples():
        label = f'{row.variable}:{row.level}'
        instantaneous = re.fullmatch(r'anl|\d+ hour fcst', row.forecast_time) is not None
        averaged_flux = (source.flux_averaged and label == FLUX_LABEL
                         and re.fullmatch(r'\d+-\d+ hour ave fcst', row.forecast_time))
        if (instantaneous or averaged_flux) and label not in rows:
            rows[label] = row
    return rows


class HourFile:
    """The cached messages of one (model, run, lead): a GRIB2 file that is
    only ever appended to, and a sidecar naming what is in it, in order.

    Kept per run rather than per site because a message is the whole
    grid: once it is on disk, every other site asking for that hour reads
    it for free. Appended to rather than fetched whole because sites want
    different levels -- a mountain launch needs nothing below 850 mb, and
    a coastal one rarely anything above 700 -- and the union is far less
    than everything.

    The sidecar records the file's size as of its last write and is
    replaced only after the data is safely down, so a fetch interrupted
    mid-append leaves a tail the next call truncates away rather than
    messages nobody can name."""

    def __init__(self, source, run, lead):
        self.source, self.run, self.lead = source, run, lead
        stem = f'{source.cache_prefix}_{run:%Y%m%d_%HZ}_f{lead:03d}.point'
        self.grib_path = CACHE_DIR / f'{stem}.grib2'
        self.index_path = CACHE_DIR / f'{stem}.json'
        self.index = {'size': 0, 'messages': [], 'absent': [], 'points': {}}
        try:
            index = json.loads(self.index_path.read_text())
            if self.grib_path.stat().st_size >= index['size']:
                self.index = {'points': {}, **index}
        except (OSError, ValueError, KeyError):
            pass

    def _save_index(self):
        tmp = self.index_path.with_suffix('.json.part')
        tmp.write_text(json.dumps(self.index))
        os.replace(tmp, self.index_path)

    def labels(self):
        return {message['label'] for message in self.index['messages']}

    def ensure(self, labels, session, pool):
        """Fetch whichever of these labels the file doesn't hold yet.
        Returns the bytes downloaded. A label the model simply doesn't
        publish at this lead is remembered as absent, not retried."""
        missing = set(labels) - self.labels() - set(self.index['absent'])
        if not missing:
            return 0
        surface = _surface_labels(self.source)
        if self.source.surface_product == self.source.profile_product:
            by_product = {self.source.profile_product: missing}
        else:
            by_product = {self.source.profile_product: missing - surface,
                          self.source.surface_product: missing & surface}

        messages, blobs = [], []
        for product, wanted in by_product.items():
            if not wanted:
                continue
            herbie = _herbie(self.source, self.run, self.lead, product)
            if herbie.grib is None or herbie.idx is None:
                raise RuntimeError(f'{self.source.model.upper()} {self.run:%Y-%m-%d %HZ} '
                                   f'f{self.lead:02d} {product} is not posted')
            inventory = _inventory(self.source, self.run, self.lead, product)
            rows = [inventory[label] for label in sorted(wanted & inventory.keys())]

            def fetch(row, url=herbie.grib):
                end = '' if pd.isna(row.end_byte) else int(row.end_byte)
                r = session.get(url, headers={'Range': f'bytes={int(row.start_byte)}-{end}'},
                                timeout=60)
                r.raise_for_status()
                return r.content

            for row, content in zip(rows, pool.map(fetch, rows)):
                messages.append({'label': f'{row.variable}:{row.level}', 'time': row.forecast_time})
                blobs.append(content)

        fetched = {message['label'] for message in messages}
        self.index['absent'] = sorted(set(self.index['absent']) | (missing - fetched))
        if blobs:
            with open(self.grib_path, 'ab') as f:
                f.truncate(self.index['size'])
                f.seek(self.index['size'])
                for blob in blobs:
                    f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            self.index['messages'] += messages
            self.index['size'] += sum(len(blob) for blob in blobs)
        self._save_index()
        return sum(len(blob) for blob in blobs)

    def decode(self, lat, lon, labels=None):
        """{label: value at the grid point nearest (lat, lon)} for the
        labels asked for (all of them by default), plus that grid point
        under '_lat'/'_lon' and, on a Lambert grid, '_cone'/'_lov' for
        turning its grid-relative winds to true north.

        Decodes one message at a time and keeps one number from each, so
        nothing the size of the grid outlives its own message -- unlike
        grib._load_fields, which holds a whole run in memory to serve a
        batch of sites and has no reason to here.

        The numbers are kept in the sidecar, per point, so asking again
        decodes nothing: each message is a whole-grid decode (~11 ms), and
        a day of them was ~7 s of every repeat request. A message's value
        at a point never changes, so they need no invalidating; a label
        appended since is simply decoded the next time it's asked for."""
        key = f'{lat:.5f},{lon:.5f}'
        cached = self.index['points'].get(key, {'_times': {}})
        wanted = {m['label'] for m in self.index['messages']} if labels is None else set(labels)
        if wanted & self.labels() <= cached.keys():
            point = {k: v for k, v in cached.items() if k.startswith('_') and k != '_times'}
            point['_times'] = {k: v for k, v in cached['_times'].items() if k in wanted}
            point.update({k: v for k, v in cached.items() if k in wanted})
            return point

        point = {'_times': {}}
        with open(self.grib_path, 'rb') as f:
            for message in self.index['messages']:
                gid = eccodes.codes_grib_new_from_file(f)
                if gid is None:
                    raise RuntimeError(f'{self.grib_path.name} is shorter than its index')
                try:
                    label = message['label']
                    if label not in wanted:
                        continue
                    if label in cached:
                        point[label] = cached[label]
                        point['_times'][label] = cached['_times'][label]
                        continue
                    index, point['_lat'], point['_lon'] = _nearest_grid_point(
                        self.source.model, gid, lat, lon)
                    point[label] = float(eccodes.codes_get_double_element(gid, 'values', index))
                    point['_times'][label] = message['time']
                    if '_cone' not in point and eccodes.codes_get(gid, 'gridType') == 'lambert':
                        point['_cone'] = grib._lcc_cone_constant(
                            eccodes.codes_get(gid, 'Latin1InDegrees'),
                            eccodes.codes_get(gid, 'Latin2InDegrees'))
                        point['_lov'] = eccodes.codes_get(gid, 'LoVInDegrees')
                finally:
                    eccodes.codes_release(gid)
        # Everything cached has to come along, or a point that only ever
        # re-read cached labels would lose its grid point and rotation.
        times = {**cached.get('_times', {}), **point['_times']}
        point = {**{k: v for k, v in cached.items() if k.startswith('_')}, **point}
        cached.update({k: v for k, v in point.items() if k != '_times'})
        cached['_times'] = times
        self.index['points'][key] = cached
        self._save_index()
        point['_times'] = {k: v for k, v in cached['_times'].items() if k in wanted}
        return point


@lru_cache(maxsize=256)
def _terrain_height(model, lat, lon):
    """grib._fetch_terrain_height, memoized: it loads the model's whole
    orography and builds a KD-tree over it on every call (~0.6 s), for an
    answer that never changes."""
    return grib._fetch_terrain_height(model, lat, lon)


def _surface_labels(source):
    return set(SURFACE_LABELS) | ({TERRAIN_LABEL} if source.terrain_in_file else set())


# (model, grid size, lat, lon) -> (index, grid lat, grid lon). The index
# is the same for every message on a grid, and finding it means decoding
# the grid's two coordinate arrays.
_grid_points = {}


def _nearest_grid_point(model, gid, lat, lon):
    key = (model, eccodes.codes_get(gid, 'numberOfDataPoints'), lat, lon)
    if key not in _grid_points:
        lats = eccodes.codes_get_array(gid, 'latitudes')
        lons = eccodes.codes_get_array(gid, 'longitudes')
        # Plain distance in degrees, longitude unscaled: the metric
        # grib.py's KD-tree uses, so this lands on the grid point a
        # sounding of the same site does.
        dlon = (lons - lon + 180) % 360 - 180
        index = int(np.argmin((lats - lat) ** 2 + dlon ** 2))
        if len(_grid_points) > 200:
            _grid_points.clear()
        _grid_points[key] = (index, float(lats[index]), float((lons[index] + 180) % 360 - 180))
    return _grid_points[key]


def _level_labels(levels, humidity_levels):
    labels = {f'{var}:{level} mb' for level in levels for var in ('HGT', 'TMP', 'UGRD', 'VGRD')}
    return labels | {f'RH:{level} mb' for level in humidity_levels}


def _wanted_levels(available, surface_pressures, top_mb):
    """(levels, humidity_levels) to fetch, from the isobaric levels a
    model publishes and the day's range of surface pressure.

    Nothing below the ground: those levels are extrapolated, and are
    dropped from the profile anyway. Humidity only as deep as the parcel
    needs it -- the mixed layer its moisture is averaged over (see
    skewt.MIXED_LAYER_DEPTH), to the first level beyond so the layer's
    top can be interpolated. RH rather than the dewpoint a sounding
    fetches: same information, half the bytes on HRRR and a third on
    RRFS."""
    levels = sorted((lvl for lvl in available if top_mb <= lvl < max(surface_pressures)),
                    reverse=True)
    mixed_top = min(surface_pressures) - MIXED_LAYER_DEPTH.m
    humidity = [lvl for lvl in levels if lvl > mixed_top]
    humidity += [lvl for lvl in levels if lvl <= mixed_top][:1]
    return levels, humidity


###########################################
# One hour, reduced to what the tables show.

def _earth_wind(point, u, v):
    """(speed kt, direction deg) from a model's u/v, rotated off the
    grid where the grid isn't north-up (see grib._fetch_grib_profile)."""
    if '_cone' in point:
        diff = ((point['_lon'] - point['_lov'] + 180) % 360) - 180
        angle = math.radians(point['_cone'] * diff)
        u, v = v * math.sin(angle) + u * math.cos(angle), v * math.cos(angle) - u * math.sin(angle)
    return math.hypot(u, v) * MS_TO_KT, math.degrees(math.atan2(-u, -v)) % 360


def _profile(point, terrain_height):
    """The hour's profile in the schema every sounding source here
    produces (see grib.py), surface first: the model's own 2 m / 10 m
    diagnostics as the ground row, and above it each isobaric level that
    is above the ground. Dewpoint is NaN above the levels humidity was
    fetched for."""
    surface_pressure = point['PRES:surface'] / 100.
    speed, direction = _earth_wind(point, point['UGRD:10 m above ground'],
                                   point['VGRD:10 m above ground'])
    rows = [{'pressure': surface_pressure, 'height': terrain_height + 2.,
             'temperature': point['TMP:2 m above ground'] - 273.15,
             'dewpoint': point['DPT:2 m above ground'] - 273.15,
             'speed': speed, 'direction': direction}]
    levels = sorted((int(label.split(':')[1].split()[0]) for label in point
                     if label.startswith('TMP:') and label.endswith(' mb')), reverse=True)
    for level in levels:
        try:
            height = point[f'HGT:{level} mb']
            temperature = point[f'TMP:{level} mb'] - 273.15
            speed, direction = _earth_wind(point, point[f'UGRD:{level} mb'], point[f'VGRD:{level} mb'])
        except KeyError:
            continue
        if height < terrain_height or level >= surface_pressure:
            continue
        dewpoint = float('nan')
        if f'RH:{level} mb' in point:
            # Floored for the same reason as thredds.py: an exact 0 %
            # takes log(0) inside the conversion.
            dewpoint = mpcalc.dewpoint_from_relative_humidity(
                units.Quantity(temperature, 'degC'),
                units.Quantity(max(point[f'RH:{level} mb'], 0.01), 'percent')).m
        rows.append({'pressure': float(level), 'height': height, 'temperature': temperature,
                     'dewpoint': dewpoint, 'speed': speed, 'direction': direction})
    df = pd.DataFrame(rows)
    df['latitude'], df['longitude'] = point['_lat'], point['_lon']
    return df


def thermal_top(profile):
    """(pressure hPa, is_cloud_base) of the top of the surface parcel's
    climb, or (None, None) where it has none -- capped below by an
    inversion on the ground, say.

    The construction skewt.render_skewt_panel marks as "Thermal tops" /
    "Cloud base", repeated here rather than shared because there it is
    tangled into the drawing: a parcel with the 2 m temperature and the
    mixed layer's mean moisture, followed up to where it first meets the
    environment, or to its condensation level if it gets there first."""
    p = profile['pressure'].values * units.hPa
    T = profile['temperature'].values * units.degC
    moist = np.isfinite(profile['dewpoint'].values)
    Td = profile['dewpoint'].values[moist] * units.degC
    _, _, mixed_dewpoint = mpcalc.mixed_parcel(p[moist], T[moist], Td, depth=MIXED_LAYER_DEPTH)

    parcel = mpcalc.parcel_profile(p, T[0], mixed_dewpoint).to('degC')
    lcl_pressure, _ = mpcalc.lcl(p[0], T[0], mixed_dewpoint)
    cross_pressure, _ = mpcalc.find_intersections(p, parcel, T, direction='decreasing', log_x=True)
    top = cross_pressure[0].m if len(cross_pressure) > 0 else lcl_pressure.m
    top = max(top, lcl_pressure.m, p.m[-1])

    parcel_at_top = np.interp(top, p.m[::-1], parcel.m[::-1])
    env_at_top = np.interp(top, p.m[::-1], T.m[::-1])
    if parcel_at_top < env_at_top - 0.05:
        return None, None
    return float(top), bool(np.isclose(top, lcl_pressure.m))


# Shape of a thermal's strength with height, from Lenschow & Stephens
# (1980): w/w* = (z/zi)^(1/3) (1 - 1.1 z/zi), which peaks a quarter of
# the way up and is gone by 0.9 zi.
def _lenschow(x):
    x = np.clip(x, 0., 1 / 1.1)
    return np.cbrt(x) * (1 - 1.1 * x)


_LENSCHOW_PEAK = float(_lenschow(1 / 4.4))


@dataclass
class Hour:
    """One column of the forecast."""
    valid: datetime
    run: datetime
    lead: int
    surface_m: float                 # model terrain at the grid point, MSL
    profile: pd.DataFrame = field(repr=False)
    top_m: float | None = None       # top of lift, MSL; None = no thermals
    cu_base_m: float | None = None   # cumulus base, MSL; None = blue (or no) thermals
    wstar: float | None = None       # m/s; None = the model gave no heat flux
    heat_flux: float | None = None   # W/m2, upward
    cloud: tuple = (None, None, None)  # low, middle, high cover, %
    gust_kt: float | None = None
    top_pressure: float | None = None

    @property
    def surface_temp_c(self):
        return float(self.profile['temperature'].iloc[0])

    @property
    def surface_wind(self):
        """(direction deg, speed kt) at 10 m."""
        return float(self.profile['direction'].iloc[0]), float(self.profile['speed'].iloc[0])

    def launch_wind(self, launch_m):
        """(direction deg, speed kt) at launch: the model's wind at the
        launch's own height. The 10 m wind where launch is unknown or no
        higher than the model's ground.

        A 3 km grid flattens a ridge -- Little Black's cell sits at 176 m
        under a 357 m launch -- so the 10 m wind is a low, sheltered
        slope's. The air at launch height is what a ridge or summit
        actually sits in; checked on 2026-10-16 against the weather
        station on Black Mountain's summit, it matched the afternoon's
        sustained wind to within a few mph where the 10 m wind ran 5-8
        mph under."""
        if launch_m is None or launch_m <= self.profile['height'].iloc[0]:
            return self.surface_wind
        return self.wind_at(launch_m) or self.surface_wind

    def launch_gust_kt(self, launch_m):
        """Gust at launch, kt -- an estimate, never below the model's own
        gust or the launch wind.

        While thermals are mixing through launch height, the launch wind
        scaled by the model's gust-to-wind ratio at its ground (held to
        1-1.5). Otherwise the air at launch is the free flow above a
        decoupled surface, and the model's surface ratio says nothing
        about it, so no scaling: before this, a calm 3 mph 10 m wind
        under a 30 mph morning jet came out as 90 mph gusts.

        Checked on 2026-10-16 against the weather station on Black
        Mountain's summit (~380 ft above Little Black's launch): mean
        error 7 mph over 8am-5pm, against 12 for the model's 10 m gust
        and 9 for xcskies'. One day at one site; the 1.5 cap is the knob."""
        if self.gust_kt is None:
            return None
        _, launch_speed = self.launch_wind(launch_m)
        gust = max(self.gust_kt, launch_speed)
        mixing = launch_m is not None and self.top_m is not None and self.top_m > launch_m
        if mixing:
            _, surface_speed = self.surface_wind
            ratio = min(max(self.gust_kt / max(surface_speed, 1.), 1.), 1.5)
            gust = max(gust, launch_speed * ratio)
        return gust

    def wind_at(self, height_m):
        """(direction deg, speed kt) at an MSL height, interpolated
        between levels by component; None below the ground or above the
        levels fetched."""
        heights = self.profile['height'].values
        if not heights[0] <= height_m <= heights[-1]:
            return None
        angle = np.radians(self.profile['direction'].values)
        speed = self.profile['speed'].values
        u = np.interp(height_m, heights, -speed * np.sin(angle))
        v = np.interp(height_m, heights, -speed * np.cos(angle))
        return float(np.degrees(np.arctan2(-u, -v)) % 360), float(np.hypot(u, v))

    def lift_at(self, height_m):
        """Expected updraft in a thermal core at an MSL height, m/s --
        the air's own climb, before a glider's sink comes off it. 0
        outside the layer thermals reach; None if there is no flux to
        size them from.

        w* (Deardorff's convective velocity scale: surface heat flux
        mixed through the depth of the layer) sets the strength and
        Lenschow's profile spreads it over height, scaled so the
        strongest level reads w* itself. That is the convention the
        soaring forecasts built on w* use -- w* as the day's thermal
        strength -- and what a core comes to against Lenschow's
        whole-thermal mean (Allen 2006 puts the core at about twice it,
        i.e. 0.9 w* at its best height)."""
        if self.wstar is None:
            return None
        if self.top_m is None or not self.surface_m < height_m < self.top_m:
            return 0.
        fraction = (height_m - self.surface_m) / (self.top_m - self.surface_m)
        return float(self.wstar * _lenschow(fraction) / _LENSCHOW_PEAK)


def _analyse(valid, run, lead, point, terrain_height, heat_flux):
    profile = _profile(point, terrain_height)
    hour = Hour(valid, run, lead, terrain_height, profile, heat_flux=heat_flux,
                cloud=tuple(point.get(label) for label in SURFACE_LABELS[-3:]),
                gust_kt=point['GUST:surface'] * MS_TO_KT if 'GUST:surface' in point else None)

    hour.top_pressure, is_cloud_base = thermal_top(profile)
    if hour.top_pressure is not None:
        pressure_to_km, _ = make_msl_height_functions(profile, 'km')
        top_m = float(pressure_to_km(hour.top_pressure)[0]) * 1000
        # A "top" within the 2 m the parcel starts from is a parcel that
        # went nowhere.
        if top_m > terrain_height + 30:
            hour.top_m = top_m
            hour.cu_base_m = top_m if is_cloud_base else None

    if heat_flux is not None:
        hour.wstar = 0.
        if hour.top_m is not None and heat_flux > 0:
            temperature = profile['temperature'].iloc[0] + 273.15
            density = profile['pressure'].iloc[0] * 100 / (287.05 * temperature)
            hour.wstar = (9.80665 / temperature * heat_flux / (density * 1004.)
                          * (hour.top_m - terrain_height)) ** (1 / 3)
    return hour


def _instantaneous_flux(source, files, valid, point, lat, lon, session, pool):
    """Sensible heat flux at this hour, W/m2, or None if not published.

    HRRR and RRFS report it as it is at the valid time. GFS reports only
    its mean since the run's last 6-hourly reset ("108-111 hour ave"), so
    by the end of each window a 2 pm value has the whole morning averaged
    into it; the mean over just the last step is recovered from this
    lead's average and the previous lead's, which share a start."""
    if FLUX_LABEL not in point:
        return None
    flux = point[FLUX_LABEL]
    if not source.flux_averaged:
        return flux
    start, end = (int(n) for n in re.match(r'(\d+)-(\d+)', point['_times'][FLUX_LABEL]).groups())
    run, lead = files[valid].run, files[valid].lead
    step = 1 if lead <= 120 else 3
    if end - start <= step:
        return flux
    previous = HourFile(source, run, lead - step)
    previous.ensure({FLUX_LABEL}, session, pool)
    earlier = previous.decode(lat, lon, {FLUX_LABEL})
    if FLUX_LABEL not in earlier:
        return flux
    return ((end - start) * flux - (end - start - step) * earlier[FLUX_LABEL]) / step


###########################################
# The day.

def sun_times(lat, lon, day):
    """(sunrise, sunset) on a local calendar day, as UTC datetimes, or
    None where the sun doesn't do both (polar day/night).

    NOAA's low-accuracy solar equations -- good to a minute or two at
    these latitudes, and no network call to be down."""
    # Fraction of the year at this longitude's solar noon, in UTC.
    gamma = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1 - lon / 360)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    # 90.833 deg: the sun's upper limb on the horizon, with refraction.
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(math.radians(lat)) * math.cos(decl))
              - math.tan(math.radians(lat)) * math.tan(decl))
    if not -1 <= cos_ha <= 1:
        return None
    ha = math.degrees(math.acos(cos_ha))
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return (midnight + timedelta(minutes=720 - 4 * (lon + ha) - eqtime),
            midnight + timedelta(minutes=720 - 4 * (lon - ha) - eqtime))


def daylight_hours(lat, lon, day):
    """Every whole UTC hour from sunrise to sunset on a local day."""
    sun = sun_times(lat, lon, day)
    if sun is None:
        return []
    hour = sun[0].replace(minute=0, second=0, microsecond=0)
    if hour < sun[0]:
        hour += timedelta(hours=1)
    hours = []
    while hour <= sun[1]:
        hours.append(hour)
        hour += timedelta(hours=1)
    return hours


@dataclass
class PointForecast:
    lat: float
    lon: float
    day: date_cls
    source: Source
    hours: list
    sunrise: datetime
    sunset: datetime
    top_level_m: float          # height of the highest level fetched, lowest over the day
    downloaded_bytes: int = 0
    launch_m: float | None = None  # the site's own launch elevation, MSL, if known

    @property
    def runs(self):
        return sorted({hour.run for hour in self.hours})


def forecast(lat, lon, day, models=None, progress=None, launch_m=None):
    """The point forecast for one local calendar day: an Hour for each
    hour the sun is up, from the first model in `models` (default
    DEFAULT_MODELS) with data for all of them -- or, if none has,
    whichever covers the most.

    One model for the whole day rather than hour-by-hour fallback: a
    column from a different model is a discontinuity that reads as
    weather. progress, if given, is called with (hours done, hours total)
    as the fetch goes. launch_m, the launch's real elevation, is carried
    along for the tables (Hour.launch_wind); nothing fetched depends on
    it."""
    valid_times = daylight_hours(lat, lon, day)
    if not valid_times:
        raise Unavailable(f'The sun does not rise and set at {lat:.2f}, {lon:.2f} on {day}')
    sunrise, sunset = sun_times(lat, lon, day)

    source = resolved = None
    tried, errors = [], []
    for name in models or DEFAULT_MODELS:
        tried.append(name.upper())
        try:
            candidate = _resolve(SOURCES[name], valid_times)
        except Exception as e:
            errors.append(f'{name.upper()}: {e}')
            continue
        if resolved is None or len(candidate) > len(resolved):
            source, resolved = SOURCES[name], candidate
        if len(candidate) == len(valid_times):
            break
    if not resolved:
        when = f'{day:%a %b} {day.day}'
        if errors:
            raise Unavailable(f'No forecast for {when}: ' + '; '.join(errors))
        if len(tried) == 1:
            raise Unavailable(f"{tried[0]} doesn't reach {when} yet.")
        raise Unavailable('Neither ' + ' nor '.join(tried) + f' reaches {when} yet.')

    session = _session()
    files = {valid: HourFile(source, run, lead) for valid, (run, lead) in resolved.items()}
    total = 2 * len(files)
    done = downloaded = 0

    def advance():
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total)

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        # Surface first: its pressure says which isobaric levels are above
        # the ground at all, before any of them is fetched.
        surface = {}
        for valid, hour_file in files.items():
            downloaded += hour_file.ensure(_surface_labels(source), session, pool)
            surface[valid] = hour_file.decode(lat, lon, _surface_labels(source))
            advance()
        missing = [label for label in SURFACE_LABELS[:5]
                   if any(label not in point for point in surface.values())]
        if missing:
            raise RuntimeError(f'{source.model.upper()} surface fields missing: {missing}')
        if source.terrain_in_file:
            terrain_height = next(iter(surface.values()))[TERRAIN_LABEL]
        else:
            terrain_height = _terrain_height(source.model, lat, lon)

        surface_pressures = [point['PRES:surface'] / 100. for point in surface.values()]

        def analyse(top_mb):
            nonlocal downloaded
            levels, humidity = _wanted_levels(source.levels, surface_pressures, top_mb)
            labels = _level_labels(levels, humidity)
            hours = []
            for valid, hour_file in files.items():
                downloaded += hour_file.ensure(labels, session, pool)
                point = {**hour_file.decode(lat, lon, labels), **surface[valid]}
                flux = _instantaneous_flux(source, files, valid, surface[valid], lat, lon,
                                           session, pool)
                hours.append(_analyse(valid, hour_file.run, hour_file.lead, point,
                                      terrain_height, flux))
                advance()
            return hours, min(levels)

        top_mb = max(LEVEL_TOP_MB, min(surface_pressures) - INITIAL_DEPTH_MB)
        hours, fetched_top = analyse(top_mb)
        # A thermal that climbs to the top of what was fetched has only
        # been measured against the levels that were there: pull the rest
        # and work the day out again (the first pass's messages are
        # already down).
        if fetched_top > LEVEL_TOP_MB and any(
                hour.top_pressure is not None and hour.top_pressure < fetched_top + EXTEND_WITHIN_MB
                for hour in hours):
            total += len(files)
            hours, _ = analyse(LEVEL_TOP_MB)

    return PointForecast(lat, lon, day, source, hours, sunrise, sunset,
                         top_level_m=min(hour.profile['height'].iloc[-1] for hour in hours),
                         downloaded_bytes=downloaded, launch_m=launch_m)


def parse_day(text, today):
    """A day typed by a person, as a date: "17" is the 17th (this month,
    or next once this month's has passed), "today"/"tomorrow", a weekday
    name its next occurrence, anything else whatever dateutil makes of it
    with missing parts taken from today. Raises ValueError otherwise."""
    text = text.strip().lower()
    if text in ('', 'today'):
        return today
    if text == 'tomorrow':
        return today + timedelta(days=1)
    if re.fullmatch(r'\d{1,2}', text):
        day = int(text)
        month_start = today.replace(day=1)
        for _ in range(3):
            try:
                candidate = month_start.replace(day=day)
            except ValueError:
                candidate = None
            if candidate is not None and candidate >= today:
                return candidate
            month_start = (month_start + timedelta(days=32)).replace(day=1)
        raise ValueError(f'No day {day} in the coming months')
    try:
        parsed = dateutil_parser.parse(text, default=datetime(today.year, today.month, today.day))
    except OverflowError as e:
        raise ValueError(str(e)) from e
    return parsed.date()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Hour-by-hour soaring forecast for one point.')
    parser.add_argument('--lat', type=float, required=True)
    parser.add_argument('--lon', type=float, required=True)
    parser.add_argument('--name', default=None, help='Label for the site, shown in the title')
    parser.add_argument('--launch-ft', type=float, default=None,
                        help='Launch elevation, ft MSL: the wind tables then show the wind and '
                             'an estimated gust at launch height rather than the model\'s 10 m wind')
    parser.add_argument('--date', default='today',
                        help='Local day to forecast: a date, a day of the month, a weekday, '
                             '"today" or "tomorrow" (default: %(default)s)')
    parser.add_argument('--timezone', default=os.environ.get('SOUNDING_TIMEZONE')
                        or 'America/Los_Angeles',
                        help='Zone the day and the hours are in (default: %(default)s)')
    parser.add_argument('--model', choices=sorted(SOURCES), default=None,
                        help='Use this model only, instead of '
                             + ' then '.join(DEFAULT_MODELS) + ' (GFS only when named)')
    args = parser.parse_args(argv)

    from forecast_table import render_tables
    tz = ZoneInfo(args.timezone)
    try:
        day = parse_day(args.date, datetime.now(tz).date())
    except ValueError as e:
        parser.error(f'Invalid date: {args.date!r} ({e})')
    try:
        result = forecast(args.lat, args.lon, day, models=[args.model] if args.model else None,
                          progress=lambda done, total: print(f'\r{done}/{total}', end='', flush=True),
                          launch_m=args.launch_ft / M_TO_FT if args.launch_ft else None)
    except Unavailable as e:
        raise SystemExit(str(e)) from None
    print(f'\n{result.source.label}, {result.downloaded_bytes / 1e6:.0f} MB downloaded')
    for path in render_tables(result, tz, name=args.name, output_dir=Path.cwd()):
        print(path)


if __name__ == '__main__':
    main()
