//! The grid a message is on, whichever kind it is, and which of its
//! points is nearest a place.

use crate::Result;
use crate::grib2::unsigned;
use crate::lambert::LambertGrid;
use crate::latlon::LatLonGrid;

/// A grid point: its index into a message's values, and where it is.
#[derive(Debug, Clone, PartialEq)]
pub struct GridPoint {
    pub index: usize,
    pub lat: f64,
    /// 0 to 360, as the grid itself is defined.
    pub lon: f64,
}

/// Great-circle distance between two points in degrees, as an angle in
/// radians (haversine).
pub fn angular_distance(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let (lat1, lat2) = (lat1.to_radians(), lat2.to_radians());
    let half_dlat = (lat2 - lat1) / 2.0;
    let half_dlon = (lon2 - lon1).to_radians() / 2.0;
    let h = half_dlat.sin().powi(2) + lat1.cos() * lat2.cos() * half_dlon.sin().powi(2);
    2.0 * h.sqrt().asin()
}

/// The nearest of `candidates` (each an index and its lat/lon) to a
/// place, by great-circle distance. The first wins a tie.
pub fn nearest_of(
    lat_deg: f64,
    lon_deg: f64,
    candidates: impl Iterator<Item = GridPoint>,
) -> Option<GridPoint> {
    let mut best: Option<(f64, GridPoint)> = None;
    for point in candidates {
        let distance = angular_distance(lat_deg, lon_deg, point.lat, point.lon);
        if best.as_ref().is_none_or(|(d, _)| distance < *d) {
            best = Some((distance, point));
        }
    }
    best.map(|(_, point)| point)
}

/// The grids these files come on: Lambert Conformal (HRRR, RRFS, URMA)
/// and regular latitude/longitude (GFS).
#[derive(Debug, Clone, PartialEq)]
pub enum Grid {
    Lambert(LambertGrid),
    LatLon(LatLonGrid),
}

impl Grid {
    /// From a whole Section 3, header included.
    pub fn from_section3(section: &[u8]) -> Result<Self> {
        let template = section.get(12..14).map(unsigned).ok_or("short section 3")?;
        match template {
            0 => Ok(Grid::LatLon(LatLonGrid::from_section3(section)?)),
            30 => Ok(Grid::Lambert(LambertGrid::from_section3(section)?)),
            _ => Err(format!("unsupported grid definition template 3.{template}").into()),
        }
    }

    /// The grid point nearest (lat, lon) on the ground, or None if the
    /// place is off the grid.
    pub fn nearest(&self, lat_deg: f64, lon_deg: f64) -> Option<GridPoint> {
        match self {
            Grid::Lambert(grid) => grid.nearest(lat_deg, lon_deg),
            Grid::LatLon(grid) => grid.nearest(lat_deg, lon_deg),
        }
    }

    /// The cone constant and central meridian of a Lambert grid, for
    /// turning its grid-relative winds to true north. A latitude/longitude
    /// grid's winds already are, so it has neither.
    pub fn cone_and_lov(&self) -> Option<(f64, f64)> {
        match self {
            Grid::Lambert(grid) => Some((grid.cone, grid.lov_deg)),
            Grid::LatLon(_) => None,
        }
    }
}
