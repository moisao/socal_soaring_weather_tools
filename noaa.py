"""Where NOAA's GRIB2 files are (HRRR, RRFS, GFS, URMA), whether one has
been posted, and what its index says is in it -- the byte ranges that
grib.py and point_forecast.py then fetch for themselves.

AWS only: every file is looked for in NOAA's public S3 bucket for its
model and nowhere else. Herbie, which did this job before, would go on
to NOMADS, Google and Azure when AWS had no such file. A file that is on
one of those but not on AWS -- a run NOMADS has a few minutes sooner, or
all of them during an S3 outage -- now counts as not posted, and the
caller falls back to an older run or the next model as it would for any
run that isn't up yet.
"""

import re
import threading
from dataclasses import dataclass
from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Seconds allowed for one lookup or index download.
TIMEOUT = 30


def _hrrr(product, run, lead):
    return (f'https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{run:%Y%m%d}/conus/'
            f'hrrr.t{run:%H}z.wrf{product}f{lead:02d}.grib2')


def _rrfs(product, run, lead):
    # Native levels come only on the 13 km North America grid.
    grid = '13km', 'na'
    if product != 'natlev':
        grid = '3km', 'conus'
    return (f'https://noaa-rrfs-ops-pds.s3.amazonaws.com/rrfs.{run:%Y%m%d/%H}/'
            f'rrfs.t{run:%H}z.{product}.{grid[0]}.f{lead:03d}.{grid[1]}.grib2')


def _gfs(product, run, lead):
    return (f'https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{run:%Y%m%d/%H}/atmos/'
            f'gfs.t{run:%H}z.{product}.f{lead:03d}')


def _urma(product, run, lead):
    # An analysis, so there is no lead in its name.
    return (f'https://noaa-urma-pds.s3.amazonaws.com/urma2p5.{run:%Y%m%d}/'
            f'urma2p5.t{run:%H}z.2dvar{product}_ndfd.grb2_wexp')


# model -> (its URL, {product as asked for here: as its files name it}).
# 'prs' (isobaric levels) means the same thing for HRRR and RRFS, which
# name it differently; the rest are each model's own names.
_MODELS = {
    'hrrr': (_hrrr, {'prs': 'prs', 'sfc': 'sfc', 'nat': 'nat'}),
    'rrfs': (_rrfs, {'prs': 'prslev', '2d': '2dfld', 'natlev': 'natlev'}),
    'gfs': (_gfs, {'pgrb2.0p25': 'pgrb2.0p25'}),
    'urma': (_urma, {'anl': 'anl'}),
}


def grib_url(model, run, lead, product):
    """URL of one GRIB2 file: this model's run (a datetime, in UTC) at
    this lead in hours, for the product named."""
    url, products = _MODELS[model]
    return url(products[product], run, lead)


def index_url(model, run, lead, product):
    """URL of the file's index, which sits beside it."""
    return grib_url(model, run, lead, product) + '.idx'


_local = threading.local()


def _session():
    """One session per thread, so lookups after the first reuse its
    connection rather than each making their own, and callers probing
    from a pool of threads don't share one."""
    if not hasattr(_local, 'session'):
        _local.session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        _local.session.mount('https://', HTTPAdapter(max_retries=retry))
    return _local.session


def _exists(url):
    response = _session().head(url, timeout=TIMEOUT)
    # 403 is what S3 answers for a missing key in a bucket that doesn't
    # allow listing.
    if response.status_code in (403, 404):
        return False
    response.raise_for_status()
    # More than a stub: a file still being written can show up empty.
    return int(response.headers.get('Content-Length', 0)) > 10


def is_posted(model, run, lead, product, index=False):
    """Whether the file is in the bucket -- and with index=True, its
    index too, which a run being uploaded gets a little later. One
    round trip each. Raises if the bucket can't be asked."""
    return (_exists(grib_url(model, run, lead, product))
            and (not index or _exists(index_url(model, run, lead, product))))


@dataclass(frozen=True)
class Message:
    """One line of an index: a GRIB2 message and where it is in the file."""
    variable: str       # 'TMP'
    level: str          # '850 mb', '2 m above ground', 'surface'
    forecast_time: str  # 'anl', '3 hour fcst', '2-3 hour acc fcst'
    start_byte: int
    end_byte: int | None  # None for the last message: it runs to the end of the file
    # What a search pattern is matched against: the line from its
    # variable on, ':TMP:850 mb:3 hour fcst:'.
    text: str

    @property
    def byte_range(self):
        """The Range header that fetches this message alone."""
        return f'bytes={self.start_byte}-{"" if self.end_byte is None else self.end_byte}'


def parse_index(text):
    """The messages a wgrib2 index lists, in file order. Its lines are
    'number:start byte:d=run:variable:level:forecast time:...'."""
    rows = []
    for line in text.splitlines():
        fields = line.split(':')
        if len(fields) < 6:
            continue
        rows.append((int(fields[1]), fields))
    starts = sorted({start for start, _ in rows})
    # A message ends where the next one starts. The next *different*
    # start, since the fields of one multi-field message share theirs.
    following = dict(zip(starts, starts[1:]))
    return [Message(variable=fields[3], level=fields[4], forecast_time=fields[5],
                    start_byte=start,
                    end_byte=following[start] - 1 if start in following else None,
                    text=':' + ':'.join(fields[3:]))
            for start, fields in rows]


# A posted file's index never changes, so each is downloaded once.
@lru_cache(maxsize=512)
def _index(url):
    response = _session().get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return tuple(parse_index(response.text))


def inventory(model, run, lead, product, search=None):
    """The file's messages, in file order -- all of them, or those whose
    index line matches the regular expression `search` (see Message.text
    for what it is matched against). Raises requests.HTTPError if the
    index isn't there."""
    messages = _index(index_url(model, run, lead, product))
    if search is None:
        return list(messages)
    pattern = re.compile(search)
    return [message for message in messages if pattern.search(message.text)]
