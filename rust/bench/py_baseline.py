"""Step 0 of rust/PLAN.md: the current Python decode of one cached GRIB2
subset, at every site in sites.tsv, as JSON for comparing against the
Rust CLI. Timing and peak memory go to stderr.

    PYTHONPATH=. python rust/bench/py_baseline.py SUBSET.grib2 [sites.tsv] > py.json
"""
import json
import re
import resource
import sys
import time
from pathlib import Path

import grib


def main():
    grib_path = Path(sys.argv[1])
    sites_path = sys.argv[2] if len(sys.argv) > 2 else 'sites.tsv'
    sites = []
    with open(sites_path) as f:
        for line in f:
            # Same pattern as telegram_bot.load_sites.
            m = re.match(r'^(.*?)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)', line)
            if m:
                sites.append((m.group(1), float(m.group(2)), float(m.group(3))))

    start = time.perf_counter()
    lats, lons, cone, lov, values, tree = grib._load_fields(grib_path)
    decode_s = time.perf_counter() - start

    points = []
    for name, lat, lon in sites:
        _, idx = tree.query([lat, lon % 360])
        points.append({
            'name': name, 'index': int(idx),
            'lat': float(lats[idx]), 'lon': float(lons[idx]),
            'values': {key: float(arr[idx]) for key, arr in values.items()},
        })
    json.dump({'cone': cone, 'lov': lov, 'messages': len(values), 'points': points},
              sys.stdout)

    usage = resource.getrusage(resource.RUSAGE_SELF)
    print(f'{grib_path.name}: {len(values)} fields, decode+tree {decode_s:.2f}s, '
          f'user {usage.ru_utime:.2f}s sys {usage.ru_stime:.2f}s, '
          f'max RSS {usage.ru_maxrss / 1024:.0f} MB', file=sys.stderr)


if __name__ == '__main__':
    main()
