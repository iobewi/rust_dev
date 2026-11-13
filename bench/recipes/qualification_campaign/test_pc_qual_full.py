"""P4-QUAL-FULL (G1, PC-11) - ONE continuous qualification of the SPI NOR flash of the ESP32-P4 ECO7 v3.2 (32 MiB), phases A -> B -> C -> D, official stub v1.2.2 at 115200 baud.

  A  targeted reads (start, partition table, 0x00800000, 0x00FFF000, 0x01000000, last sector): two independent reads of 4096 bytes each, compared;
  B  two independent full images, BACKUP-A and BACKUP-B (33 554 432 bytes each, one-MiB segments, each copied to private fsynced storage before its server artifact is deleted);
  C  validation: sizes, coverage, per-segment and global SHA-256, byte-for-byte comparison, readability; inventory (counts and offsets only) and boot-criticality of the write sectors;
  D  three 4-KiB sectors D1 0x00800000, D2 0x01000000, D3 0x01FFF000: for each, verify against BACKUP-A, then four writes with an independent readback after each
     (pattern A, blank, pattern B, restore from BACKUP-A), neighbours checked before and after, final verification.

Every operation is its own loader session (return_console=true, so a reset of the card after each operation) and carries `uart_baud: 115200`: the identity of the stub and the speed that ran are
in the evidence of every read and write. The write sectors are the ONLY approved regions. The device has no sector-erase command: a 4-KiB write erases exactly its sector (the stub skips the erase
of a blank sector), so the order pattern A -> blank -> pattern B -> restore guarantees that an erase of a NON-blank sector happens and is read back as 0xFF; the sectors next to each one are
read before and after to show that nothing wider was erased. No chip erase, no flash.erase, no eFuse, no C6, no GPIO54, no stub variant.

Local grant: the operator arms ONE grant at the start (`qual_full.grant_seconds`, up to 14400 s, it must outlast A-D); the recipe waits for it before touching the card and revokes nothing itself.
After phase D it asks for the revocation and waits `qual_full.revoke_wait_s`. No grant, no hardware: BLOCKED. If the grant would not outlast the campaign (projected from the first segment), BLOCKED
BEFORE any write.

Criteria, written before any run:
  PASS           phases A, B, C and D all PASS, the final state is controlled (reservation released, no pending operation, JEDEC unchanged);
  FAIL           a read is incomplete or differs from its twin, an identification is not 0x1940C8, the two backups differ, a readback differs, an operation failed;
  INDETERMINATE  an operation or the proof window is unresolved: nothing is replayed;
  BLOCKED        no consent / not the P4 / regions are not exactly D1-D3 / evidence not persistent or too small / no grant / a write sector is boot-critical / grant too short.
The first problem stops the campaign: no retry, no automatic restore. After the first write a failure leaves the sector as it is (the BACKUP-A files restore it explicitly). Hardware: NOT executed.
"""
import hashlib
import json
import os
import time
from importlib.metadata import version

import pytest

from iobewi_bench.client import OperationError

from .backup import assemble, build_manifest as backup_manifest, free_space_problem, plan_segments, verify_backup
from .evidence import record_storage, require_persistent_evidence
from .harness import Blocked, Outcome, make_campaign, operator_gesture, recorded
from .pc import gate, loader_state, open_loader_window      # noqa: F401  (loader_state is a fixture)
from .qual_full import (D_SECTORS, FLASH, JEDEC, READ_REGIONS, SECTOR, UART_BAUD, WRITE_STEPS, alias, blank, boot_critical, compare_files, inventory, partition_table, pattern_a, pattern_b,
                        plan_checks, sha256_file)
from .readonly import record_transport, retained_reads, start_or_block
from .session import build_manifest as campaign_manifest
from .test_pc_integrity import _failure, durable_copy, sync_evidence

pytestmark = pytest.mark.bench
SCENARIO = "P4-QUAL-FULL"
READ_TIMEOUT, WRITE_TIMEOUT = 600, 120
MIB = 1024 * 1024


def test_pc11_p4_full_qualification(bench_client, qual_session, pytestconfig, loader_state, request):
    cfg = qual_session
    gate(cfg, SCENARIO)
    plan_checks()
    if not pytestconfig.getoption("--bench-allow-destructive"):
        pytest.skip("BLOCKED: --bench-allow-destructive not given (this opt-in is not hardware consent)")
    if (cfg["chip"], cfg["flash_size"], cfg["transport"]) != ("esp32p4", FLASH, "external_uart"):
        pytest.skip(f"BLOCKED: this campaign qualifies the esp32p4 / {FLASH} bytes / external_uart; the session declares {cfg['chip']} / {cfg['flash_size']} / {cfg['transport']}")
    section = cfg["qual_full"]
    if not section.get("expected_assets_sha256"):
        pytest.skip("BLOCKED: qual_full.expected_assets_sha256 is not declared: the reviewed agent cannot be verified")
    expected_regions = sorted(({"address": a, "size": SECTOR} for _, a in D_SECTORS), key=lambda r: r["address"])
    if sorted(cfg["approved_regions"], key=lambda r: r["address"]) != expected_regions:
        pytest.skip("BLOCKED: approved_regions must be exactly the three sectors D1 0x00800000, D2 0x01000000, D3 0x01FFF000 (4096 bytes each): nothing else is writable")
    storage = require_persistent_evidence(cfg, pytestconfig)
    problem = free_space_problem(cfg["evidence_root"], 4 * FLASH + 512 * 1024 * 1024)
    if problem:
        pytest.skip("BLOCKED: " + problem)
    previous_umask = os.umask(0o077)                                      # every evidence file private: 0600, directories 0700
    request.addfinalizer(lambda: os.umask(previous_umask))
    segment_size, grant_seconds = section["segment_size"], section["grant_seconds"]
    manifest = campaign_manifest(cfg, SCENARIO, grant_seconds=grant_seconds, steps=["Arm ONE local grant at the start", "Revoke the grant locally when asked at the end",
                                                                                   "Resets of the card after every operation (return_console)"])
    case = make_campaign(bench_client, cfg, "PC-11", __file__, pytestconfig, manifest=manifest, allow_destructive=True,
                         parameters={"read_regions": [[n, a] for n, a in READ_REGIONS], "write_sectors": [[n, a] for n, a in D_SECTORS], "segment_size": segment_size,
                                     "uart_baud": UART_BAUD, "grant_seconds": grant_seconds, "expected_assets_sha256": section["expected_assets_sha256"], "steps": list(WRITE_STEPS)})
    request.addfinalizer(lambda: sync_evidence(case.directory))
    out, phases = Outcome(), {"A": "NOT RUN", "B": "NOT RUN", "C": "NOT RUN", "D": "NOT RUN"}
    state = {"phase": None, "reads": 0, "writes": 0, "first_write": False}

    def mark(phase, verdict):
        phases[phase] = verdict
        tmp = case.directory / f".phase-verdicts.{os.getpid()}"
        tmp.write_text(json.dumps({"phases": phases, "current": state["phase"], "reads": state["reads"], "writes": state["writes"]}, indent=2))
        os.replace(tmp, case.directory / "phase-verdicts.json")

    def progress(text):
        print(f"PROGRESS {time.strftime('%H:%M:%S')} {text}", flush=True)

    with recorded(case, "PC-11", out, preconditions=f"esp32p4 v3.2 / external_uart / official stub v1.2.2 at {UART_BAUD} baud, agent assets {section['expected_assets_sha256'][:12]}",
                  limits="One campaign on one card; reads at 115200 baud only; official stub; says nothing about other speeds, other stubs, endurance or the OTA system; digests are carried by "
                         "the same serial link; the two backups are sequential observations"):
        start_or_block(case)
        out.evidence.append("evidence storage: " + record_storage(case, storage))
        record_transport(case, bench_client, cfg, out)
        device = bench_client.device(cfg["board_id"])
        provenance = device.get("provenance") or {}
        health = bench_client.health()
        case.write("versions.json", {"server": health.get("build"), "epoch_prefix": health["epoch"][:8], "agent_sha256": provenance.get("agent_sha256"),
                                    "page_assets_sha256": provenance.get("assets_sha256"), "expected_assets_sha256": section["expected_assets_sha256"], "client": version("iobewi-bench"),
                                    "generation_prefix": device["generation"][:8], "usb_vid_pid": [provenance.get("usb_vid"), provenance.get("usb_pid")], "uart_baud": UART_BAUD})
        if provenance.get("assets_sha256") != section["expected_assets_sha256"]:
            raise Blocked(f"the agent reports page assets {str(provenance.get('assets_sha256'))[:12]}, the session expects {section['expected_assets_sha256'][:12]}: no command was submitted")
        if not {"flash.read", "flash.write"} <= set(device.get("capabilities") or []):
            raise Blocked("the card does not announce flash.read and flash.write; no command was submitted")
        board = case.reserve(ttl=3600)
        open_loader_window(case, board, cfg, out, loader_state)
        operator_gesture(case, f"GRANT about {grant_seconds} s locally in the page NOW (it must outlast phases A-D; the recipe touches no hardware before it sees it)", timeout=cfg["operator_timeout_s"])
        try:
            case.wait_grant(minimum_seconds=grant_seconds - 300, timeout=cfg["operator_timeout_s"])
        except TimeoutError as error:
            raise Blocked("the local grant was not observed: no hardware command was submitted") from error
        grant_seen = time.monotonic()
        out.evidence.append(f"local grant observed: {bench_client.device(cfg['board_id'])['privileged_seconds']} s left")
        evidence_dir = board.output_dir
        with retained_reads(board, out) as keeper:

            def renew():
                board.renew(ttl=3600)                                          # explicit, never implicit

            def check_identity(result, name):
                identify = result.get("identify") or {}
                assert (identify.get("jedec_id") or 0) & 0xFFFFFF == JEDEC, f"{name}: JEDEC id {identify.get('jedec_id')} is not {JEDEC:#x}"
                assert "ESP32-P4" in str(identify.get("chip_description")), f"{name}: unexpected chip {identify.get('chip_description')!r}"
                ident = result.get("stub_identity") or {}
                assert (ident.get("variant"), ident.get("uart_baud")) == ("official", UART_BAUD), f"{name}: stub identity {ident}"
                return {"jedec_id": identify.get("jedec_id"), "identify_index": identify.get("identify_index"), "uart_baud": ident.get("uart_baud"), "stub": ident.get("variant")}

            def read(address, size, name):
                renew()
                started = time.monotonic()
                try:
                    result = board.command("flash.read", {"address": address, "size": size, "return_console": True, "stub_digest": True, "uart_baud": UART_BAUD}, timeout=READ_TIMEOUT)
                except OperationError as error:
                    _failure(case, name, error, out)
                    raise
                artifact = result["artifact"]
                keeper.held.append(dict(artifact))                                # tracked at once: kept on the server unless a verified private copy exists
                data = board.client.download(artifact)
                assert len(data) == size and result.get("address") == address, f"{name}: unexpected address or size"
                state["reads"] += 1
                durable_copy(evidence_dir, state["reads"], address, data, artifact, board.last_operation["id"])
                digests = result.get("stub_digests") or []
                blocks = [(x, min(MIB, size - x)) for x in range(0, size, MIB)]
                assert len(digests) == len(blocks), f"{name}: expected {len(blocks)} stub digests, got {len(digests)}"
                for (offset, length), digest in zip(blocks, digests):
                    assert digest.get("state") == "received" and digest.get("block_address") == address + offset and digest.get("md5") == hashlib.md5(data[offset:offset + length]).hexdigest(), \
                        f"{name}: the stub's own digest of the block at {address + offset:#x} is missing or differs from the received bytes"
                problems = keeper.release()                                        # the server copy goes only after the private copy was re-read and matched
                assert not problems, f"{name}: " + "; ".join(problems)
                record = {"name": name, "address": address, "size": size, "operation": board.last_operation["id"], "sha256": hashlib.sha256(data).hexdigest(), "seconds": round(time.monotonic() - started, 1),
                          "file": f"flash-{state['reads']:03d}-{address:08x}.bin", **check_identity(result, name)}
                return data, record

            def program(address, data, name):
                st = board._check_target()
                budget = 2 * WRITE_TIMEOUT + 90
                assert st["privileged_seconds"] >= budget, f"{name}: the local grant ({st['privileged_seconds']} s) is shorter than one write needs ({budget} s)"
                renew()
                artifact = board.client.upload(data)
                state["first_write"] = True
                try:
                    result = board.command("flash.write", {"chip": "esp32p4", "return_console": True, "uart_baud": UART_BAUD,
                                                            "images": [{"address": address, "artifact": artifact["id"], "sha256": artifact["sha256"]}]}, timeout=WRITE_TIMEOUT, allow_destructive=True)
                except OperationError as error:
                    _failure(case, name, error, out)
                    raise
                state["writes"] += 1
                board.client.delete_artifact(artifact["id"])
                return {"name": name, "address": address, "operation": board.last_operation["id"], "verification": (result or {}).get("verification"), **check_identity(result or {}, name)}

            # ---------------------------------------------------------------- A
            state["phase"] = "A"
            progress("phase A: targeted reads")
            reads_a, table = [], None
            for label, address in READ_REGIONS:
                first, r1 = read(address, SECTOR, f"A-{label}-1")
                second, r2 = read(address, SECTOR, f"A-{label}-2")
                assert first == second and r1["sha256"] == r2["sha256"], f"A: the two independent reads of {label} {address:#x} differ"
                reads_a.append({"region": label, "address": address, "size": SECTOR, "sha256": r1["sha256"], "equal": True, "operations": [r1["operation"], r2["operation"]],
                                "jedec_id": r1["jedec_id"], "uart_baud": r1["uart_baud"], "seconds": [r1["seconds"], r2["seconds"]]})
                if label == "partition-table":
                    table = partition_table(first)
                out.evidence.append(f"A {label} {address:#x}: two reads equal sha256={r1['sha256']}")
            case.write("phase-A.json", {"reads": reads_a, "partition_table": table, "note": "structure only; a blank flash or an absent table is an observation"})
            mark("A", "PASS")

            # ---------------------------------------------------------------- B
            state["phase"] = "B"
            progress("phase B: two full images")
            plan = plan_segments(FLASH, segment_size)
            backups, begun = {}, time.monotonic()
            for letter in ("A", "B"):
                rows = []
                for index, address, size in plan:
                    data, rec = read(address, size, f"B-{letter}-seg{index:03d}")
                    rows.append({"index": index, "address": address, "size": size, "sha256": rec["sha256"], "operation": rec["operation"], "file": rec["file"]})
                    if letter == "A" and index == 0:
                        projected = (rec["seconds"] + 6) * len(plan) * 2
                        left = bench_client.device(cfg["board_id"])["privileged_seconds"]
                        need = projected + 15 * 60 + 12 * (2 * WRITE_TIMEOUT + 90)
                        out.evidence.append(f"first segment {rec['seconds']} s: projected {int(projected)} s for both images, grant left {left} s, needed {int(need)} s")
                        assert rec["seconds"] < READ_TIMEOUT * 0.85, f"B: a {size}-byte segment took {rec['seconds']} s: too close to the {READ_TIMEOUT} s operation limit"
                        if left < need:
                            raise Blocked(f"the local grant ({left} s) would not outlast the campaign (about {int(need)} s needed): stopped after one read, no write was allowed")
                    if index % 4 == 3 or index == len(plan) - 1:
                        progress(f"phase B image {letter}: segment {index + 1}/{len(plan)}, {int(time.monotonic() - begun)} s elapsed")
                document = backup_manifest(evidence_dir, FLASH, segment_size, rows, epoch=health["epoch"], generation=device["generation"], device=cfg["board_id"])
                global_sha = verify_backup(evidence_dir, document, expected_total=FLASH)
                document["global_sha256"], document["assembled"] = global_sha, f"BACKUP-{letter}.bin"
                assert assemble(evidence_dir, document, name=document["assembled"]) == global_sha
                assert verify_backup(evidence_dir, document, expected_total=FLASH) == global_sha
                case.write(f"manifest-{letter}.json", document)
                backups[letter] = {"manifest": document, "path": evidence_dir / document["assembled"], "sha256": global_sha}
                out.evidence.append(f"B BACKUP-{letter}: {FLASH} bytes in {len(plan)} segments sha256={global_sha}")
            mark("B", "PASS")

            # ---------------------------------------------------------------- C
            state["phase"] = "C"
            progress("phase C: validation of the backups")
            a, b = backups["A"], backups["B"]
            comparison = compare_files(a["path"], b["path"])
            segments_equal = [x["sha256"] for x in a["manifest"]["segments"]] == [x["sha256"] for x in b["manifest"]["segments"]]
            readable = {n: (x["path"].stat().st_size, sha256_file(x["path"])) for n, x in backups.items()}
            case.write("phase-C.json", {"comparison": comparison, "segment_sha256_equal": segments_equal, "global_sha256": {n: x["sha256"] for n, x in backups.items()},
                                        "re_read_from_disk": {n: {"size": s, "sha256": h} for n, (s, h) in readable.items()}, "locations": {n: str(x["path"]) for n, x in backups.items()}})
            assert comparison["equal"] and segments_equal and a["sha256"] == b["sha256"], f"C: BACKUP-A and BACKUP-B differ ({comparison})"
            assert all(s == FLASH and h == backups[n]["sha256"] for n, (s, h) in readable.items()), "C: a backup file re-read from the persistent storage differs from its verified hash"
            inv = inventory(a["path"])
            case.write("inventory.json", inv)
            out.evidence.append(f"C BACKUP-A == BACKUP-B sha256={a['sha256']}; non-0xFF bytes {inv['non_ff_total']}; partition table: {'present' if inv['partition_table'] else 'absent (an observation, not a failure)'}")
            for label, address in D_SECTORS:
                critical, why = boot_critical(address, inv["partition_table"])
                if critical:
                    raise Blocked(f"write sector {label} {address:#x} is boot-critical ({why}): no write was made")
            mark("C", "PASS")

            # ---------------------------------------------------------------- D
            state["phase"] = "D"
            progress("phase D: erase / program / read back / restore")
            image = a["path"]

            def reference(address):
                with image.open("rb") as stream:
                    stream.seek(address)
                    return stream.read(SECTOR)

            def neighbours(address):
                return [x for x in (address - SECTOR, address + SECTOR) if 0 <= x <= FLASH - SECTOR]

            writes = len(D_SECTORS) * len(WRITE_STEPS)
            left = bench_client.device(cfg["board_id"])["privileged_seconds"]
            if left < (2 * WRITE_TIMEOUT + 90) * 2:
                raise Blocked(f"the local grant ({left} s) is too short to write: no write was made")
            baseline = {}
            for label, address in D_SECTORS:
                twin = alias(address)                                          # 16-MiB wrap guard: never write where a wrapped address could land on a boot sector unseen
                if reference(address) == reference(twin):
                    critical, why = boot_critical(twin, inv["partition_table"])
                    if critical or set(reference(twin)) != {0xFF}:
                        raise Blocked(f"{label} {address:#x} holds the same bytes as {twin:#x} ({why if critical else 'not blank'}): a 16-MiB address wrap could not be excluded; no write was made")
                for target in [address] + neighbours(address) + [twin]:
                    data, rec = read(target, SECTOR, f"D-{label}-before-{target:08x}")
                    assert data == reference(target), f"D: {label} sector {target:#x} differs from BACKUP-A before any write; nothing was written"
                    baseline[target] = rec["sha256"]
                assert pattern_a(address) != reference(address) and pattern_b(address) != reference(address), f"D: {label} already holds a test pattern; nothing would be proven"
            out.restoration = "sectors D1-D3 may hold test patterns or blanks; restore explicitly from BACKUP-A (" + str(image) + ")"      # set BEFORE the first mutation
            steps = []
            for label, address in D_SECTORS:
                for step in WRITE_STEPS:
                    data = {"pattern-A": pattern_a, "pattern-B": pattern_b, "blank": lambda _a: blank(), "restore": reference}[step](address)
                    wrote = program(address, data, f"D-{label}-{step}-write")
                    back, rec = read(address, SECTOR, f"D-{label}-{step}-readback")
                    assert back == data, f"D: {label} {step}: the readback differs from the bytes written"
                    if step == "blank":
                        assert back == blank() and set(back) == {0xFF}, f"D: {label}: the sector is not 0xFF after the erase"
                    if step == "pattern-A":
                        twin_now, twin_rec = read(alias(address), SECTOR, f"D-{label}-alias-after-{step}")
                        assert twin_now == reference(alias(address)), f"D: {label}: the 16-MiB alias {alias(address):#x} changed after the first write (address wrap): stop, restore explicitly from BACKUP-A"
                    steps.append({"sector": label, "address": address, "step": step, "expected_sha256": hashlib.sha256(data).hexdigest(), "readback_sha256": rec["sha256"], "write": wrote})
                    progress(f"phase D {label} {step}: written and read back equal")
            final = {}
            for label, address in D_SECTORS:
                for target in [address] + neighbours(address) + [alias(address)]:
                    data, rec = read(target, SECTOR, f"D-{label}-final-{target:08x}")
                    assert data == reference(target) and rec["sha256"] == baseline[target], f"D: {label} {target:#x} differs from its initial content after the campaign"
                    final[f"{target:#x}"] = rec["sha256"]
            case.write("phase-D.json", {"sectors": [[n, a_] for n, a_ in D_SECTORS], "initial_sha256": {f"{k:#x}": v for k, v in baseline.items()}, "steps": steps, "final_sha256": final,
                                        "note": "initial content shown by SHA-256 only; neighbours of every sector re-read after the campaign"})
            out.restoration = "restored: every write sector and its neighbours read back equal to BACKUP-A"
            mark("D", "PASS")
            state["phase"] = "done"

        operator_gesture(case, "REVOKE the grant locally in the page (the campaign is over)", timeout=section["revoke_wait_s"])
        deadline = time.monotonic() + section["revoke_wait_s"]
        while bench_client.device(cfg["board_id"])["privileged_seconds"] > 0 and time.monotonic() < deadline:
            time.sleep(2)
        remaining = bench_client.device(cfg["board_id"])["privileged_seconds"]
        if remaining > 0:
            out.incident("operator", f"the local grant is still live ({remaining} s): revoke it in the page; the report is complete")
        case.write("campaign-summary.json", {"phases": phases, "backup_sha256": {n: x["sha256"] for n, x in backups.items()}, "backup_locations": {n: str(x["path"]) for n, x in backups.items()},
                                             "reads": state["reads"], "writes": state["writes"], "grant_left_at_end_s": remaining, "elapsed_s": int(time.monotonic() - grant_seen)})
        out.evidence.append(f"phases {phases}; {state['reads']} reads, {state['writes']} writes; grant left at the end {remaining} s")
