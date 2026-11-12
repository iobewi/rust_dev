"""PC-CYCLE (G1) - complete destructive cycle on ESP32-P4 / ESP32-C6 with firmware images, on operator-approved regions.

Backup -> program -> independent readback -> restore -> independent compare, for every approved region. The images are private files pinned by
SHA-256 (session `images`); no region and no image exists by default; the approved regions are the only write permission (an image never creates one).
Generalises Q1 (scratch pattern, ONE region, ESP32-S3) to several regions and real images; Q1 is untouched. Nothing here is firmware-specific.

Criteria, written before any run:
  PASS           both references of every region are identical; every image is written and read back equal; every region is restored and
                 the independent compare is equal; the reservation is released and the local grant revoked; Campaign.close() succeeded;
  FAIL           a reference differs between its two reads, a readback or the final compare differs, a client-side refusal reached the bench;
  INDETERMINATE  an operation or a submission is unresolved, or the proof window broke: restoration is UNKNOWN, nothing is replayed;
  BLOCKED        no consent / no region / no image / plan invalid / image changed / ephemeral evidence root / manual ROM banner not seen / no local grant.
Hazards: a write over the bootloader or the table can leave the chip unbootable until a loader entry (C6: manual gesture; P4: agent sequence, a wiring
inference). The recipe never restores by itself after a failure: the references stay in the private evidence folder for an explicit operator restore.
Hardware status: NOT executed.
"""
import hashlib

import pytest

from iobewi_bench import Orchestrator
from iobewi_bench.client import BenchError

from .evidence import record_storage, require_persistent_evidence
from .harness import Blocked, Outcome, compare_released, make_campaign, operator_gesture, recorded, wait_until
from .images import SECTOR, ImagePlanError, load_images, operations, plan
from .pc import first_command, gate, loader_state, open_loader_window      # noqa: F401  (loader_state is a fixture)
from .readonly import console_on_exit, record_transport, start_or_block
from .session import build_manifest

pytestmark = pytest.mark.bench
BACKUP_SEGMENT = 1024 * 1024          # one agent block: each backup read is a single transfer


def sector_outside(regions, flash_size):
    """A sector that no approved region touches, to prove the client-side manifest refusal. None when the regions cover everything."""
    candidates = [r["address"] + r["size"] for r in regions] + [r["address"] - SECTOR for r in regions]
    for address in candidates:
        if 0 <= address <= flash_size - SECTOR and not any(r["address"] < address + SECTOR and address < r["address"] + r["size"] for r in regions):
            return address
    return None


def test_pc4_image_cycle(bench_client, qual_session, pytestconfig, loader_state):
    cfg = qual_session
    gate(cfg, "PC-CYCLE")
    if not pytestconfig.getoption("--bench-allow-destructive"):
        pytest.skip("BLOCKED: --bench-allow-destructive not given (this opt-in is not hardware consent)")
    regions = cfg["approved_regions"]
    if not regions or not cfg["images"]:
        pytest.skip("BLOCKED: the session lacks approved_regions or images (nothing is approved by default)")
    storage = require_persistent_evidence(cfg, pytestconfig)
    try:
        loaded = load_images(cfg["images"])
        rows = plan(loaded, regions, cfg["flash_size"])
    except ImagePlanError as error:
        pytest.skip("BLOCKED: " + str(error))
    timeout = cfg["operation_timeout_s"]
    count, grant_needed = operations(rows, regions, timeout)
    if grant_needed > 14400:
        pytest.skip(f"BLOCKED: this cycle needs a {grant_needed} s grant (limit 14400 s): approve fewer or smaller regions")
    manifest = build_manifest(cfg, "PC-CYCLE", steps=["Manual loader entry when loader.entry is 'manual'", "Grant locally when asked",
                                                      "Revoke the grant locally when asked"])
    case = make_campaign(bench_client, cfg, "PC-4", __file__, pytestconfig, manifest=manifest, allow_destructive=True,
                         parameters={"regions": regions, "images": [{k: r[k] for k in ("label", "address", "size", "sha256")} for r in rows],
                                     "timeout": timeout, "loader": cfg["loader"]["entry"], "operations": count})
    runner, out = Orchestrator(case), Outcome()
    with recorded(case, "PC-4", out, limits="One cycle; transport and loader entry as declared/inferred; readbacks are sequential observations; not "
                  "endurance; says nothing about the programmed firmware, only that the bench wrote, read back and restored the bytes",
                  preconditions=f"{cfg['chip']} / {cfg['transport']} / loader {cfg['loader']['entry']}, {len(regions)} region(s), {len(rows)} image(s)"):
        start_or_block(case)
        record_transport(case, bench_client, cfg, out)
        out.evidence.append("evidence storage: " + record_storage(case, storage))
        case.write("image-plan.json", rows)
        board = case.reserve(ttl=1800)
        open_loader_window(case, board, cfg, out, loader_state)
        identity = first_command(cfg, loader_state, board.identify)
        case.write("identity.json", {k: identity.get(k) for k in ("chip", "flash_size", "flash_id")})   # never the MAC
        assert identity["chip"] == cfg["chip"], f"measured {identity['chip']}, declared {cfg['chip']}"
        assert identity["flash_size"] == cfg["flash_size"], f"measured {identity['flash_size']}, declared {cfg['flash_size']}"

        # Client-side refusals: no hardware command may be submitted by these.
        before, first = case.client.submissions, loaded[0]
        with pytest.raises(BenchError, match="consent"):
            runner.program(board, address=first["address"], source=first["data"], sha256=first["sha256"])
        outside = sector_outside(regions, cfg["flash_size"])
        if outside is not None:
            with pytest.raises(ValueError, match="manifest"):
                runner.program(board, address=outside, source=first["data"][:SECTOR], sha256=hashlib.sha256(first["data"][:SECTOR]).hexdigest(),
                               allow_destructive=True)
        else:
            out.evidence.append("manifest refusal not exercised: the approved regions cover the whole flash")
        assert case.client.submissions == before, "A refusal reached the bench"

        with console_on_exit(board, out):
            # References: two sequential reads of every region must agree; otherwise no mutation.
            refs = []
            for index, region in enumerate(regions):
                segment = min(region["size"], BACKUP_SEGMENT)
                a, ma = runner.backup(board, address=region["address"], size=region["size"], filename=f"ref-{index}-a.bin", segment_size=segment, timeout=timeout)
                b, mb = runner.backup(board, address=region["address"], size=region["size"], filename=f"ref-{index}-b.bin", segment_size=segment, timeout=timeout)
                out.evidence.append(f"region {index} {region['address']:#x}+{region['size']:#x}: ref-a sha256={ma['sha256']} ref-b sha256={mb['sha256']}")
                assert ma["sha256"] == mb["sha256"], f"Reference of region {index} not stable between reads; no mutation performed"
                refs.append((region, a, ma))
            for row, image in zip(rows, loaded):
                region, path, _ = refs[row["region"]]
                offset = image["address"] - region["address"]
                if path.read_bytes()[offset:offset + len(image["data"])] == image["data"]:
                    raise Blocked(f"image {row['label']!r} is already identical to the current content: nothing would be proven")

            operator_gesture(case, f"GRANT {grant_needed} s locally in the page", timeout=cfg["operator_timeout_s"])
            try:
                case.wait_grant(minimum_seconds=grant_needed, timeout=cfg["operator_timeout_s"])
            except TimeoutError as error:
                raise Blocked("Local grant not observed") from error

            out.restoration = "images may be present; restore required from " + ", ".join(p.name for _, p, _ in refs)   # set BEFORE the mutation
            for row, image in zip(rows, loaded):
                board.renew(ttl=3600)                                  # explicit; never implicit
                runner.program(board, address=image["address"], source=image["data"], sha256=image["sha256"], allow_destructive=True, timeout=timeout)
                out.evidence.append(f"image {row['label']} {row['address']:#x}+{row['size']:#x} programmed; independent readback equal sha256={row['sha256']}")
            for region, path, _ in refs:
                board.renew(ttl=3600)
                runner.restore(board, path, allow_destructive=True, timeout=timeout)
                assert compare_released(runner, board, path, out, timeout)["equal"] is True
            out.restoration = "restored; independent compare equal to " + ", ".join(p.name for _, p, _ in refs)

        operator_gesture(case, "REVOKE the grant locally in the page", timeout=cfg["operator_timeout_s"])
        wait_until(lambda: bench_client.device(cfg["board_id"])["privileged_seconds"] == 0, timeout=cfg["operator_timeout_s"], what="local grant revocation")
