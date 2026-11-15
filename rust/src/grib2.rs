//! Just enough GRIB2 to read the messages grib.py caches: one field per
//! message, no bitmap, simple packing (data representation template 5.0)
//! or complex packing with spatial differencing (5.3).
//!
//! The unpacking follows NCEP's g2c `comunpack`, and the scaling follows
//! eccodes (double arithmetic, then narrowed to f32), so the values come
//! out bit-for-bit the same as grib.py's.

use crate::Result;

/// Sign-and-magnitude integer of any width up to 8 bytes, as GRIB2 stores
/// signed values.
fn signed(bytes: &[u8]) -> i64 {
    let magnitude = bytes.iter().enumerate().fold(0i64, |acc, (n, &b)| {
        (acc << 8) | i64::from(if n == 0 { b & 0x7f } else { b })
    });
    if bytes[0] & 0x80 != 0 {
        -magnitude
    } else {
        magnitude
    }
}

fn unsigned(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0, |acc, &b| (acc << 8) | u64::from(b))
}

/// `base` to the power `exponent` the way eccodes' `codes_power` does it,
/// by repeated multiplication or division, so scale factors round the
/// same way.
fn power(exponent: i64, base: f64) -> f64 {
    let mut result = 1.0;
    for _ in 0..exponent.unsigned_abs() {
        if exponent > 0 {
            result *= base
        } else {
            result /= base
        }
    }
    result
}

/// One message's sections, borrowed from its bytes. Each slice is a whole
/// section including its 5-byte header, so `section[n - 1]` is the
/// octet the GRIB2 tables call n.
pub struct Message<'a> {
    pub discipline: u8,
    pub grid: &'a [u8],
    product: &'a [u8],
    representation: &'a [u8],
    data: &'a [u8],
}

/// What a field is, from Section 4.
pub struct Product {
    pub category: u8,
    pub number: u8,
    /// Code table 4.5: 1 surface, 100 isobaric, 103 height above ground,
    /// 105 hybrid level.
    pub surface_type: u8,
    pub surface_value: f64,
}

impl<'a> Message<'a> {
    pub fn parse(bytes: &'a [u8]) -> Result<Self> {
        if bytes.len() < 20 || &bytes[..4] != b"GRIB" || bytes[7] != 2 {
            return Err("not a GRIB2 message".into());
        }
        let mut sections: [Option<&[u8]>; 8] = [None; 8];
        let mut offset = 16;
        while bytes.get(offset..offset + 4) != Some(b"7777") {
            let header = bytes
                .get(offset..offset + 5)
                .ok_or("truncated GRIB2 message")?;
            let len = unsigned(&header[..4]) as usize;
            let number = usize::from(header[4]);
            let section = bytes
                .get(offset..offset + len)
                .ok_or("truncated GRIB2 section")?;
            if len < 5 || number > 7 {
                return Err(format!("bad GRIB2 section {number} of {len} bytes").into());
            }
            if sections[number].replace(section).is_some() {
                return Err("more than one field in a message is not supported".into());
            }
            offset += len;
        }
        let section = |n: usize| sections[n].ok_or(format!("message has no section {n}"));
        if section(6)?.get(5) != Some(&255) {
            return Err("bitmaps are not supported".into());
        }
        Ok(Message {
            discipline: bytes[6],
            grid: section(3)?,
            product: section(4)?,
            representation: section(5)?,
            data: section(7)?,
        })
    }

    pub fn product(&self) -> Result<Product> {
        let s = self.product;
        let template = s.get(7..9).map(unsigned).ok_or("short section 4")?;
        // Templates 4.0 to 4.15 all begin the same way, as far as the
        // first fixed surface.
        if template > 15 || s.len() < 28 {
            return Err(format!("unsupported product definition template 4.{template}").into());
        }
        Ok(Product {
            category: s[9],
            number: s[10],
            surface_type: s[22],
            surface_value: signed(&s[24..28]) as f64 / 10f64.powi(signed(&s[23..24]) as i32),
        })
    }

    /// The field's values at `wanted` (indices into the grid, ascending),
    /// written to `out` in the same order. Decoding stops at the last
    /// wanted index and nothing is allocated, so the field as a whole is
    /// never held.
    pub fn values_at(&self, wanted: &[usize], out: &mut [f32]) -> Result<()> {
        let s = self.representation;
        if s.len() < 21 {
            return Err("short section 5".into());
        }
        let points = unsigned(&s[5..9]) as usize;
        let template = unsigned(&s[9..11]);
        if wanted.last().is_some_and(|&last| last >= points) {
            return Err("grid index past the end of the field".into());
        }
        let reference = f64::from(f32::from_be_bytes(s[11..15].try_into().unwrap()));
        let binary_scale = power(signed(&s[15..17]), 2.0);
        let decimal_scale = power(-signed(&s[17..19]), 10.0);
        let scale =
            |packed: i64| ((packed as f64 * binary_scale + reference) * decimal_scale) as f32;
        let bits = u32::from(s[19]);
        let data = &self.data[5..];

        match template {
            0 => {
                if bits > 32 || (points * bits as usize).div_ceil(8) > data.len() {
                    return Err("bad simple packing".into());
                }
                for (&index, out) in wanted.iter().zip(out) {
                    let mut reader = BitReader::new(data, index * bits as usize);
                    *out = scale(reader.take(bits) as i64);
                }
                Ok(())
            }
            3 => self.complex_values_at(points, bits, scale, wanted, out),
            _ => Err(format!("unsupported data representation template 5.{template}").into()),
        }
    }

    /// Template 5.3: values are split into groups, each with its own
    /// reference and bit width, and what is packed is the first or second
    /// difference between neighbours. Undoing the differencing needs every
    /// value before the one wanted, so this walks the field from its
    /// start.
    fn complex_values_at(
        &self,
        points: usize,
        reference_bits: u32,
        scale: impl Fn(i64) -> f32,
        wanted: &[usize],
        out: &mut [f32],
    ) -> Result<()> {
        let s = self.representation;
        if s.len() < 49 {
            return Err("short section 5".into());
        }
        if s[21] != 1 || s[22] != 0 {
            return Err("unsupported group splitting or missing-value management".into());
        }
        let groups = unsigned(&s[31..35]) as usize;
        let width_reference = u32::from(s[35]);
        let width_bits = u32::from(s[36]);
        let length_reference = unsigned(&s[37..41]) as usize;
        let length_increment = usize::from(s[41]);
        let last_length = unsigned(&s[42..46]) as usize;
        let length_bits = u32::from(s[46]);
        let order = usize::from(s[47]);
        let descriptor_octets = usize::from(s[48]);
        if !(1..=2).contains(&order) || !(1..=8).contains(&descriptor_octets) {
            return Err("unsupported spatial differencing".into());
        }
        if reference_bits > 32 || width_bits > 32 || length_bits > 32 {
            return Err("bad complex packing".into());
        }

        // Section 7: the first `order` values and the minimum of the
        // differences, then the groups' references, widths and lengths,
        // each run padded to a whole octet, then the packed values.
        let data = &self.data[5..];
        let descriptors_len = (order + 1) * descriptor_octets;
        let references_at = descriptors_len;
        let widths_at = references_at + (groups * reference_bits as usize).div_ceil(8);
        let lengths_at = widths_at + (groups * width_bits as usize).div_ceil(8);
        let values_at = lengths_at + (groups * length_bits as usize).div_ceil(8);
        if values_at > data.len() {
            return Err("truncated section 7".into());
        }
        let descriptor = |n: usize| signed(&data[n * descriptor_octets..][..descriptor_octets]);
        let minimum = descriptor(order);
        let mut references = BitReader::new(data, references_at * 8);
        let mut widths = BitReader::new(data, widths_at * 8);
        let mut lengths = BitReader::new(data, lengths_at * 8);
        let mut values = BitReader::new(data, values_at * 8);
        let values_end = data.len() * 8;

        let mut wanted = wanted.iter().zip(out).peekable();
        // The two values before the current one.
        let (mut previous, mut before_previous) = (0i64, 0i64);
        let mut index = 0;
        for group in 0..groups {
            if wanted.peek().is_none() {
                return Ok(());
            }
            let reference = references.take(reference_bits) as i64;
            let width = widths.take(width_bits) + width_reference;
            let length = lengths.take(length_bits) as usize * length_increment + length_reference;
            let length = if group == groups - 1 {
                last_length
            } else {
                length
            };
            if width > 32
                || index + length > points
                || values.position + length * width as usize > values_end
            {
                return Err("bad complex packing group".into());
            }
            for _ in 0..length {
                let packed = reference + values.take(width) as i64;
                let value = if index < order {
                    descriptor(index)
                } else if order == 1 {
                    packed + minimum + previous
                } else {
                    packed + minimum + 2 * previous - before_previous
                };
                before_previous = previous;
                previous = value;
                while let Some((_, out)) = wanted.next_if(|(wanted, _)| **wanted == index) {
                    *out = scale(value);
                }
                index += 1;
            }
        }
        if wanted.peek().is_some() {
            return Err("fewer values than grid points".into());
        }
        Ok(())
    }
}

/// Reads big-endian bit fields of up to 32 bits from a byte slice.
struct BitReader<'a> {
    data: &'a [u8],
    /// In bits from the start of `data`.
    position: usize,
}

impl<'a> BitReader<'a> {
    fn new(data: &'a [u8], position: usize) -> Self {
        BitReader { data, position }
    }

    /// The next `bits` bits (at most 32) as an unsigned integer; zero
    /// bits read as 0. Bits past the end of the data read as 0.
    #[inline]
    fn take(&mut self, bits: u32) -> u32 {
        if bits == 0 {
            return 0;
        }
        let byte = self.position / 8;
        let window = match self.data.get(byte..byte + 8) {
            Some(window) => u64::from_be_bytes(window.try_into().unwrap()),
            None => {
                let mut padded = [0u8; 8];
                let tail = self.data.get(byte..).unwrap_or(&[]);
                padded[..tail.len()].copy_from_slice(tail);
                u64::from_be_bytes(padded)
            }
        };
        let value = (window << (self.position % 8)) >> (64 - bits);
        self.position += bits as usize;
        value as u32
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signed_is_sign_and_magnitude() {
        assert_eq!(signed(&[0x00, 0x05]), 5);
        assert_eq!(signed(&[0x80, 0x05]), -5);
        assert_eq!(signed(&[0x81]), -1);
    }

    #[test]
    fn bit_reader_crosses_bytes_and_runs_off_the_end() {
        let data = [0b1010_1100, 0b0101_0011, 0xff];
        let mut reader = BitReader::new(&data, 0);
        assert_eq!(reader.take(3), 0b101);
        assert_eq!(reader.take(0), 0);
        assert_eq!(reader.take(7), 0b011_0001);
        assert_eq!(reader.take(14), 0b01_0011_1111_1111);
        assert_eq!(reader.take(8), 0);
    }

    #[test]
    fn power_matches_repeated_division() {
        assert_eq!(power(-2, 10.0), 1.0 / 10.0 / 10.0);
        assert_eq!(power(3, 2.0), 8.0);
        assert_eq!(power(0, 10.0), 1.0);
    }
}
