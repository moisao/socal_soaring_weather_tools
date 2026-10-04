"""Telegram bot: on-demand Skew-T soundings via Simple_Sounding.py.

Usage (in a chat with the bot):
    /sounding                      -> latest NKX sounding
    /sounding 72572                -> latest sounding for station 72572
    /sounding NKX 2025-01-01T12    -> that station's sounding at that run

    /sites                          -> list named sites (from sites.tsv)
    /site Little Black              -> latest modeled sounding at that site
    /site Little Black 2026-09-17T19 -> that site's sounding at that time

Setup:
    export TELEGRAM_BOT_TOKEN="<token from @BotFather>"
    python telegram_bot.py

Run it from this directory: Simple_Sounding is imported directly (not
shelled out to), so it has to be importable, and its cache/ lives here.

No chat ID is needed -- this replies wherever the command came from,
it doesn't push anywhere on its own.
"""

import asyncio
import contextlib
import ctypes
import ctypes.util
import io
import logging
import os
import re
import threading
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

import grib
import Simple_Sounding
from replay import ReplayIndex
from wyoming import latest_synoptic_time

logging.basicConfig(level=logging.INFO)
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

# matplotlib's pyplot is global, mutable state, so two renders must never
# overlap -- concurrent requests queue here instead. Rendering in-process
# rather than shelling out also means the caller is handed the exact
# output path back, instead of having to guess at its own output by
# picking the newest matching PNG off disk (which two overlapping
# requests for the same site could get wrong).
_render_lock = threading.Lock()


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


def load_sites():
    """Parse sites.tsv: one 'Name   lat   lon' per line, name and the two
    floats separated by runs of whitespace (the name itself may contain
    single spaces, e.g. "Little Black") -- same format/parsing as the
    batch scripts (interesting2.sh's site list, run_batch_sites.py)."""
    sites = []
    if not SITES_FILE.exists():
        return sites
    with open(SITES_FILE) as f:
        for line in f:
            line = line.rstrip('\n')
            if not line.strip():
                continue
            m = re.match(r'^(.*?)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)$', line)
            if m:
                sites.append((m.group(1), float(m.group(2)), float(m.group(3))))
    return sites


def match_site(args):
    """Match the longest possible prefix of args (joined with spaces,
    case-insensitive) against a known site name, e.g. ['Little', 'Black',
    '2026-09-17T19'] matches the two-word site "Little Black" and leaves
    ['2026-09-17T19'] over for the datetime -- tried longest-first so a
    multi-word site name isn't cut short by a plausible-looking prefix
    match. Returns ((name, lat, lon), remaining_args), or (None, args) if
    no prefix of args names a known site."""
    sites = load_sites()
    for n in range(len(args), 0, -1):
        candidate = ' '.join(args[:n]).lower()
        for name, lat, lon in sites:
            if name.lower() == candidate:
                return (name, lat, lon), args[n:]
    return None, args


class BadArguments(Exception):
    """Arguments argparse refused, carrying its own complaint as text."""


def _render(argv: list) -> Path:
    """Blocking: build arguments from argv, render, return the PNG path.

    Runs on a worker thread (see _render_and_reply) because the whole
    fetch-and-plot is synchronous and slow enough -- seconds to minutes
    on a cold GRIB2 fetch -- to stall the bot's event loop otherwise."""
    with _render_lock:
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
            return replayed
        try:
            rendered = Simple_Sounding.render(args, output_dir=OUTPUT_DIR)
        finally:
            # The decoded-field memo pays off across sites in one batch;
            # here each request is usually a different run, so it would
            # just pin ~1 GB for the life of the service.
            grib.clear_field_cache()
            _release_freed_memory()
        REPLAY.record(args, rendered)
    return Path(f'{rendered.stub}.png')


async def _render_and_reply(update: Update, argv: list, timeout: int, label: str) -> None:
    """Shared plumbing for /sounding and /site: render in a worker
    thread, then reply with the image or with whatever went wrong."""
    await update.message.reply_text(f'Fetching {label} sounding...')

    try:
        png = await asyncio.wait_for(asyncio.to_thread(_render, argv), timeout)
    except asyncio.TimeoutError:
        # Only stops waiting -- the worker thread itself can't be
        # cancelled, so it runs to completion and keeps the lock until
        # then. Further requests queue rather than failing.
        await update.message.reply_text('Timed out fetching/plotting that sounding.')
        return
    except BadArguments as e:
        await update.message.reply_text(f'Bad arguments: {e}')
        return
    except Exception as e:
        logger.exception('Sounding render failed')
        await update.message.reply_text(f'Failed: {type(e).__name__}: {e}')
        return

    if not png.exists():
        await update.message.reply_text('Ran, but no image was produced.')
        return

    with open(png, 'rb') as photo:
        await update.message.reply_photo(photo)


async def sounding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    # A named site is a flying site, not a radiosonde launch station: there
    # is no observed sounding for it, so the Wyoming lookup below would only
    # come back empty ("No recent sounding data") -- or, for a two-word name,
    # read its second word as a bad datetime. Point at /site instead,
    # keeping any datetime that was given.
    match, remaining = match_site(args)
    if match is not None:
        suggestion = ' '.join(['/site', match[0], *remaining])
        await update.message.reply_text(f'Command failed: Did you mean {suggestion}?')
        return

    station = args[0] if len(args) >= 1 else 'NKX'
    argv = ['--station', station]
    if len(args) >= 2:
        argv += ['--datetime', args[1]]

    await _render_and_reply(update, argv, timeout=120, label=station)


async def site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            'Usage: /site <name> [datetime]\nSee /sites for the list of names.')
        return

    match, remaining = match_site(args)
    if match is None:
        await update.message.reply_text(
            f'Unknown site {" ".join(args)!r}. See /sites for the list of names.')
        return
    name, lat, lon = match

    argv = ['--lat', str(lat), '--lon', str(lon), '--name', name]
    if remaining:
        argv += ['--datetime', ' '.join(remaining)]

    # A named site is a modeled (HRRR/RRFS/GFS) sounding, not the fast
    # Wyoming CSV archive /sounding uses -- a cold fetch (no cached GRIB2
    # run yet) can take several minutes, so this gets a much longer
    # timeout than /sounding's.
    await _render_and_reply(update, argv, timeout=600, label=name)


async def list_sites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sites = load_sites()
    if not sites:
        await update.message.reply_text('No named sites configured (sites.tsv not found or empty).')
        return
    lines = [name for name, lat, lon in sites]
    await update.message.reply_text('Named sites:\n' + '\n'.join(lines))


async def post_init(application: Application) -> None:
    # Registers the "/" autocomplete menu Telegram clients show while
    # typing -- without this the command still works, but no hint pops
    # up. Uses the default scope, which covers both DMs and group chats
    # (the bot still needs to actually be a member of the group, and its
    # commands there may need "@YourBotUsername" if another bot in the
    # same group also defines the same command name).
    await application.bot.set_my_commands([
        BotCommand('sounding', 'Observed radiosonde: /sounding [station] [datetime]'),
        BotCommand('site', 'Modeled sounding at a site: /site <name> [datetime]'),
        BotCommand('sites', 'List named sites (from sites.tsv)'),
    ])


def main() -> None:
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise SystemExit('Set TELEGRAM_BOT_TOKEN in the environment first.')

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler('sounding', sounding))
    app.add_handler(CommandHandler('site', site))
    app.add_handler(CommandHandler('sites', list_sites))
    logger.info('Bot starting (polling)...')
    app.run_polling()


if __name__ == '__main__':
    main()
