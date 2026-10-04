"""Modeled vertical profiles decoded straight from NOAA GRIB2 files
(HRRR, RRFS), plus the URMA surface analysis.

Herbie resolves which run/source exists and parses the .idx sidecar
into byte ranges; the actual fetch and the eccodes decode are done
here. See thredds.py for the THREDDS-based global fallback, which
shares none of this machinery."""

import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import eccodes
import metpy.calc as mpcalc
import numpy as np
import pandas as pd
import requests
from herbie import Herbie
from metpy.units import units
from requests.adapters import HTTPAdapter
from scipy.spatial import cKDTree
from urllib3.util.retry import Retry

from config import CACHE_DIR


###########################################
# Modeled vertical profiles at an arbitrary lat/lon -- unlike the Wyoming
# archive (real radiosonde launches, fixed station locations only), these
# let the "sounding" be a gridded model's own analysis at any point, or
# stand in for an observed comparison sounding at the same station.
#
# Priority order (best resolution first): HRRR (3km CONUS) -> RRFS (NOAA's
# in-development HRRR successor) -> GFS (0.25 deg, global, coarser but
# always available). Each fetcher takes (lat, lon, date) and returns
# (dataframe, actual_valid_time) in the same schema fetch_recent_sounding
# produces (pressure/height/temperature/dewpoint/direction/speed/
# latitude/longitude), so everything downstream -- lapse rate, CCL,
# plotting -- works unchanged regardless of the data's source.


# 1000..400 mb, surface first. Top: the skew-T panel always caps its
# y-axis at 6 km MSL regardless of station (see render_skewt_panel), and
# 400 mb is comfortably above that (~7 km) in any real atmosphere, so
# levels below it would only ever be fetched to be cropped out of the
# plot. Bottom: 1000 mb is a hard ceiling, not a choice -- verified
# against both models' actual inventories, neither HRRR's nor RRFS's
# isobaric ("prslev"/wrfprs) product publishes anything below 1000 mb
# (no 1025/1050 mb messages exist to fetch), so there's no isobaric
# level available for surface pressure above 1000 mb (a ridge, or a
# low-elevation/below-sea-level point) the way there would be for a
# real radiosonde's own reported surface. Getting a genuine near-surface
# point in that situation would need the separate 2m/10m diagnostic
# fields (a different product/level string, plus the surface pressure
# value itself to know what pressure to plot them at) -- not pursued.
# Every isobaric level the model publishes, rather than a fixed list:
# matched as a pattern so each model contributes whatever it has (RRFS
# reaches 2 mb, HRRR 50 mb; both step 25 mb up to 400 and coarsen above).
# The profile used to stop at 400 mb, which was enough for thermals but
# cut off high cloud entirely, and left mpcalc.el() with no crossing to
# find on unstable days -- a convective equilibrium level sits nearer
# 200-300 mb. Measured cost of going from 1000-400 to everything: RRFS
# 125 -> 225 messages, 203 -> 320 MB; HRRR 125 -> 195, 88 -> 131 MB.
ISOBARIC_SEARCH = r':(?:HGT|TMP|DPT|UGRD|VGRD):\d+ mb:'
# Lower bound for the sanity check on how many messages the index
# returned -- the 1000-400 mb band alone is 125, so anything near that
# means the index is intact; the exact count now varies by model.
MIN_ISOBARIC_MESSAGES = 110
HRRR_FETCH_WORKERS = 16


def _lcc_cone_constant(latin1, latin2):
    """Lambert Conformal Conic cone constant from the grid's standard
    parallels -- HRRR uses a single tangent parallel (Latin1==Latin2), so
    this falls back to sin(Latin1) rather than the general two-parallel
    formula's 0/0."""
    if abs(latin1 - latin2) < 1e-6:
        return math.sin(math.radians(latin1))
    return (math.log(math.cos(math.radians(latin1)) / math.cos(math.radians(latin2)))
           / math.log(math.tan(math.radians(45 - latin1 / 2))
                      / math.tan(math.radians(45 - latin2 / 2))))


def _decode_grib_fields(grib_path, lat, lon):
    """Decode a cached GRIB2 subset (from _fetch_grib_profile) into the
    per-level {shortName: value} dict the caller needs, at the grid point
    nearest (lat, lon).

    eccodes' own codes_grib_find_nearest decodes a message's *entire*
    field internally just to answer one point -- measured at ~60s of CPU
    across a single RRFS run's ~125 messages on its ~1.9M-point North
    America grid, and that cost repeats on every call even though the
    field data is identical between calls for the same run (only the
    query point differs). Decoding straight to full numpy arrays once
    and caching them (compressed, alongside the raw GRIB2 subset)
    instead means a second point looked up against an already-decoded
    run costs a KDTree build (under a second, from the cached lat/lon
    grid) and a query (sub-millisecond) rather than that same ~60s of
    eccodes decode all over again -- and it's a bit faster even on the
    very first point, since a plain values decode is itself faster than
    find_nearest's own per-message overhead.

    The grid's lat/lon and the LCC cone constant/central meridian only
    need decoding once (identical for every message sharing a grid
    definition, which every message in one of these subsets does) --
    not once per level/variable."""
    lats, lons, cone, lov, values, tree = _load_fields(grib_path)
    # lons is in eccodes' 0-360 convention (see the longitude-normalizing
    # comment below) -- query with the same convention, or a -180..180
    # input like -117 reads as numerically far from every real point in
    # the grid's 225-299 range and returns nonsense (caught by exactly
    # this: an early version of this query, without the % 360, matched
    # coastal British Columbia for a San Diego-area query point).
    _, idx = tree.query([lat, lon % 360])
    nearest_lat, nearest_lon = float(lats[idx]), float(lons[idx])

    levels = {}
    for key, arr in values.items():
        short_name, level_str = key.rsplit('_', 1)
        level = int(level_str)
        entry = levels.setdefault(level, {})
        entry[short_name] = float(arr[idx])
        if short_name in ('u', 'v', '10u', '10v'):
            entry['_lat'], entry['_lon'] = nearest_lat, nearest_lon
    return levels, cone, lov


@lru_cache(maxsize=3)
def _load_fields(grib_path):
    """One cached GRIB2 subset's full field arrays, its grid, and a
    KD-tree over that grid -- everything about a run that doesn't depend
    on which point is being looked up.

    Memoized because none of it does depend on the point: decompressing
    the arrays and building the tree measured ~3.6s for one RRFS run, and
    a batch of sites against that same run was paying it once per site
    for an answer that never changed. The per-point work left downstream
    is a single tree query, ~0.2 ms.

    maxsize is small deliberately -- these are big (a 25-level, 5-variable
    RRFS run is ~950 MB of float32 once decompressed) -- but not 1, since
    a single profile touches three files (isobaric, surface and, where
    published, native), and a cache of 1 would evict between them and
    memoize nothing. A long-lived process that isn't batching should call
    clear_field_cache() when it's done rather than hold that."""
    fields_path = grib_path.with_suffix('.fields.npz')
    if fields_path.exists():
        cached = np.load(fields_path)
        lats, lons = cached['latitudes'], cached['longitudes']
        cone, lov = float(cached['cone']), float(cached['lov'])
        values = {k: cached[k] for k in cached.files if k not in ('latitudes', 'longitudes', 'cone', 'lov')}
    else:
        lats = lons = None
        cone = lov = None
        values = {}
        with open(grib_path, 'rb') as f:
            while True:
                gid = eccodes.codes_grib_new_from_file(f)
                if gid is None:
                    break
                short_name = eccodes.codes_get(gid, 'shortName')
                level = eccodes.codes_get(gid, 'level')
                if lats is None:
                    lats = np.asarray(eccodes.codes_get_array(gid, 'latitudes'), dtype=np.float32)
                    lons = np.asarray(eccodes.codes_get_array(gid, 'longitudes'), dtype=np.float32)
                values[f'{short_name}_{level}'] = np.asarray(
                    eccodes.codes_get_array(gid, 'values'), dtype=np.float32)
                # eccodes names the wind components by level type: plain
                # u/v on isobaric and hybrid levels, but 10u/10v for the
                # fixed 10 m diagnostic winds (URMA and the models' own
                # surface products). Both are the same grid-relative
                # components, and either one carries the projection
                # metadata this needs.
                if short_name in ('u', 'v', '10u', '10v') and cone is None:
                    cone = _lcc_cone_constant(eccodes.codes_get(gid, 'Latin1InDegrees'),
                                              eccodes.codes_get(gid, 'Latin2InDegrees'))
                    lov = eccodes.codes_get(gid, 'LoVInDegrees')
                eccodes.codes_release(gid)

        # .part + rename so a run interrupted mid-write can't leave a
        # truncated cache file that a later call mistakes for complete.
        # np.savez_compressed auto-appends ".npz" to a string/Path target
        # (which would turn ...npz.part into ...npz.part.npz and break
        # the rename below) but not to an already-open file object.
        tmp_path = fields_path.with_suffix('.npz.part')
        with open(tmp_path, 'wb') as tmp:
            np.savez_compressed(tmp, latitudes=lats, longitudes=lons,
                                cone=np.float32(cone), lov=np.float32(lov), **values)
        tmp_path.rename(fields_path)

    tree = cKDTree(np.column_stack([lats, lons]))
    return lats, lons, cone, lov, values, tree


def clear_field_cache():
    """Drop the memoized field arrays and KD-trees (see _load_fields).

    Worth calling from a long-running process that renders one profile at
    a time -- the Telegram bot -- where the memo buys nothing across
    requests but would otherwise pin ~1 GB for the life of the service."""
    _load_fields.cache_clear()


def _latest_available_run(model, max_tries=6):
    """The most recently posted run for an hourly model, found by
    starting from the current hour and stepping back until Herbie finds
    one -- posting typically lags real time by an hour or two."""
    run_date = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for _ in range(max_tries):
        h = Herbie(run_date.replace(tzinfo=None), model=model, product='prs', fxx=0, verbose=False)
        if h.grib is not None:
            return run_date
        run_date -= timedelta(hours=1)
    raise RuntimeError(f'No recent {model.upper()} run found')


# Whether a (model, run, lead) has been posted, memoized for
# newer_run_posted(): a posted file stays posted, so hits are kept for
# good, while a miss is re-checked after RUN_MISS_TTL -- it may just not
# have been posted yet.
RUN_MISS_TTL = timedelta(minutes=5)
_run_posted_memo = {}

# Beyond this many hourly candidates, newer_run_posted() stops looking and
# answers True: a render that old is cheaper to redo than to keep proving
# current at ~0.6 s a lookup.
MAX_NEWER_RUN_CANDIDATES = 12


def _run_posted(model, run_date, forecast_hour):
    key = (model, run_date, forecast_hour)
    posted, checked_at = _run_posted_memo.get(key, (False, None))
    now = datetime.now(timezone.utc)
    if posted or (checked_at is not None and now - checked_at < RUN_MISS_TTL):
        return posted
    # priority='aws' keeps a miss to one lookup (~0.6 s) instead of walking
    # every mirror Herbie knows about. A failed lookup counts as a miss:
    # the caller then keeps what it has rather than failing outright.
    try:
        h = Herbie(run_date.replace(tzinfo=None), model=model, product='prs',
                   fxx=forecast_hour, priority='aws', verbose=False)
        posted = h.grib is not None
    except Exception:
        posted = False
    if len(_run_posted_memo) > 10_000:
        _run_posted_memo.clear()
    _run_posted_memo[key] = (posted, now)
    return posted


def newer_run_posted(model, run_date, valid_time=None, now=None):
    """Whether `model` has posted a run newer than run_date that a fresh
    _fetch_grib_profile() call would use instead -- i.e. whether a render
    made from run_date has been superseded.

    valid_time is the valid time that was asked for; None means "the
    latest run's analysis" (_fetch_grib_profile's date=None case), where
    any newer run's own f00 supersedes it. Otherwise the candidates are the
    runs after run_date up to the valid time itself (a past valid time is
    always answered from the run at/before it) or up to now (a future one
    from the freshest run that reaches it), each at the lead that lands on
    valid_time -- the same (run, lead) pairs the fetch's step-back walks.

    Checked newest-first against the bucket rather than a hardcoded
    schedule: RRFS currently posts 3-hourly (f84 on 00/06/12/18Z, f18
    otherwise) and HRRR hourly (f48/f18), and either may change."""
    now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    candidate = now if valid_time is None else min(now, valid_time)
    for _ in range(MAX_NEWER_RUN_CANDIDATES):
        if candidate <= run_date:
            return False
        lead = 0 if valid_time is None else round((valid_time - candidate).total_seconds() / 3600)
        if _run_posted(model, candidate, lead):
            return True
        candidate -= timedelta(hours=1)
    return candidate > run_date


# Product name for each model's surface/2D diagnostic fields (HGT:surface
# -- terrain elevation -- among them). Different naming per model in
# NOAA's own file layout: HRRR calls it "sfc", RRFS "2d".
SURFACE_PRODUCT = {'hrrr': 'sfc', 'rrfs': '2d'}

# Product name for each model's native (hybrid/sigma) vertical levels,
# where available -- HRRR publishes these ("nat"); RRFS's own product
# name is "natlev" but as of this writing 404s (its native-level product
# doesn't appear to be populated yet, consistent with RRFS still being
# early-operational) -- not hardcoded as HRRR-only, though, so RRFS picks
# this up automatically the moment it does start publishing (see
# _fetch_native_levels, which already treats "not found" as "skip these,
# isobaric-only is all there is").
NATIVE_LEVEL_PRODUCT = {'hrrr': 'nat', 'rrfs': 'natlev'}
# How many native levels to pull. All 50 of them: the lowest 15 reach
# only ~3.5 km/667 mb, which covers the boundary layer but stops well
# below the high cloud these are now also being used to see. Costs 90 ->
# 300 messages, 139 -> 393 MB per run for HRRR.
MAX_NATIVE_LEVEL = 50

# (model, run, lead) triples already found to publish no native levels,
# so the same negative network probe isn't repeated for every point looked
# up against that run. Process-lifetime only -- a fresh run starts over.
_NATIVE_UNAVAILABLE = set()


def _fetch_terrain_height(model, lat, lon, max_tries=6):
    """Terrain elevation (m) at the grid point nearest (lat, lon), from
    the model's own orography (HGT:surface). Terrain is static for a
    given model/grid -- it doesn't change run to run or with forecast
    lead -- so this is cached once per model rather than once per run
    the way the isobaric fields are."""
    cache_path = CACHE_DIR / f'{model.upper()}_terrain.npz'
    if cache_path.exists():
        cached = np.load(cache_path)
        lats, lons, values = cached['latitudes'], cached['longitudes'], cached['values']
    else:
        run_date = _latest_available_run(model, max_tries=max_tries)
        h = Herbie(run_date.replace(tzinfo=None), model=model,
                  product=SURFACE_PRODUCT[model], fxx=0, verbose=False)
        df = h.inventory(search=r':HGT:surface:')
        row = df.iloc[0]
        start = int(row.start_byte)
        end = None if pd.isna(row.end_byte) else int(row.end_byte)
        range_hdr = f'bytes={start}-{end}' if end is not None else f'bytes={start}-'
        r = requests.get(h.grib, headers={'Range': range_hdr}, timeout=30)
        r.raise_for_status()

        fd, tmp_path = tempfile.mkstemp(suffix='.grib2')
        try:
            with open(fd, 'wb') as f:
                f.write(r.content)
            with open(tmp_path, 'rb') as f:
                gid = eccodes.codes_grib_new_from_file(f)
                lats = np.asarray(eccodes.codes_get_array(gid, 'latitudes'), dtype=np.float32)
                lons = np.asarray(eccodes.codes_get_array(gid, 'longitudes'), dtype=np.float32)
                values = np.asarray(eccodes.codes_get_array(gid, 'values'), dtype=np.float32)
                eccodes.codes_release(gid)
        finally:
            os.unlink(tmp_path)

        # .part + rename so a run interrupted mid-write can't leave a
        # truncated cache file a later call mistakes for complete (see
        # the matching pattern in _decode_grib_fields).
        tmp_cache = cache_path.with_suffix('.npz.part')
        with open(tmp_cache, 'wb') as f:
            np.savez_compressed(f, latitudes=lats, longitudes=lons, values=values)
        tmp_cache.rename(cache_path)

    tree = cKDTree(np.column_stack([lats, lons]))
    _, idx = tree.query([lat, lon % 360])
    return float(values[idx])


def _fetch_surface_row(model, cache_prefix, run_date, forecast_hour, lat, lon,
                       terrain_height, session):
    """The model's own surface diagnostics as one profile row (2 m
    temperature/dewpoint, 10 m wind, surface pressure), or None if this
    model/run doesn't publish them.

    Without this the lowest row of a modeled profile is whichever
    *isobaric* level survives the terrain trim, and a surface parcel gets
    launched from there. That is fine where native levels reach nearly to
    the ground (HRRR's level 1 measured ~11 m AGL) but badly wrong
    otherwise: RRFS publishes no native levels, so its lowest retained
    level measured 150-230 m AGL across these sites, and its 2 m
    temperature ran 3.6-4.6 degC warmer than the air up there. Launching
    a "surface" parcel from 200 m up discards exactly the superadiabatic
    layer that drives a thermal, and it starts the parcel neutral with
    its environment by construction -- both of which make the model look
    like it under-forecasts surface heating when it doesn't.

    So this is fetched for every model rather than only the ones missing
    native levels: it is what "surface" should have meant all along, and
    it removes the model-to-model asymmetry in what the parcel starts
    from. Same product the terrain height comes from, but keyed per
    run/lead rather than cached once, since these fields do vary."""
    product = SURFACE_PRODUCT.get(model)
    if product is None:
        return None

    cache_path = CACHE_DIR / f'{cache_prefix}_{run_date:%Y%m%d_%HZ}_f{forecast_hour:02d}.surface.grib2'
    if not cache_path.exists():
        h = Herbie(run_date.replace(tzinfo=None), model=model, product=product,
                   fxx=forecast_hour, verbose=False)
        if h.grib is None:
            return None

        search = (r':(?:TMP|DPT):2 m above ground:'
                  r'|:(?:UGRD|VGRD):10 m above ground:'
                  r'|:PRES:surface:')
        inventory = h.inventory(search=search)
        if len(inventory) < 5:
            return None

        def _fetch_one(row):
            start = int(row.start_byte)
            end = None if pd.isna(row.end_byte) else int(row.end_byte)
            range_hdr = f'bytes={start}-{end}' if end is not None else f'bytes={start}-'
            r = session.get(h.grib, headers={'Range': range_hdr}, timeout=60)
            r.raise_for_status()
            return r.content

        tmp_path = cache_path.with_suffix('.grib2.part')
        with ThreadPoolExecutor(max_workers=HRRR_FETCH_WORKERS) as ex:
            with open(tmp_path, 'wb') as tmp:
                for content in ex.map(_fetch_one, (row for _, row in inventory.iterrows())):
                    tmp.write(content)
        tmp_path.rename(cache_path)

    levels, cone, lov = _decode_grib_fields(cache_path, lat, lon)
    # eccodes names the height-tagged diagnostics by their level rather
    # than plainly: 2t/2d at 2 m, 10u/10v at 10 m, sp for surface
    # pressure -- not t/dpt/u/v/pres. Verified against both models.
    surface, two_m, ten_m = levels.get(0, {}), levels.get(2, {}), levels.get(10, {})
    if not ('sp' in surface and {'2t', '2d'} <= two_m.keys() and {'10u', '10v'} <= ten_m.keys()):
        return None

    diff = ((ten_m['_lon'] - lov + 180) % 360) - 180
    angle = math.radians(cone * diff)
    u_earth = ten_m['10v'] * math.sin(angle) + ten_m['10u'] * math.cos(angle)
    v_earth = ten_m['10v'] * math.cos(angle) - ten_m['10u'] * math.sin(angle)

    return {
        'pressure': surface['sp'] / 100.,  # Pa -> hPa, station (not sea-level) pressure
        # The temperature/dewpoint here are valid at 2 m, so that is where
        # the row sits; the 10 m wind is reported at the same row rather
        # than given one of its own, the way a radiosonde's surface entry
        # carries the whole surface observation at one height.
        'height': terrain_height + 2.,
        'temperature': two_m['2t'] - 273.15,
        'dewpoint': two_m['2d'] - 273.15,
        'speed': math.hypot(u_earth, v_earth) * 1.9438445,  # m/s -> kt
        'direction': math.degrees(math.atan2(-u_earth, -v_earth)) % 360,
        'latitude': ten_m['_lat'],
        'longitude': ((ten_m['_lon'] + 180) % 360) - 180,
    }


def _fetch_native_levels(model, cache_prefix, run_date, forecast_hour, lat, lon, session):
    """Extra near-surface rows (same shape as _fetch_grib_profile's own
    row dicts) from the model's native/hybrid levels 1..MAX_NATIVE_LEVEL,
    or an empty list if this model/run doesn't publish them (e.g. RRFS,
    as of this writing) -- callers merge these in alongside the regular
    isobaric levels rather than depending on them.

    Native levels don't sit at round pressures the way isobaric levels
    do, but each one reports its own actual pressure directly (a PRES
    field per level) -- no hybrid sigma-pressure coefficient math
    needed, just read it like any other per-level variable. Humidity
    here is specific humidity (SPFH, kg/kg), not dewpoint directly, so
    it needs converting via mpcalc.dewpoint_from_specific_humidity.

    Uses the given run_date/forecast_hour as-is (no retry/resolution of
    its own) -- these levels only make sense paired with the exact same
    run already resolved for the isobaric fetch, not independently
    re-resolved."""
    product = NATIVE_LEVEL_PRODUCT.get(model)
    if product is None:
        return []

    # A model that doesn't publish these (RRFS today) otherwise costs a
    # network probe on every single call -- ~2.2s each, and for a batch of
    # sites on one run that is the same negative answer over and over.
    # Remembered per (model, run, lead) rather than per model, so it stays
    # correct the day RRFS does start publishing: a later run is probed
    # again rather than being written off.
    unavailable_key = (model, run_date, forecast_hour)
    if unavailable_key in _NATIVE_UNAVAILABLE:
        return []

    # Cache is keyed the same way the isobaric fetch's is (model/run/lead,
    # not lat/lon) -- checked first so a run already on disk skips the
    # Herbie network probe entirely, same as the isobaric path below.
    cache_path = CACHE_DIR / f'{cache_prefix}_{run_date:%Y%m%d_%HZ}_f{forecast_hour:02d}.native.grib2'
    if not cache_path.exists():
        h = Herbie(run_date.replace(tzinfo=None), model=model, product=product,
                  fxx=forecast_hour, verbose=False)
        if h.grib is None:
            _NATIVE_UNAVAILABLE.add(unavailable_key)
            return []

        wanted_levels = '|'.join(str(lvl) for lvl in range(1, MAX_NATIVE_LEVEL + 1))
        search = rf':(?:PRES|HGT|TMP|SPFH|UGRD|VGRD):(?:{wanted_levels}) hybrid level:'
        inventory = h.inventory(search=search)
        if len(inventory) < MAX_NATIVE_LEVEL * 6 * 0.9:
            # missing more than expected -- treat as unavailable rather than guess
            _NATIVE_UNAVAILABLE.add(unavailable_key)
            return []

        def _fetch_one(row):
            start = int(row.start_byte)
            end = None if pd.isna(row.end_byte) else int(row.end_byte)
            range_hdr = f'bytes={start}-{end}' if end is not None else f'bytes={start}-'
            r = session.get(h.grib, headers={'Range': range_hdr}, timeout=30)
            r.raise_for_status()
            return r.content

        tmp_path = cache_path.with_suffix('.grib2.part')
        with ThreadPoolExecutor(max_workers=HRRR_FETCH_WORKERS) as ex:
            with open(tmp_path, 'wb') as tmp:
                for content in ex.map(_fetch_one, (row for _, row in inventory.iterrows())):
                    tmp.write(content)
        tmp_path.rename(cache_path)

    levels, cone, lov = _decode_grib_fields(cache_path, lat, lon)

    rows = []
    for lvl in levels:
        d = levels[lvl]
        if not all(k in d for k in ('pres', 't', 'q', 'gh', 'u', 'v')):
            continue
        pressure_hpa = d['pres'] / 100.  # Pa -> hPa
        dewpoint_c = mpcalc.dewpoint_from_specific_humidity(
            units.Quantity(pressure_hpa, 'hPa'), units.Quantity(d['q'], 'kg/kg')).m
        diff = ((d['_lon'] - lov + 180) % 360) - 180
        angle = math.radians(cone * diff)
        u_earth = d['v'] * math.sin(angle) + d['u'] * math.cos(angle)
        v_earth = d['v'] * math.cos(angle) - d['u'] * math.sin(angle)
        rows.append({
            'pressure': pressure_hpa,
            'height': d['gh'],
            'temperature': d['t'] - 273.15,
            'dewpoint': dewpoint_c,
            'speed': math.hypot(u_earth, v_earth) * 1.9438445,
            'direction': math.degrees(math.atan2(-u_earth, -v_earth)) % 360,
            'latitude': d['_lat'],
            'longitude': ((d['_lon'] + 180) % 360) - 180,
        })
    return rows


def _fetch_grib_profile(lat, lon, date, model, cache_prefix, forecast_hour=None,
                        run_datetime=None, max_tries=6):
    """Modeled vertical profile at the nearest grid point of a CONUS,
    Lambert-Conformal, hourly GRIB2 model (HRRR or RRFS), pulled from
    NOAA's public AWS archive.

    Herbie (the herbie-data package) resolves which model/source/run
    actually exists and parses the run's .idx sidecar into byte ranges
    for the requested variables/levels -- it knows the current URL
    templates and source fallbacks (AWS/NOMADS/etc.) for a large model
    registry, including RRFS's now-operational feed, so this doesn't
    hardcode any of that. But Herbie's own .download() fetches matched
    byte ranges one at a time; for a request this wide (~125 messages,
    before trimming to fewer levels this was ~195) that took over 10
    minutes in testing. So Herbie is used only to resolve the run and
    its byte ranges (.inventory()) -- the actual fetch below is a
    parallel ThreadPoolExecutor pull instead.

    Each message is decoded with eccodes and reduced to its single
    nearest grid point immediately (eccodes' own nearest-neighbor
    search), so the full 2D field is never materialized.

    These models' u/v wind components are *grid-relative* (Lambert
    Conformal), not earth-relative -- plotted or converted to direction
    without correction, they'd point the wrong way except exactly on the
    grid's central meridian. They're rotated to true north here using
    the standard LCC formula, with the cone constant and central
    meridian read from the grid's own GRIB2 metadata rather than
    hardcoded, so this isn't tied to any one model's specific projection
    parameters.

    Four ways to pick a run + forecast lead, checked in this order:

    1. run_datetime given: pins the exact run init time (no fallback --
       raises if that hour wasn't actually posted, rather than silently
       stepping to a different run than the one asked for). forecast_hour
       defaults to 0 (that run's own analysis) unless also given.
    2. forecast_hour given (without run_datetime): the latest available
       run, at that explicit lead -- e.g. "the latest run's 6-hour
       forecast," without caring what valid time that lands on.
    3. date=None (neither of the above given either): the latest run
       available, as-is (forecast_hour 0) -- the default whenever the
       caller hasn't asked for a specific time.
    4. date given: treated as the desired *valid* time, not necessarily a
       run's own init time. In the future (e.g. "local noon" requested
       before noon has happened) no run is initialized then, so the
       freshest posted run is used at whatever lead reaches that time; at
       or in the past, the run at that hour is used as its own analysis
       (lead 0).

       Either way the requested valid time is what's held fixed. Where no
       run supports it -- the hour has no run of its own (these models
       post 3-hourly off their synoptic cycles), or the freshest run
       can't reach far enough -- the run steps back an hour at a time and
       the lead *grows* to match, so the answer stays at the time asked
       for and only gets older. Cases 1-2 pin the run or lead explicitly
       and so never step: there, stepping would silently change what was
       asked for.

    The assembled GRIB2 subset (all requested messages for one run, on
    the order of 80 MB) is cached under CACHE_DIR the same way Wyoming
    soundings are -- it's the same data regardless of (lat, lon), so a
    second point looked up for a run already on disk decodes straight
    from the cache file with no network access at all, and re-plotting
    the same point/run doesn't re-pull it either."""
    # Case 1 pins the run outright, so it needs no idea what the latest
    # run is -- and finding that out costs a series of network lookups
    # (~5.5s measured), stepping back an hour at a time. Deferred into the
    # branches that actually use it, which matters most for a batch: every
    # site in a pinned run was paying for a lookup whose answer was thrown
    # away.
    # Set only when a future valid time was asked for, which is the one
    # case where an unavailable (run, lead) can be recovered by trying an
    # older run at a longer lead -- see below.
    hold_valid_time = None
    if run_datetime is not None:
        run_date = run_datetime.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        forecast_hour = forecast_hour if forecast_hour is not None else 0
        allow_retry = False
    elif forecast_hour is not None:
        run_date = _latest_available_run(model, max_tries=max_tries)
        allow_retry = False
    elif date is None:
        run_date = _latest_available_run(model, max_tries=max_tries)
        forecast_hour = 0
        allow_retry = False
    else:
        latest_run = _latest_available_run(model, max_tries=max_tries)
        valid_time = date.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if valid_time > latest_run:
            run_date = latest_run
            forecast_hour = round((valid_time - run_date).total_seconds() / 3600)
            allow_retry = False
            # The freshest run often can't reach far enough: these models
            # only run to a long lead on their synoptic cycles (RRFS goes
            # to f18 off-cycle but f84 from 00/06/12/18Z; verified against
            # the bucket). Rather than give up, fall back to older runs
            # that do cover this valid time -- holding the valid time
            # fixed and *growing* the lead as the run steps back, so
            # 21Z+f22 becomes 20Z+f23, 19Z+f24, 18Z+f25, which exists.
            # (The plain retry below steps the run back at a fixed lead,
            # which would silently move the valid time instead.)
            hold_valid_time = valid_time
        else:
            run_date = valid_time
            forecast_hour = 0
            allow_retry = True
            # Same treatment for a past valid time, and for the same
            # reason. These models don't run every hour -- RRFS posts
            # 3-hourly off its synoptic cycles (09/12/15/18Z present,
            # 10/11/13/14/16/17Z absent; verified against the bucket) --
            # so an analysis often doesn't exist at the hour asked for.
            # Stepping the run back at a fixed lead 0 silently answers a
            # different time than the one requested: 17Z walked back to
            # the 15Z analysis, two hours early, when 15Z f02 lands on
            # 17Z exactly. Holding the valid time and growing the lead
            # gets the hour that was actually asked for.
            hold_valid_time = valid_time

    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(pool_connections=HRRR_FETCH_WORKERS,
                          pool_maxsize=HRRR_FETCH_WORKERS, max_retries=retry)
    session.mount('https://', adapter)

    search = ISOBARIC_SEARCH

    grib_path = None
    for _ in range(max_tries if (allow_retry or hold_valid_time is not None) else 1):
        candidate_path = CACHE_DIR / f'{cache_prefix}_{run_date:%Y%m%d_%HZ}_f{forecast_hour:02d}.grib2'
        if candidate_path.exists():
            grib_path = candidate_path
            break

        h = Herbie(run_date.replace(tzinfo=None), model=model, product='prs',
                  fxx=forecast_hour, verbose=False)
        if h.grib is None:
            if hold_valid_time is not None:
                # Step back to an older run and lengthen the lead by the
                # same hour, so the valid time being asked for doesn't move.
                run_date -= timedelta(hours=1)
                forecast_hour = round((hold_valid_time - run_date).total_seconds() / 3600)
                continue
            if not allow_retry:
                raise RuntimeError(f'{model.upper()} run {run_date:%Y-%m-%d %HZ} has no '
                                   f'f{forecast_hour:02d} forecast')
            run_date -= timedelta(hours=1)
            continue
        url = h.grib

        inventory = h.inventory(search=search)
        if len(inventory) < MIN_ISOBARIC_MESSAGES:
            raise RuntimeError(f'{model.upper()} index missing expected messages for {run_date} '
                               f'({len(inventory)} < {MIN_ISOBARIC_MESSAGES})')

        def _fetch_one(row):
            start = int(row.start_byte)
            end = None if pd.isna(row.end_byte) else int(row.end_byte)
            range_hdr = f'bytes={start}-{end}' if end is not None else f'bytes={start}-'
            r = session.get(url, headers={'Range': range_hdr}, timeout=30)
            r.raise_for_status()
            return r.content

        # Written to a .part path and renamed only once complete, so a
        # run interrupted mid-fetch can't leave a truncated file that a
        # later call mistakes for a valid, complete cache entry.
        tmp_path = candidate_path.with_suffix('.grib2.part')
        with ThreadPoolExecutor(max_workers=HRRR_FETCH_WORKERS) as ex:
            with open(tmp_path, 'wb') as tmp:
                for content in ex.map(_fetch_one, (row for _, row in inventory.iterrows())):
                    tmp.write(content)
        tmp_path.rename(candidate_path)
        grib_path = candidate_path
        break
    else:
        if hold_valid_time is not None:
            raise RuntimeError(
                f'No {model.upper()} run reaches {hold_valid_time:%Y-%m-%d %HZ} -- tried back to '
                f'{run_date:%Y-%m-%d %HZ} at f{forecast_hour:02d}')
        raise RuntimeError(f'No {model.upper()} run found near {date}')

    levels, cone, lov = _decode_grib_fields(grib_path, lat, lon)

    rows = []
    for lvl in sorted(levels, reverse=True):
        d = levels[lvl]
        if not all(k in d for k in ('t', 'dpt', 'gh', 'u', 'v')):
            continue  # a level missing a variable (shouldn't happen) -- skip rather than fake it
        diff = ((d['_lon'] - lov + 180) % 360) - 180
        angle = math.radians(cone * diff)
        u_earth = d['v'] * math.sin(angle) + d['u'] * math.cos(angle)
        v_earth = d['v'] * math.cos(angle) - d['u'] * math.sin(angle)
        rows.append({
            'pressure': lvl,
            'height': d['gh'],
            'temperature': d['t'] - 273.15,
            'dewpoint': d['dpt'] - 273.15,
            'speed': math.hypot(u_earth, v_earth) * 1.9438445,  # m/s -> kt
            'direction': math.degrees(math.atan2(-u_earth, -v_earth)) % 360,
            'latitude': d['_lat'],
            # eccodes reports HRRR/RRFS grid-point longitudes in 0-360
            # convention -- normalize to -180..180 to match what
            # Open-Meteo/geocoding/everything else downstream expects.
            'longitude': ((d['_lon'] + 180) % 360) - 180,
        })

    # Native/hybrid levels give much finer near-surface resolution than
    # the 25 mb isobaric spacing above (e.g. HRRR's lowest ~15 native
    # levels span roughly the same range as the isobaric grid's bottom
    # 2-3 levels). Merged in here when the model actually publishes them
    # -- RRFS's equivalent product doesn't exist yet, so this is
    # currently a no-op there (see _fetch_native_levels) and will pick
    # it up automatically once it is.
    rows += _fetch_native_levels(model, cache_prefix, run_date, forecast_hour, lat, lon, session)
    rows.sort(key=lambda row: row['pressure'], reverse=True)

    # Isobaric (fixed-pressure) fields are computed everywhere on the
    # grid regardless of terrain -- where a pressure surface would fall
    # below actual ground, NOAA's post-processing extrapolates a value
    # with a standard lapse rate rather than reporting no data. Verified
    # directly against this field: Palomar (~673 m terrain) had its
    # 1000/975/950 mb isobaric heights (126/348/575 m) all below its own
    # ground. Dropping levels below the grid's own terrain height here
    # (native levels included) keeps the plotted profile starting at
    # real atmosphere, the same way a radiosonde's own lowest reported
    # level is real ground.
    terrain_height = _fetch_terrain_height(model, lat, lon)
    rows = [row for row in rows if row['height'] >= terrain_height]

    # The model's own surface diagnostics, as the profile's ground row --
    # see _fetch_surface_row for why a parcel launched from the lowest
    # *isobaric* level isn't a surface parcel at all. Everything at or
    # above its pressure is dropped so the profile keeps a single,
    # unambiguous bottom: a retained isobaric level sitting essentially
    # at ground would otherwise duplicate this row's pressure, and
    # several things downstream (the MSL height mapping, the lapse-rate
    # window) need pressure to be a strictly decreasing coordinate.
    surface_row = _fetch_surface_row(model, cache_prefix, run_date, forecast_hour,
                                     lat, lon, terrain_height, session)
    if surface_row is not None:
        rows = [row for row in rows if row['pressure'] < surface_row['pressure']]
        rows.insert(0, surface_row)

    if not rows:
        raise RuntimeError(f'No usable {model.upper()} levels decoded for {run_date}')
    # valid_time is "the sounding's time" for titles/labels/filenames;
    # run_date is when the underlying model was actually run, which
    # differs from it whenever forecast_hour > 0 -- both are returned so
    # the caller can show which run a forecast came from.
    return pd.DataFrame(rows), run_date + timedelta(hours=forecast_hour), run_date


def fetch_hrrr_profile(lat, lon, date, forecast_hour=None, run_datetime=None, max_tries=6):
    """Modeled vertical profile at the nearest HRRR (3km CONUS) grid
    point. See _fetch_grib_profile for how the fetch actually works."""
    return _fetch_grib_profile(lat, lon, date, model='hrrr', cache_prefix='HRRR',
                               forecast_hour=forecast_hour, run_datetime=run_datetime,
                               max_tries=max_tries)


def fetch_rrfs_profile(lat, lon, date, forecast_hour=None, run_datetime=None, max_tries=6):
    """Modeled vertical profile at the nearest RRFS (3km CONUS) grid
    point -- NOAA's next-generation HRRR/RAP successor, now operational
    on noaa-rrfs-ops-pds (found via Herbie's model registry; an earlier,
    now-superseded RRFS bucket, noaa-rrfs-pds, holds only retrospective
    test-case archives and isn't used here). See _fetch_grib_profile for
    how the fetch actually works."""
    return _fetch_grib_profile(lat, lon, date, model='rrfs', cache_prefix='RRFS',
                               forecast_hour=forecast_hour, run_datetime=run_datetime,
                               max_tries=max_tries)


def fetch_urma_surface(lat, lon, date=None, max_tries=12):
    """Analyzed *surface* conditions at the 2.5 km grid point nearest
    (lat, lon), from URMA (Un-Restricted Mesoscale Analysis).

    Not a profile source, and deliberately absent from MODEL_FETCHERS /
    MODEL_PRIORITY below: URMA is a two-dimensional analysis (its files
    are named "2dvaranl" -- 2-D variational analysis) with no isobaric
    and no native levels. Its entire CONUS file is 14 messages, all of
    them surface, 2 m or 10 m, so there is nothing to build a sounding
    out of. Verified directly against the run's .idx sidecar.

    What it is good for, and why it's here: unlike HRRR/RRFS/GFS, which
    are forecasts, URMA assimilates surface observations -- so its 2 m
    temperature and dewpoint are an analysis of what actually happened,
    at 2.5 km rather than 3 km. Those are exactly the two values a
    surface parcel is launched from, which makes this a way to anchor or
    verify a modeled sounding's surface parcel against observations
    instead of trusting the model's own lowest level.

    Two limits that rule it out as a forecast source. It is analysis-only
    (the template's "ges" product is the first-guess field, not a
    forecast), so it can never cover a future valid time -- only now or
    the past. And it posts with a deliberate multi-hour lag, which is the
    point of URMA over RTMA: it waits for late-arriving observations.
    date=None therefore means "the most recent analysis actually posted",
    found by stepping back an hour at a time from now.

    Returns a dict of the analysis at that point:
        run_time, latitude, longitude (the grid point's own, not the
        query's), terrain_height (m), pressure (hPa, station pressure --
        NOT reduced to sea level), temperature/dewpoint (degC, 2 m),
        speed (kt) and direction (deg, 10 m).
    """
    run_date = (date or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0)

    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(pool_connections=HRRR_FETCH_WORKERS,
                          pool_maxsize=HRRR_FETCH_WORKERS, max_retries=retry)
    session.mount('https://', adapter)

    # UGRD/VGRD rather than the WIND/WDIR the same file also carries,
    # even though those are earth-relative and would need no rotation:
    # _decode_grib_fields reads the LCC cone constant/central meridian
    # (and tags the matched grid point) off the u/v messages specifically,
    # so a subset without them decodes to cone=lov=None and fails on save.
    search = (r':(?:HGT|PRES):surface:|:(?:TMP|DPT):2 m above ground:'
              r'|:(?:UGRD|VGRD):10 m above ground:')

    grib_path = None
    for _ in range(max_tries):
        candidate_path = CACHE_DIR / f'URMA_{run_date:%Y%m%d_%HZ}.grib2'
        if candidate_path.exists():
            grib_path = candidate_path
            break

        h = Herbie(run_date.replace(tzinfo=None), model='urma', product='anl',
                   fxx=0, verbose=False)
        if h.grib is None:
            run_date -= timedelta(hours=1)
            continue

        inventory = h.inventory(search=search)
        if len(inventory) < 6:
            run_date -= timedelta(hours=1)
            continue

        def _fetch_one(row):
            start = int(row.start_byte)
            end = None if pd.isna(row.end_byte) else int(row.end_byte)
            range_hdr = f'bytes={start}-{end}' if end is not None else f'bytes={start}-'
            r = session.get(h.grib, headers={'Range': range_hdr}, timeout=60)
            r.raise_for_status()
            return r.content

        tmp_path = candidate_path.with_suffix('.grib2.part')
        with ThreadPoolExecutor(max_workers=HRRR_FETCH_WORKERS) as ex:
            with open(tmp_path, 'wb') as tmp:
                for content in ex.map(_fetch_one, (row for _, row in inventory.iterrows())):
                    tmp.write(content)
        tmp_path.rename(candidate_path)
        grib_path = candidate_path
        break
    else:
        raise RuntimeError(f'No URMA analysis found near {run_date:%Y-%m-%d %HZ}')

    levels, cone, lov = _decode_grib_fields(grib_path, lat, lon)

    # Keyed by level number, so: 0 = surface (orog/sp), 2 = the 2 m
    # fields, 10 = the 10 m winds. These shortNames are not the ones the
    # isobaric/native code uses -- eccodes names the height-tagged
    # diagnostics 2t/2d/10u/10v and surface orography/pressure orog/sp,
    # rather than t/dpt/u/v/gh. Checked against the actual messages.
    surface, two_m, ten_m = levels.get(0, {}), levels.get(2, {}), levels.get(10, {})
    u_name = '10u' if '10u' in ten_m else 'u'
    v_name = '10v' if '10v' in ten_m else 'v'
    missing = ([k for k in ('orog', 'sp') if k not in surface]
               + [k for k in ('2t', '2d') if k not in two_m]
               + [k for k in (u_name, v_name) if k not in ten_m])
    if missing:
        raise RuntimeError(f'URMA analysis {run_date:%Y-%m-%d %HZ} missing {missing}')

    diff = ((ten_m['_lon'] - lov + 180) % 360) - 180
    angle = math.radians(cone * diff)
    u_earth = ten_m[v_name] * math.sin(angle) + ten_m[u_name] * math.cos(angle)
    v_earth = ten_m[v_name] * math.cos(angle) - ten_m[u_name] * math.sin(angle)

    return {
        'run_time': run_date,
        'latitude': ten_m['_lat'],
        'longitude': ((ten_m['_lon'] + 180) % 360) - 180,
        'terrain_height': surface['orog'],
        'pressure': surface['sp'] / 100.,  # Pa -> hPa
        'temperature': two_m['2t'] - 273.15,
        'dewpoint': two_m['2d'] - 273.15,
        'speed': math.hypot(u_earth, v_earth) * 1.9438445,  # m/s -> kt
        'direction': math.degrees(math.atan2(-u_earth, -v_earth)) % 360,
    }
