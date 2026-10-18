# Change log

Started as MetPy's stock "Simple Sounding" example. Summary of everything
changed since, roughly in the order it happened.

See [README.md](README.md) for how the thing works today; this file is
history, and Part 1 below describes a single-file script that has since
been split into modules.

---

# Part 2 — modeled profiles, and the split into modules

## Modeled soundings at an arbitrary point

- `--lat`/`--lon` fetch a **modeled** vertical profile instead of an
  observed radiosonde, so a "sounding" can be had anywhere, hourly,
  rather than only at a launch site twice a day. Priority chain
  `rrfs -> hrrr -> gfs`, overridable with `--model`.
- HRRR and RRFS are read as **raw GRIB2 from NOAA's S3 buckets**. An
  earlier THREDDS-based attempt was abandoned: HRRR's THREDDS feed only
  exposes 5 pressure levels, nowhere near enough for a sounding. Herbie
  resolves which run/source exists and parses the `.idx` sidecar into
  byte ranges; the fetch itself is a parallel range-request pull, because
  Herbie's own `.download()` fetched messages one at a time and took over
  10 minutes for a request this wide.
- GFS stayed on THREDDS/NCSS (`thredds.py`) — server-side subsetting, no
  GRIB decode needed, and it's only the coarse global fallback anyway.
- `--run-datetime`/`--forecast-hour` pin an exact run and lead instead of
  resolving one from a valid time.
- `--name` adds an arbitrary label above the geocoded location, so a site
  can carry your own name for it rather than the nearest town's.

## Getting the vertical profile right

- **Terrain trim.** Isobaric fields are extrapolated below ground by
  NOAA's post-processing rather than left missing, so levels underneath
  the model's own orography were being plotted as if they were real air.
  Verified directly: Palomar (673 m terrain) had its 1000/975/950 mb
  levels at 126/348/575 m, all below its own ground. Levels below the
  grid's terrain height are now dropped.
- **Native/hybrid levels.** HRRR's `nat` product is merged in alongside
  the 25 mb isobaric steps, giving 2-3x the near-surface resolution (its
  level 1 measured ~11 m AGL). Written model-agnostically: RRFS publishes
  no native-level product yet, so it is a no-op there and will pick it up
  automatically if that changes.
- **A real surface row.** The biggest correctness fix of the lot. The
  "surface parcel" was being launched from the lowest *isobaric* level
  that survived the terrain trim — fine for HRRR at ~11 m AGL, badly
  wrong for RRFS, whose lowest retained level measured **150-230 m AGL**
  across these sites. The model's own 2 m temperature ran **3.6-4.6 °C
  warmer** than the air up there, which reads as RRFS under-forecasting
  surface heating when it does nothing of the kind. The model's surface
  diagnostics (2 m temperature/dewpoint, 10 m wind, surface pressure) are
  now prepended as the profile's ground row, for every model. Thermal
  tops went from 722→1,179 m at Little Black, 825→1,401 m at Palomar,
  1,058→1,521 m at Marshall.
- **Grid-relative winds.** HRRR/RRFS `u`/`v` are relative to the Lambert
  Conformal grid, not true north, and would point wrong everywhere off
  the central meridian. Rotated using the cone constant and central
  meridian read from each message's own metadata, not hardcoded.
- **Longitude convention.** eccodes reports grid longitudes in 0-360.
  Querying the nearest-point tree with a raw -180..180 value silently
  matched coastal British Columbia for a San Diego query before this was
  caught.

## Parcel and lapse-rate panel

- The modeled parcel is always the **surface** parcel (not MetPy's
  most-unstable search), since these plots are about surface-driven
  convection, with the ±1 °C uncertainty band kept.
- Mixing-top validity check: the −1 °C parcel often never intersects the
  environment at all, and the underlying routine reported its LCL as if
  it were a cloud base. Such a top is now rejected rather than drawn.
- Parcel-minus-environment temperature is plotted on the lapse-rate panel
  on its own twinned x-axis (the two quantities don't share units).
- A CAPE shading experiment was added and reverted — it didn't render
  well, and these dry profiles have essentially no CAPE anyway.

## Structure

- **Made importable.** The script parsed `sys.argv` and ran the entire
  fetch-and-plot at module scope, so merely importing it rendered and
  wrote a plot. Everything moved into `main(args=None, output_dir=None)`
  behind a `__main__` guard.
- **Split into modules** along the seams already marked by comment
  banners: `grib.py`, `thredds.py`, `wyoming.py`, `skewt.py`,
  `lapse_rate.py`, `geocode.py`, `cli.py`, `config.py`, with
  `Simple_Sounding.py` left as the entry point. Verified by
  byte-identical plot output before and after.
- **Backend switched from QtAgg to Agg.** Everything goes through
  `savefig`, and a GUI backend needs a display present *at import time*,
  which breaks on a headless host now that the bot imports this directly.

## Telegram bot

- `/sounding [station] [datetime]`, `/site <name> [datetime]`, `/sites`.
  Named sites come from `sites.tsv`, matched longest-prefix first so
  multi-word names survive having a datetime appended.
- Runs the render **in-process** rather than shelling out, on a worker
  thread behind a lock (pyplot is global state). It previously identified
  its own output by picking the newest matching PNG off disk, which two
  overlapping requests could get wrong.
- `httpx` request logging pinned to WARNING: the Telegram API puts the
  bot token in the URL path, and at INFO it was written in clear text to
  whatever the bot's output was redirected to.
- Runs as a systemd user service (`weather-sounding-bot.service`) with
  the token in a mode-600 `EnvironmentFile`.

## Point forecast

- `point_forecast.py` / `forecast_table.py`, and `/pointforecast <site>
  [day]` in the bot: every daylight hour of one day as two tables of
  altitude (250 ft steps) against hour — thermals (top of lift, cumulus
  base, cloud cover, updraft at altitude) and wind.
- Same models, run selection and parcel construction as a modeled
  sounding, so the two agree hour for hour; the updraft is `w*` from the
  model's surface heat flux, shaped by Lenschow's profile.
- Fetches far less than twelve soundings would: only the levels above
  the ground and as deep as the day's thermals need, RH instead of
  dewpoint, and each message reduced to one grid point as it is read.
- GFS for this comes off NOAA's GRIB2 bucket, not THREDDS — hourly
  rather than 3-hourly, with its averaged heat flux un-averaged.
- A run counts as posted only once both its files *and their `.idx`* are
  up: a run mid-upload has the GRIB2 before the index.
- `/pf` as the short form of `/pointforecast`, and a model name after the
  site (`/pf lb sat hrrr`) to use that model alone. Without one it's RRFS
  then HRRR — GFS only when named, being too coarse to trust by default.
- Site aliases: an optional fourth column in `sites.tsv` (`lb`, `torrey`,
  …), matched like the name by every command.

## Faster cold renders

A cold `/site` render measured 157 s on an M1 (84 s in the cluster).
Profiled, about a third of that was not fetching at all:

- **No more `.fields.npz`.** Every cold fetch zlib-compressed the
  decoded full fields to disk: ~56 s of single-threaded CPU, for a file
  twice the size of the GRIB2 it came from (657 vs 312 MB) that loaded
  back no faster than decoding the GRIB2 again (2.95 vs 2.98 s). Fields
  are now decoded from the GRIB2 directly, still memoized in memory.
- The native and surface subsets download in the background alongside
  the isobaric one, rather than one after another after it.
- The latest-run lookup probes its candidate hours at once instead of
  stepping back an hour at a time (~1.1 s a miss, often three misses).

Same profile, row for row; 157 → 80 s cold on the M1, where nearly
all of what's left is downloading 312 MB. Then, to get the download
itself out of the way:

- **Nothing above 100 mb.** Isobaric levels stop at 100 mb and HRRR's
  native levels at 38 (~112 mb; from 39 up they're pure pressure
  surfaces at 99 mb and above). Stratosphere only — no EL or cloud top
  gets there — and 38 MB off an RRFS lead, ~88 MB off HRRR's natives.
- **Pre-warm.** `SOUNDING_PREWARM="6-20"` has the bot fetch the freshest
  run for this hour and the next in the background, so a `/site` finds
  it on disk: ~4 s instead of a minute-plus. Per-file locks make a
  request that arrives mid-download wait for that download rather than
  start its own.
- **Region crop.** The hope was to download only SoCal, but nothing
  serves these models by area: NOMADS' grib filter has no RRFS, and only
  7 HRRR pressure levels; RRFS's smallest domain covering SoCal is
  CONUS; HRRR-Zarr's pressure levels are analyses only. So the crop is
  of what's kept: each decoded subset leaves a `.region.npz` of the box
  around `sites.tsv` (~9 MB), and later sites on that run decode from it
  in ~0.6 s instead of ~6 s, without the ~1 GB of full-grid arrays.

## Repeat requests and a busy archive

- A repeat `/pf` took 30 s+ with everything already downloaded: every
  cached GRIB2 message was decoded again (~7 s — each is a whole-grid
  decode), the hours' runs were looked up one at a time (~8 s once the
  misses aged out), and the terrain KD-tree was rebuilt each time. Each
  site's decoded values are now kept in the `.point.json` sidecar, the
  hours are looked up in parallel, and terrain is memoized: measured in
  the pod, a repeat is ~2 s in the bot, ~8 s from a cold process.
- `/sounding` failed with "No recent sounding data" while the Wyoming
  archive was answering *503: the server is too busy*. siphon turns every
  HTTP error into the same bare `ValueError`, so a busy server read as
  four missing launches in a row. Only a 404 means "not launched"; other
  errors are retried (3 s, then 10 s) and then reported as the archive
  failing.

## Command logging and metrics

- Each command is logged as it arrives and again with its outcome, how
  long the reply took and what it was for; log lines carry timestamps.
- `metrics.py`: Prometheus counters and a response-time histogram per
  command and outcome, plus the site, model and days ahead of successful
  ones, served on `METRICS_PORT`. Every label combination starts at zero
  so `increase()` sees the first request too.

## Launch elevation

- `sites.tsv` takes a launch elevation (ft MSL) after the coordinates.
  With one, the wind table's bottom row is the model's wind at launch
  height rather than its 10 m wind, and the gust is estimated from it;
  both tables draw a rule at launch.
- Why: RRFS's 3 km terrain tops out at 206 m within 6 km of Little Black
  (launch 357 m, summit ~470 m), so its 10 m wind is a sheltered slope's.
  On 2026-10-16 the Black Mountain summit station's afternoon wind was
  14–20 mph; RRFS at launch height said 16–22, its 10 m wind 11–15. The
  gust estimate's mean error over 8am–5pm was 7 mph, against 12 for the
  model's 10 m gust and 9 for xcskies'.

- The launch row went back to the model's 10 m wind, with the free-air
  rows starting above launch. Against the Holfuy and WeatherXM stations
  at Little Black on 2026-10-16/17, the 10 m wind was closer in three
  comparisons of four: the free air at launch height carried the models'
  morning easterly jet (35–45 mph forecast, 14–19 measured).
- Torrey Pines' launch: 350 ft.
- Both tables start at the row at or below launch, as xcskies' do (1,000
  ft for Little Black's 1,170), the thermals table in place of the ground
  row and the rule it drew at launch: rows of lift far below launch only
  pushed the useful part up, and with so many sites between 1,000 and
  2,000 ft the row under launch is the one to read.
  Where the site is below the model's ground (Andy Jackson, the Marshall
  LZ), both tables start at the model's ground and the footer says why.
- The gust estimate uses the model's own 10 m gust unless thermals are
  mixing through launch height; before, a morning jet just above the hill
  read as 40+ mph launch gusts. RRFS gusts now track xcskies' within a few mph.

## Point forecasts drawn ahead

- A `/pf` was cold most times it was asked: each RRFS run that posts
  moves the rest of the day onto new files, ~0.5–1.6 GB and 40–90 s a
  day. Drawn tables are now kept for as long as the same runs answer
  every hour of the day (`point_forecast.plan_day` says which, in a few
  lookups), and `POINT_FORECAST_PREWARM` draws every site's today and
  tomorrow as each new run lands, so a request finds them done.
- `HourFile.decode_points` reads one whole-grid unpack for any number of
  sites. Drawing all 17 sites' two days cold took 532 s one site at a
  time and 262 s priming the rest from each one's pass; the tables come
  out byte-identical.
- The pre-warm doesn't hold up requests: point forecasts fetch under a
  lock of their own, taking the render lock only to draw, and the pass
  waits for any request in progress before each site-day (a released
  lock can be re-taken before a waiting thread wakes). Tested in the pod
  with a cold pass under way: a `/pf` waited only for the download
  already in progress, and the render lock was never held more than 1 s.
- `OBSERVED_PREWARM` draws the newest launch of the stations it names as
  soon as Wyoming posts it, so the first bare `/sounding` after a launch
  is a replay.

## Also found along the way

- 1000 mb is the highest isobaric level either model publishes; there is
  no 1025/1050 mb to extend down to for high pressure.
- RRFS's native-level product (`natlev`) is real and documented but not
  yet populated on the operational bucket, consistent with RRFS still
  being pre-implementation (v1 scheduled 2026-10-14).
- RAP is the only other source publishing both pressure *and* native
  levels, but its native archive only reaches back to ~May 2021 and its
  13 km terrain is far too coarse for ridge sites.
- HRRR's `wrfnat` has an archive gap from 2016-08-23 to ~2016-09-30
  (`wrfprs` is fine throughout). The same gap appears on the Google
  mirror, so it is upstream. The native-level fetch degrades to
  isobaric-only there rather than failing.

---

# Part 1 — the original single-file script

## Configuration (CLI args)

Originally these were environment variables (`SOUNDING_STATION`,
`ALTITUDE_UNIT`); converted to proper `argparse` flags for discoverability
(`--help` documents them) and so a single shell command captures the full
invocation instead of needing exported env vars alongside it.

- `--station STATION` — station identifier, e.g. `NKX` (default: `NKX`).
- `--altitude-unit {km,kft}` — unit for the MSL altitude axis (default:
  `km`). `km` ticks every 1 km; `kft` (thousands of feet, `KM_TO_KFT =
  3.280839895`) ticks every 3 kft.
- `--datetime` — UTC date/time of the sounding run to plot, e.g.
  `"2025-01-01T12"` or `"2025-01-01 12:00"` (default: the latest
  available 00Z/12Z run). Accepts a handful of formats via
  `parse_datetime()`.

`SHOW_PREVIOUS` (overlay the prior run) is still a plain `True`/`False`
constant in the script, not yet exposed as a flag.

## Rendering / display

- **Backend switched to QtAgg** (`matplotlib.use('QtAgg')`, `PyQt6`
  installed into the venv). The original `TkAgg` backend rendered the
  window at the wrong pixel size under GNOME's fractional display scaling
  on Wayland (Tk has no native Wayland support and runs via XWayland,
  which doesn't handle fractional scaling correctly). Qt handles it fine.
- Fixed an invalid `figsize=(9, 9, "in")` call (matplotlib silently
  ignored the stray `"in"`; now just `(9, 9)`/`(7.5, 10)`).
- `SkewT(..., aspect='auto')` plus tighter `xlim`/`ylim` so the skewed
  plot area fills the figure instead of leaving large blank margins
  (MetPy's default fixed `aspect=80.5` centers a fixed-shape box with
  padding regardless of the figure's actual dimensions).
- Title added via `skew.ax.set_title()` (not `fig.suptitle()` — the
  latter anchors to the whole figure and left a large gap above the
  axes; `set_title()` sits directly above the plot).

## Data fetching

- **Latest-synoptic-time lookup**: `latest_synoptic_time()` rounds down
  to the most recent 00Z/12Z launch; `fetch_recent_sounding()` retries
  12 hours further back (up to 4 tries) since a sounding takes ~1-2
  hours to post after launch.
- **Local caching**: fetched soundings are saved as CSV under
  `cache/<station>_<run>.csv`; re-running the script re-reads the cache
  instead of re-hitting the Wyoming archive. Cache is per-run, so it
  never goes stale for a past sounding.
- **Open-Meteo integration** (`requests`, no API key): pulls that day's
  high temperature for the sounding station's own coordinates (read
  straight from the fetched sounding's `latitude`/`longitude` columns),
  plus the station's local UTC offset/timezone abbreviation used for the
  title and legend times.
- **Forecast vs. historical Open-Meteo endpoint**: dates within ~90 days
  use the live `/v1/forecast` endpoint (real forecast high, legend reads
  "Forecast high"); older dates automatically switch to the
  `/v1/archive` historical endpoint instead (actual observed high,
  legend reads "Observed high") — the live forecast endpoint doesn't
  serve data that far back, which matters for the 2025 batch runs below.

## Plot content

- Fixed a copy/paste bug: `np.arange(22, 24, 26, 28)` (4 positional args
  misinterpreted the 4th as a `dtype`) → an explicit `np.array([...])`.
- **`plot_clipped_dry_adiabats()`**: a hand-rolled replacement for
  MetPy's `plot_dry_adiabats()`, which has no awareness of the actual
  temperature profile and draws full-height reference lines. This
  version computes each dry adiabat, finds where it first crosses the
  environmental temperature curve going up from the surface, and draws
  only the segment below that crossing — so lines stop where they meet
  real data instead of running the full plot height.
- **Forecast-high adiabat**: the Open-Meteo forecast high is drawn as
  a special dry adiabat (the "convective temperature" technique) —
  where it crosses the morning sounding shows the forecast afternoon
  mixed-layer depth.
- **Previous-sounding overlay** (`SHOW_PREVIOUS` toggle): the prior
  synoptic run's temperature/dewpoint traces are overlaid on the same
  axes as dashed, faded (`alpha=0.4`) lines for comparison, labeled with
  their local time in the legend. (An earlier side-by-side two-panel
  version was tried and reverted in favor of this simpler overlay.)

## Axes

- **MSL altitude axis**: originally added as a secondary axis using
  MetPy's `pressure_to_height_std`/`height_to_pressure_std` (a fixed
  ICAO standard-atmosphere formula). Replaced with
  `make_msl_height_functions()`, which interpolates the sounding's own
  reported `height` column (real geopotential height, MSL-referenced)
  instead — this reflects the atmosphere actually observed that run,
  not a theoretical one.
- **Axis roles swapped**: MSL altitude (km) is now the primary
  (innermost) y-axis, capped at a 6000 m `ylim` (converted to the
  equivalent pressure bound via the sounding's own data, since the
  underlying SkewT projection is always pressure/log-p internally).
  Pressure (hPa) was moved to a secondary, outer axis.
- Along the way, fixed axis-label collisions (widened the figure,
  adjusted secondary-axis offsets/`labelpad`), a mismatched unit label
  (axis said "(m)" while actually plotting km), scientific-notation tick
  labels on log-scale secondary axes (explicit `FixedLocator`/
  `FuncFormatter` instead of the default log formatter), and a stray
  auto-generated "hectopascal" label (Pint/MetPy's matplotlib units
  integration auto-labels axes from Quantity units; needed an explicit
  `skew.ax.set_ylabel('')` to clear it).
- **`--altitude-unit kft`**: `make_msl_height_functions()` takes a `unit`
  argument and scales the interpolated heights by `KM_TO_KFT`; tick
  spacing switches to every 3 kft (vs. every 1 km) accordingly.
- **Dashed MSL altitude gridlines**: `alt_ax.grid()` silently produced
  gridline artists that never actually rendered — matplotlib's secondary
  axis gridline transform gets confused when the secondary axis's range
  is inverted (low pressure/high altitude at the top of the plot).
  Worked around by drawing the dashed lines directly on `skew.ax` via
  `axhline()`, at the pressure value each altitude tick converts to.

## Export

- `fig.savefig(..., dpi=150, bbox_inches='tight')` for a web-friendly
  PNG (2x pixel density for high-DPI screens, no dead margin).
- Matching `.svg` export for vector use.

## Housekeeping

- Dropped dead weight left over from the original MetPy example: unused
  `col_names`, the unused `get_test_data`/`add_metpy_logo` imports, and
  the `plt.rcParams['figure.figsize']` line (dead since `figsize` is
  always passed explicitly to `plt.figure()` now).
- Fixed a handful of formatting artifacts from iterative edits (stray
  trailing whitespace, a title line split across two f-strings for no
  reason, misaligned wrapped function signature).

## Historical / batch runs

With `--datetime`, the script can plot any past 00Z/12Z run, not just
the latest one. Example — the 1st of every month at 12Z for 2025:

```bash
for m in 01 02 03 04 05 06 07 08 09 10 11 12; do
    python Simple_Sounding.py --datetime "2025-${m}-01T12"
done
```

Produces `NKX_2025<MM>01_12Z.png`/`.svg` for each month. (One run, for
September, needed a longer-than-60s timeout on the Wyoming archive
fetch on retry — a transient slow response, not missing data.)

## Not done

- Looked into Meteoblue support in `siphon` — it isn't there (`siphon`
  only wraps ACIS, IA State, IGRA2, NDBC, and Wyoming). Would need a
  direct API call, same pattern as the Open-Meteo integration.
