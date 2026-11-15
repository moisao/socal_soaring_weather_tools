//! Proof of concept for rust/PLAN.md step 2: decode a cached GRIB2 subset
//! (as written by grib.py) one message at a time and print the values at
//! the grid points nearest some sites, as JSON in the same shape as
//! bench/py_baseline.py so the two can be compared value for value.
//!
//! Unlike grib.py's `_load_fields`, no full-grid array outlives its own
//! message, and there is no lat/lon array or KD-tree: the grid point is
//! found by inverting the grid's Lambert Conformal projection.

use std::collections::BTreeMap;
use std::error::Error;
use std::fs::File;
use std::os::unix::fs::FileExt;
use std::process::ExitCode;
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::json;

mod grib2;

type Result<T> = std::result::Result<T, Box<dyn Error + Send + Sync>>;

/// A Lambert Conformal grid (GRIB2 grid definition template 3.30) on a
/// spherical earth, which is all HRRR, RRFS and URMA use.
#[derive(Debug, Clone, PartialEq)]
struct LambertGrid {
    nx: usize,
    ny: usize,
    radius: f64,
    /// Cone constant, as grib.py's `_lcc_cone_constant`.
    cone: f64,
    lov_deg: f64,
    dx: f64,
    dy: f64,
    /// R * F of the usual LCC formulas.
    rf: f64,
    /// Projected coordinates of the first grid point, with the origin at
    /// the cone's apex.
    x0: f64,
    y0: f64,
}

/// Sign-and-magnitude integer, as GRIB2 stores signed values.
fn grib_i32(bytes: &[u8]) -> i32 {
    let raw = u32::from_be_bytes(bytes.try_into().unwrap());
    let magnitude = (raw & 0x7fff_ffff) as i32;
    if raw & 0x8000_0000 != 0 {
        -magnitude
    } else {
        magnitude
    }
}

fn grib_u32(bytes: &[u8]) -> u32 {
    u32::from_be_bytes(bytes.try_into().unwrap())
}

impl LambertGrid {
    /// From Section 3 without its 5-byte header, so `payload[n]` is the
    /// section's octet n + 6.
    fn from_section3(payload: &[u8]) -> Result<Self> {
        let octets = |first: usize, last: usize| &payload[first - 6..=last - 6];
        if payload.len() < 76 || u16::from_be_bytes(octets(13, 14).try_into().unwrap()) != 30 {
            return Err("not a Lambert Conformal grid (template 3.30)".into());
        }
        let radius = match payload[15 - 6] {
            0 => 6_367_470.0,
            1 => f64::from(grib_u32(octets(17, 20))) / 10f64.powi(i32::from(payload[16 - 6])),
            6 => 6_371_229.0,
            8 => 6_371_200.0,
            shape => return Err(format!("unsupported shape of the earth {shape}").into()),
        };
        let scanning_mode = payload[65 - 6];
        if scanning_mode != 0b0100_0000 {
            // Anything else stores points in a different order than the
            // index arithmetic below assumes.
            return Err(format!("unsupported scanning mode {scanning_mode:#010b}").into());
        }
        let degrees = |first| f64::from(grib_i32(octets(first, first + 3))) * 1e-6;
        let (la1, lo1) = (degrees(39), degrees(43));
        let lov_deg = degrees(52);
        let (latin1, latin2) = (degrees(66), degrees(70));

        let half_colatitude_tan =
            |lat_deg: f64| (std::f64::consts::FRAC_PI_4 - lat_deg.to_radians() / 2.0).tan();
        let cone = if (latin1 - latin2).abs() < 1e-6 {
            latin1.to_radians().sin()
        } else {
            (latin1.to_radians().cos() / latin2.to_radians().cos()).ln()
                / (half_colatitude_tan(latin1) / half_colatitude_tan(latin2)).ln()
        };
        let rf =
            radius * latin1.to_radians().cos() / (cone * half_colatitude_tan(latin1).powf(cone));

        let mut grid = LambertGrid {
            nx: grib_u32(octets(31, 34)) as usize,
            ny: grib_u32(octets(35, 38)) as usize,
            radius,
            cone,
            lov_deg,
            dx: f64::from(grib_u32(octets(56, 59))) * 1e-3,
            dy: f64::from(grib_u32(octets(60, 63))) * 1e-3,
            rf,
            x0: 0.0,
            y0: 0.0,
        };
        (grid.x0, grid.y0) = grid.project(la1, lo1);
        Ok(grid)
    }

    /// (x, y) in metres from the cone's apex.
    fn project(&self, lat_deg: f64, lon_deg: f64) -> (f64, f64) {
        let rho = self.rf
            * (std::f64::consts::FRAC_PI_4 - lat_deg.to_radians() / 2.0)
                .tan()
                .powf(self.cone);
        let dlon = (lon_deg - self.lov_deg + 180.0).rem_euclid(360.0) - 180.0;
        let theta = self.cone * dlon.to_radians();
        (rho * theta.sin(), -rho * theta.cos())
    }

    /// Fractional (i, j) grid position of a lat/lon.
    fn fractional_ij(&self, lat_deg: f64, lon_deg: f64) -> (f64, f64) {
        let (x, y) = self.project(lat_deg, lon_deg);
        ((x - self.x0) / self.dx, (y - self.y0) / self.dy)
    }

    /// Lat/lon of grid point (i, j), longitude in 0..360 as eccodes
    /// reports it.
    fn lat_lon(&self, i: usize, j: usize) -> (f64, f64) {
        let x = self.x0 + i as f64 * self.dx;
        let y = self.y0 + j as f64 * self.dy;
        let rho = x.hypot(y).copysign(self.cone);
        let theta = x.atan2(-y);
        let lat = std::f64::consts::FRAC_PI_2 - 2.0 * (rho / self.rf).powf(1.0 / self.cone).atan();
        let lon = self.lov_deg + (theta / self.cone).to_degrees();
        (lat.to_degrees(), lon.rem_euclid(360.0))
    }

    /// Index into a message's values of the grid point grib.py would
    /// pick for (lat, lon), or None if the point is off the grid.
    ///
    /// grib.py asks a KD-tree over (lat, lon % 360) in float32 degrees,
    /// so "nearest" there is measured in degrees, with a degree of
    /// longitude counting as much as one of latitude. That isn't always
    /// the grid point nearest on the ground, which is the one nearest in
    /// projected coordinates. To return the same point as Python, the
    /// projection only finds the neighbourhood; the 3x3 points around it
    /// are then compared the way the KD-tree compares them.
    fn nearest_index(&self, lat_deg: f64, lon_deg: f64) -> Option<NearestPoint> {
        let (fi, fj) = self.fractional_ij(lat_deg, lon_deg);
        let (ci, cj) = (fi.round(), fj.round());
        if ci < 0.0 || cj < 0.0 || ci >= self.nx as f64 || cj >= self.ny as f64 {
            return None;
        }
        let (ci, cj) = (ci as usize, cj as usize);
        let lon_deg = lon_deg.rem_euclid(360.0);
        let mut best: Option<(f64, NearestPoint)> = None;
        for j in cj.saturating_sub(1)..=(cj + 1).min(self.ny - 1) {
            for i in ci.saturating_sub(1)..=(ci + 1).min(self.nx - 1) {
                let (lat, lon) = self.lat_lon(i, j);
                let (lat, lon) = (lat as f32, lon as f32);
                let distance = (f64::from(lat) - lat_deg).hypot(f64::from(lon) - lon_deg);
                if best.as_ref().is_none_or(|(d, _)| distance < *d) {
                    let point = NearestPoint {
                        index: j * self.nx + i,
                        lat,
                        lon,
                        nearest_on_ground: j * self.nx + i == cj * self.nx + ci,
                    };
                    best = Some((distance, point));
                }
            }
        }
        best.map(|(_, point)| point)
    }
}

#[derive(Debug, Clone, PartialEq)]
struct NearestPoint {
    index: usize,
    lat: f32,
    lon: f32,
    /// Whether this is also the nearest point in projected coordinates.
    nearest_on_ground: bool,
}

/// eccodes' shortName for the parameters grib.py asks for, so the output
/// keys match Python's. Anything else is named by its GRIB2 numbers.
fn short_name(discipline: u8, category: u8, number: u8, surface_type: u8) -> String {
    let height_above_ground = surface_type == 103;
    let name = match (discipline, category, number) {
        (0, 0, 0) if height_above_ground => "2t",
        (0, 0, 0) => "t",
        (0, 0, 6) if height_above_ground => "2d",
        (0, 0, 6) => "dpt",
        (0, 1, 0) => "q",
        (0, 2, 2) if height_above_ground => "10u",
        (0, 2, 2) => "u",
        (0, 2, 3) if height_above_ground => "10v",
        (0, 2, 3) => "v",
        (0, 3, 0) if surface_type == 1 => "sp",
        (0, 3, 0) => "pres",
        (0, 3, 5) if surface_type == 1 => "orog",
        (0, 3, 5) => "gh",
        _ => return format!("d{discipline}c{category}n{number}"),
    };
    name.to_string()
}

/// (offset, length) of every GRIB2 message in the file, from each one's
/// Section 0.
fn message_spans(file: &File) -> Result<Vec<(u64, usize)>> {
    let file_len = file.metadata()?.len();
    let mut spans = Vec::new();
    let mut offset = 0;
    while offset < file_len {
        let mut header = [0u8; 16];
        file.read_exact_at(&mut header, offset)?;
        if &header[..4] != b"GRIB" || header[7] != 2 {
            return Err(format!("no GRIB2 message at byte {offset}").into());
        }
        let len = u64::from_be_bytes(header[8..16].try_into().unwrap());
        spans.push((offset, len as usize));
        offset += len;
    }
    Ok(spans)
}

/// One decoded message, reduced to the wanted points.
struct Extracted {
    key: String,
    grid: LambertGrid,
    is_wind: bool,
    values: Vec<f32>,
}

/// Which code unpacks the values.
#[derive(Clone, Copy)]
enum Decoder {
    /// grib2.rs in this crate.
    Own,
    /// The `grib` crate, kept as a cross-check and for comparing speed.
    GribCrate,
}

/// Read and decode one message, keeping only the values at the points
/// `locate` picks on its grid. `locate` returns value indices in the
/// caller's point order.
fn extract(
    file: &File,
    (offset, len): (u64, usize),
    decoder: Decoder,
    locate: &(dyn Fn(&LambertGrid) -> Result<Vec<usize>> + Sync),
) -> Result<Extracted> {
    let mut bytes = vec![0u8; len];
    file.read_exact_at(&mut bytes, offset)?;
    let message = grib2::Message::parse(&bytes)?;
    let grid = LambertGrid::from_section3(&message.grid[5..])?;
    let product = message.product()?;
    // eccodes' `level`: hPa on isobaric surfaces, otherwise the surface's
    // own value (hybrid level number, metres above ground, 0 at the
    // surface).
    let level = match product.surface_type {
        1 => 0.0,
        100 => product.surface_value / 100.0,
        _ => product.surface_value,
    };
    let name = short_name(
        message.discipline,
        product.category,
        product.number,
        product.surface_type,
    );
    let is_wind = message.discipline == 0
        && product.category == 2
        && (product.number == 2 || product.number == 3);

    // The decoders want ascending indices; `order` maps back to the
    // caller's point order.
    let indices = locate(&grid)?;
    let mut order: Vec<usize> = (0..indices.len()).collect();
    order.sort_unstable_by_key(|&n| indices[n]);
    let sorted: Vec<usize> = order.iter().map(|&n| indices[n]).collect();
    let mut sorted_values = vec![f32::NAN; sorted.len()];
    match decoder {
        Decoder::Own => message.values_at(&sorted, &mut sorted_values)?,
        Decoder::GribCrate => {
            let grib2 = grib::from_bytes(&bytes)?;
            let (_, submessage) = grib2.iter().next().ok_or("empty GRIB2 message")?;
            let decoder = grib::Grib2SubmessageDecoder::from(submessage)?;
            // Walk the decoded values once, picking out the wanted ones
            // as they pass, so the full field is never collected.
            let mut decoded = decoder.dispatch()?.enumerate().peekable();
            for (&index, out) in sorted.iter().zip(&mut sorted_values) {
                // peek, not next: two sites can share a grid point.
                while decoded.next_if(|(n, _)| *n < index).is_some() {}
                *out = decoded.peek().ok_or("fewer values than grid points")?.1;
            }
        }
    }
    let mut values = vec![f32::NAN; indices.len()];
    for (&n, value) in order.iter().zip(sorted_values) {
        values[n] = value;
    }

    Ok(Extracted {
        key: format!("{name}_{}", level.round() as i64),
        grid,
        is_wind,
        values,
    })
}

/// Sites from sites.tsv: a name (which may contain single spaces), then
/// latitude and longitude as decimals, as telegram_bot.load_sites reads it.
fn read_sites(path: &str) -> Result<Vec<(String, f64, f64)>> {
    let is_decimal = |token: &str| token.contains('.') && token.parse::<f64>().is_ok();
    let mut sites = Vec::new();
    for line in std::fs::read_to_string(path)?.lines() {
        let tokens: Vec<&str> = line.split_whitespace().collect();
        let Some(first) = (1..tokens.len().saturating_sub(1))
            .find(|&n| is_decimal(tokens[n]) && is_decimal(tokens[n + 1]))
        else {
            continue;
        };
        sites.push((
            tokens[..first].join(" "),
            tokens[first].parse()?,
            tokens[first + 1].parse()?,
        ));
    }
    Ok(sites)
}

fn run() -> Result<()> {
    let mut grib_path = None;
    let mut sites = Vec::new();
    let mut threads = None;
    let mut decoder = Decoder::Own;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let mut value = || args.next().ok_or(format!("{arg} needs a value"));
        match arg.as_str() {
            "--sites" => sites.extend(read_sites(&value()?)?),
            "--point" => {
                let value = value()?;
                let (lat, lon) = value.split_once(',').ok_or("--point takes LAT,LON")?;
                sites.push((value.clone(), lat.parse()?, lon.parse()?));
            }
            "--threads" => threads = Some(value()?.parse::<usize>()?),
            "--decoder" => {
                decoder = match value()?.as_str() {
                    "own" => Decoder::Own,
                    "crate" => Decoder::GribCrate,
                    other => return Err(format!("unknown decoder {other}").into()),
                }
            }
            _ if grib_path.is_none() && !arg.starts_with('-') => grib_path = Some(arg),
            _ => return Err(format!("unexpected argument {arg}").into()),
        }
    }
    let (Some(grib_path), false) = (grib_path, sites.is_empty()) else {
        return Err(
            "usage: grib-profile SUBSET.grib2 (--sites sites.tsv | --point LAT,LON)... \
             [--threads N] [--decoder own|crate]"
                .into(),
        );
    };
    let threads = match threads {
        Some(threads) => threads.max(1),
        None => std::thread::available_parallelism().map_or(1, |n| n.get()),
    };

    let file = File::open(&grib_path)?;
    let spans = message_spans(&file)?;

    let locate_points = |grid: &LambertGrid| -> Result<Vec<NearestPoint>> {
        sites
            .iter()
            .map(|(name, lat, lon)| {
                grid.nearest_index(*lat, *lon)
                    .ok_or_else(|| format!("{name} is off the grid").into())
            })
            .collect()
    };
    let locate = |grid: &LambertGrid| -> Result<Vec<usize>> {
        Ok(locate_points(grid)?
            .into_iter()
            .map(|point| point.index)
            .collect())
    };
    // Each worker takes the next message nobody has started, so at most
    // one message per worker is in memory at a time.
    let next = AtomicUsize::new(0);
    let worker = || {
        let mut done = Vec::new();
        loop {
            let n = next.fetch_add(1, Ordering::Relaxed);
            let Some(&span) = spans.get(n) else { break };
            done.push((n, extract(&file, span, decoder, &locate)));
        }
        done
    };
    let mut extracted: Vec<_> = std::thread::scope(|scope| {
        let workers: Vec<_> = (0..threads.min(spans.len()))
            .map(|_| scope.spawn(worker))
            .collect();
        workers
            .into_iter()
            .flat_map(|w| w.join().expect("worker panicked"))
            .collect()
    });
    // Back into file order, which decides which message wins a repeated key.
    extracted.sort_unstable_by_key(|(n, _)| *n);
    let extracted: Vec<Extracted> = extracted
        .into_iter()
        .map(|(_, message)| message)
        .collect::<Result<_>>()?;

    let grid = &extracted.first().ok_or("no messages")?.grid;
    if extracted.iter().any(|message| message.grid != *grid) {
        // grib.py assumes this too: it reads lat/lon from the first
        // message only.
        return Err("messages are not all on the same grid".into());
    }
    let has_wind = extracted.iter().any(|message| message.is_wind);
    let points = locate_points(grid)?;
    let moved = points
        .iter()
        .filter(|point| !point.nearest_on_ground)
        .count();
    if moved > 0 {
        eprintln!(
            "{moved} of {} points: nearest in degrees (as grib.py) is not the nearest on the ground",
            points.len()
        );
    }

    let points: Vec<_> = sites
        .iter()
        .zip(&points)
        .enumerate()
        .map(|(n, ((name, _, _), point))| {
            // Last one wins on a repeated key, as in Python's dict.
            let values: BTreeMap<&str, f32> = extracted
                .iter()
                .map(|m| (m.key.as_str(), m.values[n]))
                .collect();
            json!({
                "name": name,
                "index": point.index,
                "lat": point.lat,
                "lon": point.lon,
                "values": values,
            })
        })
        .collect();
    // Python only reads the projection off a wind message, and leaves
    // both None in a subset without one.
    let output = json!({
        "cone": has_wind.then_some(grid.cone),
        "lov": has_wind.then_some(grid.lov_deg),
        "messages": extracted.len(),
        "points": points,
    });
    println!("{output}");
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("grib-profile: {error}");
            ExitCode::FAILURE
        }
    }
}
