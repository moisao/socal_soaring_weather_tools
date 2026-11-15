"""Step 1 of rust/PLAN.md: the same decode as py_baseline.py, still with
eccodes, but one message at a time -- take the wanted points, drop the
array -- and with the grid point found by inverting the Lambert Conformal
projection rather than a KD-tree over lat/lon arrays. Same JSON on
stdout, timing and peak memory on stderr.

    python rust/bench/py_streaming.py SUBSET.grib2 [sites.tsv] > streaming.json
"""
import json
import math
import re
import resource
import sys
import time

import eccodes
import numpy as np


class LambertGrid:
    """Grid point <-> lat/lon for a GRIB2 Lambert Conformal grid on a
    spherical earth (what HRRR, RRFS and URMA use)."""

    def __init__(self, gid):
        get = lambda key: eccodes.codes_get(gid, key)
        if get('gridType') != 'lambert' or get('scanningMode') != 64:
            raise ValueError('unsupported grid')
        self.nx, self.ny = get('Nx'), get('Ny')
        self.dx, self.dy = get('DxInMetres'), get('DyInMetres')
        self.lov = get('LoVInDegrees')
        latin1, latin2 = get('Latin1InDegrees'), get('Latin2InDegrees')
        if abs(latin1 - latin2) < 1e-6:
            self.cone = math.sin(math.radians(latin1))
        else:
            self.cone = (math.log(math.cos(math.radians(latin1)) / math.cos(math.radians(latin2)))
                         / math.log(math.tan(math.radians(45 - latin1 / 2))
                                    / math.tan(math.radians(45 - latin2 / 2))))
        self.rf = (get('radius') * math.cos(math.radians(latin1))
                   / (self.cone * math.tan(math.radians(45 - latin1 / 2)) ** self.cone))
        self.x0, self.y0 = self._project(get('latitudeOfFirstGridPointInDegrees'),
                                         get('longitudeOfFirstGridPointInDegrees'))

    def _project(self, lat, lon):
        rho = self.rf * math.tan(math.radians(45 - lat / 2)) ** self.cone
        theta = self.cone * math.radians((lon - self.lov + 180) % 360 - 180)
        return rho * math.sin(theta), -rho * math.cos(theta)

    def lat_lon(self, i, j):
        x, y = self.x0 + i * self.dx, self.y0 + j * self.dy
        rho = math.copysign(math.hypot(x, y), self.cone)
        lat = 90 - 2 * math.degrees(math.atan((rho / self.rf) ** (1 / self.cone)))
        return lat, (self.lov + math.degrees(math.atan2(x, -y) / self.cone)) % 360

    def nearest(self, lat, lon):
        """(index, lat, lon) of the grid point the KD-tree in grib.py
        would pick: the projection finds the neighbourhood, then the 3x3
        points around it are compared in float32 degrees as the tree
        compares them."""
        x, y = self._project(lat, lon)
        ci, cj = round((x - self.x0) / self.dx), round((y - self.y0) / self.dy)
        if not (0 <= ci < self.nx and 0 <= cj < self.ny):
            raise ValueError(f'({lat}, {lon}) is off the grid')
        best = None
        for j in range(max(cj - 1, 0), min(cj + 2, self.ny)):
            for i in range(max(ci - 1, 0), min(ci + 2, self.nx)):
                glat, glon = (float(np.float32(v)) for v in self.lat_lon(i, j))
                distance = math.hypot(glat - lat, glon - lon % 360)
                if best is None or distance < best[0]:
                    best = (distance, j * self.nx + i, glat, glon)
        return best[1:]


def main():
    grib_path = sys.argv[1]
    sites_path = sys.argv[2] if len(sys.argv) > 2 else 'sites.tsv'
    sites = []
    with open(sites_path) as f:
        for line in f:
            # Same pattern as telegram_bot.load_sites.
            m = re.match(r'^(.*?)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)', line)
            if m:
                sites.append((m.group(1), float(m.group(2)), float(m.group(3))))

    start = time.perf_counter()
    grid = None
    has_wind = False
    values = {}
    with open(grib_path, 'rb') as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            if grid is None:
                grid = LambertGrid(gid)
                nearest = [grid.nearest(lat, lon) for _, lat, lon in sites]
                indices = np.array([n[0] for n in nearest])
            short_name = eccodes.codes_get(gid, 'shortName')
            has_wind = has_wind or short_name in ('u', 'v', '10u', '10v')
            key = f'{short_name}_{eccodes.codes_get(gid, "level")}'
            values[key] = eccodes.codes_get_array(gid, 'values')[indices].astype(np.float32)
            eccodes.codes_release(gid)
    decode_s = time.perf_counter() - start

    points = [{'name': name, 'index': int(index), 'lat': lat, 'lon': lon,
               'values': {key: float(arr[n]) for key, arr in values.items()}}
              for n, ((name, _, _), (index, lat, lon)) in enumerate(zip(sites, nearest))]
    json.dump({'cone': grid.cone if has_wind else None, 'lov': grid.lov if has_wind else None,
               'messages': len(values), 'points': points}, sys.stdout)

    usage = resource.getrusage(resource.RUSAGE_SELF)
    print(f'{grib_path.rsplit("/", 1)[-1]}: {len(values)} fields, decode {decode_s:.2f}s, '
          f'user {usage.ru_utime:.2f}s sys {usage.ru_stime:.2f}s, '
          f'max RSS {usage.ru_maxrss / 1024:.0f} MB', file=sys.stderr)


if __name__ == '__main__':
    main()
