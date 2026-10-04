# Copyright (c) 2015,2016,2017 MetPy Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
"""
===============
Simple Sounding
===============

Fetch and plot a Skew-T LogP sounding (plus a comparison run, overlaid)
for a station, using MetPy, siphon, and Open-Meteo.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import matplotlib
# Non-interactive backend: every output here goes through fig.savefig, and
# nothing opens a window (the plt.show() at the bottom of main() is left
# commented out). A GUI backend like QtAgg additionally needs a display and
# Qt itself present *at import time*, which fails on a headless host -- the
# Telegram bot imports this module directly. Switch this to 'QtAgg' if you
# re-enable plt.show() and want an interactive window.
matplotlib.use('Agg')

# Fira Sans for all plot text, with matplotlib's bundled DejaVu Sans kept
# behind it as a per-glyph fallback for anything Fira lacks.
#
# It has to be font.family, and it has to be a list: only that form builds
# a real fallback chain. Setting font.sans-serif to the same list does NOT
# -- that one is first-match-wins, so a glyph missing from the first font
# renders as a blank box even when a later entry has it. Nor does either
# list reach out to the rest of the system's fonts; only what is named
# here is ever consulted.
matplotlib.rcParams['font.family'] = ['Fira Sans', 'DejaVu Sans']

# Keep text as real <text> in the SVG instead of converting every glyph
# to outlines (matplotlib's default, svg.fonttype='path'). Outlines are
# self-contained but the labels stop being text: not selectable, not
# searchable, not editable in Inkscape/Illustrator, and a good deal
# bigger on disk. The tradeoff is that the viewer now needs Fira Sans
# installed, falling back to whatever its renderer picks otherwise --
# which is why font.family above names DejaVu Sans as the second choice.
# Only affects SVG; the PNG is rasterised either way.
matplotlib.rcParams['svg.fonttype'] = 'none'

# Set before any project module pulls in pyplot, which the imports
# below do -- so this stays above them.
import matplotlib.pyplot as plt
import requests
from metpy.units import units
from scipy.ndimage import median_filter

from cli import parse_args
from config import MODEL_PRIORITY, SHOW_PREVIOUS, utc_offset_label
from geocode import describe_location
from grib import fetch_hrrr_profile, fetch_rrfs_profile
from lapse_rate import render_lapse_rate_panel
from skewt import render_skewt_panel
from thredds import fetch_gfs_profile
from wyoming import fetch_recent_sounding, latest_synoptic_time


MODEL_FETCHERS = {'hrrr': fetch_hrrr_profile, 'rrfs': fetch_rrfs_profile, 'gfs': fetch_gfs_profile}


def fetch_model_profile(lat, lon, date, model=None, forecast_hour=None, run_datetime=None):
    """Fetch a modeled vertical profile at an arbitrary point, trying
    models in priority order (best resolution first) until one succeeds.
    Pass `model` to force a specific one instead of the full chain, and
    forecast_hour/run_datetime to pin a specific lead/run instead of
    resolving one from `date` (see _fetch_grib_profile) -- not supported
    for GFS.
    Returns (dataframe, valid_time, run_time, model_name_used). run_time
    is the underlying model run's own init time, which differs from
    valid_time for a forecast lead > 0 (e.g. a future valid time -- see
    _fetch_grib_profile); for GFS the two are always equal, since the
    specific cycle behind a given valid time isn't tracked for it."""
    errors = []
    for name in ([model] if model else MODEL_PRIORITY):
        try:
            df, valid_time, run_time = MODEL_FETCHERS[name](
                lat, lon, date, forecast_hour=forecast_hour, run_datetime=run_datetime)
        except Exception as e:
            errors.append(f'{name}: {e}')
            continue
        return df, valid_time, run_time, name
    raise RuntimeError('No model source available -- ' + '; '.join(errors))


@dataclass
class Rendered:
    """What render() drew: the output path stub (plus .png/.svg) and
    the data behind it, so a caller can tell later whether a newer model
    run has superseded it (see replay.py)."""
    stub: str
    valid_time: datetime
    model: str | None = None        # None for an observed sounding
    run_time: datetime | None = None


def main(args=None, output_dir=None):
    """Fetch the requested sounding(s), render both panels, and write the
    figure to disk as <stub>.png and <stub>.svg. Returns that stub (the
    written paths are the stub plus those two extensions). See render()
    for the details, and for what else it reports back."""
    return render(args, output_dir).stub


def render(args=None, output_dir=None):
    """Fetch the requested sounding(s), render both panels, and write the
    figure to disk as <stub>.png and <stub>.svg. Returns a Rendered with
    that stub (the written paths are the stub plus those two extensions).

    Parses the command line when args is None, but accepts an
    already-built argparse Namespace so this can be driven in-process
    (the Telegram bot, a batch driver, a test) instead of through a
    subprocess -- which also means the caller gets the output path back
    directly rather than having to guess at it from the filesystem.

    output_dir defaults to the working directory, which is what the
    command line wants; an in-process caller whose working directory
    isn't this script's should pass one explicitly."""
    if args is None:
        args = parse_args()
    station = args.station

    if args.lat is not None and args.lon is not None:
        # args.datetime (possibly None) is passed through as-is, not
        # defaulted via latest_synoptic_time() -- that rounds to the nearest
        # 00Z/12Z synoptic time, which makes sense for the twice-daily
        # Wyoming archive but would understate what's actually available for
        # an hourly model (e.g. defaulting to a 12Z analysis when a fresher
        # 15Z run has already posted). fetch_model_profile's date=None means
        # exactly "the latest run available, as-is".
        df, date, model_run_date, model_used = fetch_model_profile(
            args.lat, args.lon, args.datetime, model=args.model,
            forecast_hour=args.forecast_hour, run_datetime=args.run_datetime)
        # Kept short (no coordinates) so it doesn't collide with the
        # right-aligned date/time sharing the title's top row -- the full
        # point still ends up on the title's second line via location_desc,
        # and in full precision in filename_stub/compare_label below.
        station = f'{model_used.upper()}_{args.lat:.2f}_{args.lon:.2f}'
        title_prefix = f'{model_used.upper()} Modeled Sounding'
    else:
        date = args.datetime or latest_synoptic_time()
        df, date = fetch_recent_sounding(date, station)
        title_prefix = f'{station} Observed Sounding'
        model_run_date = None
        model_used = None

    # The comparison sounding defaults to the previous synoptic run (12h
    # earlier) for the same station, but --compare/--compare-station let it
    # be any run for any station instead, and --compare-lat/--compare-lon
    # swap in a modeled profile (at any point, any of the three model
    # sources) instead of an observed one.
    #
    # That "previous run" default is off for a modeled primary run, though,
    # unless a --compare/--compare-station/--compare-lat flag actually asked
    # for one: a model fetch is a real network pull + GRIB decode (seconds
    # to over a minute depending on cache state), so silently doubling that
    # cost for a default nobody asked for isn't worth it the way it is for
    # the Wyoming archive's near-instant CSV fetch.
    show_compare = SHOW_PREVIOUS and not (
        args.lat is not None and args.compare is None
        and args.compare_station is None and args.compare_lat is None
        and args.compare_run_datetime is None and args.compare_forecast_hour is None)

    compare_df = compare_date = compare_station = None
    if show_compare:
        if args.compare_lat is not None and args.compare_lon is not None:
            compare_start = args.compare or date
            compare_df, compare_date, _compare_run_date, compare_model_used = fetch_model_profile(
                args.compare_lat, args.compare_lon, compare_start, model=args.compare_model,
                forecast_hour=args.compare_forecast_hour, run_datetime=args.compare_run_datetime)
            compare_station = f'{compare_model_used.upper()}_{args.compare_lat:.2f}_{args.compare_lon:.2f}'
        elif args.lat is not None and args.compare_station is None:
            # Primary source was itself a modeled point and --compare/
            # --compare-run-datetime/--compare-forecast-hour (no
            # --compare-lat) asked for a comparison -- same model chain and
            # point, at that time/run/lead, rather than trying (and
            # failing) to look up "station" GFS_32.87_-117.14 in the
            # Wyoming archive.
            compare_df, compare_date, _compare_run_date, compare_model_used = fetch_model_profile(
                args.lat, args.lon, args.compare, model=args.model,
                forecast_hour=args.compare_forecast_hour, run_datetime=args.compare_run_datetime)
            compare_station = f'{compare_model_used.upper()}_{args.lat:.2f}_{args.lon:.2f}'
        else:
            compare_station = args.compare_station or station
            compare_start = args.compare or (date - timedelta(hours=12))
            compare_df, compare_date = fetch_recent_sounding(compare_start, compare_station)

    # Pull that day's high temp for the station's own coordinates from
    # Open-Meteo -- the live forecast endpoint for a recent/upcoming date, or
    # the historical archive endpoint for a date outside the forecast
    # endpoint's ~3-month window (e.g. the 2025 batch runs below). This also
    # gives us the station's local-time offset, used for both panels' titles.
    station_lat = df['latitude'].iloc[0]
    station_lon = df['longitude'].iloc[0]
    location_desc = describe_location(station, station_lat, station_lon, method=args.geocoder)
    is_forecast = (datetime.now(timezone.utc) - date) < timedelta(hours=12)
    weather_url = ('https://api.open-meteo.com/v1/forecast' if is_forecast
                  else 'https://archive-api.open-meteo.com/v1/archive')
    target_date = date.strftime('%Y-%m-%d')
    # Open-Meteo occasionally answers a well-formed request with a 200 that's
    # missing 'daily' (seen intermittently, most likely transient rate
    # limiting under repeated nearby requests) -- a couple of retries clears
    # it every time it's been hit in practice; a bare KeyError on persistent
    # failure wouldn't say why.
    for attempt in range(3):
        forecast = requests.get(
            weather_url,
            params={
                'latitude': station_lat,
                'longitude': station_lon,
                'daily': 'temperature_2m_max',
                'timezone': 'auto',
                'start_date': target_date,
                'end_date': target_date,
            },
        ).json()
        if 'daily' in forecast:
            break
        time.sleep(2)
    else:
        raise RuntimeError(f'Open-Meteo forecast request missing "daily" after 3 tries: {forecast}')
    forecast_high = forecast['daily']['temperature_2m_max'][0]

    # The IANA zone name, not Open-Meteo's utc_offset_seconds: that field
    # is the zone's offset *right now*, not on the sounding's own date, so
    # using it mislabels anything on the other side of a DST transition by
    # an hour (a January sounding rendered in July came out as UTC-7 when
    # it was really PST/UTC-8). Converting each instant through the zone
    # itself gets the historical offset right, and gives a real PST/PDT
    # abbreviation instead of a bare numeric offset.
    try:
        tz = ZoneInfo(forecast['timezone'])
    except (KeyError, ZoneInfoNotFoundError):
        tz = timezone.utc

    # Label the model run behind this profile -- for every modeled
    # sounding, including an analysis. This used to be skipped when the run
    # time equalled the valid time, on the grounds that the title's own
    # time is then the run time; but nothing on the plot said so, which
    # left no way to tell an analysis from a forecast, or to see which run
    # it came from at all. The lead comes along for the same reason, in the
    # models' own fXX notation (f00 being the analysis) -- the fallback to
    # older synoptic cycles means the lead is no longer predictable from
    # the run time alone.
    run_label = None
    if model_run_date is not None:
        run_local_dt = model_run_date.astimezone(tz)
        lead_hours = round((date - model_run_date).total_seconds() / 3600)
        run_label = (f'Model run: {run_local_dt:%Y-%m-%d %H:%M} '
                     f'{run_local_dt:%Z} ({utc_offset_label(run_local_dt)}), f{lead_hours:02d}')

    fig = plt.figure(figsize=(9.5, 10))
    gs = fig.add_gridspec(1, 2, width_ratios=[3, 1], wspace=0.14)
    skew, parcel_p_path, parcel_profile, parcel_env_T = render_skewt_panel(
        fig, gs[0, 0], df, date, tz,
        forecast_high=forecast_high, is_forecast=is_forecast,
        location_desc=location_desc, title_prefix=title_prefix,
        run_label=run_label, is_modeled=args.lat is not None, site_name=args.name,
        station=station, altitude_unit=args.altitude_unit)
    render_lapse_rate_panel(fig, gs[0, 1], df, skew.ax,
                            compare_sounding_df=compare_df if show_compare else None,
                            parcel_p=parcel_p_path, parcel_profile=parcel_profile,
                            parcel_env_T=parcel_env_T)
    filename_stub = f'{station}_{date:%Y%m%d_%HZ}'
    # A forecast also carries its run, so a later run's forecast for the
    # same hour is a new file rather than overwriting this one. Analyses
    # (and GFS, whose run isn't known) keep the plain name.
    if model_run_date is not None and model_run_date != date:
        filename_stub += f'_run{model_run_date:%Y%m%d_%HZ}'
    if output_dir is not None:
        filename_stub = str(Path(output_dir) / filename_stub)

    if show_compare:
        compare_p = compare_df['pressure'].values * units.hPa
        compare_T = compare_df['temperature'].values * units.degC
        compare_Td = compare_df['dewpoint'].values * units.degC
        compare_local_dt = compare_date.astimezone(tz)

        # Default (no --compare/--compare-station given) reads as "the
        # previous run"; an explicit comparison names the station whenever
        # it differs from the main one, since "Previous" alone would be
        # misleading for an unrelated station/date. (A modeled primary run
        # only ever reaches this block via an explicit --compare* flag --
        # show_compare is False for the unflagged default case -- so it
        # always takes the "explicit comparison" branch, never "Previous".)
        if args.compare or args.compare_station or args.compare_lat is not None:
            compare_label = (f'{compare_station} comparison' if compare_station != station
                             else 'Comparison')
        else:
            compare_label = f'Previous {station} observation'

        skew.plot(compare_p, compare_T, color='red', linestyle='dashed', alpha=0.4,
                 label=f'{compare_label} ({compare_local_dt:%y-%m-%d %H:%M})')
        skew.plot(compare_p, median_filter(compare_Td.m, size=5, mode='nearest') * units.degC,
                 color='green', linestyle='dashed', alpha=0.4)
        skew.ax.legend(loc='upper left')

    # Export a web-friendly version: PNG at 2x pixel density for crisp
    # rendering on high-DPI screens, with the whitespace margin trimmed.
    fig.savefig(f'{filename_stub}.png', dpi=150, bbox_inches='tight')

    # SVG is smaller and stays sharp at any zoom level for line art like this;
    # use it instead of the PNG if the site can serve vector images.
    fig.savefig(f'{filename_stub}.svg', bbox_inches='tight')

    # Closed explicitly: main() can now be called repeatedly in one
    # process (a batch loop, the bot), and pyplot keeps every unclosed
    # figure alive for the life of the interpreter.
    plt.close(fig)
    return Rendered(filename_stub, date, model_used, model_run_date)


if __name__ == '__main__':
    main()
