//! The application's upload decision and what follows it, replayed on the host with the real validators, on the same bytes.
//!   upload rule, old (#101, A''/B'' on the board): refuse iff esp::validate(.., total, Verify::Full) fails
//!   upload rule, new (#105)                      : refuse iff validate fails OR canonical_size != total
//!   publication gate (provision::gate, real RSA-PSS and digest of the signed bytes, signed size == total)
//!   boot selection (select::plan of #105, structure canonical size compared with the signed size)
//! usage: upload_check B2.bin B2.iap1 N1.bin N1.iap1 N2.bin N2.iap1 OTA0_IMAGE_FILE
use iobewi_firmware_auth::{ImageFormat, Mcu, Product, Target};
use iobewi_firmware_boot::{state, Boot, Entry, Raw};
use iobewi_firmware_bootauth::host::{HostRsaVerifier, HostSha256};
use iobewi_firmware_bootauth::select::{plan, Outcome, Structure};
use iobewi_firmware_bootauth::{provision, IoError, SlotRead, VerifiedBoot};
use iobewi_firmware_image::esp::{validate, MemoryMap, Read, Verify};
use iobewi_firmware_layout::domain_a::Partition;

struct Flash(Vec<u8>);
impl Read for Flash { fn read(&mut self, o: u32, out: &mut [u8]) -> Result<(), ()> { let a = o as usize; out.copy_from_slice(self.0.get(a..a + out.len()).ok_or(())?); Ok(()) } }
impl SlotRead for Flash { fn read(&mut self, o: u32, out: &mut [u8]) -> Result<(), IoError> { let a = o as usize; out.copy_from_slice(self.0.get(a..a + out.len()).ok_or(IoError)?); Ok(()) } }
const SLOTS: [Partition; 2] = [Partition { offset: 0x20000, size: 0x1b0000 }, Partition { offset: 0x1d0000, size: 0x1b0000 }];
fn s3map() -> MemoryMap { MemoryMap { chip_id: 9, drom: 0x3c000000..0x3d000000, irom: 0x42000000..0x42800000, iram: 0x40370000..0x403e0000, dram: 0x3fc88000..0x3fd00000, rtc: 0x600fe000..0x60100000, sram_alias_offset: 0x006f0000, boot_window: 0x3fcc8000..0x3fd00000, mmu_page: 0x10000 } }
fn unhex(s: &str) -> [u8; 32] { let mut o = [0u8; 32]; for i in 0..32 { o[i] = u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap(); } o }

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let rd = |i: usize| std::fs::read(&a[i]).unwrap();
    let (b2, b2p, n1, n1p, n2, n2p, ota0) = (rd(1), rd(2), rd(3), rd(4), rd(5), rd(6), rd(7));
    let pins = [unhex("4b452a4c558aec346bf2f481177e99d0c8c02543495ff4c33229793c60b607ae")];
    let variants = [0u16];
    let target = Target { product: Product::new("iobewi-ota-sys").unwrap(), mcu: Mcu::Esp32S3, image_format: ImageFormat::EspApp, hw_variants: &variants, min_security_version: 0 };
    let pol = VerifiedBoot::<HostRsaVerifier, HostSha256>::new(target, &pins, HostRsaVerifier).unwrap();
    println!("{:<34} {:>8} {:>9} {:<10} {:<9} {:<9} {:<10} {:<28}", "case", "total", "canonical", "validate", "old rule", "new rule", "pub. gate", "boot (#105 select::plan)");
    for (name, img, proof) in [("canonical B'' image", &b2, &b2p), ("N1 B'' + 16 trailing bytes", &n1, &n1p), ("N2 first 1 MiB of B''", &n2, &n2p)] {
        let total = img.len() as u32;
        let r = validate(&mut Flash(img.clone()), 0, total, &s3map(), Verify::Full);
        let (val, can) = match &r { Ok(i) => ("ok".to_string(), i.canonical_size(0)), Err(e) => (format!("{e:?}").chars().take(10).collect(), None) };
        let old = if r.is_err() { "REFUSED" } else { "accepted" };
        let new = if r.as_ref().map_or(true, |i| i.canonical_size(0) != Some(total)) { "REFUSED" } else { "accepted" };
        // flash picture: ota_0 = the A'' image already there, ota_1 = this upload + its proof (what publication would leave)
        let mut f = vec![0xffu8; 0x380000];
        f[0x20000..0x20000 + ota0.len()].copy_from_slice(&ota0);
        f[0x1d0000..0x1d0000 + img.len()].copy_from_slice(img);
        f[0x37f000..0x380000].copy_from_slice(proof);
        let gate = match provision::gate(&mut Flash(f.clone()), SLOTS[1], &pol, total) { Ok(()) => "accepts".to_string(), Err(e) => format!("{e:?}").chars().take(9).collect() };
        let probe = Flash(f.clone());
        let mut structure = |slot: u8| { let p = SLOTS[slot as usize]; match validate(&mut Flash(probe.0.clone()), p.offset, p.usable_size(), &s3map(), Verify::Structure) { Ok(i) => Structure::Bootable { canonical_size: i.end - p.offset }, Err(_) => Structure::Refused(1) } };
        let od: [Raw; 2] = [Entry::new(1, state::VALID).encode(), Entry::new(2, state::NEW).encode()];
        let mut rep = vec![];
        let p = plan(od, 2, &pol, &mut Flash(f), SLOTS, &mut structure, &mut |s, o: Outcome| rep.push((s, o))).unwrap();
        let boot = match (p.boot, rep.first()) { (Boot::Slot { slot: 1, .. }, _) => "boots the upload".to_string(), (Boot::Slot { slot, .. }, Some((_, o))) => format!("slot {slot}; slot 1: {o:?}").replace("Proof(", "").replace(')', ""), (b, _) => format!("{b:?}") };
        println!("{:<34} {:>8} {:>9} {:<10} {:<9} {:<9} {:<10} {:<28}", name, total, can.map_or("-".into(), |c| c.to_string()), val, old, new, gate, boot);
    }
}
