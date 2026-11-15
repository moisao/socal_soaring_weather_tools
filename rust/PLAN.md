# Rust GRIB path: plan and handoff

Status as of 2026-11-14. Nothing is built yet: work stopped while we set up
the dev environment (see "Blocker" below). This file is the handoff for any
agent continuing the work. Update the Status and Next steps sections as you go.

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
- The plan must state approximate CPU and memory differences, so we follow
  whichever path is most efficient, not "Rust for its own sake".

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
  `telegram_bot.py` calls `grib.set_region(...)` (lines ~969, ~1116) and
  `grib.clear_field_cache()` (lines ~282, ~983).
- Note: `_fetch_grib_profile`'s docstring still says each message is reduced
  to its nearest point "so the full 2D field is never materialized". That's
  stale: `_load_fields` materializes everything.

Numbers already recorded in grib.py's comments (measured on an M1, not the Pi):
- About 3 s to decode one RRFS run (225 messages) to arrays; 3.6 s including
  the KD-tree build.
- About 950 MB of float32 for a 125-message RRFS subset, i.e. about 7.6 MB
  per field (about 1.9M grid points). Subsets now hold more messages:
  RRFS isobaric is about 185, HRRR isobaric about 185, HRRR native 228.
- Subset sizes on disk: RRFS isobaric is about 270 to 310 MB; HRRR
  isobaric is about 130 MB, and HRRR native is about 300 MB.

## Plan, with expected CPU and memory differences

All figures are estimates from the numbers above plus usual Rust/Python
overheads. They are what the PoC has to confirm or refute; replace them with
measured Pi 5 numbers as they come in.

The key point is that **most of the memory win comes from the algorithm, not the
language.** The current code holds every decoded field of a run at once.
Decoding one message at a time, pulling out the wanted points and the region
crop, then dropping the array, means peak memory is about one field instead of
a whole run. That works in Python too. So the plan measures that change on
its own (step 1) before giving any credit to Rust.

| Step | What | Peak memory per decode (HRRR isobaric, ~185 fields) | CPU per decode | Notes |
|---|---|---|---|---|
| 0 | Baseline: current Python | ~1.4 GB of fields, plus float64 temporaries from eccodes, lat/lon arrays (~30 MB), KD-tree (~50 to 100 MB), and the Python and numpy interpreter (~150 to 250 MB RSS) | ~3 s on M1; probably 6 to 10 s on Pi 5 (to measure), single-threaded | Measure first, on the Pi |
| 1 | Python, streaming: decode, extract points and crop, drop | ~30 to 60 MB above interpreter baseline | Same decode, minus the KD-tree build (~0.5 s) | Replace the KD-tree with an LCC inverse projection, so lat/lon arrays aren't needed either. Small, low-risk change |
| 2 | Rust CLI, streaming, pure-Rust decode (`grib` crate) | ~10 to 40 MB total process RSS | Decode CPU probably comparable to eccodes (0.7 to 1.5x; eccodes is already C). Wall time ~3 to 4x better from decoding messages in parallel on 4 cores | Also drops eccodes and its double-free clash with pyproj |
| 2b | Rust CLI calling system `libeccodes` via FFI | Same as 2 | Same decode CPU as Python | Isolates language effect from decoder effect. Optional |
| 3 | Rust download path (reqwest/tokio, direct S3 URLs plus `.idx` parsing instead of Herbie) | Small | Network-bound, so little CPU difference; latency win from skipping Herbie's mirror walks (~0.6 to 1.1 s per miss) | Could also be done in Python by replacing Herbie |
| 4 | Expose to the bot via PyO3/maturin (or a subprocess call) | Python baseline stays | Same as 2 | Only if 2 beats 1 by enough to justify the extra build step in the Docker image |

Expected outcome, to be confirmed: step 1 captures most of the memory win
(GB down to tens of MB). Step 2 adds a modest single-core CPU win, a large
wall-time win through parallel decode, and a much smaller process. Steps 3 and 4
depend on the measurements.

## PoC design (step 2)

- Location: `rust/` in this repo, a standalone Cargo binary (`grib-profile`).
  The Docker image only copies `*.py` and `sites.tsv`, so nothing here affects
  prod until we choose to wire it in.
- Input: a cached GRIB2 subset (as written by the Python code) plus one or
  more lat/lon points. Output: JSON shaped like `_decode_grib_fields`'
  per-level `{shortName: value}` dict, plus `cone`/`lov` and the grid point's
  lat/lon, so it can be compared value-for-value with Python.
- Decode with the `grib` crate (pure Rust). Check which data representation
  templates the HRRR/RRFS subsets use first (probably 5.3, complex packing
  with spatial differencing; check for 5.40 JPEG2000, which needs openjpeg
  and so a C compiler).
- Index points with an LCC inverse projection computed from the grid
  definition (Latin1/2, LoV, La1/Lo1, Dx/Dy), not a KD-tree. Assert it picks
  the same grid point as Python's KD-tree for every site in `sites.tsv`.
- Decode messages in parallel (rayon), one field in memory per worker.
- Benchmark: same files and points, Python vs Rust, cold and warm page cache.
  Use `/usr/bin/time -v` (or `getrusage` on the child) for user/sys CPU and
  max RSS. Run on the Pi 5 pod, since that's where prod runs.
- Correctness bar: identical grid-point index and values equal to float32
  precision, for every field and every site in `sites.tsv`.

## Blocker: dev environment

The pod (homelab k8s, image based on the bot's python:3.14-slim) has no Rust
toolchain, no C compiler or linker, no libc dev files, and no root or sudo.
`libeccodes.so.0` exists, but its headers and dev symlink don't. The public
internet, crates.io/rustup, and the NOAA S3 bucket are reachable. Hardware:
aarch64 Pi 5, 4 cores, 8 GB RAM. `/data` is local disk, 183 GB free, wiped on
restart. `/home/app` is a persistent NAS volume.

Needed: `rustc`/`cargo` (pinned stable, plus clippy/rustfmt), `build-essential`
(gcc and libc6-dev), and `pkg-config`. Nice to have: `libeccodes-dev` (for 2b)
and GNU `time`. `maturin` can go in a user venv
(`python -m venv --system-site-packages`) with no image change.

Two options were put to the user; **they haven't chosen yet. Don't install
anything until they do.** (They rejected an attempt to install rustup into
home.)
1. Recommended: add the tools to the pod image in the homelab repo (not
   attached to this session):
   ```dockerfile
   RUN apt-get update && apt-get install -y --no-install-recommends \
         build-essential pkg-config libeccodes-dev time \
       && rm -rf /var/lib/apt/lists/*
   ENV RUSTUP_HOME=/opt/rust/rustup CARGO_HOME=/opt/rust/cargo \
       PATH=/opt/rust/cargo/bin:$PATH CARGO_TARGET_DIR=/data/target
   RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal \
         --default-toolchain <pinned-stable> --no-modify-path \
       && rustup component add clippy rustfmt \
       && chown -R 10001:10001 /opt/rust
   ```
2. User-space: rustup into `~/.cargo` (persists on the NAS) with `zig cc` as
   the linker in place of gcc. No eccodes headers.

## Next steps

1. Get the user's decision on the dev environment and confirm the tools work
   (`cargo new` plus build a hello world that links).
2. Pull one HRRR and one RRFS run into `/data/cache` with the existing Python
   (`SOUNDING_CACHE_DIR=/data/cache`, `grib.fetch_hrrr_profile(...)` for a
   SoCal site). Record the Python baseline (step 0) on the Pi.
3. Check the data representation templates in those subsets.
4. Build the Rust CLI (step 2) and compare it against Python for correctness.
5. Prototype the Python streaming change (step 1) and measure it.
6. Fill in the table above with measured numbers and recommend which steps
   to take. Open a PR (never push to `main`; see /etc/claude-code/CLAUDE.md).
