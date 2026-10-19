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
| `point_forecast.py` | A site's whole day: per-hour fetch, thermal top, updraft, wind |
| `forecast_table.py` | The point forecast's two tables, drawn |
| `telegram_bot.py` | Telegram front end |
| `metrics.py` | Per-command log lines and Prometheus metrics for the bot |

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

1. **Isobaric levels**, 1000→100 mb (25 mb steps up to 400, coarser
   above). 1000 mb is the highest level either model publishes — there
   is no 1025/1050 mb. Above 100 mb is stratosphere, and not fetched.
2. **Native/hybrid levels** merged in where the model publishes them.
   HRRR does (`nat`, level 1 is ~11 m AGL; levels 1–38, up to ~112 mb);
   RRFS does not yet, so this is currently a no-op there.
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

## Point forecast

```bash
$PY point_forecast.py --lat 32.988 --lon -117.122 --name "Little Black"
$PY point_forecast.py --lat 32.988 --lon -117.122 --date sat --model hrrr
```

One site, one local day, every hour the sun is up, as two tables of
altitude (every 250 ft MSL) against hour — `PF_<MODEL>_<lat>_<lon>_<day>_run<run>_thermals.png`
and `…_wind.png`:

- **Thermals** — top of lift, cumulus base (or "blue") and top, rain at
  the surface on days with any, the model's low / middle / high cloud
  cover, and the expected updraft at each altitude, with each cumulus
  shaded from base to top and drawn raining when it is.
- **Wind** — direction and speed at each altitude, the surface wind and
  gust, with each hour's top of lift ruled across its column.

`--date` takes a date, a bare day of the month (`17`), a weekday, `today`
or `tomorrow`.

It reads the same models as a modeled sounding, and each hour's run and
lead are chosen by the same rule — so
a column agrees with the sounding for that hour. Checked on one RRFS
hour against the sounding's own profile: same grid point, identical
heights, temperatures and winds, dewpoint within 0.1 °C (it comes from RH
here), top of lift within 15 ft. HRRR will differ a little more, since a
sounding also merges in its native levels and this doesn't fetch them.

One model serves the whole day: RRFS, or HRRR where RRFS doesn't reach
every hour (`DEFAULT_MODELS`). GFS is left out of that chain on purpose
— at 0.25° it's too coarse to trust for one site's day — so a day beyond
both (RRFS goes out ~3.5 days, HRRR 2) is reported as out of range rather
than quietly answered from GFS. `--model` (or a model named in the bot
command) uses that one model alone, GFS included. Past f120 GFS is
3-hourly, so a day that far out has a column every third hour.

**Top of lift and cumulus base** are the sounding's own construction (the
"Thermal tops" / "Cloud base" line): a parcel with the 2 m temperature and
the mixed layer's mean moisture, followed up to where it meets the
environment, or to its condensation level if that comes first.

**Cumulus top** is that parcel followed on up from its base, moist, to
where it is first no longer warmer than the air around it — the usual
estimate of how tall the cloud can build (`point_forecast.cumulus_top`).
A cloud still building at the top of what was fetched pulls the levels
up to 500 mb (~18,000 ft), as a thermal reaching the top does; one still
going there reads e.g. "19,350+". The grid shows the cloud up to 3,000 ft
above the day's highest top of lift, with an arrow where it goes on.

**Rain** is the model's precipitation rate at the surface that hour
(`PRATE`), shown from 0.1 mm/h. HRRR and RRFS publish it instantaneous;
GFS only as a mean, so a GFS day shows none.

**Updraft** is the convective velocity scale `w* = (g/T · H/ρcp · zi)^⅓`
— the model's surface sensible heat flux `H` mixed through the depth of
lift `zi` — spread over height with Lenschow & Stephens' profile
`(z/zi)^⅓ (1 − 1.1 z/zi)`, scaled so the strongest level reads `w*`. It
is the air in a thermal core; a glider climbs at that less its own sink.

**Cloud cover** is the model's own layer diagnostics, not inferred from
the profile.

### What it fetches

A sounding pulls every isobaric level of one hour and decodes the whole
grid. Twelve of those would be 2–4 GB, so this fetches narrowly instead:

- surface diagnostics first, then only the isobaric levels above the
  ground, to 250 hPa over it — the rest, up to 500 mb, only on a day
  whose thermals come within 50 hPa of that;
- RH in place of dewpoint (same information, a half to a third of the
  bytes), and only through the mixed layer the parcel's moisture comes
  from;
- each message reduced to its one grid point as it is read, so nothing
  the size of the grid is held or written back out.

That comes to roughly 45 MB an hour for HRRR at a low site, 85 MB for
RRFS, 35 MB for GFS. GFS is read from NOAA's GRIB2 bucket here rather
than through THREDDS: hourly files to f120 instead of a 3-hourly series,
and its heat flux — published only as a mean since the run's last
6-hourly reset — can be un-averaged against the previous lead.

## Cache

Everything lands in `cache/` (git-ignored):

| | |
|---|---|
| `<station>_<run>.csv` | Wyoming soundings |
| `<MODEL>_<run>_f<lead>.grib2` | Fetched GRIB2 subset |
| `<...>.native.grib2` | Native levels |
| `<...>.surface.grib2` | 2 m / 10 m / surface-pressure fields |
| `<...>.region.npz` | The bot's sites' area, cropped out of any of those three once decoded — a few MB instead of ~1 GB to decode for the next site (`grib.set_region`) |
| `<MODEL>_terrain.npz` | Orography — static, so cached once per model rather than per run |
| `<MODEL>_<run>_f<lead>.point.grib2` | Point-forecast messages for that hour; appended to as other sites ask for other levels |
| `<...>.point.json` | What that file holds, in order, and each site's values decoded from it — so a repeat request decodes nothing |
| `point-forecast/PF_*.png` | Point-forecast tables as drawn, sent or not; the ones sent are copied into the archive |

Fetches write to a `.part` file and rename on completion, so an
interrupted run can't leave a truncated file that looks complete. The
cache is keyed by run, never by point, so it only grows with new runs.

## Telegram bot

```
/sounding                      a menu: tap a station, then a launch
/sounding <station> [time]     e.g. /sounding NKX                (the latest launch)
                                    /sounding NKX 2026-09-17T17  (the 00Z launch)
/site                          a menu: tap a site, a time, then a model
/site <name> [time] [model]    e.g. /site Little Black            (this hour)
                                    /site Little Black 14         (2pm today)
                                    /site lb 14 hrrr              (HRRR only)
                                    /site Little Black 2026-09-17T12
/pf                            a menu: tap a site, a day, then a model
/pf <name> [day] [model]       e.g. /pf lb                          (today)
                                    /pf lb 17                       (the 17th)
                                    /pf torrey sat hrrr             (HRRR only)
                                    (/pointforecast is the long form)
/sites                         list the named sites
```

Times typed into the bot are **local** — `SOUNDING_TIMEZONE`, default
`America/Los_Angeles` — unless they name a zone (`2026-09-17T19Z`,
`21Z`, `+00:00`). A bare hour means that hour today; `2pm` and `14:00`
work too. The command line keeps reading bare times as UTC.

`/site` with no time is a forecast for the current hour from the freshest
model run. (It used to be that run's own analysis, which for RRFS —
3-hourly, posted ~1.5 h late — can be up to ~4 h old.)

`/pf` replies with the two [point forecast](#point-forecast) tables as
an album.

A bare `/pf`, `/site` or `/sounding` opens a menu of buttons in one
message, each tap moving it on to the next choice:

- `/pf`: the site, then one of the next four days, then the model.
- `/site`: the site, then now, an hour left in today's daylight or
  tomorrow's 9am / 12pm / 3pm, then the model.
- `/sounding`: the station, named with where it is ("NKX — San Diego
  (Miramar)"), then the latest launch or one of the two before it.

The model buttons are RRFS — the usual chain, which falls back to HRRR
(and for `/site` on to GFS) where RRFS doesn't reach — then HRRR or GFS
alone; HRRR is offered only within its 48 h. The last tap turns the menu
into the "Fetching…" message, and the result follows in the same chat.
Taps reach the bot in a group even with privacy mode on, where a typed
command may need `@sdwxtestbot` after it. Opening a menu is counted as
`pf_menu`, `site_menu` or `sounding_menu`; what it leads to as `pf`,
`site` or `sounding`. A model name (`rrfs`, `hrrr`, `gfs`) anywhere after the site
uses that model alone; without one it's RRFS, then HRRR. A bare number is a **day of the month** here, not an
hour — the tables already cover every daylight hour — and no day means
today, or tomorrow once the sun has set. A cold day is several hundred MB
of GRIB2, so the "Fetching…" message counts up as it goes; a second site
on the same runs reuses whatever messages are already down. Tables once
drawn are kept (in memory, PNGs in `cache/point-forecast/`) for as long
as the same runs still answer every hour of that day, so a repeat is
answered at once; a newer run reaching any hour means drawing it again.

Named sites come from `../sites.tsv`: one tab-separated `name`, `lat`,
`lon` per line, then optionally the launch elevation in whole feet MSL,
then optionally the other names the site goes by, comma-separated
(`Little Black → lb`, `Torrey Pines → torrey`). Elevations are the San
Diego HG/PG Association site guide's where it lists one, otherwise the
terrain at the site's coordinates (Torrey Pines: 350 ft).
Aliases work everywhere a name does — `/site lb 14`, `/pf torrey sat` —
and `/sites` lists them beside each name. The parser splits on `\s+` rather than on tabs
specifically, so space-aligned lines still work — but a site name may
contain single spaces ("Little Black"), so keep the columns tab-separated
to avoid ambiguity. The longest matching prefix wins, so a trailing
datetime doesn't confuse a two-word name.

Renders run in-process on a worker thread behind a lock, because pyplot
is global mutable state — concurrent requests queue rather than
interleaving.

`SOUNDING_PREWARM="6-20"` keeps the freshest model run's soundings for
this hour and the next downloaded ahead of time, checking every 5
minutes between those local hours. A cold `/site` is mostly download
(~280 MB of RRFS a lead); pre-warmed, it renders in about 4 s. That costs
roughly 0.5–0.8 GB of downloads an hour while it's on. Unset, nothing is
fetched until someone asks.

`POINT_FORECAST_PREWARM="5-21"` does the same for `/pf`: every 5 minutes
between those local hours it draws every site's point forecast for the
day a bare `/pf` means and the day after (`POINT_FORECAST_PREWARM_DAYS`,
default 2), skipping those whose runs haven't changed. RRFS's runs land
in the bucket 3-hourly, ~1.5–2 h late; each one costs ~1–1.5 GB and a
few minutes of fetching and decoding in the background, after which a
`/pf` for any site is answered in about a second. Today's tables come
first, for every site, then tomorrow's; each site-day decodes its hours
for all the sites in one pass (`point_forecast.prime`), so after the
first, most take a second or two. The pass never starts a site-day
while a request is in progress, so a `/pf` arriving mid-pass waits only
for the one under way: seconds, or the first of a run's download
(~1.5 min), which it would have waited on anyway. Point forecasts fetch
under their own lock, so `/site` and `/sounding` wait only for a table
being drawn (~1–2 s), never for a point-forecast download.

`OBSERVED_PREWARM="NKX"` (comma-separated stations) draws each station's
newest launch as soon as the Wyoming archive has it, so a bare
`/sounding` replays it in under a second instead of taking 5–30 s. It
checks every 5 minutes, outside the render lock, and draws twice a day.

`/site`'s `SOUNDING_PREWARM` is left off in the deployment: each new hour
needs a new lead (~280 MB of RRFS), it decodes whole grids outside the
render lock (~2 GiB peak, so one overlapping a `/site` request can reach
the pod's memory limit), and a warm `/site` saves only ~10–15 s.

No server offers these models cut to an area — NOMADS' grib filter has
no RRFS and only 7 HRRR pressure levels — so every fetch is the whole
grid. What does get cut to an area is what's kept decoded: the bot crops
each subset to a box around `sites.tsv` (`.region.npz`), so later sites
on that run read a few MB rather than decoding ~1 GB.

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

### Logs and metrics

Every command is logged twice: as it arrives, and with its outcome and
how long the reply took.

```
2026-10-16 09:12:03 INFO commands: /pf lb 17 from user 42 in private chat 42
2026-10-16 09:12:07 INFO commands: /pf lb 17 -> ok in 4.2s [site=Little Black, model=rrfs, day=1]: RRFS 3 km, runs 16 06Z, 0 MB downloaded
```

Users and chats are logged by ID, never by username or group title. The
bot being added to or removed from a chat is logged too, as is a group
becoming a supergroup (which changes its ID):

```
2026-10-18 12:30:00 INFO chats: Added to supergroup chat -1001234567890 by user 42 (left -> member)
```

Outcomes are `ok`, `bad_request` (couldn't read the command),
`unavailable` (the data doesn't exist yet, or the archive is down),
`timeout` and `error` (the bot failed).

With `METRICS_PORT` set, the same is served for Prometheus on
`:<port>/metrics`:

| Metric | Labels |
|---|---|
| `soaring_bot_commands_total` | `command`, `outcome` |
| `soaring_bot_response_seconds` (histogram) | `command`, `outcome` |
| `soaring_bot_requests_total` — successful commands only | `command`, `site`, `model`, `day` (days ahead: `past`, `0`…`6`, `7+`) |

Labels are kept to a fixed set, with every combination created at zero
on start-up, because PromQL's `increase()` can't see a series' first
increment and at a few commands a day most series would be exactly that.
A station typed into `/sounding` that doesn't look like one is counted as
`other`.

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
so it grows by roughly 300 MB per RRFS lead fetched (about 430 MB per
HRRR lead).

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
- **A point forecast's ground is the model's.** Altitudes are MSL, but the
  depth of lift starts from the grid cell's terrain, which a 3 km model
  smooths and a 0.25° one flattens: RRFS has nothing above 206 m within
  6 km of Little Black, whose launch is at 357 m. With a launch elevation
  in `sites.tsv`, the wind table's launch row is the model's 10 m wind and
  the gust is estimated from the free air at launch height (see
  `Hour.launch_gust_kt`). The thermals table's rows start at the 250 ft
  row at or below launch, as xcskies' do — 1,000 ft for Little Black's
  1,170 — and the wind table's free-air rows at the first row above it
  (1,250), so the launch row under them is the lowest altitude there.
  Neither goes at or below the model's ground. Where the model's ground is within
  150 ft of launch, or nearer it than any row (Horse Canyon's 4,400 ft
  against RRFS's 4,530; an LZ at the foot of a range like Andy Jackson,
  1,700 ft against 2,330), launch is read at the model's ground: the
  launch row is its 10 m wind and gust, the rows start just above the
  ground, and the footer says so.
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
