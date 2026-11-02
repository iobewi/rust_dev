//! The application's upload decision (examples/firmware/ota-system/src/ota.rs), replayed on the host with the real image
//! validator: bytes received = `total`, the validator is bounded to `total`, then #105 compares the canonical length with `total`.
//!   old rule (#101, A''/B'' on the board):  refuse iff validate(..) fails
//!   new rule (#105)                      :  refuse iff validate(..) fails OR canonical_size != total
use iobewi_firmware_image::esp::{validate, MemoryMap, Read, Verify};

struct Flash(Vec<u8>);
impl Read for Flash { fn read(&mut self, o: u32, out: &mut [u8]) -> Result<(), ()> { let a = o as usize; out.copy_from_slice(self.0.get(a..a + out.len()).ok_or(())?); Ok(()) } }
fn s3map() -> MemoryMap { MemoryMap { chip_id: 9, drom: 0x3c000000..0x3d000000, irom: 0x42000000..0x42800000, iram: 0x40370000..0x403e0000, dram: 0x3fc88000..0x3fd00000, rtc: 0x600fe000..0x60100000, sram_alias_offset: 0x006f0000, boot_window: 0x3fcc8000..0x3fd00000, mmu_page: 0x10000 } }

fn main() {
    let img = std::fs::read(std::env::args().nth(1).expect("image")).unwrap();
    let canonical = validate(&mut Flash(img.clone()), 0, img.len() as u32, &s3map(), Verify::Full).unwrap().canonical_size(0).unwrap();
    println!("image {} bytes, canonical size {}", img.len(), canonical);
    let mut cases: Vec<(String, Vec<u8>)> = vec![("canonical image".into(), img.clone())];
    for k in [1usize, 16, 4096] { let mut v = img.clone(); v.extend(std::iter::repeat(0u8).take(k)); cases.push((format!("N1 canonical + {k} trailing bytes (canonical < total)"), v)); }
    for k in [1usize, 32, 4096] { cases.push((format!("N2 canonical - {k} bytes cut (canonical > total)"), img[..img.len() - k].to_vec())); }
    println!("{:<62} {:>8} {:>9} {:<22} {:<8} {:<8}", "case", "total", "canonical", "validate", "old rule", "new rule");
    for (name, v) in cases {
        let total = v.len() as u32;
        let r = validate(&mut Flash(v), 0, total, &s3map(), Verify::Full);
        let (val, can) = match &r { Ok(i) => ("ok".to_string(), i.canonical_size(0)), Err(e) => (format!("{e:?}"), None) };
        let old = if r.is_err() { "REFUSED" } else { "accepted" };
        let new = if r.as_ref().map_or(true, |i| i.canonical_size(0) != Some(total)) { "REFUSED" } else { "accepted" };
        println!("{:<62} {:>8} {:>9} {:<22} {:<8} {:<8}", name, total, can.map_or("-".into(), |c| c.to_string()), val.chars().take(21).collect::<String>(), old, new);
    }
}
