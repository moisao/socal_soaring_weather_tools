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
pub fn signed(bytes: &[u8]) -> i64 {
    let magnitude = bytes.iter().enumerate().fold(0i64, |acc, (n, &b)| {
        (acc << 8) | i64::from(if n == 0 { b & 0x7f } else { b })
    });
    if bytes[0] & 0x80 != 0 {
        -magnitude
    } else {
        magnitude
    }
}

pub fn unsigned(bytes: &[u8]) -> u64 {
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

    /// Appends big-endian bit fields to a byte vector.
    #[derive(Default)]
    struct BitWriter {
        bytes: Vec<u8>,
        bits: usize,
    }

    impl BitWriter {
        fn put(&mut self, value: u64, width: u32) {
            for bit in (0..width).rev() {
                if self.bits.is_multiple_of(8) {
                    self.bytes.push(0);
                }
                *self.bytes.last_mut().unwrap() |=
                    (((value >> bit) & 1) as u8) << (7 - self.bits % 8);
                self.bits += 1;
            }
        }

        fn into_octets(self) -> Vec<u8> {
            self.bytes
        }
    }

    fn sign_magnitude(value: i64, octets: usize) -> Vec<u8> {
        let mut bytes = value.unsigned_abs().to_be_bytes()[8 - octets..].to_vec();
        if value < 0 {
            bytes[0] |= 0x80;
        }
        bytes
    }

    fn section(number: u8, body: &[u8]) -> Vec<u8> {
        let mut s = ((body.len() + 5) as u32).to_be_bytes().to_vec();
        s.push(number);
        s.extend_from_slice(body);
        s
    }

    /// A whole message around the given Section 5 and 7 bodies: TMP on
    /// the 850 hPa surface, on the HRRR grid's definition.
    fn message(section5: &[u8], section7: &[u8]) -> Vec<u8> {
        let mut product = vec![0u8; 29];
        product[17] = 100; // isobaric surface
        product[19..23].copy_from_slice(&85_000u32.to_be_bytes());
        let mut bytes = b"GRIB\0\0\0\x02\0\0\0\0\0\0\0\0".to_vec();
        bytes.extend(section(1, &[0; 16]));
        bytes.extend(crate::lambert::tests::hrrr_section3());
        bytes.extend(section(4, &product));
        bytes.extend(section(5, section5));
        bytes.extend(section(6, &[255]));
        bytes.extend(section(7, section7));
        bytes.extend(b"7777");
        bytes
    }

    /// Section 5's body up to the bit width: `points` values, template
    /// `template`, reference 250.5, binary scale -2, decimal scale 1.
    fn representation(points: usize, template: u16, bits: u8) -> Vec<u8> {
        let mut s = (points as u32).to_be_bytes().to_vec();
        s.extend(template.to_be_bytes());
        s.extend(250.5f32.to_be_bytes());
        s.extend(sign_magnitude(-2, 2));
        s.extend(sign_magnitude(1, 2));
        s.push(bits);
        s.push(0); // original values are floating point
        s
    }

    fn expected(packed: i64) -> f32 {
        ((packed as f64 * 0.25 + 250.5) * (1.0 / 10.0)) as f32
    }

    /// Pack `field` with template 5.3 in groups of `group_length`, the
    /// way an encoder would: differences, minus their minimum, then each
    /// group's values minus the group's own minimum.
    fn complex_message(field: &[i64], order: usize, group_length: usize) -> Vec<u8> {
        let mut differences = vec![0i64; field.len()];
        for n in order..field.len() {
            differences[n] = match order {
                1 => field[n] - field[n - 1],
                _ => field[n] - 2 * field[n - 1] + field[n - 2],
            };
        }
        let minimum = *differences[order..].iter().min().unwrap();
        let mut packed: Vec<i64> = differences.iter().map(|d| d - minimum).collect();
        packed[..order].fill(0);

        let groups: Vec<&[i64]> = packed.chunks(group_length).collect();
        let references: Vec<i64> = groups.iter().map(|g| *g.iter().min().unwrap()).collect();
        let widths: Vec<u32> = groups
            .iter()
            .zip(&references)
            .map(|(g, r)| 64 - ((g.iter().max().unwrap() - r) as u64).leading_zeros())
            .collect();
        let reference_bits = 64 - (*references.iter().max().unwrap() as u64).leading_zeros();

        let mut s5 = representation(field.len(), 3, reference_bits as u8);
        s5.push(1); // general group splitting
        s5.push(0); // no missing values
        s5.extend([0; 8]);
        s5.extend((groups.len() as u32).to_be_bytes());
        s5.push(0); // reference for group widths
        s5.push(6); // bits per group width
        s5.extend((group_length as u32).to_be_bytes()); // reference for group lengths
        s5.push(1); // length increment
        s5.extend((groups.last().unwrap().len() as u32).to_be_bytes());
        s5.push(1); // bits per scaled group length
        s5.push(order as u8);
        s5.push(3); // octets per extra descriptor

        let mut s7 = Vec::new();
        for value in &field[..order] {
            s7.extend(sign_magnitude(*value, 3));
        }
        s7.extend(sign_magnitude(minimum, 3));
        let mut writer = BitWriter::default();
        references
            .iter()
            .for_each(|r| writer.put(*r as u64, reference_bits));
        s7.extend(writer.into_octets());
        let mut writer = BitWriter::default();
        widths.iter().for_each(|w| writer.put(u64::from(*w), 6));
        s7.extend(writer.into_octets());
        let mut writer = BitWriter::default();
        groups.iter().for_each(|_| writer.put(0, 1));
        s7.extend(writer.into_octets());
        let mut writer = BitWriter::default();
        for ((group, reference), width) in groups.iter().zip(&references).zip(&widths) {
            group
                .iter()
                .for_each(|p| writer.put((p - reference) as u64, *width));
        }
        s7.extend(writer.into_octets());
        message(&s5, &s7)
    }

    /// A field with smooth stretches, jumps, a constant run (a group of
    /// width 0) and negative values.
    fn field() -> Vec<i64> {
        (0..1000i64)
            .map(|n| match n {
                300..400 => 77,
                _ => (n * n) % 997 - 300 + n / 7,
            })
            .collect()
    }

    #[test]
    fn complex_packing_round_trips() {
        let field = field();
        for (order, group_length) in [(2, 7), (2, 64), (1, 7), (1, 1000)] {
            let bytes = complex_message(&field, order, group_length);
            let message = Message::parse(&bytes).unwrap();
            let wanted: Vec<usize> = (0..field.len()).collect();
            let mut out = vec![f32::NAN; wanted.len()];
            message.values_at(&wanted, &mut out).unwrap();
            let want: Vec<f32> = field.iter().map(|v| expected(*v)).collect();
            assert_eq!(out, want, "order {order}, groups of {group_length}");
        }
    }

    #[test]
    fn complex_packing_picks_points_and_repeats() {
        let field = field();
        let bytes = complex_message(&field, 2, 7);
        let message = Message::parse(&bytes).unwrap();
        let wanted = [0, 1, 2, 350, 350, 998, 999];
        let mut out = [f32::NAN; 7];
        message.values_at(&wanted, &mut out).unwrap();
        assert_eq!(out.to_vec(), wanted.map(|n| expected(field[n])).to_vec());
        assert!(message.values_at(&[1000], &mut [0.0]).is_err());
    }

    #[test]
    fn simple_packing() {
        let field: Vec<i64> = (0..50).map(|n| (n * 37) % 1024).collect();
        let mut writer = BitWriter::default();
        field.iter().for_each(|v| writer.put(*v as u64, 11));
        let bytes = message(&representation(field.len(), 0, 11), &writer.into_octets());
        let message = Message::parse(&bytes).unwrap();
        let wanted = [0, 7, 7, 49];
        let mut out = [f32::NAN; 4];
        message.values_at(&wanted, &mut out).unwrap();
        assert_eq!(out.to_vec(), wanted.map(|n| expected(field[n])).to_vec());
    }

    #[test]
    fn product_and_truncation() {
        let bytes = complex_message(&field(), 2, 7);
        let product = Message::parse(&bytes).unwrap().product().unwrap();
        assert_eq!((product.category, product.number), (0, 0));
        assert_eq!(
            (product.surface_type, product.surface_value),
            (100, 85_000.0)
        );
        assert!(Message::parse(&bytes[..bytes.len() - 40]).is_err());
        // Values cut short, with the section lengths still consistent.
        let mut short = bytes.clone();
        let cut = 200;
        short.drain(bytes.len() - 4 - cut..bytes.len() - 4);
        let section7 = short.len() - 4 - (section7_len(&bytes) - cut);
        let len = (section7_len(&bytes) - cut) as u32;
        short[section7..section7 + 4].copy_from_slice(&len.to_be_bytes());
        let message = Message::parse(&short).unwrap();
        assert!(message.values_at(&[999], &mut [0.0]).is_err());
    }

    fn section7_len(bytes: &[u8]) -> usize {
        Message::parse(bytes).unwrap().data.len()
    }
}
