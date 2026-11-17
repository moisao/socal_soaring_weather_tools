//! Values at the grid points nearest some places, from a GRIB2 subset as
//! grib.py and point_forecast.py cache them (HRRR, RRFS, URMA, GFS), as
//! JSON on stdout. grib.py's `_decode_grib_fields` runs this for values
//! by name, and its `decode_messages` (with `--messages`) for values by
//! where their message is in the file.
//!
//! Messages are decoded one at a time, straight to the wanted points, so
//! nothing the size of the grid is ever held: about half a second and
//! under 20 MB for a 185-message subset on a Raspberry Pi 5. The grid
//! point comes from inverting the grid's projection, with no lat/lon
//! arrays to search.

use std::error::Error;
use std::fmt::Write as _;
use std::fs::File;
use std::os::unix::fs::FileExt;
use std::process::ExitCode;
use std::sync::atomic::{AtomicUsize, Ordering};

use grid::{Grid, GridPoint};

mod grib2;
mod grid;
mod lambert;
mod latlon;

type Result<T> = std::result::Result<T, Box<dyn Error + Send + Sync>>;

const USAGE: &str = "usage: grib-profile SUBSET.grib2 (--point LAT,LON | --sites sites.tsv)... \
                     [--messages N,N...] [--threads N]";

/// eccodes' shortName for the parameters grib.py asks for, which is what
/// its callers look values up by. Anything else is named by its GRIB2
/// numbers.
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

/// (offset, length) of the GRIB2 messages in the file, from each one's
/// Section 0: all of them, or only as many as `count`, leaving the rest
/// of the file unread.
fn message_spans(file: &File, count: Option<usize>) -> Result<Vec<(u64, usize)>> {
    let file_len = file.metadata()?.len();
    let mut spans = Vec::new();
    let mut offset = 0;
    while offset < file_len && count.is_none_or(|count| spans.len() < count) {
        let mut header = [0u8; 16];
        file.read_exact_at(&mut header, offset)?;
        if &header[..4] != b"GRIB" || header[7] != 2 {
            return Err(format!("no GRIB2 message at byte {offset}").into());
        }
        let len = u64::from_be_bytes(header[8..16].try_into().unwrap());
        if len < 16 || offset + len > file_len {
            return Err(format!("truncated GRIB2 message at byte {offset}").into());
        }
        spans.push((offset, len as usize));
        offset += len;
    }
    Ok(spans)
}

/// One decoded message, reduced to the wanted points.
struct Extracted {
    /// "shortName_level", e.g. "t_850"; None where messages are asked
    /// for by position, and what they hold is the caller's business.
    key: Option<String>,
    grid: Grid,
    values: Vec<f64>,
}

/// A message's "shortName_level".
fn key(message: &grib2::Message) -> Result<String> {
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
    Ok(format!("{name}_{}", level.round() as i64))
}

/// Read and decode one message, keeping only its values at `places`
/// (lat, lon), in their order.
fn extract(
    file: &File,
    (offset, len): (u64, usize),
    places: &[(f64, f64)],
    named: bool,
) -> Result<Extracted> {
    let mut bytes = vec![0u8; len];
    file.read_exact_at(&mut bytes, offset)?;
    let message = grib2::Message::parse(&bytes)?;
    let grid = Grid::from_section3(message.grid)?;
    let key = named.then(|| key(&message)).transpose()?;

    let indices = locate(&grid, places)?.into_iter().map(|point| point.index);
    let indices: Vec<usize> = indices.collect();
    // The decoder wants ascending indices; `order` maps back to the
    // order of `places`.
    let mut order: Vec<usize> = (0..indices.len()).collect();
    order.sort_unstable_by_key(|&n| indices[n]);
    let sorted: Vec<usize> = order.iter().map(|&n| indices[n]).collect();
    let mut sorted_values = vec![f64::NAN; sorted.len()];
    message.values_at(&sorted, &mut sorted_values)?;
    let mut values = vec![f64::NAN; indices.len()];
    for (&n, value) in order.iter().zip(sorted_values) {
        values[n] = value;
    }

    Ok(Extracted { key, grid, values })
}

fn locate(grid: &Grid, places: &[(f64, f64)]) -> Result<Vec<GridPoint>> {
    places
        .iter()
        .map(|&(lat, lon)| {
            grid.nearest(lat, lon)
                .ok_or_else(|| format!("{lat},{lon} is off the grid").into())
        })
        .collect()
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

/// A JSON string literal.
fn json_string(text: &str) -> String {
    let mut out = String::from('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if u32::from(c) < 0x20 => write!(out, "\\u{:04x}", u32::from(c)).unwrap(),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// A JSON number, or null for what JSON can't write.
fn json_number(value: f64) -> String {
    if value.is_finite() {
        format!("{value}")
    } else {
        "null".to_string()
    }
}

/// The output: a Lambert grid's cone constant and central meridian (for
/// turning its grid-relative winds to true north; null on a grid that is
/// north-up already), and for each place its name, grid point and values:
/// `{"shortName_level": value}`, narrowed to f32 as grib.py has always
/// had them, or with `by_position` a list of the messages' values in the
/// order they were asked for, as the doubles they were unpacked to.
fn to_json(
    grid: &Grid,
    names: &[String],
    points: &[GridPoint],
    extracted: &[Extracted],
    by_position: bool,
) -> String {
    let (cone, lov) = grid.cone_and_lov().unwrap_or((f64::NAN, f64::NAN));
    let mut out = format!(
        "{{\"cone\":{},\"lov\":{},\"messages\":{},\"points\":[",
        json_number(cone),
        json_number(lov),
        extracted.len()
    );
    for (n, (name, point)) in names.iter().zip(points).enumerate() {
        if n > 0 {
            out.push(',');
        }
        write!(
            out,
            "{{\"name\":{},\"index\":{},\"lat\":{},\"lon\":{},\"values\":",
            json_string(name),
            point.index,
            json_number(point.lat),
            json_number(point.lon)
        )
        .unwrap();
        if by_position {
            let values: Vec<String> = extracted
                .iter()
                .map(|message| json_number(message.values[n]))
                .collect();
            write!(out, "[{}]}}", values.join(",")).unwrap();
            continue;
        }
        out.push('{');
        // A key that comes up twice keeps its last value, as it would
        // in the dict this is parsed into.
        let mut written = 0;
        for (m, message) in extracted.iter().enumerate() {
            if extracted[m + 1..]
                .iter()
                .any(|later| later.key == message.key)
            {
                continue;
            }
            if written > 0 {
                out.push(',');
            }
            written += 1;
            // Narrowed, then widened again so the digits printed are the
            // f32's own, not the shortest decimal that rounds to it.
            let value = json_number(f64::from(message.values[n] as f32));
            let key = message.key.as_deref().unwrap_or_default();
            write!(out, "{}:{value}", json_string(key)).unwrap();
        }
        out.push_str("}}");
    }
    out.push_str("]}");
    out
}

fn run() -> Result<()> {
    let mut grib_path = None;
    let mut sites = Vec::new();
    let mut threads = None;
    let mut positions: Option<Vec<usize>> = None;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let mut value = || args.next().ok_or(format!("{arg} needs a value"));
        match arg.as_str() {
            "--version" => {
                println!("grib-profile {}", env!("CARGO_PKG_VERSION"));
                return Ok(());
            }
            "--sites" => sites.extend(read_sites(&value()?)?),
            "--point" => {
                let value = value()?;
                let (lat, lon) = value.split_once(',').ok_or("--point takes LAT,LON")?;
                sites.push((value.clone(), lat.parse()?, lon.parse()?));
            }
            "--messages" => {
                let list: std::result::Result<Vec<usize>, _> =
                    value()?.split(',').map(str::parse).collect();
                positions = Some(list.map_err(|e| format!("--messages takes N,N...: {e}"))?);
            }
            "--threads" => threads = Some(value()?.parse::<usize>()?),
            _ if grib_path.is_none() && !arg.starts_with('-') => grib_path = Some(arg),
            _ => return Err(format!("unexpected argument {arg}\n{USAGE}").into()),
        }
    }
    let (Some(grib_path), false) = (grib_path, sites.is_empty()) else {
        return Err(USAGE.into());
    };
    let threads = match threads {
        Some(threads) => threads.max(1),
        None => std::thread::available_parallelism().map_or(1, |n| n.get()),
    };
    let names: Vec<String> = sites.iter().map(|(name, _, _)| name.clone()).collect();
    let places: Vec<(f64, f64)> = sites.iter().map(|&(_, lat, lon)| (lat, lon)).collect();

    let file = File::open(&grib_path).map_err(|e| format!("{grib_path}: {e}"))?;
    // With --messages, only those, in the order given, and no more of
    // the file is looked at than it takes to find them: past its last
    // whole message a file still being appended to may hold anything.
    let spans = match &positions {
        None => message_spans(&file, None)?,
        Some(positions) => {
            let count = positions.iter().max().map_or(0, |last| last + 1);
            let spans = message_spans(&file, Some(count))?;
            let span = |&n: &usize| -> Result<(u64, usize)> {
                let span = spans.get(n).copied();
                Ok(span.ok_or_else(|| format!("no message {n}: the file holds {}", spans.len()))?)
            };
            positions.iter().map(span).collect::<Result<_>>()?
        }
    };

    // Each worker takes the next message nobody has started, so at most
    // one message per worker is in memory at a time.
    let next = AtomicUsize::new(0);
    let worker = || {
        let mut done = Vec::new();
        loop {
            let n = next.fetch_add(1, Ordering::Relaxed);
            let Some(&span) = spans.get(n) else { break };
            done.push((n, extract(&file, span, &places, positions.is_none())));
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
    // Back into the order asked for: the file's own, which decides which
    // message wins a repeated key, or that of --messages.
    extracted.sort_unstable_by_key(|(n, _)| *n);
    let extracted: Vec<Extracted> = extracted
        .into_iter()
        .map(|(_, message)| message)
        .collect::<Result<_>>()?;

    let grid = &extracted.first().ok_or("no messages")?.grid;
    if extracted.iter().any(|message| message.grid != *grid) {
        // One grid point per place is reported, so it has to be the
        // same point in every message.
        return Err("messages are not all on the same grid".into());
    }
    println!(
        "{}",
        to_json(
            grid,
            &names,
            &locate(grid, &places)?,
            &extracted,
            positions.is_some()
        )
    );
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_escapes_and_nulls() {
        assert_eq!(json_string("a\"b\\c\n"), "\"a\\\"b\\\\c\\u000a\"");
        assert_eq!(json_number(f64::NAN), "null");
        assert_eq!(json_number(f64::from(287.92f32)), "287.9200134277344");
    }

    #[test]
    fn names_follow_eccodes() {
        assert_eq!(short_name(0, 0, 0, 100), "t");
        assert_eq!(short_name(0, 0, 0, 103), "2t");
        assert_eq!(short_name(0, 3, 5, 1), "orog");
        assert_eq!(short_name(0, 3, 5, 105), "gh");
        assert_eq!(short_name(0, 3, 0, 1), "sp");
        assert_eq!(short_name(2, 0, 0, 1), "d2c0n0");
    }
}
