"""Check grib-profile against eccodes on real files: for every site, the
same grid point (found here by brute force over eccodes' own lat/lon
arrays, not by projection) and the same value in every message, to the
last bit of a float32.

    python rust/check_against_eccodes.py SUBSET.grib2... [--sites sites.tsv]

With --by-position the messages are asked for by where they are in the
file (grib-profile's --messages, as point_forecast.py uses it) rather
than by name, and compared to the last bit of the double they unpack to.
That is the way to check a point forecast's files (*.point.grib2), whose
fields grib-profile mostly has no name for, GFS's among them.

Exits non-zero on any difference. Run from the repo root, with
grib-profile built (`cargo build --release` in rust/) or on PATH.
"""
import argparse
import json
import re
import subprocess
import sys

import eccodes
import numpy as np

import grib


def read_sites(path):
    sites = []
    with open(path) as f:
        for line in f:
            # Same pattern as telegram_bot.load_sites.
            m = re.match(r'^(.*?)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)', line)
            if m:
                sites.append((m.group(1), float(m.group(2)), float(m.group(3))))
    return sites


def eccodes_values(grib_path, sites, by_position=False):
    """(indices, {shortName_level: float32 values at those indices}),
    decoding one message at a time. With by_position, the keys are the
    messages' positions in the file and the values are left as doubles."""
    indices = None
    values = {}
    with open(grib_path, 'rb') as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            if indices is None:
                lats = np.radians(eccodes.codes_get_array(gid, 'latitudes'))
                lons = np.radians(eccodes.codes_get_array(gid, 'longitudes'))
                indices = []
                for _, lat, lon in sites:
                    lat, lon = np.radians(lat), np.radians(lon)
                    haversine = (np.sin((lats - lat) / 2) ** 2
                                 + np.cos(lat) * np.cos(lats) * np.sin((lons - lon) / 2) ** 2)
                    indices.append(int(np.argmin(haversine)))
                del lats, lons
            field = eccodes.codes_get_array(gid, 'values')[indices]
            if by_position:
                values[len(values)] = field
            else:
                key = f'{eccodes.codes_get(gid, "shortName")}_{eccodes.codes_get(gid, "level")}'
                values[key] = field.astype(np.float32)
            eccodes.codes_release(gid)
    return indices, values


def check(grib_path, sites, by_position=False):
    indices, values = eccodes_values(grib_path, sites, by_position)
    command = [grib.grib_profile_binary(), grib_path]
    if by_position:
        command += ['--messages', ','.join(map(str, values))]
    for _, lat, lon in sites:
        command += ['--point', f'{lat!r},{lon!r}']
    decoded = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)

    problems = []
    compared = 0
    for n, ((name, _, _), point) in enumerate(zip(sites, decoded['points'], strict=True)):
        if point['index'] != indices[n]:
            problems.append(f'{name}: grid index eccodes {indices[n]}, grib-profile {point["index"]}')
            continue
        if by_position:
            # The same doubles, so compared as they are.
            found = dict(enumerate(np.array(point['values'], dtype=float)))
        else:
            found = {key: np.float32(value) for key, value in point['values'].items()}
        if found.keys() != values.keys():
            problems.append(f'{name}: keys differ: {sorted(found.keys() ^ values.keys())}')
            continue
        for key, value in found.items():
            compared += 1
            if value != values[key][n]:
                problems.append(f'{name} {key}: eccodes {values[key][n]!r}, grib-profile {value!r}')

    print(f'{grib_path}: {len(sites)} sites, {compared} values compared, {len(problems)} differences')
    for problem in problems[:20]:
        print('  ' + problem)
    return not problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('subsets', nargs='+', metavar='SUBSET.grib2')
    parser.add_argument('--sites', default='sites.tsv')
    parser.add_argument('--by-position', action='store_true',
                        help='Ask for messages by position, and compare doubles')
    args = parser.parse_args()
    sites = read_sites(args.sites)
    # A list, not a generator: check every file even after one fails.
    return 0 if all([check(path, sites, args.by_position) for path in args.subsets]) else 1


if __name__ == '__main__':
    sys.exit(main())
