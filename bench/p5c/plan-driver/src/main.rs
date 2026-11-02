use iobewi_firmware_auth::{ImageFormat, Mcu, Product, Target};
use iobewi_firmware_boot::{decode, state, Boot, Decoded, Entry, Raw};
use iobewi_firmware_bootauth::host::{HostRsaVerifier, HostSha256};
use iobewi_firmware_bootauth::select::{plan, Outcome, Structure};
use iobewi_firmware_bootauth::{IoError, SlotRead, VerifiedBoot};
use iobewi_firmware_image::esp::{validate, MemoryMap, Read, Verify};
use iobewi_firmware_layout::domain_a::Partition;

struct Flash(Vec<u8>);
impl SlotRead for Flash { fn read(&mut self, o: u32, out: &mut [u8]) -> Result<(), IoError> { let a = o as usize; out.copy_from_slice(self.0.get(a..a + out.len()).ok_or(IoError)?); Ok(()) } }
impl Read for Flash { fn read(&mut self, o: u32, out: &mut [u8]) -> Result<(), ()> { let a = o as usize; out.copy_from_slice(self.0.get(a..a + out.len()).ok_or(())?); Ok(()) } }
const SLOTS: [Partition; 2] = [Partition { offset: 0x20000, size: 0x1b0000 }, Partition { offset: 0x1d0000, size: 0x1b0000 }];
fn rd(p: &str) -> Vec<u8> { std::fs::read(p).unwrap() }
fn unhex(s: &str) -> [u8; 32] { let mut o = [0u8; 32]; for i in 0..32 { o[i] = u8::from_str_radix(&s[2*i..2*i+2], 16).unwrap(); } o }
fn ent(seq: u32, st: u32) -> Raw { Entry::new(seq, st).encode() }
fn name(r: &Raw) -> String { match decode(r) { Decoded::Blank => "blank".into(), Decoded::Corrupt => "CORRUPT".into(), Decoded::Ok(e) => format!("seq{} st{}", e.seq, e.state) } }
fn s3map() -> MemoryMap { MemoryMap { chip_id: 9, drom: 0x3c000000..0x3d000000, irom: 0x42000000..0x42800000, iram: 0x40370000..0x403e0000, dram: 0x3fc88000..0x3fd00000, rtc: 0x600fe000..0x60100000, sram_alias_offset: 0x006f0000, boot_window: 0x3fcc8000..0x3fd00000, mmu_page: 0x10000 } }
fn bootable(end_rel: u32) -> Structure { Structure::Bootable { canonical_size: end_rel } }

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (ota0, ota1, bad_proof) = (rd(&a[1]), rd(&a[2]), rd(&a[3]));
    let pins = [unhex("4b452a4c558aec346bf2f481177e99d0c8c02543495ff4c33229793c60b607ae")];
    let variants = [0u16];
    let target = Target { product: Product::new("iobewi-ota-sys").unwrap(), mcu: Mcu::Esp32S3, image_format: ImageFormat::EspApp, hw_variants: &variants, min_security_version: 0 };
    let pol = VerifiedBoot::<HostRsaVerifier, HostSha256>::new(target, &pins, HostRsaVerifier).unwrap();
    let mk = |b_proof: Option<&[u8]>| { let mut f = vec![0xffu8; 0x380000]; f[0x20000..0x1d0000].copy_from_slice(&ota0); f[0x1d0000..0x380000].copy_from_slice(&ota1); if let Some(p) = b_proof { f[0x37f000..0x380000].copy_from_slice(p); } f };
    let run = |tag: &str, f: Vec<u8>, od: [Raw; 2]| {
        let mut rep: Vec<(u8, Outcome)> = vec![];
        let probe = Flash(f.clone());
        let mut structure = |slot: u8| { let p = SLOTS[slot as usize]; match validate(&mut Flash(probe.0.clone()), p.offset, p.usable_size(), &s3map(), Verify::Structure) { Ok(i) => bootable(i.end - p.offset), Err(_) => Structure::Refused(1) } };
        let p = plan(od, 2, &pol, &mut Flash(f), SLOTS, &mut structure, &mut |s, o| rep.push((s, o))).unwrap();
        println!("[{tag}] otadata [{} | {}]", name(&od[0]), name(&od[1]));
        for (s, o) in &rep { println!("    refused slot {s}: {o:?}"); }
        match p.boot { Boot::Slot { slot, seq, .. } => println!("    -> BOOT slot {slot} seq {seq}"), Boot::Halt(h) => println!("    -> HALT {h:?}") }
        for w in p.writes() { println!("    plan write: sector {} seq {} state {}", w.sector, w.entry.seq, w.entry.state); }
    };
    let blank = [0xffu8; 32];
    let mut torn = ent(2, state::NEW); torn[20..24].copy_from_slice(&[0xff; 4]);       // body programmed, commit word not
    run("S1  after W1 (A'' alone, authentic proofs)", mk(None), [ent(1, state::VALID), blank]);
    run("A1  after /ota/activate: B'' New", mk(None), [ent(1, state::VALID), ent(2, state::NEW)]);
    run("B1' B'' Pending, reset before confirm (rollback)", mk(None), [ent(1, state::VALID), ent(2, state::PENDING_VERIFY)]);
    run("C1  B'' confirmed Valid", mk(None), [ent(1, state::VALID), ent(2, state::VALID)]);
    run("D4  B'' New but proof signature invalid", mk(Some(&bad_proof)), [ent(1, state::VALID), ent(2, state::NEW)]);
    run("D2' activation entry torn (commit word missing)", mk(None), [ent(1, state::VALID), torn]);
    run("D10 after rollback: B'' Aborted", mk(None), [ent(1, state::VALID), ent(2, state::ABORTED)]);
}
