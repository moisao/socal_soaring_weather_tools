"""
Plot MSLP forecast difference (LAX − DAG) from HRRR, GFS, or NAM.

Usage:
  python hrrr_pressure_diff.py               # defaults to hrrr
  python hrrr_pressure_diff.py --model gfs
  python hrrr_pressure_diff.py --model nam

Needs herbie-data, which is not in requirements.txt: nothing else here
uses it, and the bot's image is built without it.
"""

import argparse
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")           # writes files only; see Simple_Sounding.py
import numpy as np
import pandas as pd
from herbie import FastHerbie

from gradient import render_gradient_panel

# Per-model configuration
MODEL_CONFIGS = {
    "hrrr": {
        "product": "sfc",
        "search":  "MSLMA:mean sea level",
        "fxx":     lambda hour: list(range(0, 49 if hour in (0, 12) else 19)),
    },
    "gfs": {
        "product": "pgrb2.0p25",
        "search":  "PRMSL:mean sea level",
        # hourly 0-120 (default cap), then 3-hourly to 384 if user requests more
        "fxx":     lambda hour: list(range(0, 121)),
        "max_fxx": 384,
    },
    "nam": {
        "product": "conusnest.hiresf",
        "search":  "MSLMA:mean sea level",
        "fxx":     lambda hour: list(range(0, 61)),
    },
}

# THREDDS/NCSS equivalents. The server subsets to a point for us, so a
# whole series comes back as ~1.5 KB of CSV instead of the ~29 MB of GRIB
# the byte-range path has to pull (0.6 MB per MSLP message x 49 hours) --
# GRIB messages are whole 2D CONUS fields, so there is no way to fetch
# just one column. Measured: ~20,000x less data for the same 98 numbers.
#
# The "Best" series is used rather than the full reference/forecast
# collection because this server ignores the NCSS `runtime` parameter on
# point queries -- every form of it returns the earliest reference time,
# so a specific run cannot be pinned this way. Best stitches each valid
# time from the most recent run covering it, which is what you want for
# a current forecast but means there is no single run to label.
SIPHON_CONFIGS = {
    "hrrr": ("https://thredds.ucar.edu/thredds/catalog/grib/NCEP/HRRR/CONUS_2p5km/catalog.xml",
             "Best HRRR CONUS 2.5km Forecasts Time Series"),
    "gfs":  ("https://thredds.ucar.edu/thredds/catalog/grib/NCEP/GFS/Global_0p25deg/catalog.xml",
             "Best GFS Quarter Degree Forecast Time Series"),
    "nam":  ("https://thredds.ucar.edu/thredds/catalog/grib/NCEP/NAM/CONUS_12km/catalog.xml",
             "Best NAM CONUS 12km from NOAAPORT Time Series"),
}
NCSS_MSLP_VAR = "Pressure_reduced_to_MSL_msl"   # same name on all three

parser = argparse.ArgumentParser()
parser.add_argument("--model", choices=MODEL_CONFIGS.keys(), default="hrrr")
parser.add_argument("--fxx", type=int, default=None, help="Cap forecast horizon in hours")
parser.add_argument("--source", choices=("siphon", "grib"), default="siphon",
                    help="siphon: THREDDS/NCSS point query, tiny download, but no run "
                         "pinning and HRRR only reaches ~18h. grib: byte-range GRIB2, "
                         "hundreds of MB, but pins a run and gets HRRR's full 48h "
                         "(default: %(default)s)")
args = parser.parse_args()

cfg = MODEL_CONFIGS[args.model]

# Airport coordinates (lat, lon)
LOCATIONS = {
    "LAX": (33.9425, -118.4081),
    "DAG": (34.8537, -116.7868),
}

# --- Find latest run (GRIB path only; Best has no single run) ---
# Herbie requires tz-naive UTC datetimes.
now = datetime.now(timezone.utc).replace(tzinfo=None)
run_time = None
if args.source == "grib" and args.model == "hrrr":
    # Default to latest 00z or 12z run (48h horizon); go back enough to ensure it's complete
    candidate = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    while candidate.hour not in (0, 12):
        candidate -= timedelta(hours=1)
    run_time = candidate
elif args.source == "grib" and args.model == "gfs":
    # GFS runs at 00/06/12/18z; go back ~4h to ensure data is available
    candidate = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=4)
    while candidate.hour not in (0, 6, 12, 18):
        candidate -= timedelta(hours=1)
    run_time = candidate
elif args.source == "grib":
    run_time = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)

if args.source == "grib":
    fxx_list = cfg["fxx"](run_time.hour)
    # If --fxx specified, extend or trim the list accordingly
    if args.fxx is not None:
        model_max = cfg.get("max_fxx", fxx_list[-1])
        cap = min(args.fxx, model_max)
        if cap > fxx_list[-1]:
            # Extend with 3-hourly steps beyond the default
            fxx_list = fxx_list + list(range(fxx_list[-1] + 3, cap + 1, 3))
        else:
            fxx_list = [f for f in fxx_list if f <= cap]
    max_fxx = fxx_list[-1]
    print(f"Fetching {args.model.upper()} run: {run_time.strftime('%Y-%m-%d %H:%Mz')}, fxx 0–{max_fxx}h")
else:
    print(f"Fetching {args.model.upper()} via THREDDS/NCSS (best available series)")

# --- Fetch MSLP for all forecast hours ---
def fetch_siphon():
    """Point series per location from THREDDS/NCSS.

    The server does the spatial subsetting, so each location costs a
    couple of KB of CSV rather than a share of the whole CONUS field.
    Returns (valid_times UTC-aware, {name: hPa array})."""
    import io
    import requests
    from siphon.catalog import TDSCatalog

    cat_url, ds_name = SIPHON_CONFIGS[args.model]
    ncss = TDSCatalog(cat_url).datasets[ds_name].access_urls["NetcdfSubset"]
    horizon = args.fxx if args.fxx is not None else cfg.get("max_fxx", 120)
    start = datetime.now(timezone.utc)

    out, times = {}, None
    for name, (lat, lon) in LOCATIONS.items():
        r = requests.get(ncss, timeout=120, params={
            "var": NCSS_MSLP_VAR, "latitude": lat, "longitude": lon, "accept": "csv",
            "time_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "time_end": (start + timedelta(hours=horizon)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        r.raise_for_status()
        frame = pd.read_csv(io.StringIO(r.text))
        col = [c for c in frame.columns if NCSS_MSLP_VAR in c][0]
        # Each location is subset independently, so assert the time axes
        # agree rather than trusting them to line up.
        t = pd.to_datetime(frame["time"], utc=True)
        if times is None:
            times = t
        elif not times.equals(t):
            raise RuntimeError(f"{name} returned a different time axis than the first location")
        out[name] = frame[col].to_numpy() / 100.0
        print(f"  {name}: {len(frame)} times, {len(r.content):,} bytes")
    return pd.DatetimeIndex(times), out


def fetch_grib():
    """Point series per location from raw GRIB2 byte ranges via Herbie.

    Pulls whole 2D fields (there is no spatial subsetting in GRIB) and
    reduces each to its nearest grid point locally. Hundreds of MB, but
    it pins an exact run and reaches HRRR's full 48h from a 00/12z cycle.
    Returns (valid_times UTC-aware, {name: hPa array})."""
    max_threads = 2 if args.model == "gfs" else 20
    FH = FastHerbie([run_time], model=args.model, product=cfg["product"],
                    fxx=fxx_list, max_threads=max_threads)
    ds = FH.xarray(cfg["search"], remove_grib=False)
    print(f"Dataset variables: {list(ds.data_vars)}")
    print(f"Dataset dims: {dict(ds.dims)}")

    def nearest_point(ds, lat, lon):
        """Nearest grid point, for either a rectilinear grid (GFS: 1D
        lat/lon dims) or a Lambert Conformal one (HRRR: 2D coords on y/x)."""
        lon_360 = lon % 360
        if "latitude" in ds.dims and "longitude" in ds.dims:
            return ds.sel(latitude=lat, longitude=lon_360, method="nearest")
        dist = (ds.latitude - lat) ** 2 + (ds.longitude - lon_360) ** 2
        idx = np.unravel_index(int(dist.argmin()), dist.shape)
        return ds.isel(y=idx[0], x=idx[1])

    mslp_var = [v for v in ds.data_vars if "msl" in v.lower()][0]
    print(f"Using variable: {mslp_var}")
    out = {name: nearest_point(ds, lat, lon)[mslp_var].values.flatten() / 100.0
           for name, (lat, lon) in LOCATIONS.items()}
    # Valid times from the dataset's own steps, which guards against a
    # partial download silently shortening the series.
    steps = ds.step.values
    times = pd.to_datetime([run_time + pd.Timedelta(s) for s in steps]).tz_localize("UTC")
    return times, out


valid_times, series = fetch_siphon() if args.source == "siphon" else fetch_grib()

lax = series["LAX"]
dag = series["DAG"]
diff = lax - dag

# Convert valid times to Pacific time for display.
#
# Through the zone rather than a fixed offset: a hardcoded -7 is silently
# an hour wrong for the whole PST half of the year, and -- since these
# runs reach 48h and GFS 384h -- a single forecast can straddle a DST
# transition, which no single offset can represent at all. tz_convert
# applies whichever offset was actually in force at each valid time.
# Both fetch paths return UTC-aware times already, so this only converts.
valid_times_pt = valid_times.tz_convert("America/Los_Angeles")

# --- TSV output ---                                                                                     
tsv_out = "hrrr_pressure_diff.tsv"                                                                       
df_out = pd.DataFrame({                                                                                  
    "valid_time_pt": valid_times_pt.strftime("%Y-%m-%d %H:%M"),                                          
    "lax_hpa":       np.round(lax, 2),                                                                   
    "dag_hpa":       np.round(dag, 2),                                                                   
    "diff_hpa":      np.round(diff, 2),                                                                  
})                                                                                                       
df_out.to_csv(tsv_out, sep="\t", index=False)                                                            
print(f"Saved → {tsv_out}")                                                                              
                   
      
# --- Plot ---
# The GRIB path pins one run and can name it; the Best series stitches
# each valid time from whichever run covers it best, so there is no
# single run to name -- say that rather than imply a precision it hasn't
# got. Issue time is the series' own first valid time.
if run_time is not None:
    subtitle = run_time.strftime("%Y-%m-%d %H:%Mz run")
else:
    subtitle = f"best available, from {valid_times[0].strftime('%Y-%m-%d %H:%Mz')}"

fig = render_gradient_panel(
    valid_times_pt, diff,
    f"{args.model.upper()} MSLP Difference "
    f"(LAX \N{MINUS SIGN} DAG) \N{EM DASH} {subtitle}")

out = "hrrr_pressure_diff.png"
fig.savefig(out, dpi=150)
print(f"Saved → {out}")
