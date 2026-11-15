"""Compare py_baseline.py's JSON with grib-profile's for one subset: same
grid point for every site, and every value equal as float32.

    python rust/bench/compare.py py.json rust.json
"""
import json
import sys

import numpy as np


def main():
    with open(sys.argv[1]) as f:
        py = json.load(f)
    with open(sys.argv[2]) as f:
        rs = json.load(f)

    problems = []
    for key in ('cone', 'lov', 'messages'):
        same = (py[key] == rs[key] if None in (py[key], rs[key])
                else abs(py[key] - rs[key]) < 1e-12)
        if not same:
            problems.append(f'{key}: python {py[key]}, rust {rs[key]}')

    compared = 0
    worst_latlon = 0.0
    for p, r in zip(py['points'], rs['points'], strict=True):
        name = p['name']
        if p['index'] != r['index']:
            problems.append(f'{name}: grid index python {p["index"]}, rust {r["index"]}')
            continue
        worst_latlon = max(worst_latlon, abs(p['lat'] - r['lat']), abs(p['lon'] - r['lon']))
        if p['values'].keys() != r['values'].keys():
            problems.append(f'{name}: keys differ: {sorted(p["values"].keys() ^ r["values"].keys())}')
            continue
        for key, value in p['values'].items():
            compared += 1
            if np.float32(value) != np.float32(r['values'][key]):
                problems.append(f'{name} {key}: python {value!r}, rust {r["values"][key]!r}')

    print(f'{len(py["points"])} sites, {compared} values compared, '
          f'{len(problems)} differences; grid point lat/lon differ by at most {worst_latlon:.2g} deg')
    for problem in problems[:20]:
        print('  ' + problem)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
