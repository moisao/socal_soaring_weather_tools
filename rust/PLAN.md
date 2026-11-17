# Rust GRIB path: plan and handoff

Status as of 2026-11-17: `grib.py` and `point_forecast.py` both decode
through `grib-profile` (this directory), and nothing in the bot calls
eccodes. The eccodes decode, the region crops and the field cache are
gone from `grib.py`, and the grid point is now the nearest over the ground.
This file is the handoff for any agent continuing the work. Update it as you
go.

## Goal

Find the most CPU- and memory-efficient way to run the bot's GRIB path
(`grib.py`: fetch HRRR/RRFS/URMA subsets from NOAA S3, decode, extract a
profile at a point), measured against the Python it replaces.

Decisions (agreed with the user):
- No full rewrite. The MetPy thermodynamics and the matplotlib plots stay in
  Python.
- Follow whichever path measures as most efficient, not "Rust for its own
  sake".
- Accuracy over matching the old output: use the grid point truly nearest
  the site.

## What is here

- `grib-profile SUBSET.grib2 (--point LAT,LON | --sites sites.tsv)...
  [--messages N,N...] [--threads N]` prints JSON: `cone`, `lov` (null on
  a latitude/longitude grid), and per point the grid index, the grid
  point's lat/lon and `{shortName_level: value}` with eccodes' names
  (`t_850`, `10u_10`, `sp_0`, `orog_0`...), as float32. With `--messages`
  the values are instead a list, one per message position asked for
  (from 0), as the doubles they unpack to, and the file is read no
  further than the last of them. No dependencies beyond std.
  - `src/grib2.rs`: a decoder for exactly what these subsets contain (one
    field per message, no bitmap, data representation templates 5.0 and
    5.3). It decodes straight to the wanted points, stops at the last one,
    and allocates nothing per value. It scales values the way eccodes does
    (in f64, then narrowed), which is what makes the output bit-identical.
  - `src/lambert.rs`: the grid point from inverting the Lambert Conformal
    projection in Section 3, then the 3x3 points around it compared by
    great-circle distance.
  - `src/latlon.rs`: the same for a regular latitude/longitude grid
    (template 3.0, GFS), by rounding, with the wrap at the first meridian.
  - `src/grid.rs`: the two behind one type, picked by Section 3's
    template.
  - `src/main.rs`: the CLI. Messages are decoded on a few threads, one
    message in memory per thread.
- `grib._decode_grib_fields` runs it once per subset and point.
  `grib.decode_messages` runs it with `--messages` for
  `point_forecast.HourFile.decode_points`: once per hour file, for all
  the points and labels not yet in the sidecar.
  `grib.grib_profile_binary()` finds it: `$SOUNDING_GRIB_PROFILE`, then
  `PATH` (the Docker image builds it in its own stage and installs it in
  `/usr/local/bin`), then this checkout's `cargo build --release`.
- `check_against_eccodes.py`: runs the binary and eccodes on the same files
  and sites and compares grid index and every value as float32. The index
  on the eccodes side is brute force over eccodes' own lat/lon arrays, so it
  checks the projection too. `--by-position` does the same through
  `--messages`, comparing doubles; use it for `*.point.grib2` files.
- `cargo test`: the projection against known HRRR grid points, the
  latitude/longitude grid, and the decoder against synthetic 5.0 and 5.3
  messages.

## Measurements

On the pod (Pi 5, 4 cores, 4 GB memory limit), one HRRR run (2026-11-15 02Z
f00) and one RRFS run (2026-11-15 00Z f00), 20 sites, page cache warm unless
it says cold. Wall time and peak RSS. "Before" is the eccodes decode
(`_load_fields`), excluding interpreter start and imports; the Rust figures
are the whole process.

| Subset | Before | Streaming Python | Rust, 4 threads | Rust, 1 thread |
|---|---|---|---|---|
| HRRR isobaric, 185 fields, 126 MB | 16.8 s, 1.68 GB | 18.4 s, 84 MB | 0.44 s (1.4 s CPU), 10 MB; cold 1.2 s | 1.4 s, 5 MB |
| HRRR native, 228 fields, 308 MB | 21.8 s, 2.00 GB | 24.7 s, 86 MB | 0.54 s (2.0 s CPU), 18 MB; cold 2.3 s | 1.9 s, 9 MB |
| HRRR surface, 5 fields, 9 MB | 3.2 s, 345 MB | 0.7 s, 84 MB | 0.01 s, 8 MB | 0.04 s, 6 MB |
| RRFS isobaric, 185 fields, 283 MB | 18.4 s, 1.68 GB | 20.5 s, 86 MB | 0.47 s (1.8 s CPU), 18 MB; cold 2.1 s | 1.5 s, 7 MB |
| RRFS surface, 5 fields, 9 MB | 3.4 s, 344 MB | 0.8 s, 86 MB | 0.02 s, 9 MB | 0.05 s, 6 MB |

"Streaming Python" was eccodes one message at a time, keeping only the
wanted points: it fixed the memory but was about 10% slower, since eccodes
still decodes every full field (about 70 ms each). The scripts for the
"Before" and "Streaming" columns were removed with the code they measured;
they are in commit 1b715cc under `rust/bench/`.

After wiring in, with one process doing HRRR, RRFS and URMA for all 20 sites
(runs pinned and already on disk): 55 s in all, Python's peak RSS 242 MB.
Before, one cold `fetch_hrrr_profile` alone peaked at 3.55 GB. One full
Skew-T render from cached files: 18 s, 327 MB peak.

### Point forecasts (2026-11-17)

One day at Little Black (11 or 12 hour files per model), files already
fetched, sidecars emptied of decoded values. "Decode" is every
`decode_points` call for one site; "forecast" is the whole
`point_forecast.py` process, tables drawn. The pod was busy with other
runs, so take these as rough; RRFS's decode ranged from 11 to 27 s
between runs, with its 1.5 GB of files in and out of the page cache.

| Model | Messages | Decode, eccodes | Decode, grib-profile | Forecast before | Forecast now |
|---|---|---|---|---|---|
| HRRR | 649 | 70 s | 4 s | 91 s, 336 MB | 21 s, 231 MB |
| RRFS | 1221 | 146 s | 18 s | 129 s (with a 151 MB download), 333 MB | 30 s, 230 MB |
| GFS | 784 | 59 s | 2 s | 78 s, 270 MB | 21 s, 250 MB |

One hour file through the binary alone, 20 sites: HRRR 59 messages
0.45 s and 9 MB, RRFS 111 messages 1.3 s and 16 MB, GFS 71 messages
0.09 s and 10 MB.

## Findings

- **Point forecast correctness.** On those 34 hour files at all 20
  sites, the old eccodes decode and the new one give the same double for
  every value, and grid points within 1e-13 degrees. On
  GFS, 9 more places around the world (southern hemisphere, either side
  of the first meridian, the date line, a near-tie between rows) agree
  too. At the poles they differ, by design: every point of a pole's row
  is the same place, eccodes' arrays put the tie at an arbitrary
  longitude, and grib-profile takes the column nearest the longitude
  asked for.
- **Herbie keeps eccodes in the image.** `import herbie` imports
  `cfgrib`, which imports `eccodes` at import time. With eccodes blocked,
  `import grib` fails. So `eccodes`, `libeccodes0` and the Dockerfile's
  `eccodeslib` workaround stay until Herbie is replaced (below).

- **Correctness.** At the 20 sites and at 300 random points spread over
  the grid, grib-profile and eccodes give the same grid point and
  bit-identical values on every file tried: HRRR isobaric, native, surface
  and terrain, RRFS isobaric, surface and terrain, and URMA. HRRR isobaric
  and RRFS surface were also checked at the grid's corners.
- **The speed is the decoder's, not the language's.** With the `grib` crate
  (0.20) the decode took 11.9 s single-threaded on HRRR isobaric, about what
  eccodes takes, and didn't speed up past 2 threads. The decoder here takes
  1.2 s of CPU for the same file. Part of that is stopping early: the SoCal
  sites are 35 to 44% of the way through the grid. Decoding to the last grid
  point takes about 17 ms per field against eccodes' 70 ms.
- **Formats.** Every message is grid template 3.30 (Lambert Conformal,
  spherical earth, scanning mode 64) with no bitmap. Data representation is
  5.3 (complex packing, second-order spatial differencing) for all but a
  handful of messages that use 5.0. No JPEG2000. RRFS is served on HRRR's
  grid (1799 x 1059); URMA's is its own.
- **Nearest grid point.** The old KD-tree measured distance in degrees,
  treating a degree of longitude like a degree of latitude. For 2 of the 20
  sites that picked the next row over: Elsinore (2.13 km away rather than
  2.06 km) and Skyport (1.69 km rather than 1.68 km). Both now use the
  nearer point. `point_forecast._nearest_grid_point` was changed to match
  (great-circle), and agrees with grib-profile at all 20 sites.
- **Off the grid.** A place outside the model's grid is now an error from
  the decode, where the KD-tree returned the nearest edge point. The subset
  is still downloaded first.
- **Imports.** `import grib` alone takes 8 s and 180 MB on the Pi (MetPy,
  Herbie, pandas). Not part of this work, but it is in every cold start.
- **Open-Meteo has no timeout.** `Simple_Sounding.py`'s Open-Meteo request
  passes no `timeout`; during testing one stalled connection hung a render
  for over five minutes. Not changed here.

## Not done, in rough order of value

1. Replace Herbie for run lookup and `.idx` parsing (0.6 to 1.1 s per
   lookup). Network-bound, so a latency win rather than a CPU one. Could
   be Python or Rust. It is also the last thing holding eccodes, cfgrib
   and xarray in the image, and part of the 8 s `import grib`. Herbie is
   used in `grib.py` and in `point_forecast.py` (`_herbie`, `_inventory`).
2. A place off the grid could be rejected before the download, from the
   grid definition alone.

## Dev environment

The pod image has Rust 1.100 (cargo, clippy, rustfmt), gcc, pkg-config,
libeccodes-dev and GNU time. `CARGO_TARGET_DIR` is `/data/target`, so the
binary is `/data/target/release/grib-profile`, and `grib.py` finds it there.
`/data` is wiped when the pod restarts: rebuild with `cargo build --release`
in `rust/`, and refetch test subsets with e.g. `PYTHONPATH=. python -c
"import grib; grib.fetch_hrrr_profile(32.99, -117.12, None)"`, then
`PYTHONPATH=. python rust/check_against_eccodes.py /data/cache/*.grib2`.

The eccodes decode this replaced needs 2 to 3.5 GB; running it in this pod
(4 GB limit) alongside anything else is risky.
