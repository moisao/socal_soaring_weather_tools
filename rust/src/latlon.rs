//! The regular latitude/longitude grid GFS is on, and which of its points
//! is nearest a place.

use crate::Result;
use crate::grib2::{signed, unsigned};
use crate::grid::{GridPoint, nearest_of};

/// A regular latitude/longitude grid (GRIB2 grid definition template
/// 3.0), stored a row of constant latitude at a time, west to east.
#[derive(Debug, Clone, PartialEq)]
pub struct LatLonGrid {
    pub nx: usize,
    pub ny: usize,
    /// Of the first grid point, in degrees.
    la1: f64,
    lo1: f64,
    /// Degrees between columns, positive.
    di: f64,
    /// Degrees between rows: negative where the rows run north to south.
    dj: f64,
    /// Whether the columns go all the way round, so that the last is
    /// next to the first.
    wraps: bool,
}

impl LatLonGrid {
    /// From a whole Section 3, header included, so `section[n - 1]` is
    /// the octet the GRIB2 tables call n.
    pub fn from_section3(section: &[u8]) -> Result<Self> {
        let octets = |first: usize, last: usize| &section[first - 1..last];
        if section.len() < 72 || unsigned(octets(13, 14)) != 0 {
            return Err("not a latitude/longitude grid (template 3.0)".into());
        }
        let basic_angle = unsigned(octets(39, 42));
        if basic_angle != 0 && basic_angle != 0xffff_ffff {
            // Angles would be in units other than millionths of a degree.
            return Err(format!("unsupported basic angle {basic_angle}").into());
        }
        let scanning_mode = section[71];
        if scanning_mode & !0b0100_0000 != 0 {
            // Anything but rows west to east stores points in a different
            // order than the index arithmetic below assumes.
            return Err(format!("unsupported scanning mode {scanning_mode:#010b}").into());
        }
        let south_to_north = scanning_mode & 0b0100_0000 != 0;

        let (nx, ny) = (
            unsigned(octets(31, 34)) as usize,
            unsigned(octets(35, 38)) as usize,
        );
        if nx == 0 || ny == 0 {
            return Err("empty grid".into());
        }
        let degrees = |first| signed(octets(first, first + 3)) as f64 * 1e-6;
        let (la1, lo1) = (degrees(47), degrees(51));
        let (la2, lo2) = (degrees(56), degrees(60));
        // The increments are only there if the resolution flags say so;
        // otherwise they follow from the first and last points.
        let flags = section[54];
        let di = if flags & 0b0010_0000 != 0 {
            unsigned(octets(64, 67)) as f64 * 1e-6
        } else {
            (lo2 - lo1).rem_euclid(360.0) / (nx.max(2) - 1) as f64
        };
        let dj = if flags & 0b0001_0000 != 0 {
            unsigned(octets(68, 71)) as f64 * 1e-6
        } else {
            (la2 - la1).abs() / (ny.max(2) - 1) as f64
        };
        if !(di > 0.0 && dj > 0.0) {
            return Err("empty grid".into());
        }
        Ok(LatLonGrid {
            nx,
            ny,
            la1,
            lo1,
            di,
            dj: if south_to_north { dj } else { -dj },
            wraps: (nx as f64 * di - 360.0).abs() < di * 1e-3,
        })
    }

    /// Grid point (i, j).
    fn point(&self, i: usize, j: usize) -> GridPoint {
        GridPoint {
            index: j * self.nx + i,
            lat: self.la1 + j as f64 * self.dj,
            lon: (self.lo1 + i as f64 * self.di).rem_euclid(360.0),
        }
    }

    /// The grid point nearest (lat, lon) on the ground, or None if the
    /// place is off the grid.
    ///
    /// Rounding gives the column outright, but not always the row: rows
    /// nearer a pole have their points closer together, so a place almost
    /// midway between two rows can be nearer a point of the further one.
    /// The points around the rounded one are compared by great-circle
    /// distance to settle it, as on the Lambert grid. The rounded column
    /// comes first, to win where they tie: at a pole the whole row is one
    /// place, and its winds are told apart only by their meridian.
    pub fn nearest(&self, lat_deg: f64, lon_deg: f64) -> Option<GridPoint> {
        let mut east = (lon_deg - self.lo1).rem_euclid(360.0);
        if east > 360.0 - self.di / 2.0 {
            // Just west of the first column, which is then the nearest.
            east -= 360.0;
        }
        let ci = (east / self.di).round();
        let ci = if self.wraps {
            ci.rem_euclid(self.nx as f64)
        } else {
            ci
        };
        let cj = ((lat_deg - self.la1) / self.dj).round();
        // Written so that NaN (a lat/lon that isn't one) is off the grid.
        if !(ci >= 0.0 && cj >= 0.0 && ci < self.nx as f64 && cj < self.ny as f64) {
            return None;
        }
        let (ci, cj) = (ci as usize, cj as usize);
        let columns: Vec<usize> = if self.wraps {
            [0, self.nx - 1, 1]
                .iter()
                .map(|step| (ci + step) % self.nx)
                .collect()
        } else {
            let neighbours = [ci.checked_sub(1), Some(ci + 1).filter(|&i| i < self.nx)];
            std::iter::once(ci)
                .chain(neighbours.into_iter().flatten())
                .collect()
        };
        let rows = cj.saturating_sub(1)..=(cj + 1).min(self.ny - 1);
        let candidates = rows.flat_map(|j| columns.iter().map(move |&i| self.point(i, j)));
        nearest_of(lat_deg, lon_deg, candidates)
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;

    /// Section 3 of a GFS 0.25 degree message: 1440 x 721, from 90N 0E,
    /// rows north to south.
    pub fn gfs_section3() -> Vec<u8> {
        let mut s = vec![0u8; 72];
        s[..4].copy_from_slice(&72u32.to_be_bytes());
        s[4] = 3;
        s[14] = 6;
        let mut put = |octet: usize, value: u32| {
            s[octet - 1..octet + 3].copy_from_slice(&value.to_be_bytes());
        };
        put(31, 1440);
        put(35, 721);
        put(47, 90_000_000);
        put(51, 0);
        put(56, 90_000_000 | 0x8000_0000); // 90S, sign and magnitude
        put(60, 359_750_000);
        put(64, 250_000);
        put(68, 250_000);
        s[54] = 0b0011_0000;
        s
    }

    fn gfs() -> LatLonGrid {
        LatLonGrid::from_section3(&gfs_section3()).unwrap()
    }

    #[test]
    fn nearest_rounds_to_the_quarter_degree() {
        let little_black = gfs()
            .nearest(32.987952027309035, -117.12235348316403)
            .unwrap();
        // 33N is row (90 - 33) / 0.25, 117W = 243E is column 243 / 0.25.
        assert_eq!(little_black.index, 228 * 1440 + 972);
        assert_eq!((little_black.lat, little_black.lon), (33.0, 243.0));
    }

    #[test]
    fn wraps_at_the_greenwich_meridian() {
        let grid = gfs();
        assert_eq!(grid.nearest(0.0, -0.1).unwrap().index, 360 * 1440);
        assert_eq!(grid.nearest(0.0, 359.8).unwrap().index, 360 * 1440 + 1439);
        assert_eq!(grid.nearest(0.0, 0.1).unwrap().index, 360 * 1440);
        // The poles are rows like any other, and nothing is off the grid.
        let north_pole = grid.nearest(89.9, 10.0).unwrap();
        assert_eq!((north_pole.lat, north_pole.lon), (90.0, 10.0));
        assert_eq!(grid.nearest(-90.0, 10.0).unwrap().lat, -90.0);
        assert_eq!(grid.nearest(f64::NAN, 10.0), None);
    }

    /// A place a hair nearer the southern of two rows in latitude, and
    /// midway between columns, is nearer the northern row's points over
    /// the ground, since they are closer together.
    #[test]
    fn nearest_is_by_ground_distance() {
        let grid = gfs();
        let point = grid.nearest(45.12499, 0.125).unwrap();
        assert_eq!(point.lat, 45.25);
        assert_eq!(grid.nearest(45.12499, 0.0).unwrap().lat, 45.0);
    }

    #[test]
    fn a_regional_grid_has_edges() {
        let mut s = gfs_section3();
        // 10 x 10 from 30N 240E, rows south to north.
        s[30..34].copy_from_slice(&10u32.to_be_bytes());
        s[34..38].copy_from_slice(&10u32.to_be_bytes());
        s[46..50].copy_from_slice(&30_000_000u32.to_be_bytes());
        s[50..54].copy_from_slice(&240_000_000u32.to_be_bytes());
        s[71] = 0b0100_0000;
        let grid = LatLonGrid::from_section3(&s).unwrap();
        assert_eq!(grid.nearest(30.3, -119.9).unwrap().index, 10);
        assert_eq!(grid.nearest(30.0, -120.1).unwrap().index, 0);
        assert_eq!(grid.nearest(30.0, -120.2), None);
        assert_eq!(grid.nearest(32.4, -117.8), None);
        assert_eq!(grid.nearest(29.8, -119.0), None);
    }
}
