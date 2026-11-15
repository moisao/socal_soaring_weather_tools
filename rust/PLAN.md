# Rust GRIB path: plan and handoff

Status as of 2026-11-14: the proof of concept is built and measured on the
Pi 5 pod. The Rust decode returns the same grid points and bit-identical
values as the current Python, in about 1/40 of the wall time, 1/12 of the CPU
and 1/100 of the memory. Nothing is wired into the bot yet; that is the next
decision (see "Recommendation" and "Next steps"). This file is the handoff
for any agent continuing the work. Update it as you go.

## Goal

Find the most CPU- and memory-efficient way to run the bot's GRIB path
(`grib.py`: fetch HRRR/RRFS/URMA subsets from NOAA S3, decode, extract a
profile at a point). Start with a Rust proof of concept, measure it against
the current Python, and only then decide how far to take Rust.

Decisions so far (agreed with the user):
- No full rewrite. The MetPy thermodynamics and the matplotlib plots
  (Skew-T, tables) are the expensive parts to port and stay in Python for now.
- The GRIB path goes first: it is self-contained, uses the most memory, and is
  easy to verify (same file and point in, same numbers out).
- Follow whichever path measures as most efficient, not "Rust for its own
  sake".

## How the current Python path works (grib.py)

- `_fetch_grib_profile` picks a run with Herbie (one `.idx` lookup per
  candidate run, ~0.6 to 1.1 s each), then downloads matching messages as
  parallel HTTP byte ranges (16 threads) into one cached `.grib2` subset per
  (model, run, lead) under `SOUNDING_CACHE_DIR`. Three subsets per profile:
  isobaric (`prs`), native/hybrid levels (HRRR `nat`, levels 1 to 38), and
  surface (`sfc`/`2d`). Terrain is cached once per model as an `.npz`.
- `_load_fields` decodes **every message of a subset to a full-grid float32
  array** with eccodes and keeps all of them, plus the lat/lon arrays and a
  scipy `cKDTree` over the whole grid. It is `lru_cache(maxsize=3)` and
  guarded by `_full_decode_lock`.
- A point is then one KD-tree query. Winds are rotated from grid-relative to
  earth-relative with the LCC cone constant (`_lcc_cone_constant`) and `LoV`.
- `set_region` makes `_load_fields` also write a cropped `.region.npz` around
  the bot's sites, so later requests for the same run skip the full decode.
  `telegram_bot.py` calls `grib.set_region(...)` and
  `grib.clear_field_cache()`.
- Note: `_fetch_grib_profile`'s docstring still says each message is reduced
  to its nearest point "so the full 2D field is never materialized". That's
  stale: `_load_fields` materializes everything.

## What was built

- `rust/`: a standalone Cargo binary, `grib-profile`. The Docker image only
  copies `*.py` and `sites.tsv`, so nothing here affects prod.
  - `grib-profile SUBSET.grib2 (--sites sites.tsv | --point LAT,LON)...
    [--threads N] [--decoder own|crate]` prints JSON: `cone`, `lov`, and per
    point the grid index, the grid point's lat/lon and `{shortName_level:
    value}` with eccodes' names (`t_850`, `10u_10`, `sp_0`...).
  - `src/grib2.rs` is a hand-written decoder for exactly what these subsets
    contain (see "Findings"): one field per message, no bitmap, data
    representation templates 5.0 and 5.3. It decodes straight to the wanted
    points, stops at the last one, and allocates nothing per message. It
    scales values the way eccodes does (in f64, then narrowed), which is what
    makes the output bit-identical.
  - `src/main.rs` finds the grid point by inverting the Lambert Conformal
    projection from Section 3 (no lat/lon arrays, no KD-tree), and decodes
    messages on a few threads, one message in memory per thread.
  - `--decoder crate` uses the `grib` crate (0.20) instead. It is only there
    as a cross-check and a speed comparison, and it accounts for 34 of the 40
    packages in `Cargo.lock`.
- `rust/bench/py_baseline.py`: step 0, the current `grib._load_fields` at
  every site, in the same JSON shape.
- `rust/bench/py_streaming.py`: step 1, the same eccodes decode but one
  message at a time, with the projection in place of the KD-tree.
- `rust/bench/compare.py`: same grid index and float32-equal values, per
  site and field.
- `rust/bench/bench.sh SUBSET.grib2...`: runs all of the above and prints
  the timings below.

## Findings

Measured on the pod (Pi 5, 4 cores, 4 GB memory limit) with one HRRR run
(2026-11-15 02Z f00) and one RRFS run (2026-11-15 00Z f00), at the 20 sites in
`sites.tsv`, page cache warm unless it says cold. "Decode" for Python excludes
interpreter start and imports; Rust figures are the whole process.

| Subset | Current Python (step 0) | Streaming Python (step 1) | Rust, 4 threads (step 2) | Rust, 1 thread |
|---|---|---|---|---|
| HRRR isobaric, 185 fields, 126 MB | 16.8 s, 1.68 GB | 18.4 s, 84 MB | 0.44 s (1.4 s CPU), 10 MB; cold 1.2 s | 1.4 s, 5 MB |
| HRRR native, 228 fields, 308 MB | 21.8 s, 2.00 GB | 24.7 s, 86 MB | 0.54 s (2.0 s CPU), 18 MB; cold 2.3 s | 1.9 s, 9 MB |
| HRRR surface, 5 fields, 9 MB | 3.2 s, 345 MB | 0.7 s, 84 MB | 0.01 s, 8 MB | 0.04 s, 6 MB |
| RRFS isobaric, 185 fields, 283 MB | 18.4 s, 1.68 GB | 20.5 s, 86 MB | 0.47 s (1.8 s CPU), 18 MB; cold 2.1 s | 1.5 s, 7 MB |
| RRFS surface, 5 fields, 9 MB | 3.4 s, 344 MB | 0.8 s, 86 MB | 0.02 s, 9 MB | 0.05 s, 6 MB |

Times are wall time; memory is peak RSS of the process. The current Python
is single-threaded, so its CPU time is about its wall time.

- **Correctness.** Rust, streaming Python and current Python pick the same
  grid point for all 20 sites, and all 12,160 values are equal as float32.
  The same holds at 302 points spread over the whole grid, corners included
  (55,870 values on each isobaric subset), which exercises the decoder to
  the end of the field.
- **The whole Python request is worse than its parts.** One cold
  `fetch_hrrr_profile` (download and decode of all three subsets) took 71 s
  and peaked at 3.55 GB RSS, because `lru_cache(maxsize=3)` holds all three
  decoded subsets at once. The pod's limit is 4 GB.
- **Step 1 fixes memory but not CPU.** Streaming in Python brings peak memory
  from 1.7 to 2.0 GB down to about 85 MB (most of that is the interpreter,
  numpy and eccodes), but it is about 10% slower than today: eccodes still
  decodes every full field (about 70 ms each), and allocating and freeing a
  15 MB float64 array per message costs system time. It does remove the
  ~3 s fixed cost of generating lat/lon arrays and building the KD-tree,
  which is nearly all of the surface subsets' time.
- **Rust's CPU win comes from the decoder, not the language.** With the
  `grib` crate the decode takes 11.9 s single-threaded on HRRR isobaric,
  about the same as eccodes, and it doesn't speed up past 2 threads. The
  hand-written decoder takes 1.2 s of CPU for the same file. Part of that is
  stopping early: the SoCal sites are 35 to 44% of the way through the grid.
  Decoding to the very last grid point takes 0.9 s wall and 3.1 s CPU, about
  17 ms per field against eccodes' 70 ms.
- **Formats.** Every message is grid template 3.30 (Lambert Conformal,
  1799 x 1059, spherical earth, scanning mode 64) with no bitmap. Data
  representation is 5.3 (complex packing, second-order spatial differencing)
  for all but a handful of messages that use 5.0 (simple packing). No
  JPEG2000, so no C library is needed. RRFS is served on the same grid as
  HRRR.
- **Nearest grid point.** The KD-tree measures distance in degrees, treating
  a degree of longitude like a degree of latitude, so it doesn't always
  return the grid point nearest on the ground. Rust reproduces Python's
  choice (it compares the 3x3 neighbourhood the same way) so results match.
  For 2 of the 20 sites the two differ by one grid row: Elsinore (2.13 km
  away rather than 2.06 km) and Skyport (1.69 km rather than 1.68 km).
- **Imports.** `import grib` alone takes 8 s and 180 MB on the Pi (MetPy,
  Herbie, pandas). Not part of this work, but it is in every cold start.

The estimates this replaces predicted that step 1 would capture most of the
memory win (true) and that Rust's decode CPU would be comparable to eccodes
(true for the `grib` crate, not for a decoder written for these files).

## Recommendation

Take step 2 into the bot: have `grib.py` get its per-point values from
`grib-profile` instead of `_load_fields`. At about half a second and under
20 MB per subset, with nothing held between requests, that also makes the
`.region.npz` crops, the `lru_cache`, `_full_decode_lock` and
`clear_field_cache()` unnecessary.

- Call it as a subprocess first (JSON on stdout). Process start is a few
  milliseconds, against a build that needs only a Rust stage in the
  Dockerfile. PyO3/maturin would save nothing measurable here.
- Skip step 1 as a separate change: it would be superseded at once.
- Skip step 2b (Rust calling libeccodes): the measurements already separate
  the decoder's effect from the language's.
- Step 3 (Rust download, replacing Herbie) is independent and network-bound.
  Worth doing later for the 0.6 to 1.1 s per `.idx` lookup and to drop
  Herbie and pandas from the import, not for CPU.

## Dev environment

Resolved: the pod image now has Rust 1.100 (cargo, clippy, rustfmt), gcc,
pkg-config, libeccodes-dev and GNU time. `CARGO_TARGET_DIR` is `/data/target`,
so the binary is `/data/target/release/grib-profile`. `/data` is wiped when
the pod restarts; refetch the test subsets with step 1 below.

## Next steps

1. To reproduce: `cd rust && cargo build --release`, fetch a run into
   `/data/cache` (`PYTHONPATH=. python -c "import grib;
   grib.fetch_hrrr_profile(32.99, -117.12, None)"`, likewise
   `fetch_rrfs_profile`), then `rust/bench/bench.sh /data/cache/*.grib2`
   from the repo root.
2. Get the user's go-ahead on the recommendation, and on whether to keep
   Python's grid-point choice or switch to the true nearest point.
3. Wire it in: `_decode_grib_fields` calls the binary; remove the region
   crop, the field cache and their callers in `telegram_bot.py`. Check that
   terrain (`_fetch_terrain_height`) and URMA (`fetch_urma_surface`) go
   through the same path, so eccodes can be dropped from the image. URMA is
   a different grid (2.5 km) and hasn't been run through the decoder yet.
4. Add a Rust build stage to the `Dockerfile` (aarch64) and copy the binary
   into the image. The workflows under `.github/workflows/` can't be edited
   with the bot's token, so check that the existing image build needs no
   change.
5. Before shipping: drop the `grib` crate and `--decoder crate` (cross-check
   only), and add decoder tests against a small committed GRIB2 fixture (one
   surface message is about 1.7 MB) or a synthetic 5.3 message.
6. Later, separately: step 3.
