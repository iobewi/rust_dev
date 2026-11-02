//! Host model of the whole rollback scenario with the real code and the real bytes: the application's `activate`, the #105
//! bootloader's `select::plan` (real RSA-PSS verification, canonical length), and the application's reconciliation (`boot_action`).
//! Each step applies the writes the real code plans to an in-memory `otadata`; the bytes of the entry that must not change are
//! compared after every step. The application side replicates the three-line outcome rule of `arch/esp32/firmware/a/src/service.rs`
//! (`image_outcome`: a Pending entry => PendingConfirmation, else a Valid one => Confirmed).
//! usage: rollback_model D_IMAGE D_PROOF B_IMAGE B_PROOF
use iobewi_firmware_auth::{ImageFormat, Mcu, Product, Target};
use iobewi_firmware_boot::{activate, decode, state, Boot, Decoded, Entry, Raw, Write};
use iobewi_firmware_bootauth::host::{HostRsaVerifier, HostSha256};
use iobewi_firmware_bootauth::select::{plan, Outcome, Structure};
use iobewi_firmware_bootauth::{IoError, SlotRead, VerifiedBoot};
use iobewi_firmware_image::esp::{validate, MemoryMap, Read, Verify};
use iobewi_firmware_layout::domain_a::Partition;
use iobewi_firmware_update::metadata::{boot_action, Stage, Staged};
use iobewi_firmware_update::BackendOutcome;

struct Flash(Vec<u8>);
impl Read for Flash { fn read(&mut self, o: u32, out: &mut [u8]) -> Result<(), ()> { let a = o as usize; out.copy_from_slice(self.0.get(a..a + out.len()).ok_or(())?); Ok(()) } }
impl SlotRead for Flash { fn read(&mut self, o: u32, out: &mut [u8]) -> Result<(), IoError> { let a = o as usize; out.copy_from_slice(self.0.get(a..a + out.len()).ok_or(IoError)?); Ok(()) } }
const SLOTS: [Partition; 2] = [Partition { offset: 0x20000, size: 0x1b0000 }, Partition { offset: 0x1d0000, size: 0x1b0000 }];
fn s3map() -> MemoryMap { MemoryMap { chip_id: 9, drom: 0x3c000000..0x3d000000, irom: 0x42000000..0x42800000, iram: 0x40370000..0x403e0000, dram: 0x3fc88000..0x3fd00000, rtc: 0x600fe000..0x60100000, sram_alias_offset: 0x006f0000, boot_window: 0x3fcc8000..0x3fd00000, mmu_page: 0x10000 } }
fn unhex(s: &str) -> [u8; 32] { let mut o = [0u8; 32]; for i in 0..32 { o[i] = u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap(); } o }
fn name(r: &Raw) -> String { match decode(r) { Decoded::Blank => "blank".into(), Decoded::Corrupt => "CORRUPT".into(), Decoded::Ok(e) => format!("seq{} {}", e.seq, ["New", "PendingVerify", "Valid", "Invalid", "Aborted"][e.state as usize]) } }
fn show(od: &[Raw; 2]) -> String { format!("[{} | {}]", name(&od[0]), name(&od[1])) }
fn apply(od: &mut [Raw; 2], w: Write) { od[w.sector as usize] = w.entry.encode(); }
/// `image_outcome` of arch/esp32/firmware/a/src/service.rs
fn outcome(od: &[Raw; 2]) -> BackendOutcome {
    let any = |st| od.iter().any(|r| matches!(decode(r), Decoded::Ok(e) if e.state == st));
    if any(state::PENDING_VERIFY) { BackendOutcome::PendingConfirmation } else if any(state::VALID) { BackendOutcome::Confirmed } else { BackendOutcome::Other }
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let rd = |i: usize| std::fs::read(&a[i]).unwrap();
    let (d, dp, b, bp) = (rd(1), rd(2), rd(3), rd(4));
    let pins = [unhex("4b452a4c558aec346bf2f481177e99d0c8c02543495ff4c33229793c60b607ae")];
    let variants = [0u16];
    let target = Target { product: Product::new("iobewi-ota-sys").unwrap(), mcu: Mcu::Esp32S3, image_format: ImageFormat::EspApp, hw_variants: &variants, min_security_version: 0 };
    let pol = VerifiedBoot::<HostRsaVerifier, HostSha256>::new(target, &pins, HostRsaVerifier).unwrap();
    let flash = |d_proof: &[u8], b_proof: &[u8]| { let mut f = vec![0xffu8; 0x380000]; f[0x20000..0x20000 + d.len()].copy_from_slice(&d); f[0x1cf000..0x1d0000].copy_from_slice(d_proof); f[0x1d0000..0x1d0000 + b.len()].copy_from_slice(&b); f[0x37f000..0x380000].copy_from_slice(b_proof); f };
    // one bootloader pass on the given flash picture and otadata; returns (slot booted, writes planned, refusals)
    let boot = |f: &Vec<u8>, od: [Raw; 2]| {
        let mut rep = vec![];
        let probe = Flash(f.clone());
        let mut structure = |slot: u8| { let p = SLOTS[slot as usize]; match validate(&mut Flash(probe.0.clone()), p.offset, p.usable_size(), &s3map(), Verify::Structure) { Ok(i) => Structure::Bootable { canonical_size: i.end - p.offset }, Err(_) => Structure::Refused(1) } };
        let p = plan(od, 2, &pol, &mut Flash(f.clone()), SLOTS, &mut structure, &mut |s, o: Outcome| rep.push((s, o))).unwrap();
        (p.boot, p.writes().collect::<Vec<_>>(), rep)
    };
    let mut ok = true;
    let mut check = |c: bool, what: &str| { println!("{} {}", if c { "PASS" } else { "FAIL" }, what); ok &= c; };
    let f = flash(&dp, &bp);
    let mut od: [Raw; 2] = [Entry::new(1, state::VALID).encode(), Entry::new(2, state::VALID).encode()];
    let b_entry = od[1];
    println!("P5-C state          otadata {}", show(&od));

    // 1. /ota/activate (application): boot::activate
    let w = activate(od, 2, 0).unwrap();
    check(w.sector == 0 && w.entry == Entry::new(3, state::NEW), "activate writes ONE entry, sector 0, seq 3 New (the sector that does not hold the last Valid)");
    apply(&mut od, w);
    check(od[1] == b_entry, "B'' entry (sector 1) byte for byte unchanged by the activation");
    println!("after activate      otadata {}", show(&od));

    // 2. first boot: New -> PendingVerify before the jump, D is authenticated
    let (bt, ws, rep) = boot(&f, od);
    check(matches!(bt, Boot::Slot { slot: 0, seq: 3, .. }) && rep.is_empty(), "bootloader #105 authenticates D (RSA-PSS, canonical length) and selects slot 0 seq 3, no refusal");
    check(ws.len() == 1 && ws[0].sector == 0 && ws[0].entry == Entry::new(3, state::PENDING_VERIFY), "planned write: sector 0 seq 3 PendingVerify, before the jump");
    for w in ws { apply(&mut od, w); }
    check(od[1] == b_entry, "B'' entry unchanged by the first boot of D");
    println!("D running           otadata {}   (D hangs in its self-check, never confirms; software reset at the 15 s deadline)", show(&od));
    // D side: reconcile says AwaitConfirmation; nothing writes
    let st = Staged { stage: Stage::Activating, slot: "ota_0".into(), digest: String::new(), deployment_id: "d".into(), size: 0 };
    check(format!("{:?}", boot_action(&st, outcome(&od), Some("ota_0"))) == "AwaitConfirmation", "application D: Pending + Activating + same slot => AwaitConfirmation (watchdog fed once, self-check hangs, 15 s timeout => reset)");

    // 3. reset without confirmation -> bootloader: Pending -> Aborted, falls back to B''
    let (bt, ws, rep) = boot(&f, od);
    check(matches!(bt, Boot::Slot { slot: 1, seq: 2, .. }) && rep.is_empty(), "after the reset the bootloader authenticates B'' and selects slot 1 seq 2 (D is not examined)");
    check(ws.len() == 1 && ws[0].sector == 0 && ws[0].entry == Entry::new(3, state::ABORTED), "planned write: sector 0 seq 3 Aborted (the only write)");
    for w in ws { apply(&mut od, w); }
    check(od[1] == b_entry, "B'' entry byte for byte unchanged by the rollback");
    println!("after rollback      otadata {}", show(&od));

    // 4. B'' reconciles: Activating for ota_0 while running ota_1 and a Valid entry exists => ClearStale
    check(format!("{:?}", boot_action(&st, outcome(&od), Some("ota_1"))) == "ClearStale", "application B'': staged Activating(ota_0) while running ota_1 => ClearStale (ota_meta cleared, then `stable`)");

    // 5. persistence reboot: nothing changes, D is never started again
    let (bt, ws, rep) = boot(&f, od);
    check(matches!(bt, Boot::Slot { slot: 1, seq: 2, .. }) && ws.is_empty() && rep.is_empty(), "next boot: B'' again, no write, D never started again");

    // failure branches (same flash, other metadata / other bytes)
    println!("--- branches");
    let mut od2: [Raw; 2] = [Entry::new(3, state::PENDING_VERIFY).encode(), b_entry];
    let (bt, ws, _) = boot(&f, od2);
    check(matches!(bt, Boot::Slot { slot: 1, .. }) && ws.len() == 1 && ws[0].entry.state == state::ABORTED, "reset too early (still Pending): same outcome, Aborted + B''");
    let mut torn = Entry::new(3, state::ABORTED).encode(); torn[20..24].copy_from_slice(&[0xff; 4]);
    od2 = [torn, b_entry];
    let (bt, ws, _) = boot(&f, od2);
    check(matches!(bt, Boot::Slot { slot: 1, .. }) && ws.is_empty(), "Aborted write torn (no commit word): entry ignored as corrupt, B'' boots, nothing is invented");
    let f2 = flash(&[0xff; 4096], &bp);
    let (bt, ws, rep) = boot(&f2, [Entry::new(1, state::VALID).encode(), b_entry]);
    let _ = (bt, ws, rep);
    od2 = [Entry::new(3, state::NEW).encode(), b_entry];
    let (bt, ws, rep) = boot(&f2, od2);
    check(matches!(bt, Boot::Slot { slot: 1, .. }) && ws.len() == 1 && ws[0].entry.state == state::INVALID && !rep.is_empty(), &format!("D without a proof: refused ({:?}), entry Invalid, B'' boots", rep.first().map(|r| r.1)));
    let f3 = flash(&dp, &[0xff; 4096]);
    od2 = [Entry::new(3, state::ABORTED).encode(), b_entry];
    let (bt, _, _) = boot(&f3, od2);
    check(matches!(bt, Boot::Halt(_)), &format!("B'' without a valid proof after the rollback: {:?} (recovery by the ROM loader)", bt));
    println!("\nRESULT: {}", if ok { "PASS" } else { "FAIL" });
    std::process::exit(if ok { 0 } else { 1 });
}
