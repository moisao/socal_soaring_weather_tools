//! The Lambert Conformal grid HRRR, RRFS and URMA are on, and which of
//! its points is nearest a place.

use std::f64::consts::{FRAC_PI_2, FRAC_PI_4};

use crate::Result;
use crate::grib2::{signed, unsigned};
use crate::grid::{GridPoint, nearest_of};

/// A Lambert Conformal grid (GRIB2 grid definition template 3.30) on a
/// spherical earth.
#[derive(Debug, Clone, PartialEq)]
pub struct LambertGrid {
    pub nx: usize,
    pub ny: usize,
    /// Cone constant, as grib.py's `_lcc_cone_constant`.
    pub cone: f64,
    pub lov_deg: f64,
    dx: f64,
    dy: f64,
    /// R * F of the usual LCC formulas.
    rf: f64,
    /// Projected coordinates of the first grid point, with the origin at
    /// the cone's apex.
    x0: f64,
    y0: f64,
}

impl LambertGrid {
    /// From a whole Section 3, header included, so `section[n - 1]` is
    /// the octet the GRIB2 tables call n.
    pub fn from_section3(section: &[u8]) -> Result<Self> {
        let octets = |first: usize, last: usize| &section[first - 1..last];
        if section.len() < 81 || unsigned(octets(13, 14)) != 30 {
            return Err("not a Lambert Conformal grid (template 3.30)".into());
        }
        let radius = match section[14] {
            0 => 6_367_470.0,
            1 => unsigned(octets(17, 20)) as f64 / 10f64.powi(i32::from(section[15])),
            6 => 6_371_229.0,
            8 => 6_371_200.0,
            shape => return Err(format!("unsupported shape of the earth {shape}").into()),
        };
        let scanning_mode = section[64];
        if scanning_mode != 0b0100_0000 {
            // Anything else stores points in a different order than the
            // index arithmetic below assumes.
            return Err(format!("unsupported scanning mode {scanning_mode:#010b}").into());
        }
        let degrees = |first| signed(octets(first, first + 3)) as f64 * 1e-6;
        let (la1, lo1) = (degrees(39), degrees(43));
        let lov_deg = degrees(52);
        let (latin1, latin2) = (degrees(66), degrees(70));

        let half_colatitude_tan = |lat_deg: f64| (FRAC_PI_4 - lat_deg.to_radians() / 2.0).tan();
        let cone = if (latin1 - latin2).abs() < 1e-6 {
            latin1.to_radians().sin()
        } else {
            (latin1.to_radians().cos() / latin2.to_radians().cos()).ln()
                / (half_colatitude_tan(latin1) / half_colatitude_tan(latin2)).ln()
        };
        let rf =
            radius * latin1.to_radians().cos() / (cone * half_colatitude_tan(latin1).powf(cone));

        let mut grid = LambertGrid {
            nx: unsigned(octets(31, 34)) as usize,
            ny: unsigned(octets(35, 38)) as usize,
            cone,
            lov_deg,
            dx: unsigned(octets(56, 59)) as f64 * 1e-3,
            dy: unsigned(octets(60, 63)) as f64 * 1e-3,
            rf,
            x0: 0.0,
            y0: 0.0,
        };
        if grid.nx == 0 || grid.ny == 0 || grid.dx == 0.0 || grid.dy == 0.0 {
            return Err("empty grid".into());
        }
        (grid.x0, grid.y0) = grid.project(la1, lo1);
        Ok(grid)
    }

    /// (x, y) in metres from the cone's apex.
    fn project(&self, lat_deg: f64, lon_deg: f64) -> (f64, f64) {
        let rho = self.rf
            * (FRAC_PI_4 - lat_deg.to_radians() / 2.0)
                .tan()
                .powf(self.cone);
        let dlon = (lon_deg - self.lov_deg + 180.0).rem_euclid(360.0) - 180.0;
        let theta = self.cone * dlon.to_radians();
        (rho * theta.sin(), -rho * theta.cos())
    }

    /// Lat/lon of grid point (i, j).
    fn lat_lon(&self, i: usize, j: usize) -> (f64, f64) {
        let x = self.x0 + i as f64 * self.dx;
        let y = self.y0 + j as f64 * self.dy;
        let rho = x.hypot(y).copysign(self.cone);
        let theta = x.atan2(-y);
        let lat = FRAC_PI_2 - 2.0 * (rho / self.rf).powf(1.0 / self.cone).atan();
        let lon = self.lov_deg + (theta / self.cone).to_degrees();
        (lat.to_degrees(), lon.rem_euclid(360.0))
    }

    /// The grid point nearest (lat, lon) on the ground, or None if the
    /// place is off the grid.
    ///
    /// The projection gives the grid cell directly, with no search. The
    /// map's scale changes slightly across a cell, though, so the 3x3
    /// points around it are compared by great-circle distance to settle
    /// the rare place that sits almost midway between two points.
    pub fn nearest(&self, lat_deg: f64, lon_deg: f64) -> Option<GridPoint> {
        let (x, y) = self.project(lat_deg, lon_deg);
        let (ci, cj) = (
            ((x - self.x0) / self.dx).round(),
            ((y - self.y0) / self.dy).round(),
        );
        // Written so that NaN (a lat/lon that isn't one) is off the grid.
        if !(ci >= 0.0 && cj >= 0.0 && ci < self.nx as f64 && cj < self.ny as f64) {
            return None;
        }
        let (ci, cj) = (ci as usize, cj as usize);
        let rows = cj.saturating_sub(1)..=(cj + 1).min(self.ny - 1);
        let columns = ci.saturating_sub(1)..=(ci + 1).min(self.nx - 1);
        let candidates = rows.flat_map(|j| {
            columns.clone().map(move |i| {
                let (lat, lon) = self.lat_lon(i, j);
                GridPoint {
                    index: j * self.nx + i,
                    lat,
                    lon,
                }
            })
        });
        nearest_of(lat_deg, lon_deg, candidates)
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;

    /// Section 3 of an HRRR message (the grid RRFS is served on too).
    pub fn hrrr_section3() -> Vec<u8> {
        let mut s = vec![0u8; 81];
        s[..4].copy_from_slice(&81u32.to_be_bytes());
        s[4] = 3;
        s[12..14].copy_from_slice(&30u16.to_be_bytes());
        s[14] = 6;
        let mut put = |octet: usize, value: u32| {
            s[octet - 1..octet + 3].copy_from_slice(&value.to_be_bytes());
        };
        put(31, 1799);
        put(35, 1059);
        put(39, 21_138_123);
        put(43, 237_280_472);
        put(48, 38_500_000);
        put(52, 262_500_000);
        put(56, 3_000_000);
        put(60, 3_000_000);
        put(66, 38_500_000);
        put(70, 38_500_000);
        s[64] = 0b0100_0000;
        s
    }

    fn hrrr() -> LambertGrid {
        LambertGrid::from_section3(&hrrr_section3()).unwrap()
    }

    #[test]
    fn cone_constant_and_first_point() {
        let grid = hrrr();
        assert!((grid.cone - 0.622_514_636_637_619_5).abs() < 1e-15);
        let (lat, lon) = grid.lat_lon(0, 0);
        assert!((lat - 21.138123).abs() < 1e-9 && (lon - 237.280472).abs() < 1e-9);
    }

    /// Indices and coordinates eccodes gives for these sites' points.
    #[test]
    fn nearest_matches_eccodes() {
        let grid = hrrr();
        let little_black = grid
            .nearest(32.987952027309035, -117.12235348316403)
            .unwrap();
        assert_eq!(little_black.index, 701_901);
        assert!((little_black.lat - 32.999664).abs() < 1e-5);
        assert!((little_black.lon - 242.875671).abs() < 1e-5);
        let torrey = grid
            .nearest(32.890234820022194, -117.25180563384053)
            .unwrap();
        assert_eq!(torrey.index, 696_499);
    }

    /// Two sites where the nearest point in degrees of lat/lon (what a
    /// KD-tree over lat/lon finds) is one row off the nearest on the
    /// ground.
    #[test]
    fn nearest_is_by_ground_distance() {
        let grid = hrrr();
        let elsinore = grid
            .nearest(33.628268786158195, -117.37011729863373)
            .unwrap();
        assert_eq!(elsinore.index, 414 * 1799 + 289);
        let skyport = grid.nearest(34.4809515, -119.6846529).unwrap();
        assert_eq!(skyport.index, 462 * 1799 + 226);
    }

    #[test]
    fn off_the_grid_is_none() {
        let grid = hrrr();
        assert_eq!(grid.nearest(21.3, -157.8), None);
        assert_eq!(grid.nearest(f64::NAN, -117.0), None);
        // The corners themselves are on it.
        let (lat, lon) = grid.lat_lon(grid.nx - 1, grid.ny - 1);
        assert_eq!(grid.nearest(lat, lon).unwrap().index, grid.nx * grid.ny - 1);
    }
}
