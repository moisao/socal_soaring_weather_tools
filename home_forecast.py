"""Home forecast: the next 24 hours at one point, as JSON over HTTP, for
Home Assistant (or anything else on the LAN).

The weather a household plans around rather than a pilot: temperature,
humidity, wind and gusts, rain, cloud cover, a UV index, and the sun's
rise and set. The hours come from the same RRFS (then HRRR) runs, the
same run-and-lead choice and the same cached GRIB2 messages as a point
forecast (point_forecast.HourFile), so run beside the bot on its cache
the daylight hours the bot has already pre-drawn cost no download; only
the night hours and the few fields the bot doesn't need (total cloud,
solar radiation) are fetched.

    python home_forecast.py --lat 32.95 --lon -117.10     # print once
    python home_forecast.py serve --port 8080             # HTTP server

    GET /forecast?lat=32.95&lon=-117.10
    GET /healthz

A point is forecast on its first request -- a few hundred MB of whole-grid
messages for a new run, so that request is answered 503 "warming" if it
takes longer than a few seconds -- and from then on kept current in the
background as new runs post (and across restarts: the points asked for
are remembered in the cache directory). A point not asked for in a week
is dropped.
"""

import argparse
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import point_forecast as pf
from config import CACHE_DIR

logger = logging.getLogger('home_forecast')

HOURS = 24

# Fields every hour needs. All but total cloud are ones the bot's point
# forecast fetches anyway (point_forecast.SURFACE_LABELS).
TEMPERATURE = 'TMP:2 m above ground'
DEWPOINT = 'DPT:2 m above ground'
U10, V10 = 'UGRD:10 m above ground', 'VGRD:10 m above ground'
GUST = 'GUST:surface'
RAIN = pf.RAIN_LABEL
TOTAL_CLOUD = 'TCDC:entire atmosphere (considered as a single layer)'
HOURLY_LABELS = (TEMPERATURE, DEWPOINT, U10, V10, GUST, RAIN, TOTAL_CLOUD)
# Only while the sun is up, for the UV index: the shortwave reaching the
# ground, and what would reach it under a clear sky.
SOLAR = 'DSWRF:surface'
CLEAR_SKY_SOLAR = 'CSDSF:surface'
DAYLIGHT_LABELS = (SOLAR, CLEAR_SKY_SOLAR)

KT_TO_MS = 1 / pf.MS_TO_KT

# How often the background loop checks whether a newer run has posted.
# A check that finds nothing new is a handful of ~1 s lookups.
REFRESH_INTERVAL = timedelta(minutes=5)
FORGET_AFTER = timedelta(days=7)
POINTS_FILE = CACHE_DIR / 'home_forecast_points.json'


###########################################
# The sun.

def _solar_terms(t):
    """(equation of time, minutes; declination, radians) at a UTC time:
    NOAA's low-accuracy series, as point_forecast.sun_times uses."""
    hour = t.hour + t.minute / 60
    gamma = 2 * math.pi / 365 * (t.timetuple().tm_yday - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    return eqtime, decl


def cos_zenith(lat, lon, t):
    """Cosine of the sun's zenith angle at (lat, lon) at UTC time t;
    negative with the sun below the horizon."""
    t = t.astimezone(timezone.utc)
    eqtime, decl = _solar_terms(t)
    true_solar_minutes = t.hour * 60 + t.minute + t.second / 60 + eqtime + 4 * lon
    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    phi = math.radians(lat)
    return math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)


def uv_index(mu, solar=None, clear_sky_solar=None, cloud_pct=None):
    """UV index from the sun's height and the cloud.

    Clear sky: 12.5 mu^2.42 (Madronich 2007, for typical mid-latitude
    ozone), mu being the cosine of the zenith angle. Cloud scales that by
    how much of the clear-sky shortwave the model lets through
    (solar / clear_sky_solar), or, without those, by Kasten & Czeplak's
    factor for the total cloud cover. Cloud dims UV less than it dims
    the whole shortwave, hence the ratio to the 0.6: on 2026-11-14 at
    Little Black, under 57 % cloud letting 11 % of the shortwave
    through, the plain ratio gave UV 0.3 where Open-Meteo had 0.85;
    0.11^0.6 gives 0.9. In sun it is the clear-sky value regardless."""
    if mu <= 0:
        return 0.
    clear = 12.5 * mu ** 2.42
    if solar is not None and clear_sky_solar is not None and clear_sky_solar > 20:
        factor = max(solar / clear_sky_solar, 0.) ** 0.6
    elif cloud_pct is not None:
        factor = 1 - 0.75 * (cloud_pct / 100) ** 3.4
    else:
        factor = 1.
    return clear * min(max(factor, 0.), 1.)


###########################################
# One hour.

def relative_humidity(t_c, td_c):
    """%, from temperature and dewpoint (Magnus, over water)."""
    def es(t):
        return math.exp(17.625 * t / (t + 243.04))
    return min(100., max(0., 100 * es(td_c) / es(t_c)))


def condition(rain_mm, cloud_pct, wind_ms, daylight):
    """Home Assistant's weather condition for an hour
    (https://developers.home-assistant.io/docs/core/entity/weather/#recommended-values-for-state-and-condition)."""
    if rain_mm is not None and rain_mm >= 4:
        return 'pouring'
    if rain_mm is not None and rain_mm >= 0.2:
        return 'rainy'
    cloud_pct = cloud_pct or 0
    if wind_ms is not None and wind_ms >= 10.8:  # Beaufort 6, a strong breeze
        return 'windy-variant' if cloud_pct > 75 else 'windy'
    if cloud_pct > 75:
        return 'cloudy'
    if cloud_pct > 25:
        return 'partlycloudy'
    return 'sunny' if daylight else 'clear-night'


def _round(value, digits=1):
    return None if value is None else round(value, digits)


def _hour(valid, run, point, lat, lon, tz):
    t_c = point[TEMPERATURE] - 273.15
    td_c = point[DEWPOINT] - 273.15
    speed_kt, bearing = pf._earth_wind(point, point[U10], point[V10])
    wind_ms = speed_kt * KT_TO_MS
    cloud = point.get(TOTAL_CLOUD)
    rain = point.get(RAIN)
    mu = cos_zenith(lat, lon, valid)
    return {
        'time': valid.astimezone(tz).isoformat(),
        'run': run.isoformat(),
        'temperature_c': _round(t_c),
        'dew_point_c': _round(td_c),
        'humidity': _round(relative_humidity(t_c, td_c), 0),
        'wind_speed_ms': _round(wind_ms),
        'wind_gust_ms': _round(point.get(GUST)),
        'wind_bearing': _round(bearing, 0),
        'precipitation_mm': _round(rain, 2),
        'cloud_coverage': _round(cloud, 0),
        'uv_index': _round(uv_index(mu, point.get(SOLAR), point.get(CLEAR_SKY_SOLAR), cloud)),
        'daylight': mu > 0,
        'condition': condition(rain, cloud, wind_ms, mu > 0),
    }


###########################################
# The next 24 hours.

def valid_times(now=None):
    """The current whole hour and the 23 after it, UTC."""
    start = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = start.replace(minute=0, second=0, microsecond=0)
    return [start + timedelta(hours=h) for h in range(HOURS)]


def plan(models=None, now=None):
    """(source, ((valid, (run, lead)), ...)) for the next 24 hours: the
    first model that has every hour, else whichever has the most -- one
    model throughout, as point_forecast.plan_day does for a day."""
    times = valid_times(now)
    best, errors = None, []
    for name in models or pf.DEFAULT_MODELS:
        try:
            resolved = pf._resolve(pf.SOURCES[name], times)
        except Exception as e:
            errors.append(f'{name.upper()}: {e}')
            continue
        if best is None or len(resolved) > len(best[1]):
            best = (pf.SOURCES[name], tuple(sorted(resolved.items())))
        if len(resolved) == len(times):
            break
    if not best or not best[1]:
        raise pf.Unavailable('No model has the next 24 hours' +
                             (': ' + '; '.join(errors) if errors else ''))
    return best


def _sun_events(lat, lon, tz, now):
    """Today's and tomorrow's sunrise and sunset, local time, and the
    next of each still to come."""
    today = now.astimezone(tz).date()
    events = {}
    for name, day in (('today', today), ('tomorrow', today + timedelta(days=1))):
        sun = pf.sun_times(lat, lon, day)
        events[name] = sun and [t.astimezone(tz) for t in sun]
    rises = [events[d][0] for d in ('today', 'tomorrow') if events[d] and events[d][0] > now]
    sets = [events[d][1] for d in ('today', 'tomorrow') if events[d] and events[d][1] > now]

    def iso(t):
        return t.isoformat() if t else None

    return {
        'sunrise': iso(events['today'] and events['today'][0]),
        'sunset': iso(events['today'] and events['today'][1]),
        'sunrise_tomorrow': iso(events['tomorrow'] and events['tomorrow'][0]),
        'sunset_tomorrow': iso(events['tomorrow'] and events['tomorrow'][1]),
        'next_sunrise': iso(rises[0] if rises else None),
        'next_sunset': iso(sets[0] if sets else None),
    }


def forecast(lat, lon, tz, models=None, the_plan=None):
    """The next 24 hours at (lat, lon) as a JSON-ready dict."""
    now = datetime.now(timezone.utc)
    source, resolved = the_plan or plan(models, now)
    session = pf._session()
    downloaded = 0
    hours = []
    with ThreadPoolExecutor(max_workers=pf.FETCH_WORKERS) as pool:
        for valid, (run, lead) in resolved:
            labels = set(HOURLY_LABELS)
            if cos_zenith(lat, lon, valid) > 0:
                labels |= set(DAYLIGHT_LABELS)
            hour_file = pf.HourFile(source, run, lead)
            downloaded += hour_file.ensure(labels, session, pool)
            point = hour_file.decode(lat, lon, labels)
            missing = [label for label in (TEMPERATURE, DEWPOINT, U10, V10) if label not in point]
            if missing:
                logger.warning('%s %s f%02d lacks %s; hour left out', source.model.upper(),
                               f'{run:%Y-%m-%d %HZ}', lead, missing)
                continue
            hours.append(_hour(valid, run, point, lat, lon, tz))
            grid = point['_lat'], point['_lon']
    if not hours:
        raise pf.Unavailable('No hour of the forecast could be read')
    return {
        'model': source.label,
        'generated': now.astimezone(tz).isoformat(),
        'runs': sorted({run.isoformat() for _, (run, _) in resolved}),
        'latitude': lat,
        'longitude': lon,
        'grid_latitude': round(grid[0], 4),
        'grid_longitude': round(grid[1], 4),
        'downloaded_mb': round(downloaded / 1e6, 1),
        **_sun_events(lat, lon, tz, now),
        'hours': hours,
    }


###########################################
# The service.

def _key(lat, lon):
    # ~100 m: every request from one house lands on one entry, and on one
    # 3 km grid cell regardless.
    return f'{lat:.3f},{lon:.3f}'


class Forecaster:
    """Forecasts by point, kept current in the background. One fetch at a
    time: they share the same whole-grid messages, so a second at once
    would only wait on the first's downloads."""

    def __init__(self, tz, models=None):
        self.tz, self.models = tz, models
        self._fetch_lock = threading.Lock()
        self._guard = threading.Lock()
        self._results = {}        # key -> (plan, forecast dict)
        self._errors = {}         # key -> last error message
        self._pending = set()
        self._points = self._load_points()  # key -> {'lat', 'lon', 'asked'}

    def _load_points(self):
        try:
            return json.loads(POINTS_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _save_points(self):
        tmp = POINTS_FILE.with_suffix('.json.part')
        tmp.write_text(json.dumps(self._points))
        os.replace(tmp, POINTS_FILE)

    def _update(self, key, force=False):
        point = self._points[key]
        try:
            with self._fetch_lock:
                the_plan = plan(self.models)
                current = self._results.get(key)
                if current and current[0] == the_plan and not force:
                    return
                started = time.monotonic()
                result = forecast(point['lat'], point['lon'], self.tz, the_plan=the_plan)
            with self._guard:
                self._results[key] = (the_plan, result)
                self._errors.pop(key, None)
            logger.info('Forecast %s: %s, runs %s, %.0f MB downloaded, in %.1fs', key,
                        result['model'], ', '.join(result['runs']), result['downloaded_mb'],
                        time.monotonic() - started)
        except Exception as e:
            logger.exception('Forecast %s failed', key)
            with self._guard:
                self._errors[key] = str(e)
        finally:
            with self._guard:
                self._pending.discard(key)

    def _start(self, key):
        with self._guard:
            if key in self._pending:
                return
            self._pending.add(key)
        threading.Thread(target=self._update, args=(key,), daemon=True).start()

    def get(self, lat, lon, wait=20.):
        """(forecast dict or None, error or None). Starts a first forecast
        of a new point and waits up to `wait` seconds for it."""
        key = _key(lat, lon)
        with self._guard:
            known = key in self._points
            self._points[key] = {'lat': round(lat, 3), 'lon': round(lon, 3),
                                 'asked': datetime.now(timezone.utc).isoformat()}
            if not known:
                self._save_points()
            current = self._results.get(key)
        if current:
            return current[1], None
        self._start(key)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            with self._guard:
                if key in self._results:
                    return self._results[key][1], None
                if key not in self._pending:
                    return None, self._errors.get(key, 'failed')
            time.sleep(0.5)
        return None, None

    def refresh_loop(self):
        while True:
            cutoff = datetime.now(timezone.utc) - FORGET_AFTER
            with self._guard:
                stale = [k for k, p in self._points.items()
                         if datetime.fromisoformat(p['asked']) < cutoff]
                for k in stale:
                    del self._points[k]
                    self._results.pop(k, None)
                if stale:
                    self._save_points()
                keys = list(self._points)
            for key in keys:
                self._update(key)
            time.sleep(REFRESH_INTERVAL.total_seconds())


def _handler(forecaster):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, headers=()):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == '/healthz':
                return self._send(200, {'status': 'ok'})
            if url.path != '/forecast':
                return self._send(404, {'error': 'not found'})
            query = parse_qs(url.query)
            try:
                lat, lon = float(query['lat'][0]), float(query['lon'][0])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except (KeyError, ValueError, IndexError):
                return self._send(400, {'error': 'lat and lon are required, in degrees'})
            result, error = forecaster.get(lat, lon)
            if result is not None:
                return self._send(200, result)
            if error is None:
                return self._send(503, {'status': 'warming', 'error': 'First forecast for this '
                                        'point is still downloading; try again shortly'},
                                  [('Retry-After', '60')])
            return self._send(503, {'status': 'error', 'error': error})

        def log_message(self, fmt, *args):
            logger.debug('%s ' + fmt, self.address_string(), *args)

    return Handler


def serve(port, tz, models=None):
    forecaster = Forecaster(tz, models)
    threading.Thread(target=forecaster.refresh_loop, daemon=True).start()
    server = ThreadingHTTPServer(('', port), _handler(forecaster))
    logger.info('Serving the home forecast on :%d (%d remembered points)', port,
                len(forecaster._points))
    server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description='The next 24 hours at a point, as JSON.')
    parser.add_argument('command', nargs='?', choices=('print', 'serve'), default='print')
    parser.add_argument('--lat', type=float)
    parser.add_argument('--lon', type=float)
    parser.add_argument('--port', type=int, default=int(os.environ.get('HOME_FORECAST_PORT', 8080)))
    parser.add_argument('--timezone', default=os.environ.get('SOUNDING_TIMEZONE')
                        or os.environ.get('TZ') or 'America/Los_Angeles')
    parser.add_argument('--model', choices=sorted(pf.SOURCES), default=None,
                        help='This model only, instead of ' + ' then '.join(pf.DEFAULT_MODELS))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
    tz = ZoneInfo(args.timezone)
    models = [args.model] if args.model else None
    if args.command == 'serve':
        serve(args.port, tz, models)
        return
    if args.lat is None or args.lon is None:
        parser.error('--lat and --lon are required to print a forecast')
    print(json.dumps(forecast(args.lat, args.lon, tz, models), indent=2))


if __name__ == '__main__':
    main()
