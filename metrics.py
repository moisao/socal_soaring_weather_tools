"""Command logging and Prometheus metrics for the Telegram bot.

Every command handler is wrapped in @tracked: it logs the command as it
arrives and again with its outcome and how long the reply took, and
counts both for Prometheus. A handler says how it went through
current() -- current().done('bad_request', ...) on an early return,
current().ask(site=..., model=..., day=...) for what was asked for --
and anything it doesn't mark explicitly counts as 'ok', or 'error' if
it raised.

Served on METRICS_PORT when that is set (see telegram_bot.main); with it
unset nothing listens, so a local run doesn't claim a port.
"""

import contextvars
import functools
import logging
import re
import time
from datetime import datetime

from prometheus_client import Counter, Histogram, disable_created_metrics, start_http_server

logger = logging.getLogger('commands')

# Outcomes a command can end in. Kept to these so each one's series can
# be created up front (see initialise).
OUTCOMES = ('ok', 'bad_request', 'unavailable', 'timeout', 'error')
COMMANDS = ('sounding', 'site', 'pf', 'sites')
MODELS = ('rrfs', 'hrrr', 'gfs', 'observed')
# Days from today, in the bot's zone. Bucketed so the label stays small.
DAYS = ('past', '0', '1', '2', '3', '4', '5', '6', '7+')

COMMANDS_TOTAL = Counter(
    'soaring_bot_commands', 'Commands handled, by outcome.', ['command', 'outcome'])
RESPONSE_SECONDS = Histogram(
    'soaring_bot_response_seconds',
    'From a command arriving to its final reply being sent.', ['command', 'outcome'],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1200, 1800))
REQUESTS_TOTAL = Counter(
    'soaring_bot_requests',
    'Successful commands, by what they were for: site (or station), the model that '
    'answered, and how many days ahead.', ['command', 'site', 'model', 'day'])

_current = contextvars.ContextVar('command')


def initialise(sites, port=None):
    """Create every expected series at zero, then serve /metrics.

    Up front because PromQL's increase() can't see a series' first
    increment -- a counter that appears already at 1 has risen from
    nothing -- and at a few commands a day most of them would be exactly
    that. Created again on every start, a restart reads as a reset to 0
    rather than as series going missing.

    The *_created series prometheus_client adds beside every counter are
    turned off: nothing here reads them, and they would double the
    count."""
    disable_created_metrics()
    for command in COMMANDS:
        for outcome in OUTCOMES:
            COMMANDS_TOTAL.labels(command, outcome)
            RESPONSE_SECONDS.labels(command, outcome)
    for day in DAYS:
        REQUESTS_TOTAL.labels('sounding', 'NKX', 'observed', day)
        for name in sites:
            for model in MODELS[:3]:
                REQUESTS_TOTAL.labels('site', name, model, day)
                REQUESTS_TOTAL.labels('pf', name, model, day)
    REQUESTS_TOTAL.labels('sites', '', '', '')
    if port:
        start_http_server(int(port))
        logging.getLogger(__name__).info('Serving metrics on :%s/metrics', port)


def day_label(day, today):
    """DAYS bucket for a date (or datetime) relative to today."""
    if isinstance(day, datetime):
        day = day.date()
    ahead = (day - today).days
    if ahead < 0:
        return 'past'
    return str(ahead) if ahead < 7 else '7+'


def station_label(station):
    """A station id as typed, folded to 'other' unless it looks like one,
    so free text can't mint new series."""
    station = station.upper()
    return station if re.fullmatch(r'[A-Z0-9]{3,5}', station) else 'other'


class Command:
    """What one command asked for and how it ended."""

    def __init__(self, name, text):
        self.name, self.text = name, text
        self.outcome = None
        self.detail = ''
        self.params = {'site': '', 'model': '', 'day': ''}

    def ask(self, **params):
        self.params.update({k: v for k, v in params.items() if v is not None})

    def done(self, outcome, detail=''):
        assert outcome in OUTCOMES, outcome
        self.outcome, self.detail = outcome, detail


def current():
    """The command being handled. Outside a tracked handler it's a
    throwaway, so a helper can always call it."""
    return _current.get(None) or Command('', '')


def _who(update):
    user = update.effective_user
    chat = update.effective_chat
    who = f'@{user.username}' if user and user.username else f'user {user.id}' if user else '?'
    return f'{who} (chat {chat.id})' if chat else who


def tracked(name):
    """Decorator for a command handler: log it, time it, count it."""
    def decorate(handler):
        @functools.wraps(handler)
        async def wrapper(update, context):
            invoked = (update.message.text or '').split()[0] if update.message else f'/{name}'
            text = ' '.join([invoked, *(context.args or [])])
            command = Command(name, text)
            token = _current.set(command)
            logger.info('%s from %s', text, _who(update))
            started = time.monotonic()
            try:
                await handler(update, context)
            except Exception as e:
                command.done('error', f'{type(e).__name__}: {e}')
                raise
            finally:
                elapsed = time.monotonic() - started
                outcome = command.outcome or 'ok'
                COMMANDS_TOTAL.labels(name, outcome).inc()
                RESPONSE_SECONDS.labels(name, outcome).observe(elapsed)
                if outcome == 'ok':
                    p = command.params
                    REQUESTS_TOTAL.labels(name, p['site'], p['model'], p['day']).inc()
                params = ', '.join(f'{k}={v}' for k, v in command.params.items() if v)
                logger.info('%s -> %s in %.1fs%s%s', text, outcome, elapsed,
                            f' [{params}]' if params else '',
                            f': {command.detail}' if command.detail else '')
                _current.reset(token)
        return wrapper
    return decorate
