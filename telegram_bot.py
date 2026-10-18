"""Telegram bot: on-demand Skew-T soundings via Simple_Sounding.py.

Usage (in a chat with the bot):
    /sounding                      -> latest NKX sounding
    /sounding 72572                -> latest sounding for station 72572
    /sounding NKX 2025-07-01T17    -> that station's 00Z launch (5pm PDT)

    /sites                          -> list named sites (from sites.tsv)
    /site Little Black              -> modeled sounding at that site, this hour
    /site Little Black 14           -> ... at 2pm today
    /site Little Black 2026-09-17T12 -> ... at that time

    /pf lb                          -> today's daylight hours, as tables
    /pf lb 17                       -> ... the 17th's
    /pf lb sat hrrr                 -> ... Saturday's, from HRRR
    (/pf is short for /pointforecast; "lb" is an alias from sites.tsv)

Times are local (SOUNDING_TIMEZONE, default America/Los_Angeles) unless
they say otherwise -- "2026-09-17T19Z" or "21Z" are UTC.

Setup:
    export TELEGRAM_BOT_TOKEN="<token from @BotFather>"
    python telegram_bot.py

Run it from this directory: Simple_Sounding is imported directly (not
shelled out to), so it has to be importable, and its cache/ lives here.

No chat ID is needed -- this replies wherever the command came from,
it doesn't push anywhere on its own.

SOUNDING_PREWARM="6-20" fetches each new model run for this hour and
the next ahead of any /site, between those local hours (inclusive);
unset, nothing is fetched until asked for. See _prewarm_loop.

Every command is logged as it arrives and again with its outcome and how
long the reply took ("/pf lb 17 -> ok in 4.2s [site=Little Black, ...]"),
and counted for Prometheus on METRICS_PORT when that is set -- see
metrics.py.
"""

import asyncio
import contextlib
import ctypes
import ctypes.util
import io
import logging
import os
import re
import shutil
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil import parser as dateutil_parser
from telegram import BotCommand, InputMediaPhoto, Update
from telegram import ChatMember
from telegram.ext import (Application, ChatMemberHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import forecast_table
import grib
import metrics
import point_forecast
import Simple_Sounding
import wyoming
from config import CACHE_DIR
from replay import ReplayIndex
from wyoming import latest_synoptic_time

# With the time on every line, so a command can be found by when it ran
# without kubectl --timestamps.
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
# httpx logs every request at INFO, and the Telegram API puts the bot
# token in the URL path -- so at INFO the token gets written in clear
# text to wherever this bot's output is redirected. Kept at WARNING so
# a log file never becomes a credential leak.
logging.getLogger('httpx').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent
# Overridable so a container can keep the site list in a mounted config
# file and write renders to a volume rather than next to the code.
SITES_FILE = Path(os.environ.get('SOUNDING_SITES_FILE') or SCRIPT_DIR.parent / 'sites.tsv')
OUTPUT_DIR = Path(os.environ.get('SOUNDING_OUTPUT_DIR') or SCRIPT_DIR)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
# Earlier renders, re-sent while still current instead of fetched and
# drawn again -- see replay.py.
REPLAY = ReplayIndex(OUTPUT_DIR / 'replay-index')
# The zone times typed into chat are read in. The command line keeps
# reading bare times as UTC (cli.parse_datetime); people in a chat think
# in local time, and "2pm" shouldn't need converting to 21Z first.
LOCAL_TZ = ZoneInfo(os.environ.get('SOUNDING_TIMEZONE') or 'America/Los_Angeles')

# matplotlib's pyplot is global, mutable state, so two renders must never
# overlap -- concurrent requests queue here instead. Rendering in-process
# rather than shelling out also means the caller is handed the exact
# output path back, instead of having to guess at its own output by
# picking the newest matching PNG off disk (which two overlapping
# requests for the same site could get wrong).
_render_lock = threading.Lock()
# Point forecasts' fetch and decode, which only ever race each other (on
# the same cached hour files) -- not soundings, which use other files.
# Only drawing their tables needs _render_lock too, so a /site or
# /sounding waits out a point forecast's ~2 s of drawing, not its fetch.
# Always taken before _render_lock, never while holding it.
_point_forecast_lock = threading.Lock()


class _Requests:
    """How many requests are in progress, so the pre-warm can stand aside
    for them. A lock alone doesn't do that: whoever releases it can take
    it straight back before a waiting thread wakes, and the pre-warm
    releasing and re-taking it site after site could keep a request
    waiting through several."""

    def __init__(self):
        self._changed = threading.Condition()
        self._active = 0

    @contextlib.contextmanager
    def active(self):
        with self._changed:
            self._active += 1
        try:
            yield
        finally:
            with self._changed:
                self._active -= 1
                self._changed.notify_all()

    def wait_until_idle(self):
        with self._changed:
            self._changed.wait_for(lambda: self._active == 0)


REQUESTS = _Requests()


def _release_freed_memory():
    """Hand memory the allocator is holding back to the OS.

    Freeing a ~950 MB run's field arrays returns them to glibc, which
    keeps them on its own free lists rather than to the kernel -- so RSS
    stays high and a long-lived service looks like it is leaking when it
    isn't. malloc_trim releases what it can. Paired with MALLOC_ARENA_MAX
    in the systemd unit, which stops the fetch pool's threads each
    building their own arena in the first place.

    glibc-specific: absent on macOS, hence the getattr guard."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library('c'))
        trim = getattr(libc, 'malloc_trim', None)
        if trim is not None:
            trim(0)
    except OSError:
        pass


# A named site. elevation_ft is the launch's own, MSL, or None if unknown.
Site = namedtuple('Site', 'name lat lon elevation_ft aliases')


def load_sites():
    """Parse sites.tsv: one 'Name   lat   lon   [elevation]   [aliases]'
    per line, name and the two floats separated by runs of whitespace
    (the name itself may contain single spaces, e.g. "Little Black"),
    then optionally the launch elevation in whole feet MSL, then the
    other names the site goes by, comma-separated ("lb"). Returns Sites."""
    sites = []
    if not SITES_FILE.exists():
        return sites
    with open(SITES_FILE) as f:
        for line in f:
            line = line.rstrip('\n')
            if not line.strip():
                continue
            m = re.match(r'^(.*?)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)(?:\s+(\d+))?(?:\s+(.+?))?\s*$', line)
            if m:
                aliases = tuple(a.strip() for a in (m.group(5) or '').split(',') if a.strip())
                elevation = int(m.group(4)) if m.group(4) else None
                sites.append(Site(m.group(1), float(m.group(2)), float(m.group(3)), elevation, aliases))
    return sites


def sites_region(sites, margin=0.25):
    """(lat_min, lat_max, lon_min, lon_max) around every named site, with
    `margin` degrees to spare -- the area grib.set_region keeps on hand.
    None without any sites."""
    if not sites:
        return None
    lats = [site.lat for site in sites]
    lons = [site.lon for site in sites]
    return (min(lats) - margin, max(lats) + margin, min(lons) - margin, max(lons) + margin)


def match_site(args):
    """Match the longest possible prefix of args (joined with spaces,
    case-insensitive) against a known site name, e.g. ['Little', 'Black',
    '2026-09-17T19'] matches the two-word site "Little Black" and leaves
    ['2026-09-17T19'] over for the datetime -- tried longest-first so a
    multi-word site name isn't cut short by a plausible-looking prefix
    match. A site's aliases match the same way as its name ("lb 14").
    Returns (Site, remaining_args), or (None, args) if no prefix of args
    names a known site."""
    sites = load_sites()
    for n in range(len(args), 0, -1):
        candidate = ' '.join(args[:n]).lower()
        for site in sites:
            if candidate in (site.name.lower(), *(alias.lower() for alias in site.aliases)):
                return site, args[n:]
    return None, args


def chat_datetime(text):
    """A time typed into chat, as an ISO string with an explicit offset
    for --datetime: read in LOCAL_TZ unless it names its own zone, with
    missing fields taken from the current local hour. A bare hour ("14")
    means that hour today, and "21Z" that hour in UTC -- dateutil alone
    would read "14" as the 14th of the month. Anything unparseable is
    passed through untouched, for argparse to reject with its usual
    "Invalid date/time" message."""
    now = datetime.now(LOCAL_TZ).replace(minute=0, second=0, microsecond=0)
    m = re.fullmatch(r'(\d{1,2})([zZ]?)', text.strip())
    if m and int(m.group(1)) < 24:
        if m.group(2):
            return now.astimezone(ZoneInfo('UTC')).replace(hour=int(m.group(1))).isoformat()
        return now.replace(hour=int(m.group(1))).isoformat()
    try:
        dt = dateutil_parser.parse(text, default=now.replace(tzinfo=None))
    except (ValueError, OverflowError):
        return text
    return (dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)).isoformat()


class BadArguments(Exception):
    """Arguments argparse refused, carrying its own complaint as text."""


def _render(argv: list, request=True) -> tuple:
    """Blocking: build arguments from argv, render, return the PNG path
    and whether it was replayed rather than drawn. request=False for the
    pre-warm, which isn't one (REQUESTS).

    Runs on a worker thread (see _render_and_reply) because the whole
    fetch-and-plot is synchronous and slow enough -- seconds to minutes
    on a cold GRIB2 fetch -- to stall the bot's event loop otherwise."""
    with REQUESTS.active() if request else contextlib.nullcontext(), _render_lock:
        # argparse reports bad input by printing to stderr and exiting,
        # so the actual complaint ("Invalid date/time: ...") is only
        # available by capturing that -- SystemExit itself carries just
        # the exit status. Captured under the lock because redirecting
        # stderr is process-wide.
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                args = Simple_Sounding.parse_args(argv)
        except SystemExit:
            complaint = stderr.getvalue().strip().splitlines()
            # Last line is argparse's "<prog>: error: <what was wrong>";
            # only the last part means anything to someone in a chat.
            message = complaint[-1].split('error: ', 1)[-1] if complaint else 'invalid arguments'
            raise BadArguments(message) from None
        # "Latest" for an observed sounding means the latest synoptic
        # launch, so pin that now: it's what this request is for, and it
        # has to be part of the replay key or a 00Z render would be
        # replayed for the 12Z launch.
        if args.lat is None and args.datetime is None:
            args.datetime = latest_synoptic_time()
        replayed = REPLAY.lookup(args)
        if replayed is not None:
            logger.info('Replaying %s', replayed.name)
            return replayed, True
        try:
            rendered = Simple_Sounding.render(args, output_dir=OUTPUT_DIR)
        finally:
            # The decoded-field memo pays off across sites in one batch;
            # here each request is usually a different run, so it would
            # just pin ~1 GB for the life of the service.
            grib.clear_field_cache()
            _release_freed_memory()
        REPLAY.record(args, rendered)
    return Path(f'{rendered.stub}.png'), False


async def _render_and_reply(update: Update, argv: list, timeout: int, label: str) -> None:
    """Shared plumbing for /sounding and /site: render in a worker
    thread, then reply with the image or with whatever went wrong."""
    await update.message.reply_text(f'Fetching {label} sounding...')

    command = metrics.current()
    try:
        png, replayed = await asyncio.wait_for(asyncio.to_thread(_render, argv), timeout)
    except asyncio.TimeoutError:
        # Only stops waiting -- the worker thread itself can't be
        # cancelled, so it runs to completion and keeps the lock until
        # then. Further requests queue rather than failing.
        command.done('timeout')
        await update.message.reply_text('Timed out fetching/plotting that sounding.')
        return
    except BadArguments as e:
        command.done('bad_request', str(e))
        await update.message.reply_text(f'Bad arguments: {e}')
        return
    except Exception as e:
        # No data to be had -- an archive that's down, a launch or run
        # that doesn't exist -- isn't the bot failing.
        unavailable = isinstance(e, wyoming.ArchiveUnavailable) or str(e).startswith(
            ('No recent sounding data', 'No model source available'))
        if not unavailable:
            logger.exception('Sounding render failed')
        command.done('unavailable' if unavailable else 'error', f'{type(e).__name__}: {e}')
        await update.message.reply_text(f'Failed: {type(e).__name__}: {e}')
        return

    if not png.exists():
        command.done('error', 'no image produced')
        await update.message.reply_text('Ran, but no image was produced.')
        return

    # The model is the first part of a modeled render's name
    # ("RRFS_32.99_-117.12_..."); an observed one starts with its station.
    prefix = png.name.split('_')[0].lower()
    command.ask(model=prefix if prefix in point_forecast.SOURCES else 'observed')
    command.done('ok', f'{"replayed" if replayed else "rendered"} {png.name}')
    with open(png, 'rb') as photo:
        await update.message.reply_photo(photo)


def _day_of(iso_text):
    """metrics.DAYS bucket for a time chat_datetime produced, or None if
    it couldn't make one (argparse will reject it shortly)."""
    try:
        when = datetime.fromisoformat(iso_text).astimezone(LOCAL_TZ)
    except ValueError:
        return None
    return metrics.day_label(when, datetime.now(LOCAL_TZ).date())


@metrics.tracked('sounding')
async def sounding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    # A named site is a flying site, not a radiosonde launch station: there
    # is no observed sounding for it, so the Wyoming lookup below would only
    # come back empty ("No recent sounding data") -- or, for a two-word name,
    # read its second word as a bad datetime. Point at /site instead,
    # keeping any datetime that was given.
    match, remaining = match_site(args)
    if match is not None:
        suggestion = ' '.join(['/site', match.name, *remaining])
        metrics.current().done('bad_request', f'a site, not a station: {suggestion}')
        await update.message.reply_text(f'Command failed: Did you mean {suggestion}?')
        return

    station = args[0] if len(args) >= 1 else 'NKX'
    argv = ['--station', station]
    day = metrics.day_label(latest_synoptic_time().astimezone(LOCAL_TZ), datetime.now(LOCAL_TZ).date())
    if len(args) >= 2:
        argv += ['--datetime', chat_datetime(args[1])]
        day = _day_of(argv[-1])
    metrics.current().ask(site=metrics.station_label(station), day=day)

    await _render_and_reply(update, argv, timeout=120, label=station)


@metrics.tracked('site')
async def site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        metrics.current().done('bad_request', 'no site')
        await update.message.reply_text(
            'Usage: /site <name> [datetime]\nSee /sites for the list of names.')
        return

    match, remaining = match_site(args)
    if match is None:
        metrics.current().done('bad_request', 'unknown site')
        await update.message.reply_text(
            f'Unknown site {" ".join(args)!r}. See /sites for the list of names.')
        return
    name, lat, lon = match.name, match.lat, match.lon

    # No time means this hour -- a forecast from the freshest run -- rather
    # than the freshest run's own analysis, which for RRFS (3-hourly, posted
    # ~1.5 h late) can be up to ~4 h old: 11am's at 2pm.
    when = chat_datetime(' '.join(remaining)) if remaining else datetime.now(LOCAL_TZ).isoformat()
    argv = ['--lat', str(lat), '--lon', str(lon), '--name', name, '--datetime', when]
    metrics.current().ask(site=name, day=_day_of(when))

    # A named site is a modeled (HRRR/RRFS/GFS) sounding, not the fast
    # Wyoming CSV archive /sounding uses -- a cold fetch (no cached GRIB2
    # run yet) can take several minutes, so this gets a much longer
    # timeout than /sounding's.
    await _render_and_reply(update, argv, timeout=600, label=name)


# Point forecasts already drawn, so a repeat -- or one the pre-warm got
# to first -- is a reply rather than a fetch: {(site, day, model): (plan,
# forecast, PNG paths)}. Good while the plan is: one whose runs differ
# (a newer run now answers some hour of the day) is drawn afresh and
# replaces it.
_drawn = {}
# Drawn into the download cache, which the janitor prunes, rather than
# the archive: most pre-drawn tables are never asked for. The ones sent
# are copied into the archive as they go (see pointforecast).
POINT_FORECAST_DIR = CACHE_DIR / 'point-forecast'


def _point_forecast_tables(site, day, models, progress=None, prime_for=()):
    """Blocking: one site's day as (forecast, [thermals PNG, wind PNG],
    whether it had been drawn already). prime_for: other sites about to
    be asked for the same day, to decode this one's hours for in the same
    pass (point_forecast.prime).

    Planned first, outside any lock -- a few lookups at most -- so a day
    already drawn from its current runs comes straight back, whatever
    else is running. Otherwise fetched under _point_forecast_lock (two at
    once would race on the same cached hour files, and two cold fetches
    only halve each other's bandwidth anyway) and drawn under
    _render_lock as well, for matplotlib's global rcParams."""
    plan = point_forecast.plan_day(site.lat, site.lon, day, models)
    key = (site, day, plan.source.model)

    def drawn():
        entry = _drawn.get(key)
        if entry is not None and entry[0] == plan and all(path.exists() for path in entry[2]):
            return entry[1], entry[2], True
        return None

    if (hit := drawn()) is not None:
        return hit
    with _point_forecast_lock:
        # The pre-warm may have drawn it while this waited.
        if (hit := drawn()) is not None:
            return hit
        launch_m = site.elevation_ft / point_forecast.M_TO_FT if site.elevation_ft else None
        try:
            result = point_forecast.forecast(site.lat, site.lon, day, progress=progress,
                                             launch_m=launch_m, plan=plan)
            if prime_for:
                point_forecast.prime(plan, [(other.lat, other.lon) for other in prime_for])
        finally:
            _release_freed_memory()
        POINT_FORECAST_DIR.mkdir(parents=True, exist_ok=True)
        with _render_lock:
            paths = forecast_table.render_tables(result, LOCAL_TZ, name=site.name,
                                                 output_dir=POINT_FORECAST_DIR)
        today = datetime.now(LOCAL_TZ).date()
        for stale in [k for k in _drawn if k[1] < today]:
            del _drawn[stale]
        _drawn[key] = (plan, result, paths)
    return result, paths, False


def _requested_point_forecast_tables(*args):
    """_point_forecast_tables for a request, which the pre-warm waits for."""
    with REQUESTS.active():
        return _point_forecast_tables(*args)


def _point_forecast_day(site, now):
    """The day a /pf without one means: the next daylight at the site --
    today, until the sun has set on it."""
    day = now.date()
    sun = point_forecast.sun_times(site.lat, site.lon, day)
    if sun is not None and now > sun[1]:
        day += timedelta(days=1)
    return day


@metrics.tracked('pf')
async def pointforecast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    command = metrics.current()
    if not args:
        command.done('bad_request', 'no site')
        await update.message.reply_text(
            'Usage: /pf <site> [day] [rrfs|hrrr|gfs]\nSee /sites for the list of names.')
        return

    match, remaining = match_site(args)
    if match is None:
        command.done('bad_request', 'unknown site')
        await update.message.reply_text(
            f'Unknown site {" ".join(args)!r}. See /sites for the list of names.')
        return
    name = match.name

    # A model named anywhere after the site forces that one model; without
    # one it is point_forecast.DEFAULT_MODELS, RRFS and then HRRR.
    named = [word.lower() for word in remaining if word.lower() in point_forecast.SOURCES]
    if len(named) > 1:
        command.done('bad_request', 'more than one model')
        await update.message.reply_text(f'Pick one model, not {" and ".join(named)}.')
        return
    models = named or None
    remaining = [word for word in remaining if word.lower() not in point_forecast.SOURCES]

    now = datetime.now(LOCAL_TZ)
    if remaining:
        try:
            day = point_forecast.parse_day(' '.join(remaining), now.date())
        except ValueError:
            command.done('bad_request', 'bad day')
            await update.message.reply_text(
                f'Bad day {" ".join(remaining)!r}: try 17, sat, tomorrow or 2026-10-17.')
            return
    else:
        day = _point_forecast_day(match, now)

    command.ask(site=name, day=metrics.day_label(day, now.date()))
    label = f'{name} point forecast for {day:%a %b} {day.day}'
    status = await update.message.reply_text(f'Fetching {label}...')

    # A cold day is a dozen hours of GRIB2 and takes minutes, so the
    # status message counts them off. Called from the worker thread;
    # edits are spaced out to stay clear of Telegram's flood limit.
    loop = asyncio.get_running_loop()
    last_edit, shown = time.monotonic(), 0

    def progress(done, total):
        nonlocal last_edit, shown
        # The total grows if the day's thermals need a second, deeper
        # fetch; the count shown never goes back down for it.
        percent = 100 * done // total
        if shown < percent < 100 and time.monotonic() - last_edit >= 5:
            last_edit, shown = time.monotonic(), percent
            asyncio.run_coroutine_threadsafe(
                status.edit_text(f'Fetching {label}... {percent}%'), loop)

    try:
        result, paths, drawn = await asyncio.wait_for(
            asyncio.to_thread(_requested_point_forecast_tables, match, day, models, progress),
            1800)
    except asyncio.TimeoutError:
        command.done('timeout')
        await update.message.reply_text('Timed out fetching that forecast.')
        return
    except point_forecast.Unavailable as e:
        command.done('unavailable', str(e))
        hint = ''
        if models is None:
            # GFS is never a silent fallback (see DEFAULT_MODELS), so say
            # how to ask for it.
            hint = (' RRFS goes out about 3.5 days and HRRR 2.'
                    f'\nFor GFS anyway: /pf {" ".join(args)} gfs')
        await update.message.reply_text(f'{e}{hint}')
        return
    except Exception as e:
        logger.exception('Point forecast failed')
        command.done('error', f'{type(e).__name__}: {e}')
        await update.message.reply_text(f'Failed: {type(e).__name__}: {e}')
        return

    command.ask(model=result.source.model)
    command.done('ok', f'{result.source.label}, runs '
                       + ', '.join(f'{run:%d %HZ}' for run in result.runs)
                       + (', drawn already' if drawn else
                          f', {result.downloaded_bytes / 1e6:.0f} MB downloaded'))

    newest = result.runs[-1].astimezone(LOCAL_TZ)
    caption = (f'{name}, {day:%a %b} {day.day} \N{EM DASH} {result.source.label}, '
               f'run {newest:%a %H:%M %Z}')
    media = [InputMediaPhoto(path.read_bytes(), caption=caption if i == 0 else None)
             for i, path in enumerate(paths)]
    await update.message.reply_media_group(media)
    # Into the archive with every other image sent (see POINT_FORECAST_DIR).
    for path in paths:
        with contextlib.suppress(OSError):
            if not (OUTPUT_DIR / path.name).exists():
                shutil.copyfile(path, OUTPUT_DIR / path.name)
    # The tables replace the progress counter, which would otherwise sit
    # above them stopped short of 100%.
    with contextlib.suppress(Exception):
        await status.delete()


@metrics.tracked('sites')
async def list_sites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sites = load_sites()
    if not sites:
        metrics.current().done('unavailable', 'no sites.tsv')
        await update.message.reply_text('No named sites configured (sites.tsv not found or empty).')
        return
    lines = [f'{site.name} ({", ".join(site.aliases)})' if site.aliases else site.name
             for site in sites]
    await update.message.reply_text('Named sites:\n' + '\n'.join(lines))


# How often the pre-warm checks for a newer run. A run posts about hourly;
# a check that finds nothing new is a handful of ~1 s lookups.
PREWARM_INTERVAL = timedelta(minutes=5)


def prewarm_hours(spec):
    """The local hours a SOUNDING_PREWARM or POINT_FORECAST_PREWARM value
    like '6-20' covers (inclusive), or an empty range when it's unset."""
    if not spec:
        return range(0)
    start, end = (int(part) for part in spec.split('-'))
    return range(start, end + 1)


def _prewarm_loop(hours):
    """Keep the freshest run's soundings for this hour and the next on
    disk before anyone asks, so a /site doesn't wait out the download --
    which, at ~300 MB a run for RRFS, is most of a cold render.

    Asks for exactly what /site would (fetch_model_profile at a valid
    time, the same model chain), so whatever run and lead a request
    resolves to is the one already cached. One site's point is enough:
    the subsets are the whole grid, and the region crop the decode
    leaves behind (grib.set_region) covers every site.

    Not under _render_lock: a download here would otherwise hold up
    every request for its whole length. grib's own per-file locks keep a
    request for the same file from fetching it a second time -- it waits
    for this one to finish instead."""
    while True:
        now = datetime.now(LOCAL_TZ)
        sites = load_sites()
        if sites and now.hour in hours:
            grib.set_region(sites_region(sites))
            lat, lon = sites[0].lat, sites[0].lon
            for ahead in (0, 1):
                valid = now + timedelta(hours=ahead)
                try:
                    started = time.monotonic()
                    _, valid_time, run_time, model = Simple_Sounding.fetch_model_profile(lat, lon, valid)
                    logger.info('Pre-warmed %s %s f%02d in %.1fs', model.upper(),
                                f'{run_time:%Y-%m-%d %HZ}',
                                round((valid_time - run_time).total_seconds() / 3600),
                                time.monotonic() - started)
                except Exception:
                    logger.exception('Pre-warm for %s failed', f'{valid:%H:00}')
                finally:
                    grib.clear_field_cache()
                    _release_freed_memory()
        time.sleep(PREWARM_INTERVAL.total_seconds())


def _point_forecast_prewarm_loop(hours, days):
    """Keep every site's point forecast for the next `days` days drawn
    from the freshest runs, so a /pf is answered from tables already
    drawn rather than waiting out the fetch -- which, with each new RRFS
    run (3-hourly in the bucket) moving the rest of today onto it, most
    requests would otherwise do.

    The days and model are what a bare /pf asks for (_point_forecast_day,
    DEFAULT_MODELS); a check that finds every plan unchanged is a few
    lookups. Today first, for every site, then the day after: a run's
    files are the whole grid, so the first site of a day downloads and
    the rest mostly read what it decoded for them (prime_for). One
    site-day at a time, and none started while a request is in progress
    (REQUESTS), so a request waits behind at most the one under way --
    seconds, or up to ~1.5 min for the first of a run, which is the
    download any request for that run would have waited on anyway."""
    while True:
        now = datetime.now(LOCAL_TZ)
        if now.hour in hours:
            sites = load_sites()
            for ahead in range(days):
                for site in sites:
                    day = _point_forecast_day(site, now) + timedelta(days=ahead)
                    REQUESTS.wait_until_idle()
                    started = time.monotonic()
                    try:
                        result, _, drawn = _point_forecast_tables(site, day, None,
                                                                  prime_for=sites)
                    except point_forecast.Unavailable:
                        continue
                    except Exception:
                        logger.exception('Pre-drawing the %s point forecast for %s failed',
                                         site.name, day)
                        continue
                    if not drawn:
                        logger.info('Pre-drew the %s point forecast for %s: %s, runs %s, '
                                    '%.0f MB downloaded, in %.1fs', site.name, f'{day:%a %b %d}',
                                    result.source.label,
                                    ', '.join(f'{run:%d %HZ}' for run in result.runs),
                                    result.downloaded_bytes / 1e6, time.monotonic() - started)
        time.sleep(PREWARM_INTERVAL.total_seconds())


def _sounding_prewarm_loop(stations):
    """Draw each station's newest launch as soon as the Wyoming archive
    has it, so the first /sounding for it is a replay (~1 s) rather than
    the archive lookups, the previous launch to compare with, the
    Open-Meteo high and the plot (5-30 s).

    Asks for exactly what a bare /sounding does (the latest synoptic
    launch), so it lands under the same replay key. The archive is
    checked outside the render lock -- load_sounding caches the CSV, so
    once posted that's a file read -- and the render lock is taken only
    once there is a launch to draw: twice a day, a few seconds each."""
    while True:
        launch = latest_synoptic_time()
        for station in stations:
            try:
                wyoming.load_sounding(launch, station)
            except (ValueError, wyoming.ArchiveUnavailable):
                continue    # not posted yet, or the archive is failing: next time
            REQUESTS.wait_until_idle()
            started = time.monotonic()
            try:
                png, replayed = _render(['--station', station], request=False)
            except Exception:
                logger.exception('Pre-drawing the %s sounding failed', station)
                continue
            if not replayed:
                logger.info('Pre-drew the %s sounding for %s in %.1fs', station,
                            f'{launch:%Y-%m-%d %HZ}', time.monotonic() - started)
        time.sleep(PREWARM_INTERVAL.total_seconds())


# Membership changes, logged by ID (see metrics.who): with privacy mode on,
# a group the bot was never in, or was removed from, looks exactly like
# one whose commands it can't see, and nothing else would say which.
chats_logger = logging.getLogger('chats')
_PRESENT = {ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER, ChatMember.RESTRICTED}


async def membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The bot added to, removed from, or changed in a chat."""
    change = update.my_chat_member
    old, new = change.old_chat_member.status, change.new_chat_member.status
    if old not in _PRESENT and new in _PRESENT:
        what = 'Added to'
    elif old in _PRESENT and new not in _PRESENT:
        what = 'Removed from'
    else:
        what = 'Membership changed in'
    chats_logger.info('%s %s chat %s by user %s (%s -> %s)', what, change.chat.type,
                      change.chat.id, change.from_user.id, old, new)


async def migrated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A group upgraded to a supergroup, which gives it a new ID. Telegram
    posts this in both the old chat and the new; logged once, from the old."""
    message = update.effective_message
    if message.migrate_to_chat_id:
        chats_logger.info('Group chat %s became supergroup chat %s', message.chat.id,
                          message.migrate_to_chat_id)


async def post_init(application: Application) -> None:
    # Registers the "/" autocomplete menu Telegram clients show while
    # typing -- without this the command still works, but no hint pops
    # up. Uses the default scope, which covers both DMs and group chats
    # (the bot still needs to actually be a member of the group, and its
    # commands there may need "@YourBotUsername" if another bot in the
    # same group also defines the same command name).
    await application.bot.set_my_commands([
        BotCommand('sounding', 'Observed radiosonde: /sounding [station] [local time]'),
        BotCommand('site', 'Modeled sounding at a site, now: /site <name> [local time]'),
        BotCommand('pf', 'Point forecast, hourly lift/wind/cloud: /pf <site> [day] [model]'),
        BotCommand('sites', 'List named sites (from sites.tsv)'),
    ])


def main() -> None:
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise SystemExit('Set TELEGRAM_BOT_TOKEN in the environment first.')

    metrics.initialise([site.name for site in load_sites()], os.environ.get('METRICS_PORT'))

    # Set here as well as by the pre-warm, so a cold request's decode
    # leaves a region crop behind for the next site even with it off.
    grib.set_region(sites_region(load_sites()))
    hours = prewarm_hours(os.environ.get('SOUNDING_PREWARM'))
    if hours:
        threading.Thread(target=_prewarm_loop, args=(hours,), name='prewarm', daemon=True).start()
        logger.info('Pre-warming model runs between %02d:00 and %02d:59', hours[0], hours[-1])

    pf_hours = prewarm_hours(os.environ.get('POINT_FORECAST_PREWARM'))
    if pf_hours:
        pf_days = int(os.environ.get('POINT_FORECAST_PREWARM_DAYS') or 2)
        threading.Thread(target=_point_forecast_prewarm_loop, args=(pf_hours, pf_days),
                         name='pf-prewarm', daemon=True).start()
        logger.info('Pre-drawing point forecasts %d day(s) ahead between %02d:00 and %02d:59',
                    pf_days, pf_hours[0], pf_hours[-1])

    stations = [s.strip().upper() for s in (os.environ.get('OBSERVED_PREWARM') or '').split(',')
                if s.strip()]
    if stations:
        threading.Thread(target=_sounding_prewarm_loop, args=(stations,),
                         name='sounding-prewarm', daemon=True).start()
        logger.info('Pre-drawing the newest launch from %s', ', '.join(stations))

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler('sounding', sounding))
    app.add_handler(CommandHandler('site', site))
    app.add_handler(CommandHandler(['pf', 'pointforecast'], pointforecast))
    app.add_handler(CommandHandler('sites', list_sites))
    app.add_handler(ChatMemberHandler(membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, migrated))
    logger.info('Bot starting (polling)...')
    # Named rather than left to Telegram, which otherwise keeps whatever an
    # earlier poller asked for: messages (commands, migrations) and the
    # bot's own membership changes.
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.MY_CHAT_MEMBER])


if __name__ == '__main__':
    main()
