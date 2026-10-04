# Skew-T sounding plotter

Fetches and plots a Skew-T log-P sounding with a lapse-rate side panel,
lifted-parcel analysis and thermal-top estimates. Two kinds of sounding:

- **Observed** — a real radiosonde launch from the University of Wyoming
  archive. Fixed station locations, 00Z/12Z only.
- **Modeled** — a vertical profile pulled out of a forecast model at any
  lat/lon, hourly. RRFS, HRRR or GFS.

Output is `<station>_<valid time>.png` plus a matching `.svg`, written to
the working directory.

## Human Note and Attribution

Guided by me through claude, using examples from MetPy's excellent documentation. Herbie is used to pull raw GRIB data.


## Quick start

```bash
cd ~/Documents/weather/sounding
PY=../.venv/bin/python

$PY Simple_Sounding.py                                  # latest NKX radiosonde
$PY Simple_Sounding.py --station 72572                  # another station
$PY Simple_Sounding.py --lat 33.3352 --lon -116.9438    # modeled, anywhere
$PY Simple_Sounding.py --lat 33.3352 --lon -116.9438 \
    --run-datetime 2026-09-16T12 --forecast-hour 31 --name Palomar
```

`--help` documents every flag. The ones worth knowing:

| Flag | What it does |
|---|---|
| `--station` | Radiosonde station id (default `NKX`) |
| `--lat` / `--lon` | Modeled profile at a point, instead of a station |
| `--model` | Force `rrfs`, `hrrr` or `gfs` instead of the priority chain |
| `--datetime` | Desired **valid** time |
| `--run-datetime` | Pin an exact model **run** (init) time |
| `--forecast-hour` | Forecast lead in hours from the run |
| `--name` | Your own label for the site, shown above the geocoded name |
| `--altitude-unit` | `km` (default) or `kft` |
| `--compare*` | Overlay a second sounding; same flags, `--compare` prefixed |

### How a run and lead get chosen

Four cases, checked in order:

1. `--run-datetime` given — that exact run, no fallback. Lead defaults to 0.
2. `--forecast-hour` alone — latest available run, at that lead.
3. Neither, and no `--datetime` — latest available run, lead 0.
4. `--datetime` — treated as the desired **valid** time. If it's in the
   future, the freshest posted run is used with whatever lead reaches it;
   if it's now or past, the run at/before it, stepping back an hour at a
   time if that hour wasn't posted.

`--run-datetime`/`--forecast-hour` aren't supported for GFS — its NCSS
"Best" series doesn't expose individual cycles.

## Layout

| File | |
|---|---|
| `Simple_Sounding.py` | Entry point: `main()`, the model dispatcher, CLI wiring |
| `grib.py` | GRIB2 fetch + eccodes decode off S3 — HRRR, RRFS, URMA |
| `thredds.py` | THREDDS/NCSS fetch — GFS |
| `wyoming.py` | Observed radiosonde archive |
| `skewt.py` | The Skew-T panel: traces, barbs, adiabats, parcel, MSL axis |
| `lapse_rate.py` | Lapse rate, surface inversion, the side panel |
| `geocode.py` | Station code or lat/lon → place name for the title |
| `cli.py` | Argument parsing |
| `config.py` | Shared constants (cache dir, model priority, units) |
| `telegram_bot.py` | Telegram front end |

The two source modules are named for their *access mechanism*, not their
model, because that's the real difference: `grib.py` does byte-range
fetches and decodes GRIB2 itself, `thredds.py` lets a server do the
subsetting. Dependencies point inward to `config.py`; there are no
cycles.

`main()` can be called in-process — `main(args, output_dir=...)` returns
the output path stub — so importing this module doesn't render anything.

## Where the data comes from

| Source | Bucket / service | Grid | Used for |
|---|---|---|---|
| RRFS | `noaa-rrfs-ops-pds` | 3 km | `prslev` pressure levels, `2dfld` surface |
| HRRR | `noaa-hrrr-bdp-pds` | 3 km | `prs` pressure, `nat` native levels, `sfc` surface |
| GFS | Unidata THREDDS | 0.25° | Global fallback |
| URMA | `noaa-urma-pds` | 2.5 km | Surface analysis only — see below |
| Wyoming | siphon | — | Observed radiosondes |

Herbie is used only to resolve which run/source exists and to parse the
`.idx` sidecar into byte ranges. The fetch is a parallel range-request
pull, since Herbie's own downloader is sequential and was taking over ten
minutes for a request this wide.

`fetch_urma_surface()` exists in `grib.py` but is **deliberately not
wired into anything**. URMA is a 2-D analysis — no vertical levels at
all — so it cannot produce a sounding. It's there because it's an
observation-assimilated 2 m temperature/dewpoint at 2.5 km, which is the
natural way to sanity-check a modeled surface parcel against reality. It
is analysis-only, so it can never cover a future valid time.

## How a modeled profile is assembled

In order, and the order matters:

1. **Isobaric levels**, 1000→400 mb in 25 mb steps. 1000 mb is the
   highest level either model publishes — there is no 1025/1050 mb.
2. **Native/hybrid levels** merged in where the model publishes them.
   HRRR does (`nat`, level 1 is ~11 m AGL); RRFS does not yet, so this
   is currently a no-op there.
3. **Terrain trim** — anything below the model's own orography is
   dropped. Isobaric fields are extrapolated below ground rather than
   left missing, so without this the plot starts in imaginary
   underground air.
4. **Surface row prepended** — the model's own 2 m temperature and
   dewpoint, 10 m wind and surface pressure, placed at terrain height.
   Everything at or above its pressure is dropped, so pressure stays a
   strictly decreasing coordinate.

Step 4 matters more than it looks. Without it the "surface parcel" starts
from the lowest isobaric level that survived the trim — which for RRFS
measured 150–230 m above ground, where the air ran 3.6–4.6 °C cooler than
the model's own 2 m temperature. That discards exactly the superadiabatic
layer that drives a thermal, and it starts the parcel neutrally buoyant
with its environment by construction. It looks like the model
under-forecasts surface heating; it doesn't.

Winds are rotated from grid-relative to true north using the Lambert
Conformal cone constant and central meridian read from each GRIB
message's own metadata. eccodes reports longitudes in 0-360, so lookups
normalise to that convention — querying with a raw -180..180 value
silently matches somewhere thousands of km away.

## Cache

Everything lands in `cache/` (git-ignored):

| | |
|---|---|
| `<station>_<run>.csv` | Wyoming soundings |
| `<MODEL>_<run>_f<lead>.grib2` | Fetched GRIB2 subset |
| `<...>.fields.npz` | Decoded full fields, so a second point at the same run needs no network and no re-decode |
| `<...>.native.grib2` | Native levels |
| `<...>.surface.grib2` | 2 m / 10 m / surface-pressure fields |
| `<MODEL>_terrain.npz` | Orography — static, so cached once per model rather than per run |

Fetches write to a `.part` file and rename on completion, so an
interrupted run can't leave a truncated file that looks complete. The
cache is keyed by run, never by point, so it only grows with new runs.

## Telegram bot

```
/sounding [station] [time]     e.g. /sounding NKX 2026-09-17T17  (the 00Z launch)
/site <name> [time]            e.g. /site Little Black            (this hour)
                                    /site Little Black 14         (2pm today)
                                    /site Little Black 2026-09-17T12
/sites                         list the named sites
```

Times typed into the bot are **local** — `SOUNDING_TIMEZONE`, default
`America/Los_Angeles` — unless they name a zone (`2026-09-17T19Z`,
`21Z`, `+00:00`). A bare hour means that hour today; `2pm` and `14:00`
work too. The command line keeps reading bare times as UTC.

`/site` with no time is a forecast for the current hour from the freshest
model run. (It used to be that run's own analysis, which for RRFS —
3-hourly, posted ~1.5 h late — can be up to ~4 h old.)

Named sites come from `../sites.tsv`: one tab-separated `name`, `lat`,
`lon` per line. The parser splits on `\s+` rather than on tabs
specifically, so space-aligned lines still work — but a site name may
contain single spaces ("Little Black"), so keep the columns tab-separated
to avoid ambiguity. The longest matching prefix wins, so a trailing
datetime doesn't confuse a two-word name.

Renders run in-process on a worker thread behind a lock, because pyplot
is global mutable state — concurrent requests queue rather than
interleaving.

Repeat requests are answered from earlier renders while those are still
current (`replay.py`, index in `replay-index/` beside the images):

- **Observed** — a render of the launch asked for is kept for good.
- **HRRR/RRFS** — kept until the bucket has a newer run covering the same
  valid time (checked newest-first, ~0.6 s a run). A forecast refreshes
  when a fresher run reaches its hour; a past hour is final once nothing
  newer can still post (4 h after it).
- **GFS**, or a render that fell back from the preferred model — kept for
  an hour, since GFS doesn't say which cycle it came from.

Forecast images carry their run in the name
(`RRFS_…_20261004_11Z_run20261004_00Z.png`), so one run's forecast
never overwrites another's. A new version of the code re-renders
everything rather than replaying old-looking plots.

### Running it as a service

```bash
systemctl --user status weather-sounding-bot      # state
systemctl --user restart weather-sounding-bot     # after changing code
journalctl --user -u weather-sounding-bot -f      # logs
```

- Unit: `~/.config/systemd/user/weather-sounding-bot.service`
- Token: `~/.config/weather-sounding-bot.env`, mode 600, `EnvironmentFile`
  so it never reaches `ps`, `systemctl show` or the journal
- Linger is enabled, so it survives logout and starts at boot
- Restarts on failure, capped at 5 tries per 5 minutes

**Python caches modules at import**, so a running bot keeps using the
code as it was when it started. Restart it after editing anything.

To rotate the token:

```bash
install -m 600 /dev/null ~/.config/weather-sounding-bot.env
printf 'TELEGRAM_BOT_TOKEN=%s\n' 'NEW_TOKEN' > ~/.config/weather-sounding-bot.env
systemctl --user restart weather-sounding-bot
```

### Running it in a container

Every push to `main` publishes an amd64 + arm64 image to
`ghcr.io/mguzhou/socal_soaring_weather_tools` (`latest`, plus
`sha-<commit>`), built by `.github/workflows/docker.yml`:

```bash
docker run -d --name sounding-bot --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN="$TOKEN" \
  -v sounding-data:/data \
  ghcr.io/mguzhou/socal_soaring_weather_tools:latest
```

In the image, the cache and rendered PNG/SVGs live under `/data` and the
site list is `sites.tsv` from this repo. The paths are set by
`SOUNDING_CACHE_DIR`, `SOUNDING_OUTPUT_DIR` and `SOUNDING_SITES_FILE`,
which default to the usual locations when unset. Nothing prunes `/data`,
so it grows by roughly 900 MB per model run fetched.

The image uses Debian's `libeccodes` rather than the `eccodeslib` wheel.
That wheel's `eckitlib` dependency bundles its own `libproj`/`libsqlite3`,
and on Linux, loading it alongside pyproj's copies crashes
(`double free or corruption`) when `eccodes` is imported before
`metpy.calc`, as `grib.py` does.

## Batch runs

- `interesting.sh` — a fixed list of past dates, observed soundings.
- `interesting2.sh` — every site in `sites.tsv`, pinned to one exact run
  and lead so it stays reproducible.

## Known limits

- **RRFS has no native levels yet.** `natlev` is a real, documented
  product but isn't populated on the operational bucket; RRFS v1 is
  scheduled for 2026-10-14. The code already maps it and will use it the
  moment it appears.
- **HRRR `wrfnat` has an archive gap**, 2016-08-23 to ~2016-09-30
  (`wrfprs` is unaffected). It's upstream — the same gap is on the Google
  mirror. The fetch degrades to isobaric-only rather than failing.
- **Native level spacing has changed across HRRR versions**, so "the
  lowest 15 levels" may not cover the same depth in 2015 as today. Fine
  for recent runs; worth checking before a long historical study.
- **GFS can't pin a run or lead** — its "Best" series doesn't expose
  cycles.
- **1000 mb is the floor**, so a sounding at a below-sea-level or
  very-high-pressure site has no isobaric level beneath that. The surface
  row now covers the bottom regardless.

## Dependencies

Python 3.14 in `../.venv`. Key packages: `metpy`, `siphon`,
`herbie-data`, `eccodes`, `numpy`, `pandas`, `scipy`, `matplotlib`,
`requests`, `reverse_geocoder`, `python-dateutil`,
`python-telegram-bot`.

`eccodes` needs the ECMWF C library present, and wants a real file on
disk — it can't decode from an in-memory buffer.
